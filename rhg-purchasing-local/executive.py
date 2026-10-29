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


# ---------------------------------------------------------------- price savings
def _item_key(company, key, a, b):
    return f"items|{company}|{key}|{a.isoformat()}|{b.isoformat()}"


def summarise_items(rows):
    """{location: {item: [cost, quantity]}} and item names, from the purchase value
    entries the tower already reads (same cost and quantity rules as Price movers)."""
    import analytics
    cells, names = {}, {}
    for r in rows:
        item = r.get("Item_No")
        if not item:
            continue
        loc = r.get("Location_Code") or ""
        c = cells.setdefault(loc, {}).setdefault(item, [0.0, 0.0])
        c[0] += analytics._cost(r)
        c[1] += analytics._qty(r)
        if r.get("Item_Description"):
            names[item] = r["Item_Description"].strip()
    for by_item in cells.values():
        for v in by_item.values():
            v[0], v[1] = round(v[0], 2), round(v[1], 3)
    return cells, names


def _read_items(cache, company, key, a, b, today, refresh):
    ck = _item_key(company, key, a, b)
    with _lock:
        hit = _data().get(ck)
    if hit:
        age = time.time() - hit.get("at", 0)
        open_month = (today - b).days <= OPEN_DAYS
        ttl = 3600 if b >= today - timedelta(days=1) else (6 * 3600 if open_month else 30 * 86400)
        if age < ttl and not (refresh and open_month and age > 120):
            return hit
    closed = b < today - timedelta(days=1)
    rows = cache.get(company, a.isoformat(), b.isoformat(), 86400 if closed else 600, kind="value")["rows"]
    cells, names = summarise_items(rows)
    del rows
    with cache.lock:                     # the rows stay on disk; do not hold a year of them in memory
        cache.store.pop(("value", company, a.isoformat(), b.isoformat()), None)
    row = {"at": time.time(), "cells": cells, "names": names}
    with _lock:
        d = _data()
        for stale in [k for k in d if k.startswith(f"items|{company}|{key}|") and k != ck]:
            d.pop(stale, None)
        d[ck] = row
        _save()
    return row


MAX_PRICE_JUMP = 3.0      # a unit cost more than 4x or under 1/4 of last year's is a unit or data error


def savings_summary(cur, ly, top=6):
    """cur, ly: {item: [cost, qty]}. Price effect on the items bought in both periods:
    (this year's average unit cost - last year's) x this year's quantity."""
    cur_total = sum(v[0] for v in cur.values())
    ly_total = sum(v[0] for v in ly.values())
    save = extra = matched = 0.0
    excluded, excluded_cost, items = 0, 0.0, []
    for it, (c, q) in cur.items():
        p = ly.get(it)
        if not p or q <= 0 or c <= 0 or p[1] <= 0 or p[0] <= 0:
            continue
        ca, la = c / q, p[0] / p[1]
        if ca > la * (1 + MAX_PRICE_JUMP) or ca < la / (1 + MAX_PRICE_JUMP):
            excluded += 1
            excluded_cost += c
            continue
        eff = (ca - la) * q
        matched += c
        if eff < 0:
            save -= eff
        else:
            extra += eff
        items.append([it, round(ca, 2), round(la, 2), round(q, 2), round(eff, 0)])
    net = extra - save
    items.sort(key=lambda x: x[4])
    best = [x for x in items if x[4] < 0][:top]
    worst = [x for x in reversed(items) if x[4] > 0][:top]
    return {"cur": round(cur_total, 0), "ly": round(ly_total, 0), "save": round(save, 0),
            "extra": round(extra, 0), "net": round(net, 0), "matched": round(matched, 0),
            "volume": round((cur_total - ly_total) - net, 0), "items": len(items),
            "excluded": excluded, "excluded_cost": round(excluded_cost, 0), "best": best, "worst": worst}


_savings_memo = {}


def savings(company, year, today=None):
    """Every period the dashboard can pick (month, quarter to date, year to date, up to each
    month of the year) for the whole company and each location, against the same dates
    last year. Worked out here once, so the page filters without asking again."""
    today = today or date.today()
    plan = months_of(year, today)
    with _lock:
        d = _data()
        held = {k: d.get(_item_key(company, k, a, b)) for k, a, b in plan}
    sig = (company, year, tuple(sorted((k, v["at"]) for k, v in held.items() if v)))
    if sig in _savings_memo:
        return _savings_memo[sig]
    keys = [k for k, _, _ in plan if "~" not in k]
    names = {}
    for v in held.values():
        if v:
            names.update(v.get("names") or {})

    def total(month_keys):
        out = {}
        for mk in month_keys:
            row = held.get(mk)
            if not row:
                return None
            for loc, by_item in row["cells"].items():
                for scope in ("*", loc):
                    t = out.setdefault(scope, {})
                    for it, (c, q) in by_item.items():
                        x = t.setdefault(it, [0.0, 0.0])
                        x[0] += c
                        x[1] += q
        return out

    ranges, used = {}, set()
    year_keys = [k for k in keys if k.startswith(str(year))]
    for end in year_keys:
        m = int(end[5:])
        for mode, start in (("month", m), ("quarter", (m - 1) // 3 * 3 + 1), ("ytd", 1)):
            cur_keys = [f"{year}-{i:02d}" for i in range(start, m + 1)]
            ly_keys = [(f"{k}~ly" if f"{k}~ly" in held else f"{year - 1}{k[4:]}") for k in cur_keys]
            cur, ly = total(cur_keys), total(ly_keys)
            if cur is None or ly is None:
                continue
            out = {}
            for scope in set(cur) | set(ly):
                out[scope] = savings_summary(cur.get(scope, {}), ly.get(scope, {}))
                for x in out[scope]["best"] + out[scope]["worst"]:
                    used.add(x[0])
            ranges[f"{mode}|{end}"] = out
    result = {"ranges": ranges, "names": {k: names.get(k, "") for k in used},
              "max_jump_pct": MAX_PRICE_JUMP * 100}
    _savings_memo.clear()
    _savings_memo[sig] = result
    return result


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
            "savings": savings(company, year, today),
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
                           "last year's) x this year's quantity. Cheaper items are savings, dearer items extra "
                           "spending. The rest of the change in spend is volume and mix: more or less bought, "
                           "and items bought in only one of the two periods. Item purchases come from the "
                           "purchase value entries (receipts at expected cost until invoiced), by location; "
                           "a unit cost more than four times or under a quarter of last year's is treated as "
                           "a unit or data error and left out."}}


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
        _jobs[key]["total"] = len(months) * (2 if cache is not None else 1)
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
        if cache is not None:
            # one month at a time: a month of value entries is tens of thousands of rows
            for k, a, b in months:
                try:
                    _read_items(cache, company, k, a, b, today, refresh)
                except Exception as exc:                       # noqa: BLE001
                    errors.append(f"items {k}: {str(exc)[:200]}")
                with _jobs_lock:
                    _jobs[key]["done"] += 1
        state = "done"
    except Exception as exc:                                       # noqa: BLE001
        traceback.print_exc()
        errors.append(str(exc)[:300])
        state = "failed"
    with _jobs_lock:
        _jobs[key].update(state=state, finished=time.time(), errors=errors)
