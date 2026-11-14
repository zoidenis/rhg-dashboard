"""Turns raw purchase value entries into dashboard numbers."""
from collections import defaultdict
import currency
from calendar import monthrange
from datetime import date, timedelta

import config


MODES = ("week", "month", "quarter", "ytd", "range")


def split_mode(mode):
    """"month" or "month+ly" (compared with the same dates last year) -> ("month", "prior" | "last_year")."""
    base, _, cmp = str(mode or "week").lower().partition("+")
    base = "ytd" if base == "year" else base
    return (base if base in MODES else "week"), ("last_year" if cmp == "ly" else "prior")


def _last_year(d):
    try:
        return d.replace(year=d.year - 1)
    except ValueError:          # 29 February
        return date(d.year - 1, 2, 28)


def periods_for(start=None, mode="week", today=None, end=None):
    """Returns (current_from, current_to, prior_from, prior_to, like_for_like, label).

    mode: week    - Monday to Sunday, or Monday to today for the running week
          month   - first of the month to month end, or to today for the running month
          quarter - first of the quarter to its end, or to today for the running quarter
          ytd     - 1 January to today, or a full calendar year when a past year is chosen
          range   - start to end, chosen by hand (never past today)
    "+ly" after the mode compares with the same dates last year instead of the period before.
    The period before covers the same number of days while the period is running, so a
    part-period is never compared against a full one; a finished month or quarter is
    compared with the whole month or quarter before it.
    """
    today = today or date.today()
    mode, cmp = split_mode(mode)
    c_from, c_to, p_from, p_to, running, label = _periods(start, mode, today, end)
    if cmp == "last_year":
        p_from, p_to = _last_year(c_from), _last_year(c_to)
    return c_from, c_to, p_from, p_to, running, label


def _periods(start, mode, today, end):
    if mode == "range":
        a = start or today.replace(day=1)
        b = min(end or today, today)
        a = min(a, today)
        if b < a:
            a, b = b, a
        a = max(a, b - timedelta(days=3 * 366))         # at most three years
        days = (b - a).days + 1
        p_to = a - timedelta(days=1)
        p_from = p_to - timedelta(days=days - 1)
        running = b >= today
        label = (f"{a:%d %b}" + ("" if a.year == b.year else f" {a.year}") + f" – {b:%d %b %Y}"
                 + (" to date" if running else ""))
        return a, b, p_from, p_to, running, label

    if mode == "month":
        anchor = start or today
        c_from = anchor.replace(day=1)
        running = (c_from.year, c_from.month) == (today.year, today.month)
        month_end = c_from.replace(day=monthrange(c_from.year, c_from.month)[1])
        c_to = today if running else month_end
        p_from = (c_from - timedelta(days=1)).replace(day=1)
        p_end = p_from.replace(day=monthrange(p_from.year, p_from.month)[1])
        p_to = min(p_from + timedelta(days=(c_to - c_from).days), p_end) if running else p_end
        label = f"{c_from:%B %Y}" + (" to date" if running else "")
        return c_from, c_to, p_from, p_to, running, label

    if mode == "quarter":
        anchor = start or today
        q = (anchor.month - 1) // 3
        c_from = date(anchor.year, q * 3 + 1, 1)
        q_end = date(anchor.year, q * 3 + 3, monthrange(anchor.year, q * 3 + 3)[1])
        running = c_from <= today <= q_end
        c_to = today if running else q_end
        p_from = date(c_from.year - (1 if q == 0 else 0), ((c_from.month - 4) % 12) + 1, 1)
        p_end = date(p_from.year, p_from.month + 2, monthrange(p_from.year, p_from.month + 2)[1])
        p_to = min(p_from + timedelta(days=(c_to - c_from).days), p_end) if running else p_end
        label = f"Q{q + 1} {anchor.year}" + (" to date" if running else "")
        return c_from, c_to, p_from, p_to, running, label

    if mode == "ytd":
        anchor = start or today
        c_from = date(anchor.year, 1, 1)
        running = anchor.year == today.year
        c_to = today if running else date(anchor.year, 12, 31)
        label = f"{anchor.year}" + (" to date" if running else "")
        return c_from, c_to, date(anchor.year - 1, 1, 1), _last_year(c_to), running, label

    # --- week ---
    monday_this_week = today - timedelta(days=today.weekday())
    s = start or monday_this_week
    s = s - timedelta(days=s.weekday())
    if s >= monday_this_week:
        s, e, running = monday_this_week, today, True
    else:
        e, running = s + timedelta(days=6), False
    span = (e - s).days
    p_from = s - timedelta(days=7)
    p_to = p_from + timedelta(days=span)
    label = f"Week of {s:%d %b %Y}" + (" to date" if running else "")
    return s, e, p_from, p_to, running, label


def _cost(row):
    c = float(row.get("Cost_Amount_Actual") or 0)
    if config.INCLUDE_EXPECTED_COST:
        c += float(row.get("Cost_Amount_Expected") or 0)
    return c


def _qty(row):
    return float(row.get("Item_Ledger_Entry_Quantity") or 0)


def _by_item(rows):
    items = defaultdict(lambda: {"cost": 0.0, "qty": 0.0, "lines": 0, "desc": "", "locations": set()})
    for r in rows:
        it = items[r.get("Item_No") or "?"]
        it["cost"] += _cost(r)
        it["qty"] += _qty(r)
        it["lines"] += 1
        if r.get("Item_Description"):
            it["desc"] = r["Item_Description"].strip()
        if r.get("Location_Code"):
            it["locations"].add(r["Location_Code"])
    for it in items.values():
        it["avg"] = it["cost"] / it["qty"] if it["qty"] > 0 else None
    return items


def _sum(rows, key_fn):
    out = defaultdict(float)
    for r in rows:
        out[key_fn(r)] += _cost(r)
    return out


def uninvoiced_in_period(rows):
    """Goods received in the period whose invoice has not arrived yet.

    Business Central books a receipt at expected cost and reverses that expected cost
    when the invoice is posted, so the net expected cost of a period is exactly what
    that period's receipts still owe an invoice. This is period-scoped, unlike the open
    purchase orders, which are always a position as at today.
    """
    expected = 0.0
    still_open = set()
    for r in rows:
        exp = float(r.get("Cost_Amount_Expected") or 0)
        expected += exp
        if r.get("Document_Type") == "Purchase Receipt" and exp:
            still_open.add(r.get("Document_No"))
        if r.get("Document_Type") == "Purchase Invoice" and exp:
            still_open.discard(r.get("Document_No"))
    return {"value": expected, "open_receipts": len(still_open)}


def build(cur_rows, prior_rows, location=None):
    all_locations = sorted({r.get("Location_Code") for r in cur_rows + prior_rows if r.get("Location_Code")})
    if location:
        cur_rows = [r for r in cur_rows if r.get("Location_Code") == location]
        prior_rows = [r for r in prior_rows if r.get("Location_Code") == location]

    uninvoiced = uninvoiced_in_period(cur_rows)
    cur, prev = _by_item(cur_rows), _by_item(prior_rows)
    total_cur = sum(i["cost"] for i in cur.values())
    total_prev = sum(i["cost"] for i in prev.values())

    # Price movers: items bought in both periods whose average unit cost moved
    movers = []
    for no, c in cur.items():
        p = prev.get(no)
        if not p or not c["avg"] or not p["avg"] or p["avg"] <= 0:
            continue
        if c["cost"] < config.MIN_SPEND_FOR_MOVER:
            continue
        pct = (c["avg"] - p["avg"]) / p["avg"] * 100
        if abs(pct) < config.MIN_PCT_FOR_MOVER:
            continue
        movers.append({
            "item_no": no, "desc": c["desc"] or p["desc"], "locations": sorted(c["locations"]),
            "prev_avg": p["avg"], "cur_avg": c["avg"], "pct": pct,
            "impact": (c["avg"] - p["avg"]) * c["qty"], "cur_qty": c["qty"], "cur_cost": c["cost"],
        })
    movers.sort(key=lambda m: abs(m["impact"]), reverse=True)

    increases = [m for m in movers if m["pct"] > 0]
    decreases = [m for m in movers if m["pct"] < 0]

    # Same item, different price across locations (current period)
    loc_prices = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))
    for r in cur_rows:
        acc = loc_prices[r.get("Item_No")][r.get("Location_Code") or "?"]
        acc[0] += _cost(r)
        acc[1] += _qty(r)
    spreads = []
    for no, locs in loc_prices.items():
        avgs = {l: v[0] / v[1] for l, v in locs.items() if v[1] > 0 and v[0] > 0}
        if len(avgs) < 2 or cur[no]["cost"] < config.MIN_SPEND_FOR_MOVER:
            continue
        lo_loc, lo = min(avgs.items(), key=lambda kv: kv[1])
        hi_loc, hi = max(avgs.items(), key=lambda kv: kv[1])
        spread = (hi - lo) / lo * 100
        if spread >= config.LOCATION_SPREAD_PCT:
            qty_hi = locs[hi_loc][1]
            spreads.append({
                "item_no": no, "desc": cur[no]["desc"], "low_loc": lo_loc, "low": lo,
                "high_loc": hi_loc, "high": hi, "spread": spread, "overpay": (hi - lo) * qty_hi,
            })
    spreads.sort(key=lambda s: s["overpay"], reverse=True)

    # Exceptions
    exceptions = []
    for m in movers:
        if abs(m["pct"]) >= config.PRICE_ALERT_PCT:
            exceptions.append({"type": "price", "title": f"{m['desc']} price {'up' if m['pct'] > 0 else 'down'} {m['pct']:+.1f}%",
                               "detail": f"Average cost {m['prev_avg']:,.2f} → {m['cur_avg']:,.2f} {currency.code()}. Check the invoice unit and the agreed price.",
                               "amount": m["impact"]})
    for no, p in prev.items():
        if p["cost"] >= config.EXCEPTION_SPEND and (no not in cur or cur[no]["cost"] == 0):
            exceptions.append({"type": "missing", "title": f"{p['desc'] or no} not bought this period",
                               "detail": f"{p['cost']:,.0f} {currency.code()} in the comparison period, nothing yet now. Check stock cover or a missed order.",
                               "amount": -p["cost"]})
    for no, c in cur.items():
        if c["cost"] >= config.EXCEPTION_SPEND and no not in prev:
            exceptions.append({"type": "new", "title": f"{c['desc'] or no}: large purchase, not bought last period",
                               "detail": f"{c['cost']:,.0f} {currency.code()} across {', '.join(sorted(c['locations']))}. Confirm it is planned.",
                               "amount": c["cost"]})
        if c["cost"] < 0:
            exceptions.append({"type": "credit", "title": f"{c['desc'] or no}: net negative cost",
                               "detail": f"{c['cost']:,.0f} {currency.code()}. Returns or credit memos outweigh purchases.",
                               "amount": c["cost"]})
        if c["qty"] > 0 and c["cost"] == 0:
            exceptions.append({"type": "nocost", "title": f"{c['desc'] or no}: received with zero cost",
                               "detail": f"{c['qty']:,.2f} units posted without a cost. Price may be missing on the order.",
                               "amount": 0})
    exceptions.sort(key=lambda e: abs(e["amount"]), reverse=True)

    # Daily and location breakdowns
    daily_cur = _sum(cur_rows, lambda r: r.get("Posting_Date"))
    daily_prev = _sum(prior_rows, lambda r: r.get("Posting_Date"))
    by_loc_cur = _sum(cur_rows, lambda r: r.get("Location_Code") or "?")
    by_loc_prev = _sum(prior_rows, lambda r: r.get("Location_Code") or "?")
    locations = sorted(
        [{"code": k, "cur": v, "prev": by_loc_prev.get(k, 0.0)} for k, v in by_loc_cur.items()],
        key=lambda x: x["cur"], reverse=True)

    top_spend = sorted(
        [{"item_no": no, "desc": c["desc"], "cost": c["cost"], "qty": c["qty"], "avg": c["avg"],
          "prev_cost": prev[no]["cost"] if no in prev else 0.0} for no, c in cur.items()],
        key=lambda x: x["cost"], reverse=True)[:15]

    latest = sorted(cur_rows, key=lambda r: r.get("Entry_No") or 0, reverse=True)[:15]
    latest = [{"entry": r.get("Entry_No"), "date": r.get("Posting_Date"), "item": (r.get("Item_Description") or r.get("Item_No") or "").strip(),
               "location": r.get("Location_Code"), "doc": r.get("Document_No"),
               "type": (r.get("Document_Type") or "").strip(),
               "qty": _qty(r), "cost": _cost(r)} for r in latest]

    return {
        "kpis": {
            "total_cur": total_cur, "total_prev": total_prev,
            "total_pct": ((total_cur - total_prev) / total_prev * 100) if total_prev else None,
            "increases": len(increases), "decreases": len(decreases),
            "increase_impact": sum(m["impact"] for m in increases),
            "saving": -sum(m["impact"] for m in decreases),
            "net_price_impact": sum(m["impact"] for m in movers),
            "lines": len(cur_rows), "items": len(cur),
            "documents": len({r.get("Document_No") for r in cur_rows}),
            "exceptions": len(exceptions),
            "uninvoiced_in_period": uninvoiced["value"],
            "uninvoiced_receipts": uninvoiced["open_receipts"],
        },
        "movers": movers[:40], "spreads": spreads[:20], "exceptions": exceptions[:25],
        "daily_cur": dict(sorted(daily_cur.items())), "daily_prev": dict(sorted(daily_prev.items())),
        "locations": locations, "top_spend": top_spend, "latest": latest,
        "all_locations": all_locations,
    }


def by_location(cur_rows, prior_rows):
    """Purchase figures of every location for the location squares: spend against the comparison,
    documents, items, price moves of the items it buys, goods received but not invoiced, credits,
    its three largest items and the spend by day. Built from the rows already read."""
    cur_by, prev_by = defaultdict(list), defaultdict(list)
    for r in cur_rows:
        cur_by[r.get("Location_Code") or "?"].append(r)
    for r in prior_rows:
        prev_by[r.get("Location_Code") or "?"].append(r)
    out = {}
    for loc in set(cur_by) | set(prev_by):
        c, p = cur_by.get(loc, []), prev_by.get(loc, [])
        ci, pi = _by_item(c), _by_item(p)
        tc, tp = sum(i["cost"] for i in ci.values()), sum(i["cost"] for i in pi.values())
        up, down = [], []
        for no, x in ci.items():
            y = pi.get(no)
            if not y or not x["avg"] or not y["avg"] or y["avg"] <= 0 or x["cost"] < config.MIN_SPEND_FOR_MOVER:
                continue
            pct = (x["avg"] - y["avg"]) / y["avg"] * 100
            if abs(pct) < config.MIN_PCT_FOR_MOVER:
                continue
            impact = (x["avg"] - y["avg"]) * x["qty"]
            (up if pct > 0 else down).append((impact, x["desc"] or no, pct))
        un = uninvoiced_in_period(c)
        top = sorted(ci.items(), key=lambda kv: kv[1]["cost"], reverse=True)[:3]
        out[loc] = {
            "cur": tc, "prev": tp, "pct": ((tc - tp) / tp * 100) if tp else None,
            "lines": len(c), "documents": len({r.get("Document_No") for r in c}), "items": len(ci),
            "price_up": [len(up), sum(m[0] for m in up)], "price_down": [len(down), -sum(m[0] for m in down)],
            "top_up": [[m[1], m[2], m[0]] for m in sorted(up, key=lambda m: -m[0])[:3]],
            "uninvoiced": un["value"], "open_receipts": un["open_receipts"],
            "credits": sum(_cost(r) for r in c if _cost(r) < 0),
            "top_items": [[v["desc"] or no, v["cost"]] for no, v in top],
            "daily": dict(sorted(_sum(c, lambda r: r.get("Posting_Date")).items())),
        }
    return out

