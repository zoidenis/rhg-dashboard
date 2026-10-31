"""Location intelligence → Today (Purchasing): what each location bought and received today, so far.

Per BC location code, from Business Central, posted or entered today:
  * receipts: purchase receipts posted today (value entries), with their suppliers and value;
  * invoices: purchase invoices posted today (headers), and those posted without a purchase order;
  * credit memos posted today;
  * orders: purchase orders dated today (open documents), their value, and those still to be released;
  * requisitions: requisition lines due today or overdue.
Business Central's figures refresh every few minutes; read-only. The read runs in the background and the
screen asks again.
"""
import threading
import time
import traceback
from datetime import date

import operations

_jobs, _lock = {}, threading.Lock()
KEEP = 240


def _f(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def build(client, company, progress):
    T = date.today()
    iso = T.isoformat()
    out, errors = {}, []
    names = {}
    try:
        for r in client.locations(company):
            c = r.get("code") or r.get("Code") or ""
            names[c] = r.get("name") or r.get("Name") or c
    except Exception as exc:                                        # noqa: BLE001
        errors.append(f"Locations: {str(exc)[:160]}")

    def loc(code):
        code = code or ""
        L = out.get(code)
        if L is None:
            L = out[code] = {"name": names.get(code) or code or "No location", "receipts": None, "invoices": None,
                             "credits": None, "orders": None, "reqs": None, "alerts": []}
        return L

    progress("Business Central: receipts and invoices posted today")
    try:
        docs = {}
        for e in client.purchase_entries(company, iso, iso):
            key = (e.get("Document_Type"), str(e.get("Document_No") or ""))
            d = docs.setdefault(key, {"value": 0.0, "lines": 0, "locs": set(), "items": set()})
            d["value"] += _f(e.get("Cost_Amount_Actual")) + (_f(e.get("Cost_Amount_Expected")) if key[0] == operations.RCPT else 0)
            d["lines"] += 1
            d["locs"].add(e.get("Location_Code") or "")
            if e.get("Item_No"):
                d["items"].add(e["Item_No"])
        for (typ, no), d in docs.items():
            field = "receipts" if typ == operations.RCPT else "credits" if typ == operations.CM else "invoices" if typ == operations.INV else None
            if not field or field == "invoices":                   # invoices are counted from their headers below
                continue
            for c in d["locs"] or {""}:
                L = loc(c)
                S = L[field] = L[field] or {"n": 0, "value": 0.0, "lines": 0, "docs": []}
                share = d["value"] / max(1, len(d["locs"]))
                S["n"] += 1
                S["value"] += share
                S["lines"] += d["lines"]
                S["docs"].append([no, "", round(share), len(d["items"])])
    except Exception as exc:                                        # noqa: BLE001
        traceback.print_exc()
        errors.append(f"Value entries: {str(exc)[:160]}")
    try:
        for h in client.posted_invoices(company, iso, iso, max_age=240):
            if h.get("Cancelled"):
                continue
            L = loc(h.get("Location_Code") or "")
            S = L["invoices"] = L["invoices"] or {"n": 0, "value": 0.0, "no_po": 0, "no_po_value": 0.0, "docs": []}
            amt = _f(h.get("Amount"))
            S["n"] += 1
            S["value"] += amt
            if not (h.get("Order_No") or "").strip():
                S["no_po"] += 1
                S["no_po_value"] += amt
            S["docs"].append([str(h.get("No") or ""), (h.get("Buy_from_Vendor_Name") or "").strip(), round(amt),
                              (h.get("Order_No") or "").strip(), (h.get("Vendor_Invoice_No") or "").strip()])
    except Exception as exc:                                        # noqa: BLE001
        traceback.print_exc()
        errors.append(f"Posted invoices: {str(exc)[:160]}")
    progress("Business Central: purchase orders of today")
    try:
        for r in client.purchase_order_list(company, max_age=240):
            if r.get("Document_Type") != "Order" or str(r.get("Document_Date") or "")[:10] != iso:
                continue
            L = loc(r.get("Location_Code") or "")
            S = L["orders"] = L["orders"] or {"n": 0, "value": 0.0, "open": 0, "docs": []}
            S["n"] += 1
            S["value"] += _f(r.get("Amount"))
            if (r.get("Status") or "") != "Released":
                S["open"] += 1
            S["docs"].append([str(r.get("No") or ""), (r.get("Buy_from_Vendor_Name") or "").strip(), round(_f(r.get("Amount"))),
                              r.get("Status") or "", rd if (rd := str(r.get("Requested_Receipt_Date") or "")[:10]) > "0001-01-01" else ""])
    except Exception as exc:                                        # noqa: BLE001
        traceback.print_exc()
        errors.append(f"Purchase orders: {str(exc)[:160]}")
    progress("Business Central: requisitions")
    try:
        lines, _ = client.requisition_lines(company)
        for r in lines or []:
            due = str(r.get("due_date") or r.get("order_date") or "")[:10]
            if not due or due > iso:
                continue
            L = loc(r.get("location") or "")
            S = L["reqs"] = L["reqs"] or {"today": 0, "late": 0, "value": 0.0, "lines": []}
            S["today" if due == iso else "late"] += 1
            S["value"] += _f(r.get("quantity")) * _f(r.get("unit_cost"))
            if len(S["lines"]) < 12:
                S["lines"].append([r.get("item") or "", r.get("description") or "", _f(r.get("quantity")), r.get("uom") or "",
                                   due, r.get("vendor") or ""])
    except Exception as exc:                                        # noqa: BLE001
        traceback.print_exc()
        errors.append(f"Requisitions: {str(exc)[:160]}")

    for code, L in out.items():
        for k in ("receipts", "invoices", "credits", "orders", "reqs"):
            S = L[k]
            if not S:
                continue
            for f in ("value", "no_po_value"):
                if f in S:
                    S[f] = round(S[f])
            if "docs" in S:
                S["docs"] = sorted(S["docs"], key=lambda x: -abs(x[2]))[:12]
        al = []
        I = L["invoices"] or {}
        if I.get("no_po"):
            al.append(["warn", f"{I['no_po']} invoice{'s' if I['no_po'] != 1 else ''} without a purchase order"])
        if (L["reqs"] or {}).get("late"):
            al.append(["info", f"{L['reqs']['late']} requisition line{'s' if L['reqs']['late'] != 1 else ''} overdue"])
        if not code:
            al.append(["info", "Posted with no location code"])
        L["alerts"] = al
    return {"company": company, "locations": out, "errors": errors, "date": iso, "read_at": time.strftime("%H:%M"),
            "definitions": DEFINITIONS}


def request(client, company, refresh=False):
    key = (company or "", date.today().isoformat())
    now = time.time()
    with _lock:
        job = _jobs.get(key)
        if job and job["state"] == "running":
            return {"pending": True, "stage": job.get("stage", ""), "previous": job.get("previous")}
        if job and job["state"] == "done" and not refresh and now - job["finished"] < KEEP:
            return job["result"]
        if job and job["state"] == "failed" and not refresh and now - job["finished"] < 60:
            return {"available": False, "error": job["error"]}
        prev = job.get("result") if job and job.get("state") == "done" else None
        job = _jobs[key] = {"state": "running", "started": now, "stage": "Starting", "previous": prev}

    def progress(text):
        with _lock:
            job["stage"] = text

    def run():
        try:
            r = build(client, company, progress)
            with _lock:
                job.update(state="done", finished=time.time(), result=r, previous=None)
        except Exception as exc:                                   # noqa: BLE001
            traceback.print_exc()
            with _lock:
                job.update(state="failed", finished=time.time(), error=str(exc)[:300])
    threading.Thread(target=run, daemon=True, name="todayloc").start()
    return {"pending": True, "stage": "Starting", "previous": prev}


DEFINITIONS = {
    "receipts": "Receipts = purchase receipts posted today (value entries at their expected and actual cost), by "
                "the location of their lines; a receipt on two locations is split evenly.",
    "invoices": "Invoices = purchase invoice headers posted today (amount excluding VAT), by their location; "
                "without a purchase order = no order number on the invoice. Credit memos apart.",
    "orders": "Orders = purchase orders dated today, their amount and status (not released yet = still open).",
    "reqs": "Requisitions = requisition worksheet lines due today, or overdue, by location.",
}
