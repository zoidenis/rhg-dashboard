"""Sales → Menu engineering: every menu item by popularity and contribution margin (Kasavana & Smith), for a
brand, a location or all of RHG, in the period of the filters, with what operations and sales should do.

Sources
  * Simphony Reporting and Analytics, getMenuItemDailyTotals, one call per location and business day: items
    sold and sales by menu item (sales less the VAT, before discounts; item discounts apart), kept in
    data/menu_items.db (closed days are never read again). getMenuItemDimensions gives names and groups.
  * Business Central, the item cards and Assembly BOMs of the company of each location (recipes.master): the
    Simphony menu item number is the BC item number (the POS import posts it as ItemNo). The cost of a portion:
      - the recipe: quantity per × unit conversion × the component's unit cost, for an item with a BOM;
      - the item's unit cost, for an item sold as bought (bottles, soft drinks);
      - else the preparation cost Simphony holds; else none ("No cost").
Method, within each menu section (Simphony family group, or major group):
  * popularity: the item's share of the section's items sold (menu mix) against 70% of an equal share (1 / items);
  * contribution margin = average net price − portion cost, against the section's average margin weighted by
    items sold;
  * Star (popular, high margin), Plowhorse (popular, low margin), Puzzle (high margin, sold little), Dog (low
    margin, sold little). Items with no cost are ranked by popularity only; items sold at no price and POS
    technical items (Settings) stay out.
A brand or all of RHG sums its locations by menu item; locations in another currency are converted at the group
rates of Settings. Nothing is written to Simphony or Business Central.
"""
import io
import sqlite3
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

import config
import locintel
import recipes
import simphony
import store

DB_PATH = store.DATA_DIR / "menu_items.db"
KEEP_SECONDS = 900
CLASSES = ("star", "plowhorse", "puzzle", "dog", "nocost")
LABEL = {"star": "Star", "plowhorse": "Plowhorse", "puzzle": "Puzzle", "dog": "Dog", "nocost": "No cost"}
MEANING = {"star": "Popular and high margin", "plowhorse": "Popular, low margin",
           "puzzle": "High margin, sold little", "dog": "Low margin, sold little",
           "nocost": "No recipe or cost in BC: ranked by popularity only"}
ACTION = {
    "star": ("Keep it as it is: protect the recipe, the portion and its place on the menu.",
             "Recommend it; a small price rise can hold."),
    "plowhorse": ("Lower the portion cost: portion size, ingredients, garnish; pair it with higher margin sides.",
                  "Raise the price a little, or sell it with a higher margin item."),
    "puzzle": ("Check the presentation and the time it takes; keep the recipe.",
               "Move it up on the menu, describe it better, train the staff to recommend it; or lower the price."),
    "dog": ("Take it off or replace it, unless the offer needs it; free its stock and prep time.",
            "Stop promoting it; if it stays, re-price or re-design it."),
    "nocost": ("Add or complete the recipe (Assembly BOM) in BC so its margin can be measured.", ""),
}
_jobs, _lock, _db_lock = {}, threading.Lock(), threading.Lock()
_conn = None


# ---------------------------------------------------------------- the store of daily item totals
def _db():
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        # by order type too (the channel), for Channel profitability; the first tables (mi, days) had no order type
        _conn.executescript("""
            CREATE TABLE IF NOT EXISTS mio (ref TEXT, day TEXT, ot INTEGER, mi INTEGER, net REAL, qty REAL, disc REAL,
                                            prep REAL, rtn REAL, PRIMARY KEY (ref, day, ot, mi));
            CREATE TABLE IF NOT EXISTS dayso (ref TEXT, day TEXT, final INTEGER, at REAL, PRIMARY KEY (ref, day));
            -- as Simphony gives them, VAT included and before discounts, every order type together: the sales
            -- reconciliation against the POS import of Business Central (salesrec.py); dayv: days held so
            CREATE TABLE IF NOT EXISTS miv (ref TEXT, day TEXT, mi INTEGER, sls REAL, cnt REAL, dsc REAL,
                                            PRIMARY KEY (ref, day, mi));
            CREATE TABLE IF NOT EXISTS dayv (ref TEXT, day TEXT, PRIMARY KEY (ref, day));
        """)
    return _conn


def read_day(ref, day, latest=None, vat=False, force=False):
    """The menu item totals of one location and business day, read from Simphony unless held: a closed day
    (two days before the last business day) is never read again, a recent one after 15 minutes. vat: the day must
    also be held VAT included (miv), which days read before the sales reconciliation are not."""
    with _db_lock:
        hit = _db().execute("SELECT final, at FROM dayso WHERE ref=? AND day=?", (ref, day)).fetchone()
        held_v = not vat or _db().execute("SELECT 1 FROM dayv WHERE ref=? AND day=?", (ref, day)).fetchone()
    if hit and held_v and not force and (hit[0] or time.time() - hit[1] < KEEP_SECONDS):
        return False
    j = simphony.call("getMenuItemDailyTotals", {"locRef": ref, "busDt": day}) or {}
    agg, inc = {}, {}
    for rvc in j.get("revenueCenters") or []:
        for m in rvc.get("menuItems") or []:
            if m.get("miNum") is None:
                continue
            v = inc.setdefault(int(m["miNum"]), [0.0, 0.0, 0.0])
            v[0] += float(m.get("slsTtl") or 0)
            v[1] += float(m.get("slsCnt") or 0)
            v[2] += abs(float(m.get("dscTtl") or 0))
            tax = float(m.get("vatTtl") or 0) or float(m.get("inclTaxTtl") or 0)
            x = agg.setdefault((int(m.get("otNum") or 0), int(m["miNum"])), [0.0, 0.0, 0.0, 0.0, 0.0])
            x[0] += float(m.get("slsTtl") or 0) - tax
            x[1] += float(m.get("slsCnt") or 0)
            x[2] += abs(float(m.get("dscTtl") or 0)) - abs(float(m.get("dscVatTtl") or 0))
            x[3] += float(m.get("prepCost") or 0)
            x[4] += float(m.get("rtnCnt") or 0)
    final = bool(latest and day <= (date.fromisoformat(latest) - timedelta(days=2)).isoformat())
    with _db_lock:
        db = _db()
        db.execute("DELETE FROM mio WHERE ref=? AND day=?", (ref, day))
        db.executemany("INSERT INTO mio VALUES (?,?,?,?,?,?,?,?,?)",
                       [(ref, day, k[0], k[1], round(v[0], 2), v[1], round(v[2], 2), round(v[3], 2), v[4])
                        for k, v in agg.items()])
        db.execute("INSERT OR REPLACE INTO dayso VALUES (?,?,?,?)", (ref, day, int(final), time.time()))
        db.execute("DELETE FROM miv WHERE ref=? AND day=?", (ref, day))
        db.executemany("INSERT INTO miv VALUES (?,?,?,?,?,?)",
                       [(ref, day, mi, round(v[0], 2), v[1], round(v[2], 2)) for mi, v in inc.items()])
        db.execute("INSERT OR REPLACE INTO dayv VALUES (?,?)", (ref, day))
        db.commit()
    return True


def items_vat(ref, a, b):
    """{day: {menu item: (sales VAT included before discounts, count, discounts)}} of one location, the days held."""
    with _db_lock:
        rows = _db().execute("SELECT day, mi, sls, cnt, dsc FROM miv WHERE ref=? AND day>=? AND day<=?", (ref, a, b)).fetchall()
        held = [d for (d,) in _db().execute("SELECT day FROM dayv WHERE ref=? AND day>=? AND day<=?", (ref, a, b))]
    out = {d: {} for d in held}
    for day, mi, sls, cnt, dsc in rows:
        out.setdefault(day, {})[mi] = (sls or 0.0, cnt or 0.0, dsc or 0.0)
    return out


def vat_by_item(ref, a, b):
    """{day: {menu item: VAT}} of one location: Simphony's VAT on the item after its discounts (vatTtl), as the
    sales VAT included (miv) less the sales without VAT (mio, every order type) of the same read."""
    with _db_lock:
        inc = _db().execute("SELECT day, mi, sls FROM miv WHERE ref=? AND day>=? AND day<=?", (ref, a, b)).fetchall()
        ex = _db().execute("SELECT day, mi, SUM(net) FROM mio WHERE ref=? AND day>=? AND day<=? GROUP BY day, mi",
                           (ref, a, b)).fetchall()
    net = {(d, mi): v or 0.0 for d, mi, v in ex}
    out = {}
    for day, mi, sls in inc:
        out.setdefault(day, {})[mi] = round((sls or 0.0) - net.get((day, mi), 0.0), 2)
    return out


# ---------------------------------------------------------------- costs from Business Central
def _coster(m):
    """cost(item) -> (unit cost or None, basis) from a recipes.master."""
    items = m.get("items") or {}
    uom = {(it, code): per for it, code, per in m.get("uom") or []}
    lines = {}
    for parent, _ln, typ, no, _d, _nested, qty, unit in m.get("bom") or []:
        lines.setdefault(parent, []).append((typ, no, qty, (unit or "").upper()))
    memo = {}

    def cost(item):
        if item in memo:
            return memo[item]
        card = items.get(item)
        if card is None:
            memo[item] = (None, "Not in BC")
            return memo[item]
        comps = [x for x in lines.get(item) or [] if x[0] == "Item"]
        if comps:
            total, ok = 0.0, True
            for _t, no, qty, unit in comps:
                comp = items.get(no) or {}
                base = str(comp.get("Base_Unit_of_Measure") or "").upper()
                per = 1.0 if not unit or unit == base else uom.get((no, unit))
                uc = float(comp.get("Unit_Cost") or 0)
                if not comp or not per or per <= 0 or qty <= 0 or uc <= 0:
                    ok = False
                if per and per > 0:
                    total += qty * per * uc
            memo[item] = (round(total, 2), "Recipe" if ok else "Recipe, incomplete") if total > 0 else (None, "Recipe without cost")
            return memo[item]
        uc = float(card.get("Unit_Cost") or 0)
        memo[item] = (uc, "Item cost") if uc > 0 else (None, "No recipe, no cost")
        return memo[item]
    return cost


def items_by_order_type(ref, a, b):
    """[(day, ot, mi, net, qty, prep)] of one location between two dates, from the held days."""
    with _db_lock:
        return _db().execute("SELECT day, ot, mi, net, qty, prep FROM mio WHERE ref=? AND day>=? AND day<=?",
                             (ref, a, b)).fetchall()


# ---------------------------------------------------------------- the read
def _dates(a, b):
    out, d = [], a
    while d <= b:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def build(periods, latest_fn, client, refs=None, progress=None):
    c_from, c_to, *_rest = periods
    label = periods[7] if len(periods) > 7 else ""
    settings = store.get_settings()
    reg = {r["ref"]: r for r in locintel.registry(settings)}
    fallback = (settings.get("group") or {}).get("reporting_currency") or "ALL"
    locs = [l for l in simphony.locations() if locintel.in_use(l) and (refs is None or l["locRef"].upper() in refs)]
    latest = {}
    for l in locs:
        try:
            latest[l["locRef"]] = latest_fn(l["locRef"])
        except Exception:                                          # noqa: BLE001
            latest[l["locRef"]] = None
    closed = [v for v in latest.values() if v]
    if closed and date.fromisoformat(max(closed)) < c_to:
        c_to = max(c_from, date.fromisoformat(max(closed)))
    days = _dates(c_from, c_to)
    jobs = [(l["locRef"], d) for l in locs for d in days if not latest.get(l["locRef"]) or d <= latest[l["locRef"]]]
    errors, done = [], 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = {pool.submit(read_day, r, d, latest.get(r)): (r, d) for r, d in jobs}
        for f in as_completed(futs):
            done += 1
            try:
                f.result()
            except Exception as exc:                               # noqa: BLE001
                if len(errors) < 8:
                    errors.append(f"{futs[f][0]} {futs[f][1]}: {str(exc)[:140]}")
            if progress and (done % 10 == 0 or done == len(jobs)):
                progress(done, len(jobs))
    with _db_lock:
        q = _db().execute("SELECT ref, mi, SUM(net), SUM(qty), SUM(disc), SUM(prep), SUM(rtn) FROM mio "
                          "WHERE day>=? AND day<=? GROUP BY ref, mi", (c_from.isoformat(), c_to.isoformat())).fetchall()
    sold = {}
    for ref, mi, net, qty, disc, prep, rtn in q:
        sold.setdefault(ref, []).append((mi, net or 0, qty or 0, disc or 0, prep or 0, rtn or 0))
    technical = recipes.technical_matcher(settings)
    masters, costers = {}, {}
    out_locs, rows = [], {}
    for l in locs:
        ref = l["locRef"]
        rg = reg.get(ref) or {}
        company = rg.get("company") or config.BC_COMPANY
        if company not in costers:
            try:
                masters[company] = recipes.master(client, company)
                costers[company] = _coster(masters[company])
            except Exception as exc:                               # noqa: BLE001
                traceback.print_exc()
                errors.append(f"BC item cards and recipes of {company} could not be read: {str(exc)[:160]}")
                costers[company] = lambda it: (None, "BC not read")
        try:
            names = simphony.menu_items(ref)
        except Exception as exc:                                   # noqa: BLE001
            names = {}
            errors.append(f"{ref}: menu item names: {str(exc)[:120]}")
        out_locs.append({"ref": ref, "name": rg.get("name") or l.get("name") or ref, "brand": rg.get("brand") or "",
                         "company": company, "bc": rg.get("bc") or "",
                         "currency": rg.get("currency") or locintel.norm_currency(l.get("currency")) or fallback})
        lst = []
        for mi, net, qty, disc, prep, rtn in sold.get(ref, []):
            nm = names.get(mi) or {}
            if nm.get("excluded") or (qty <= 0 and abs(net) < 0.01):
                continue
            name = nm.get("name") or f"Item {mi}"
            uc, basis = costers[company](str(mi))
            if uc is None and prep > 0 and qty > 0:
                uc, basis = prep / qty, "Simphony prep cost"
            lst.append({"mi": mi, "name": name, "major": nm.get("major") or "Other", "family": nm.get("family") or "Other",
                        "qty": qty, "net": net, "disc": disc, "rtn": rtn, "unit_cost": uc, "basis": basis,
                        "technical": bool(technical(str(mi), name))})
        rows[ref] = lst
    return {"period": {"from": c_from.isoformat(), "to": c_to.isoformat(), "label": label, "days": len(days)},
            "locations": out_locs, "rows": rows, "errors": errors,
            "read_at": time.strftime("%Y-%m-%dT%H:%M:%S")}


def request(periods, latest_fn, client, key, refs=None, refresh=False):
    now = time.time()
    with _lock:
        job = _jobs.get(key)
        if job and job["state"] == "running":
            return {"pending": True, "done": job.get("done", 0), "total": job.get("total", 0),
                    "elapsed": int(now - job["started"])}
        if job and job["state"] == "done" and not refresh and now - job["finished"] < KEEP_SECONDS:
            return job["result"]
        if job and job["state"] == "failed" and not refresh:
            _jobs.pop(key, None)
            return {"available": False, "error": job["error"]}
        job = _jobs[key] = {"state": "running", "started": now, "done": 0, "total": 0}
        for k in [k for k, v in _jobs.items() if v.get("state") == "done" and now - v.get("finished", 0) > 3 * KEEP_SECONDS]:
            _jobs.pop(k, None)

    def progress(done, total):
        with _lock:
            job.update(done=done, total=total)

    def run():
        try:
            state = {"state": "done", "result": build(periods, latest_fn, client, refs, progress)}
        except simphony.SimphonyError as exc:
            state = {"state": "failed", "error": str(exc)}
        except Exception as exc:                                   # noqa: BLE001
            traceback.print_exc()
            state = {"state": "failed", "error": f"The menu items could not be read: {str(exc)[:300]}"}
        with _lock:
            _jobs[key] = dict(state, started=now, finished=time.time())
    threading.Thread(target=run, daemon=True, name="menu-engineering").start()
    return {"pending": True, "done": 0, "total": 0, "elapsed": 0}


# ---------------------------------------------------------------- the analysis
def _rates(settings):
    g = settings.get("group") or {}
    reporting = (g.get("reporting_currency") or "ALL").strip().upper()
    rates = {reporting: 1.0}
    for part in (g.get("rates") or "").replace(";", ",").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            try:
                rates[locintel.norm_currency(k)] = float(v)
            except ValueError:
                pass
    return reporting, rates


def settings_of(settings):
    inv = (settings or {}).get("inventory") or {}

    def num(k, d):
        try:
            v = float(inv.get(k) if inv.get(k) not in (None, "") else d)
            return v if v > 0 else d
        except (TypeError, ValueError):
            return d
    method = str(inv.get("menu_margin_method") or "weighted").strip().lower()
    return {"pop": num("menu_popularity_pct", 70.0), "target": num("menu_cost_target_pct", 32.0),
            "disc": num("menu_discount_pct", 15.0), "margin": method if method in ("weighted", "simple") else "weighted"}


def _classify(items, S):
    """Sets cls, mm, mm_threshold, cm_avg, x, y on the items of one section."""
    priced = [x for x in items if x["price"] > 0]
    total_qty = sum(x["qty"] for x in priced) or 1
    n = len(priced) or 1
    thr = S["pop"] / 100 / n
    costed = [x for x in priced if x["cost"] is not None]
    cq = sum(x["qty"] for x in costed)
    if S.get("margin") == "simple":                    # every item counts the same
        avg_cm = sum(x["cm"] for x in costed) / len(costed) if costed else None
    else:                                              # weighted by items sold (Kasavana & Smith)
        avg_cm = sum(x["cm"] * x["qty"] for x in costed) / cq if cq else None
    for x in priced:
        x["mm"] = x["qty"] / total_qty
        x["mm_threshold"] = thr
        x["cm_avg"] = avg_cm
        high_pop = x["mm"] >= thr
        x["x"] = round(x["mm"] / thr, 3) if thr else None
        if x["cost"] is None or avg_cm is None:
            x["cls"], x["y"] = "nocost", None
            x["pop_high"] = high_pop
            continue
        high_cm = x["cm"] >= avg_cm
        x["y"] = round(x["cm"] / avg_cm, 3) if avg_cm > 0 else None
        x["cls"] = ("star" if high_cm else "plowhorse") if high_pop else ("puzzle" if high_cm else "dog")
        x["pop_high"] = high_pop
    return avg_cm


def analyse(ds, level="all", key="", section="family", settings=None):
    settings = settings or store.get_settings()
    S = settings_of(settings)
    reporting, rates = _rates(settings)
    locs = ds["locations"]
    if level == "brand":
        sel = [L for L in locs if (L["brand"] or "").upper() == (key or "").upper()]
        title = f"{key} (brand)"
    elif level == "location":
        sel = [L for L in locs if L["ref"] == key]
        title = sel[0]["name"] if sel else key
    else:
        sel, title, level = list(locs), "All RHG", "all"
    curs = {L["currency"] for L in sel}
    convert = len(curs) > 1
    currency = reporting if convert else (next(iter(curs)) if curs else reporting)
    missing_rates = sorted(c for c in curs if convert and c not in rates)
    sec_key = "major" if section == "major" else "family"

    def collect(refs):
        agg = {}
        for L in refs:
            f = rates.get(L["currency"], None) if convert else 1.0
            if f is None:
                continue
            for r in ds["rows"].get(L["ref"], []):
                if r["technical"]:
                    continue
                a = agg.setdefault(r["mi"], {"mi": r["mi"], "name": r["name"], "major": r["major"], "family": r["family"],
                                             "qty": 0.0, "net": 0.0, "disc": 0.0, "rtn": 0.0, "costq": 0.0, "costv": 0.0,
                                             "bases": set(), "locations": set()})
                a["qty"] += r["qty"]
                a["net"] += r["net"] * f
                a["disc"] += r["disc"] * f
                a["rtn"] += r["rtn"]
                a["bases"].add(r["basis"])
                a["locations"].add(L["ref"])
                if r["unit_cost"] is not None:
                    a["costq"] += r["qty"]
                    a["costv"] += r["unit_cost"] * r["qty"] * f
        items = []
        for a in agg.values():
            if a["qty"] <= 0:
                continue
            price = a["net"] / a["qty"]
            cost = a["costv"] / a["costq"] if a["costq"] >= a["qty"] * 0.999 and a["costq"] > 0 else None
            basis = sorted(a["bases"])
            it = {"mi": a["mi"], "name": a["name"], "major": a["major"], "family": a["family"],
                  "section": a[sec_key], "qty": round(a["qty"], 2), "net": round(a["net"], 0), "price": round(price, 2),
                  "disc": round(a["disc"], 0), "rtn": a["rtn"], "cost": round(cost, 2) if cost is not None else None,
                  "basis": basis[0] if len(basis) == 1 else ", ".join(basis),
                  "locations": sorted(a["locations"])}
            it["cm"] = round(price - cost, 2) if cost is not None else None
            it["cm_total"] = round(it["cm"] * a["qty"], 0) if cost is not None else None
            it["cost_pct"] = round(cost / price * 100, 1) if cost is not None and price > 0 else None
            it["disc_pct"] = round(a["disc"] / (a["net"]) * 100, 1) if a["net"] > 0 else None
            items.append(it)
        secs = {}
        for it in items:
            secs.setdefault(it["section"], []).append(it)
        avg = {s: _classify(lst, S) for s, lst in secs.items()}
        for it in items:
            if it["price"] <= 0:
                it.update(cls="noprice", x=None, y=None, mm=None)
        return items, secs, avg

    items, secs, avg = collect(sel)
    for it in items:
        flags = []
        if it["cls"] == "noprice":
            flags.append("Sold at no price")
        if it["cost_pct"] is not None and it["cost_pct"] > S["target"]:
            flags.append(f"Cost {it['cost_pct']:.0f}% of the price, above the {S['target']:.0f}% target")
        if "incomplete" in it["basis"]:
            flags.append("Recipe incomplete in BC (a component without cost or unit conversion)")
        if it["basis"] in ("Not in BC",):
            flags.append("Menu item number not found as a BC item")
        if it["disc_pct"] is not None and it["disc_pct"] > S["disc"]:
            flags.append(f"Item discounts {it['disc_pct']:.0f}% of sales")
        if it["rtn"]:
            flags.append(f"{it['rtn']:.0f} returned")
        it["flags"] = flags
        ops, sales = ACTION.get(it["cls"], ("", ""))
        it["ops"], it["sales"] = ops, sales

    # the same item across the locations of the selection
    across = []
    if len(sel) > 1:
        per = {}
        for L in sel:
            li, _s, _a = collect([L])
            for it in li:
                per.setdefault(it["mi"], {})[L["ref"]] = (it["cls"], it["qty"], it["cm"])
        names = {it["mi"]: it for it in items}
        for mi, by in per.items():
            cl = {v[0] for v in by.values() if v[0] in ("star", "plowhorse", "puzzle", "dog")}
            if len(by) > 1 and len(cl) > 1:
                it = names.get(mi) or {}
                across.append({"mi": mi, "name": it.get("name", str(mi)), "section": it.get("section", ""),
                               "by": {r: {"cls": v[0], "qty": v[1], "cm": v[2]} for r, v in by.items()}})
        across.sort(key=lambda a: -sum(v["qty"] for v in a["by"].values()))

    priced = [it for it in items if it["cls"] != "noprice"]
    net = sum(it["net"] for it in priced)
    costed = [it for it in priced if it["cost"] is not None]
    cm_total = sum(it["cm_total"] for it in costed)
    net_costed = sum(it["net"] for it in costed)
    by_cls = {c: {"items": sum(1 for it in priced if it["cls"] == c), "qty": sum(it["qty"] for it in priced if it["cls"] == c),
                  "net": round(sum(it["net"] for it in priced if it["cls"] == c), 0),
                  "cm": round(sum(it["cm_total"] or 0 for it in priced if it["cls"] == c), 0)} for c in CLASSES}
    sections = []
    for s, lst in secs.items():
        lp = [x for x in lst if x["cls"] != "noprice"]
        lc = [x for x in lp if x["cost"] is not None]
        sn = sum(x["net"] for x in lp)
        sections.append({"section": s, "items": len(lp), "qty": round(sum(x["qty"] for x in lp), 0), "net": round(sn, 0),
                         "cm": round(sum(x["cm_total"] for x in lc), 0) if lc else None,
                         "cost_pct": round(sum(x["cost"] * x["qty"] for x in lc) / sum(x["net"] for x in lc) * 100, 1)
                         if lc and sum(x["net"] for x in lc) > 0 else None,
                         "avg_cm": round(avg[s], 2) if avg.get(s) is not None else None,
                         "threshold_pct": round(S["pop"] / max(1, len(lp)), 2),
                         **{c: sum(1 for x in lp if x["cls"] == c) for c in CLASSES}})
    sections.sort(key=lambda s: -s["net"])
    rank = {"dog": 0, "plowhorse": 1, "puzzle": 2, "nocost": 3, "star": 4, "noprice": 5}
    actions = []
    for it in sorted(priced, key=lambda i: (rank[i["cls"]], -(i["net"]))):
        if it["cls"] == "star" and not it["flags"]:
            continue
        actions.append({"mi": it["mi"], "name": it["name"], "section": it["section"], "cls": it["cls"],
                        "ops": it["ops"], "sales": it["sales"], "flags": it["flags"], "net": it["net"],
                        "cm_total": it["cm_total"]})
    brands = sorted({L["brand"] for L in locs if L["brand"]})
    return {"title": title, "level": level, "key": key, "section_by": sec_key, "currency": currency,
            "converted": convert, "missing_rates": missing_rates, "period": ds["period"], "read_at": ds["read_at"],
            "errors": ds["errors"], "settings": S,
            "selection": [{"ref": L["ref"], "name": L["name"], "currency": L["currency"]} for L in sel],
            "kpis": {"items": len(priced), "qty": round(sum(it["qty"] for it in priced), 0), "net": round(net, 0),
                     "cm": round(cm_total, 0), "cm_pct": round(cm_total / net_costed * 100, 1) if net_costed else None,
                     "cost_pct": round((net_costed - cm_total) / net_costed * 100, 1) if net_costed else None,
                     "costed_share": round(net_costed / net * 100, 1) if net else None,
                     "noprice": sum(1 for it in items if it["cls"] == "noprice")},
            "classes": by_cls, "labels": LABEL, "meaning": MEANING, "advice": ACTION,
            "items": sorted(items, key=lambda i: -i["net"]), "sections": sections, "actions": actions,
            "across": across[:200], "brands": brands,
            "locations": [{"ref": L["ref"], "name": L["name"], "brand": L["brand"]} for L in locs],
            "definitions": dict(DEFINITIONS, method=(
                f"Within each menu section (family group, or major group): popularity = the item's share of the items sold "
                f"in the section against {S['pop']:g}% of an equal share; contribution margin = average net price − portion "
                f"cost against the section's average margin "
                + ("weighted by items sold" if S["margin"] == "weighted" else "of its items, each counted once")
                + ". Star = popular and high margin; Plowhorse = popular, low margin; Puzzle = high margin, sold little; "
                  "Dog = low margin, sold little."))}


DEFINITIONS = {
    "sales": "Items sold and sales by menu item from Simphony (getMenuItemDailyTotals), sales less the VAT and before "
             "discounts; item discounts are shown apart. The period of the filters, to the last business day Simphony "
             "has closed.",
    "cost": "Portion cost from Business Central (the Simphony menu item number is the BC item number): the recipe "
            "(Assembly BOM: quantity per × unit conversion × the component's unit cost), or the item's unit cost for an "
            "item sold as bought; else the preparation cost Simphony holds. Estimated: unit costs, not the posted cost.",
    "method": "Within each menu section (family group, or major group): popularity = the item's share of the items "
              "sold in the section against 70% of an equal share; contribution margin = average net price − portion "
              "cost against the section's average margin weighted by items sold. Star = popular and high margin; "
              "Plowhorse = popular, low margin; Puzzle = high margin, sold little; Dog = low margin, sold little.",
    "scope": "A brand or all of RHG sums its locations by menu item; another currency is converted at the group rates "
             "of Settings. POS technical items (Settings → Inventory) and items sold at no price stay out of the matrix.",
}


# ---------------------------------------------------------------- Excel
def render_xlsx(r):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    fill, font = PatternFill("solid", fgColor="E4F3F9"), Font(bold=True, color="0E2733")
    money, qty, pct = '#,##0;[Red]-#,##0', '#,##0', '0.0'

    def sheet(title, header, rows, formats, widths, first=False):
        ws = wb.active if first else wb.create_sheet()
        ws.title = title[:31]
        ws.append(header)
        for c in ws[1]:
            c.fill, c.font, c.alignment = fill, font, Alignment(wrap_text=True, vertical="top")
        for row in rows:
            ws.append(row)
        for i, f in enumerate(formats, start=1):
            if f:
                for cell in ws.iter_rows(min_row=2, min_col=i, max_col=i):
                    cell[0].number_format = f
        for i, w in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.freeze_panes = "A2"
        if rows:
            ws.auto_filter.ref = ws.dimensions
        return ws
    L = dict(LABEL, noprice="No price")
    C = r["currency"]
    sheet("Items", ["Menu item", "Name", "Section", "Major group", "Family group", "Class", "Items sold", "Menu mix %",
                    f"Net sales {C}", f"Avg price {C}", f"Portion cost {C}", "Cost basis", "Cost %", f"Margin {C}",
                    f"Total margin {C}", f"Item discounts {C}", "Locations", "Operations", "Sales", "Flags"],
          [[it["mi"], it["name"], it["section"], it["major"], it["family"], L.get(it["cls"], it["cls"]), it["qty"],
            round(it["mm"] * 100, 2) if it.get("mm") is not None else None, it["net"], it["price"], it["cost"], it["basis"],
            it["cost_pct"], it["cm"], it["cm_total"], it["disc"], ", ".join(it["locations"]), it["ops"], it["sales"],
            "; ".join(it["flags"])] for it in r["items"]],
          [None] * 6 + [qty, '0.00', money, '#,##0.00', '#,##0.00', None, pct, '#,##0.00', money, money] + [None] * 4,
          [11, 34, 22, 16, 22, 11, 10, 9, 13, 11, 11, 18, 8, 11, 13, 12, 20, 60, 60, 60], first=True)
    sheet("Sections", ["Section", "Items", "Items sold", f"Net sales {C}", f"Total margin {C}", "Cost %", f"Avg margin {C}",
                       "Popularity line %", "Stars", "Plowhorses", "Puzzles", "Dogs", "No cost"],
          [[s["section"], s["items"], s["qty"], s["net"], s["cm"], s["cost_pct"], s["avg_cm"], s["threshold_pct"],
            s["star"], s["plowhorse"], s["puzzle"], s["dog"], s["nocost"]] for s in r["sections"]],
          [None, None, qty, money, money, pct, '#,##0.00', '0.00'] + [None] * 5, [26, 8, 10, 14, 14, 8, 11, 10, 7, 9, 8, 7, 8])
    sheet("Actions", ["Class", "Menu item", "Name", "Section", "Operations", "Sales", "Flags", f"Net sales {C}"],
          [[L.get(a["cls"], a["cls"]), a["mi"], a["name"], a["section"], a["ops"], a["sales"], "; ".join(a["flags"]), a["net"]]
           for a in r["actions"]], [None] * 7 + [money], [11, 11, 34, 22, 60, 60, 60, 13])
    if r["across"]:
        refs = [s["ref"] for s in r["selection"]]
        sheet("Across locations", ["Menu item", "Name", "Section", *refs],
              [[a["mi"], a["name"], a["section"], *[L.get((a["by"].get(x) or {}).get("cls"), "") for x in refs]] for a in r["across"]],
              [None] * (3 + len(refs)), [11, 34, 22] + [12] * len(refs))
    ws = sheet("Read me", ["Menu engineering"],
               [[f"{r['title']} · {r['period']['from']} to {r['period']['to']} · amounts in {C}"
                 + (" (converted at the group rates of Settings)" if r["converted"] else "")]]
               + [[f"{LABEL[c]}: {MEANING[c]}. Operations: {ACTION[c][0]} Sales: {ACTION[c][1]}"] for c in CLASSES]
               + [[v] for v in DEFINITIONS.values()] + [["Simphony and Business Central were only read."]], [None], [140])
    for row in ws.iter_rows(min_row=2):
        row[0].alignment = Alignment(wrap_text=True, vertical="top")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
