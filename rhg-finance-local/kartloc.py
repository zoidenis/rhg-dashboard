"""Kartela → Location intelligence: payables and receivables of every location of a company.

Per location (the restaurant registry of Settings → Location intelligence, this company's rows), from the
supplier and customer ledgers of Business Central, entries placed on the restaurant's department through
their dimension set:
  * payables: what is open today with the suppliers, by age of the due date (not due, 1-30, 31-60, 61-90,
    over 90 days), the suppliers it is owed to, the largest overdue entries, advances paid (debit balances);
    supplier invoices and payments posted in the month chosen;
  * receivables: the same for the customers (overdue, by customer, the largest overdue entries, credit
    balances), invoiced and received in the month chosen.
Entries on no department, or on one that is not a restaurant, are listed apart (payments often carry no
department). Read-only; the read runs in the background and the screen asks again.
"""
import threading
import time
import traceback
from datetime import date, timedelta

import finloc
import locintel
import store

_jobs, _lock = {}, threading.Lock()
KEEP = 600
_sets = {}
AGES = ("not_due", "d30", "d60", "d90", "over90")
AGE_LABEL = {"not_due": "Not due", "d30": "1-30 days", "d60": "31-60 days", "d90": "61-90 days", "over90": "Over 90 days"}


def _dim_sets(client, company):
    hit = _sets.get(company)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    value = client.dimension_sets(company)
    _sets[company] = (time.time(), value)
    return value


def _age(due, today):
    """The aging bucket of an entry by its due date (no due date = not due)."""
    if not due:
        return "not_due", 0
    try:
        days = (today - date.fromisoformat(due)).days
    except ValueError:
        return "not_due", 0
    if days <= 0:
        return "not_due", days
    return ("d30" if days <= 30 else "d60" if days <= 60 else "d90" if days <= 90 else "over90"), days


def _side():
    return {"open": 0.0, "overdue": 0.0, "overdue_60": 0.0, "advance": 0.0, "n": 0,
            "ages": {a: 0.0 for a in AGES}, "by": {}, "late": [], "invoiced": 0.0, "paid": 0.0, "docs": 0}


def build(client, company, period, currency, rights, progress):
    settings = store.get_settings()
    th = finloc.thresholds(settings)
    c_from, c_to = period[0], period[1]
    today = date.today()
    reg = [r for r in locintel.registry(settings) if (r.get("company") or "").upper() == (company or "").upper()]
    by_dept = {}
    for r in reg:
        d = r.get("dept") or r.get("bc")
        if d:
            by_dept.setdefault(d, r)
    progress("Business Central: dimension sets")
    sets = _dim_sets(client, company)
    out, errors = {}, []

    def loc(code):
        L = out.get(code)
        if L is None:
            r = by_dept.get(code)
            L = out[code] = {"name": (r or {}).get("name") or code or "Not on a location",
                             "group": "restaurant" if r else ("unassigned" if not code else "support"),
                             "brand": (r or {}).get("brand") or "", "ref": (r or {}).get("ref") or "",
                             "seasonal": bool((r or {}).get("seasonal")), "ap": None, "ar": None}
        return L

    def dept(e):
        return sets.get(int(e.get("Dimension_Set_ID") or 0), "")

    def take(side, e, amount, name, no):
        """amount = what is owed (to the supplier, or by the customer); below zero = an advance or credit."""
        due = str(e.get("Due_Date") or "")[:10]
        side["open"] += amount
        if amount < 0:
            side["advance"] += -amount
            return
        side["n"] += 1
        bucket, days = _age(due, today)
        side["ages"][bucket] += amount
        if bucket != "not_due":
            side["overdue"] += amount
            if days > 60:
                side["overdue_60"] += amount
            side["late"].append([str(e.get("Document_No") or ""), name, due, days, amount,
                                 str(e.get("Document_Type") or "")])
        b = side["by"].setdefault(name, {"no": no, "open": 0.0, "overdue": 0.0, "n": 0, "oldest": ""})
        b["open"] += amount
        b["n"] += 1
        if bucket != "not_due":
            b["overdue"] += amount
            if due and (not b["oldest"] or due < b["oldest"]):
                b["oldest"] = due

    if rights.get("ap"):
        progress("Business Central: open supplier entries")
        try:
            for e in client.open_vendor_entries(company):
                L = loc(dept(e))
                L["ap"] = L["ap"] or _side()
                name = (e.get("Vendor_Name") or e.get("Vendor_No") or "?").strip()
                take(L["ap"], e, -float(e.get("Remaining_Amt_LCY") or 0), name, e.get("Vendor_No") or "")
            progress("Business Central: supplier invoices and payments of the month")
            for e in client.vendor_entries(company, c_from.isoformat(), c_to.isoformat()):
                L = loc(dept(e))
                L["ap"] = L["ap"] or _side()
                amt, typ = float(e.get("Amount_LCY") or 0), e.get("Document_Type")
                if typ in ("Invoice", "Credit Memo"):
                    L["ap"]["invoiced"] += -amt
                    L["ap"]["docs"] += 1 if typ == "Invoice" else 0
                elif typ in ("Payment", "Refund"):
                    L["ap"]["paid"] += amt
        except Exception as exc:                                    # noqa: BLE001
            traceback.print_exc()
            errors.append(f"Payables: {str(exc)[:200]}")
    if rights.get("ar"):
        progress("Business Central: open customer entries")
        try:
            for e in client.open_customer_entries(company):
                L = loc(dept(e))
                L["ar"] = L["ar"] or _side()
                name = (e.get("Customer_Name") or e.get("Customer_No") or "?").strip()
                take(L["ar"], e, float(e.get("Remaining_Amt_LCY") or 0), name, e.get("Customer_No") or "")
            progress("Business Central: customer invoices and receipts of the month")
            for e in client.customer_entries(company, c_from.isoformat(), c_to.isoformat()):
                L = loc(dept(e))
                L["ar"] = L["ar"] or _side()
                amt, typ = float(e.get("Amount_LCY") or 0), e.get("Document_Type")
                if typ in ("Invoice", "Credit Memo"):
                    L["ar"]["invoiced"] += amt
                    L["ar"]["docs"] += 1 if typ == "Invoice" else 0
                elif typ in ("Payment", "Refund"):
                    L["ar"]["paid"] += -amt
        except Exception as exc:                                    # noqa: BLE001
            traceback.print_exc()
            errors.append(f"Receivables: {str(exc)[:200]}")

    # ---- rounding, the largest, alerts
    for code, L in out.items():
        al = []
        for k in ("ap", "ar"):
            S = L[k]
            if not S:
                continue
            by = S.pop("by")
            S["parties"] = len(by)
            S["top"] = [[n, v["no"], round(v["open"]), round(v["overdue"]), v["n"], v["oldest"]]
                        for n, v in sorted(by.items(), key=lambda kv: -kv[1]["open"])[:12] if round(v["open"])]
            S["late"] = [x[:4] + [round(x[4]), x[5]] for x in sorted(S["late"], key=lambda x: -x[4])[:12]]
            for f in ("open", "overdue", "overdue_60", "advance", "invoiced", "paid"):
                S[f] = round(S[f])
            S["ages"] = {a: round(v) for a, v in S["ages"].items()}
            S["owed"] = sum(S["ages"].values())
        ap, ar = L["ap"] or {}, L["ar"] or {}
        owed_ap = ap.get("open", 0) + ap.get("advance", 0)
        if owed_ap > 0 and ap.get("overdue_60", 0) > owed_ap * th["overdue_60"] / 100:
            al.append(["warn", f"{round(ap['overdue_60'] / owed_ap * 100)}% of payables overdue beyond 60 days"])
        if ap.get("ages", {}).get("over90", 0) > 0 and owed_ap and ap["ages"]["over90"] > owed_ap * 0.25:
            al.append(["crit", f"{round(ap['ages']['over90'] / owed_ap * 100)}% of payables over 90 days"])
        if ar.get("ages", {}).get("over90", 0) > 0:
            al.append(["warn", "Receivables over 90 days"])
        elif ar.get("overdue_60", 0) > 0:
            al.append(["info", "Receivables overdue beyond 60 days"])
        if ap.get("advance", 0) > 0 and owed_ap and ap["advance"] > owed_ap * 0.1:
            al.append(["info", "Advances paid to suppliers not yet settled"])
        if ar.get("advance", 0) > 0:
            al.append(["info", "Customers with a credit balance"])
        L["alerts"] = sorted(al, key=lambda x: {"crit": 0, "warn": 1, "info": 2}[x[0]])
    out = {k: v for k, v in out.items()
           if any(v[s] and (v[s]["open"] or v[s]["advance"] or v[s]["invoiced"] or v[s]["paid"]) for s in ("ap", "ar"))}
    return {"company": company, "currency": currency, "locations": out, "errors": errors,
            "period": {"from": c_from.isoformat(), "to": c_to.isoformat(), "label": period[7]},
            "as_of": today.isoformat(), "ages": AGE_LABEL, "rights": {k: bool(rights.get(k)) for k in ("ap", "ar")},
            "thresholds": th, "definitions": DEFINITIONS}


def request(client, company, period, currency, rights, refresh=False):
    key = (company or "", period[0].isoformat(), period[1].isoformat(), bool(rights.get("ap")), bool(rights.get("ar")))
    now = time.time()
    with _lock:
        job = _jobs.get(key)
        if job and job["state"] == "running":
            return {"pending": True, "stage": job.get("stage", ""), "elapsed": int(now - job["started"])}
        if job and job["state"] == "done" and not refresh and now - job["finished"] < KEEP:
            return job["result"]
        if job and job["state"] == "failed" and not refresh and now - job["finished"] < 60:
            return {"available": False, "error": job["error"]}
        job = _jobs[key] = {"state": "running", "started": now, "stage": "Starting"}

    def progress(text):
        with _lock:
            job["stage"] = text

    def run():
        try:
            r = build(client, company, period, currency, rights, progress)
            with _lock:
                job.update(state="done", finished=time.time(), result=r)
        except Exception as exc:                                   # noqa: BLE001
            traceback.print_exc()
            with _lock:
                job.update(state="failed", finished=time.time(), error=str(exc)[:300])
    threading.Thread(target=run, daemon=True, name="kartloc").start()
    return {"pending": True, "stage": "Starting", "elapsed": 0}


DEFINITIONS = {
    "ap": "Payables: supplier ledger entries open today, placed on the restaurant's department through their "
          "dimension set. Open = what is owed to the suppliers, less advances paid (entries with a debit balance). "
          "Aging by the due date: not due, 1-30, 31-60, 61-90 and over 90 days past it. Invoiced = supplier "
          "invoices less credit memos posted in the month chosen; paid = payments posted in the month on the "
          "department (payments often carry no department and then show under Not on a location).",
    "ar": "Receivables: customer ledger entries open today on the department, the same aging; credit balances "
          "(customers paid ahead or credit notes not applied) apart. Invoiced and received in the month chosen.",
    "alerts": "Alerts: payables overdue beyond 60 days over the share set as \"Overdue beyond 60 days\" in Settings "
              "→ Location intelligence; more than a quarter of payables over 90 days (critical); receivables over "
              "90 days, or beyond 60; advances to suppliers over 10% of payables; customers with a credit balance.",
}
