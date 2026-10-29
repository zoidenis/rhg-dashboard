"""Price savings report: what paying this year's prices instead of last year's saved or cost.

Source: posted purchase invoice lines (Type = Item) with their headers. Each line gives
the supplier, the item, the stock location, the quantity in the line's unit of measure
and the unit cost in local currency excluding VAT (Unit_Cost_LCY, after discounts).
Quantities are converted to the item's base unit with Item Units of Measure, so a
case and a bottle compare correctly.

Price effect, for each item bought in both periods:
    (this period's average unit cost - same dates last year's) x this period's quantity
Negative is a saving, positive is extra spending. The rest of the change in spend is
volume and mix: more or less bought, items new this year or no longer bought.

The lines are held in a local SQLite file, summed by day, location, supplier and item.
A month is read once from Business Central and refreshed while it can still change.
"""
import io
import re
import sqlite3
import threading
import time
from calendar import monthrange
from datetime import date, timedelta

import classify
import config
import store

DB_PATH = config.BASE_DIR / "data" / "savings.db"
OPEN_DAYS = 45                # invoices still arrive this long after a month ends
MAX_PRICE_JUMP = 3.0          # a unit cost over 4x or under 1/4 of last year's is a unit/data error
_lock = threading.Lock()
_conn = None
_load_lock = threading.Lock()
_uom = {"at": {}, "map": {}}


def _db():
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript("""
            CREATE TABLE IF NOT EXISTS buys (
                company TEXT, day TEXT, location TEXT, dept TEXT, vendor TEXT, item TEXT,
                cost REAL, qty REAL, lines INTEGER);
            CREATE INDEX IF NOT EXISTS buys_day ON buys (company, day);
            CREATE TABLE IF NOT EXISTS loaded (
                company TEXT, month TEXT, date_to TEXT, at REAL, PRIMARY KEY (company, month));
            CREATE TABLE IF NOT EXISTS names (
                company TEXT, kind TEXT, code TEXT, name TEXT, PRIMARY KEY (company, kind, code));
        """)
    return _conn


# ---------------------------------------------------------------- reading BC
def _uoms(client, company):
    """{(item, uom): qty per unit of measure}, read once a day."""
    if time.time() - _uom["at"].get(company, 0) < 86400 and company in _uom["map"]:
        return _uom["map"][company]
    rows = client._get_all(client._url(company, "ItemUnitsOfMeasure"),
                           {"$select": "Item_No,Code,Qty_per_Unit_of_Measure"})
    m = {(str(r.get("Item_No")), str(r.get("Code") or "").upper()): float(r.get("Qty_per_Unit_of_Measure") or 1)
         for r in rows}
    _uom["map"][company], _uom["at"][company] = m, time.time()
    return m


def _month_bounds(month, today):
    y, m = int(month[:4]), int(month[5:7])
    a = date(y, m, 1)
    b = min(date(y, m, monthrange(y, m)[1]), today)
    return a, b


def _fresh(company, month, b, today, force):
    with _lock:
        row = _db().execute("SELECT date_to, at FROM loaded WHERE company=? AND month=?",
                            (company, month)).fetchone()
    if not row:
        return False
    date_to, at = row
    age = time.time() - at
    if date_to < b.isoformat():
        return False                      # the month has grown since it was read
    open_month = (today - b).days <= OPEN_DAYS
    if force and open_month and age > 120:
        return False
    ttl = 3600 if b >= today - timedelta(days=1) else (6 * 3600 if open_month else 30 * 86400)
    return age < ttl


def load_month(client, company, month, today=None, force=False):
    """Reads one month of item invoice lines into the local file, unless it is fresh."""
    today = today or date.today()
    a, b = _month_bounds(month, today)
    if a > today or _fresh(company, month, b, today, force):
        return False
    headers = client.posted_invoice_headers(company, a.isoformat(), b.isoformat())
    live = {str(h["No"]): h for h in headers
            if h.get("No") and not (h.get("Cancelled") or h.get("Corrective"))}
    lines = []
    if live:
        docs = sorted(live)
        lines = client._get_all(client._url(company, config.BC_PINV_LINE_ENTITY),
                                {"$filter": f"Type eq 'Item' and Document_No ge '{docs[0]}' "
                                            f"and Document_No le '{docs[-1]}'",
                                 "$select": "Document_No,No,Description,Quantity,Unit_of_Measure_Code,"
                                            "Unit_Cost_LCY,Shortcut_Dimension_1_Code"})
    uom = _uoms(client, company) if lines else {}
    agg, items, vendors = {}, {}, {}
    for l in lines:
        h = live.get(str(l.get("Document_No")))
        item = str(l.get("No") or "")
        qty = float(l.get("Quantity") or 0)
        if not h or not item or not qty:
            continue
        per = uom.get((item, str(l.get("Unit_of_Measure_Code") or "").upper()), 1.0) or 1.0
        cost = float(l.get("Unit_Cost_LCY") or 0) * qty
        vendor = str(h.get("Buy_from_Vendor_No") or "")
        key = (str(h.get("Posting_Date"))[:10], str(h.get("Location_Code") or ""),
               str(l.get("Shortcut_Dimension_1_Code") or h.get("Shortcut_Dimension_1_Code") or ""),
               vendor, item)
        x = agg.setdefault(key, [0.0, 0.0, 0])
        x[0] += cost
        x[1] += qty * per
        x[2] += 1
        if l.get("Description"):
            items[item] = l["Description"].strip()
        vendors[vendor] = h.get("Buy_from_Vendor_Name") or vendor
    with _lock:
        db = _db()
        db.execute("DELETE FROM buys WHERE company=? AND day>=? AND day<=?",
                   (company, a.isoformat(), date(a.year, a.month, monthrange(a.year, a.month)[1]).isoformat()))
        db.executemany("INSERT INTO buys VALUES (?,?,?,?,?,?,?,?,?)",
                       [(company, *k, round(v[0], 2), round(v[1], 4), v[2]) for k, v in agg.items()])
        db.executemany("INSERT OR REPLACE INTO names VALUES (?,?,?,?)",
                       [(company, "item", k, v) for k, v in items.items()]
                       + [(company, "vendor", k, v) for k, v in vendors.items()])
        db.execute("INSERT OR REPLACE INTO loaded VALUES (?,?,?,?)", (company, month, b.isoformat(), time.time()))
        db.commit()
    return True


def months_between(a, b):
    out, y, m = [], a.year, a.month
    while (y, m) <= (b.year, b.month):
        out.append(f"{y}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def last_year(d):
    try:
        return d.replace(year=d.year - 1)
    except ValueError:
        return date(d.year - 1, 2, 28)


def needed_months(a, b):
    return sorted(set(months_between(a, b)) | set(months_between(last_year(a), last_year(b))))


def missing_months(company, months, today=None):
    today = today or date.today()
    return [m for m in months if not _fresh(company, m, _month_bounds(m, today)[1], today, False)]


def ensure(client, company, months, force=False, tick=None):
    """Loads the months one at a time (a month is tens of thousands of lines)."""
    errors = []
    with _load_lock:
        for m in months:
            try:
                load_month(client, company, m, force=force)
            except Exception as exc:                               # noqa: BLE001
                errors.append(f"{m}: {str(exc)[:200]}")
            if tick:
                tick()
    return errors


# ---------------------------------------------------------------- figures
def rows(company, a, b, location=None, vendor=None):
    sql = ("SELECT location, vendor, item, SUM(cost), SUM(qty), SUM(lines) FROM buys "
           "WHERE company=? AND day>=? AND day<=?")
    args = [company, a.isoformat(), b.isoformat()]
    if location:
        sql += " AND location=?"
        args.append(location)
    if vendor:
        sql += " AND vendor=?"
        args.append(vendor)
    sql += " GROUP BY location, vendor, item"
    with _lock:
        return _db().execute(sql, args).fetchall()


def names(company, kind):
    with _lock:
        return dict(_db().execute("SELECT code, name FROM names WHERE company=? AND kind=?",
                                  (company, kind)).fetchall())


def effect(cur, ly):
    """cur, ly: {key: [cost, qty]} -> price effect detail and totals."""
    cur_total = sum(v[0] for v in cur.values())
    ly_total = sum(v[0] for v in ly.values())
    save = extra = matched = excluded_cost = 0.0
    excluded, detail = 0, {}
    for k, (c, q) in cur.items():
        p = ly.get(k)
        if not p or q <= 0 or c <= 0 or p[1] <= 0 or p[0] <= 0:
            continue
        ca, la = c / q, p[0] / p[1]
        if ca > la * (1 + MAX_PRICE_JUMP) or ca < la / (1 + MAX_PRICE_JUMP):
            excluded += 1
            excluded_cost += c
            detail[k] = ("excluded", ca, la, None)
            continue
        e = (ca - la) * q
        matched += c
        if e < 0:
            save -= e
        else:
            extra += e
        detail[k] = ("both", ca, la, e)
    net = extra - save
    return {"cur": cur_total, "ly": ly_total, "save": save, "extra": extra, "net": net,
            "matched": matched, "volume": (cur_total - ly_total) - net,
            "items": sum(1 for d in detail.values() if d[0] == "both"),
            "excluded": excluded, "excluded_cost": excluded_cost}, detail


def _sum(rows_in, key_fn, keep=None):
    out = {}
    for loc, ven, item, c, q, n in rows_in:
        if keep and not keep(loc, ven, item):
            continue
        k = key_fn(loc, ven, item)
        x = out.setdefault(k, [0.0, 0.0])
        x[0] += c or 0
        x[1] += q or 0
    return out


def item_attributes(client, company, settings=None):
    """{item: (category code, purchase type)} from the item cards and the tower's mapping."""
    settings = settings or store.get_settings()
    try:
        catalog = client.item_catalog(company)
    except Exception:                                              # noqa: BLE001
        catalog = {}
    maps = classify.parse_maps(settings.get("classification") or {})
    out = {}
    for no, card in catalog.items():
        ptype, _, _ = classify.classify_item(card, maps)
        out[no] = (card.get("code") or "", ptype)
    return out


def report(client, company, a, b, location="", vendor="", category="", stream="", q="", today=None):
    """The full report for any date range against the same dates last year."""
    la, lb = last_year(a), last_year(b)
    attrs = item_attributes(client, company)
    item_names, vendor_names = names(company, "item"), names(company, "vendor")
    ql = (q or "").strip().lower()

    def keep(loc, ven, item):
        cat, typ = attrs.get(item, ("", "Unclassified"))
        if category and cat != category:
            return False
        if stream and typ != stream:
            return False
        if ql and ql not in item.lower() and ql not in (item_names.get(item) or "").lower():
            return False
        return True
    cur_rows = rows(company, a, b, location or None, vendor or None)
    ly_rows = rows(company, la, lb, location or None, vendor or None)

    by_item_c, by_item_l = _sum(cur_rows, lambda l, v, i: i, keep), _sum(ly_rows, lambda l, v, i: i, keep)
    total, detail = effect(by_item_c, by_item_l)

    vend_c, vend_l = _sum(cur_rows, lambda l, v, i: v, keep), _sum(ly_rows, lambda l, v, i: v, keep)
    item_vendors = {}
    for loc, ven, item, c, *_ in cur_rows:
        if keep(loc, ven, item):
            item_vendors.setdefault(item, {}).setdefault(ven, 0.0)
            item_vendors[item][ven] += c or 0

    items = []
    for it in set(by_item_c) | set(by_item_l):
        c = by_item_c.get(it, [0.0, 0.0])
        p = by_item_l.get(it, [0.0, 0.0])
        d = detail.get(it)
        status = d[0] if d else ("new" if c[0] and not p[0] else "dropped" if p[0] and not c[0] else "other")
        cat, typ = attrs.get(it, ("", "Unclassified"))
        top_v = sorted((item_vendors.get(it) or {}).items(), key=lambda kv: -kv[1])
        items.append({"item": it, "desc": item_names.get(it, ""), "category": cat, "stream": typ,
                      "cur_cost": round(c[0], 2), "cur_qty": round(c[1], 3),
                      "cur_avg": round(c[0] / c[1], 4) if c[1] > 0 else None,
                      "ly_cost": round(p[0], 2), "ly_qty": round(p[1], 3),
                      "ly_avg": round(p[0] / p[1], 4) if p[1] > 0 else None,
                      "effect": round(d[3], 2) if d and d[3] is not None else None,
                      "pct": round((d[1] - d[2]) / d[2] * 100, 2) if d else None,
                      "status": status, "vendors": [vendor_names.get(v, v) for v, _ in top_v[:3]]})
    items.sort(key=lambda r: (r["effect"] is None, -abs(r["effect"] or 0), -r["cur_cost"]))

    def group(key_of):
        out = {}
        for it, d in detail.items():
            if d[0] != "both":
                continue
            g = out.setdefault(key_of(it), {"save": 0.0, "extra": 0.0, "items": 0})
            g["items"] += 1
            if d[3] < 0:
                g["save"] -= d[3]
            else:
                g["extra"] += d[3]
        for it, (c, _) in by_item_c.items():
            out.setdefault(key_of(it), {"save": 0.0, "extra": 0.0, "items": 0}).setdefault("cur", 0.0)
            out[key_of(it)]["cur"] = out[key_of(it)].get("cur", 0.0) + c
        for it, (c, _) in by_item_l.items():
            out.setdefault(key_of(it), {"save": 0.0, "extra": 0.0, "items": 0})
            out[key_of(it)]["ly"] = out[key_of(it)].get("ly", 0.0) + c
        return [{"key": k, "cur": round(v.get("cur", 0), 0), "ly": round(v.get("ly", 0), 0),
                 "save": round(v["save"], 0), "extra": round(v["extra"], 0),
                 "net": round(v["extra"] - v["save"], 0), "items": v["items"]}
                for k, v in out.items()]

    categories = sorted(group(lambda it: attrs.get(it, ("", ""))[0] or "(no category)"), key=lambda g: g["net"])
    streams = sorted(group(lambda it: attrs.get(it, ("", "Unclassified"))[1]), key=lambda g: g["net"])

    # suppliers and locations: the price effect on what was bought from that supplier /
    # at that location in both periods, so a change of supplier shows as volume there
    def by_pair(key_idx):
        kc = _sum(cur_rows, lambda l, v, i: ((l, v)[key_idx], i), keep)
        kl = _sum(ly_rows, lambda l, v, i: ((l, v)[key_idx], i), keep)
        keys = {k[0] for k in kc} | {k[0] for k in kl}
        out = []
        for g in keys:
            c = {k[1]: v for k, v in kc.items() if k[0] == g}
            p = {k[1]: v for k, v in kl.items() if k[0] == g}
            t, _ = effect(c, p)
            out.append({"key": g, "cur": round(t["cur"], 0), "ly": round(t["ly"], 0), "save": round(t["save"], 0),
                        "extra": round(t["extra"], 0), "net": round(t["net"], 0), "items": t["items"]})
        return sorted(out, key=lambda g: -g["cur"])
    vendors = by_pair(1)
    for v in vendors:
        v["name"] = vendor_names.get(v["key"], v["key"])
    locations = by_pair(0)

    # month by month inside the range, each against the same dates last year
    months = []
    for m in months_between(a, b):
        ma, mb = _month_bounds(m, today or date.today())
        ma, mb = max(ma, a), min(mb, b)
        c = _sum(rows(company, ma, mb, location or None, vendor or None), lambda l, v, i: i, keep)
        p = _sum(rows(company, last_year(ma), last_year(mb), location or None, vendor or None),
                 lambda l, v, i: i, keep)
        t, _ = effect(c, p)
        months.append({"month": m, "from": ma.isoformat(), "to": mb.isoformat(),
                       **{k: round(t[k], 0) for k in ("cur", "ly", "save", "extra", "net", "volume")}})

    # what the filters can offer: everything bought in either period, before the filters
    all_c = rows(company, a, b) + rows(company, la, lb)
    return {
        "period": {"from": a.isoformat(), "to": b.isoformat()},
        "last_year": {"from": la.isoformat(), "to": lb.isoformat()},
        "filters": {"location": location, "vendor": vendor, "category": category, "stream": stream, "q": q},
        "summary": {k: (round(v, 0) if isinstance(v, float) else v) for k, v in total.items()},
        "items": items[:3000], "items_total": len(items),
        "vendors": vendors, "categories": categories, "streams": streams, "locations": locations,
        "months": months, "max_jump_pct": MAX_PRICE_JUMP * 100,
        "options": {
            "locations": sorted({r[0] for r in all_c if r[0]}),
            "vendors": sorted(({"no": v, "name": vendor_names.get(v, v)} for v in {r[1] for r in all_c if r[1]}),
                              key=lambda x: x["name"].lower()),
            "categories": sorted({attrs.get(r[2], ("", ""))[0] for r in all_c} - {""}),
            "streams": sorted({attrs.get(r[2], ("", "Unclassified"))[1] for r in all_c}),
        },
        "definitions": DEFINITIONS,
    }


DEFINITIONS = {
    "method": "Savings and extra spending are the price effect against the same dates last year: for each "
              "item bought in both periods, (this period's average unit cost - last year's) x this period's "
              "quantity. Cheaper is a saving, dearer is extra spending.",
    "source": "Posted purchase invoice lines of type Item, unit cost in local currency excluding VAT after "
              "discounts, by posting date of the invoice. Quantities are converted to the item's base unit. "
              "Goods received but not yet invoiced are not counted until the invoice is posted.",
    "volume": "Volume and mix is the rest of the change in spend: more or less bought, and items bought in "
              "only one of the two periods (new or dropped).",
    "excluded": "An item whose unit cost moved more than four times either way is left out of the price "
                "effect as a likely unit of measure or entry error, and counted in volume and mix.",
    "suppliers": "By supplier and by location, only what was bought from that supplier or at that location "
                 "in both periods is compared, so moving an item to another supplier shows as volume there; "
                 "the total above compares each item across all suppliers.",
}


# ---------------------------------------------------------------- the executive dashboard
_dash_memo = {}


def dashboard_ranges(company, year, months_keys, today=None):
    """For the executive dashboard: every month / quarter-to-date / year-to-date range of the
    year, whole company and each location, against the same dates last year."""
    today = today or date.today()
    with _lock:
        sig = tuple(_db().execute("SELECT month, at FROM loaded WHERE company=? ORDER BY month",
                                  (company,)).fetchall())
    key = (company, year, sig, today.isoformat())
    if key in _dash_memo:
        return _dash_memo[key]
    item_names = names(company, "item")
    ranges, used = {}, set()
    for end in [k for k in months_keys if k.startswith(str(year))]:
        m = int(end[5:])
        for mode, start in (("month", m), ("quarter", (m - 1) // 3 * 3 + 1), ("ytd", 1)):
            a = date(year, start, 1)
            b = min(date(year, m, monthrange(year, m)[1]), today)
            cur, ly = rows(company, a, b), rows(company, last_year(a), last_year(b))
            if not cur and not ly:
                continue
            out = {}
            for scope in {"*"} | {r[0] for r in cur} | {r[0] for r in ly}:
                keep = None if scope == "*" else (lambda l, v, i, s=scope: l == s)
                c, p = _sum(cur, lambda l, v, i: i, keep), _sum(ly, lambda l, v, i: i, keep)
                t, detail = effect(c, p)
                its = sorted(([i, d[1], d[2], c[i][1], d[3]] for i, d in detail.items() if d[0] == "both"),
                             key=lambda x: x[4])
                best = [[x[0], round(x[1], 2), round(x[2], 2), round(x[3], 2), round(x[4], 0)]
                        for x in its if x[4] < 0][:6]
                worst = [[x[0], round(x[1], 2), round(x[2], 2), round(x[3], 2), round(x[4], 0)]
                         for x in reversed(its) if x[4] > 0][:6]
                used.update(x[0] for x in best + worst)
                out[scope] = {**{k: round(v, 0) if isinstance(v, float) else v for k, v in t.items()},
                              "best": best, "worst": worst}
            ranges[f"{mode}|{end}"] = out
    result = {"ranges": ranges, "names": {k: item_names.get(k, "") for k in used},
              "max_jump_pct": MAX_PRICE_JUMP * 100}
    _dash_memo.clear()
    _dash_memo[key] = result
    return result
