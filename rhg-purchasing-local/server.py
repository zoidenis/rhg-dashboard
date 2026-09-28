"""RHG Purchasing Control Tower - local server.

Business Central is read-only. Actions, comments and thresholds created in the
app are stored locally under ./data and never written back to BC.

Run:  python server.py     then open http://127.0.0.1:8765
"""
import json
import sys
from concurrent.futures import ThreadPoolExecutor
import threading
import time
import traceback
import webbrowser
from calendar import monthrange
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import analytics
import auth
import brief
import classify
import compliance
import contracts
import comparisons
import config
import localdb
import postings
import requisitions as requisition_view
import memuse
import stock
import currency
import history
import invoices
import operations
import quality
import reports
import store
from bc_client import BCClient, BCError, PeriodCache

STATIC = config.BASE_DIR / "static"
STATIC_FILES = {"brand.css": "text/css; charset=utf-8", "logo.svg": "image/svg+xml",
                "logo.png": "image/png", "favicon.ico": "image/x-icon",
                "icon.svg": "image/svg+xml",
                "manifest.webmanifest": "application/manifest+json",
                "sw.js": "application/javascript; charset=utf-8",
                "select.js": "application/javascript; charset=utf-8",
                "select.css": "text/css; charset=utf-8",
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


def _dashboard_build(company, week, location, mode="week"):
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
    data = analytics.build(cur["rows"], prev["rows"], location or None)

    meta = {
        "company": company, "location": location or "",
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
            req_hist, closed_reqs = history.track_requisitions(company, req_rows)
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

        sla = (operations.sla_exceptions(docs, pos, data["movers"], settings, meta)
               + req_exceptions + contract_exceptions)
        rank = {"critical": 0, "warning": 1, "info": 2}
        sla.sort(key=lambda e: (rank.get(e["severity"], 3), -abs(e.get("impact") or 0)))
        sup_rows, sup_total = operations.supplier_summary(docs, settings)
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
            "po_error": po_error, "sla": sla, "suppliers": sup_rows, "supplier_total": sup_total,
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
    actions = store.list_actions()
    data["actions"] = actions
    tracked = {a.get("source_exception") for a in actions if a.get("source_exception")}
    live_codes = {e["code"] for e in (ops.get("sla") or [])}
    for a in actions:
        src = a.get("source_exception") or ""
        a["still_open"] = (src in live_codes) if src and not src.startswith("group-") else None
    untracked = [e for e in (ops.get("sla") or []) if e["code"] not in tracked]
    data["untracked"] = {
        "critical": [e for e in untracked if e["severity"] == "critical"][:40],
        "warning": [e for e in untracked if e["severity"] == "warning"][:40],
        "counts": {"critical": sum(1 for e in untracked if e["severity"] == "critical"),
                   "warning": sum(1 for e in untracked if e["severity"] == "warning"),
                   "impact": sum(abs(e.get("impact") or 0) for e in untracked
                                 if e["severity"] in ("critical", "warning"))},
        "note": "Exceptions from the selected period that nobody has turned into an action yet. "
                "The action list itself is not automatic: an action exists only when someone decides "
                "to own it.",
    }
    data["action_keys"] = sorted(store.open_keys())
    if "error" not in ops:
        data["brief"] = brief.build(data, inv_data, ops, settings, meta, len(operations.DATA_GAPS))
    meta["query_ms"] = int((time.time() - t0) * 1000)
    data["meta"] = meta

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


_streams_cache = {}


def streams(company, week, location, mode="week"):
    """Procurement split into its streams: food and beverage, non-food, supplies,
    services and CAPEX. Read on demand, because it needs the item catalogue and the
    purchase postings in the general ledger as well as the value entries."""
    key = (company, week or "", location or "", mode)
    hit = _streams_cache.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
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

    critical_missing = [x for x in sources if x["status"] == "unavailable" and x.get("critical")]
    out = {
        "period": [c_from.isoformat(), c_to.isoformat()], "label": label, "mode": mode,
        "company": company, "location": location or "", "summary": summary,
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
            "filters": f"location={location or 'all'}",
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
    hit = _compare_cache.get(key)
    if hit and time.time() - hit[0] < 900:
        return hit[1]
    c_from, c_to, _, _, like_for_like, label = _period(week, mode)
    settings = store.get_settings()
    cur = period_rows(company, c_from, c_to, 60)
    current_total = comparisons.total(cur["rows"], location or None)

    specs, rolling_label = comparisons.specs(c_from, c_to, mode)
    periods = []
    for spec in specs:
        rows = period_rows(company, spec["from"], spec["to"], 3600)["rows"]
        periods.append({**spec, "from": spec["from"].isoformat(), "to": spec["to"].isoformat(),
                        "total": comparisons.total(rows, location or None)})
    rows = comparisons.build(current_total, periods, rolling_label)

    # ---- budget benchmark ----
    accounts = [a.strip() for a in (settings["budget"].get("cogs_accounts") or "").split(",") if a.strip()]
    budget = {"available": False, "reason": "No cost-of-goods accounts configured in Settings."}
    if accounts:
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
                     "/api/users": "users", "/api/password": "view"}


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
        payload = body if isinstance(body, bytes) else json.dumps(body, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
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

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
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
                self._send(200, dashboard(q.get("company") or config.BC_COMPANY, q.get("week"),
                                          q.get("location"), q.get("mode", "week")))
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
                                        q.get("location"), q.get("mode", "week")))
            elif url.path == "/api/comparisons":
                self._send(200, compare(q.get("company") or config.BC_COMPANY, q.get("week"),
                                        q.get("location"), q.get("mode", "week")))
            elif url.path == "/api/report":
                kind = q.get("type", "weekly")
                mode = q.get("mode", "week")
                full = dashboard(q.get("company") or config.BC_COMPANY, q.get("week"), q.get("location"), mode)
                if kind == "daily":
                    day = date.fromisoformat(q["day"]) if q.get("day") else date.today() - timedelta(days=1)
                    company = q.get("company") or config.BC_COMPANY
                    day_rows = period_rows(company, day, day, 600)["rows"]
                    loc = q.get("location")
                    if loc:
                        day_rows = [r for r in day_rows if r.get("Location_Code") == loc]
                    self._send(200, reports.daily(full, day_rows, day))
                else:
                    cmp_data = compare(q.get("company") or config.BC_COMPANY, q.get("week"), q.get("location"), mode)
                    self._send(200, reports.weekly(full, cmp_data))
            elif url.path == "/api/drill":
                self._send(200, drill(q.get("kind", "purchases"), q.get("key", "")))
            elif url.path == "/api/actions":
                self._send(200, {"actions": store.list_actions(), "statuses": store.STATUSES,
                                 "priorities": store.PRIORITIES})
            elif url.path == "/api/settings":
                self._send(200, store.get_settings())
            elif url.path == "/api/audit":
                self._send(200, {"audit": store.get_audit()})
            elif url.path == "/api/companies":
                self._send(200, {"companies": client.list_companies(), "default": config.BC_COMPANY})
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

    def do_POST(self):
        url = urlparse(self.path)
        body = self._body()
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
        needed = WRITE_PERMISSIONS.get(url.path) or ("actions" if url.path.startswith("/api/actions") else None)
        if needed and not auth.can(user, needed):
            self._send(403, {"error": f"Your role ({user['role']}) cannot change this."})
            return
        actor = user["username"]
        try:
            if url.path == "/api/actions":
                self._send(200, store.create_action(body, actor))
            elif url.path.startswith("/api/actions/"):
                updated = store.update_action(url.path.rsplit("/", 1)[-1], body, actor)
                self._send(200 if updated else 404, updated or {"error": "Action not found"})
            elif url.path == "/api/settings":
                self._send(200, store.save_settings(body, actor))
            else:
                self._send(404, {"error": "Not found"})
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
    if "--no-browser" not in sys.argv:
        threading.Timer(1.0, lambda: webbrowser.open(link)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")


if __name__ == "__main__":
    main()
