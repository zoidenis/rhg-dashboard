"""Unicorn, the business analyst of pulse: the facts Finance gives it every morning.

Unicorn lives with identity (the chat and the writing); this module only gathers the figures it talks about,
from what the tower already reads, as compact numbers:
  * sales (Simphony, ex VAT): yesterday per restaurant against the same weekday last week, the same weekday
    last year and the budget of the day; the month to date against budget and the same dates last year;
    discounts, voids and error corrects of the day as a share of gross;
  * P&L of the latest closed month per company (sales, food, labour, other costs, EBITDA against budget and
    last year, year to date, forecast) with the largest account variances and the memo sentences;
  * cash in the bank and cash accounts; payables and receivables (open, overdue, beyond the critical limit,
    the largest parties, the number of exceptions and how many are new);
  * stock: dead and slow stock and goods received but never used (when Movement & dead stock is held).
Each part fails on its own (the error is listed); read-only. Asked by identity through /internal/unicorn/facts
(service token); the read runs in the background and identity asks again.
"""
import threading
import time
import traceback
from datetime import date, timedelta

import brand_report
import locintel
import movement
import pl_memo

_jobs, _lock = {}, threading.Lock()
KEEP = 1800


def _cur(r, currency_of):
    c = (r.get("currency") or currency_of(r.get("company")) or "ALL").upper()
    return "ALL" if c in ("L", "LEK") else c


def _pct(a, b):
    return round((a - b) / abs(b) * 100, 1) if b else None


def _share(x, gross):
    return round(x / gross * 100, 1) if gross else None


def sales(day, client, latest_fn, parallel_fn, currency_of, errors, progress):
    reg = [r for r in locintel.registry() if r.get("ref")]
    if not reg:
        return None
    m1 = day.replace(day=1)
    try:
        ly_day, ly_m1 = day.replace(year=day.year - 1), m1.replace(year=m1.year - 1)
    except ValueError:                                              # 29 February
        ly_day, ly_m1 = day - timedelta(days=365), m1.replace(year=m1.year - 1)
    mtd = [m1 + timedelta(days=i) for i in range((day - m1).days + 1)]
    mtd_ly = [ly_m1 + timedelta(days=i) for i in range((ly_day - ly_m1).days + 1)]
    lw, lyw = day - timedelta(days=7), day - timedelta(days=364)
    progress("Simphony: yesterday, last week, last year and the month to date")
    figs = brand_report._simphony_days([r["ref"] for r in reg], set(mtd) | set(mtd_ly) | {lw, lyw}, latest_fn, parallel_fn)
    # budgets: the P&L cube of each company of the registry (its budget lines by department)
    cubes = {}
    for co in sorted({(r.get("company") or "").upper() for r in reg if r.get("company")}):
        progress(f"Business Central: the budget of {co}")
        try:
            cubes[co] = brand_report._cube(client, co, day.year, currency_of)
        except Exception as exc:                                    # noqa: BLE001
            errors.append(f"Budget of {co}: {str(exc)[:160]}")
    out, totals = [], {}
    for r in reg:
        f = figs.get(r["ref"]) or {}
        y = f.get(day.isoformat())
        if not y and not any(f.get(d.isoformat()) for d in mtd):
            continue                                                # closed or not read
        cur = _cur(r, currency_of)
        ex = lambda d: (d["net"] - (d.get("tax") or 0)) if d else None   # noqa: E731
        s_y, s_lw, s_ly = ex(y), ex(f.get(lw.isoformat())), ex(f.get(lyw.isoformat()))
        s_mtd = brand_report._sales(f, mtd)
        s_mly = brand_report._sales(f, mtd_ly)
        cube = cubes.get((r.get("company") or "").upper())
        dept = r.get("dept") or r.get("bc")
        b_day = b_mtd = None
        if cube and dept:
            cube_of = lambda yr, c=cube: c if yr == day.year else None   # noqa: E731
            b1 = brand_report._budget_days(cube_of, dept, day, day)
            b2 = brand_report._budget_days(cube_of, dept, m1, day)
            b_day = round(b1["sales"]) if b1 and b1.get("sales") else None
            b_mtd = round(b2["sales"]) if b2 and b2.get("sales") else None
        L = {"name": r.get("name") or r["ref"], "brand": r.get("brand") or "", "company": r.get("company") or "",
             "currency": cur, "seasonal": bool(r.get("seasonal")),
             "yesterday": round(s_y) if s_y is not None else None,
             "same_weekday_last_week": round(s_lw) if s_lw is not None else None,
             "same_weekday_last_year": round(s_ly) if s_ly is not None else None,
             "budget_of_the_day": b_day,
             "vs_last_week_pct": _pct(s_y, s_lw) if s_y is not None and s_lw else None,
             "vs_last_year_pct": _pct(s_y, s_ly) if s_y is not None and s_ly else None,
             "vs_budget_pct": _pct(s_y, b_day) if s_y is not None and b_day else None,
             "month_to_date": round(s_mtd["ex"]), "month_to_date_last_year": round(s_mly["ex"]) if s_mly["days"] else None,
             "month_to_date_budget": b_mtd,
             "mtd_vs_last_year_pct": _pct(s_mtd["ex"], s_mly["ex"]) if s_mly["days"] and s_mly["ex"] else None,
             "mtd_vs_budget_pct": _pct(s_mtd["ex"], b_mtd) if b_mtd else None}
        if y:
            L.update({"checks": y["checks"], "guests": y["guests"],
                      "average_check": round(y["net"] / y["checks"]) if y["checks"] else None,
                      "discounts_pct_of_gross": _share(y["discounts"], y["gross"]),
                      "voids_pct_of_gross": _share(y["voids"], y["gross"]),
                      "error_corrects_pct_of_gross": _share(y["error_corrects"], y["gross"]),
                      "open_checks": y["open_checks"]})
        out.append(L)
        t = totals.setdefault(cur, {"yesterday": 0, "same_weekday_last_week": 0, "same_weekday_last_year": 0,
                                    "budget_of_the_day": 0, "month_to_date": 0, "month_to_date_last_year": 0,
                                    "month_to_date_budget": 0, "restaurants": 0})
        t["restaurants"] += 1
        for k in ("yesterday", "same_weekday_last_week", "same_weekday_last_year", "budget_of_the_day",
                  "month_to_date", "month_to_date_last_year", "month_to_date_budget"):
            t[k] += L.get(k) or 0
    for t in totals.values():
        t["vs_last_week_pct"] = _pct(t["yesterday"], t["same_weekday_last_week"])
        t["vs_last_year_pct"] = _pct(t["yesterday"], t["same_weekday_last_year"])
        t["vs_budget_pct"] = _pct(t["yesterday"], t["budget_of_the_day"])
        t["mtd_vs_last_year_pct"] = _pct(t["month_to_date"], t["month_to_date_last_year"])
        t["mtd_vs_budget_pct"] = _pct(t["month_to_date"], t["month_to_date_budget"])
    out.sort(key=lambda L: (L["currency"], -(L["yesterday"] or 0)))
    return {"day": day.isoformat(), "weekday": day.strftime("%A"), "last_week_day": lw.isoformat(),
            "last_year_day": lyw.isoformat(), "month_from": m1.isoformat(),
            "note": "Simphony net sales excluding VAT; last year = the same weekday 52 weeks back; budget = the "
                    "month's G/L budget of the restaurant's department spread over its days.",
            "totals_by_currency": totals, "restaurants": out}


def _pl_short(P):
    if not P:
        return None
    keys = ("sales", "food", "labor", "opex", "ebitda", "food_pct", "labor_pct", "ebitda_pct", "prime_pct")
    return {k: (round(P[k], 1) if k.endswith("_pct") else round(P[k])) for k in keys if P.get(k) is not None}


def pl(company, day, client, currency_of):
    R = pl_memo.build_company(client, company, pl_memo.report_month(day + timedelta(days=1)), currency_of)
    P = R["pl"]
    return {"company": company, "currency": R["currency"], "month": R["month"],
            "actual": _pl_short(P["cur"]), "budget": _pl_short(P["bud"]),
            "last_year": _pl_short(P["ly"]), "year_to_date": _pl_short(P["ytd"]),
            "year_to_date_budget": _pl_short(P["ytd_bud"]), "year_to_date_last_year": _pl_short(P["ytd_ly"]),
            "full_year_budget": _pl_short(P.get("fy_bud")), "forecast": _pl_short(P.get("forecast")),
            "departments": [{"name": d["name"], "brand": d["brand"], "actual": _pl_short(d["cur"]),
                             "budget": _pl_short(d["bud"]), "last_year": _pl_short(d["ly"])}
                            for d in R["depts"] if d["group"] == "restaurant"][:20],
            "largest_variances": R["variances"], "memo": R["memo"]}


def apar(company, digest_fn):
    d = digest_fn(company)

    def side(s):
        return {"open": round(s["total"]), "overdue": round(s["overdue"]), "beyond_critical": round(s["beyond"]),
                "critical_days": s["limit"],
                "largest": [{"name": p.get("name") or p.get("party"), "open": round(p.get("remaining") or p.get("total") or 0),
                             "overdue": round(p.get("overdue") or 0)} for p in (s.get("top") or [])[:5]]}
    return {"company": company, "currency": d["currency"], "receivables": side(d["ar"]), "payables": side(d["ap"]),
            "supplier_debit_balances": round(d["ap"].get("debit") or 0),
            "exceptions": d["exceptions"], "new_exceptions": d["new"],
            "top_exceptions": [{"area": g["area"], "rule": g["rule"], "party": g["party"], "severity": g["severity"],
                                "amount": round(g["impact"]), "oldest_days": g["oldest"], "new": g["new"]}
                               for g in d["groups"][:8]]}


def cash(company, dashboard_fn):
    data = dashboard_fn(company)
    c = (data or {}).get("cash") or {}
    return {"company": company, "currency": (data.get("meta") or {}).get("currency"),
            "cash_and_bank": round(c.get("total") or 0), "movement_of_the_month": round(c.get("movement") or 0),
            "accounts": [{"name": a["name"], "balance": round(a["balance"])} for a in (c.get("accounts") or [])[:8]]}


def stock(company, client, day):
    r = movement.request(client, company, day.isoformat(), 90)
    if r.get("pending") or not r.get("totals"):
        return None                                                 # not held now: left out, not waited for
    t = r["totals"]
    return {"company": company, "as_of": r.get("as_of") or day.isoformat(), "items": t.get("items"),
            "estimated_value": t.get("value"), "classes": t.get("classes"),
            "received_never_used": {"lines": (t.get("received_not_used") or [0, 0])[0],
                                    "value": (t.get("received_not_used") or [0, 0])[1]}}


def build(client, latest_fn, parallel_fn, currency_of, dashboard_fn, digest_fn, day, progress):
    errors = []
    out = {"day": day.isoformat(), "generated_at": time.strftime("%Y-%m-%d %H:%M"), "errors": errors}
    try:
        out["sales"] = sales(day, client, latest_fn, parallel_fn, currency_of, errors, progress)
    except Exception as exc:                                        # noqa: BLE001
        traceback.print_exc()
        errors.append(f"Sales: {str(exc)[:200]}")
    companies = sorted({(r.get("company") or "").upper() for r in locintel.registry() if r.get("company")})
    out["companies"] = companies
    for key, fn in (("pl", lambda co: pl(co, day, client, currency_of)), ("cash", lambda co: cash(co, dashboard_fn)),
                    ("payables_receivables", lambda co: apar(co, digest_fn)), ("stock", lambda co: stock(co, client, day))):
        out[key] = []
        for co in companies:
            progress(f"{key.replace('_', ' ').capitalize()}: {co}")
            try:
                v = fn(co)
                if v:
                    out[key].append(v)
            except Exception as exc:                                # noqa: BLE001
                traceback.print_exc()
                errors.append(f"{key} {co}: {str(exc)[:200]}")
    return out


def request(client, latest_fn, parallel_fn, currency_of, dashboard_fn, digest_fn, day=None, refresh=False):
    day = day or (date.today() - timedelta(days=1))
    key = day.isoformat()
    now = time.time()
    with _lock:
        job = _jobs.get(key)
        if job and job["state"] == "running":
            return {"pending": True, "stage": job.get("stage", ""), "elapsed": int(now - job["started"])}
        if job and job["state"] == "done" and not refresh and now - job["finished"] < KEEP:
            return job["result"]
        if job and job["state"] == "failed" and not refresh and now - job["finished"] < 120:
            return {"available": False, "error": job["error"]}
        job = _jobs[key] = {"state": "running", "started": now, "stage": "Starting"}
        for k in [k for k in _jobs if k != key and _jobs[k]["state"] != "running"]:
            _jobs.pop(k, None)

    def progress(text):
        with _lock:
            job["stage"] = text

    def run():
        try:
            r = build(client, latest_fn, parallel_fn, currency_of, dashboard_fn, digest_fn, day, progress)
            with _lock:
                job.update(state="done", finished=time.time(), result=r)
        except Exception as exc:                                    # noqa: BLE001
            traceback.print_exc()
            with _lock:
                job.update(state="failed", finished=time.time(), error=str(exc)[:300])
    threading.Thread(target=run, daemon=True, name="unicorn-facts").start()
    return {"pending": True, "stage": "Starting", "elapsed": 0}
