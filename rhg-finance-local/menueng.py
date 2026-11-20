"""Sales → Menu engineering: every menu item by popularity and contribution margin (Kasavana & Smith), for a
brand, a location or all of RHG, in the period of the filters, with what operations and sales should do.

Sources
  * Simphony Reporting and Analytics, getMenuItemDailyTotals, one call per location and business day: items
    sold and sales by menu item (sales less the VAT, before discounts; item discounts apart), kept in
    data/menu_items.db (closed days are never read again). getMenuItemDimensions gives names and groups.
  * Business Central, the item cards and Assembly BOMs of the company of each location (recipes.master). The BC item
    of a menu item is the one the POS import posts for it (MIDSimphonyArchive: Menu_Item_Number → ItemNo, e.g.
    10025002 Chicken Gyros → A2405); else the item with the menu item number. The cost of a portion (Geri, 20 Nov
    2026):
      - an item with a recipe (Assembly BOM): its Standard Cost on the item card (BC rolls the BOM up into it);
        without a Standard Cost, the recipe at today's unit costs (quantity per × unit conversion × unit cost);
      - the item's unit cost, for an item sold as bought (bottles, soft drinks);
      - else the preparation cost Simphony holds; else none ("No cost").
  * The menu price: the item's price in Simphony EMC (getMenuItemPrices: the lowest price level with a real price,
    the most common across revenue centres), VAT included; without VAT at the VAT % of the BC item (its VAT Prod.
    Posting Group, e.g. TVSH20). The margin is taken on it; the average price received is shown beside it.
Method, within each menu section (Simphony family group, or major group):
  * popularity: the item's share of the section's items sold (menu mix) against 70% of an equal share (1 / items);
  * contribution margin = the EMC menu price without VAT (else the average price received) − portion cost,
    against the section's average margin weighted by
    items sold;
  * Star (popular, high margin), Plowhorse (popular, low margin), Puzzle (high margin, sold little), Dog (low
    margin, sold little). Items with no cost are ranked by popularity only; items sold at no price and POS
    technical items (Settings) stay out.
A brand or all of RHG sums its locations by menu item; locations in another currency are converted at the group
rates of Settings. Nothing is written to Simphony or Business Central.
"""
import io
import json
import re
import sqlite3
import threading
import time
import traceback
from collections import Counter
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
    """cost(item) -> (unit cost or None, basis) from a recipes.master. An item with a recipe costs its Standard Cost
    (the BOM rolled up by BC), else the recipe at today's unit costs. cost.recipe(item) -> the recipe at today's unit
    costs or None; cost.card(item) -> the item card or None."""
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
            std = float(card.get("Standard_Cost") or 0)
            if std > 0:
                memo[item] = (round(std, 2), "Standard cost (Assembly BOM)")
                return memo[item]
            total, ok = recipe(item)
            memo[item] = (round(total, 2), "Recipe" if ok else "Recipe, incomplete") if total > 0 else (None, "Recipe without cost")
            return memo[item]
        uc = float(card.get("Unit_Cost") or 0)
        memo[item] = (uc, "Item cost") if uc > 0 else (None, "No recipe, no cost")
        return memo[item]

    def recipe(item):
        """(the recipe at today's unit costs, every component costed) of an item with a BOM; (0, False) without."""
        total, ok = 0.0, True
        comps = [x for x in lines.get(item) or [] if x[0] == "Item"]
        for _t, no, qty, unit in comps:
            comp = items.get(no) or {}
            base = str(comp.get("Base_Unit_of_Measure") or "").upper()
            per = 1.0 if not unit or unit == base else uom.get((no, unit))
            uc = float(comp.get("Unit_Cost") or 0)
            if not comp or not per or per <= 0 or qty <= 0 or uc <= 0:
                ok = False
            if per and per > 0:
                total += qty * per * uc
        return total, ok and bool(comps)
    cost.recipe = lambda item: (round(recipe(item)[0], 2) or None) if lines.get(item) else None
    cost.card = lambda item: items.get(item)
    return cost


def vat_pct(card):
    """The VAT % of a BC item from its VAT Prod. Posting Group (TVSH20 → 20, TVSH0 → 0); None when it names none."""
    m = re.search(r"(\d+(?:[.,]\d+)?)", str((card or {}).get("VAT_Prod_Posting_Group") or ""))
    return float(m.group(1).replace(",", ".")) if m else None


# the BC item the POS import posts for each menu item, by company: {company: {"at", "map": {menu item: BC item}}}
_bcmap, MAP_FILE, MAP_TTL = {}, store.DATA_DIR / "menu_bc_map.json", 6 * 3600


def bc_map(client, company, a, b):
    """{menu item number: BC item number} as the POS import posts them (MIDSimphonyArchive, the period read), on top
    of what earlier reads found (kept in data/menu_bc_map.json, so a day not imported yet keeps its item)."""
    hit = _bcmap.get(company)
    if hit and time.time() - hit["at"] < MAP_TTL and hit.get("span") == (a, b):
        return hit["map"]
    try:
        held = json.loads(MAP_FILE.read_text()) if MAP_FILE.exists() else {}
    except (OSError, ValueError):
        held = {}
    m = dict(held.get(company) or {})
    rows = client._get_all(client._url(company, config.E_POS_ARCHIVE), {
        "$filter": f"TransactionDateArch ge {a} and TransactionDateArch le {b}",
        "$select": "Menu_Item_Number,ItemNo,TransactionDateArch"}, page_size=5000)
    last = {}
    for r in rows:
        mi, it = str(r.get("Menu_Item_Number") or "").strip(), str(r.get("ItemNo") or "").strip()
        d = str(r.get("TransactionDateArch") or "")[:10]
        if mi and it and d >= last.get(mi, ""):
            last[mi] = d
            m[mi] = it
    held[company] = m
    try:
        MAP_FILE.write_text(json.dumps(held))
    except OSError:
        pass
    _bcmap[company] = {"at": time.time(), "span": (a, b), "map": m}
    return m


def emc_prices(ref):
    """{menu item: (the EMC price for the report, the lowest real price)} of a location, VAT included: the lowest
    price level with a real price, the most common across revenue centres; prices of 1 or less are placeholders."""
    by = {}
    for r in simphony.menu_item_prices(ref):
        p = r.get("price")
        if p is not None and float(p) > 1:
            by.setdefault(r["num"], []).append((r.get("level"), float(p)))
    out = {}
    for num, rs in by.items():
        lv = min((lvl for lvl, _p in rs if lvl is not None), default=None)
        ps = [p for lvl, p in rs if lvl == lv] or [p for _l, p in rs]
        out[num] = (Counter(ps).most_common(1)[0][0], min(p for _l, p in rs))
    return out


def priced_shares(ref, a, b, prices=None):
    """{menu item: share of its units sold at a price} of one location between two dates. A menu item that is part of
    a combo meal is rung at no price inside the combo (the combo item carries the price, and in BC it has no recipe):
    e.g. Truffle Chicken Gyros Big, 140 units 1-19 Nov 2026, 58 of them at 0 in "Truffle Chicken Gyros Big Combo".
    Counted with the others they pulled its price per unit from 390 to 229 and its cost to 53% (Geri, 20 Nov 2026).
    Units at a price = the item's sales VAT included before discounts (miv) / its lowest real price in Simphony
    (getMenuItemPrices), capped at the units sold: the lowest price never takes for a combo a unit sold at a
    higher price level. Items without a price in Simphony, or days not held VAT included, count every unit."""
    if prices is None:
        try:
            prices = {}
            for r in simphony.menu_item_prices(ref):
                p = r.get("price")
                if p is not None and float(p) > 1:                         # 1 or less: placeholders, open prices
                    prices[r["num"]] = min(prices.get(r["num"], float(p)), float(p))
        except Exception:                                          # noqa: BLE001 - no prices: every unit counts
            return {}
    with _db_lock:
        rows = _db().execute("SELECT mi, SUM(sls), SUM(cnt) FROM miv WHERE ref=? AND day>=? AND day<=? GROUP BY mi",
                             (ref, a, b)).fetchall()
    out = {}
    for mi, sls, cnt in rows:
        p = prices.get(mi)
        if not p or not cnt or cnt <= 0:
            continue
        at_price = min(float(cnt), max(0.0, round((sls or 0) / p)))
        if at_price < cnt:
            out[mi] = at_price / cnt
    return out


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
    masters, costers, maps = {}, {}, {}
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
                maps[company] = bc_map(client, company, c_from.isoformat(), c_to.isoformat())
            except Exception as exc:                               # noqa: BLE001
                maps[company] = {}
                errors.append(f"The BC items of the menu items (POS import of {company}) could not be read: {str(exc)[:140]}")
        try:
            names = simphony.menu_items(ref)
        except Exception as exc:                                   # noqa: BLE001
            names = {}
            errors.append(f"{ref}: menu item names: {str(exc)[:120]}")
        out_locs.append({"ref": ref, "name": rg.get("name") or l.get("name") or ref, "brand": rg.get("brand") or "",
                         "company": company, "bc": rg.get("bc") or "",
                         "currency": rg.get("currency") or locintel.norm_currency(l.get("currency")) or fallback})
        lst = []
        try:
            prices = emc_prices(ref)
        except Exception as exc:                                   # noqa: BLE001
            prices = {}
            errors.append(f"{ref}: menu item prices (EMC): {str(exc)[:120]}")
        shares = priced_shares(ref, c_from.isoformat(), c_to.isoformat(), prices={k: v[1] for k, v in prices.items()})
        coster, bmap = costers[company], maps.get(company) or {}
        card_of, recipe_of = getattr(coster, "card", lambda it: None), getattr(coster, "recipe", lambda it: None)
        for mi, net, qty, disc, prep, rtn in sold.get(ref, []):
            nm = names.get(mi) or {}
            if nm.get("excluded") or (qty <= 0 and abs(net) < 0.01):
                continue
            name = nm.get("name") or f"Item {mi}"
            bc = bmap.get(str(mi)) or str(mi)                     # the BC item the POS import posts for it
            uc, basis = coster(bc)
            card = card_of(bc)
            vat = vat_pct(card)
            emc = (prices.get(mi) or (None, None))[0]
            if uc is None and prep > 0 and qty > 0:
                uc, basis = prep / qty, "Simphony prep cost"
            lst.append({"mi": mi, "name": name, "major": nm.get("major") or "Other", "family": nm.get("family") or "Other",
                        "qty": qty, "net": net, "disc": disc, "rtn": rtn, "unit_cost": uc, "basis": basis,
                        "priced_share": round(shares.get(mi, 1.0), 4),
                        "bc_item": bc if card else "", "emc": emc, "vat": vat,
                        "emc_ex": round(emc / (1 + vat / 100), 4) if emc and vat is not None else None,
                        "std_cost": round(float(card.get("Standard_Cost") or 0), 2) if card else None,
                        "recipe_cost": recipe_of(bc),
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
                                             "combo_qty": 0.0, "combo_cost": 0.0, "emcv": 0.0, "emcvi": 0.0,
                                             "emcq": 0.0, "emc_set": set(), "bc": set(), "std": None, "recipe": None,
                                             "bases": set(), "locations": set()})
                # units rung at no price inside a combo meal are apart: the price, popularity and margin of the item
                # are those of the units sold at a price; their cost is shown apart (it belongs to the combo)
                share = r.get("priced_share", 1.0)
                q, cq_ = r["qty"] * share, r["qty"] * (1 - share)
                a["qty"] += q
                a["combo_qty"] += cq_
                if r.get("bc_item"):
                    a["bc"].add(r["bc_item"])
                if r.get("std_cost") and a["std"] is None:
                    a["std"] = r["std_cost"] * f
                if r.get("recipe_cost") and a["recipe"] is None:
                    a["recipe"] = r["recipe_cost"] * f
                if r.get("emc"):
                    a["emc_set"].add(round(r["emc"] * f, 2))
                if r.get("emc_ex"):                                  # the EMC price without VAT, for the margin
                    a["emcv"] += r["emc_ex"] * q * f
                    a["emcvi"] += r["emc"] * q * f
                    a["emcq"] += q
                if r["unit_cost"] is not None:
                    a["combo_cost"] += r["unit_cost"] * cq_ * f
                a["net"] += r["net"] * f
                a["disc"] += r["disc"] * f
                a["rtn"] += r["rtn"]
                a["bases"].add(r["basis"])
                a["locations"].add(L["ref"])
                if r["unit_cost"] is not None:
                    a["costq"] += q
                    a["costv"] += r["unit_cost"] * q * f
        items, combo_only = [], []
        for a in agg.values():
            if a["qty"] <= 0:
                if a["combo_qty"] > 0:                             # rung only inside combo meals, at no price
                    combo_only.append({"mi": a["mi"], "name": a["name"], "section": a[sec_key],
                                       "combo_qty": round(a["combo_qty"], 2), "combo_cost": round(a["combo_cost"], 0)})
                continue
            avg = a["net"] / a["qty"]
            emc_ok = a["emcq"] >= a["qty"] * 0.999 and a["emcq"] > 0
            price = a["emcv"] / a["emcq"] if emc_ok else avg              # the menu price of EMC, else as received
            cost = a["costv"] / a["costq"] if a["costq"] >= a["qty"] * 0.999 and a["costq"] > 0 else None
            basis = sorted(a["bases"])
            it = {"mi": a["mi"], "name": a["name"], "major": a["major"], "family": a["family"],
                  "section": a[sec_key], "qty": round(a["qty"], 2), "net": round(a["net"], 0), "price": round(price, 2),
                  "disc": round(a["disc"], 0), "rtn": a["rtn"], "cost": round(cost, 2) if cost is not None else None,
                  "basis": basis[0] if len(basis) == 1 else ", ".join(basis),
                  "locations": sorted(a["locations"]),
                  "combo_qty": round(a["combo_qty"], 2), "combo_cost": round(a["combo_cost"], 0),
                  "price_source": "EMC" if emc_ok else "Average received",
                  "avg_price": round(avg, 2),
                  "emc_incl": round(a["emcvi"] / a["emcq"], 2) if emc_ok else None,
                  "emc_range": [min(a["emc_set"]), max(a["emc_set"])] if a["emc_set"] else None,
                  "bc_item": ", ".join(sorted(a["bc"])),
                  "std_cost": round(a["std"], 2) if a["std"] is not None else None,
                  "recipe_cost": round(a["recipe"], 2) if a["recipe"] is not None else None}
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
        collect.combo_only = combo_only
        return items, secs, avg

    items, secs, avg = collect(sel)
    combo_only = sorted(collect.combo_only, key=lambda x: -x["combo_qty"])
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
        if it["price_source"] != "EMC":
            flags.append("No price in EMC (or no VAT % on its BC item): margin on the average price received")
        if it["combo_qty"]:
            flags.append(f"{it['combo_qty']:.0f} more rung inside combo meals at no price: left out of its price and margin")
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
                     "noprice": sum(1 for it in items if it["cls"] == "noprice"),
                     # units rung inside combo meals at no price (their cost belongs to the combo, which BC does not
                     # cost): left out of the items' price and margin
                     "in_combos": round(sum(it["combo_qty"] for it in items) + sum(x["combo_qty"] for x in combo_only), 0),
                     "in_combos_cost": round(sum(it["combo_cost"] for it in items) + sum(x["combo_cost"] for x in combo_only), 0)},
            "combo_only": combo_only,
            "classes": by_cls, "labels": LABEL, "meaning": MEANING, "advice": ACTION,
            "items": sorted(items, key=lambda i: -i["net"]), "sections": sections, "actions": actions,
            "across": across[:200], "brands": brands,
            "locations": [{"ref": L["ref"], "name": L["name"], "brand": L["brand"]} for L in locs],
            "definitions": dict(DEFINITIONS, method=(
                f"Within each menu section (family group, or major group): popularity = the item's share of the items sold "
                f"in the section against {S['pop']:g}% of an equal share; contribution margin = the EMC menu price "
                f"without VAT (else the average price received) − portion "
                f"cost against the section's average margin "
                + ("weighted by items sold" if S["margin"] == "weighted" else "of its items, each counted once")
                + ". Star = popular and high margin; Plowhorse = popular, low margin; Puzzle = high margin, sold little; "
                  "Dog = low margin, sold little."))}


DEFINITIONS = {
    "sales": "Items sold and sales by menu item from Simphony (getMenuItemDailyTotals), sales less the VAT and before "
             "discounts; item discounts are shown apart. The period of the filters, to the last business day Simphony "
             "has closed.",
    "cost": "Portion cost from Business Central, on the BC item the POS import posts for the menu item "
            "(MIDSimphonyArchive), else the item with the menu item number: for an item with a recipe (Assembly BOM) "
            "its Standard Cost, else the recipe at today's unit costs (quantity per × unit conversion × unit cost); "
            "the item's unit cost for an item sold as bought; else the preparation cost Simphony holds.",
    "price": "The price is the menu price set in Simphony EMC (getMenuItemPrices, VAT included), without VAT at the "
             "VAT % of the BC item; the average price received (net sales / items sold) is shown beside it and used "
             "when EMC has no price.",
    "method": "Within each menu section (family group, or major group): popularity = the item's share of the items "
              "sold in the section against 70% of an equal share; contribution margin = the EMC menu price without VAT "
              "(else the average price received) − portion "
              "cost against the section's average margin weighted by items sold. Star = popular and high margin; "
              "Plowhorse = popular, low margin; Puzzle = high margin, sold little; Dog = low margin, sold little.",
    "combos": "A menu item rung inside a combo meal is at no price (the combo item carries the price, and BC holds no "
              "recipe for the combo). Those units are counted apart: the item's price, popularity and margin are of the "
              "units sold at a price, i.e. its sales VAT included / its lowest real price in Simphony (no more than the "
              "units sold). Their portion cost is shown apart and left out of the theoretical food cost with the combo's "
              "own sales.",
    "scope": "A brand or all of RHG sums its locations by menu item; another currency is converted at the group rates "
             "of Settings. POS technical items (Settings → Inventory) and items sold at no price stay out of the matrix.",
}


# ---------------------------------------------------------------- Excel
def columns_explained(C, S):
    """(column, how it is worked out) of the Items sheet, for its Read me."""
    pop = S.get("pop", 70)
    return [
        ("Menu item", "the Simphony menu item number (EMC)."),
        ("BC item", "the Business Central item the POS import posts for the menu item (MIDSimphonyArchive: Menu Item "
                    "Number → Item No.); blank when BC has no item for it. Its card, recipe and VAT % are used."),
        ("Section / Major group / Family group", "the Simphony groups of the menu item; the section is the family "
                                                 "group (or the major group, as chosen on the screen)."),
        ("Class", f"Star, Plowhorse, Puzzle or Dog within its section: popular when its Menu mix % is at least the "
                  f"section's Popularity line % ({pop:g}% of an equal share = {pop:g}% × 100 / items of the section); "
                  "a high margin when its Margin is at least the section's average margin (weighted by items sold, "
                  "or simple, as set in the Parameters). No cost = no portion cost in BC: ranked by popularity only."),
        ("Items sold", "units sold at a price in the period (Simphony getMenuItemDailyTotals, slsCnt), all the "
                       "locations of the selection; units rung inside combo meals at no price are not in it."),
        ("In combos at no price", "units rung inside combo meals at no price = units sold − its sales VAT included "
                                  "/ its lowest real EMC price (never below 0)."),
        ("Menu mix %", "Items sold of the item / Items sold of every item of its section × 100."),
        (f"Net sales {C}", "sales without VAT, before item discounts (Simphony slsTtl − VAT), of the period."),
        (f"EMC price incl. VAT {C}", "the menu price set in Simphony EMC (getMenuItemPrices): the lowest price level "
                                     "with a real price (above 1), the most common across revenue centres; for "
                                     "several locations, weighted by the units each sold."),
        ("EMC prices differ by location", "the lowest and the highest EMC price when the locations differ."),
        (f"EMC price excl. VAT {C}", "EMC price incl. VAT / (1 + VAT % of the BC item, from its VAT Prod. Posting "
                                     "Group, e.g. TVSH20 = 20%)."),
        (f"Avg price received {C}", "Net sales / Items sold: what came in per unit (after combos, before item discounts)."),
        ("Price used", "EMC when every unit has an EMC price and a VAT %; else Average received."),
        (f"Portion cost {C}", "the cost of one portion, by the Cost basis."),
        ("Cost basis", "Standard cost (Assembly BOM) = the Standard Cost of the BC item card for an item with a recipe "
                       "(BC rolls the BOM up into it); Recipe / Recipe, incomplete = without a Standard Cost, the BOM "
                       "at today's unit costs (incomplete: a component without cost or unit conversion); Item cost = "
                       "the unit cost of an item sold as bought; Simphony prep cost; Not in BC; No recipe, no cost."),
        (f"Standard cost (BC) {C}", "the Standard Cost field of the BC item card."),
        (f"Recipe at today's unit costs {C}", "Σ over the BOM lines of quantity per × unit conversion × the "
                                              "component's unit cost today, to compare with the Standard Cost."),
        ("Cost %", "Portion cost / price used × 100."),
        (f"Margin {C}", "price used − Portion cost (contribution margin of one unit)."),
        (f"Total margin {C}", "Margin × Items sold."),
        (f"Item discounts {C}", "item discounts without VAT (Simphony dscTtl − dscVatTtl), shown apart."),
        ("Locations", "the Simphony locations (locRef) that sold the item in the period."),
        ("Operations / Sales", "what to do by its class."),
        ("Flags", "cost above the target, recipe incomplete, no BC item, discounts above the limit, returns, units in "
                  "combos, no EMC price."),
    ]


def sections_explained(C, S):
    return [
        ("Items / Items sold / Net sales", "the items of the section with a price, their units sold and sales."),
        (f"Total margin {C}", "Σ Total margin of its items with a cost."),
        ("Cost %", "Σ (Portion cost × Items sold) / Σ Net sales of its items with a cost × 100."),
        (f"Avg margin {C}", "the section's average margin: Σ (Margin × Items sold) / Σ Items sold of its items with a "
                            "cost (or the simple mean, as set in the Parameters)."),
        ("Popularity line %", f"{S.get('pop', 70):g}% × 100 / items of the section."),
        ("Stars … No cost", "the items of each class."),
    ]


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
    rng = lambda it: (f"{it['emc_range'][0]:,.2f} – {it['emc_range'][1]:,.2f}" if it.get("emc_range")
                      and it["emc_range"][0] != it["emc_range"][1] else None)
    sheet("Items", ["Menu item", "BC item", "Name", "Section", "Major group", "Family group", "Class", "Items sold",
                    "In combos at no price", "Menu mix %", f"Net sales {C}", f"EMC price incl. VAT {C}",
                    "EMC prices differ by location", f"EMC price excl. VAT {C}", f"Avg price received {C}",
                    "Price used", f"Portion cost {C}", "Cost basis", f"Standard cost (BC) {C}",
                    f"Recipe at today's unit costs {C}", "Cost %", f"Margin {C}", f"Total margin {C}",
                    f"Item discounts {C}", "Locations", "Operations", "Sales", "Flags"],
          [[it["mi"], it.get("bc_item") or None, it["name"], it["section"], it["major"], it["family"],
            L.get(it["cls"], it["cls"]), it["qty"], it.get("combo_qty") or None,
            round(it["mm"] * 100, 2) if it.get("mm") is not None else None, it["net"], it.get("emc_incl"), rng(it),
            it["price"] if it.get("price_source") == "EMC" else None, it.get("avg_price", it["price"]),
            it.get("price_source") or "Average received", it["cost"], it["basis"], it.get("std_cost"),
            it.get("recipe_cost"), it["cost_pct"], it["cm"], it["cm_total"], it["disc"], ", ".join(it["locations"]),
            it["ops"], it["sales"], "; ".join(it["flags"])] for it in r["items"]],
          [None] * 7 + [qty, qty, '0.00', money, '#,##0.00', None, '#,##0.00', '#,##0.00', None, '#,##0.00', None,
                        '#,##0.00', '#,##0.00', pct, '#,##0.00', money, money] + [None] * 4,
          [11, 11, 34, 22, 16, 22, 11, 10, 10, 9, 13, 12, 14, 12, 12, 12, 11, 24, 12, 12, 8, 11, 13, 12, 20, 60, 60, 60],
          first=True)
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
               + [[v] for v in DEFINITIONS.values()]
               + [[""], ["How each column of Items is worked out"]]
               + [[f"{k}: {v}"] for k, v in columns_explained(C, r["settings"])]
               + [[""], ["How each column of Sections is worked out"]]
               + [[f"{k}: {v}"] for k, v in sections_explained(C, r["settings"])]
               + [["Simphony and Business Central were only read."]], [None], [140])
    for row in ws.iter_rows(min_row=2):
        row[0].alignment = Alignment(wrap_text=True, vertical="top")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
