"""Price simulator: the elasticity measured from RHG's own price changes (Geri, 25 Nov 2026, approved).

An event is a change of an item's menu price at a location in Simphony (getMenuItemPrices with its history: the
lowest price level, the price most revenue centres carry, a change of 3% or more). Around it, the 28 days before and
the 28 days after (four whole weeks each, the same weekdays): the units sold at a price before and after
(menu item totals VAT included, before discounts: units at a price = sales ÷ the menu price, at most the units
rung, so units inside combo meals at no price do not count), against the units of the location's items whose price
did not change on that date (the control: the season, the weather, a busy month move them all).

    elasticity = ln((units after ÷ units before) ÷ (control after ÷ control before)) ÷ ln(price after ÷ price before)

An event counts with at least 20 units at a price in each window. An item's elasticity is the units-weighted mean of
its events, kept between −3 and −0.1; an item without one takes the median of its family group when three items of
the family have one; else the class default of the Parameters. The days not yet held are read from Simphony in the
background, four at a time (read only); nothing is written to Simphony.
"""
import math
import statistics
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import menueng
import simphony

WINDOW = 28
MIN_STEP = 0.03
MIN_UNITS = 20
MAX_DATES = 4            # the most recent price-change dates of a location
LOOKBACK = 540           # days
LO, HI = -3.0, -0.1
_job = {"running": False, "done": 0, "total": 0, "at": 0.0}
_lock = threading.Lock()


def _iso(d):
    return d.isoformat()


def events(ref, items, today=None):
    """[(date, num, p0, p1)] of one location: the menu-price changes of the items asked, the most recent dates."""
    today = today or date.today()
    rows = simphony.menu_item_prices(ref, history=True)
    by = {}
    for r in rows:
        p = r.get("price")
        if r["num"] not in items or p is None or float(p) <= 1 or not r.get("from"):
            continue
        by.setdefault(r["num"], []).append(r)
    out = []
    lo, hi = _iso(today - timedelta(days=LOOKBACK)), _iso(today - timedelta(days=WINDOW + 1))
    for num, rs in by.items():
        lv = min((r.get("level") for r in rs if r.get("level") is not None), default=None)
        rs = [r for r in rs if r.get("level") == lv] or rs
        at = {}
        for r in rs:
            at.setdefault(r["from"], []).append(round(float(r["price"]), 2))
        seq = [(d, statistics.mode(ps)) for d, ps in sorted(at.items())]
        for (d0, p0), (d1, p1) in zip(seq, seq[1:]):
            if p0 > 0 and abs(p1 / p0 - 1) >= MIN_STEP and lo <= d1 <= hi:
                out.append((d1, num, p0, p1))
    dates = sorted({e[0] for e in out}, reverse=True)[:MAX_DATES]
    return [e for e in out if e[0] in dates]


def _windows(d):
    x = date.fromisoformat(d)
    return (_iso(x - timedelta(days=WINDOW)), _iso(x - timedelta(days=1))), (d, _iso(x + timedelta(days=WINDOW - 1)))


def _held(ref, a, b):
    with menueng._db_lock:
        return {r[0] for r in menueng._db().execute("SELECT day FROM dayv WHERE ref=? AND day>=? AND day<=?",
                                                    (ref, a, b)).fetchall()}


def _days(a, b):
    x, y, out = date.fromisoformat(a), date.fromisoformat(b), []
    while x <= y:
        out.append(_iso(x))
        x += timedelta(days=1)
    return out


def _units(ref, a, b):
    with menueng._db_lock:
        return {mi: (sls or 0.0, cnt or 0.0) for mi, sls, cnt in menueng._db().execute(
            "SELECT mi, SUM(sls), SUM(cnt) FROM miv WHERE ref=? AND day>=? AND day<=? GROUP BY mi", (ref, a, b)).fetchall()}


def one(ref, d, evs):
    """{num: (elasticity, weight)} of the events of one location on one date, the days held."""
    (a0, b0), (a1, b1) = _windows(d)
    u0, u1 = _units(ref, a0, b0), _units(ref, a1, b1)
    changed = {num for _d, num, _p0, _p1 in evs}
    c0 = sum(c for mi, (_s, c) in u0.items() if mi not in changed)
    c1 = sum(c for mi, (_s, c) in u1.items() if mi not in changed)
    if c0 <= 0 or c1 <= 0:
        return {}
    out = {}
    for _d, num, p0, p1 in evs:
        s0, n0 = u0.get(num, (0.0, 0.0))
        s1, n1 = u1.get(num, (0.0, 0.0))
        q0, q1 = min(n0, s0 / p0), min(n1, s1 / p1)
        if q0 < MIN_UNITS or q1 < MIN_UNITS:
            continue
        e = math.log((q1 / q0) / (c1 / c0)) / math.log(p1 / p0)
        out[num] = (e, q0 + q1)
    return out


def read_behind(jobs):
    """Read the missing days from Simphony in the background (read only), one job at a time."""
    with _lock:
        if _job["running"] or not jobs:
            return
        _job.update(running=True, done=0, total=len(jobs), at=time.time())

    def go():
        try:
            latest = {}
            for ref in {r for r, _d in jobs}:
                try:
                    latest[ref] = simphony.latest_business_date(ref)
                except Exception:                                    # noqa: BLE001
                    latest[ref] = None

            def get(rd):
                ref, d = rd
                try:
                    menueng.read_day(ref, d, latest.get(ref), vat=True)
                except Exception:                                    # noqa: BLE001
                    pass
                with _lock:
                    _job["done"] += 1
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(get, jobs))
        except Exception:                                            # noqa: BLE001
            traceback.print_exc()
        finally:
            with _lock:
                _job["running"] = False
    threading.Thread(target=go, daemon=True, name="pricesim-elasticity").start()


def measure(refs, items, family=None, read=True, today=None):
    """{"items": {num: {value, source, events, units, last}}, "status": {...}} for the items (menu item numbers) of the
    locations: measured where the days are held, the missing days read behind."""
    family = family or {}
    found, missing, errors = {}, [], []
    for ref in refs:
        try:
            evs = events(ref, set(items), today)
        except Exception as exc:                                     # noqa: BLE001
            errors.append(f"{ref}: price history: {str(exc)[:120]}")
            continue
        by_date = {}
        for e in evs:
            by_date.setdefault(e[0], []).append(e)
        for d, es in by_date.items():
            (a0, _b0), (_a1, b1) = _windows(d)
            held = _held(ref, a0, b1)
            need = [x for x in _days(a0, b1) if x not in held]
            if need:
                missing += [(ref, x) for x in need]
                continue
            for num, (e, w) in one(ref, d, es).items():
                found.setdefault(num, []).append((e, w, d))
    out = {}
    for num, lst in found.items():
        w = sum(x[1] for x in lst)
        v = sum(x[0] * x[1] for x in lst) / w
        out[num] = {"value": round(max(LO, min(HI, v)), 2), "source": "item", "events": len(lst), "units": round(w),
                    "last": max(x[2] for x in lst)}
    fam = {}
    for num, x in out.items():
        if family.get(num):
            fam.setdefault(family[num], []).append(x["value"])
    for num in items:
        if num in out or not family.get(num):
            continue
        vals = fam.get(family[num]) or []
        if len(vals) >= 3:
            out[num] = {"value": round(statistics.median(vals), 2), "source": "family", "events": len(vals), "units": 0,
                        "last": ""}
    missing = sorted(set(missing))
    if read and missing:
        read_behind(missing)
    with _lock:
        job = dict(_job)
    return {"items": out, "status": {"measured": sum(1 for x in out.values() if x["source"] == "item"),
                                     "family": sum(1 for x in out.values() if x["source"] == "family"),
                                     "missing_days": len(missing), "running": job["running"], "done": job["done"],
                                     "total": job["total"], "errors": errors[:6]}}
