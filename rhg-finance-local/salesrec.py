"""Control → Simphony ↔ BC: the daily sales reconciliation, every restaurant and business day of the last weeks.

Three figures for each restaurant (Settings → Locations registry: the Simphony reference, the BC customer the POS
sales are invoiced to) and day:
  * Simphony: what the till sold, by menu item (getMenuItemDailyTotals, VAT included, before discounts, less the
    item discounts), kept in data/menu_items.db with the menu engineering days;
  * Imported: the POS import of Business Central (MID Simphony Archive): per menu item the line total, the line
    discount, the BC item it was posted as, and whether it went into a sales document (Postuar);
  * Invoiced: the sales invoices (less credit memos) of the restaurant's customer on that posting date in the
    customer ledger, and what is still open on them (the takings not yet applied).
The checks, each one an exception in the register with its owner, closed by the data when it is gone:
  * Not imported: Simphony sold, nothing imported after the import lag (default 2 days);
  * Import ≠ Simphony: the imported total differs beyond the tolerance (the menu items that differ are listed);
  * Not posted: imported lines not in a sales document, or no invoice, after the posting lag (default 4 days);
  * Invoice ≠ import: the posted invoice differs from the import (or from Simphony when nothing was imported);
  * Not settled: the day's invoice still open (cash, cards, online not applied) after the settle days (default 7);
  * Item mapping, per menu item over the period: imported with no BC item, an item BC does not know, a blocked
    item, an item that needs a recipe and has none, or a menu item Simphony sold that the import never carries.
A day still within its lag is "Pending", not an exception. Nothing is written to Simphony or Business Central.
"""
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta

import config
import locintel
import menueng
import recipes
import simphony
import store

E_ARCHIVE = config.E_POS_ARCHIVE
DEFAULTS = {"days": "14", "import_lag": "2", "post_lag": "4", "settle_days": "7", "tol_abs": "1000", "tol_pct": "0.5"}
STATUS = {"not_imported": "Not imported", "import_diff": "Import ≠ Simphony", "invoice_diff": "Invoice ≠ import",
          "not_posted": "Not posted", "unpaid": "Not settled", "pending": "Pending", "ok": "Reconciled",
          "no_sales": "No sales"}
RANK = ("not_imported", "import_diff", "invoice_diff", "not_posted", "unpaid", "pending", "ok", "no_sales")
MAPPING = {"no_item": "Imported with no BC item", "unknown_item": "BC item does not exist",
           "blocked_item": "BC item is blocked", "no_recipe": "Needs a recipe, has none",
           "not_in_import": "Sold in Simphony, never imported"}
_results, _jobs, _lock = {}, {}, threading.Lock()
on_built = None          # (company, result) → the exceptions register (server.register_after_sales)
KEEP = 900


def settings_of(settings):
    s = dict(DEFAULTS, **{k: v for k, v in ((settings or {}).get("sales_rec") or {}).items() if v not in (None, "")})

    def num(k, lo, hi):
        try:
            return max(lo, min(hi, float(s[k])))
        except (TypeError, ValueError):
            return float(DEFAULTS[k])
    return {"days": int(num("days", 3, 62)), "import_lag": int(num("import_lag", 0, 30)),
            "post_lag": int(num("post_lag", 0, 30)), "settle_days": int(num("settle_days", 0, 90)),
            "tol_abs": num("tol_abs", 0, 1e9), "tol_pct": num("tol_pct", 0, 100)}


def _dates(a, b):
    out, d = [], a
    while d <= b:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def restaurants(company, settings=None):
    """The registry rows of a company whose POS sales are invoiced to a customer."""
    return [r for r in locintel.registry(settings) if (r.get("company") or "").upper() == (company or "").upper()
            and r.get("customer")]


def companies(settings=None):
    return sorted({r["company"] for r in locintel.registry(settings) if r.get("company") and r.get("customer")})


# ---------------------------------------------------------------- reads
def read_bc(client, company, a, b, customers):
    """imp: {(customer, day): {"mi": {menu item: [total, count, discount, BC item, name]}, "unposted", "rows",
    "imported_at"}}; inv: {(customer, day): {"amount", "remaining", "docs"}}."""
    cust = {c.upper(): c for c in customers}
    imp, inv = {}, {}
    rows = client._get_all(client._url(company, E_ARCHIVE), {
        "$filter": f"TransactionDateArch ge {a} and TransactionDateArch le {b}",
        "$select": "CustomerNo,LocationCode,TransactionDateArch,Menu_Item_Number,Menu_Item_Name1,ItemNo,Postuar,"
                   "RLineTotal,RLineCount,LineDiscount,SystemCreatedAt88448"}, page_size=5000)
    for r in rows:
        c = cust.get(str(r.get("CustomerNo") or "").upper()) or cust.get(str(r.get("LocationCode") or "").upper())
        if not c:
            continue
        d = str(r.get("TransactionDateArch") or "")[:10]
        x = imp.setdefault((c, d), {"mi": {}, "unposted": 0, "rows": 0, "imported_at": ""})
        mi = str(r.get("Menu_Item_Number") or "").strip()
        m = x["mi"].setdefault(mi, [0.0, 0.0, 0.0, "", ""])
        m[0] += float(r.get("RLineTotal") or 0)
        m[1] += float(r.get("RLineCount") or 0)
        m[2] += float(r.get("LineDiscount") or 0)
        m[3] = m[3] or str(r.get("ItemNo") or "").strip()
        m[4] = m[4] or str(r.get("Menu_Item_Name1") or "").strip()
        x["rows"] += 1
        x["unposted"] += 0 if r.get("Postuar") else 1
        x["imported_at"] = max(x["imported_at"], str(r.get("SystemCreatedAt88448") or "")[:16])
    led = client._get_all(client._url(company, config.E_CUSTOMER), {
        "$filter": f"Posting_Date ge {a} and Posting_Date le {b} and "
                   f"(Document_Type eq 'Invoice' or Document_Type eq 'Credit Memo')",
        "$select": "Customer_No,Posting_Date,Document_Type,Document_No,Amount_LCY,Remaining_Amt_LCY,Open"})
    for r in led:
        c = cust.get(str(r.get("Customer_No") or "").upper())
        if not c:
            continue
        d = str(r.get("Posting_Date") or "")[:10]
        x = inv.setdefault((c, d), {"amount": 0.0, "remaining": 0.0, "docs": []})
        x["amount"] += float(r.get("Amount_LCY") or 0)
        x["remaining"] += float(r.get("Remaining_Amt_LCY") or 0)
        x["docs"].append(f"{'CM ' if r.get('Document_Type') == 'Credit Memo' else ''}{r.get('Document_No') or ''}")
    return imp, inv


def read_simphony(refs, days, latest_fn, progress=None):
    """{ref: {day: {menu item: (sales, count, discounts)}}} for the days each location has closed; errors."""
    errors, latest = [], {}
    for ref in refs:
        try:
            latest[ref] = latest_fn(ref)
        except Exception:                                           # noqa: BLE001
            latest[ref] = None
    jobs = [(r, d) for r in refs for d in days if latest.get(r) and d <= latest[r]]
    done = 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = {pool.submit(menueng.read_day, r, d, latest.get(r), True): (r, d) for r, d in jobs}
        for f in as_completed(futs):
            done += 1
            try:
                f.result()
            except Exception as exc:                                # noqa: BLE001
                if len(errors) < 6:
                    errors.append(f"Simphony {futs[f][0]} {futs[f][1]}: {str(exc)[:140]}")
            if progress and (done % 10 == 0 or done == len(jobs)):
                progress(f"Simphony · {done} of {len(jobs)} days")
    out = {r: menueng.items_vat(r, days[0], days[-1]) for r in refs}
    return out, latest, errors


# ---------------------------------------------------------------- the reconciliation
def analyse(company, regs, days, sim, latest, imp, inv, master, S, today=None, currency="ALL", sim_on=True,
            dims=None):
    """dims: {ref: simphony.menu_items(ref)}: names, and the items not included in sales (left out)."""
    today = today or date.today()
    dims = dims or {}
    items = (master or {}).get("items") or {}
    has_bom = {p for p, *_ in (master or {}).get("bom") or []}

    def gap(a, b):
        return abs(a - b) > max(S["tol_abs"], S["tol_pct"] / 100 * max(abs(a), abs(b)))
    locs, mapping = [], {}
    tot = {"sim": 0.0, "imp": 0.0, "inv": 0.0, "remaining": 0.0, "imp_gap": 0.0, "inv_gap": 0.0}
    # menu items the import never carries in the period: one exception each (item mapping), so their sales are
    # left out of the daily comparison instead of making every day differ
    ever = {mi for im in imp.values() for mi in im["mi"]}
    counts = {k: 0 for k in RANK}
    for r in regs:
        ref, cu = r["ref"], r["customer"]
        L = {"ref": ref, "name": r["name"], "brand": r.get("brand") or "", "bc": r.get("bc") or cu, "customer": cu,
             "latest": latest.get(ref), "cells": {}, "issues": 0, "gap": 0.0}
        excl = {k for k, v in (dims.get(ref) or {}).items() if v.get("excluded")}
        sim_days = {d: {k: v for k, v in x.items() if k not in excl} for d, x in (sim.get(ref) or {}).items()}
        for d in days:
            age = (today - date.fromisoformat(d)).days
            sd = sim_days.get(d) if sim_on else None
            s_t = round(sum(v[0] - v[2] for v in sd.values()), 2) if sd is not None else None
            unmapped = round(sum(v[0] - v[2] for k, v in sd.items() if str(k) not in ever), 2) if sd and imp else 0.0
            im = imp.get((cu, d))
            i_t = round(sum(v[0] + v[2] for v in im["mi"].values()), 2) if im else None
            iv = inv.get((cu, d))
            v_t = round(iv["amount"], 2) if iv else None
            issues = []
            if s_t and i_t is None:
                issues.append("not_imported" if age > S["import_lag"] else "pending")
            if s_t is not None and i_t is not None and gap(i_t, s_t - unmapped):
                issues.append("import_diff")
            if i_t is not None:
                if (im["unposted"] or v_t is None) and age > S["post_lag"]:
                    issues.append("not_posted")
                elif im["unposted"] or v_t is None:
                    issues.append("pending")
            if v_t is not None:
                base = i_t if i_t is not None else s_t
                if base is not None and gap(v_t, base):
                    issues.append("invoice_diff")
                if iv["remaining"] > S["tol_abs"] and age > S["settle_days"]:
                    issues.append("unpaid")
            if not issues:
                issues = ["no_sales"] if not (s_t or i_t or v_t) else ["ok"]
            status = min(issues, key=RANK.index)
            diffs = []
            if sd is not None and im:
                for mi in set(str(k) for k in sd) | set(im["mi"]):
                    a = sd.get(int(mi)) if mi.isdigit() else None
                    b = im["mi"].get(mi)
                    sv, sc = (a[0] - a[2], a[1]) if a else (0.0, 0.0)
                    iv_, ic = (b[0] + b[2], b[1]) if b else (0.0, 0.0)
                    if abs(sv - iv_) >= 1 or abs(sc - ic) >= 0.01:
                        nm = (b or [""] * 5)[4] or ((dims.get(ref) or {}).get(int(mi) if mi.isdigit() else mi) or {}).get("name", "")
                        diffs.append({"mi": mi, "name": nm, "item": (b or [""] * 5)[3],
                                      "sim": round(sv, 2), "sim_qty": sc, "imp": round(iv_, 2), "imp_qty": ic,
                                      "diff": round(iv_ - sv, 2)})
                diffs.sort(key=lambda x: -abs(x["diff"]))
            cell = {"status": status, "issues": [i for i in dict.fromkeys(issues) if i not in ("ok", "no_sales")],
                    "sim": s_t, "imp": i_t, "inv": v_t, "age": age,
                    "remaining": round(iv["remaining"], 2) if iv else None, "docs": iv["docs"] if iv else [],
                    "unposted": im["unposted"] if im else 0, "rows": im["rows"] if im else 0,
                    "imported_at": im["imported_at"] if im else "", "items": diffs[:25], "items_n": len(diffs),
                    "unmapped": unmapped}
            L["cells"][d] = cell
            counts[status] += 1
            L["issues"] += sum(1 for i in cell["issues"] if i != "pending")
            for k, v in (("sim", s_t), ("imp", i_t), ("inv", v_t)):
                tot[k] += v or 0
            if iv:
                tot["remaining"] += max(iv["remaining"], 0)
            if s_t is not None and i_t is not None:
                L["gap"] += i_t - s_t
                tot["imp_gap"] += i_t - s_t
            if v_t is not None and i_t is not None:
                tot["inv_gap"] += v_t - i_t
            # item mapping, over the period
            if im:
                for mi, b in im["mi"].items():
                    k = None
                    card = items.get(b[3]) if b[3] else None
                    if not b[3]:
                        k = "no_item"
                    elif items and card is None:
                        k = "unknown_item"
                    elif card and card.get("Blocked"):
                        k = "blocked_item"
                    elif card and ((card.get("Assembly_Policy") or "") == "Assemble-to-Order"
                                   or (card.get("Replenishment_System") or "") == "Assembly") \
                            and b[3] not in has_bom and not card.get("Assembly_BOM"):
                        k = "no_recipe"
                    if k:
                        M = mapping.setdefault((k, mi), {"kind": k, "mi": mi, "name": b[4], "item": b[3],
                                                         "value": 0.0, "qty": 0.0, "locations": set(), "days": set()})
                        M["value"] += b[0] + b[2]
                        M["qty"] += b[1]
                        M["locations"].add(L["bc"])
                        M["days"].add(d)
            if sd and im:
                imported = set(im["mi"])
                for mi, a in sd.items():
                    if str(mi) not in imported and abs(a[0] - a[2]) >= 1:
                        M = mapping.setdefault(("not_in_import", str(mi)), {
                            "kind": "not_in_import", "mi": str(mi), "name": ((dims.get(ref) or {}).get(mi) or {}).get("name", ""),
                            "item": "", "value": 0.0, "qty": 0.0,
                            "locations": set(), "days": set(), "_seen": set()})
                        M["value"] += a[0] - a[2]
                        M["qty"] += a[1]
                        M["locations"].add(L["bc"])
                        M["days"].add(d)
        L["gap"] = round(L["gap"], 2)
        locs.append(L)
    # a menu item missing from the import only counts when it was never imported in the period
    names = {mi: b[4] for (c, d), im in imp.items() for mi, b in im["mi"].items() if b[4]}
    maps = []
    for (k, mi), M in mapping.items():
        if k == "not_in_import" and mi in ever:
            continue
        M.pop("_seen", None)
        M.update(value=round(M["value"], 0), locations=sorted(M["locations"]), days=len(M["days"]),
                 last=max(M["days"]), name=M["name"] or names.get(mi, ""), label=MAPPING[k])
        maps.append(M)
    maps.sort(key=lambda M: (list(MAPPING).index(M["kind"]), -abs(M["value"])))
    checked = [c for L in locs for c in L["cells"].values() if c["status"] not in ("pending", "no_sales")]
    kpis = {"sim": round(tot["sim"]), "imp": round(tot["imp"]), "inv": round(tot["inv"]),
            "remaining": round(tot["remaining"]), "imp_gap": round(tot["imp_gap"]),
            "inv_gap": round(tot["inv_gap"]), "days": len(checked),
            "ok": sum(1 for c in checked if c["status"] == "ok"),
            "issues": sum(L["issues"] for L in locs) + len(maps), "mapping": len(maps), "counts": counts}
    return {"company": company, "currency": currency, "from": days[0], "to": days[-1], "days": days,
            "today": today.isoformat(), "settings": S, "locations": locs, "mapping": maps, "kpis": kpis,
            "statuses": STATUS, "mapping_labels": MAPPING, "simphony": sim_on}


def build(client, company, latest_fn, progress=None, today=None):
    today = today or date.today()
    settings = store.get_settings()
    S = settings_of(settings)
    regs = restaurants(company, settings)
    if not regs:
        raise ValueError(f"No restaurant of {company} has a BC customer in Settings → Locations registry.")
    b = today - timedelta(days=1)
    days = _dates(b - timedelta(days=S["days"] - 1), b)
    errors = []
    sim_on = bool(simphony.info().get("configured"))
    sim, latest, dims = {}, {}, {}
    if sim_on:
        (progress or (lambda *_: None))("Simphony · menu items by day")
        sim, latest, errs = read_simphony([r["ref"] for r in regs], days, latest_fn, progress)
        errors += errs
        for ref in sim:
            try:
                dims[ref] = simphony.menu_items(ref)
            except Exception:                                       # noqa: BLE001
                dims[ref] = {}
    (progress or (lambda *_: None))("Business Central · POS import and invoices")
    imp, inv = read_bc(client, company, days[0], days[-1], [r["customer"] for r in regs])
    (progress or (lambda *_: None))("Business Central · item cards and recipes")
    try:
        m = recipes.master_cached(company) or recipes.master(client, company)
    except Exception as exc:                                        # noqa: BLE001
        m, errors = {}, errors + [f"Item cards: {str(exc)[:160]}"]
    ccy = ((settings.get("group") or {}).get("reporting_currency") or "ALL")
    r = analyse(company, regs, days, sim, latest, imp, inv, m, S, today, ccy, sim_on, dims)
    r.update(errors=errors, checked_at=datetime.now().isoformat(timespec="seconds"),
             complete=not errors)
    return r


# ---------------------------------------------------------------- held results and background reads
def held(company):
    with _lock:
        hit = _results.get(company)
    return hit[1] if hit else None


def remember(company, r):
    with _lock:
        _results[company] = (time.time(), r)


def request(client, company, latest_fn, refresh=False):
    """The held result while fresh, else a background read: {"pending": True, "stage"} until it is ready."""
    with _lock:
        hit = _results.get(company)
        job = _jobs.get(company)
        if hit and not refresh and time.time() - hit[0] < KEEP and not (job and job["state"] == "running"):
            return hit[1]
        if job and job["state"] == "running":
            return {"pending": True, "stage": job["stage"], "note": "Reading Simphony and Business Central…"}
        if job and job["state"] == "failed" and not refresh and time.time() - job["at"] < 60:
            _jobs.pop(company, None)
            return {"available": False, "error": job["error"]}
        job = _jobs[company] = {"state": "running", "stage": "starting", "at": time.time(), "error": ""}

    def go():
        try:
            r = build(client, company, latest_fn, progress=lambda st: job.update(stage=st))
            if on_built:
                try:
                    r["register"] = on_built(company, r)
                except Exception:                                  # noqa: BLE001
                    traceback.print_exc()
            remember(company, r)
            job["state"] = "done"
        except Exception as exc:                                   # noqa: BLE001
            traceback.print_exc()
            job.update(state="failed", error=str(exc)[:300], at=time.time())
    threading.Thread(target=go, daemon=True, name=f"salesrec-{company}").start()
    return {"pending": True, "stage": "starting", "note": "Reading Simphony and Business Central…"}


# ---------------------------------------------------------------- the register
def findings(r):
    """The exceptions of a reconciliation for exreg.sync (source "sales_rec") and the days it covered."""
    out, ccy = [], r.get("currency") or ""
    sev = {"not_imported": "critical", "import_diff": "warning", "invoice_diff": "critical", "not_posted": "warning",
           "unpaid": "warning"}

    def money(v):
        return "–" if v is None else f"{v:,.0f}"
    for L in r["locations"]:
        for d, c in L["cells"].items():
            for k in c["issues"]:
                if k == "pending":
                    continue
                base = c["imp"] if c["imp"] is not None else c["sim"]
                if k == "not_imported":
                    value, title = c["sim"], f"{L['name']} {d}: sales not imported in BC"
                    detail = (f"Simphony {money(c['sim'])} {ccy}; nothing in the POS import of BC {c['age']} days "
                              f"later (lag {r['settings']['import_lag']} days).")
                elif k == "import_diff":
                    value, title = c["imp"] - c["sim"] + c["unmapped"], f"{L['name']} {d}: import differs from Simphony"
                    top = "; ".join(f"{x['mi']} {x['name'] or ''} {x['diff']:+,.0f}" for x in c["items"][:5])
                    detail = (f"Imported {money(c['imp'])} against Simphony {money(c['sim'])} {ccy} "
                              f"({c['imp'] - c['sim']:+,.0f}"
                              + (f", of which {c['unmapped']:,.0f} menu items never imported" if c["unmapped"] else "")
                              + f"); {c['items_n']} menu items differ" + (f": {top}." if top else "."))
                elif k == "not_posted":
                    value, title = base, f"{L['name']} {d}: sales not posted"
                    detail = (f"{c['unposted']} of {c['rows']} imported lines not in a sales document"
                              if c["unposted"] else "Imported, but no sales invoice of the customer "
                              f"{L['customer']} on {d}") + f", {c['age']} days later (lag {r['settings']['post_lag']} days)."
                elif k == "invoice_diff":
                    value, title = c["inv"] - (base or 0), f"{L['name']} {d}: invoice differs from the import"
                    detail = (f"Invoiced {money(c['inv'])} ({', '.join(c['docs'])}) against "
                              f"{'the import' if c['imp'] is not None else 'Simphony'} {money(base)} {ccy}.")
                else:
                    value, title = c["remaining"], f"{L['name']} {d}: takings not settled"
                    detail = (f"{money(c['remaining'])} {ccy} still open on {', '.join(c['docs'])} after {c['age']} "
                              f"days: the cash, card and online receipts of the day are not applied.")
                out.append({"subject": f"{d} {k}", "kind": k, "location": L["bc"], "title": title, "detail": detail,
                            "severity": "critical" if k == "import_diff" and base and abs(value) > 0.05 * abs(base)
                            else sev[k], "value": abs(value or 0)})
    for M in r["mapping"]:
        out.append({"subject": f"item {M['mi']}", "kind": M["kind"], "location": "",
                    "title": f"{M['mi']} {M['name']}: {M['label']}".strip(),
                    "detail": (f"BC item {M['item'] or '–'}; {M['qty']:g} sold for {M['value']:,.0f} {ccy} on "
                               f"{M['days']} days at {', '.join(M['locations'])}, last {M['last']}."),
                    "severity": "critical" if M["kind"] in ("no_item", "not_in_import") else "warning",
                    "value": abs(M["value"])})
    return out


def in_scope(r):
    """Which active register items a reconciliation may close: its days, and the item mapping."""
    days = set(r["days"])
    return lambda x: x["kind"] in MAPPING or (x["subject"] or "")[:10] in days
