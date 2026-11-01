"""Inventory → SKU planning: the stockkeeping units (SKU cards) of every location against what the location
actually uses, with a recommendation for each item.

Business Central plans replenishment per item and location from the SKU card (Reordering Policy, Reorder
Point, Reorder Quantity, Maximum Inventory, Safety Stock, Replenishment System, Transfer-from, Lead Time).
Most cards are empty, some were filled years ago; this screen puts them next to the last weeks of use.

Use (the item ledger summed by day in data/movement.db, the same file as Movement & dead stock), per location
and item, over the window (Settings, default 60 days, ending yesterday):
  * demand = sales net of returns + consumption by recipes (assembly); at the distribution locations
    (Settings, default MAGAZINE, CENTRAL) also the transfers out, which supply the restaurants;
    adjustments are not demand;
  * supply seen = purchase receipts, transfers in (and the distribution location that shipped the same item the
    same day), output of made items.
Recommendation (statistical):
  * average daily demand d and its day-to-day spread s (zero days included);
  * lead time L in days (Settings: a default, and per location, e.g. MAGAZINE=45 for imports; fresh families
    always take the default, being bought locally);
  * delivery cycle R = the days between deliveries seen in the window (Settings default when none were seen);
  * safety stock = z × s × √L for the service level (Settings, default 95% → z 1.645);
  * reorder point = d × L + safety stock;
  * restaurants and other locations supplied by transfer: Reordering Policy "Maximum Qty.",
    Maximum Inventory = reorder point + d × R, Replenishment "Transfer" from the location that ships it;
  * distribution locations that buy: "Fixed Reorder Qty.", Reorder Quantity = the larger of d × R and the usual
    receipt seen (the supplier's pack), Replenishment "Purchase";
  * items made at the location (output): Replenishment "Assembly", "Maximum Qty.";
  * fresh families (Settings → typology, fresh families): the maximum never covers more than the fresh limit.
Statuses: missing card (the item moves, no SKU at the location), empty card, incomplete card (a policy without
its quantities), off (reorder point or maximum away from the recommendation by more than the tolerance), in line,
set but unused (parameters on a card with no use in the window: review), low use (too few days of use to plan).
Business Central stays read-only: the recommendations go to Excel, to be checked and imported (Edit in Excel or
a configuration package).
"""
import fnmatch
import json
import math
import threading
import time
import traceback
from datetime import date, datetime, timedelta
from statistics import median

import config
import locintel
import movement
import recipes
import store

_jobs, _lock = {}, threading.Lock()
KEEP = 1800
CARD_FILE = store.DATA_DIR / "sku_cards.json"
CARD_TTL = 6 * 3600
_card_lock = threading.Lock()
CARD_SELECT = ("Location_Code,Item_No,Variant_Code,Description,Inventory,Replenishment_System,Lead_Time_Calculation,"
               "Vendor_No,Transfer_from_Code,Reordering_Policy,Safety_Lead_Time,Safety_Stock_Quantity,Reorder_Point,"
               "Reorder_Quantity,Maximum_Inventory,Minimum_Order_Quantity,Order_Multiple,Last_Date_Modified")
Z = [(90, 1.282), (95, 1.645), (97.5, 1.96), (98, 2.054), (99, 2.326), (99.5, 2.576)]
FRACTIONAL = {"KG", "KGM", "L", "LT", "LTR", "LITER", "LITRE", "M", "M2", "M3", "GR", "G", "ML", "CL"}
STATUS_ORDER = ("missing", "empty", "incomplete", "off", "unused", "ok", "low_use")
STATUS_LABEL = {"missing": "No SKU card", "empty": "Empty card", "incomplete": "Incomplete card",
                "off": "Off the use", "unused": "Set but unused", "ok": "In line", "low_use": "Low use"}


# ---------------------------------------------------------------- settings
def params(settings=None):
    inv = (settings or store.get_settings()).get("inventory") or {}

    def num(k, d):
        try:
            return float(str(inv.get(k, d)).strip())
        except (TypeError, ValueError):
            return d
    lead_text = str(inv.get("sku_lead_days") or "2, MAGAZINE=45")
    lead_default, lead_by = 2.0, {}
    for part in lead_text.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            k, _, v = part.partition("=")
            try:
                lead_by[k.strip().upper()] = float(v)
            except ValueError:
                pass
        else:
            try:
                lead_default = float(part)
            except ValueError:
                pass
    service = num("sku_service_level", 95)
    z = min(Z, key=lambda t: abs(t[0] - service))[1]

    def lst(k, d):
        return {p.strip().upper() for p in str(inv.get(k) if inv.get(k) is not None else d).split(",") if p.strip()}
    return {"window": int(num("sku_window_days", 60)), "service": service, "z": z, "lead_default": lead_default,
            "lead_by": lead_by, "review_default": num("sku_review_days", 3), "fresh_max_days": num("sku_fresh_max_days", 4),
            "tolerance": num("sku_tolerance_pct", 50), "min_days": int(num("sku_min_use_days", 5)),
            "lead_text": lead_text,
            "distribution": lst("distribution_locations", "MAGAZINE, CENTRAL"),
            "ex_locs": lst("movement_exclude_locations", "*_IM, *_IN, TRANSIT"),
            "ex_cats": lst("movement_exclude_categories", "INVENTAR I IMET"),
            "fresh": lst("turnover_fresh_families", "PERIME&FRUTE, MISH&SHPENDE, PESHK&PROD.DETI, BULMET&DJATHE, "
                                                     "PROD BUKE&FURRE")}


# ---------------------------------------------------------------- the SKU cards
def _card_cache():
    try:
        return json.loads(CARD_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def cards(client, company, force=False):
    """Every SKU card of the company (about 34,000 at SALT), kept six hours in data/sku_cards.json."""
    with _card_lock:
        cache = _card_cache()
    hit = cache.get(company)
    if hit and not force and time.time() - hit.get("at", 0) < CARD_TTL and hit.get("select") == CARD_SELECT:
        return hit
    rows = client._get_all(client._url(company, config.E_SKU), {"$select": CARD_SELECT})
    keep = [f for f in CARD_SELECT.split(",") if f not in ("Location_Code", "Item_No")]
    value = {"at": time.time(), "read_at": datetime.now().isoformat(timespec="seconds"), "select": CARD_SELECT,
             "fields": keep,
             "cards": [[str(r.get("Location_Code") or ""), str(r.get("Item_No") or "")] + [r.get(f) for f in keep]
                       for r in rows if r.get("Item_No")]}
    with _card_lock:
        cache = _card_cache()
        cache[company] = value
        CARD_FILE.parent.mkdir(exist_ok=True)
        tmp = CARD_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache), encoding="utf-8")
        tmp.replace(CARD_FILE)
    return value


# ---------------------------------------------------------------- helpers
def _f(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _round(q, uom):
    """Up, to a quantity Business Central takes: whole units, or one decimal for weights and volumes."""
    if q <= 0:
        return 0
    if (uom or "").upper() in FRACTIONAL:
        return math.ceil(q * 10) / 10 if q < 100 else math.ceil(q)
    return math.ceil(q)


def _policy(p):
    p = (p or "").strip()
    return "" if p in ("", " ") else p


def _set(c):
    """Whether a card carries planning parameters."""
    return bool(_policy(c["policy"]) or c["rop"] or c["max"] or c["ss"] or c["rq"])


def _far(cur, rec, tol):
    """cur and rec differ by more than tol % (of the larger), when the recommendation is not nothing."""
    if rec <= 0 and cur <= 0:
        return False
    big = max(abs(cur), abs(rec))
    return abs(cur - rec) / big * 100 > tol if big else False


# ---------------------------------------------------------------- the analysis
def build(client, company, progress, force=False):
    settings = store.get_settings()
    P = params(settings)
    today = date.today()
    end = today - timedelta(days=1)
    start = end - timedelta(days=P["window"] - 1)
    W = P["window"]

    progress("Business Central: SKU cards")
    cd = cards(client, company, force)
    progress("Business Central: item cards")
    master = recipes.master(client, company)
    items = master["items"]
    progress("Business Central: item ledger of the window")
    errors = []
    for mo in recipes.months_between(start, end):
        try:
            movement.load_month(client, company, mo, force=force)
        except Exception as exc:                                    # noqa: BLE001
            errors.append(f"ledger {mo}: {str(exc)[:200]}")
    progress("Working out the recommendations")
    with movement._lock:
        rows = movement._db().execute(
            "SELECT location, item, kind, day, qty FROM mv WHERE company=? AND day>=? AND day<=?",
            (company, start.isoformat(), end.isoformat())).fetchall()

    def left_out(loc):
        u = (loc or "").upper()
        return not u or any(fnmatch.fnmatchcase(u, p) for p in P["ex_locs"])
    dist = P["distribution"]
    use = {}                                        # (loc, item) -> {day: demand}
    rec_days, xin_days, out_days = {}, {}, {}       # (loc, item) -> {day: qty}
    tout = {}                                       # (item, day) -> {distribution loc: qty out}
    for loc, it, kind, day, qty in rows:
        if left_out(loc):
            continue
        k = (loc, it)
        d = 0.0
        if kind in ("sale", "consumption"):
            d = -qty
        elif kind == "sale_return":
            d = -qty                                # a return (positive quantity) takes demand back
        elif kind == "transfer_out" and loc.upper() in dist:
            d = -qty
        if kind == "transfer_out" and loc.upper() in dist:
            x = tout.setdefault((it, day), {})
            x[loc] = x.get(loc, 0) - qty
        if d:
            u = use.setdefault(k, {})
            u[day] = u.get(day, 0) + d
        if kind == "receipt" and qty > 0:
            r = rec_days.setdefault(k, {})
            r[day] = r.get(day, 0) + qty
        elif kind == "transfer_in" and qty > 0:
            r = xin_days.setdefault(k, {})
            r[day] = r.get(day, 0) + qty
        elif kind == "output" and qty > 0:
            r = out_days.setdefault(k, {})
            r[day] = r.get(day, 0) + qty

    reg = [r for r in locintel.registry(settings) if (r.get("company") or "").upper() == (company or "").upper()]
    reg_by_bc = {(r.get("bc") or "").upper(): r for r in reg if r.get("bc")}
    fields = cd["fields"]
    fi = {f: i for i, f in enumerate(fields)}
    card_by = {}
    for c in cd["cards"]:
        loc, it = c[0], c[1]
        if c[2 + fi["Variant_Code"]]:
            continue                                # variants are planned on their own; left as they are
        g = lambda f: c[2 + fi[f]]                  # noqa: E731
        card_by[(loc, it)] = {"policy": _policy(g("Reordering_Policy")), "rop": _f(g("Reorder_Point")),
                              "rq": _f(g("Reorder_Quantity")), "max": _f(g("Maximum_Inventory")),
                              "ss": _f(g("Safety_Stock_Quantity")), "repl": g("Replenishment_System") or "",
                              "from": g("Transfer_from_Code") or "", "lead": g("Lead_Time_Calculation") or "",
                              "vendor": g("Vendor_No") or "", "inv": _f(g("Inventory")),
                              "modified": str(g("Last_Date_Modified") or "")[:10], "desc": g("Description") or ""}

    keys = set(use) | set(rec_days) | set(xin_days) | set(out_days) | set(card_by)
    out = {}
    for (loc, it) in keys:
        if left_out(loc):
            continue
        card = items.get(it) or {}
        if str(card.get("Type") or "Inventory") not in ("Inventory", ""):
            continue                                # service and non-inventory items are not stocked
        if (card.get("Assembly_Policy") or "") == "Assemble-to-Order":
            continue                                # menu items made when sold: nothing to stock
        cat = str(card.get("Item_Category_Code") or "").upper()
        if cat in P["ex_cats"]:
            continue
        c = card_by.get((loc, it))
        series = use.get((loc, it)) or {}
        total = sum(series.values())
        if total <= 0 and not (c and _set(c)):
            continue                                # nothing used, nothing set: not a planning question
        uom = str(card.get("Base_Unit_of_Measure") or "").upper()
        fam = str(card.get("Item_Category_Group_4") or "").upper()
        d = max(total, 0) / W
        var = sum((series.get((start + timedelta(days=i)).isoformat(), 0) - d) ** 2 for i in range(W)) / W
        sd = math.sqrt(var)
        active = sum(1 for v in series.values() if v > 0)
        rec, xin, made = rec_days.get((loc, it)) or {}, xin_days.get((loc, it)) or {}, out_days.get((loc, it)) or {}
        deliveries = len(set(rec) | set(xin) | set(made))
        # where the supply comes from
        if made and len(made) >= max(len(rec), len(xin)):
            repl, src = "Assembly", ""
        elif xin and len(xin) >= len(rec):
            by = {}
            for day in xin:
                for s_loc, q in (tout.get((it, day)) or {}).items():
                    if s_loc != loc:
                        by[s_loc] = by.get(s_loc, 0) + 1
            repl, src = "Transfer", (max(by, key=by.get) if by else "")
        elif rec:
            repl, src = "Purchase", ""
        else:
            repl = (c or {}).get("repl") or ("Purchase" if loc.upper() in dist else "Transfer")
            src = (c or {}).get("from") or ""
        fresh = fam in P["fresh"]
        # fresh food is bought locally and often: the location's import lead time (MAGAZINE=45) is not its lead time
        lead = P["lead_default"] if fresh else P["lead_by"].get(loc.upper(), P["lead_default"])
        cycle = round(W / deliveries, 1) if deliveries else P["review_default"]
        cycle = max(1.0, min(cycle, 30.0))
        ss = P["z"] * sd * math.sqrt(lead)
        rop = d * lead + ss
        # fresh families: never more than the fresh limit in days of use above the reorder point
        mx = rop + d * (min(cycle, P["fresh_max_days"]) if fresh else cycle)
        rq = 0.0
        if repl == "Purchase" and loc.upper() in dist:
            policy = "Fixed Reorder Qty."
            usual = median(rec.values()) if rec else 0
            rq = max(d * cycle, usual)
            mx = 0.0
        else:
            policy = "Maximum Qty."
        R = {"policy": policy, "rop": _round(rop, uom), "ss": _round(ss, uom), "max": _round(mx, uom),
             "rq": _round(rq, uom), "repl": repl, "from": src if repl == "Transfer" else "",
             "lead": f"{int(lead) if lead == int(lead) else lead}D"}
        if R["max"] and R["max"] <= R["rop"]:
            R["max"] = R["rop"] + (0.1 if uom in FRACTIONAL and R["rop"] < 100 else 1)   # a maximum above the point
        # status
        if active < P["min_days"]:
            status = "unused" if (c and _set(c) and total <= 0) else "low_use"
        elif not c:
            status = "missing"
        elif not _set(c):
            status = "empty"
        elif (c["policy"] == "Fixed Reorder Qty." and (not c["rq"] or not c["rop"])) or \
                (c["policy"] == "Maximum Qty." and not c["max"]) or (not c["policy"]):
            status = "incomplete"
        elif _far(c["rop"], R["rop"], P["tolerance"]) or \
                (R["max"] and c["policy"] == "Maximum Qty." and _far(c["max"], R["max"], P["tolerance"])) or \
                (R["rq"] and c["policy"] == "Fixed Reorder Qty." and _far(c["rq"], R["rq"], P["tolerance"])):
            status = "off"
        else:
            status = "ok"
        if total <= 0 and c and _set(c):
            status = "unused"
        cost = _f(card.get("Unit_Cost")) or _f(card.get("Last_Direct_Cost"))
        inv = c["inv"] if c else None
        L = out.get(loc)
        if L is None:
            r = reg_by_bc.get(loc.upper())
            L = out[loc] = {"name": (r or {}).get("name") or loc,
                            "group": "restaurant" if r else ("warehouse" if loc.upper() in dist else "other"),
                            "brand": (r or {}).get("brand") or "", "cards": 0, "with_parameters": 0, "moving": 0,
                            "counts": {s: 0 for s in STATUS_ORDER}, "use_value": 0.0, "fix_value": 0.0,
                            "lead": lead, "rows": []}
        L["counts"][status] += 1
        if total > 0:
            L["moving"] += 1
            L["use_value"] += d * 30 * cost
            if status in ("missing", "empty", "incomplete", "off"):
                L["fix_value"] += d * 30 * cost
        L["rows"].append([it, (c or {}).get("desc") or card.get("Description") or "", fam or cat, uom,
                          round(d, 4), round(sd, 4), active, deliveries, round(cycle, 1), lead,
                          {k: (c or {}).get(k) for k in ("policy", "rop", "rq", "max", "ss", "repl", "from", "lead",
                                                        "vendor", "modified")} if c else None,
                          R, status, inv, round(inv / d, 1) if (inv is not None and d > 0) else None,
                          round(d * 30 * cost), fresh])
    # cards and parameters per location (all cards, used or not)
    for (loc, it), c in card_by.items():
        L = out.get(loc)
        if L is None:
            continue
        L["cards"] += 1
        L["with_parameters"] += 1 if _set(c) else 0
    for L in out.values():
        L["rows"].sort(key=lambda x: (STATUS_ORDER.index(x[12]), -x[15]))
        need = L["moving"] - L["counts"]["low_use"]
        L["coverage"] = round(L["counts"]["ok"] / need * 100, 1) if need > 0 else None
        L["use_value"], L["fix_value"] = round(L["use_value"]), round(L["fix_value"])
        al = []
        if L["counts"]["missing"]:
            al.append(["warn", f"{L['counts']['missing']} moving items without an SKU card"])
        if L["counts"]["empty"] + L["counts"]["incomplete"]:
            al.append(["warn", f"{L['counts']['empty'] + L['counts']['incomplete']} cards empty or incomplete"])
        if L["counts"]["off"]:
            al.append(["info", f"{L['counts']['off']} cards off the actual use"])
        if L["counts"]["unused"]:
            al.append(["info", f"{L['counts']['unused']} cards set but not used in {W} days"])
        L["alerts"] = al
    return {"company": company, "window": {"from": start.isoformat(), "to": end.isoformat(), "days": W},
            "params": {k: (sorted(v) if isinstance(v, set) else v) for k, v in P.items()},
            "cards_read_at": cd.get("read_at"), "cards_total": len(cd["cards"]), "read_at": time.strftime("%H:%M"),
            "statuses": STATUS_LABEL, "locations": out, "errors": errors, "definitions": DEFINITIONS}


# ---------------------------------------------------------------- background job
def request(client, company, refresh=False):
    key = company or ""
    now = time.time()
    with _lock:
        job = _jobs.get(key)
        if job and job["state"] == "running":
            return {"pending": True, "stage": job.get("stage", ""), "elapsed": int(now - job["started"])}
        if job and job["state"] == "done" and not refresh and now - job["finished"] < KEEP:
            return summary(job["result"])
        if job and job["state"] == "failed" and not refresh and now - job["finished"] < 60:
            return {"available": False, "error": job["error"]}
        job = _jobs[key] = {"state": "running", "started": now, "stage": "Starting"}

    def progress(text):
        with _lock:
            job["stage"] = text

    def run():
        try:
            r = build(client, company, progress, force=refresh)
            with _lock:
                job.update(state="done", finished=time.time(), result=r)
        except Exception as exc:                                   # noqa: BLE001
            traceback.print_exc()
            with _lock:
                job.update(state="failed", finished=time.time(), error=str(exc)[:300])
    threading.Thread(target=run, daemon=True, name="skuplan").start()
    return {"pending": True, "stage": "Starting", "elapsed": 0}


def held(company):
    with _lock:
        job = _jobs.get(company or "")
        return job.get("result") if job and job.get("state") == "done" else None


def summary(r):
    """The screen's answer: every location without its item rows (they come with the popup)."""
    return {**{k: v for k, v in r.items() if k != "locations"},
            "locations": {c: {k: v for k, v in L.items() if k != "rows"} for c, L in r["locations"].items()}}


def location_rows(r, code):
    L = r["locations"].get(code)
    return {"code": code, "name": L["name"], "rows": L["rows"]} if L else None


# ---------------------------------------------------------------- Excel for Business Central
IMPORT_HEAD = ["Location Code", "Item No.", "Variant Code", "Description", "Replenishment System", "Transfer-from Code",
               "Lead Time Calculation", "Reordering Policy", "Safety Stock Quantity", "Reorder Point", "Reorder Quantity",
               "Maximum Inventory", "Action", "Status", "Avg use / day", "Days used", "Deliveries"]


def render_xlsx(r, code=None, statuses=None):
    from io import BytesIO
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    want = set(statuses or ("missing", "empty", "incomplete", "off", "unused"))
    wb = Workbook()
    ws = wb.active
    ws.title = "SKU import"
    ws.append(IMPORT_HEAD)
    basis = wb.create_sheet("Basis")
    basis.append(["Location Code", "Item No.", "Description", "Family / category", "Base UoM", "Status",
                  "Avg use / day", "Spread / day", "Days used", "Deliveries", "Delivery cycle (days)",
                  "Lead time (days)", "Fresh", "Inventory now", "Days of cover now", "Use value / 30 days",
                  "Current policy", "Current reorder point", "Current reorder qty", "Current maximum",
                  "Current safety stock", "Current replenishment", "Current transfer-from", "Card last modified",
                  "Recommended policy", "Recommended reorder point", "Recommended reorder qty", "Recommended maximum",
                  "Recommended safety stock", "Recommended replenishment", "Recommended transfer-from"])
    bold = Font(bold=True)
    head_fill = PatternFill("solid", fgColor="EFEDE6")
    for sh in (ws, basis):
        for cell in sh[1]:
            cell.font, cell.fill = bold, head_fill
    for loc in sorted(r["locations"]):
        if code and loc != code:
            continue
        for x in r["locations"][loc]["rows"]:
            it, desc, fam, uom, d, sd, active, deliv, cycle, lead, cur, R, status, inv, cover, val, fresh = x
            cur = cur or {}
            basis.append([loc, it, desc, fam, uom, STATUS_LABEL[status], d, sd, active, deliv, cycle, lead,
                          "yes" if fresh else "", inv, cover, val, cur.get("policy"), cur.get("rop"), cur.get("rq"),
                          cur.get("max"), cur.get("ss"), cur.get("repl"), cur.get("from"), cur.get("modified"),
                          R["policy"], R["rop"], R["rq"], R["max"], R["ss"], R["repl"], R["from"]])
            if status not in want:
                continue
            if status == "unused":
                ws.append([loc, it, "", desc, cur.get("repl") or "", cur.get("from") or "", cur.get("lead") or "",
                           "", 0, 0, 0, 0, "Review: clear the policy (no use in the window)", STATUS_LABEL[status],
                           d, active, deliv])
                continue
            ws.append([loc, it, "", desc, R["repl"], R["from"], R["lead"], R["policy"], R["ss"], R["rop"], R["rq"],
                       R["max"], "Create the SKU card" if status == "missing" else "Update the card",
                       STATUS_LABEL[status], d, active, deliv])
    for sh, widths in ((ws, [14, 12, 8, 36, 14, 14, 10, 18, 12, 12, 12, 12, 34, 16, 12, 10, 10]),
                       (basis, [14, 12, 36, 18, 8, 16] + [12] * 25)):
        for i, w in enumerate(widths):
            sh.column_dimensions[chr(65 + i) if i < 26 else "A" + chr(65 + i - 26)].width = w
        sh.freeze_panes = "C2"
    notes = wb.create_sheet("Read me")
    for line in [
        "SKU planning from pulse (Finance → Inventory → SKU planning). Business Central was only read.",
        f"Company {r['company']}, use of {r['window']['from']} to {r['window']['to']} ({r['window']['days']} days).",
        "Sheet 'SKU import': one line per card to create or update, in the columns of the Stockkeeping Unit list "
        "(page 5700). Check the lines, then import with Edit in Excel or a configuration package (table 5700 "
        "Stockkeeping Unit); new cards (Action 'Create the SKU card') need inserting, not only modifying.",
        "Quantities are in the item's base unit of measure.",
        "Sheet 'Basis': every item looked at, with its use, the current card and the recommendation.",
    ] + [f"{k}: {v}" for k, v in DEFINITIONS.items()]:
        notes.append([line])
    notes.column_dimensions["A"].width = 160
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


DEFINITIONS = {
    "use": "Use = sales net of returns and consumption by recipes (assembly), day by day, over the window ending "
           "yesterday; at the distribution locations (Settings → Inventory) also the transfers out to the restaurants. "
           "Adjustments are not use. Quantities in the item's base unit.",
    "method": "Recommendation: average use per day d and its day-to-day spread s (days without use count as zero); "
              "safety stock = z × s × √(lead time) for the service level in Settings; reorder point = d × lead time + "
              "safety stock; maximum = reorder point + d × delivery cycle (the days between deliveries seen in the "
              "window); for fresh families the maximum holds at most the fresh limit in days of use above the reorder "
              "point, and their lead time is the default (bought locally).",
    "policy": "Policy: locations supplied by transfer (restaurants) and items made at the location get 'Maximum Qty.' "
              "(top up to the maximum when stock falls to the reorder point), with the location that shipped them as "
              "Transfer-from; distribution locations that buy get 'Fixed Reorder Qty.' with the reorder quantity = the "
              "larger of d × cycle and the usual receipt (the supplier's pack).",
    "status": "Status: No SKU card = the item is used at the location and has no card; Empty card = a card without a "
              "policy or quantities; Incomplete = a policy without the quantity it needs; Off the use = reorder point, "
              "maximum or reorder quantity away from the recommendation by more than the tolerance in Settings; Set but "
              "unused = parameters on a card with no use in the window (review: probably clear them); Low use = used on "
              "fewer days than the minimum in Settings, too little to plan (order on demand).",
    "scope": "Left out: locations and categories excluded in Settings → Inventory (equipment, goods in transit), "
             "service and non-inventory items, menu items made to order (Assemble-to-Order), item variants.",
}
