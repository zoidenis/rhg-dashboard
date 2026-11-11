"""Fixed assets → the register: every asset of the company with its cost, accumulated depreciation and net book
value at the end of the period chosen, and its depreciation (the period, the year, the last month).

Built from the FA ledger summary that Location intelligence keeps (faloc.py: every entry ever posted, summed per
asset, depreciation book, posting type and month), so the whole history is there without reading the ledger again.
The depreciation book used is the one with the most entries (named on the screen). Read-only.

The depreciation checks, on the assets in use at the end of the period:
  * not depreciated in the month: net book value left, depreciated before, nothing posted in the period's month
    (a month that has ended);
  * never depreciated: acquired before the period's month, net book value left, no depreciation ever;
  * depreciated beyond cost: accumulated depreciation larger than the cost;
  * fully depreciated, still on the books (for information).
"""
from datetime import date

import faloc
import locintel

_upto, _between = faloc._upto, faloc._between


def _book(S):
    books = S.get("books") or {}
    return max(books, key=lambda b: books[b]) if books else ""


def _months(a, b):
    """The months a..b (YYYY-MM), both included."""
    y, m = int(a[:4]), int(a[5:7])
    out = []
    while f"{y}-{m:02d}" <= b and len(out) < 600:
        out.append(f"{y}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def _names(company, settings):
    reg = [r for r in locintel.registry(settings) if (r.get("company") or "").upper() == (company or "").upper()]
    out = {}
    for r in reg:
        for k in (r.get("bc"), r.get("dept"), r.get("ref")):
            if k:
                out.setdefault(k.strip().upper(), r.get("name") or k)
    return out


def _state(b, m_from, m_to, y_from):
    """One asset at the end of m_to, in one book: its figures and status."""
    cost = _upto(b.get("a"), m_to) + _upto(b.get("ax"), m_to)
    dep = _upto(b.get("d"), m_to) + _upto(b.get("dx"), m_to)
    wd = _upto(b.get("w"), m_to) + _upto(b.get("wx"), m_to)
    acquired = _upto(b.get("a"), m_to)
    disposed_in = sorted(k for k in list((b.get("ax") or {}).keys()) + list((b.get("dx") or {}).keys()) if k <= m_to)
    a_months = sorted(k for k, v in (b.get("a") or {}).items() if v and k <= m_to)
    d_months = sorted(k for k, v in (b.get("d") or {}).items() if v and k <= m_to)
    return {"cost": cost, "acc": -(dep + wd), "nbv": cost + dep + wd, "acquired": acquired,
            "disposed": bool(disposed_in) and abs(cost) < 1, "disposed_month": disposed_in[-1] if disposed_in else "",
            "first": a_months[0] if a_months else "", "last_dep": d_months[-1] if d_months else "",
            "dep_period": -_between(b.get("d"), m_from, m_to), "dep_ytd": -_between(b.get("d"), y_from, m_to),
            "dep_month": -(b.get("d") or {}).get(m_to, 0.0),
            "add_period": _between(b.get("a"), m_from, m_to), "add_ytd": _between(b.get("a"), y_from, m_to),
            "disp_ytd": -(_between(b.get("ax"), y_from, m_to) + _between(b.get("dx"), y_from, m_to)
                          + _between(b.get("wx"), y_from, m_to)),
            "proc_ytd": -_between(b.get("p"), y_from, m_to)}


def register(S, company, period, currency, settings, today=None):
    c_from, c_to = period[0], period[1]
    m_from, m_to = f"{c_from:%Y-%m}", f"{c_to:%Y-%m}"
    y_from = f"{c_to.year}-01"
    today = today or date.today()
    closed = c_to.replace(day=1) < today.replace(day=1)          # the period's month has ended
    book = _book(S)
    names = _names(company, settings)
    months = _months(y_from, m_to)
    rows, classes, locs = [], {}, {}
    T = {"assets": 0, "cost": 0.0, "acc": 0.0, "nbv": 0.0, "dep_period": 0.0, "dep_ytd": 0.0, "dep_month": 0.0,
         "add_period": 0.0, "add_ytd": 0.0, "disp_ytd": 0.0, "proc_ytd": 0.0, "disposed_ytd": 0,
         "fully": 0, "fully_cost": 0.0}
    m_add, m_dep, m_nbv = {m: 0.0 for m in months}, {m: 0.0 for m in months}, {m: 0.0 for m in months}
    checks = {"missed": [], "never": [], "beyond": [], "fully": []}
    for fa, A in (S.get("assets") or {}).items():
        b = (A.get("b") or {}).get(book)
        if not b:
            continue
        x = _state(b, m_from, m_to, y_from)
        if not x["acquired"] and abs(x["nbv"]) < 1:
            continue                                               # bought after the period, or nothing on it
        for m in months:                                           # the year month by month, disposals apart
            m_add[m] += (b.get("a") or {}).get(m, 0.0)
            m_dep[m] -= (b.get("d") or {}).get(m, 0.0)
            if not (any(k <= m for k in (b.get("ax") or {})) and abs(_upto(b.get("a"), m) + _upto(b.get("ax"), m)) < 1):
                m_nbv[m] += (_upto(b.get("a"), m) + _upto(b.get("ax"), m) + _upto(b.get("d"), m) + _upto(b.get("dx"), m)
                             + _upto(b.get("w"), m) + _upto(b.get("wx"), m))
        for k in ("dep_period", "dep_ytd", "dep_month", "add_period", "add_ytd", "disp_ytd", "proc_ytd"):
            T[k] += x[k]
        code = A.get("loc") or ""
        status = "disposed" if x["disposed"] else ("fully" if x["cost"] > 0 and x["nbv"] <= max(1.0, x["cost"] * 0.001)
                                                     else "in use")
        if x["disposed"]:
            if x["disposed_month"] < y_from:
                continue                                           # disposed in an earlier year: off the register
            T["disposed_ytd"] += 1
        else:
            T["assets"] += 1
            T["cost"] += x["cost"]
            T["acc"] += x["acc"]
            T["nbv"] += x["nbv"]
            c = classes.setdefault(A.get("class") or "—", {"assets": 0, "cost": 0.0, "acc": 0.0, "nbv": 0.0, "dep_ytd": 0.0})
            L = locs.setdefault(code, {"assets": 0, "cost": 0.0, "acc": 0.0, "nbv": 0.0, "dep_ytd": 0.0})
            for o in (c, L):
                o["assets"] += 1
                o["cost"] += x["cost"]
                o["acc"] += x["acc"]
                o["nbv"] += x["nbv"]
                o["dep_ytd"] += x["dep_ytd"]
            if status == "fully":
                T["fully"] += 1
                T["fully_cost"] += x["cost"]
                checks["fully"].append(fa)
            if x["nbv"] < -1:
                checks["beyond"].append(fa)
            elif status == "in use" and x["nbv"] > 1:
                if not x["last_dep"] and x["first"] and x["first"] < m_to:
                    checks["never"].append(fa)
                elif closed and x["last_dep"] and not x["dep_month"]:
                    checks["missed"].append(fa)
        rows.append([fa, A.get("desc") or "", A.get("class") or "", code, names.get(code.upper(), ""), x["first"],
                     round(x["cost"]), round(x["acc"]), round(x["nbv"]), round(x["dep_period"]), round(x["dep_ytd"]),
                     round(x["dep_month"]), x["last_dep"], status,
                     round(x["acc"] / x["cost"] * 100, 1) if x["cost"] > 0 else None, round(x["add_ytd"])])
    rows.sort(key=lambda r: -r[8])
    rnd = lambda d: {k: (round(v) if isinstance(v, float) else v) for k, v in d.items()}
    return {
        "available": True, "company": company, "currency": currency, "book": book, "closed": closed,
        "period": {"from": c_from.isoformat(), "to": c_to.isoformat(), "months": [m_from, m_to], "label": period[7]},
        "totals": rnd(T),
        "columns": ["asset", "description", "class", "location", "location_name", "acquired", "cost", "acc_dep", "nbv",
                    "dep_period", "dep_ytd", "dep_month", "last_dep", "status", "dep_pct", "add_ytd"],
        "rows": rows,
        "classes": sorted(([k, v["assets"], round(v["cost"]), round(v["acc"]), round(v["nbv"]), round(v["dep_ytd"])]
                           for k, v in classes.items()), key=lambda r: -r[2]),
        "locations": sorted(([k, names.get(k.upper(), ""), v["assets"], round(v["cost"]), round(v["acc"]), round(v["nbv"]),
                              round(v["dep_ytd"])] for k, v in locs.items()), key=lambda r: -r[5]),
        "months": months, "m_add": [round(m_add[m]) for m in months], "m_dep": [round(m_dep[m]) for m in months],
        "m_nbv": [round(m_nbv[m]) for m in months],
        "checks": checks, "entries": S.get("entries", 0), "read_at": S.get("at"),
        "definitions": {**faloc.DEFINITIONS, "checks": (
            "Not depreciated in the month: an asset in use with net book value left, depreciated before, and nothing "
            "posted in the period's month once that month has ended. Never depreciated: acquired before the period's "
            "month, net book value left, no depreciation ever posted. Depreciated beyond cost: accumulated depreciation "
            "larger than the cost. Fully depreciated: net book value nil, still on the books."),
            "register": "Every asset with an acquisition in the book, at the end of the period's month; assets disposed "
                        "of in an earlier year are left out, those disposed of this year are listed as disposed."},
    }


def asset(S, fa, period):
    """One asset month by month in the book: acquisition, depreciation, write-downs, disposal and proceeds, and the
    running cost, accumulated depreciation and net book value; with the pace of its depreciation."""
    A = (S.get("assets") or {}).get(str(fa))
    book = _book(S)
    if not A or not (A.get("b") or {}).get(book):
        return {"available": False, "error": "This asset is not in the FA ledger summary."}
    b = A["b"][book]
    c_from, c_to = period[0], period[1]
    m_to = f"{c_to:%Y-%m}"
    keys = sorted({k for cell in b.values() for k in cell})
    if not keys:
        return {"available": False, "error": "No entry on this asset."}
    months = _months(keys[0], max(keys[-1], m_to))
    hist, cost = [], 0.0
    acc = 0.0
    for m in months:
        a = (b.get("a") or {}).get(m, 0.0) + (b.get("ax") or {}).get(m, 0.0)
        d = (b.get("d") or {}).get(m, 0.0) + (b.get("dx") or {}).get(m, 0.0)
        w = (b.get("w") or {}).get(m, 0.0) + (b.get("wx") or {}).get(m, 0.0)
        p = (b.get("p") or {}).get(m, 0.0)
        cost += a
        acc -= d + w
        if a or d or w or p:
            hist.append([m, round(a), round(-d), round(-w), round(-p), round(cost), round(acc), round(cost - acc)])
    x = _state(b, f"{c_from:%Y-%m}", m_to, f"{c_to.year}-01")
    last3 = [-(b.get("d") or {}).get(m, 0.0) for m in _months(_shift(m_to, -2), m_to)]
    pace = sum(last3) / 3 if any(last3) else 0.0
    left = round(x["nbv"] / pace) if pace > 0 and x["nbv"] > 0 else None
    return {"available": True, "asset": fa, "description": A.get("desc") or "", "class": A.get("class") or "",
            "location": A.get("loc") or "", "book": book, "first": x["first"], "last_dep": x["last_dep"],
            "cost": round(x["cost"]), "acc_dep": round(x["acc"]), "nbv": round(x["nbv"]),
            "dep_ytd": round(x["dep_ytd"]), "dep_month": round(x["dep_month"]), "pace": round(pace),
            "months_left": left, "disposed": x["disposed"], "history": hist,
            "columns": ["month", "acquisition", "depreciation", "write_down", "proceeds", "cost", "acc_dep", "nbv"]}


def month(S, m):
    """The assets with an acquisition or depreciation in one month (YYYY-MM)."""
    book = _book(S)
    out = []
    for fa, A in (S.get("assets") or {}).items():
        b = (A.get("b") or {}).get(book) or {}
        add = (b.get("a") or {}).get(m, 0.0)
        dep = -(b.get("d") or {}).get(m, 0.0)
        disp = -((b.get("ax") or {}).get(m, 0.0) + (b.get("dx") or {}).get(m, 0.0) + (b.get("wx") or {}).get(m, 0.0))
        if add or dep or disp:
            out.append([fa, A.get("desc") or "", A.get("class") or "", A.get("loc") or "", round(add), round(dep), round(disp)])
    out.sort(key=lambda r: -(abs(r[4]) + abs(r[5]) + abs(r[6])))
    return {"month": m, "book": book, "columns": ["asset", "description", "class", "location", "acquisition",
                                                  "depreciation", "disposal"], "rows": out[:2000], "count": len(out)}


def _shift(m, n):
    y, mm = int(m[:4]), int(m[5:7]) + n
    while mm < 1:
        y, mm = y - 1, mm + 12
    while mm > 12:
        y, mm = y + 1, mm - 12
    return f"{y}-{mm:02d}"
