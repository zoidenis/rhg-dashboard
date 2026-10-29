"""Purchasing figures for the executive dashboard in the Control center.

Posted purchase invoices, month by month for the chosen year and the year before,
summed by restaurant (DEPARTMENT, shortcut dimension 1) and supplier. The page cuts
these totals by period, restaurant and comparison on its own, so a filter never asks
Business Central for another report.

An invoice "without a purchase order" is one whose header carries no order number,
the same test as the monthly trend on the Invoices without PO screen. Cancelled and
corrective invoices are left out, as there.
"""
import json
import threading
import time
import traceback
from calendar import monthrange
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

import savings
import store

CACHE_FILE = store.DATA_DIR / "executive_purchases.json"
OPEN_DAYS = 45                       # late invoices still arrive this long after a month ends

_lock = threading.Lock()
_mem = {"data": None}
_jobs, _jobs_lock = {}, threading.Lock()


def _data():
    if _mem["data"] is None:
        try:
            _mem["data"] = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _mem["data"] = {}
    return _mem["data"]


def _save():
    tmp = CACHE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(_mem["data"], separators=(",", ":")), encoding="utf-8")
    tmp.replace(CACHE_FILE)


def months_of(year, today=None):
    """[(key, from, to)]: every month of the year before and of the year, plus, for the
    month in progress, the same days of last year (~ly) and of the month before (~prior)
    so the page can compare like for like."""
    today = today or date.today()
    out, extra = [], []
    for y in (year - 1, year):
        for m in range(1, 13):
            a = date(y, m, 1)
            if a > today:
                break
            b = date(y, m, monthrange(y, m)[1])
            if a <= today <= b and y == year:
                b = today
                try:
                    extra.append((f"{y}-{m:02d}~ly", a.replace(year=y - 1), b.replace(year=y - 1)))
                except ValueError:
                    extra.append((f"{y}-{m:02d}~ly", a.replace(year=y - 1), date(y - 1, 2, 28)))
                pa = (a - timedelta(days=1)).replace(day=1)
                extra.append((f"{y}-{m:02d}~prior", pa,
                              min(pa + (b - a), pa.replace(day=monthrange(pa.year, pa.month)[1]))))
            out.append((f"{y}-{m:02d}", a, min(b, today)))
    return out + extra


def summarise(headers):
    """{dept: {vendor: [amount, invoices, without_po, amount_without_po]}}, vendor names,
    and the foreign-currency invoices, which are summed at face value."""
    cells, names, foreign = {}, {}, 0
    for h in headers:
        if h.get("Cancelled") or h.get("Corrective"):
            continue
        dept = str(h.get("Shortcut_Dimension_1_Code") or "")
        vendor = str(h.get("Buy_from_Vendor_No") or "?")
        names[vendor] = h.get("Buy_from_Vendor_Name") or vendor
        amt = float(h.get("Amount") or 0)
        no_po = not str(h.get("Order_No") or "").strip()
        if (h.get("Currency_Code") or "").strip():
            foreign += 1
        c = cells.setdefault(dept, {}).setdefault(vendor, [0.0, 0, 0, 0.0])
        c[0] += amt
        c[1] += 1
        if no_po:
            c[2] += 1
            c[3] += amt
    for by_vendor in cells.values():
        for v in by_vendor.values():
            v[0], v[3] = round(v[0], 2), round(v[3], 2)
    return cells, names, foreign


def _read_month(client, company, key, a, b, today, refresh):
    ck = f"{company}|{key}|{a.isoformat()}|{b.isoformat()}"
    with _lock:
        hit = _data().get(ck)
    if hit:
        age = time.time() - hit.get("at", 0)
        open_month = (today - b).days <= OPEN_DAYS
        ttl = 3600 if b >= today - timedelta(days=1) else (6 * 3600 if open_month else 30 * 86400)
        if age < ttl and not (refresh and open_month and age > 120):
            return hit
    cells, names, foreign = summarise(client.posted_invoice_headers(company, a.isoformat(), b.isoformat()))
    row = {"at": time.time(), "to": b.isoformat(), "cells": cells, "names": names, "foreign": foreign}
    with _lock:
        d = _data()
        for stale in [k for k in d if k.startswith(f"{company}|{key}|") and k != ck]:
            d.pop(stale, None)          # the month in progress gets a new end every day
        d[ck] = row
        _save()
    return row


def assemble(company, year, currency, today=None):
    today = today or date.today()
    months = months_of(year, today)
    out, vendors, foreign = {}, {}, {}
    with _lock:
        d = _data()
        for key, a, b in months:
            row = d.get(f"{company}|{key}|{a.isoformat()}|{b.isoformat()}")
            if not row:
                continue
            vendors.update(row.get("names") or {})
            foreign[key] = row.get("foreign", 0)
            for dept, by_vendor in (row.get("cells") or {}).items():
                out.setdefault(dept, {})[key] = by_vendor
    return {"company": company, "year": year, "currency": currency,
            "months": [k for k, _, _ in months if "~" not in k], "held": len(foreign),
            "invoices": out, "vendors": vendors, "foreign": foreign,
            "savings": savings.dashboard_ranges(company, year, [k for k, _, _ in months if "~" not in k], today),
            "definitions": {
                "spend": "Posted purchase invoices, amount excluding VAT, by posting date. Invoices in a "
                         "foreign currency are summed at face value; how many there are is shown.",
                "no_po": "An invoice without a purchase order carries no order number on its header. "
                         "Cancelled and corrective invoices are left out.",
                "restaurant": "The restaurant is the DEPARTMENT on the invoice header; invoices with none "
                              "are shown as Not on a restaurant.",
                "share": "Purchases as a share of revenue compare what was invoiced with what was sold in "
                         "the same months. Stock bought and not yet used makes it differ from food cost.",
                "savings": "Savings and extra spending are the price effect against the same dates last "
                           "year: for every item bought in both periods, (this year's average unit cost - "
                           "last year's) x this year's quantity, from the posted purchase invoice lines in "
                           "base units. The rest of the change in spend is volume and mix. The restaurant "
                           "filter follows the location on the invoice. The full report, with suppliers and "
                           "categories, is in Purchasing → Price savings."}}


def request(client, company, year, currency, refresh=False, cache=None):
    key = (company, int(year))
    now = time.time()
    start = False
    with _jobs_lock:
        job = _jobs.get(key)
        if not job or (job["state"] != "running" and (refresh or now - job.get("finished", 0) > 900)):
            job = _jobs[key] = {"state": "running", "started": now, "done": 0, "total": 0, "errors": []}
            start = True
        snap = dict(job)
    if start:
        threading.Thread(target=_run, args=(client, company, int(year), key, refresh, cache),
                         daemon=True, name="executive-purchases").start()
    payload = assemble(company, int(year), currency)
    payload.update(pending=snap["state"] == "running", errors=snap.get("errors") or [],
                   progress={"done": snap.get("done", 0), "total": snap.get("total", 0)})
    return payload


def _run(client, company, year, key, refresh, cache=None):
    today = date.today()
    months = list(reversed(months_of(year, today)))
    errors = []
    with _jobs_lock:
        _jobs[key]["total"] = len(months) + len({k for k, _, _ in months if "~" not in k})
    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {k: pool.submit(_read_month, client, company, k, a, b, today, refresh)
                       for k, a, b in months}
            for k, f in futures.items():
                try:
                    f.result()
                except Exception as exc:                           # noqa: BLE001
                    errors.append(f"{k}: {str(exc)[:200]}")
                with _jobs_lock:
                    _jobs[key]["done"] += 1
        # item invoice lines for the price savings, kept by the savings report
        wanted = sorted({k for k, _, _ in months if "~" not in k}, reverse=True)

        def tick():
            with _jobs_lock:
                _jobs[key]["done"] += 1
        errors += [f"items {e}" for e in savings.ensure(client, company, wanted, force=refresh, tick=tick)]
        state = "done"
    except Exception as exc:                                       # noqa: BLE001
        traceback.print_exc()
        errors.append(str(exc)[:300])
        state = "failed"
    with _jobs_lock:
        _jobs[key].update(state=state, finished=time.time(), errors=errors)
