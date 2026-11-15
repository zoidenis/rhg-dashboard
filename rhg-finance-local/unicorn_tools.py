"""Unicorn's read-only tools in the Finance tower (Layer 2 → Layer 3 of the Unicorn 2.0 architecture).

Identity asks POST /internal/unicorn/tool {"name", "args"} with the service token; the tool reads what the tower
already reads for its screens (Simphony, Business Central, Crunchtime) and answers JSON. Nothing is written
anywhere. A read that takes longer than WAIT seconds goes on in the background: the answer is {"pending": true,
"stage"} and the same call asked again returns the result once it is there (kept KEEP seconds).

Every tool returns figures with their source and period; a figure that could not be read is left out and named
under "errors", never given as zero.
"""
import hashlib
import json
import threading
import time
import traceback
from datetime import date, timedelta

import brand_report
import kpilib
import locintel
import opsexec
import crunchtime
import pl_memo
import unicorn_facts

WAIT = 20
KEEP = 900
MAX_DAYS = 92
_jobs, _lock = {}, threading.Lock()


def _d(v, what):
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{what}: a date YYYY-MM-DD.") from exc


def _range(args, max_days=MAX_DAYS):
    a = _d(args.get("date_from"), "date_from")
    b = _d(args.get("date_to") or args.get("date_from"), "date_to")
    if b < a:
        a, b = b, a
    if b >= date.today():
        b = date.today() - timedelta(days=1)
    if (b - a).days + 1 > max_days:
        raise ValueError(f"At most {max_days} days in one call.")
    return a, b


def _days(a, b):
    return [a + timedelta(days=i) for i in range((b - a).days + 1)]


def _pct(a, b):
    return round((a / b - 1) * 100, 1) if a is not None and b else None


def _registry(only=None):
    reg = [r for r in locintel.registry() if r.get("ref")]
    if only:
        want = {str(x).strip().upper() for x in only if str(x).strip()}
        reg = [r for r in reg if {str(r.get(k) or "").upper() for k in ("ref", "name", "bc", "dept")} & want]
    return reg


# ---------------------------------------------------------------- the tools
def t_list_restaurants(ctx, args):
    out = []
    for r in _registry():
        out.append({"ref": r["ref"], "name": r.get("name") or r["ref"], "brand": r.get("brand") or "",
                    "company": r.get("company") or "", "bc_location": r.get("bc") or "", "department": r.get("dept") or "",
                    "seasonal": bool(r.get("seasonal")), "currency": ctx["currency_of"](r["company"]) if r.get("company") else ""})
    return {"restaurants": out, "source": "Settings → Location intelligence (the restaurants of pulse)"}


def _sales_block(figs, days):
    t = {"net": 0.0, "ex": 0.0, "checks": 0, "guests": 0, "gross": 0.0, "discounts": 0.0, "voids": 0.0, "days": 0}
    for d in days:
        f = figs.get(d.isoformat())
        if not f:
            continue
        t["days"] += 1
        t["net"] += f["net"]
        t["ex"] += f["net"] - (f.get("tax") or 0)
        t["checks"] += f["checks"]
        t["guests"] += f["guests"]
        t["gross"] += f.get("gross") or 0
        t["discounts"] += f.get("discounts") or 0
        t["voids"] += f.get("voids") or 0
    if not t["days"]:
        return None
    return {"net_sales_ex_vat": round(t["ex"]), "net_sales_with_vat": round(t["net"]), "checks": t["checks"],
            "guests": t["guests"], "average_check_ex_vat": round(t["ex"] / t["checks"]) if t["checks"] else None,
            "discounts_pct_of_gross": round(t["discounts"] / t["gross"] * 100, 1) if t["gross"] else None,
            "voids_pct_of_gross": round(t["voids"] / t["gross"] * 100, 1) if t["gross"] else None,
            "days_with_sales": t["days"]}


def t_sales(ctx, args, progress):
    a, b = _range(args)
    days = _days(a, b)
    cmp = args.get("compare") or "last_year"
    if cmp == "prior":
        n = len(days)
        cdays = [d - timedelta(days=n) for d in days]
    elif cmp == "last_year":
        cdays = [d - timedelta(days=364) for d in days]
    else:
        cdays = []
    reg = _registry(args.get("restaurants"))
    progress(f"Simphony: {len(reg)} restaurants, {len(days) + len(cdays)} days")
    figs = brand_report._simphony_days([r["ref"] for r in reg], set(days) | set(cdays), ctx["latest_fn"], ctx["parallel_fn"])
    out = []
    for r in reg:
        f = figs.get(r["ref"]) or {}
        cur = _sales_block(f, days)
        if not cur:
            continue
        row = {"ref": r["ref"], "name": r.get("name") or r["ref"], "company": r.get("company") or "",
               "currency": ctx["currency_of"](r["company"]) if r.get("company") else "", **cur}
        if cdays:
            c = _sales_block(f, cdays)
            row["comparison"] = c
            row["vs_comparison_pct"] = _pct(cur["net_sales_ex_vat"], c["net_sales_ex_vat"]) if c else None
        out.append(row)
    missing = [r.get("name") or r["ref"] for r in reg if r["ref"] not in {x["ref"] for x in out}]
    return {"period": {"from": a.isoformat(), "to": b.isoformat()},
            "comparison": ({"kind": cmp, "from": cdays[0].isoformat(), "to": cdays[-1].isoformat()} if cdays else None),
            "restaurants": out, "not_read_or_closed": missing,
            "source": "Simphony Reporting and Analytics, net sales excluding VAT; last year = the same weekdays 52 weeks back"}


def t_sales_by_day(ctx, args, progress):
    a, b = _range(args, 62)
    reg = _registry([args.get("restaurant")]) if args.get("restaurant") else []
    if not reg:
        raise ValueError("restaurant: a ref or name from list_restaurants.")
    r = reg[0]
    days = _days(a, b)
    ly = [d - timedelta(days=364) for d in days]
    progress(f"Simphony: {r.get('name')} day by day")
    f = brand_report._simphony_days([r["ref"]], set(days) | set(ly), ctx["latest_fn"], ctx["parallel_fn"]).get(r["ref"]) or {}
    rows = []
    for d, l in zip(days, ly):
        x, y = f.get(d.isoformat()), f.get(l.isoformat())
        ex = (lambda z: round(z["net"] - (z.get("tax") or 0)) if z else None)  # noqa: E731
        rows.append({"day": d.isoformat(), "weekday": d.strftime("%a"), "net_sales_ex_vat": ex(x),
                     "checks": x["checks"] if x else None, "guests": x["guests"] if x else None,
                     "discounts": round(x["discounts"]) if x else None, "voids": round(x["voids"]) if x else None,
                     "last_year_day": l.isoformat(), "last_year_net_sales_ex_vat": ex(y)})
    return {"restaurant": {"ref": r["ref"], "name": r.get("name"), "company": r.get("company")},
            "days": rows, "source": "Simphony Reporting and Analytics (a day not read or closed has no figures)"}


def t_pl_month(ctx, args, progress):
    co = str(args.get("company") or "").strip().upper()
    if not co:
        raise ValueError("company: e.g. SALT (list_restaurants gives the companies).")
    month = str(args.get("month") or pl_memo.report_month())[:7]
    progress(f"Business Central: the P&L of {co}")
    R = pl_memo.build_company(ctx["client"], co, month, ctx["currency_of"])
    short = unicorn_facts._pl_short
    return {"company": co, "currency": R["currency"], "month": R["month"], "asked": R["asked"],
            "company_total": {k: short(R["pl"].get(k)) for k in ("cur", "bud", "ly", "ytd", "ytd_bud", "ytd_ly")},
            "departments": [{"code": d["code"], "name": d["name"], "brand": d["brand"], "group": d["group"],
                             "actual": short(d["cur"]), "budget": short(d["bud"]), "last_year": short(d["ly"])}
                            for d in R["depts"]],
            "largest_variances": (R.get("variances") or [])[:12],
            "kpi_mapping": unicorn_facts._kpi_status(co),
            "source": "Business Central chart of accounts by department and month, G/L budget; lines of the KPI library"}


def t_payables_receivables(ctx, args, progress):
    co = str(args.get("company") or "").strip().upper()
    if not co:
        raise ValueError("company: e.g. SALT.")
    progress(f"Business Central: Kartela of {co}")
    r = unicorn_facts.apar(co, ctx["digest_fn"])
    r["source"] = "Business Central vendor and customer ledger entries (Kartela)"
    return r


def t_food_safety(ctx, args, progress):
    if not crunchtime.info().get("configured"):
        return {"available": False, "error": "The Crunchtime connection is not set (Operations & Food Safety → Connection)."}
    a, b = _range(args, 120)
    r = opsexec.held_request(("ox", a, b), lambda st: opsexec.build(a, b, st))
    deadline = time.time() + 240
    while (r.get("pending") or r.get("reading")) and time.time() < deadline:
        progress("Crunchtime: " + (r.get("stage") or "reading"))
        time.sleep(5)
        r = opsexec.held_request(("ox", a, b), lambda st: opsexec.build(a, b, st))
    if r.get("pending"):
        return {"available": False, "error": "Crunchtime is still being read; ask again in a few minutes."}
    if r.get("available") is False:
        return r
    rows = []
    for L in r.get("locations") or []:
        rows.append({"restaurant": L["name"], "crunchtime_location": L["location"], "mapping": L["how"],
                     "tasks_due": L["tasks"]["due"], "tasks_done": L["tasks"]["done"], "tasks_overdue": L["tasks"]["overdue"],
                     "checklist_completion_pct": L["completion"], "temperature_readings": L["temp_n"],
                     "temperatures_out_of_range": L["temp_out"], "temperatures_in_range_pct": L["temp_pct_in"],
                     "sensors": len(L["sensors"]), "sensors_out_of_range_now": L["sensors_out"],
                     "sensors_offline": L["sensors_off"], "oil_measurements": L["oil_n"], "oil_to_change": L["oil_over"],
                     "products_checked_on_arrival": L["trace_n"], "products_refused": L["trace_unfit"],
                     "spoil_entries": len(L["spoil"]), "yield_pct": L["yield_pct"],
                     "worst_checklists": [{"checklist": c[0], "due": c[1], "done": c[2], "overdue": c[4]} for c in L["checklists"][:5]]})
    return {"period": r.get("period"), "limits_c": r.get("limits"), "restaurants": rows,
            "suppliers_on_arrival": [{"supplier": s[0], "checked": s[1], "refused": s[2], "avg_c": s[3], "max_c": s[4]}
                                     for s in (r.get("suppliers") or [])[:15]],
            "errors": r.get("errors") or [],
            "source": "Crunchtime Ops Execution (tasks, food safety forms, Monnit sensors now), read only"}


def t_kpi_definitions(ctx, args):
    return {"kpis": [{"key": k["key"], "label": k.get("label"), "formula": k.get("formula"),
                      "unit": k.get("unit")} for k in kpilib.KPIS],
            "source": "The KPI library of pulse (kpilib): one formula for every screen"}


# name → (function, slow?) ; slow tools run as background jobs
TOOLS = {
    "list_restaurants": (t_list_restaurants, False),
    "kpi_definitions": (t_kpi_definitions, False),
    "sales": (t_sales, True),
    "sales_by_day": (t_sales_by_day, True),
    "pl_month": (t_pl_month, True),
    "payables_receivables": (t_payables_receivables, True),
    "food_safety": (t_food_safety, True),
}


def call(ctx, name, args):
    """The answer of one tool, or {"pending": true, "stage"} while a slow read goes on."""
    if name not in TOOLS:
        return {"available": False, "error": f"Finance has no tool {name}."}
    fn, slow = TOOLS[name]
    args = args if isinstance(args, dict) else {}
    try:
        if not slow:
            return fn(ctx, args)
    except ValueError as exc:
        return {"available": False, "error": str(exc)}
    except Exception as exc:                                        # noqa: BLE001
        return {"available": False, "error": f"Could not read: {str(exc)[:300]}"}
    key = hashlib.sha1((name + json.dumps(args, sort_keys=True, default=str)).encode()).hexdigest()
    now = time.time()
    with _lock:
        for k in [k for k, j in _jobs.items() if j["state"] != "running" and now - j.get("finished", now) > KEEP]:
            _jobs.pop(k, None)
        job = _jobs.get(key)
        if not job or (job["state"] == "failed" and now - job["finished"] > 60):
            job = _jobs[key] = {"state": "running", "stage": "starting", "started": now}

            def go(j=job):
                try:
                    v = fn(ctx, args, lambda st: j.update(stage=st))
                    j.update(state="done", result=v, finished=time.time())
                except ValueError as exc:
                    j.update(state="done", result={"available": False, "error": str(exc)}, finished=time.time())
                except Exception as exc:                            # noqa: BLE001
                    traceback.print_exc()
                    j.update(state="failed", error=str(exc)[:300], finished=time.time())
            threading.Thread(target=go, daemon=True, name="unicorn-tool").start()
    t0 = time.time()
    while job["state"] == "running" and time.time() - t0 < WAIT:
        time.sleep(0.5)
    if job["state"] == "done":
        return job["result"]
    if job["state"] == "failed":
        return {"available": False, "error": job.get("error") or "The read failed."}
    return {"pending": True, "stage": job.get("stage") or "reading"}
