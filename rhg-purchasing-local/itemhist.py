"""Prices & contracts → Item history: what each item cost in the period chosen above, from which suppliers, and how
its price moved.

Source: the posted purchase invoice lines held for Price savings (savings.py: item lines of the invoices that are
neither cancelled nor corrective, unit cost in local currency excluding VAT after discounts, the quantity in the
item's base unit, summed by day, location and supplier). Credit memos are not in these lines.

For each item:
  * a price is one purchase: one day, one location, one supplier, its cost / its quantity;
  * average price = cost / quantity of the period (weighted by what was bought);
  * min and max = the lowest and highest of those prices; a price over four times or under a quarter of the item's
    median is a unit or data error, shown apart and left out of min and max (the average keeps the real cost);
  * last price = the price of the latest purchase (a unit or data error left out); spread = max / min − 1;
  * comparison = the same figures in the comparison period chosen above.
Nothing is written to Business Central.
"""
import time
from collections import defaultdict
from datetime import date, timedelta

import savings
import savings_report

JUMP = 4.0
MAX_ITEMS = 600


def ensure(client, company, a, b, refresh=False):
    """None when the months of a..b are held in the Price savings file; else the progress of the read running
    (the same background read as Price savings, one month at a time)."""
    months = savings.months_between(a, b)
    missing = savings.missing_months(company, months)
    if refresh:
        recent = [m for m in months if m >= (date.today() - timedelta(days=savings.OPEN_DAYS)).strftime("%Y-%m")]
        missing = sorted(set(missing) | set(recent))
    with savings_report._jobs_lock:
        job = dict(savings_report._jobs.get(company) or {})
    if (job.get("state") == "done" and job.get("errors") and not refresh
            and time.time() - (job.get("finished") or 0) < 120):
        return None             # the months that failed are named on the screen, not asked again at once
    if job.get("state") == "running" or missing:
        if job.get("state") != "running":
            job = dict(savings_report._start_load(client, company, sorted(missing, reverse=True), refresh))
        return {"done": job.get("done", 0), "total": job.get("total", 0)}
    return None


def last_errors(company):
    with savings_report._jobs_lock:
        return list((savings_report._jobs.get(company) or {}).get("errors") or [])


def parse_range(a, b):
    a, b = date.fromisoformat(str(a)[:10]), date.fromisoformat(str(b)[:10])
    if b < a:
        a, b = b, a
    if (b - a).days > 731:
        raise ValueError("Choose a period of two years or less.")
    return a, b


def _median(v):
    s = sorted(v)
    n = len(s)
    return (s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2) if n else None


def _stats(buys):
    """buys: [(day, location, vendor, cost, qty)] -> the figures of one item (or one supplier of it)."""
    pos = [b for b in buys if b[4] > 0 and b[3] > 0]
    cost, qty = sum(b[3] for b in buys), sum(b[4] for b in buys)
    prices = [b[3] / b[4] for b in pos]
    med = _median(prices)
    ok = [p for p in prices if med and med / JUMP <= p <= med * JUMP]
    good = [b for b in pos if med and med / JUMP <= b[3] / b[4] <= med * JUMP]       # the last and first leave errors out
    last = max(good, key=lambda b: b[0]) if good else None
    first = min(good, key=lambda b: b[0]) if good else None
    return {"cost": round(cost, 2), "qty": round(qty, 4), "avg": round(cost / qty, 4) if qty > 0 else None,
            "min": round(min(ok), 4) if ok else None, "max": round(max(ok), 4) if ok else None,
            "median": round(med, 4) if med else None,
            "spread": round((max(ok) / min(ok) - 1) * 100, 1) if ok and min(ok) > 0 else None,
            "last": round(last[3] / last[4], 4) if last else None, "last_day": last[0] if last else None,
            "first": round(first[3] / first[4], 4) if first else None, "first_day": first[0] if first else None,
            "buys": len(pos), "outliers": len(prices) - len(ok)}


def report(client, company, a, b, pa, pb, location="", only=None, filters=None, currency="ALL", errors=None):
    filters = filters or {}
    subs = {}
    attrs = savings.item_attributes(client, company, subs=subs)
    try:
        catalog = client.item_catalog(company)
    except Exception:                                              # noqa: BLE001
        catalog = {}
    item_names, vendor_names = savings.names(company, "item"), savings.names(company, "vendor")
    ql = (filters.get("q") or "").strip().lower()
    want_vendor, want_item = filters.get("vendor") or "", filters.get("item") or ""
    want_cat, want_stream = filters.get("category") or "", filters.get("stream") or ""

    def keep(loc, ven, item):
        if location and (loc or "").upper() != location.upper():
            return False
        if only is not None and (loc or "").upper() not in only:
            return False
        if want_vendor and ven != want_vendor:
            return False
        cat, typ = attrs.get(item, ("", "Unclassified"))
        if want_cat and cat != want_cat:
            return False
        if want_stream and typ != want_stream:
            return False
        if ql and ql not in item.lower() and ql not in (item_names.get(item) or "").lower():
            return False
        return True

    def read(x, y):
        out = defaultdict(list)
        for day, loc, ven, item, cost, qty in savings.daily(company, x, y):
            if keep(loc, ven, item):
                out[item].append((day, loc or "", ven or "", cost or 0.0, qty or 0.0))
        return out
    cur, prev = read(a, b), read(pa, pb)
    rows = []
    for item, buys in cur.items():
        st = _stats(buys)
        pst = _stats(prev.get(item, [])) if prev.get(item) else None
        by_v = defaultdict(float)
        for d, l, v, c, q in buys:
            by_v[v] += c
        main = max(by_v.items(), key=lambda kv: kv[1])[0] if by_v else ""
        cat, typ = attrs.get(item, ("", "Unclassified"))
        rows.append({"item": item, "desc": item_names.get(item) or (catalog.get(item) or {}).get("desc") or "",
                     "category": cat, "stream": typ, "sub": (subs.get(item) or ("", ""))[0],
                     "uom": (catalog.get(item) or {}).get("uom") or "", **st,
                     "suppliers": len(by_v), "main_supplier": vendor_names.get(main, main),
                     "locations": len({l for _, l, _, _, _ in buys}),
                     "prev_avg": pst["avg"] if pst else None, "prev_qty": pst["qty"] if pst else None,
                     "change_pct": round((st["avg"] / pst["avg"] - 1) * 100, 1) if pst and pst["avg"] and st["avg"] else None})
    rows.sort(key=lambda r: -r["cost"])
    out = {"available": True, "company": company, "currency": currency, "location": location,
           "period": {"from": a.isoformat(), "to": b.isoformat()}, "prior": {"from": pa.isoformat(), "to": pb.isoformat()},
           "count": len(rows), "rows": rows[:MAX_ITEMS], "errors": errors or [],
           "kpis": {"items": len(rows), "spend": round(sum(r["cost"] for r in rows), 2),
                    "suppliers": len({v for bs in cur.values() for _, _, v, _, _ in bs}),
                    "up": sum(1 for r in rows if (r["change_pct"] or 0) >= 2),
                    "down": sum(1 for r in rows if (r["change_pct"] or 0) <= -2),
                    "multi": sum(1 for r in rows if r["suppliers"] > 1),
                    "spread": sum(1 for r in rows if (r["spread"] or 0) >= 10)},
           "options": {"vendors": sorted(({"no": k, "name": v} for k, v in vendor_names.items()), key=lambda x: x["name"] or ""),
                       "categories": sorted({c for c, _ in attrs.values() if c}),
                       "streams": sorted({t for _, t in attrs.values() if t})},
           "definitions": {
               "source": "Posted purchase invoice lines of items (cancelled and corrective invoices left out), unit cost in "
                         "local currency excluding VAT after discounts, quantities in the item's base unit.",
               "price": "A price is one purchase: one day, one location, one supplier, its cost divided by its quantity.",
               "avg": "Average price: cost / quantity of the period, so a large purchase weighs more than a small one.",
               "minmax": "Min and max: the lowest and highest price paid. A price over four times or under a quarter of the "
                         "item's median is a unit or data error: shown apart, not counted in min and max.",
               "change": "Change: the average price against the average price of the comparison period chosen above.",
               "spread": "Spread: how far apart the highest and the lowest price are (max / min − 1)."}}
    if want_item:
        out["detail"] = detail(want_item, cur.get(want_item, []), prev.get(want_item, []), vendor_names, a, b)
        r = next((x for x in rows if x["item"] == want_item), None)
        out["detail"]["row"] = r
    return out


def detail(item, buys, prev_buys, vendor_names, a, b):
    """One item: its suppliers, its locations, every purchase and the price by week or month."""
    by_v, by_l = defaultdict(list), defaultdict(list)
    for x in buys:
        by_v[x[2]].append(x)
        by_l[x[1]].append(x)
    pv = defaultdict(list)
    for x in prev_buys:
        pv[x[2]].append(x)
    suppliers = []
    for v, bs in by_v.items():
        st = _stats(bs)
        pst = _stats(pv[v]) if pv.get(v) else None
        suppliers.append({"no": v, "name": vendor_names.get(v, v), **st, "prev_avg": pst["avg"] if pst else None})
    suppliers.sort(key=lambda s: -s["cost"])
    locations = sorted(({"code": l or "–", **_stats(bs)} for l, bs in by_l.items()), key=lambda s: -s["cost"])
    weekly = (b - a).days <= 92
    buckets = defaultdict(lambda: [0.0, 0.0, None, None])

    def key(day):
        d = date.fromisoformat(day)
        return (d - timedelta(days=d.weekday())).isoformat() if weekly else day[:7]
    for d, l, v, c, q in buys:
        if q <= 0 or c <= 0:
            continue
        k = buckets[key(d)]
        k[0] += c
        k[1] += q
        p = c / q
        k[2] = p if k[2] is None else min(k[2], p)
        k[3] = p if k[3] is None else max(k[3], p)
    series = [[k, round(v[0] / v[1], 4), round(v[2], 4), round(v[3], 4), round(v[1], 3)] for k, v in sorted(buckets.items())]
    med = _median([c / q for _, _, _, c, q in buys if q > 0 and c > 0])
    purchases = [[d, l, vendor_names.get(v, v), round(q, 3), round(c / q, 4) if q else None, round(c, 2),
                  bool(med and q > 0 and c > 0 and not (med / JUMP <= c / q <= med * JUMP))]
                 for d, l, v, c, q in sorted(buys, key=lambda x: x[0], reverse=True)]
    return {"item": item, "by": "week" if weekly else "month", "series": series, "suppliers": suppliers,
            "locations": locations, "purchases": purchases[:400], "purchases_count": len(purchases),
            "prev": _stats(prev_buys) if prev_buys else None}
