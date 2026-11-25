"""Price simulator → a plan for a future period (Geri, 25 Nov 2026, approved): 2027, H1 or H2, a quarter.

The plan starts from the same period last year as Simphony sold it, so the season is right: Q1 2027 from Q1 2026.
When that period has not ended yet (2027 against 2026 on 25 Nov 2026), the days sold so far are completed with the
season of the year before: each restaurant's checks and sales of the whole period two years back ÷ those of the same
days (Simphony daily totals); without them, by the number of days.

Per restaurant and channel (Simphony order types, the same channels as the simulator): net sales, checks and guests
of the base (getOrderTypeQuarterHourTotals, kept with the daily totals), the completion factor, and once the plan
period has started, what was actually sold month by month. The days not held are read behind, four at a time.
Read only; nothing is written to Simphony.
"""
import calendar
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import chanprof
import mkkpi
import simphony

PARTS = {"H1": (1, 6), "H2": (7, 12), "Q1": (1, 3), "Q2": (4, 6), "Q3": (7, 9), "Q4": (10, 12)}
_job = {"running": False, "done": 0, "total": 0, "at": 0.0}
_lock = threading.Lock()


def options(today=None):
    """The plan periods offered: the next two years, whole, by half and by quarter."""
    today = today or date.today()
    out = []
    for y in (today.year + 1, today.year + 2):
        out.append({"code": str(y), "label": f"Whole {y}"})
        out += [{"code": f"{y}-{p}", "label": f"{p} {y}"} for p in ("H1", "H2", "Q1", "Q2", "Q3", "Q4")]
    return out


def span(code):
    """(first day, last day, label) of a plan code: 2027, 2027-H1, 2027-Q3."""
    code = str(code or "").strip().upper()
    y, _, part = code.partition("-")
    if not y.isdigit() or len(y) != 4 or (part and part not in PARTS):
        raise ValueError("Choose a plan period: a year, a half or a quarter.")
    y = int(y)
    m0, m1 = PARTS[part] if part else (1, 12)
    return date(y, m0, 1), date(y, m1, calendar.monthrange(y, m1)[1]), (f"{part} {y}" if part else f"Whole {y}")


def shift(d, years):
    try:
        return d.replace(year=d.year + years)
    except ValueError:                                               # 29 February
        return d.replace(year=d.year + years, day=28)


def days(a, b):
    out = []
    while a <= b:
        out.append(a.isoformat())
        a += timedelta(days=1)
    return out


def base_window(start, end, latest):
    """(base from, base to, completion) for a plan period: the same dates a year earlier, cut at the last closed day;
    completion: (from, to, to of the part sold) two years back, to complete a cut base with that year's season."""
    b0, b1 = shift(start, -1), shift(end, -1)
    if b1 <= latest:
        return b0, b1, None
    if latest < b0:                                                  # not begun last year: two years back
        return shift(start, -2), shift(end, -2), None
    return b0, latest, (shift(start, -2), shift(end, -2), shift(latest, -1))


def _q(ref, d):
    with simphony._days_lock:
        hit = simphony._days().get(f"Q|{ref}|{d}")
    return hit["q"] if hit else None


def read_behind(jobs):
    """The order-type totals of the days not held, read in the background (read only)."""
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
                try:
                    simphony.order_type_hours(rd[0], rd[1], latest.get(rd[0]))
                except Exception:                                    # noqa: BLE001
                    pass
                with _lock:
                    _job["done"] += 1
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(get, jobs))
            simphony.flush_days()
        except Exception:                                            # noqa: BLE001
            traceback.print_exc()
        finally:
            with _lock:
                _job["running"] = False
    threading.Thread(target=go, daemon=True, name="pricesim-plan").start()


def by_channel(ref, ds_, missing):
    """{channel: {net, checks, guests}} of one restaurant over the days, the days not held added to missing."""
    try:
        ots = simphony.order_type_dims(ref)
    except Exception:                                                # noqa: BLE001
        ots = {}
    out = {}
    for d in ds_:
        q = _q(ref, d)
        if q is None:
            missing.append((ref, d))
            continue
        for k, (net, _disc, chk, gst) in q.items():
            ot = k.split("|")[1] if "|" in k else ""
            n = ots.get(int(ot)) if str(ot).lstrip("-").isdigit() and int(ot) else None
            ch = chanprof.channel_key(n or ("No order type" if not ot or ot == "0" else f"Order type {ot}"))
            x = out.setdefault(ch, {"net": 0.0, "checks": 0, "guests": 0})
            x["net"] += net
            x["checks"] += chk
            x["guests"] += gst
    return out


def _daily(ref, ds_, missing):
    net = chk = 0.0
    n = 0
    for d in ds_:
        h = simphony.held_day(ref, d)
        if h is None:
            missing.append((ref, d))
            continue
        f = simphony.day_figures(h.get("rvcs") or [])
        net += f["net"]
        chk += f["checks"]
        n += 1
    return net, chk, n


def completion(ref, comp, base_days_full, base_days_cut, missing_daily):
    """{net, checks, source}: how much bigger the whole base period is than the days sold so far."""
    by_days = len(base_days_full) / max(1, len(base_days_cut))
    if not comp:
        return {"net": 1.0, "checks": 1.0, "source": "complete"}
    c0, c1, cut = comp
    full, part = days(c0, c1), days(c0, cut)
    nf, cf, n1 = _daily(ref, full, missing_daily)
    npart, cpart, _n2 = _daily(ref, part, [])
    if n1 < len(full) * 0.9 or npart <= 0 or cpart <= 0:
        return {"net": round(by_days, 4), "checks": round(by_days, 4), "source": "days"}
    return {"net": round(nf / npart, 4), "checks": round(cf / cpart, 4), "source": f"season of {c0.year}"}


def window(code, refs, today=None):
    """{start, end, label, last (the last closed day), b0, b1 (the base), comp} of a plan code for the restaurants."""
    today = today or date.today()
    start, end, label = span(code)
    latest = []
    for ref in refs:
        try:
            x = simphony.latest_business_date(ref)
            if x:
                latest.append(date.fromisoformat(x))
        except Exception:                                            # noqa: BLE001
            pass
    last = max(latest) if latest else today - timedelta(days=1)
    b0, b1, comp = base_window(start, end, last)
    return {"start": start, "end": end, "label": label, "last": last, "b0": b0, "b1": b1, "comp": comp}


def build(code, refs, names, today=None, read=True, w=None):
    """The plan's base by restaurant and channel, its completion and, once begun, what was sold month by month."""
    w = w or window(code, refs, today)
    start, end, label, last, b0, b1, comp = (w[k] for k in ("start", "end", "label", "last", "b0", "b1", "comp"))
    full = days(shift(start, -1) if comp else b0, shift(end, -1) if comp else b1)
    cut = days(b0, b1)
    missing_q, missing_d, locs = [], [], []
    for ref in refs:
        ch = by_channel(ref, cut, missing_q)
        locs.append({"ref": ref, "name": names.get(ref, ref), "channels": ch,
                     "factor": completion(ref, comp, full, cut, missing_d)})
    # what was sold so far in the plan period, by month
    actual = []
    if start <= last:
        m = start
        while m <= min(end, last):
            m_end = min(date(m.year, m.month, calendar.monthrange(m.year, m.month)[1]), last)
            row = {"month": m.strftime("%Y-%m"), "to": m_end.isoformat(), "locs": {}}
            for ref in refs:
                row["locs"][ref] = by_channel(ref, days(m, m_end), missing_q)
            actual.append(row)
            m = date(m.year + (m.month == 12), m.month % 12 + 1, 1)
    # the share of each month of the plan in the base, to spread the plan by month
    months = []
    m = start
    while m <= end:
        months.append(m.strftime("%Y-%m"))
        m = date(m.year + (m.month == 12), m.month % 12 + 1, 1)
    missing_q = sorted(set(missing_q))
    if read and missing_q:
        read_behind(missing_q)
    if read and missing_d:
        mkkpi.read_behind(sorted(set(missing_d)))
    with _lock:
        job = dict(_job)
    return {"code": code.upper(), "label": label, "from": start.isoformat(), "to": end.isoformat(),
            "days": (end - start).days + 1, "months": months,
            "base": {"from": b0.isoformat(), "to": b1.isoformat(), "days": len(cut), "cut": bool(comp),
                     "season": [comp[0].isoformat(), comp[1].isoformat()] if comp else None},
            "locs": locs, "actual": actual, "started": start <= last,
            "reading": {"running": job["running"], "done": job["done"], "total": job["total"],
                        "missing": len(missing_q)}}
