"""Unicorn, the business analyst of pulse: the facts Purchasing gives it every morning.

Per company, from what the tower already builds:
  * spend of the month to date against the comparison period, price effects (rises, savings, net), goods
    received and not invoiced;
  * the tower's own brief of the period (status, developments, risks, the actions it proposes);
  * the largest price movers, the critical and warning exceptions nobody has taken on yet;
  * invoices posted yesterday without a purchase order (count, value, rate).
Read-only; asked by identity through /internal/unicorn/facts (service token), read in the background.
"""
import threading
import time
import traceback
from datetime import date, timedelta

_jobs, _lock = {}, threading.Lock()
KEEP = 1800


def _r(v):
    return round(v) if isinstance(v, (int, float)) else v


def company_facts(company, day, dashboard_fn, nopo_fn, currency_of):
    out = {"company": company, "currency": currency_of(company)}
    data = dashboard_fn(company, day.replace(day=1).isoformat(), "", "month")
    k = data.get("kpis") or {}
    meta = data.get("meta") or {}
    out["month_to_date"] = {
        "period": meta.get("period_label"), "dates": meta.get("current"), "comparison_dates": meta.get("prior"), "spend": _r(k.get("total_cur")),
        "comparison_spend": _r(k.get("total_prev")),
        "vs_comparison_pct": round(k["total_pct"], 1) if k.get("total_pct") is not None else None,
        "price_rises_cost": _r(k.get("increase_impact")), "price_savings": _r(k.get("saving")),
        "net_price_effect": _r(k.get("net_price_impact")), "received_not_invoiced": _r(k.get("uninvoiced_in_period")),
        "documents": k.get("documents")}
    b = data.get("brief") or {}
    if b:
        out["tower_brief"] = {"status": b.get("status"), "status_note": b.get("status_note"),
                              "developments": b.get("developments"), "risks": b.get("risks"),
                              "actions": b.get("actions"), "exceptions": b.get("counts")}
    out["largest_price_movers"] = [
        {"item": m.get("desc") or m.get("item_no"), "locations": (m.get("locations") or [])[:4],
         "previous_average_cost": _r(m.get("prev_avg")), "current_average_cost": _r(m.get("cur_avg")),
         "change_pct": round(m["pct"], 1) if m.get("pct") is not None else None,
         "impact": _r(m.get("impact"))}
        for m in (data.get("movers") or [])[:6]]
    u = data.get("untracked") or {}
    out["exceptions_without_owner"] = {"counts": u.get("counts"),
                                       "critical": [{"title": e.get("title"), "location": e.get("location"),
                                                     "impact": _r(e.get("impact")), "rule": e.get("rule")}
                                                    for e in (u.get("critical") or [])[:6]]}
    n = nopo_fn(company, day, day)
    if n.get("available"):
        nk = n.get("kpis") or {}
        out["invoices_without_po_yesterday"] = {"invoices": nk.get("invoices"), "without_order": nk.get("no_order"),
                                                "partly_without_order": nk.get("partial"),
                                                "value_without_order": _r(nk.get("value_breach")),
                                                "rate_pct": round(nk.get("rate") or 0, 1)}
    return out


def build(companies, day, dashboard_fn, nopo_fn, currency_of, progress):
    out = {"day": day.isoformat(), "generated_at": time.strftime("%Y-%m-%d %H:%M"), "companies": [], "errors": []}
    for co in companies:
        progress(f"Purchasing: {co}")
        try:
            out["companies"].append(company_facts(co, day, dashboard_fn, nopo_fn, currency_of))
        except Exception as exc:                                    # noqa: BLE001
            traceback.print_exc()
            out["errors"].append(f"Purchasing {co}: {str(exc)[:200]}")
    return out


def request(companies, dashboard_fn, nopo_fn, currency_of, day=None, refresh=False):
    day = day or (date.today() - timedelta(days=1))
    companies = [c for c in (companies or []) if c][:12]
    key = (day.isoformat(), tuple(companies))
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
            r = build(companies, day, dashboard_fn, nopo_fn, currency_of, progress)
            with _lock:
                job.update(state="done", finished=time.time(), result=r)
        except Exception as exc:                                    # noqa: BLE001
            traceback.print_exc()
            with _lock:
                job.update(state="failed", finished=time.time(), error=str(exc)[:300])
    threading.Thread(target=run, daemon=True, name="unicorn-facts").start()
    return {"pending": True, "stage": "Starting", "elapsed": 0}
