"""RHG Purchasing Control Tower - local server.

Business Central is read-only. Actions, comments and thresholds created in the
app are stored locally under ./data and never written back to BC.

Run:  python server.py     then open http://127.0.0.1:8765
"""
import base64
import gzip
import io
import access
import hmac
import html
import json
from collections import defaultdict
import re
import sys
from concurrent.futures import ThreadPoolExecutor
import threading
import time
import traceback
import webbrowser
from calendar import monthrange
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse

import analytics
import xlsxpdf
import exportmail
import auth
import brief
import classify
import compliance
import executive
import notify
import savings
import savings_report
import negotiations
import pricechg
import purchasers
import weekly_report
import unicorn_facts
import contracts
import comparisons
import config
import localdb
import postings
import requisitions as requisition_view
import memuse
import stock
import currency
import exreg
import history
import invoices
import operations
import todayloc
import quality
import recipients
import reports
import smtp_config
import store
from bc_client import BCClient, BCError, PeriodCache

STATIC = config.BASE_DIR / "static"
STATIC_FILES = {"brand.css": "text/css; charset=utf-8", "logo.svg": "image/svg+xml",
                "logo.png": "image/png", "favicon.ico": "image/x-icon",
                "icon.svg": "image/svg+xml",
                "manifest.webmanifest": "application/manifest+json",
                "sw.js": "application/javascript; charset=utf-8",
                "select.js": "application/javascript; charset=utf-8",
                "recipients.js": "application/javascript; charset=utf-8",
                "select.css": "text/css; charset=utf-8",
                "tables.js": "application/javascript; charset=utf-8",
                "pulse.js": "application/javascript; charset=utf-8",
                "layout.js": "application/javascript; charset=utf-8",
                "login.html": "text/html; charset=utf-8"}
client = BCClient()
cache = PeriodCache(client)
_last = {}          # cached period rows for drill-down, per request signature
_last_lock = threading.Lock()


def company_currency(company, settings=None):
    """The currency a company posts in.

    Business Central keeps each company's amounts in that company's own currency, so
    RKS and Zoi figures are already euro. This only fixes the label; nothing is converted.
    """
    settings = settings or store.get_settings()
    block = settings.get("currencies") or {}
    mapping = {}
    for part in (block.get("map") or "").split(","):
        if "=" in part:
            k, _, v = part.partition("=")
            mapping[k.strip().lower()] = v.strip().upper()
    return mapping.get((company or "").lower(), (block.get("default") or "ALL").upper())


def _period(start, mode="week"):
    anchor = date.fromisoformat(start) if start else None
    return analytics.periods_for(anchor, mode)


def _month_chunks(c_from, c_to):
    out, cur = [], c_from
    while cur <= c_to:
        end = min(c_to, cur.replace(day=monthrange(cur.year, cur.month)[1]))
        out.append((cur, end))
        cur = end + timedelta(days=1)
    return out


def in_parallel(jobs, workers=6):
    """Runs independent Business Central reads at the same time.

    Each read is a separate HTTP conversation, so waiting for them one after another
    added up: a month and its comparison period are 40,000+ rows between them. Failures
    are returned, not raised, so one slow or missing source cannot take the screen down.
    """
    out = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {name: pool.submit(fn) for name, fn in jobs.items()}
        for name, fut in futures.items():
            try:
                out[name] = (fut.result(), None)
            except Exception as exc:                      # noqa: BLE001
                out[name] = (None, exc)
    return out


def period_rows(company, c_from, c_to, max_age, kind="value"):
    """Reads a date range from BC, split into monthly chunks for long periods.
    Settled months are cached for a day; only the current one is refreshed often."""
    today = date.today()
    chunks = _month_chunks(c_from, c_to) if (c_to - c_from).days > 31 else [(c_from, c_to)]
    rows, checked, new = [], 0.0, 0
    for a, b in chunks:
        age = max_age if b >= today - timedelta(days=1) else 86400
        item = cache.get(company, a.isoformat(), b.isoformat(), age, kind=kind)
        rows.extend(item["rows"])
        checked = max(checked, item["checked_at"])
        new += item.get("new_since_last", 0)
    return {"rows": rows, "checked_at": checked or time.time(), "new_since_last": new}


_dash_cache = {}
_dash_building = set()
_dash_lock = threading.Lock()


def dashboard(company, week, location, mode="week"):
    """Answers from what is already held and refreshes behind the screen.

    Waiting for Business Central is what made the app feel slow, not the figures
    themselves. The last good payload goes out at once, marked as refreshing, and the
    new one replaces it as soon as it arrives. The very first request for a period has
    nothing to show, so that one waits.
    """
    key = (company, week or "", location or "", mode)
    if not config.SERVE_WHILE_REFRESHING:
        return _dashboard_build(company, week, location, mode)
    with _dash_lock:
        held = _dash_cache.get(key)
        age = time.time() - held[0] if held else None
        fresh_for = max(config.REFRESH_SECONDS - 5, 5)
        if held and age < fresh_for:
            return held[1]
        building = key in _dash_building
        if held and not building:
            _dash_building.add(key)
            threading.Thread(target=_refresh_dashboard, args=(key, company, week, location, mode),
                             daemon=True).start()
    if held:
        payload = dict(held[1])
        payload["meta"] = {**payload["meta"], "serving": "held figures, refreshing now",
                           "held_seconds": int(age)}
        return payload
    out = _dashboard_build(company, week, location, mode)
    with _dash_lock:
        _dash_cache[key] = (time.time(), out)
    _trim_dash()
    return out


_tr_jobs, _tr_lock = {}, threading.Lock()


def transfers_request(company, period, refresh=False):
    """Supplier performance → the goods each location received from the other locations of the company in the
    period (operations.transfer_matrix), read in the background: {"pending": True} until it is ready."""
    if not period or len(period) != 2:
        return {"error": "No period."}
    key = (company, period[0], period[1])
    with _tr_lock:
        j = _tr_jobs.get(key)
        if j and j["state"] == "done" and not refresh and time.time() - j["at"] < 600:
            return j["value"]
        if j and j["state"] == "running":
            return {"pending": True}
        if j and j["state"] == "failed" and not refresh and time.time() - j["at"] < 60:
            return {"error": j["error"]}
        j = _tr_jobs[key] = {"state": "running", "at": time.time()}
        for k in [k for k, v in _tr_jobs.items() if time.time() - v["at"] > 3600]:
            _tr_jobs.pop(k, None)

    def go():
        try:
            start = (date.fromisoformat(period[0]) - timedelta(days=45)).isoformat()
            rows = client.transfer_entries(company, start, period[1], 0 if refresh else 600)
            out = operations.transfer_matrix(rows, period[0], period[1])
            try:
                catalog = client.item_catalog(company)
            except BCError:
                catalog = {}
            out["items"] = {}
            for r in out["rows"]:
                if r[2] not in out["items"]:
                    c = catalog.get(r[2]) or {}
                    out["items"][r[2]] = [c.get("desc") or "", c.get("code") or c.get("posting_group") or "", c.get("uom") or ""]
            j.update(state="done", at=time.time(), value=out)
        except Exception as exc:                                 # noqa: BLE001
            j.update(state="failed", at=time.time(), error=str(exc)[:300])
    threading.Thread(target=go, daemon=True, name="transfers").start()
    return {"pending": True}


def _trim_dash(keep=6):
    with _dash_lock:
        if len(_dash_cache) > keep:
            for k, _ in sorted(_dash_cache.items(), key=lambda kv: kv[1][0])[:len(_dash_cache) - keep]:
                _dash_cache.pop(k, None)


def _refresh_dashboard(key, company, week, location, mode):
    try:
        out = _dashboard_build(company, week, location, mode)
        with _dash_lock:
            _dash_cache[key] = (time.time(), out)
    except Exception as exc:                                     # noqa: BLE001
        print(f"[{datetime.now():%H:%M:%S}] background refresh failed: {exc}")
    finally:
        with _dash_lock:
            _dash_building.discard(key)


def _scope_of(location):
    """location: "" (every location), one code, or a frozenset of codes (a person or a report limited to some
    locations, who reads them all together): (the one code or "", the set in upper case or None)."""
    if isinstance(location, (frozenset, set)):
        return "", {str(x).strip().upper() for x in location}
    return location or "", None


def _in_scope(only, code):
    return only is None or str(code or "").strip().upper() in only


def _contract_above(rows, settings):
    """Purchasing charts → price variance: the items paid above the contracted price beyond the tolerance, every one
    counted (the screen lists the largest), and those paid below it."""
    tol = float(settings["thresholds"].get("price_above_contract_pct", 2) or 0)
    up = [r for r in rows if r["pct"] > tol]
    down = [r for r in rows if r["pct"] < -tol]
    return {"tolerance": tol, "compared": len(rows), "count": len(up), "value": round(sum(r["overpaid"] for r in up), 2),
            "below": len(down), "below_value": round(-sum(r["overpaid"] for r in down), 2),
            "items": [{k: r[k] for k in ("item_no", "desc", "paid", "contract", "pct", "overpaid", "qty", "vendor", "locations")}
                      for r in sorted(up, key=lambda r: -r["overpaid"])[:25]]}


def _dashboard_build(company, week, location, mode="week", remember=True):
    location, only = _scope_of(location)
    c_from, c_to, p_from, p_to, like_for_like, label = _period(week, mode)
    cur_age = max(config.REFRESH_SECONDS - 5, 5) if like_for_like else 600
    t0 = time.time()
    settings = store.get_settings()
    currency.set_current(company_currency(company, settings))

    first = in_parallel({
        "cur": lambda: period_rows(company, c_from, c_to, cur_age),
        "prev": lambda: period_rows(company, p_from, p_to, 600),
        "vcur": lambda: period_rows(company, c_from, c_to, cur_age, kind="vendor"),
        "vprev": lambda: period_rows(company, p_from, p_to, 600, kind="vendor"),
        "po": lambda: client.purchase_order_list(company),
        "prices": lambda: client.price_list(company),
        "requisitions": lambda: client.requisition_lines(company),
        "lines": lambda: client.purchase_lines(company),
    }, workers=8)
    if first["cur"][1]:
        raise first["cur"][1]
    cur = first["cur"][0]
    prev = first["prev"][0] if not first["prev"][1] else {"rows": [], "checked_at": time.time(),
                                                          "new_since_last": 0}
    if only is not None:                    # some locations only: everything below is built from their entries
        cur = dict(cur, rows=[r for r in cur["rows"] if _in_scope(only, r.get("Location_Code"))])
        prev = dict(prev, rows=[r for r in prev["rows"] if _in_scope(only, r.get("Location_Code"))])
    data = analytics.build(cur["rows"], prev["rows"], location or None)
    # Location intelligence (Overview): every location's purchase figures, on the all-locations read
    data["by_location"] = analytics.by_location(cur["rows"], prev["rows"]) if not location else None

    meta = {
        "company": company, "location": location or "", "scope": sorted(only) if only is not None else None,
        "current": [c_from.isoformat(), c_to.isoformat()],
        "prior": [p_from.isoformat(), p_to.isoformat()],
        "like_for_like": like_for_like, "mode": mode, "period_label": label,
        "currency": company_currency(company, settings),
        "currency_note": "Amounts are in the company's own posting currency, as Business Central "
                         "stores them; nothing is converted.",
        "checked_at": datetime.fromtimestamp(cur["checked_at"]).isoformat(timespec="seconds"),
        "new_since_last": cur.get("new_since_last", 0),
        "refresh_seconds": config.REFRESH_SECONDS,
        "expected_cost_included": config.INCLUDE_EXPECTED_COST,
        "late_days": settings["thresholds"]["invoice_posting_late_days"],
        "read_only": True,
    }

    ve_cur = [r for r in cur["rows"] if not location or r.get("Location_Code") == location]
    inv_data, ops = None, None
    try:
        if first["vcur"][1]:
            raise first["vcur"][1]
        vcur = first["vcur"][0]
        vprev = first["vprev"][0] if not first["vprev"][1] else {"rows": []}
        if only is not None:                # the supplier documents of those locations' entries only
            dn = {str(r.get("Document_No")) for r in cur["rows"]}
            dp = {str(r.get("Document_No")) for r in prev["rows"]}
            vcur = dict(vcur, rows=[r for r in vcur["rows"] if str(r.get("Document_No")) in dn])
            vprev = dict(vprev, rows=[r for r in vprev["rows"] if str(r.get("Document_No")) in dp])
        registers, reg_status = None, None
        entries = [r.get("Entry_No") or 0 for r in cur["rows"]]
        if entries:
            try:
                registers, reg_status = client.registers_for(company, min(entries), max(entries))
            except BCError as exc:
                reg_status = {"error": str(exc)}
        else:
            reg_status = client.register_fields(company)
            registers = [] if reg_status and "error" not in reg_status else None
        inv_data = invoices.build(vcur["rows"], vprev["rows"], cur["rows"], prev["rows"],
                                  registers, reg_status, location or None)

        suppliers = operations.supplier_index(vcur["rows"])
        lookup = None
        if registers:
            starts = [r["from"] for r in registers]
            from bisect import bisect_right

            def lookup(entry):
                i = bisect_right(starts, entry) - 1
                if i >= 0 and registers[i]["from"] <= entry <= registers[i]["to"]:
                    return registers[i]["user"]
                return ""
        docs = operations.documents(ve_cur, suppliers, lookup)
        try:
            po_rows = first["po"][0] if not first["po"][1] else []
        except BCError as exc:
            po_rows, po_error = [], str(exc)
        else:
            po_error = None
        pos = operations.purchase_orders(po_rows, settings)
        if only is not None:
            pos = [p for p in pos if _in_scope(only, p.get("location"))]
        if data.get("by_location") is not None:
            # suppliers and open orders of every location, for the location squares
            from collections import defaultdict as _dd
            # Only invoice and credit-memo lines name a supplier (receipt lines carry the
            # receipt number, which the vendor ledger does not know), and on those the
            # actual cost is the purchase: the expected cost there is the receipt's reversal.
            loc_sup = _dd(lambda: _dd(float))
            for r in ve_cur:
                if r.get("Document_Type") not in (operations.INV, operations.CM):
                    continue
                sup = (suppliers.get(str(r.get("Document_No"))) or {}).get("vendor")
                if not sup:
                    continue
                loc_sup[r.get("Location_Code") or "?"][sup] += float(r.get("Cost_Amount_Actual") or 0)
            for loc, sups in loc_sup.items():
                tot = sum(v for v in sups.values() if v > 0)
                best = max(sups.items(), key=lambda kv: kv[1]) if sups else ("", 0)
                rec = data["by_location"].setdefault(loc, {"cur": 0, "prev": 0})
                rec["suppliers"] = sum(1 for v in sups.values() if v > 0)
                rec["top_supplier"] = [best[0], best[1], (best[1] / tot * 100) if tot else None]
            for p in pos:
                rec = data["by_location"].get(p["location"] or "?")
                if rec is None:
                    continue
                o = rec.setdefault("orders", {"open": 0, "value": 0.0, "awaiting": 0.0, "awaiting_orders": 0, "stale": 0})
                o["open"] += 1
                o["value"] += p.get("amount") or 0
                o["awaiting"] += p["received_not_invoiced"]
                o["awaiting_orders"] += 1 if p["received_not_invoiced"] > 0 else 0
                o["stale"] += 1 if p.get("flag") else 0
        if location:
            pos = [p for p in pos if p["location"] == location]
        order_hist = history.track_orders(company, pos)
        for p in pos:
            h = order_hist.get(p["no"], {})
            p["hours_in_status"] = h.get("hours_in_status")
            p["released_after_hours"] = h.get("released_after_hours")
            p["first_seen"] = h.get("first_seen")

        # ---- requisitions (only if the worksheet is published) ----
        if first["requisitions"][1]:
            req_rows, req_status = None, {"error": str(first["requisitions"][1])[:300]}
        else:
            req_rows, req_status = first["requisitions"][0]
        data["postings"] = postings.build(ve_cur, settings, label)
        data["requisitions"] = (requisition_view.build(req_rows, settings) if req_rows is not None
                                else {"unavailable": req_status})
        if req_rows is not None:
            # the planning leftovers are not requisitions, so they must not reach the
            # events feed, the exception list or the counters either
            req_rows, _leftovers = requisition_view.split(req_rows)
        reqs, closed_reqs, req_exceptions = [], [], []
        if req_rows is not None:
            if location:
                req_rows = [r for r in req_rows if r["location"] == location]
            if only is not None:
                req_rows = [r for r in req_rows if _in_scope(only, r.get("location"))]
            # a read of some locations closes only their vanished lines, never those of the others
            req_hist, closed_reqs = history.track_requisitions(
                company, req_rows, {location.upper()} if location else only)
            reqs = operations.requisitions(req_rows, req_hist, settings)
            req_exceptions = operations.requisition_exceptions(reqs, settings)

        prices, price_status = (first["prices"][0] if not first["prices"][1]
                                else (None, {"available": False, "reason": str(first["prices"][1])[:300]}))
        item_avg = operations.item_averages(ve_cur)
        contract_rows, contract_exceptions = operations.contract_exceptions(item_avg, prices, settings)
        try:
            catalog = client.item_catalog(company)
        except BCError:
            catalog = {}
        data["contracts"] = contracts.build(ve_cur, prices or {}, price_status, catalog, settings, label)
        if data.get("by_location") is not None:
            # Supplier performance → Location intelligence / Whole company (sent by /api/suppliers/locations only)
            data["_supplier_matrix"] = operations.supplier_matrix(
                ve_cur, suppliers, prev["rows"], operations.supplier_index(vprev["rows"]), catalog)

        sla = (operations.sla_exceptions(docs, pos, data["movers"], settings, meta)
               + req_exceptions + contract_exceptions)
        rank = {"critical": 0, "warning": 1, "info": 2}
        sla.sort(key=lambda e: (rank.get(e["severity"], 3), -abs(e.get("impact") or 0)))
        if data.get("by_location") is not None:
            for e in sla:
                for loc in {x.strip() for x in str(e.get("location") or "").split(",") if x.strip()}:
                    rec = data["by_location"].get(loc)
                    if rec is not None:
                        x = rec.setdefault("exceptions", {"critical": 0, "warning": 0, "info": 0})
                        x[e.get("severity") if e.get("severity") in x else "info"] += 1
        sup_all, sup_total = operations.supplier_summary(docs, settings, limit=None)
        sup_rows = sup_all[:20]
        ops = {
            "events": operations.events(docs, pos, reqs=reqs, closed_reqs=closed_reqs, order_hist=order_hist),
            "purchase_orders": pos[:60],
            "requisitions": reqs, "closed_requisitions": closed_reqs,
            "requisition_status": ({"available": True, "open": len(reqs),
                                    "note": (req_status or {}).get("_note", ""),
                                    "fields": (req_status or {}).get("_fields", [])}
                                   if req_rows is not None else
                                   {"available": False, "reason": (req_status or {}).get("error", "")}),
            "history": history.stats(),
            "contract_prices": contract_rows[:40], "price_status": price_status,
            "contract_above": _contract_above(contract_rows, settings),
            "po_error": po_error, "sla": sla, "suppliers": sup_rows, "supplier_total": sup_total,
            # Purchasing charts → Pareto: every supplier's invoiced spend, largest first (name, spend)
            "supplier_pareto": {"rows": [[r["supplier"], round(r["spend"], 2)] for r in sup_all if r["spend"] > 0][:60],
                                "count": sum(1 for r in sup_all if r["spend"] > 0),
                                "total": round(sum(r["spend"] for r in sup_all if r["spend"] > 0), 2)},
            "pending_invoice_value": sum(p["received_not_invoiced"] for p in pos),
            "open_orders": len(pos),
            # totals are counted on every order, not on the sixty carried to the screen
            "po_summary": {
                "orders": len(pos),
                "released": sum(1 for p in pos if p.get("status") == "Released"),
                "open_status": sum(1 for p in pos if p.get("status") == "Open"),
                "awaiting_invoice": sum(p["received_not_invoiced"] for p in pos),
                "awaiting_invoice_orders": sum(1 for p in pos if p["received_not_invoiced"] > 0),
                "value": sum(p.get("amount") or 0 for p in pos),
                "shown": min(len(pos), 60),
                "note": "Every open purchase order Business Central holds today, whatever its date. An "
                        "order raised in August and still open counts here, so this figure is larger than "
                        "an extract filtered to one month.",
            },
            "documents_total": len(docs),
            "documents_shown": min(len(docs), 800),
            "documents": sorted(
                [{"no": d["no"], "type": d["type"], "supplier": d["supplier"], "user": d["user"],
                  "posting": d["posting"], "document_date": d["document_date"], "lines": d["lines"],
                  "value": d["value"], "locations": sorted(d["locations"]), "neg_lines": d["neg_lines"],
                  "zero_lines": d["zero_lines"]} for d in docs.values()],
                key=lambda d: d["posting"] or "", reverse=True)[:800],
        }
        data["invoices"] = inv_data

        prev_items = operations.item_averages(prev["rows"] if not location else
                                              [r for r in prev["rows"] if r.get("Location_Code") == location])
        free_text = []
        for l in (first["lines"][0] or []) if not first["lines"][1] else []:
            if only is not None and not _in_scope(only, l.get("Location_Code")):
                continue
            if l.get("Document_Type") == "Order" and not (l.get("No_") or "").strip() and (l.get("Description") or "").strip():
                free_text.append({"Description": (l.get("Description") or "").strip(),
                                  "Quantity": l.get("Quantity"), "Location": l.get("Location_Code") or "",
                                  "Unit of measure": l.get("Unit_of_Measure_Code") or "",
                                  "Order date": l.get("Order_Date")})
        data["quality"] = quality.build(ve_cur, vcur["rows"], docs, prev_items, item_avg, free_text)
    except BCError as exc:
        print(f"[{datetime.now():%H:%M:%S}] Operations data unavailable: {exc}", file=sys.stderr)
        data["invoices"] = {"error": str(exc)}
        data["quality"] = {"checks": [], "totals": {"findings": 0}, "error": str(exc)}
        ops = {"error": str(exc), "events": [], "purchase_orders": [], "sla": [], "suppliers": [],
               "pending_invoice_value": 0, "open_orders": 0, "documents": [], "documents_total": 0,
               "documents_shown": 0, "requisitions": [],
               "closed_requisitions": [], "requisition_status": {"available": False, "reason": str(exc)}}

    data["operations"] = ops
    gaps = list(operations.DATA_GAPS)
    if (ops.get("requisition_status") or {}).get("available"):
        gaps = [g for g in gaps if g is not operations.REQUISITION_GAP]
    if (ops.get("price_status") or {}).get("available"):
        gaps = [g for g in gaps if g is not operations.CONTRACT_PRICE_GAP]
    data["gaps"] = gaps
    data["settings"] = settings
    # the Exceptions register: what it holds of the company (found by the daily run or tracked by hand)
    week_ago = (date.today() - timedelta(days=7)).isoformat()
    reg_all = [x for x in exreg.query("all", company=company, limit=100000)["items"]
               if (only is None or _in_scope(only, x.get("location")))
               and (x["status"] in exreg.ACTIVE or (x["closed_at"] or "") >= week_ago)]
    reg = [x for x in reg_all if x["status"] in exreg.ACTIVE]
    data["register"] = {x["subject"]: {"id": x["id"], "status": x["status"], "owner": x["owner"], "due": x["due"],
                                       "source": x["source"], "overdue": x["overdue"]} for x in reg}
    tracked = set(data["register"])
    # the reports read the register as they read the Action center (title, owner, priority, due, status)
    pri = {"critical": "Critical", "warning": "High", "info": "Low"}
    data["actions"] = [{"title": x["title"], "owner": x["owner"], "priority": pri.get(x["severity"], "Medium"),
                        "due_date": x["due"], "source_exception": x["subject"], "id": x["id"],
                        "status": "Overdue" if x["overdue"] else {"Closed": "Resolved"}.get(x["status"], x["status"])}
                       for x in reg_all]
    untracked = [e for e in (ops.get("sla") or []) if e["code"] not in tracked]
    data["untracked"] = {
        "critical": [e for e in untracked if e["severity"] == "critical"][:40],
        "warning": [e for e in untracked if e["severity"] == "warning"][:40],
        "counts": {"critical": sum(1 for e in untracked if e["severity"] == "critical"),
                   "warning": sum(1 for e in untracked if e["severity"] == "warning"),
                   "impact": sum(abs(e.get("impact") or 0) for e in untracked
                                 if e["severity"] in ("critical", "warning"))},
        "note": "Exceptions of the selected period that the Exceptions register does not hold yet: the daily run "
                "reads the month in progress, so another period, or what appeared since this morning, is not in it.",
    }
    data["action_keys"] = sorted(tracked)
    if "error" not in ops:
        data["brief"] = brief.build(data, inv_data, ops, settings, meta, len(operations.DATA_GAPS))
    meta["query_ms"] = int((time.time() - t0) * 1000)
    data["meta"] = meta

    if only is not None or not remember:    # the drill-down (and the daily run) keep the read of every location
        return data
    with _last_lock:
        _last["rows"] = ve_cur
        _last["prev"] = prev["rows"]
        _last["data"] = {k: data.get(k) for k in ("movers", "spreads", "top_spend", "invoices",
                                                  "operations", "meta", "quality")}
    return data


_stock_cache = {}


def _month_bounds(month):
    """'2026-08' -> (2026-07-31, 2026-08-01, 2026-08-31). The day before the month is the
    opening balance date, because BC answers 'stock up to and including' this date."""
    year, mon = int(month[:4]), int(month[5:7])
    start = date(year, mon, 1)
    end = date(year + (mon == 12), (mon % 12) + 1, 1) - timedelta(days=1)
    return start - timedelta(days=1), start, end


def stock_screen(company, location, month, mode="bridge", header_period=None):
    """The monthly stock bridge for one location, reconciled against Business Central."""
    key = (company, location or "", month, mode)
    hit = _stock_cache.get(key)
    if hit and time.time() - hit[0] < 900:
        return hit[1]
    before, start, end = _month_bounds(month)
    settings = store.get_settings()
    currency.set_current(company_currency(company, settings))
    sources, t0 = [], time.time()
    settled = end < date.today()
    age = 86400 if settled else 900
    reads = in_parallel({
        "opening": lambda: client.inventory_at(company, location, before.isoformat(), age),
        "closing": lambda: client.inventory_at(company, location, end.isoformat(), age),
        # the raw movements are read once and turned into the bridge; they are not kept,
        # because a month of one location is 40,000 rows and the bridge is a few hundred
        "ile": lambda: client.item_ledger(company, location, start.isoformat(), end.isoformat(), 0),
        "sku": lambda: client.stockkeeping_units(company, location),
    }, workers=4)
    for name, label in (("opening", f"Stock at {before.isoformat()}"),
                        ("closing", f"Stock at {end.isoformat()}"),
                        ("ile", "Item ledger entries for the month")):
        value, err = reads[name]
        sources.append({"source": label, "status": "unavailable" if err else "verified",
                        "reason": str(err)[:300] if err else "",
                        "rows": (len(value[0]) if name != "ile" and value else
                                 len(value) if value else 0)})
    if any(reads[n][1] for n in reads if n != "sku"):
        out = {"available": False, "sources": sources, "month": month, "location": location,
               "error": "Data unavailable or not verified in Business Central."}
        _stock_cache[key] = (time.time(), out)
        return out

    opening, cards_open = reads["opening"][0]
    closing, cards_close = reads["closing"][0]
    cards = {**cards_open, **cards_close}
    ile = reads["ile"][0]
    expected_ile = client.count(company, config.BC_ILE_ENTITY,
                                f"Posting_Date ge {start.isoformat()} and Posting_Date le {end.isoformat()}"
                                + (f" and Location_Code eq '{location}'" if location else ""))

    skus, sku_status = reads["sku"][0] if not reads["sku"][1] else ({}, {
        "available": False, "reason": str(reads["sku"][1])[:300]})
    rows = stock.build(ile, opening, closing, cards, skus, location or "")
    cadence = stock.parse_cadence((settings.get("stock") or {}).get("cadence"))
    policy = stock.policy_for(location or "*", cadence)
    totals = stock.summary(rows, sku_status, location or "", month)
    exceptions = stock.exceptions(rows, settings, location or "all locations", policy)
    categories = stock.category_view(rows, policy)
    why = stock.causes(rows, totals, categories, policy, sku_status)
    this_week = stock.decisions(rows, categories, policy, location or "all locations", settings)
    out = {
        "available": True, "company": company, "location": location or "", "month": month,
        "period": [start.isoformat(), end.isoformat()], "opening_date": before.isoformat(),
        "currency": currency.code(), "settled": settled,
        "rows": rows[:400], "row_count": len(rows), "totals": totals, "exceptions": exceptions[:80],
        "sku_status": sku_status, "proposed": stock.proposed_parameters(rows, policy),
        "policy": policy, "categories": categories, "causes": why, "decisions": this_week,
        "header_period": header_period or "",
        "period_mismatch": bool(header_period and not header_period.startswith(month)),
        "sources": sources, "ms": int((time.time() - t0) * 1000),
        "completeness": {"ile_rows_read": len(ile), "ile_rows_expected": expected_ile,
                         "complete": expected_ile is None or len(ile) >= expected_ile},
        "provenance": {
            "datasets": [f"{config.BC_ILE_ENTITY} (posted movements of the month)",
                         f"{config.BC_ITEMS_ENTITY} InventorybyDate with Date_Filter '..date' "
                         f"(opening and closing balance)"],
            "method": "Opening plus receipts less returns, plus transfers in less transfers out, plus "
                      "assembly output less assembly consumption, less sales and other consumption, plus "
                      "and less inventory adjustments. The result is compared with the balance Business "
                      "Central itself reports at the month end.",
            "valuation": "Quantities are posted movements. Values are quantity times the item card's unit "
                         "cost, which is a management valuation, not the posted inventory value in the "
                         "general ledger.",
            "unavailable": [
                "Variant codes: not published on the item ledger, so a bridge line covers all variants.",
                ("Reorder point and maximum inventory: read from the published stockkeeping unit cards. "
                 f"{sku_status.get('with_maximum', 0):,} of {sku_status.get('cards', 0):,} cards carry a "
                 f"maximum and {sku_status.get('with_reorder_point', 0):,} a reorder point, so the check "
                 f"applies only to those items."
                 if sku_status.get("available") else
                 "Reorder point and maximum inventory: the stockkeeping unit service could not be read."),
                "Supplier lead time: not published, so a reorder point is measured against usage only.",
                "Physical inventory ledger entries: not published, so a count is visible only as the "
                "adjustment it produced.",
                "Expiry: only where the entry carries a lot with an expiration date.",
            ],
        },
    }
    _stock_cache[key] = (time.time(), out)
    if len(_stock_cache) > 4:          # each entry is a whole month of one location
        for k, _ in sorted(_stock_cache.items(), key=lambda kv: kv[1][0])[:2]:
            _stock_cache.pop(k, None)
    return out


def stock_overview(company, month):
    """Stock value by location at the month end against the month before. Read from the
    item cards, so it needs no movement history and stays quick."""
    before, start, end = _month_bounds(month)
    settings = store.get_settings()
    currency.set_current(company_currency(company, settings))
    locations = [l["code"] for l in client.locations(company)]
    settled = end < date.today()
    age = 86400 if settled else 900
    jobs = {}
    for loc in locations:
        jobs[f"end::{loc}"] = (lambda l=loc: client.inventory_at(company, l, end.isoformat(), age))
        jobs[f"start::{loc}"] = (lambda l=loc: client.inventory_at(company, l, before.isoformat(), age))
    got = in_parallel(jobs, workers=6)
    rows, failed = [], []
    for loc in locations:
        e, e_err = got[f"end::{loc}"]
        s_, s_err = got[f"start::{loc}"]
        if e_err or s_err:
            failed.append(loc)
            continue
        bal_end, cards = e
        bal_start, _ = s_
        value_end = sum(q * (cards.get(i, {}).get("unit_cost") or 0) for i, q in bal_end.items())
        value_start = sum(q * (cards.get(i, {}).get("unit_cost") or 0) for i, q in bal_start.items())
        rows.append({"location": loc, "items": len(bal_end), "value": value_end,
                     "previous": value_start, "change": value_end - value_start,
                     "negative_items": sum(1 for q in bal_end.values() if q < 0)})
    rows.sort(key=lambda r: -r["value"])
    return {"company": company, "month": month, "currency": currency.code(), "rows": rows,
            "unavailable": failed, "settled": settled,
            "total": sum(r["value"] for r in rows), "previous": sum(r["previous"] for r in rows),
            "note": "Value is quantity on hand at the month end times the item card's unit cost. It is a "
                    "management figure, not the posted inventory value in the general ledger; the finance "
                    "tower shows the accounting value."}


_compliance_cache = {}


def compliance_screen(company, month, months_back=6):
    """Invoices posted without a purchase order, for one month and the months before it."""
    key = (company, month)
    hit = _compliance_cache.get(key)
    if hit and time.time() - hit[0] < 900:
        return hit[1]
    before, start, end = _month_bounds(month)
    settings = store.get_settings()
    currency.set_current(company_currency(company, settings))
    t0 = time.time()
    try:
        headers = client.posted_invoices(company, start.isoformat(), end.isoformat())
    except BCError as exc:
        return {"available": False,
                "error": "Data unavailable or not verified in Business Central.",
                "reason": str(exc)[:300]}
    docs = sorted(str(h.get("No")) for h in headers if h.get("No"))
    blank_lines = []
    if docs:
        try:
            blank_lines = client.invoice_lines_without_order(company, docs[0], docs[-1])
            blank_lines = [l for l in blank_lines if str(l.get("Document_No")) in set(docs)]
        except BCError:
            blank_lines = None            # the lines could not be read; partial cases unknown
    out = compliance.build(headers, blank_lines or [], settings, month)
    out["available"] = True
    out["company"] = company
    out["lines_read"] = len(blank_lines) if blank_lines is not None else None
    out["lines_note"] = ("Only the lines carrying no order number are read, within the block of document "
                         "numbers this month used."
                         if blank_lines is not None else
                         "The invoice lines could not be read, so partly linked invoices are not detected "
                         "in this period.")

    # the months before, so the trend is real rather than a single reading
    trend_jobs = {}
    cursor = start
    for _ in range(months_back):
        cursor = (cursor - timedelta(days=1)).replace(day=1)
        m_before, m_start, m_end = _month_bounds(cursor.strftime("%Y-%m"))
        trend_jobs[cursor.strftime("%Y-%m")] = (
            lambda a=m_start, b=m_end: client.posted_invoices(company, a.isoformat(), b.isoformat(), 86400))
    trend = [{"month": month, "rate": out["kpis"]["rate"], "invoices": out["kpis"]["invoices"],
              "breaches": out["kpis"]["no_order"] + out["kpis"]["partial"],
              "value": out["kpis"]["value_breach"]}]
    for name, (rows, err) in in_parallel(trend_jobs, workers=3).items():
        if err or rows is None:
            trend.append({"month": name, "rate": None, "invoices": None, "breaches": None, "value": None})
            continue
        live = [h for h in rows if not (h.get("Cancelled") or h.get("Corrective"))]
        breaches = [h for h in live if not str(h.get("Order_No") or "").strip()]
        trend.append({"month": name, "invoices": len(live), "breaches": len(breaches),
                      "rate": len(breaches) / len(live) * 100 if live else 0,
                      "value": sum(float(h.get("Amount") or 0) for h in breaches),
                      "note": "Header order number only; partly linked invoices are not counted here."})
    out["trend"] = sorted(trend, key=lambda t: t["month"])
    out["ms"] = int((time.time() - t0) * 1000)
    _compliance_cache[key] = (time.time(), out)
    if len(_compliance_cache) > 4:
        for k, _ in sorted(_compliance_cache.items(), key=lambda kv: kv[1][0])[:2]:
            _compliance_cache.pop(k, None)
    return out


def nopo_window(company, date_from, date_to):
    """Invoices without an order over any window of days, for the daily email.

    The same proof as the Invoices without PO screen (compliance.build), without the
    monthly trend, and read fresh: the email goes out once a day and must not rely on
    a figure held from the evening before."""
    settings = store.get_settings()
    currency.set_current(company_currency(company, settings))
    try:
        headers = client.posted_invoices(company, date_from.isoformat(), date_to.isoformat(), 60)
    except BCError as exc:
        return {"available": False, "error": "Data unavailable or not verified in Business Central.",
                "reason": str(exc)[:300]}
    docs = sorted(str(h.get("No")) for h in headers if h.get("No"))
    blank_lines = []
    if docs:
        try:
            wanted = set(docs)
            blank_lines = [l for l in client.invoice_lines_without_order(company, docs[0], docs[-1], 60)
                           if str(l.get("Document_No")) in wanted]
        except BCError:
            blank_lines = []
    out = compliance.build(headers, blank_lines, settings,
                           f"{date_from.isoformat()} to {date_to.isoformat()}")
    out["available"] = True
    return out


notifier = notify.Notifier(nopo_window, config.BC_COMPANY)
weekly = weekly_report.Weekly(lambda company, week, location="": _dashboard_build(company, week, location or "", "week"), client,
                              lambda: notifier.companies(), company_currency)


_streams_cache = {}


_streams_lock, _streams_reading = threading.Lock(), set()


def streams(company, week, location, mode="week", _fresh=False):
    """Procurement split into its streams: food and beverage, non-food, supplies,
    services and CAPEX. Read on demand, because it needs the item catalogue and the
    purchase postings in the general ledger as well as the value entries."""
    key = (company, week or "", location or "", mode)
    hit = _streams_cache.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    if hit and not _fresh:                   # older: the last one at once, a new read behind it
        with _streams_lock:
            start = key not in _streams_reading
            _streams_reading.add(key)
        if start:
            def again():
                try:
                    streams(company, week, location, mode, _fresh=True)
                except Exception:                                   # noqa: BLE001
                    traceback.print_exc()
                finally:
                    with _streams_lock:
                        _streams_reading.discard(key)
            threading.Thread(target=again, daemon=True, name="streams-refresh").start()
        return dict(hit[1], reading=True)
    location, only = _scope_of(location)
    c_from, c_to, p_from, p_to, like_for_like, label = _period(week, mode)
    settings = store.get_settings()
    sources = []

    def _read(name, call, fallback, critical=True):
        t = time.time()
        try:
            value = call()
            sources.append({"source": name, "status": "verified", "ms": int((time.time() - t) * 1000)})
            return value
        except BCError as exc:
            sources.append({"source": name, "status": "unavailable", "critical": critical,
                            "ms": int((time.time() - t) * 1000), "reason": str(exc)[:300]})
            return fallback

    ve_all = period_rows(company, c_from, c_to, 120)["rows"]
    ve = [r for r in ve_all if r.get("Location_Code") == location] if location else ve_all
    if only is not None:
        ve = [r for r in ve if _in_scope(only, r.get("Location_Code"))]
    ve_flt = (f"Posting_Date ge {c_from.isoformat()} and Posting_Date le {c_to.isoformat()}"
              + (f" and {config.BC_PURCHASE_FILTER}" if config.BC_PURCHASE_FILTER else ""))
    ve_expected = client.count(company, config.BC_ENTITY, ve_flt)
    ve_complete = ve_expected is None or len(ve_all) >= ve_expected
    sources.append({"source": "Value entries (item purchases)",
                    "status": "verified" if ve_complete else "partial",
                    "rows": len(ve_all), "expected": ve_expected,
                    "reason": ("" if ve_complete else
                               f"Business Central counts {ve_expected} rows but only {len(ve_all)} were "
                               f"returned, so this period is incomplete.")})

    catalog = _read("Item cards (category codes)", lambda: client.item_catalog(company), {})
    accounts = _read("Chart of accounts", lambda: client.chart_of_accounts(company), {})
    cl = settings["classification"]
    service_map = {}
    for part in (cl.get("service_accounts") or "").split(","):
        if "=" in part:
            k, _, v = part.partition("=")
            if v.strip() in classify.TYPES:
                service_map[k.strip()] = v.strip()
    capex_accounts = [a.strip() for a in (cl.get("capex_accounts") or "").split(",") if a.strip()] \
        or classify.DEFAULT_CAPEX_ACCOUNTS
    rules = {"capex_accounts": capex_accounts, "service_map": service_map,
             "goods_prefixes": (cl.get("goods_prefixes") or "").strip() or classify.DEFAULT_GOODS_PREFIXES,
             "service_prefix": (cl.get("service_prefix") or "").strip() or classify.DEFAULT_SERVICE_PREFIX,
             "prepayment_prefix": (cl.get("prepayment_prefix") or "").strip() or classify.DEFAULT_PREPAYMENT_PREFIX}
    wanted_accounts = classify.accounts_to_read(accounts, rules)
    gl = _read("Purchase postings in the general ledger",
               lambda: client.purchase_gl_entries(company, c_from.isoformat(), c_to.isoformat(),
                                                  wanted_accounts), [])
    gl_expected = client.count(
        company, config.BC_GL_ENTITY,
        f"Posting_Date ge {c_from.isoformat()} and Posting_Date le {c_to.isoformat()} "
        f"and Source_Code eq '{config.BC_PURCHASE_SOURCE_CODE}'")
    # the account-by-account read is deliberately narrower than the full purchase sweep,
    # so a smaller number here is expected; it is reported, not treated as a loss
    gl_source = next((x for x in sources if x["source"].startswith("Purchase postings")), None)
    if gl_source:
        gl_source["rows"] = len(gl)
        gl_source["all_purchase_postings"] = gl_expected
    vendor_rows = _read("Vendor ledger entries",
                        lambda: cache.get(company, c_from.isoformat(), c_to.isoformat(), 300,
                                          kind="vendor")["rows"], [])
    suppliers_by_doc = {str(r.get("Document_No")): {"vendor": r.get("Vendor_Name"),
                                                    "vendor_no": r.get("Vendor_No")}
                        for r in vendor_rows}

    maps = classify.parse_maps(cl)
    # the posted invoices name the location (else the department) of a posting in the general ledger
    heads = _read("Posted purchase invoices", lambda: client.posted_invoices(company, c_from.isoformat(),
                                                                             c_to.isoformat()), [], critical=False)
    head_loc = {str(h.get("No") or ""): (h.get("Location_Code") or h.get("Shortcut_Dimension_1_Code") or "").strip()
                for h in heads}
    if only is not None:                    # some locations only: their postings and invoices
        gl = [g for g in gl if _in_scope(only, head_loc.get(str(g.get("Document_No") or ""), ""))]
        heads = [h for h in heads if _in_scope(only, head_loc.get(str(h.get("No") or ""), ""))]

    item_streams, item_rows, unclassified, by_field = classify.item_types(ve, catalog, maps)
    gl_streams, gl_rows, skipped, unknown_accounts, goods = classify.gl_types(
        gl, accounts, suppliers_by_doc, rules)
    summary = classify.summarize(item_streams, gl_streams)
    exceptions = classify.service_exceptions(gl_rows, settings, suppliers_by_doc)

    unclassified_rows = sorted(({"item": k, **v} for k, v in unclassified.items()),
                               key=lambda r: -r["value"])
    services = [r for r in gl_rows if r["type"] not in ("CAPEX",)]
    capex = [r for r in gl_rows if r["type"] == "CAPEX"]
    gl_source = next((x for x in sources if x["source"].startswith("Purchase postings")), {})

    # ---- Buying → Location intelligence: everything above by location. Item lines carry their location;
    # postings in the general ledger take the location (else the department) of their posted invoice.
    by_loc = {}

    def bl(code):
        return by_loc.setdefault(code or "", {"types": {}, "item_value": 0.0, "services": 0.0, "capex": 0.0,
                                              "prepay": 0.0, "svc_accounts": {}, "capex_docs": [], "invoices": 0,
                                              "invoiced": 0.0, "no_po": 0, "no_po_value": 0.0, "credit_memos": 0,
                                              "suppliers": {}, "nopo_docs": []})
    # the detail behind each purchase type of a location: by category (group 3 of the item card, else group 4) and
    # by item; postings in the general ledger by account and by document
    det = {}

    def dt(code, ptype):
        return det.setdefault((code or "", ptype), {"cats": {}, "items": {}, "lines": 0})
    for r in item_rows:
        L = bl(r["location"])
        L["types"][r["type"]] = L["types"].get(r["type"], 0.0) + r["value"]
        L["item_value"] += r["value"]
        D = dt(r["location"], r["type"])
        cat = (r["group3"] or r["group4"] or "").strip()
        c = D["cats"].setdefault(cat or "(no category 3 or 4)", {"value": 0.0, "items": set(), "level": "3" if r["group3"] else "4" if r["group4"] else ""})
        c["value"] += r["value"]
        c["items"].add(r["item"])
        it = D["items"].setdefault(r["item"] or r["description"], {"no": r["item"], "desc": r["description"], "cat": cat,
                                                                   "qty": 0.0, "value": 0.0, "lines": 0, "docs": set(),
                                                                   "sup": defaultdict(float), "last": ""})
        it["qty"] += r["quantity"]
        it["value"] += r["value"]
        it["lines"] += 1
        it["docs"].add(r["document"])
        it["last"] = max(it["last"], str(r["posting"] or "")[:10])
        sup = (suppliers_by_doc.get(str(r["document"])) or {}).get("vendor")
        if sup:
            it["sup"][sup] += r["value"]
        D["lines"] += 1
    for r in gl_rows:
        D = dt(head_loc.get(str(r["document"]), ""), r["type"])
        acc = f"{r['account']} {r['account_name']}".strip()
        c = D["cats"].setdefault(acc, {"value": 0.0, "items": set(), "level": "account"})
        c["value"] += r["value"]
        c["items"].add(r["document"])
        it = D["items"].setdefault(str(r["document"]), {"no": str(r["document"]), "desc": r["supplier"], "cat": acc, "qty": None,
                                                        "value": 0.0, "lines": 0, "docs": {r["document"]},
                                                        "sup": defaultdict(float), "last": ""})
        it["value"] += r["value"]
        it["lines"] += 1
        it["last"] = max(it["last"], str(r["posting"] or "")[:10])
        if r["supplier"]:
            it["sup"][r["supplier"]] += r["value"]
        D["lines"] += 1
    for (code, ptype), D in det.items():
        items = sorted(D["items"].values(), key=lambda x: -abs(x["value"]))
        bl(code).setdefault("detail", {})[ptype] = {
            "cats": sorted(([k, round(v["value"]), len(v["items"]), v["level"]] for k, v in D["cats"].items()), key=lambda x: -x[1]),
            "items": [{"no": x["no"], "desc": x["desc"], "cat": x["cat"], "qty": round(x["qty"], 2) if x["qty"] is not None else None,
                       "value": round(x["value"]), "lines": x["lines"], "docs": len(x["docs"]), "last": x["last"],
                       "sup": max(x["sup"].items(), key=lambda kv: kv[1])[0] if x["sup"] else ""} for x in items[:120]],
            "count": len(items), "lines": D["lines"]}
    for r in gl_rows:
        L = bl(head_loc.get(str(r["document"]), ""))
        if r["type"] == "CAPEX":
            L["capex"] += r["value"]
            if len(L["capex_docs"]) < 10:
                L["capex_docs"].append([r["document"], r["supplier"], f"{r['account']} {r['account_name']}", round(r["value"])])
        elif r["type"] == "Prepayments and advances":
            L["prepay"] += r["value"]
        elif r["type"] == "Unclassified":
            pass
        else:
            L["services"] += r["value"]
            k = f"{r['account']} {r['account_name']}"
            L["svc_accounts"][k] = L["svc_accounts"].get(k, 0.0) + r["value"]
        L["types"][r["type"]] = L["types"].get(r["type"], 0.0) + r["value"]
    for h in heads:
        if h.get("Cancelled"):
            continue
        L = bl((h.get("Location_Code") or h.get("Shortcut_Dimension_1_Code") or "").strip())
        amt = float(h.get("Amount") or 0)
        if h.get("Corrective") or amt < 0:
            L["credit_memos"] += 1
            continue
        L["invoices"] += 1
        L["invoiced"] += amt
        v = (h.get("Buy_from_Vendor_Name") or "").strip()
        L["suppliers"][v] = L["suppliers"].get(v, 0.0) + amt
        if not (h.get("Order_No") or "").strip():
            L["no_po"] += 1
            L["no_po_value"] += amt
            L["nopo_docs"].append([str(h.get("No") or ""), v, str(h.get("Posting_Date") or "")[:10], round(amt)])
    for L in by_loc.values():
        for k in ("item_value", "services", "capex", "prepay", "invoiced", "no_po_value"):
            L[k] = round(L[k])
        L["types"] = {k: round(v) for k, v in sorted(L["types"].items(), key=lambda kv: -kv[1]) if round(v)}
        L["svc_accounts"] = sorted(([k, round(v)] for k, v in L["svc_accounts"].items()), key=lambda kv: -kv[1])[:8]
        L["suppliers"] = sorted(([k, round(v)] for k, v in L["suppliers"].items()), key=lambda kv: -kv[1])[:6]
        L["nopo_docs"] = sorted(L["nopo_docs"], key=lambda x: -x[3])[:10]

    critical_missing = [x for x in sources if x["status"] == "unavailable" and x.get("critical")]
    out = {
        "by_location": by_loc,
        "period": [c_from.isoformat(), c_to.isoformat()], "label": label, "mode": mode,
        "company": company, "location": location or "", "scope": sorted(only) if only is not None else None,
        "summary": summary,
        "item_rows": item_rows[:600], "gl_rows": gl_rows[:600],
        "services": services[:400], "capex": capex[:200],
        "unclassified": unclassified_rows[:200],
        "unclassified_value": sum(r["value"] for r in unclassified_rows),
        "exceptions": exceptions, "sources": sources, "skipped": skipped,
        "classified_by": sorted(({"field": k, "value": v} for k, v in by_field.items()),
                                key=lambda x: -x["value"]),
        "unknown_accounts": unknown_accounts, "gl_rows_read": len(gl),
        "accounts_queried": len(wanted_accounts),
        "completeness": {
            "value_entries_read": len(ve_all), "value_entries_expected": ve_expected,
            "gl_rows_read": len(gl), "gl_purchase_postings_total": gl_expected,
            "note": "Row counts are checked against Business Central's own count. Value entries must "
                    "match exactly. The purchase postings are read only for the accounts a purchase can "
                    "land on, so that figure is smaller than the total by design: the rest are supplier, "
                    "VAT and other counter-entries.",
        },
        "goods_mirror": {**goods,
                         "item_stream": sum(v["value"] for k, v in item_streams.items()),
                         "difference": goods["total"] - sum(v["value"] for k, v in item_streams.items()),
                         "note": "These are the goods accounts in the general ledger. The same purchases "
                                 "already arrive through the value entries as items, so they are excluded "
                                 "from the streams to avoid counting the spend twice. The difference "
                                 "against the item stream is a reconciliation figure: it should be small, "
                                 "and is usually free-text purchase lines posted straight to a goods "
                                 "account without an item, or cost adjustments."},
        "gl_status": gl_source.get("status", "unknown"), "gl_reason": gl_source.get("reason", ""),
        "rules": {"capex_accounts": capex_accounts, "service_prefix": rules["service_prefix"],
                  "prepayment_prefix": rules["prepayment_prefix"],
                  "goods_prefixes": rules["goods_prefixes"]},
        "catalog_size": len(catalog), "accounts_size": len(accounts),
        "status": ("unavailable" if critical_missing else
                   "partial" if any(x["status"] == "unavailable" for x in sources) else "verified"),
        "provenance": {
            "environment": config.BC_BASE_URL, "company": company,
            "datasets": ["ValueEntries (Item_Ledger_Entry_Type eq 'Purchase')",
                         f"{config.BC_GL_ENTITY} (Source_Code eq '{config.BC_PURCHASE_SOURCE_CODE}', "
                         f"read account by account for {len(wanted_accounts)} accounts)",
                         config.BC_ITEMS_ENTITY, config.BC_ACCOUNTS_ENTITY, config.BC_VENDOR_ENTITY],
            "date_field": "Posting_Date", "date_range": f"{c_from.isoformat()} .. {c_to.isoformat()}",
            "filters": f"location={location or (', '.join(sorted(only)) if only is not None else 'all')}",
            "records": {"value_entries": len(ve), "gl_purchase_entries": len(gl),
                        "item_cards": len(catalog), "accounts": len(accounts)},
            "currency": company_currency(company),
            "vat": "Costs are ex-VAT: value entries hold cost, and the VAT lines of purchase postings "
                   "sit on liability accounts, which are excluded.",
            "credit_notes": "Credit notes reduce the stream they belong to, because their value entries "
                            "and postings are negative.",
            "uom": "Not normalized: value entries carry no unit of measure.",
            "method": "Item purchases are classified from the item card, trying Item Category Group 3 "
                      "(what the goods are), then Group 2 (how they are handled), then the Item Category "
                      "Code; the field that decided each line is shown. Non-item purchase postings are "
                      "classified by the G/L account and its category. The mappings are application "
                      "settings, not Business Central data.",
            "limitations": [
                "Purchase orders cannot be linked to posted invoices: the published vendor ledger has no "
                "order number, so 'invoice without PO' cannot be tested.",
                "Contracts do not exist in the published services, so contract compliance is not tested.",
                "Approvals are not published, so no approval status is shown.",
            ],
        },
    }
    _streams_cache[key] = (time.time(), out)
    with _last_lock:
        _last["streams"] = out
    return out


_compare_cache = {}


def compare(company, week, location, mode="week"):
    """Equivalent-period comparisons and the budget benchmark. Cached for 15 minutes
    because these are settled periods; the live 30-second refresh does not touch them."""
    key = (company, week or "", location or "", mode)
    location, only = _scope_of(location)
    mine = (lambda rows: rows) if only is None else \
        (lambda rows: [r for r in rows if _in_scope(only, r.get("Location_Code"))])
    hit = _compare_cache.get(key)
    if hit and time.time() - hit[0] < 900:
        return hit[1]
    c_from, c_to, _, _, like_for_like, label = _period(week, mode)
    settings = store.get_settings()
    cur = period_rows(company, c_from, c_to, 60)
    current_total = comparisons.total(mine(cur["rows"]), location or None)

    specs, rolling_label = comparisons.specs(c_from, c_to, mode)
    periods = []
    for spec in specs:
        rows = period_rows(company, spec["from"], spec["to"], 3600)["rows"]
        periods.append({**spec, "from": spec["from"].isoformat(), "to": spec["to"].isoformat(),
                        "total": comparisons.total(mine(rows), location or None)})
    rows = comparisons.build(current_total, periods, rolling_label)

    # ---- budget benchmark ----
    accounts = [a.strip() for a in (settings["budget"].get("cogs_accounts") or "").split(",") if a.strip()]
    budget = {"available": False, "reason": "No cost-of-goods accounts configured in Settings."}
    if accounts and only is not None:
        budget = {"available": False, "reason": "The budget is the whole company's; it is not compared with "
                                                "some locations."}
    elif accounts:
        try:
            b_rows = client.budget_entries(company, (c_from.replace(day=1)).isoformat(), c_to.isoformat(),
                                           accounts, settings["budget"].get("name") or "")
            names = sorted({r.get("Budget_Name") for r in b_rows if r.get("Budget_Name")})
            pro = comparisons.prorate_budget(b_rows, c_from, c_to)
            gl = client.gl_entries(company, c_from.isoformat(), c_to.isoformat(), accounts)
            actual_cogs = sum(float(g.get("Amount") or 0) for g in gl)
            budget = {
                "available": bool(b_rows), "accounts": accounts, "budget_names": names,
                "budget_name": settings["budget"].get("name") or (names[0] if names else ""),
                "amount": pro["amount"], "days": pro["days"], "detail": pro["detail"],
                "actual_cogs": actual_cogs, "purchases": current_total,
                "variance": actual_cogs - pro["amount"],
                "variance_pct": ((actual_cogs - pro["amount"]) / pro["amount"] * 100) if pro["amount"] else None,
                "note": "Budget is a cost-of-goods figure, pro-rated by days from the monthly budget rows. "
                        "Actual cost of goods comes from the G/L entries of the same accounts; purchases are shown "
                        "beside it because buying and consuming are not the same thing.",
                "reason": "" if b_rows else "No budget rows for these accounts and period.",
            }
            if not b_rows:
                budget["available"] = False
        except BCError as exc:
            budget = {"available": False, "reason": str(exc)}

    out = {"current": {"from": c_from.isoformat(), "to": c_to.isoformat(), "total": current_total,
                       "like_for_like": like_for_like, "mode": mode, "period_label": label,
                       "company": company, "location": location or ""},
           "rows": rows, "budget": budget, "generated_at": datetime.now().isoformat(timespec="seconds")}
    _compare_cache[key] = (time.time(), out)
    return out


def drill(kind, key):
    """Returns the underlying BC records behind any KPI, card or exception."""
    with _last_lock:
        rows = list(_last.get("rows") or [])
        snap = _last.get("data") or {}
    inv = (snap.get("invoices") or {}) if isinstance(snap.get("invoices"), dict) else {}
    ops = snap.get("operations") or {}
    cols, out, title = [], [], kind

    def line_rows(filtered):
        return [{"Posting date": r.get("Posting_Date"), "Document": r.get("Document_No"),
                 "Type": r.get("Document_Type"), "Item": r.get("Item_No"),
                 "Description": (r.get("Item_Description") or "").strip(), "Location": r.get("Location_Code"),
                 "Quantity": r.get("Item_Ledger_Entry_Quantity"),
                 "Cost (ALL)": round(float(r.get("Cost_Amount_Actual") or 0)
                                     + (float(r.get("Cost_Amount_Expected") or 0)
                                        if r.get("Document_Type") == "Purchase Receipt" else 0), 2),
                 "Unit cost": r.get("Cost_per_Unit"), "Entry no.": r.get("Entry_No")} for r in filtered][:500]

    if kind in ("purchases", "lines"):
        title, out = "All purchase value entries in the period", line_rows(rows)
    elif kind == "item":
        title, out = f"Purchase lines for item {key}", line_rows([r for r in rows if r.get("Item_No") == key])
    elif kind == "document":
        title, out = f"Lines of document {key}", line_rows([r for r in rows if str(r.get("Document_No")) == str(key)])
    elif kind == "location":
        title, out = f"Purchase lines at {key}", line_rows([r for r in rows if r.get("Location_Code") == key])
    elif kind in ("movers", "increases", "decreases"):
        movers = snap.get("movers") or []
        if kind == "increases":
            movers = [m for m in movers if m["pct"] > 0]
        elif kind == "decreases":
            movers = [m for m in movers if m["pct"] < 0]
        title = "Items with a changed unit cost"
        out = [{"Item": m["item_no"], "Description": m["desc"], "Locations": ", ".join(m["locations"]),
                "Previous avg": round(m["prev_avg"], 2), "Current avg": round(m["cur_avg"], 2),
                "Change %": round(m["pct"], 1), "Quantity": round(m["cur_qty"], 2),
                "Impact (ALL)": round(m["impact"])} for m in movers]
    elif kind == "spreads":
        title = "Same item, different price by location"
        out = [{"Item": s["item_no"], "Description": s["desc"], "Cheapest": s["low_loc"],
                "Low cost": round(s["low"], 2), "Dearest": s["high_loc"], "High cost": round(s["high"], 2),
                "Spread %": round(s["spread"]), "Extra paid (ALL)": round(s["overpay"])}
               for s in (snap.get("spreads") or [])]
    elif kind == "top_spend":
        title = "Top spend items"
        out = [{"Item": t["item_no"], "Description": t["desc"], "Quantity": round(t["qty"], 2),
                "Avg cost": round(t["avg"], 2) if t["avg"] else None, "Spend (ALL)": round(t["cost"]),
                "Comparison spend": round(t["prev_cost"])} for t in (snap.get("top_spend") or [])]
    elif kind in ("invoices", "documents"):
        title = "Posted purchase documents"
        out = [{"Document": d["no"], "Type": d["type"], "Supplier": d["supplier"], "Posted by": d["user"],
                "Invoice date": d["document_date"], "Posted": d["posting"], "Lines": d["lines"],
                "Value (ALL)": round(d["value"]), "Locations": ", ".join(d["locations"])}
               for d in ops.get("documents", [])]
    elif kind == "suspicious":
        title = "Invoices with negative or zero-cost lines"
        out = [{"Invoice": x["doc"], "Posted by": x.get("user") or "", "Lines": x["lines"],
                "Negative lines": x["neg_lines"], "Zero-cost lines": x["zero_lines"],
                "Value (ALL)": round(x["value"]), "Locations": ", ".join(x["locations"])}
               for x in inv.get("suspicious", [])]
    elif kind == "late":
        title = "Invoices posted after the threshold"
        out = [{"Invoice": x["doc"], "Supplier": x["vendor"], "Invoice date": x["document_date"],
                "Posted": x["posting_date"], "Days": x["lag"], "Value (ALL)": round(x["value"])}
               for x in inv.get("late", [])]
    elif kind in ("po", "po_pending", "purchase_orders"):
        pos = ops.get("purchase_orders", [])
        if kind == "po_pending":
            pos = [p for p in pos if p["received_not_invoiced"] > 0]
        title = "Open purchase orders"
        out = [{"Order": p["no"], "Supplier": p["supplier"], "Location": p["location"], "Status": p["status"],
                "Document date": p["document_date"], "Age (working days)": p["age_days"],
                "Order amount": round(p["amount"]), "Received not invoiced (ALL)": round(p["received_not_invoiced"])}
               for p in pos]
    elif kind == "suppliers":
        title = "Suppliers by posted value"
        out = [{"Supplier": s["supplier"], "Spend (ALL)": round(s["spend"]), "Share %": round(s["share"], 1),
                "Invoices": s["invoices"], "Credit notes": s["credit_notes"], "Items": s["items"],
                "Locations": ", ".join(s["locations"]), "Documents with issues": s["issues"]}
               for s in ops.get("suppliers", [])]
    elif kind == "users":
        title = "Posting activity by user"
        out = [{"User": u["user"], "Invoices": u["invoices"], "Lines": u["lines"], "Value (ALL)": round(u["value"]),
                "Receipts": u["receipts"], "Credit notes": u["credit_memos"], "Negative lines": u["neg_lines"],
                "Zero-cost lines": u["zero_lines"], "Avg delay (days)": round(u["avg_lag"], 1) if u["avg_lag"] is not None else None,
                "Late": u["late"], "Backdated": u["backdated"]} for u in inv.get("users", [])]
    elif kind == "user":
        title = f"Documents posted by {key}"
        out = [{"Document": d["no"], "Type": d["type"], "Supplier": d["supplier"], "Posted": d["posting"],
                "Lines": d["lines"], "Value (ALL)": round(d["value"])}
               for d in ops.get("documents", []) if d["user"] == key]
    elif kind == "sla":
        title = "Service-level exceptions"
        out = [{"Severity": e["severity"], "Rule": e["rule"], "Exception": e["title"],
                "Document": e.get("document", ""), "Supplier": e.get("supplier", ""),
                "Impact (ALL)": round(e.get("impact") or 0), "Recommended action": e.get("next_action", "")}
               for e in ops.get("sla", [])]
    elif kind == "requisitions":
        title = "Open requisition worksheet lines"
        out = [{"Item": r["item"], "Description": r["description"], "Quantity": r["quantity"], "UOM": r["uom"],
                "Location": r["location"], "Supplier": r["vendor"], "Requested by": r["user"],
                "Due date": r["due_date"], "First seen by the app": r["first_seen"],
                "Hours open": r["hours_open"], "Value (ALL)": round(r["value"])}
               for r in ops.get("requisitions", [])]
    elif kind == "requisitions_closed":
        title = "Requisitions carried out or cleared (measured by this app)"
        out = [{"Item": c["item"], "Description": c["description"], "Location": c["location"],
                "Supplier": c["vendor"], "Requested by": c["user"], "First seen": c["first_seen"],
                "Closed": c["closed_at"], "Hours open": c["hours_open"]}
               for c in ops.get("closed_requisitions", [])]
    elif kind.startswith("quality:"):
        code = kind.split(":", 1)[1]
        q = snap.get("quality") or {}
        check = next((c for c in q.get("checks", []) if c["code"] == code), None)
        title = check["title"] if check else "Data quality finding"
        out = check["rows"] if check else []
    elif kind in ("services", "capex", "stream_items", "unclassified"):
        with _last_lock:
            st = _last.get("streams") or {}
        if kind == "services":
            title = "Service and other non-item purchase postings"
            out = [{"Document": r["document"], "Posted": r["posting"], "Supplier": r["supplier"],
                    "Account": r["account"], "Account name": r["account_name"], "Type": r["type"],
                    "Amount (ALL)": round(r["value"])} for r in st.get("services", [])]
        elif kind == "capex":
            title = "CAPEX postings in the period"
            out = [{"Document": r["document"], "Posted": r["posting"], "Supplier": r["supplier"],
                    "Account": r["account"], "Account name": r["account_name"],
                    "Amount (ALL)": round(r["value"])} for r in st.get("capex", [])]
        elif kind == "unclassified":
            title = "Purchases that could not be classified"
            out = [{"Item": r["item"], "Description": r["desc"], "Value on the card": r["category"],
                    "Why": r["reason"], "Value (ALL)": round(r["value"])}
                   for r in st.get("unclassified", [])]
        else:
            title = "Item purchases by purchase type"
            out = [{"Item": r["item"], "Description": r["description"], "Sub-category": r["category"],
                    "Group 3": r["group3"], "Group 2": r["group2"], "Classified by": r["classified_by"],
                    "Purchase type": r["type"], "Document": r["document"], "Posted": r["posting"],
                    "Location": r["location"], "Quantity": round(r["quantity"], 2),
                    "Value (ALL)": round(r["value"])} for r in st.get("item_rows", [])
                   if not key or r["type"] == key]
    elif kind == "contract":
        title = "Paid price against the contracted price"
        out = [{"Item": r["item_no"], "Description": r["desc"], "Contracted": round(r["contract"], 2),
                "Paid (avg)": round(r["paid"], 2), "Difference %": round(r["pct"], 1),
                "Quantity": round(r["qty"], 2), "Over/under paid (ALL)": round(r["overpaid"]),
                "Supplier": r["vendor"], "Locations": ", ".join(r["locations"])}
               for r in ops.get("contract_prices", [])]
    elif kind == "gaps":
        title = "KPIs that BC does not currently expose"
        out = [{"KPI": g["kpi"], "What is missing": g["missing"], "Technical requirement": g["requirement"]}
               for g in operations.DATA_GAPS]
    if out:
        cols = list(out[0].keys())
    return {"title": title, "kind": kind, "key": key, "columns": cols, "rows": out[:500], "count": len(out)}


PUBLIC_PATHS = {"/login", "/login.html", "/api/login", "/api/session", "/brand.css", "/logo.svg", "/logo.png", "/icon.svg", "/favicon.ico", "/manifest.webmanifest", "/sw.js"}
WRITE_PERMISSIONS = {"/api/settings": "settings", "/api/actions": "actions",
                     "/api/register/settings": "settings", "/api/register/run": "settings", "/api/register/test": "settings",
                     "/api/notify/test": "settings", "/api/notify/smtp": "settings",
                     "/api/savings/send": "export", "/api/weekly/job": "settings", "/api/recipients": "settings",
                     "/api/price-changes/send": "settings",
                     "/api/users": "users", "/api/password": "view"}


# ---------------------------------------------------------------- access scope (access.py)
# downloads: the export action of the role (Admin → Roles & permissions → Actions)
EXPORT_PATHS = {"/api/savings/pdf", "/api/savings/xlsx", "/api/purchasers/xlsx", "/api/price-changes/xlsx",
                "/api/negotiations/xlsx"}
NEGOTIATIONS_SCREEN = "purchasing.s.compare.negotiations"
NEGOTIATIONS_EDIT = "purchasing.negotiations.edit"
ACCESS_FREE = {"/api/session", "/api/logout", "/api/password", "/api/health", "/api/users", "/api/cache",
               "/api/notify/status", "/api/weekly/status",
               # the exceptions register cuts every answer to the person's scope itself (register_allow)
               "/api/register", "/api/register/update"}
# read for one location (the "location" parameter) by a person limited to some locations of the company
ACCESS_ONE_LOCATION = {"/api/dashboard", "/api/streams", "/api/comparisons", "/api/savings", "/api/savings/pdf",
                       "/api/savings/xlsx", "/api/stock", "/api/report"}
# read without a location by a person limited to some locations: all of theirs together, built from their entries
# only (the Location intelligence screens, Live operations, the Action center, Supplier performance)
ACCESS_SCOPE_SET = {"/api/dashboard", "/api/streams", "/api/comparisons", "/api/report", "/api/suppliers/locations"}
# answered cut to the locations in scope
ACCESS_FILTERED = {"/api/today/locations", "/api/locations", "/api/recipients/locations"}


# ---------------------------------------------------------------- the exceptions register (exreg.py)
# What Exceptions and the Action center were: the rules of the screens, read every day for the month in progress
# and kept until the data shows them gone, and what people track by hand. Owners come from Email recipients.
exreg.configure(
    sources={"orders": "Purchase orders", "requisitions": "Requisitions", "prices": "Prices & contracts",
             "invoices": "Invoice postings", "manual": "Tracked by hand"},
    owner_role={("orders", "*"): "purchasing_coordinator", ("requisitions", "*"): "purchasing_coordinator",
                ("prices", "*"): "purchasing_manager", ("invoices", "*"): "inputer",
                ("manual", "*"): "purchasing_coordinator"},
    role_label=dict(recipients.ROLES), default_role="purchasing_coordinator", tower="Purchasing",
    link="/purchasing/#register", mailer=lambda to, cc, subject, body, text: notify.send_mail(to, cc, subject, body, text))
RULE_GROUP = (("po-", "orders"), ("req-", "requisitions"), ("price-", "prices"), ("contract-", "prices"),
              ("neg-", "invoices"), ("zero-", "invoices"), ("noloc-", "invoices"), ("late-", "invoices"),
              ("nosup-", "invoices"))


def register_rules(company, actor="control run"):
    """The rules of the month in progress into the register, by group; a group whose Business Central read failed
    is not synced, so nothing closes for a failed read. Invoice postings close only for invoices still inside the
    month read (those posted before it leave the register with a note); a price rise belongs to its month."""
    d = _dashboard_build(company, None, "", "month", remember=False)
    ops = d.get("operations") or {}
    if ops.get("error"):
        return {"skipped": "Business Central did not answer: " + str(ops["error"])[:200]}
    c_from = (d.get("meta") or {}).get("current", [""])[0]
    posted = {x["no"]: x.get("posting") or "" for x in ops.get("documents") or []}
    groups = {g: [] for _, g in RULE_GROUP}
    for e in ops.get("sla") or []:
        code = str(e.get("code") or "")
        g = next((g for p, g in RULE_GROUP if code.startswith(p)), None)
        if g is None:
            continue
        locs = [x.strip() for x in str(e.get("location") or "").split(",") if x.strip()]
        extra = {k: e.get(k) for k in ("rule", "next_action", "document", "document_type", "supplier", "drill") if e.get(k)}
        if len(locs) > 1:
            extra["locations"] = locs
        if g == "invoices":
            extra["posting"] = posted.get(code.split("-", 1)[1], "")
        if code.startswith("price-"):
            extra["period"] = c_from
        groups[g].append({"subject": code, "kind": e.get("rule") or g, "location": locs[0] if len(locs) == 1 else "",
                          "title": e.get("title") or code,
                          "detail": " · ".join(x for x in (e.get("detail"), ("To do: " + e["next_action"]) if e.get("next_action") else "") if x),
                          "severity": e.get("severity"), "value": abs(float(e.get("impact") or 0)), "extra": extra})
    failed = {"orders": bool(ops.get("po_error")),
              "requisitions": not (ops.get("requisition_status") or {}).get("available"),
              "prices": not (ops.get("price_status") or {}).get("available"), "invoices": False}
    out = {}
    for g, found in groups.items():
        if failed[g]:
            out[g] = {"skipped": "Business Central did not answer for this group"}
            continue
        scope = None
        if g == "invoices":
            scope = lambda x: (x["extra"].get("posting") or c_from) >= c_from
        elif g == "prices":
            scope = lambda x: not x["subject"].startswith("price-") or x["extra"].get("period") == c_from
        out[g] = exreg.sync(g, company, found, actor=actor, in_scope=scope)
        if g == "invoices":
            out[g]["left"] = exreg.retire(g, company, lambda x: (x["extra"].get("posting") or c_from) < c_from,
                                          f"Left the month the control reads (posted before {c_from})", actor)
        elif g == "prices":
            out[g]["left"] = exreg.retire(g, company, lambda x: x["subject"].startswith("price-")
                                          and x["extra"].get("period") != c_from,
                                          "A new month: price rises are read again for it", actor)
    return out


def register_controls(actor="scheduler", force=False):
    """The daily run: every company of the daily notice, and the default one."""
    out = []
    for c in dict.fromkeys([config.BC_COMPANY] + list(notifier.companies())):
        if not force and exreg.synced_today("invoices", c):
            out.append({"company": c, "control": "rules", "status": "already read today"})
            continue
        try:
            out.append({"company": c, "control": "rules", "status": "synced", "rules": register_rules(c)})
        except Exception as exc:                                    # noqa: BLE001
            traceback.print_exc()
            out.append({"company": c, "control": "rules", "status": "failed", "detail": str(exc)[:300]})
    store.audit("controls", "exceptions register", actor, {}, {"companies": len(out)}, "")
    return out


def register_allow(user):
    """What a person limited to some companies or locations may see of the register; None = everything."""
    if not access.limited(user):
        return None

    def ok(company, location):
        if not access.company_ok(user, company):
            return False
        L = access.locations(user, company)
        return L is None or (location or "").upper() in L
    return ok


def register_status(user):
    return {"settings": exreg.settings(), "job": exreg.job(), "recent": exreg.recent_runs(),
            "mail": {"configured": bool(notify.smtp_info().get("configured"))}, "me": (user.get("email") or "").strip().lower(),
            "can_manage": auth.can(user, "actions") or auth.can(user, "settings"), "can_settings": auth.can(user, "settings")}


def register_as_actions():
    """The register as the daily notice reads the Action center: an invoice without a PO is closed when its item is."""
    return [{"source_exception": x["subject"], "status": {"Closed": "Resolved"}.get(x["status"], x["status"]),
             "owner": x["owner"], "due_date": x["due"]} for x in exreg.query("all", limit=100000)["items"]]


notify.ACTIONS = register_as_actions


class Handler(BaseHTTPRequestHandler):
    def _redirect(self, to):
        self.send_response(302)
        self.send_header("Location", to)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _cookie(self, name=None):
        name = name or auth.cookie_name()
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            if "=" in part:
                k, _, v = part.partition("=")
                if k.strip() == name:
                    return v.strip()
        return None

    def _user(self):
        return auth.session(self._cookie())

    def _set_session_cookie(self, token, clear=False):
        if clear:
            self.send_header("Set-Cookie", "session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0")
        else:
            self.send_header("Set-Cookie",
                             f"session={token}; Path=/; HttpOnly; SameSite=Strict; "
                             f"Max-Age={auth.SESSION_HOURS * 3600}")

    def log_message(self, fmt, *args):
        pass

    def _send(self, status, body, ctype="application/json; charset=utf-8"):
        if status == 200 and getattr(self, "_scope_filter", None) and isinstance(body, dict):
            body = self._scope_filter(body)
        payload = body if isinstance(body, bytes) else json.dumps(body, default=str).encode("utf-8")
        zipped = (len(payload) > 16 * 1024 and "gzip" in (self.headers.get("Accept-Encoding") or "")
                  and ctype.startswith("application/json"))
        # JSON only: the pages, scripts and styles of this tower pass through nginx's sub_filter, which rewrites
        # "/api/" into "/finance/api/" (or /purchasing/) and cannot read a compressed body (deploy/nginx/default.conf)
        if zipped:                                       # a large answer or page travels compressed
            payload = gzip.compress(payload, compresslevel=5)
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        if zipped:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            return {}

    # ---- Purchasing → Prices & contracts → Negotiations (negotiations.py)
    def _negotiations_get(self, url, q, user):
        if not auth.can(user, NEGOTIATIONS_SCREEN):
            self._send(403, {"error": "Your role does not hold the Negotiations screen."})
            return
        company = q.get("company") or config.BC_COMPANY
        try:
            a, b = savings_report.parse_range(q.get("from"), q.get("to"))
        except ValueError as exc:
            self._send(400, {"error": str(exc)})
            return
        local_cur = company_currency(company)
        if url.path == "/api/negotiations/audit":
            try:
                self._send(200, {"audit": negotiations.audit_of(company, int(q.get("scenario") or 0), q.get("target") or "")})
            except ValueError as exc:
                self._send(404, {"error": str(exc)})
            return
        if url.path == "/api/negotiations/detail":
            self._send(200, negotiations.detail(company, a, b, q.get("item") or "", q.get("variant") or "",
                                                q.get("vendor") or "", q.get("currency") or local_cur, local_cur))
            return
        progress = negotiations.ensure(client, company, a, b, q.get("refresh") == "1" and url.path == "/api/negotiations")
        period = {"from": a.isoformat(), "to": b.isoformat()}
        if progress:
            if url.path == "/api/negotiations/xlsx":
                self._send(409, {"error": "The months are still being read; try again in a moment."})
            else:
                self._send(200, {"pending": True, "progress": progress, "period": period,
                                 "note": "Reading the posted purchase invoices and credit memos of these months from "
                                         "Business Central. A month is read once; the next view is immediate."})
            return
        f = {k: (q.get(k) or "")[:120] for k in ("location", "dept", "brand", "vendor", "purchaser", "currency",
                                                  "contract", "ptype", "sub", "q")}
        f["_company_currency"] = local_cur
        view = negotiations.build(client, company, a, b, f)
        rates, no_rate, fx_err = negotiations.fx_rates(client, company, local_cur,
                                                       {r["currency"] for r in view["rows"]}, a, b)
        scen = negotiations.scenarios(company)
        if url.path == "/api/negotiations/xlsx":
            sid = int(q.get("scenario") or 0)
            sc = next((x for x in scen if x["id"] == sid), None)
            meta = {"company": company, "assumptions": [
                ("Company", company), ("Period (posting date)", f"{a.isoformat()} to {b.isoformat()}"),
                ("Contract validity checked on", date.today().isoformat()),
                ("Scenario", f"{sc['name']} · {sc['status']} · owner {sc['owner'] or '-'} · target {sc['target'] or '-'}"
                 if sc else "none"),
                ("Filters", ", ".join(f"{k}={v}" for k, v in f.items() if v and not k.startswith("_")) or "none"),
                ("Quantity", "Posted purchase invoices minus posted credit memos (Type = Item), in the item's base unit"),
                ("Paid price", "Amount excluding VAT after line and invoice discounts ÷ base quantity, in the document's "
                               "currency. Freight, item charges and rebates are not included"),
                ("Contract", "Price list lines active today, of the vendor (or every vendor), item, variant and currency; "
                             "taken back to excluding VAT when the price list includes VAT; per base unit after the "
                             "line discount; minimum quantity per invoice line"),
                ("Baseline", "The contract when exactly one price applies, else the weighted average; several prices = "
                             "ambiguous, no baseline"),
                ("Potential Savings at Historical Volume", "(Baseline − proposed price) × quantity of the period. "
                                                           "Positive = saving, negative = cost increase"),
                ("Note", "Scenario based on quantities purchased in the selected period. Actual savings depend on future "
                         "purchase volume, effective date, supplier terms, and implementation. 'Agreed' does not mean a "
                         "BC price list or a signed contract has been updated."),
                ("Exchange rates", ", ".join(f"1 {c} = {r:g} {local_cur}" for c, r in rates.items()) +
                 " (period average of BC Currency Exchange Rates, page 483)" if rates else "none needed"),
                ("Calculated at", time.strftime("%Y-%m-%d %H:%M")),
                ("Read from Business Central", "Posted purchase invoices and credit memos, price lists, item cards")]}
            body = negotiations.render_xlsx(view, sc, meta)
            store.audit("export", "negotiations", user["username"], {}, {"company": company, "scenario": sid}, "")
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header("Content-Disposition", f'attachment; filename="Negotiations_{company}_{a.isoformat()}_{b.isoformat()}.xlsx"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        view.update(period=period, company=company, scenarios=scen, can_edit=auth.can(user, NEGOTIATIONS_EDIT),
                    fx={"rates": rates, "missing": no_rate, "error": fx_err,
                        "source": "period average of BC Currency Exchange Rates (page 483), as the Executive group view"},
                    errors=negotiations.last_errors(company), today=date.today().isoformat(),
                    read_at=time.strftime("%Y-%m-%d %H:%M"), statuses=list(negotiations.STATUSES))
        self._send(200, view)

    def _negotiations_post(self, url, body, user):
        if not (auth.can(user, NEGOTIATIONS_SCREEN) and auth.can(user, NEGOTIATIONS_EDIT)):
            self._send(403, {"error": "Your role can read Negotiations but not change proposed prices."})
            return
        if self.headers.get("X-Requested-With") != "RHG-Purchasing":
            self._send(403, {"error": "This request must come from the Purchasing tower."})
            return
        company = str(body.get("company") or config.BC_COMPANY)
        who = user.get("name") or user["username"]
        try:
            if url.path == "/api/negotiations/scenario":
                sid = negotiations.save_scenario(company, body, who)
            elif url.path == "/api/negotiations/price":
                negotiations.set_price(company, body, who)
                sid = int(body.get("scenario"))
            elif url.path == "/api/negotiations/switch":
                negotiations.set_switch(company, body, who)
                sid = int(body.get("scenario"))
            else:
                self._send(404, {"error": "Not found"})
                return
        except (ValueError, TypeError) as exc:
            self._send(400, {"error": str(exc)})
            return
        self._send(200, {"ok": True, "scenario": sid,
                         "scenarios": negotiations.scenarios(company)})

    # ---- Export → PDF: any Excel export (an address ending in /xlsx) asked with format=pdf is built as usual and
    # handed back as a PDF of the same workbook (xlsxpdf.py); an answer that is not a workbook passes unchanged
    def _capture_get(self, path):
        """This tower's answer to a GET of path, with this request's session, kept instead of sent:
        (status, [(header, value)], body). For Export → PDF and Export → Send by email."""
        saved = self.path
        scope = {k: getattr(self, k, None) for k in ("_scope_filter", "_scope_set", "_allowed")}
        self.path = path
        cap = {"status": 200, "headers": []}
        real = (self.wfile, self.send_response, self.send_header, self.end_headers)
        buf = io.BytesIO()
        self.wfile = buf
        self.send_response = lambda code, message=None: cap.update(status=code)
        self.send_header = lambda k, v: cap["headers"].append((k, v))
        self.end_headers = lambda: None
        self._pdf_inner = True
        try:
            self.do_GET()
        finally:
            self.wfile, self.send_response, self.send_header, self.end_headers = real
            self._pdf_inner = False
            self.path = saved
            for k, v in scope.items():                      # the inner answer's scope filter stays with it
                setattr(self, k, v)
        return cap["status"], cap["headers"], buf.getvalue()

    def _as_pdf(self, url):
        rest = [(k, v) for k, v in parse_qsl(url.query, keep_blank_values=True) if k != "format"]
        status, headers, body = self._capture_get(url.path + ("?" + urlencode(rest) if rest else ""))
        cap = {"status": status, "headers": headers}
        h = {k.lower(): v for k, v in cap["headers"]}
        if cap["status"] != 200 or not str(h.get("content-type", "")).startswith(xlsxpdf.XLSX):
            self.send_response(cap["status"])
            for k, v in cap["headers"]:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)
            return
        m = re.search(r'filename="([^"]+)"', h.get("content-disposition", ""))
        name = m.group(1) if m else "export.xlsx"
        try:
            pdf = xlsxpdf.render(body, re.sub(r"\.xlsx$", "", name, flags=re.I).replace("_", " "))
        except Exception:                                          # noqa: BLE001
            traceback.print_exc()
            self._send(500, {"error": "The PDF could not be made from this export; Export → Excel gives the same data."})
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Disposition", f'attachment; filename="{xlsxpdf.pdf_name(name)}"')
        self.send_header("Content-Length", str(len(pdf)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(pdf)

    def _export_email(self, body, user, tower, send):
        """Export → Send by email (exportmail.py): the file built here as its download would be, then sent."""
        try:
            to, fmt = exportmail.check(body)
            if body.get("table"):
                if tower != "Finance":
                    raise ValueError("This export cannot be sent by email.")
                t = body["table"]
                fname = xlsxtable.filename(t.get("name"))
                data = xlsxtable.render(t.get("sheets"), t.get("notes") or [])
                title = str(t.get("name") or "")
            else:
                path = exportmail.clean_url(body.get("url"))
                status, headers, data = self._capture_get(path)
                h = {k.lower(): v for k, v in headers}
                if status != 200 or not str(h.get("content-type", "")).startswith(exportmail.XLSX):
                    try:
                        j = json.loads(gzip.decompress(data) if h.get("content-encoding") == "gzip" else data)
                    except (ValueError, OSError):
                        j = {}
                    msg = j.get("error") or ("The report is still being read; send it again in a moment."
                                             if status in (200, 202) else f"The export answered {status}.")
                    self._send(409 if status in (200, 202) else status, {"error": msg})
                    return
                m = re.search(r'filename="([^"]+)"', h.get("content-disposition", ""))
                fname = m.group(1) if m else "export.xlsx"
                title = re.sub(r"\.xlsx$", "", fname, flags=re.I).replace("_", " ")
            screen = str(body.get("title") or "")[:200]
            ctype = exportmail.XLSX
            if fmt == "pdf":
                data = xlsxpdf.render(data, title)
                fname, ctype = xlsxpdf.pdf_name(fname), "application/pdf"
            if len(data) > exportmail.MAX_BYTES:
                raise ValueError("The file is too large to send by email; download it instead.")
            me = (user.get("email") or "").strip()
            cc = [me] if body.get("copy_me") and me and me not in to else []
            subject, html_body, text_body = exportmail.compose(user, title, fname, fmt, body.get("note"), tower, screen)
            send(to, cc, subject, html_body, text_body, fname, ctype, data, me or None)
        except ValueError as exc:
            self._send(400, {"error": str(exc)})
            return
        except Exception as exc:                                   # noqa: BLE001
            traceback.print_exc()
            self._send(502, {"error": f"Could not send: {str(exc)[:240]}"})
            return
        store.audit("email", f"export · {fname}", user["username"], {},
                    {"to": to, "cc": cc, "format": fmt, "title": title}, str(body.get("note") or "")[:300])
        self._send(200, {"sent": {"to": to, "cc": cc, "file": fname}})

    def do_GET(self):
        if not getattr(self, "_pdf_inner", False) and "format=pdf" in self.path:
            u = urlparse(self.path)
            if u.path.endswith("/xlsx") and parse_qs(u.query).get("format", [""])[0] == "pdf":
                return self._as_pdf(u)
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        self._scope_filter = None
        self._scope_set = None
        if url.path == "/internal/recipients":
            if not self._service_call():
                self._send(403, {"error": "Forbidden."})
            else:
                self._send(200, recipients.public())
            return
        if url.path == "/internal/mail/status":
            if not self._service_call():
                self._send(403, {"error": "Forbidden."})
            else:
                info = smtp_config.info()
                self._send(200, {"configured": info.get("configured"), "from": info.get("from"),
                                 "host": info.get("host")})
            return
        user = self._user()
        try:
            if url.path in ("/login", "/login.html"):
                # Sign-in is central now.
                self._redirect("/")
            elif url.path == "/api/session":
                self._send(200, {"user": user, "roles": auth.ROLES} if user
                           else {"user": None, "roles": auth.ROLES})
            elif not user and url.path not in PUBLIC_PATHS:
                if url.path.startswith("/api/"):
                    self._send(401, {"error": "Not signed in."})
                else:
                    self._redirect("/")
            elif url.path not in PUBLIC_PATHS and not auth.can(user, "view"):
                # Signed in, but this role is not allowed into this tower.
                if url.path.startswith("/api/"):
                    self._send(403, {"error": "Your role does not have access to this application."})
                else:
                    self._redirect("/")
            elif url.path.startswith("/api/") and (refusal := self._access(url.path, q, user)):
                self._send(403, refusal)
            elif url.path in EXPORT_PATHS and not auth.can(user, "export"):
                self._send(403, {"error": "Your role cannot export."})
            elif url.path == "/api/users":
                if not auth.can(user, "users"):
                    self._send(403, {"error": "Your role cannot manage accounts."})
                else:
                    self._send(200, {"users": auth.list_users(), "roles": auth.ROLES,
                                     "sessions": auth.active_sessions()})
            elif url.path == "/api/logout":
                auth.logout(self._cookie())
                store.audit("logout", user["username"], user["username"], {}, {}, "")
                self._send(200, {"ok": True})
            elif url.path in ("/", "/index.html"):
                self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif url.path.lstrip("/") in STATIC_FILES and (STATIC / url.path.lstrip("/")).exists():
                name = url.path.lstrip("/")
                self._send(200, (STATIC / name).read_bytes(), STATIC_FILES[name])
            elif url.path.startswith("/fonts/") and url.path.endswith((".woff2", ".woff", ".otf", ".ttf")):
                font = (STATIC / "fonts" / url.path.rsplit("/", 1)[-1]).resolve()
                if font.exists() and str(font).startswith(str((STATIC / "fonts").resolve())):
                    self._send(200, font.read_bytes(), "font/woff2")
                else:
                    self._send(404, {"error": "Font not installed"})
            elif url.path == "/api/dashboard":
                d = dashboard(q.get("company") or config.BC_COMPANY, q.get("week"), self._scope_set or q.get("location"),
                              q.get("mode", "week"))
                self._send(200, {k: v for k, v in d.items() if k != "_supplier_matrix"} if isinstance(d, dict) else d)
            elif url.path == "/api/suppliers/locations" and not (
                    auth.can(user, "purchasing.s.suppliers.suploc") or auth.can(user, "purchasing.s.suppliers.supco")):
                self._send(403, {"error": "Your role does not include Supplier performance by location."})
            elif url.path == "/api/suppliers/locations":
                # Supplier performance → Location intelligence and Whole company: the company read of the period
                # (every location), by location, supplier and item
                d = dashboard(q.get("company") or config.BC_COMPANY, q.get("week"), self._scope_set or "",
                              q.get("mode", "week"))
                m = d.get("_supplier_matrix") if isinstance(d, dict) else None
                if m is None:
                    self._send(200, {"available": False, "error": ((d or {}).get("operations") or {}).get("error")
                                     or "The supplier figures could not be read."})
                else:
                    tr = transfers_request(q.get("company") or config.BC_COMPANY, (d.get("meta") or {}).get("current"),
                                           q.get("refresh") == "1")
                    if self._scope_set and isinstance(tr, dict) and isinstance(tr.get("rows"), list):
                        # a person limited to some locations: what they received and what they sent
                        A = {x.upper() for x in self._scope_set}
                        mine = lambda to, fr: (to or "").upper() in A or (fr or "").upper() in A
                        rows = [r for r in tr["rows"] if mine(r[0], r[1])]
                        tr = dict(tr, rows=rows, docs=[x for x in tr.get("docs") or [] if mine(x[0], x[1])],
                                  items={r[2]: (tr.get("items") or {}).get(r[2]) for r in rows},
                                  unmatched_documents=None)
                    self._send(200, dict(m, available=True, meta=d.get("meta"), transfers=tr))
            elif url.path == "/api/cache":
                with _dash_lock:
                    held = [{"company": k[0], "period": k[1] or "current", "location": k[2] or "all",
                             "mode": k[3], "age_seconds": int(time.time() - v[0])}
                            for k, v in _dash_cache.items()]
                self._send(200, {"local_file": localdb.stats(), "screens_held": held,
                                 "warm_at": config.WARM_AT,
                                 "memory_mb": memuse.process_mb(),
                                 "memory_note": "Working set of this application only. The history lives in "
                                                "the local file, so memory holds just the periods in use.",
                                 "note": "Ledger entries never change once posted, so every period read "
                                         "is kept in a local file and only newer entries are fetched. "
                                         "Deleting the file changes nothing except the next read's speed."})
            elif url.path == "/api/today/locations":
                company = q.get("company") or config.BC_COMPANY
                self._send(200, todayloc.request(client, company, q.get("refresh") == "1"))
            elif url.path == "/api/locations":
                company = q.get("company") or config.BC_COMPANY
                try:
                    rows = client.locations(company)
                    self._send(200, {"locations": rows, "source": config.BC_LOCATIONS_ENTITY})
                except BCError as exc:
                    # fall back to the locations seen in this period's postings, so the
                    # screen still works when the location page cannot be read
                    with _last_lock:
                        seen = sorted((_last.get("full") or {}).get("all_locations") or [])
                    self._send(200, {"locations": [{"code": c, "name": c} for c in seen],
                                     "error": str(exc)[:300],
                                     "source": "posted movements (the location page could not be read)"})
            elif url.path == "/api/executive":
                company = q.get("company") or config.BC_COMPANY
                try:
                    year = int(q.get("year") or date.today().year)
                except ValueError:
                    year = date.today().year
                year = max(date.today().year - 3, min(year, date.today().year))
                self._send(200, executive.request(client, company, year, company_currency(company),
                                                  q.get("refresh") == "1", cache))
            elif url.path in ("/api/savings", "/api/savings/pdf", "/api/savings/xlsx"):
                company = q.get("company") or config.BC_COMPANY
                filters = {k: (q.get(k) or "")[:120] for k in ("location", "vendor", "category", "stream", "q")}
                try:
                    r = savings_report.request(client, company, q.get("from"), q.get("to"), filters,
                                               q.get("refresh") == "1" and url.path == "/api/savings")
                except ValueError as exc:
                    self._send(400, {"error": str(exc)})
                    return
                if url.path == "/api/savings":
                    r["currency"] = company_currency(company)
                    r["recipients"] = dict(zip(("to", "cc"), savings_report.recipients(company=company)))
                    r["next_monthly"] = savings_report.next_monthly()
                    r["runs"] = savings_report.recent_runs()
                    self._send(200, r)
                elif r.get("pending"):
                    self._send(409, {"error": "The months are still being read; try again in a moment."})
                else:
                    cur = company_currency(company)
                    pdf = url.path.endswith("pdf")
                    body = savings_report.render_pdf(r, cur) if pdf else savings_report.render_xlsx(r, cur)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/pdf" if pdf else
                                     "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                    self.send_header("Content-Disposition",
                                     f'{"inline" if pdf else "attachment"}; filename="'
                                     f'{savings_report.file_stem(r)}.{"pdf" if pdf else "xlsx"}"')
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
            elif url.path.startswith("/api/negotiations"):
                self._negotiations_get(url, q, user)
            elif url.path in ("/api/price-changes", "/api/price-changes/xlsx"):
                # Purchase price changes (pricechg.py): against the last purchase and the contract, by purchaser
                company = q.get("company") or config.BC_COMPANY
                try:
                    r = pricechg.request(client, company, q.get("from"), q.get("to"),
                                         q.get("refresh") == "1" and url.path == "/api/price-changes")
                except ValueError as exc:
                    self._send(400, {"error": str(exc)})
                    return
                cur = company_currency(company)
                if url.path == "/api/price-changes":
                    to, cc = pricechg.summary_recipients(company)
                    self._send(200, dict(r, currency=cur, schedule=pricechg.settings(), next_run=pricechg.next_run(),
                                         runs=pricechg.recent_runs(), summary_to=to, summary_cc=cc))
                elif r.get("pending"):
                    self._send(409, {"error": "The months are still being read; try again in a moment."})
                else:
                    if q.get("purchaser") is not None and q.get("purchaser") != "*":
                        r = pricechg.cut(r, q.get("purchaser"))
                    body = pricechg.render_xlsx(r, cur)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                    self.send_header("Content-Disposition", f'attachment; filename="{pricechg.file_stem(r)}.xlsx"')
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
            elif url.path in ("/api/purchasers", "/api/purchasers/xlsx"):
                # Purchaser performance (purchasers.py): suppliers, invoices, prices, open orders and payables by
                # the Purchaser Code of Business Central.
                if not auth.can(user, "purchasing.spend.view"):
                    self._send(403, {"error": "Your role does not include purchase spend."})
                    return
                company = q.get("company") or config.BC_COMPANY
                try:
                    r = purchasers.request(client, company, q.get("from"), q.get("to"))
                except ValueError as exc:
                    self._send(400, {"error": str(exc)})
                    return
                cur = company_currency(company)
                if url.path == "/api/purchasers":
                    r["currency"] = cur
                    self._send(200, r)
                else:
                    body = purchasers.render_xlsx(r, cur)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                    self.send_header("Content-Disposition", f'attachment; filename="Purchasers_{company}_'
                                                            f'{r["period"]["from"]}_{r["period"]["to"]}.xlsx"')
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    store.audit("export", "purchaser performance", user["username"], {},
                                {"from": r["period"]["from"], "to": r["period"]["to"]}, company)
            elif url.path == "/api/compliance":
                self._send(200, compliance_screen(q.get("company") or config.BC_COMPANY,
                                                  q.get("month") or date.today().strftime("%Y-%m")))
            elif url.path == "/api/stock":
                self._send(200, stock_screen(q.get("company") or config.BC_COMPANY, q.get("location"),
                                             q.get("month") or date.today().strftime("%Y-%m"),
                                             header_period=q.get("header")))
            elif url.path == "/api/stock-overview":
                self._send(200, stock_overview(q.get("company") or config.BC_COMPANY,
                                               q.get("month") or date.today().strftime("%Y-%m")))
            elif url.path == "/api/streams":
                self._send(200, streams(q.get("company") or config.BC_COMPANY, q.get("week"),
                                        self._scope_set or q.get("location"), q.get("mode", "week")))
            elif url.path == "/api/comparisons":
                self._send(200, compare(q.get("company") or config.BC_COMPANY, q.get("week"),
                                        self._scope_set or q.get("location"), q.get("mode", "week")))
            elif url.path == "/api/report":
                kind = q.get("type", "weekly")
                mode = q.get("mode", "week")
                full = dashboard(q.get("company") or config.BC_COMPANY, q.get("week"), self._scope_set or q.get("location"),
                                 mode)
                if kind == "daily":
                    day = date.fromisoformat(q["day"]) if q.get("day") else date.today() - timedelta(days=1)
                    company = q.get("company") or config.BC_COMPANY
                    day_rows = period_rows(company, day, day, 600)["rows"]
                    loc = q.get("location")
                    if loc:
                        day_rows = [r for r in day_rows if r.get("Location_Code") == loc]
                    elif self._scope_set:
                        day_rows = [r for r in day_rows if _in_scope({x.upper() for x in self._scope_set},
                                                                     r.get("Location_Code"))]
                    self._send(200, reports.daily(full, day_rows, day))
                else:
                    cmp_data = compare(q.get("company") or config.BC_COMPANY, q.get("week"),
                                       self._scope_set or q.get("location"), mode)
                    self._send(200, reports.weekly(full, cmp_data))
            elif url.path == "/api/drill":
                self._send(200, drill(q.get("kind", "purchases"), q.get("key", "")))
            elif url.path == "/api/actions":
                # the Action center is the Exceptions register now: its items tracked by hand
                d = exreg.query("all", source="manual", allow=register_allow(user))
                self._send(200, {"actions": d["items"], "moved": "The actions are in the Exceptions register.",
                                 "statuses": exreg.STATUSES})
            elif url.path == "/api/register":
                if not auth.can(user, "purchasing.s.register"):
                    self._send(403, {"error": "Your role does not include the Exceptions register."})
                    return
                owner = q.get("owner") or ""
                if q.get("mine") == "1":
                    owner = (user.get("email") or "").strip() or "(nobody has this address)"
                d = exreg.query(q.get("status") or "active", owner, q.get("source") or "", q.get("company") or "",
                                q.get("location") or "", q.get("q") or "", register_allow(user))
                self._send(200, dict(d, **register_status(user)))
            elif url.path == "/api/settings":
                self._send(200, store.get_settings())
            elif url.path == "/api/notify/status":
                self._send(200, {"smtp": notify.smtp_info(), "settings": store.get_settings()["notifications"],
                                 "recipients": notifier.recipients(), "companies": notifier.companies(),
                                 "next_run": notifier.next_run(), "runs": notify.recent_runs(),
                                 "test": notifier.test_status(user["username"]),
                                 "me": (user or {}).get("email") or ""})
            elif url.path == "/api/notify/test":
                if not auth.can(user, "settings"):
                    self._send(403, {"error": "Your role cannot send email tests."})
                else:
                    job = notifier.test_status(user["username"], q.get("id"))
                    self._send(200 if job else 404, job or {
                        "error": "Email test not found. The server may have restarted. "
                                 "Check email history and your inbox before sending again."})
            elif url.path == "/api/weekly/status":
                to, cc = weekly_report.recipients(company=(notifier.companies() or [None])[0])
                self._send(200, {"next_run": weekly_report.next_run(), "runs": weekly_report.recent_runs(),
                                 "to": to, "cc": cc, "companies": notifier.companies(),
                                 "week": [d.isoformat() for d in weekly_report.last_week()]})
            elif url.path in ("/api/weekly/job", "/api/weekly/preview"):
                job = weekly.job(q.get("id") or "", user["username"])
                if not job:
                    self._send(404, {"error": "This report is no longer held; build it again."})
                elif url.path == "/api/weekly/job":
                    self._send(200, {k: v for k, v in job.items() if k != "html"})
                elif job.get("html"):
                    self._send(200, job["html"].encode("utf-8"), "text/html; charset=utf-8")
                else:
                    msg = "Still reading Business Central…" if job["status"] == "running" else html.escape(job.get("detail") or "")
                    self._send(200, (f"<!doctype html><meta http-equiv='refresh' content='4'><p style='font-family:sans-serif;"
                                     f"padding:20px'>{msg}</p>").encode("utf-8"), "text/html; charset=utf-8")
            elif url.path == "/api/notify/preview":
                try:
                    body = notifier.preview(q.get("company") or notifier.companies()[0])
                except Exception as exc:                         # noqa: BLE001
                    body = (f"<p style='font-family:sans-serif'>The preview could not be built: "
                            f"{html.escape(str(exc)[:300])}</p>")
                self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")
            elif url.path == "/api/audit":
                self._send(200, {"audit": store.get_audit()})
            elif url.path == "/api/companies":
                self._send(200, {"companies": client.list_companies(), "default": config.BC_COMPANY})
            elif url.path == "/api/recipients/locations":
                try:
                    locs = [x["code"] for x in client.locations(q.get("company") or config.BC_COMPANY)]
                except Exception as exc:                         # noqa: BLE001
                    self._send(200, {"locations": [], "error": str(exc)[:200]})
                    return
                self._send(200, {"locations": sorted(set(locs))})
            elif url.path == "/api/recipients":
                try:
                    companies = client.list_companies()
                except Exception:                                # noqa: BLE001
                    companies = []
                self._send(200, dict(recipients.public(), companies=companies, tower="Purchasing"))
            elif url.path == "/api/health":
                self._send(200, {"ok": True, "bc": config.BC_BASE_URL, "company": config.BC_COMPANY})
            else:
                self._send(404, {"error": "Not found"})
        except BCError as exc:
            print(f"[{datetime.now():%H:%M:%S}] BC error: {exc}", file=sys.stderr)
            self._send(502, {"error": str(exc)})
        except ValueError as exc:
            self._send(400, {"error": f"Bad request: {exc}"})
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._send(500, {"error": f"Unexpected error: {exc}"})

    def _access(self, path, q, user, body=None):
        """The company and location scope of the person (access.py): a refusal to send, or None. A person limited to
        some locations of a company reads each screen for one of them (the location filter) and the location
        lists cut to them; the screens of the whole company are refused."""
        if not access.limited(user) or path in ACCESS_FREE:
            return None
        if body is not None and not body.get("company"):
            return None                     # a change that names no company (an action, a setting): the role decides
        company = q.get("company") or (body or {}).get("company") or config.BC_COMPANY
        if path == "/api/companies":
            self._scope_filter = lambda b: dict(b, companies=access.filter_companies(b.get("companies"), user))
            return None
        if not access.company_ok(user, company):
            return access.refuse_company(company)
        L = access.locations(user, company)
        if L is not None:
            if body is not None:
                return access.refuse_company_wide(user, company)
            loc = (q.get("location") or "").upper()
            if not loc and path in ACCESS_SCOPE_SET:
                self._scope_set = frozenset(L)          # every location of theirs, together
            elif not loc and path in ACCESS_ONE_LOCATION and len(L) == 1:
                q["location"] = next(iter(L))           # their one location
            elif path in ACCESS_ONE_LOCATION:
                if loc not in L:
                    return {"error": f"Choose one of your locations: {access.describe(user, company)}.",
                            "access": "location", "scope": access.describe(user, company)}
            elif path not in ACCESS_FILTERED:
                return access.refuse_company_wide(user, company)
        if L is not None and (path in ACCESS_ONE_LOCATION or path in ACCESS_SCOPE_SET):
            # read for one location of the scope, or for all of theirs: built from their entries, keep it whole
            self._scope_filter = lambda b: access.filter_payload(b, user, company, None, ("all_locations",), False)
        else:
            self._scope_filter = lambda b: access.filter_payload(b, user, company)
        return None

    def _service_call(self):
        """Another RHG service on the internal network, proven by the shared service token."""
        token = auth.SERVICE_TOKEN
        given = self.headers.get("X-Service-Token") or ""
        return bool(token) and hmac.compare_digest(given.encode(), token.encode())

    # what another tower may attach: content type -> (file extension, first bytes of the file)
    ATTACHMENT_TYPES = {"application/pdf": (".pdf", b"%PDF"), "text/csv": (".csv", b""),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": (".xlsx", b"PK\x03\x04"),
                        # pictures of a screen (scheduled reports): shown in the body (cid) or attached
                        "image/png": (".png", b"\x89PNG"), "image/jpeg": (".jpg", b"\xff\xd8\xff")}

    def _internal_mail(self, body):
        """Sends one email for another tower (Finance) through the mail server set up here,
        so the password is entered once. Recipients are given by the calling tower."""
        if not self._service_call():
            self._send(403, {"error": "Forbidden."})
            return
        to, bad_to = notify.parse_emails(",".join(body.get("to") or []))
        cc, bad_cc = notify.parse_emails(",".join(body.get("cc") or []))
        subject = str(body.get("subject") or "")[:300]
        html_body, text_body = str(body.get("html") or ""), str(body.get("text") or "")
        if bad_to or bad_cc or not to or len(to) + len(cc) > 20 or not subject or len(html_body) > 2_000_000:
            self._send(400, {"error": "Recipients, subject or body are not valid."})
            return
        source = str(body.get("source") or "another tower")[:60]
        files, inline = [], []
        for a in (body.get("attachments") or [])[:5]:
            name = re.sub(r"[^A-Za-z0-9._ -]", "_", str(a.get("filename") or "attachment"))[:120]
            ctype = str(a.get("content_type") or "application/octet-stream")
            if ctype not in self.ATTACHMENT_TYPES:
                self._send(400, {"error": "Only PDF, CSV, Excel (.xlsx) and PNG / JPEG attachments are accepted."})
                return
            try:
                data = base64.b64decode(a.get("data") or "", validate=True)
            except (ValueError, TypeError):
                self._send(400, {"error": "An attachment could not be read."})
                return
            ext, magic = self.ATTACHMENT_TYPES[ctype]
            if not name.lower().endswith(ext) or (magic and not data.startswith(magic)):
                self._send(400, {"error": f"The attachment {name} is not a {ext} file."})
                return
            if len(data) > 10_000_000:
                self._send(400, {"error": "An attachment is larger than 10 MB."})
                return
            cid = re.sub(r"[^A-Za-z0-9._-]", "", str(a.get("cid") or ""))[:60]
            if cid and ctype.startswith("image/"):
                inline.append((cid, *ctype.split("/", 1), data))
            else:
                files.append((name, *ctype.split("/", 1), data))
        try:
            reply, _bad = notify.parse_emails(str(body.get("reply_to") or ""))
            notify.send_mail(to, [c for c in cc if c not in to], subject, html_body, text_body or subject, files,
                             inline, reply_to=reply[0] if reply else None)
        except Exception as exc:                                # noqa: BLE001
            store.audit("internal mail", source, "service", {}, {"to": to, "cc": cc, "status": "failed"},
                        str(exc)[:300])
            self._send(502, {"error": str(exc)[:300]})
            return
        store.audit("internal mail", source, "service", {}, {"to": to, "cc": cc, "status": "sent"}, subject)
        self._send(200, {"ok": True, "from": smtp_config.current().get("from")})

    def do_POST(self):
        url = urlparse(self.path)
        body = self._body()
        if url.path == "/internal/mail":
            self._internal_mail(body)
            return
        if url.path == "/internal/unicorn/facts":       # Unicorn (identity) asking for its morning figures
            if not self._service_call():
                self._send(403, {"error": "Forbidden."})
                return
            try:
                day = date.fromisoformat(str(body.get("date"))[:10]) if body.get("date") else None
                companies = [str(c).strip().upper() for c in (body.get("companies") or []) if str(c).strip()] \
                    or [config.BC_COMPANY]
                self._send(200, unicorn_facts.request(companies, dashboard, nopo_window, company_currency,
                                                      day, bool(body.get("refresh"))))
            except ValueError:
                self._send(400, {"error": "Bad date."})
            return
        if url.path == "/internal/recipients":           # Finance saving the directory for its user
            if not self._service_call():
                self._send(403, {"error": "Forbidden."})
                return
            try:
                self._send(200, recipients.save(body.get("data") or {}, f"{str(body.get('actor') or 'finance')[:60]} (Finance)"))
            except ValueError as exc:
                self._send(400, {"error": str(exc)})
            return
        user = self._user()
        if url.path == "/api/login":
            self._send(410, {"error": "Sign in at the site root; this tower no longer holds accounts."})
            return
        if not user:
            self._send(401, {"error": "Not signed in."})
            return
        if not auth.can(user, "view"):
            self._send(403, {"error": "Your role does not have access to this application."})
            return
        self._scope_filter = None
        self._scope_set = None
        refusal = self._access(url.path, {}, user, body) if url.path.startswith("/api/") else None
        if refusal:
            self._send(403, refusal)
            return
        if url.path == "/api/password":
            ok, err = auth.change_own_password(user["username"], body.get("current"), body.get("new"))
            store.audit("password change", user["username"], user["username"], {}, {"ok": ok}, err or "")
            self._send(200 if ok else 400, {"ok": ok} if ok else {"error": err})
            return
        if url.path.startswith("/api/users"):
            if not auth.can(user, "users"):
                self._send(403, {"error": "Your role cannot manage accounts."})
                return
            if url.path == "/api/users":
                created, err = auth.create_user(body.get("username"), body.get("name"), body.get("role"),
                                                body.get("password"), user["username"])
                store.audit("user created", body.get("username", ""), user["username"], {},
                            {"role": body.get("role")}, err or "")
                self._send(200 if created else 400, created or {"error": err})
            else:
                target = url.path.rsplit("/", 1)[-1]
                updated, err = auth.update_user(target, body, user["username"])
                store.audit("user updated", target, user["username"], {},
                            {k: v for k, v in body.items() if k != "password"}, err or "")
                self._send(200 if updated else 400, updated or {"error": err})
            return
        if url.path.startswith("/api/negotiations/"):
            self._negotiations_post(url, body, user)
            return
        needed = WRITE_PERMISSIONS.get(url.path) or ("actions" if url.path.startswith("/api/actions") else None)
        if needed and not auth.can(user, needed):
            self._send(403, {"error": f"Your role ({user['role']}) cannot change this."})
            return
        actor = user["username"]
        try:
            if url.path == "/api/actions":
                # Track: an item of the Exceptions register, tracked by hand (or the one already there)
                try:
                    self._send(200, exreg.track(body or {}, actor, user.get("email") or ""))
                except ValueError as exc:
                    self._send(400, {"error": str(exc)})
            elif url.path.startswith("/api/actions/"):
                self._send(410, {"error": "The Action center is now the Exceptions register: change the item there."})
            elif url.path == "/api/register/update":
                if self.headers.get("X-Requested-With") != "RHG-Purchasing":
                    self._send(403, {"error": "This request must come from Purchasing."})
                    return
                if not auth.can(user, "purchasing.s.register"):
                    self._send(403, {"error": "Your role does not include the Exceptions register."})
                    return
                allow = register_allow(user)
                item = exreg.get(str(body.get("id") or ""))
                if not item or (allow is not None and not allow(item["company"], item["location"])):
                    self._send(404, {"error": "This exception is not in the register, or not in your scope."})
                    return
                me = (user.get("email") or "").strip().lower()
                mine = bool(me) and me in [o.lower() for o in (item["owner"] or "").split(", ")]
                if not mine and not (auth.can(user, "actions") or auth.can(user, "settings")):
                    self._send(403, {"error": "Only its owner, or a role with actions, can change this item."})
                    return
                patch = {k: body[k] for k in ("status", "comment", "owner", "due") if k in body}
                try:
                    self._send(200, {"item": exreg.update(item["id"], patch, actor)})
                except (KeyError, ValueError) as exc:
                    self._send(400, {"error": str(exc).strip("'\"")})
            elif url.path in ("/api/register/settings", "/api/register/run", "/api/register/test"):
                if self.headers.get("X-Requested-With") != "RHG-Purchasing":
                    self._send(403, {"error": "This request must come from Purchasing."})
                    return
                if url.path.endswith("/settings"):
                    store.save_settings({"exception_register": body}, actor)
                    self._send(200, register_status(user))
                elif url.path.endswith("/run"):
                    self._send(202, {"job": exreg.start_job(actor)})
                else:
                    to, bad = notify.parse_emails(body.get("to") or user.get("email") or "")
                    if bad or not to:
                        self._send(400, {"error": "Give the address to send the test to."
                                         if not bad else "Not an email address: " + ", ".join(bad)})
                        return
                    try:
                        self._send(200, {"sent": exreg.send_digests(to, body.get("owner") or None, actor)})
                    except ValueError as exc:
                        self._send(400, {"error": str(exc)})
            elif url.path == "/api/settings":
                self._send(200, store.save_settings(body, actor))
            elif url.path == "/api/export/email":
                if self.headers.get("X-Requested-With") != "RHG-Purchasing":
                    self._send(403, {"error": "This request must come from Purchasing."})
                    return
                if not auth.can(user, "export"):
                    self._send(403, {"error": "Your role cannot export."})
                    return
                self._export_email(body, user, "Purchasing", lambda to, cc, subj, h, t, fname, ctype, data, reply:
                                   notify.send_mail(to, cc, subj, h, t, [(fname, "application", ctype.split("/", 1)[1], data)],
                                                    reply_to=reply))
            elif url.path == "/api/savings/send":
                if self.headers.get("X-Requested-With") != "RHG-Purchasing":
                    self._send(403, {"error": "This request must come from Purchasing."})
                    return
                company = body.get("company") or config.BC_COMPANY
                filters = {k: str(body.get(k) or "")[:120] for k in ("location", "vendor", "category", "stream", "q")}
                try:
                    r = savings_report.request(client, company, body.get("from"), body.get("to"), filters)
                except ValueError as exc:
                    self._send(400, {"error": str(exc)})
                    return
                if r.get("pending"):
                    self._send(409, {"error": "The months are still being read; send when the report is on screen."})
                    return
                only, bad = notify.parse_emails(body.get("only_to") or "")
                if bad:
                    self._send(400, {"error": "Not an email address: " + ", ".join(bad)})
                    return
                try:
                    entry = savings_report.send(r, company_currency(company), to=only or None, cc=[] if only else None,
                                                actor=actor, test=bool(only))
                    self._send(200, {"ok": True, "sent": entry})
                except Exception as exc:                        # noqa: BLE001
                    traceback.print_exc()
                    self._send(502, {"error": str(exc)[:300]})
            elif url.path == "/api/price-changes/send":
                if self.headers.get("X-Requested-With") != "RHG-Purchasing":
                    self._send(403, {"error": "This request must come from Purchasing."})
                    return
                company = body.get("company") or config.BC_COMPANY
                test_to, bad = notify.parse_emails(body.get("test_to") or "")
                if bad:
                    self._send(400, {"error": "Not an email address: " + ", ".join(bad)})
                    return
                try:
                    r = pricechg.request(client, company, body.get("from"), body.get("to"))
                except ValueError as exc:
                    self._send(400, {"error": str(exc)})
                    return
                if r.get("pending"):
                    self._send(409, {"error": "The months are still being read; send when the report is on screen."})
                    return
                only = body.get("purchaser")
                sent = pricechg.send_all(r, company_currency(company), actor=actor, test_to=test_to or None,
                                         only=None if only in (None, "", "*") else only)
                self._send(200, {"sent": sent, "runs": pricechg.recent_runs()})
            elif url.path == "/api/weekly/job":
                if self.headers.get("X-Requested-With") != "RHG-Purchasing":
                    self._send(403, {"error": "This request must come from Purchasing."})
                    return
                to, bad = notify.parse_emails(body.get("to") or "")
                if bad:
                    self._send(400, {"error": "Not an email address: " + ", ".join(bad)[:120]})
                    return
                company = body.get("company") or notifier.companies()[0]
                self._send(202, weekly.start_job(company, actor, to or None))
            elif url.path == "/api/recipients":
                if self.headers.get("X-Requested-With") != "RHG-Purchasing":
                    self._send(403, {"error": "This request must come from Purchasing."})
                    return
                self._send(200, dict(recipients.save(body, actor), companies=client.list_companies(), tower="Purchasing"))
            elif url.path == "/api/notify/smtp":
                if self.headers.get("X-Requested-With") != "RHG-Purchasing":
                    self._send(403, {"error": "This request must come from Purchasing."})
                    return
                self._send(200, {"smtp": smtp_config.save(body, actor)})
            elif url.path == "/api/notify/test":
                to, bad = notify.parse_emails(body.get("to") or "")
                if bad:
                    self._send(400, {"error": f"Not an email address: {', '.join(bad)[:120]}"})
                    return
                company = body.get("company") or notifier.companies()[0]
                self._send(202, notifier.start_test(company, only_to=to or None, actor=actor))
            else:
                self._send(404, {"error": "Not found"})
        except ValueError as exc:
            self._send(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._send(500, {"error": f"Unexpected error: {exc}"})


def _warm(company):
    """Reads the periods people open first, so they are already in the local file."""
    today = date.today()
    first = today.replace(day=1)
    prev_end = first - timedelta(days=1)
    jobs = {
        "this month": lambda: period_rows(company, first, today, 600),
        "last month": lambda: period_rows(company, prev_end.replace(day=1), prev_end, 86400),
        "this month, vendors": lambda: period_rows(company, first, today, 600, kind="vendor"),
        "last month, vendors": lambda: period_rows(company, prev_end.replace(day=1), prev_end, 86400,
                                                   kind="vendor"),
        "item cards": lambda: client.item_catalog(company),
        "chart of accounts": lambda: client.chart_of_accounts(company),
        "stockkeeping units": lambda: client.stockkeeping_units(company),
    }
    started = time.time()
    done = in_parallel(jobs, workers=4)
    ok = [n for n, (_, err) in done.items() if not err]
    failed = {n: str(err)[:120] for n, (_, err) in done.items() if err}
    print(f"[{datetime.now():%H:%M:%S}] warm-up for {company}: {len(ok)} of {len(jobs)} sources in "
          f"{time.time() - started:.0f}s" + (f" · failed: {failed}" if failed else ""))


def _warm_loop():
    """Warms on start, then once a day at the configured time."""
    if config.WARM_ON_START:
        time.sleep(3)
        try:
            _warm(config.BC_COMPANY)
        except Exception as exc:                                 # noqa: BLE001
            print(f"warm-up failed: {exc}")
    if not config.WARM_AT:
        return
    while True:
        now = datetime.now()
        hour, _, minute = config.WARM_AT.partition(":")
        try:
            target = now.replace(hour=int(hour), minute=int(minute or 0), second=0, microsecond=0)
        except ValueError:
            return
        if target <= now:
            target += timedelta(days=1)
        time.sleep(max(60, (target - now).total_seconds()))
        try:
            _warm(config.BC_COMPANY)
        except Exception as exc:                                 # noqa: BLE001
            print(f"warm-up failed: {exc}")


def main():
    first_password = auth.bootstrap()
    if first_password:
        print("=" * 62)
        print("First run: an administrator account was created.")
        print("   username: admin")
        print(f"   password: {first_password}")
        print("Sign in with it and change the password when asked. It is shown only now.")
        print("=" * 62)
    if not config.BC_USERNAME:
        print("WARNING: BC_USERNAME is empty in .env - Business Central will probably refuse the connection.")
    server = ThreadingHTTPServer((config.APP_HOST, config.APP_PORT), Handler)
    shown_host = "127.0.0.1" if config.APP_HOST in ("0.0.0.0", "") else config.APP_HOST
    link = f"http://{shown_host}:{config.APP_PORT}"
    print(f"RHG Purchasing Control Tower running at {link}")
    print(f"Business Central: {config.BC_BASE_URL}  company: {config.BC_COMPANY}  (read-only)")
    print("Press Ctrl+C to stop.")
    threading.Thread(target=_warm_loop, daemon=True).start()
    notifier.start()
    savings_report.Monthly(client, lambda: notifier.companies(), company_currency).start()
    weekly.start()
    try:
        moved = exreg.migrate_actions(store.list_actions())
        if moved:
            print(f"Exceptions register: {moved} actions of the Action center moved in.")
    except Exception:                                               # noqa: BLE001
        traceback.print_exc()
    exreg.start(register_controls)
    pricechg.Weekly(client, lambda: notifier.companies(), company_currency).start()
    smtp = notify.smtp_info()
    print("Email notices: " + ("mail server " + smtp["host"] if smtp["configured"]
                               else "mail server not configured in Settings"))
    if "--no-browser" not in sys.argv:
        threading.Timer(1.0, lambda: webbrowser.open(link)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")


if __name__ == "__main__":
    main()
