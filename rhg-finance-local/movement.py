"""Inventory movement and dead stock (Inventory Control, phase 3).

Extends the existing stock classes of the Inventory screen with what the item ledger says
about each item at each location:

- Quantity on hand at a date, per location: the item card's InventorybyDate flow field,
  evaluated by BC for the location (Verified). Value = quantity x the item's unit cost
  (Estimated: the posted inventory value is in the value entries and the G/L, which the
  Inventory screen reconciles).
- Movements: item ledger entries summed by day, location, item and kind (purchase receipt,
  transfer in/out, sale, assembly consumption/output, positive/negative adjustment,
  returns). Kept in data/movement.db. Months the Inventory screen already holds are summed
  from the local file; older months are read once from BC and summed as they arrive.

Qualifying consumption (what makes stock "move"), per the Inventory prompt v2:
- Assembly Consumption and Sale (net of sales returns) always count.
- Transfers out count only at distribution locations (Settings, default MAGAZINE, CENTRAL),
  whose job is to supply the restaurants. Elsewhere a transfer is not consumption.
- Negative adjustments count only if Settings says so (default no) until RHG decides which
  adjustments are counts, corrections or waste.
- Purchases, transfers in, positive adjustments and assembly output never count.

Nothing is written to Business Central.
"""
import fnmatch
import io
import json
import sqlite3
import threading
import time
import traceback
from calendar import monthrange
from datetime import date, datetime, timedelta

import config
import localdb
import recipes
import store

DB_PATH = store.DATA_DIR / "movement.db"
KINDS = ("receipt", "purchase_return", "sale", "sale_return", "consumption", "output",
         "transfer_in", "transfer_out", "adj_pos", "adj_neg", "other")
WINDOWS = (7, 30, 60, 90, 180, 365)
_lock = threading.Lock()
_conn = None
_jobs, _jobs_lock = {}, threading.Lock()
_results = {}


def _db():
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript("""
            CREATE TABLE IF NOT EXISTS mv (company TEXT, day TEXT, location TEXT, item TEXT, kind TEXT,
                                           qty REAL, cost REAL, n INTEGER);
            CREATE INDEX IF NOT EXISTS mv_day ON mv (company, day);
            CREATE TABLE IF NOT EXISTS loaded (company TEXT, month TEXT, date_to TEXT, at REAL,
                                               PRIMARY KEY (company, month));
        """)
    return _conn


def kind_of(entry_type, qty):
    t = (entry_type or "").strip()
    if t == "Purchase":
        return "receipt" if qty > 0 else "purchase_return"
    if t == "Sale":
        return "sale" if qty < 0 else "sale_return"
    if t in ("Assembly Consumption", "Consumption"):
        return "consumption"
    if t in ("Assembly Output", "Output"):
        return "output"
    if t == "Transfer":
        return "transfer_in" if qty > 0 else "transfer_out"
    if t == "Positive Adjmt.":
        return "adj_pos"
    if t == "Negative Adjmt.":
        return "adj_neg"
    return "other"


def _month_bounds(month, today):
    y, m = int(month[:4]), int(month[5:7])
    return date(y, m, 1), date(y, m, monthrange(y, m)[1])


def _fresh(company, month, b, today, force):
    with _lock:
        row = _db().execute("SELECT date_to, at FROM loaded WHERE company=? AND month=?", (company, month)).fetchone()
    if not row:
        return False
    date_to, at = row
    want_to = min(b, today).isoformat()
    if date_to < want_to:
        return False
    age = time.time() - at
    open_month = (today - b).days <= 35
    if force and open_month and age > 120:
        return False
    return age < (3600 if b >= today - timedelta(days=1) else (6 * 3600 if open_month else 30 * 86400))


def load_month(client, company, month, today=None, force=False):
    """Sums one month of item ledger entries into data/movement.db."""
    today = today or date.today()
    a, b = _month_bounds(month, today)
    if a > today or _fresh(company, month, b, today, force):
        return False
    end = min(b, today)
    agg = {}

    def add(rows):
        for r in rows:
            q = float(r.get("Quantity") or 0)
            k = (str(r.get("Posting_Date"))[:10], r.get("Location_Code") or "", str(r.get("Item_No") or ""),
                 kind_of(r.get("Entry_Type"), q))
            x = agg.setdefault(k, [0.0, 0.0, 0])
            x[0] += q
            x[1] += float(r.get("Cost_Amount_Actual") or 0)
            x[2] += 1
    recent = (today - b).days <= 120
    if recent:
        # the months the Inventory screen keeps: topped up in the local file, then summed
        kind = client.hold_item_ledger(company, a.isoformat(), end.isoformat())
        batch = []
        for r in localdb.iter_rows(kind, company, a.isoformat(), end.isoformat()):
            batch.append(r)
            if len(batch) >= 20000:
                add(batch)
                batch = []
        add(batch)
    else:
        client._get_all(client._url(company, config.E_ILE),
                        {"$filter": f"Posting_Date ge {a.isoformat()} and Posting_Date le {end.isoformat()}",
                         "$select": client.ILE_SELECT}, on_page=add)
    with _lock:
        db = _db()
        db.execute("DELETE FROM mv WHERE company=? AND day>=? AND day<=?", (company, a.isoformat(), b.isoformat()))
        db.executemany("INSERT INTO mv VALUES (?,?,?,?,?,?,?,?)",
                       [(company, *k, round(v[0], 4), round(v[1], 2), v[2]) for k, v in agg.items()])
        db.execute("INSERT OR REPLACE INTO loaded VALUES (?,?,?,?)", (company, month, end.isoformat(), time.time()))
        db.commit()
    return True


def months_back(as_of, days):
    start = as_of - timedelta(days=days - 1)
    return recipes.months_between(start, as_of)


# ---------------------------------------------------------------- the analysis
def _list(text):
    return {p.strip().upper() for p in (text or "").split(",") if p.strip()}


def thresholds(settings):
    th = settings["thresholds"]
    inv = settings.get("inventory") or {}
    base = [int(th.get("stock_monitor_days") or 30), int(th.get("stock_slow_days") or 60),
            int(th.get("stock_dead_days") or 90), int(th.get("stock_deadstock_days") or 180)]
    try:
        per = [int(float(x)) for x in (inv.get("perishable_days") or "7, 14, 30, 60").split(",")][:4]
        if len(per) < 4:
            raise ValueError
    except ValueError:
        per = [7, 14, 30, 60]
    return base, per


def analyse(company, as_of, history_days, qty_by_loc, master_data, settings, held_from):
    th = settings["thresholds"]
    inv = settings.get("inventory") or {}
    base, per = thresholds(settings)
    distribution = _list(inv.get("distribution_locations") or "MAGAZINE, CENTRAL")
    # locations and categories that hold equipment or goods in transit, not stock that is
    # consumed (Settings; * is a wildcard). They are left out and reported as left out.
    ex_locs = _list(inv.get("movement_exclude_locations") if inv.get("movement_exclude_locations") is not None
                    else "*_IM, *_IN, TRANSIT")
    ex_cats = _list(inv.get("movement_exclude_categories") if inv.get("movement_exclude_categories") is not None
                    else "INVENTAR I IMET")

    def left_out_loc(loc):
        u = (loc or "").upper()
        return any(fnmatch.fnmatchcase(u, p) for p in ex_locs)
    seasonal = _list(inv.get("seasonal_items"))
    perishable = _list(inv.get("perishable_categories"))
    count_neg = (inv.get("count_negative_adjustments") or "no").strip().lower() in ("yes", "true", "1")
    new_days = int(th.get("stock_new_days") or 30)
    items = master_data["items"]
    start = as_of - timedelta(days=history_days - 1)

    with _lock:
        rows = _db().execute("SELECT location, item, kind, day, qty, cost FROM mv WHERE company=? AND day>=? "
                             "AND day<=?", (company, start.isoformat(), as_of.isoformat())).fetchall()
    stats = {}
    adj = {}
    for loc, it, kind, day, qty, cost in rows:
        s = stats.setdefault((loc, it), {"cons": {w: 0.0 for w in WINDOWS}, "last_cons": "", "last_rcpt": "",
                                         "first_rcpt": "", "first_any": "", "in_qty": 0.0, "kinds": {}})
        age = (as_of - date.fromisoformat(day)).days
        k = s["kinds"].setdefault(kind, [0.0, 0.0])
        k[0] += qty
        k[1] += cost
        s["first_any"] = min(s["first_any"] or day, day)
        qualifies = kind in ("consumption", "sale", "sale_return") or \
            (kind == "transfer_out" and loc.upper() in distribution) or (kind == "adj_neg" and count_neg)
        if qualifies:
            used = -qty                       # outflows are negative quantities; returns reduce use
            for w in WINDOWS:
                if age < w:
                    s["cons"][w] += used
            if used > 0:
                s["last_cons"] = max(s["last_cons"], day)
        if kind in ("receipt", "transfer_in"):
            s["last_rcpt"] = max(s["last_rcpt"], day)
            s["first_rcpt"] = min(s["first_rcpt"] or day, day)
            s["in_qty"] += qty
        if kind in ("adj_pos", "adj_neg") and not left_out_loc(loc):
            a = adj.setdefault(loc, {"location": loc, "pos_qty": 0.0, "pos_cost": 0.0, "neg_qty": 0.0,
                                     "neg_cost": 0.0, "entries": 0})
            if kind == "adj_pos":
                a["pos_qty"] += qty
                a["pos_cost"] += cost
            else:
                a["neg_qty"] += qty
                a["neg_cost"] += cost

    out_rows, negative = [], []
    excluded = {}
    for loc, by_item in qty_by_loc.items():
        loc_out = left_out_loc(loc)
        for it, qty in by_item.items():
            card = items.get(it) or {}
            if loc_out or str(card.get("Item_Category_Code") or "").upper() in ex_cats:
                e = excluded.setdefault(loc if loc_out else f"category {card.get('Item_Category_Code')}", [0, 0.0])
                e[0] += 1
                e[1] += qty * float(card.get("Unit_Cost") or 0)
                continue
            cost = float(card.get("Unit_Cost") or 0)
            s = stats.get((loc, it)) or {"cons": {w: 0.0 for w in WINDOWS}, "last_cons": "", "last_rcpt": "",
                                         "first_rcpt": "", "first_any": "", "in_qty": 0.0, "kinds": {}}
            cat = str(card.get("Item_Category_Code") or "").upper()
            limits = per if cat in perishable else base
            days_without = (as_of - date.fromisoformat(s["last_cons"])).days if s["last_cons"] else None
            beyond = days_without is None
            d = days_without if days_without is not None else history_days
            if qty < 0:
                cls = "NEGATIVE"
            elif it.upper() in seasonal or cat in seasonal:
                cls = "SEASONAL"
            elif s["first_rcpt"] and s["first_rcpt"] == s["first_any"] and \
                    (as_of - date.fromisoformat(s["first_rcpt"])).days < new_days and \
                    s["first_rcpt"] > start.isoformat():
                cls = "NEW ITEM"
            elif d < limits[0]:
                cls = "ACTIVE"
            elif d < limits[1]:
                cls = "MONITOR"
            elif d < limits[2]:
                cls = "SLOW MOVING"
            elif d < limits[3] or (beyond and history_days < limits[3]):
                cls = "NON-MOVING"
            else:
                cls = "DEAD STOCK"
            received_never_used = qty > 0 and bool(s["last_rcpt"]) and not s["last_cons"]
            sev = None
            if received_never_used:
                # how long the stock has been there unused: from the first receipt in the history
                since = (as_of - date.fromisoformat(s["first_rcpt"] or s["last_rcpt"])).days
                sev = "critical" if since >= limits[2] else "warning" if since >= limits[1] else "review"
            row = [loc, it, round(qty, 3), round(qty * cost, 2), round(s["cons"][7], 2), round(s["cons"][30], 2),
                   round(s["cons"][60], 2), round(s["cons"][90], 2), round(s["cons"][180], 2),
                   round(s["cons"][365], 2), s["last_rcpt"], s["last_cons"], days_without, cls,
                   sev or "", cat, "P" if cat in perishable else ""]
            out_rows.append(row)
            if qty < 0:
                negative.append(row)

    # summaries
    classes = ["ACTIVE", "MONITOR", "SLOW MOVING", "NON-MOVING", "DEAD STOCK", "NEW ITEM", "SEASONAL", "NEGATIVE"]
    by_loc = {}
    for r in out_rows:
        L = by_loc.setdefault(r[0], {"location": r[0], **{c: [0, 0.0] for c in classes}, "rnc": [0, 0.0],
                                     "items": 0, "value": 0.0})
        L["items"] += 1
        L["value"] += r[3]
        L[r[13]][0] += 1
        L[r[13]][1] += r[3]
        if r[14]:
            L["rnc"][0] += 1
            L["rnc"][1] += r[3]
    buckets = [("0–30", 0, 30), ("31–60", 31, 60), ("61–90", 61, 90), ("91–180", 91, 180),
               ("181–365", 181, 365), ("no use in history", None, None)]
    aging = {}
    for r in out_rows:
        if r[2] <= 0:
            continue
        d = r[12]
        label = buckets[-1][0] if d is None else next((b[0] for b in buckets[:-1] if b[1] <= d <= b[2]), "181–365")
        a = aging.setdefault(r[0], {})
        a[label] = a.get(label, 0.0) + r[3]
    totals = {c: [sum(L[c][0] for L in by_loc.values()), round(sum(L[c][1] for L in by_loc.values()), 0)]
              for c in classes}
    rnc = [r for r in out_rows if r[14]]
    names = {}
    for r in out_rows:
        names[r[1]] = (items.get(r[1]) or {}).get("Description") or ""
    return {
        "company": company, "as_of": as_of.isoformat(), "history_days": history_days,
        "history_from": max(start.isoformat(), held_from or start.isoformat()),
        "totals": {"items": len(out_rows), "value": round(sum(r[3] for r in out_rows if r[2] > 0), 0),
                   "classes": totals, "received_not_used": [len(rnc), round(sum(r[3] for r in rnc), 0)],
                   "negative": [len(negative), round(sum(r[3] for r in negative), 0)]},
        "locations": sorted(({**{k: v for k, v in L.items()}, "value": round(L["value"], 0)} for L in by_loc.values()),
                            key=lambda L: -L["value"]),
        "aging": aging, "buckets": [b[0] for b in buckets],
        "adjustments": sorted(adj.values(), key=lambda a: -(a["pos_cost"] - a["neg_cost"])),
        "excluded": sorted(([k, v[0], round(v[1], 0)] for k, v in excluded.items()), key=lambda x: -abs(x[2])),
        "rules": {"exclude_locations": sorted(ex_locs), "exclude_categories": sorted(ex_cats),
                  "limits": base, "perishable_limits": per, "distribution": sorted(distribution),
                  "count_negative_adjustments": count_neg, "seasonal": sorted(seasonal),
                  "perishable": sorted(perishable), "new_days": new_days},
        "_rows": out_rows, "_names": names,
    }


COLS = ["location", "item", "qty", "value", "c7", "c30", "c60", "c90", "c180", "c365", "last_receipt",
        "last_consumption", "days_without", "class", "received_not_used", "category", "perishable"]


def select_rows(r, location="", cls="", category="", q="", rnc=False, sort="value", limit=500):
    rows = r["_rows"]
    ql = (q or "").strip().lower()
    out = []
    for x in rows:
        if location and x[0] != location:
            continue
        if cls and x[13] != cls:
            continue
        if category and x[15] != category.upper():
            continue
        if rnc and not x[14]:
            continue
        if ql and ql not in x[1].lower() and ql not in (r["_names"].get(x[1]) or "").lower():
            continue
        out.append(x)
    key = {"value": lambda x: -abs(x[3]), "days": lambda x: -(x[12] if x[12] is not None else 10 ** 6),
           "qty": lambda x: -abs(x[2])}.get(sort, lambda x: -abs(x[3]))
    out.sort(key=key)
    return {"total": len(out), "value": round(sum(x[3] for x in out), 0), "cols": COLS,
            "rows": out[:limit], "names": {x[1]: r["_names"].get(x[1], "") for x in out[:limit]}}


# ---------------------------------------------------------------- the request
def request(client, company, as_of, history_days, refresh=False):
    today = date.today()
    a = min(date.fromisoformat(as_of[:10]), today - timedelta(days=1)) if as_of else today - timedelta(days=1)
    history_days = history_days if history_days in (90, 180, 365) else 90
    key = (company, a.isoformat(), history_days)
    now = time.time()
    with _jobs_lock:
        job = _jobs.get(company)
        running = job and job["state"] == "running"
        cached = _results.get(key)
    if cached and not refresh and now - cached[0] < 1800 and not running:
        return cached[1]
    if not running:
        with _jobs_lock:
            job = _jobs[company] = {"state": "running", "stage": "Item cards", "done": 0, "total": 0,
                                    "errors": [], "started": now}
        threading.Thread(target=_run, args=(client, company, a, history_days, refresh, job), daemon=True,
                         name="movement").start()
    with _jobs_lock:
        snap = dict(_jobs[company])
    return {"pending": True, "progress": {k: snap.get(k) for k in ("stage", "done", "total")},
            "as_of": a.isoformat(), "history_days": history_days,
            "note": "Reading stock by location and the item ledger from Business Central. A month of the ledger "
                    "is read once; the first read of a long history takes several minutes."}


def public(r):
    """The result without the row store (rows are fetched with select_rows)."""
    return {k: v for k, v in r.items() if not k.startswith("_")}


def _run(client, company, a, history_days, refresh, job):
    key = (company, a.isoformat(), history_days)
    try:
        m = recipes.master(client, company)
        with _jobs_lock:
            job.update(stage="Stock by location")
        locations = client.locations(company)
        qty_by_loc = {}
        for loc in locations:
            try:
                qty_by_loc[loc] = client.stock_by_location(company, loc, a.isoformat(), 86400 if not refresh else 0)
            except Exception as exc:                               # noqa: BLE001
                job["errors"].append(f"stock {loc}: {str(exc)[:160]}")
        months = months_back(a, history_days)
        with _jobs_lock:
            job.update(stage="Item ledger by month", total=len(months), done=0)
        for mo in reversed(months):
            try:
                load_month(client, company, mo, force=refresh)
            except Exception as exc:                               # noqa: BLE001
                job["errors"].append(f"ledger {mo}: {str(exc)[:200]}")
            with _jobs_lock:
                job["done"] += 1
        with _jobs_lock:
            job.update(stage="Classifying")
        out = analyse(company, a, history_days, qty_by_loc, m, store.get_settings(), None)
        out.update(errors=job["errors"], checked_at=datetime.now().isoformat(timespec="seconds"),
                   locations_read=len(qty_by_loc), definitions=DEFINITIONS)
        with _jobs_lock:
            _results[key] = (time.time(), out)
            for k, _ in sorted(_results.items(), key=lambda kv: kv[1][0])[:-3]:
                _results.pop(k, None)
            job["state"] = "done"
    except Exception as exc:                                       # noqa: BLE001
        traceback.print_exc()
        with _jobs_lock:
            _results[key] = (time.time(), {"available": False, "as_of": a.isoformat(),
                                           "error": f"The stock movements could not be read: {str(exc)[:300]}"})
            job.update(state="failed")


def held(company, as_of, history_days):
    with _jobs_lock:
        hit = _results.get((company, as_of, history_days))
    return hit[1] if hit else None


# ---------------------------------------------------------------- Excel
def render_xlsx(r):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    fill, bold = PatternFill("solid", fgColor="E4F3F9"), Font(bold=True)

    def sheet(ws, header, rows, widths):
        ws.append(header)
        for c in ws[1]:
            c.fill, c.font = fill, bold
        for row in rows:
            ws.append(row)
        for i, w in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.freeze_panes = "A2"
        if rows:
            ws.auto_filter.ref = ws.dimensions
    classes = ["ACTIVE", "MONITOR", "SLOW MOVING", "NON-MOVING", "DEAD STOCK", "NEW ITEM", "SEASONAL", "NEGATIVE"]
    ws = wb.active
    ws.title = "By location"
    sheet(ws, ["Location", "Items", "Value (est.)"] + [f"{c} items" for c in classes] + [f"{c} value" for c in classes]
          + ["Received, never used: items", "Received, never used: value"],
          [[L["location"], L["items"], L["value"]] + [L[c][0] for c in classes] + [round(L[c][1], 0) for c in classes]
           + [L["rnc"][0], round(L["rnc"][1], 0)] for L in r["locations"]], [14, 8, 14] + [10] * 16 + [14, 14])
    head = ["Location", "Item", "Description", "Category", "Perishable", "Qty on hand", "Value (est.)",
            "Used 7d", "Used 30d", "Used 60d", "Used 90d", "Used 180d", "Used 365d", "Last receipt",
            "Last consumption", "Days without consumption", "Class", "Received, never used"]
    rows = [[x[0], x[1], r["_names"].get(x[1], ""), x[15], x[16], x[2], x[3], x[4], x[5], x[6], x[7], x[8], x[9],
             x[10], x[11], x[12], x[13], x[14]] for x in sorted(r["_rows"], key=lambda x: (x[0], -abs(x[3])))]
    sheet(wb.create_sheet("Items by location"), head, rows,
          [12, 12, 40, 14, 9, 11, 13, 9, 9, 9, 9, 10, 10, 12, 14, 12, 13, 12])
    sheet(wb.create_sheet("Received never used"), head, [x for x in rows if x[17]],
          [12, 12, 40, 14, 9, 11, 13, 9, 9, 9, 9, 10, 10, 12, 14, 12, 13, 12])
    sheet(wb.create_sheet("Left out"), ["Location or category", "Lines", "Value (est.)"],
          [list(x) for x in r.get("excluded") or []], [30, 10, 16])
    sheet(wb.create_sheet("Adjustments"), ["Location", "Positive qty", "Positive cost", "Negative qty", "Negative cost"],
          [[a["location"], round(a["pos_qty"], 2), round(a["pos_cost"], 0), round(a["neg_qty"], 2),
            round(a["neg_cost"], 0)] for a in r["adjustments"]], [14, 14, 16, 14, 16])
    ws = wb.create_sheet("Definitions")
    for k, v in DEFINITIONS.items():
        ws.append([k, v])
    ws.column_dimensions["A"].width = 20
    ws.column_dimensions["B"].width = 140
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


DEFINITIONS = {
    "on_hand": "Quantity on hand at the date, per location, calculated by Business Central (InventorybyDate). "
               "Verified.",
    "value": "Value = quantity x the item's unit cost. Estimated: the posted value is in the value entries and "
             "the inventory accounts, which the Inventory screen reconciles.",
    "consumption": "Qualifying consumption: assembly consumption and sales (net of sales returns); transfers out "
                   "only at distribution locations; negative adjustments only if Settings says so. Purchases, "
                   "transfers in, positive adjustments and assembly output never count.",
    "classes": "Days since the last qualifying consumption against the stock thresholds in Settings: Active, "
               "Monitor, Slow moving, Non-moving, Dead stock. Perishable categories use their own, shorter "
               "thresholds. New item: first received within the new-item days with no earlier movement. "
               "Seasonal: listed in Settings. An item with no use in the whole history read is not called dead "
               "stock unless the history is at least as long as the dead-stock threshold.",
    "received_not_used": "Received but never used: stock on hand, received (purchase or transfer in) in the "
                         "history read, with no qualifying consumption. Severity by days since the first receipt in the history: review, then warning and critical at the slow and non-moving thresholds.",
    "excluded": "Locations and item categories set in Settings as equipment, small inventory or goods in transit "
                "(default locations *_IM, *_IN, TRANSIT and category INVENTAR I IMET) are left out of the classes, "
                "the aging and the adjustments; what was left out is listed with its count and value.",
    "adjustments": "Positive and negative adjustments are shown apart, by location, until it is agreed which are "
                   "physical counts, corrections or waste.",
}
