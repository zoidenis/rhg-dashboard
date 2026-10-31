"""Location intelligence → Today: what every location did today, so far.

Per location (the restaurant registry of Settings → Location intelligence; scope "all" = every restaurant of
every company, for Sales):
  * Simphony: the business day so far (net sales excluding VAT, checks, guests, the average check, discounts,
    voids, error corrects, checks still open) next to the same weekday last week and last year (whole days),
    and today's payments by tender group (cash, cards, other);
  * the budget of the day: the month's G/L budget of the restaurant's department spread over its days;
  * Business Central, entries posted today on the restaurant's department (dimension set) or location code:
    supplier invoices and payments, customer invoices and receipts, cash-box, bank and online receipts,
    stock movements by kind (receipts, sales and consumption, adjustments, transfers), fixed-asset postings.
Simphony's figures of the day refresh every 15 minutes; Business Central's every 5. Read-only; the read
runs in the background and the screen asks again.
"""
import threading
import time
import traceback
from datetime import date, timedelta

import brand_report
import cashloc
import locintel
import simphony
import store

_jobs, _lock = {}, threading.Lock()
KEEP = 240
_sets = {}
STOCK = {"Purchase": "receipts", "Sale": "sales", "Consumption": "consumption", "Output": "output",
         "Positive Adjmt.": "adj_in", "Negative Adjmt.": "adj_out", "Transfer": "transfer"}


def _dim_sets(client, company):
    hit = _sets.get(company)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    value = client.dimension_sets(company)
    _sets[company] = (time.time(), value)
    return value


def _pos(f):
    return {"net": round(f["net"]), "ex": round(f["net"] - (f.get("tax") or 0)), "checks": f["checks"],
            "guests": f["guests"], "discounts": round(f["discounts"]), "voids": round(f["voids"]),
            "gross": round(f["gross"]), "error_corrects": round(f["error_corrects"]),
            "open_checks": f["open_checks"], "open_value": round(f["open_value"]),
            "returns": round(f["returns"])}


def build(client, company, scope, currency_of, latest_fn, parallel_fn, progress):
    settings = store.get_settings()
    lim = locintel.thresholds(settings)
    T = date.today()
    lw, ly = T - timedelta(days=7), T - timedelta(days=364)
    every = scope == "all"
    reg = [r for r in locintel.registry(settings)
           if every or (r.get("company") or "").upper() == (company or "").upper()]
    out, errors = {}, []
    by_dept, by_bc = {}, {}

    def key_of(r):
        return r.get("dept") or r.get("bc") or r["ref"]
    for r in reg:
        if r.get("dept"):
            by_dept.setdefault(r["dept"].upper(), r)
        if r.get("bc"):
            by_bc.setdefault(r["bc"].upper(), r)

    def loc(code, r=None):
        if r is None:
            r = by_dept.get((code or "").upper()) or by_bc.get((code or "").upper())
        k = key_of(r) if r else (code or "")
        L = out.get(k)
        if L is None:
            cur = currency_of(r.get("company")) if r and r.get("company") else currency_of(company)
            L = out[k] = {"name": (r or {}).get("name") or code or "Not on a location",
                          "group": "restaurant" if r else ("unassigned" if not code else "support"),
                          "brand": (r or {}).get("brand") or "", "ref": (r or {}).get("ref") or "",
                          "company": (r or {}).get("company") or company, "currency": cur,
                          "seasonal": bool((r or {}).get("seasonal")), "pos": None, "lw": None, "ly": None,
                          "tenders": None, "budget": None, "ap": None, "ar": None, "bank": None, "stock": None,
                          "fa": None, "latest": None}
        return L
    for r in reg:
        loc(None, r)

    # ---- Simphony: today so far, and the same weekday last week and last year
    sim_on = bool(simphony.info().get("configured"))
    if sim_on:
        progress("Simphony: today at every restaurant")
        refs = [r for r in reg if r.get("ref")]
        latest = {}
        for ref, (v, e) in parallel_fn({r["ref"]: (lambda x=r["ref"]: latest_fn(x)) for r in refs}).items():
            latest[ref] = v if not e else None
        jobs = {}
        for r in refs:
            lt = latest.get(r["ref"])
            loc(None, r)["latest"] = lt
            for tag, d in (("t", T), ("w", lw), ("y", ly)):
                iso = d.isoformat()
                if lt and iso > lt:
                    continue
                jobs[(tag, r["ref"])] = (lambda x=r["ref"], dd=iso, l=lt: simphony.daily_totals(x, dd, l))
            if not lt or T.isoformat() <= lt:
                jobs[("m", r["ref"])] = (lambda x=r["ref"], l=lt: simphony.tender_totals(x, T.isoformat(), l))
                jobs[("d", r["ref"])] = (lambda x=r["ref"]: simphony.tender_dims(x))
        res = parallel_fn(jobs, workers=4)
        simphony.flush_days()
        bad = sorted({r for (tag, r), (v, e) in res.items() if e})
        if bad:
            errors.append(f"Simphony could not be read for {', '.join(bad[:5])}{' …' if len(bad) > 5 else ''}")
        for r in refs:
            L = loc(None, r)
            for tag, field in (("t", "pos"), ("w", "lw"), ("y", "ly")):
                v, e = res.get((tag, r["ref"]), (None, None))
                if v and not e:
                    L[field] = _pos(simphony.day_figures(v.get("rvcs") or []))
            tv, dv = res.get(("m", r["ref"]), (None, None))[0], res.get(("d", r["ref"]), (None, None))[0] or {}
            if tv:
                t = {"cash": 0.0, "card": 0.0, "other": 0.0, "paid_out": 0.0}
                for num, (ttl, cnt) in tv.items():
                    g = cashloc.tender_group(dv.get(int(num)) or {"name": f"Tender {num}"})
                    if g == "paid_in":
                        continue
                    if g == "paid_out":
                        t["paid_out"] += abs(ttl)
                    else:
                        t[g if g in ("cash", "card") else "other"] += ttl
                L["tenders"] = {k: round(v) for k, v in t.items()}

    # ---- the budget of the day: the month's budget of the department over its days
    if not every:
        try:
            progress("Business Central: the budget of the day")
            cube = brand_report._cube(client, company, T.year, currency_of)
            for r in reg:
                d = r.get("dept") or r.get("bc")
                if d:
                    b = brand_report._budget_days(lambda y, c=cube: c if y == T.year else None, d, T, T)
                    if b and b.get("sales"):
                        loc(None, r)["budget"] = round(b["sales"])
        except Exception as exc:                                    # noqa: BLE001
            traceback.print_exc()
            errors.append(f"Budget: {str(exc)[:160]}")

    # ---- Business Central, posted today
    if not every:
        iso = T.isoformat()
        try:
            progress("Business Central: dimension sets")
            sets = _dim_sets(client, company)

            def dept(e):
                return sets.get(int(e.get("Dimension_Set_ID") or 0), "")
        except Exception as exc:                                    # noqa: BLE001
            traceback.print_exc()
            errors.append(f"Dimension sets: {str(exc)[:160]}")
            sets = None
        if sets is not None:
            def side():
                return {"invoiced": 0.0, "inv_n": 0, "paid": 0.0, "pay_n": 0, "docs": []}
            try:
                progress("Business Central: supplier entries of today")
                for e in client.vendor_entries(company, iso, iso, max_age=240):
                    L = loc(dept(e))
                    S = L["ap"] = L["ap"] or side()
                    amt, typ = float(e.get("Amount_LCY") or 0), e.get("Document_Type")
                    name = (e.get("Vendor_Name") or e.get("Vendor_No") or "").strip()
                    if typ in ("Invoice", "Credit Memo"):
                        S["invoiced"] += -amt
                        S["inv_n"] += 1 if typ == "Invoice" else 0
                        S["docs"].append([str(e.get("Document_No") or ""), name, typ, round(-amt)])
                    elif typ in ("Payment", "Refund"):
                        S["paid"] += amt
                        S["pay_n"] += 1
                        S["docs"].append([str(e.get("Document_No") or ""), name, typ, round(amt)])
            except Exception as exc:                                # noqa: BLE001
                traceback.print_exc()
                errors.append(f"Supplier entries: {str(exc)[:160]}")
            try:
                progress("Business Central: customer entries of today")
                for e in client.customer_entries(company, iso, iso, max_age=240):
                    L = loc(dept(e))
                    S = L["ar"] = L["ar"] or side()
                    amt, typ = float(e.get("Amount_LCY") or 0), e.get("Document_Type")
                    name = (e.get("Customer_Name") or e.get("Customer_No") or "").strip()
                    if typ in ("Invoice", "Credit Memo"):
                        S["invoiced"] += amt
                        S["inv_n"] += 1 if typ == "Invoice" else 0
                        S["docs"].append([str(e.get("Document_No") or ""), name, typ, round(amt)])
                    elif typ in ("Payment", "Refund"):
                        S["paid"] += -amt
                        S["pay_n"] += 1
                        S["docs"].append([str(e.get("Document_No") or ""), name, typ, round(-amt)])
            except Exception as exc:                                # noqa: BLE001
                traceback.print_exc()
                errors.append(f"Customer entries: {str(exc)[:160]}")
            try:
                progress("Business Central: cash boxes and banks today")
                for e in client.bank_entries_dim(company, iso, iso):
                    L = loc(dept(e))
                    B = L["bank"] = L["bank"] or {"received": 0.0, "cash": 0.0, "bank": 0.0, "online": 0.0, "out": 0.0, "n": 0}
                    dr, cr = float(e.get("Debit_Amount_LCY") or 0), float(e.get("Credit_Amount_LCY") or 0)
                    if not dr and not cr:
                        a = float(e.get("Amount_LCY") or 0)
                        dr, cr = max(a, 0), max(-a, 0)
                    kind = cashloc.account_kind(e.get("Bank_Account_No"), e.get("Bank_Account_Name"))
                    B["received"] += dr
                    B[kind] += dr
                    B["out"] += cr
                    B["n"] += 1
            except Exception as exc:                                # noqa: BLE001
                traceback.print_exc()
                errors.append(f"Bank entries: {str(exc)[:160]}")
            try:
                progress("Business Central: stock movements of today")
                for e in client.item_ledger(company, iso, iso):
                    L = loc(e.get("Location_Code") or "")
                    S = L["stock"] = L["stock"] or {"n": 0, "items": 0, **{v: 0.0 for v in set(STOCK.values())}, "other": 0.0}
                    k = STOCK.get(e.get("Entry_Type") or "", "other")
                    S[k] += abs(float(e.get("Cost_Amount_Actual") or 0))
                    S["n"] += 1
            except Exception as exc:                                # noqa: BLE001
                traceback.print_exc()
                errors.append(f"Stock movements: {str(exc)[:160]}")
            try:
                progress("Business Central: fixed assets of today")
                for e in client.fa_entries(company, iso, iso):
                    L = loc(e.get("FA_Location_Code") or "")
                    F = L["fa"] = L["fa"] or {"acquisition": 0.0, "depreciation": 0.0, "disposal": 0.0, "other": 0.0, "n": 0, "assets": []}
                    t = (e.get("FA_Posting_Type") or "").lower()
                    k = "acquisition" if "acquisition" in t else "depreciation" if "depreciation" in t else \
                        "disposal" if ("disposal" in t or "proceeds" in t) else "other"
                    F[k] += float(e.get("Amount_LCY") or 0)
                    F["n"] += 1
                    if len(F["assets"]) < 12:
                        F["assets"].append([str(e.get("FA_No") or ""), (e.get("FA_Description") or "").strip(),
                                            e.get("FA_Posting_Type") or "", round(float(e.get("Amount_LCY") or 0))])
            except Exception as exc:                                # noqa: BLE001
                traceback.print_exc()
                errors.append(f"Fixed assets: {str(exc)[:160]}")

    # ---- rounding and alerts
    for code, L in out.items():
        for k in ("ap", "ar"):
            if L[k]:
                L[k]["invoiced"], L[k]["paid"] = round(L[k]["invoiced"]), round(L[k]["paid"])
                L[k]["docs"] = sorted(L[k]["docs"], key=lambda x: -abs(x[3]))[:12]
        for k in ("bank", "stock", "fa"):
            if L[k]:
                L[k] = {a: (round(v) if isinstance(v, float) else v) for a, v in L[k].items()}
        al, P = [], L["pos"]
        if P and P["gross"]:
            if P["discounts"] > P["gross"] * lim["discounts"] / 100:
                al.append(["warn", f"Discounts {P['discounts'] / P['gross'] * 100:.0f}% of gross sales"])
            if P["voids"] > P["gross"] * lim["voids"] / 100:
                al.append(["warn", f"Voids {P['voids'] / P['gross'] * 100:.1f}% of gross sales"])
            if P["error_corrects"] > P["gross"] * lim["error_corrects"] / 100:
                al.append(["warn", f"Error corrects {P['error_corrects'] / P['gross'] * 100:.0f}% of gross sales"])
        if P and P["open_checks"]:
            al.append(["info", f"{P['open_checks']} check{'s' if P['open_checks'] != 1 else ''} open ({P['open_value']:,})"])
        if L["group"] == "restaurant" and L["ref"] and sim_on and not P and L.get("latest") and L["latest"] < T.isoformat():
            al.append(["info", "No sales in Simphony yet today"])
        L["alerts"] = sorted(al, key=lambda x: {"crit": 0, "warn": 1, "info": 2}[x[0]])
    keep = {k: v for k, v in out.items()
            if v["group"] == "restaurant" or any(v[x] for x in ("pos", "ap", "ar", "bank", "stock", "fa"))}
    return {"company": company, "scope": scope, "currency": currency_of(company), "locations": keep, "errors": errors,
            "date": T.isoformat(), "last_week": lw.isoformat(), "last_year": ly.isoformat(), "simphony": sim_on,
            "limits": lim, "read_at": time.strftime("%H:%M"), "definitions": DEFINITIONS}


def request(client, company, scope, currency_of, latest_fn, parallel_fn, refresh=False):
    key = (company or "", scope, date.today().isoformat())
    now = time.time()
    with _lock:
        job = _jobs.get(key)
        if job and job["state"] == "running":
            return {"pending": True, "stage": job.get("stage", ""), "elapsed": int(now - job["started"]),
                    "previous": job.get("previous")}
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
            r = build(client, company, scope, currency_of, latest_fn, parallel_fn, progress)
            with _lock:
                job.update(state="done", finished=time.time(), result=r, previous=None)
        except Exception as exc:                                   # noqa: BLE001
            traceback.print_exc()
            with _lock:
                job.update(state="failed", finished=time.time(), error=str(exc)[:300])
    threading.Thread(target=run, daemon=True, name="today").start()
    return {"pending": True, "stage": "Starting", "elapsed": 0, "previous": prev}


DEFINITIONS = {
    "pos": "Simphony: the business day so far (net sales excluding VAT, checks, guests, discounts, voids, error "
           "corrects, checks still open), refreshed every 15 minutes, next to the same weekday last week and last "
           "year (whole days). Payments by tender: cash, cards and the rest, as on Cash & bank.",
    "budget": "Budget of the day = the month's G/L budget of the restaurant's department spread evenly over the "
              "month's days. The day is still running, so the gap closes as the day goes on.",
    "bc": "Business Central: entries with today's posting date on the restaurant's department (dimension set) — "
          "supplier and customer invoices and payments, cash-box, bank and online receipts — or on its location "
          "code (stock movements, fixed assets). Entries on no department show under Not on a location.",
}
