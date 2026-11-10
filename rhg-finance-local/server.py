"""Finance & Inventory Control Tower - local server (Phase 1).

Business Central is read only. Actions, thresholds and the audit trail are the
application's own and live under ./data.
"""
import access
import base64
import hmac
import gzip
import io
import html
import json
import re
import sys
import threading
import time
import traceback
import webbrowser
from calendar import monthrange
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, parse_qsl, urlencode, urlparse

import arap_notify
import xlsxpdf
import exportmail
import brand_report
import sales_report
import channels
import pricelist
import pl_memo
import cashloc
import kartloc
import supstock
import skuplan
import recipeuse
import menueng
import menucost
import chanprof
import today as todayloc
import unicorn_facts
import faloc
import recipients
import exmail
import itemstock
import shrinkage
import salestop
import salestop_report
import xlsxtable
import emcpriv
import sevenrooms
import srsim
import sevenrooms_report
import requests
import cost_report
import negstock
import allocation
import rcpt
import stockcount
import capital
import dq
import kpilib
import fx
import exreg
import salesrec
import billy
import billyrec
import assets
import auth
import brief
import config
import localdb
import memuse
import movement
import typology
import locintel
import invloc
import finloc
import executive
import fiscal
import cis_client
import fiscal_sales
import finance
import inventory
import recipes
import simphony
import store
import treasury
from concurrent.futures import ThreadPoolExecutor

from bc_client import BCClient, BCError

STATIC = config.BASE_DIR / "static"
STATIC_FILES = {"brand.css": "text/css; charset=utf-8", "logo.svg": "image/svg+xml", "logo.png": "image/png",
                "icon.svg": "image/svg+xml",
                "manifest.webmanifest": "application/manifest+json",
                "sw.js": "application/javascript; charset=utf-8",
                "select.js": "application/javascript; charset=utf-8",
                "select.css": "text/css; charset=utf-8",
                "tables.js": "application/javascript; charset=utf-8",
                "pulse.js": "application/javascript; charset=utf-8",
                "layout.js": "application/javascript; charset=utf-8",
                "recipients.js": "application/javascript; charset=utf-8",
                "login.html": "text/html; charset=utf-8"}
PUBLIC_PATHS = {"/login", "/login.html", "/api/login", "/api/session", "/brand.css", "/logo.svg", "/logo.png", "/icon.svg", "/favicon.ico", "/manifest.webmanifest", "/sw.js"}
WRITE_PERMISSIONS = {"/api/settings": "settings", "/api/actions": "actions", "/api/closing": "closing",
                     "/api/simphony/settings": "settings", "/api/simphony/test": "settings",
                     "/api/billy/settings": "settings", "/api/billy/test": "settings",
                     "/api/arap-notify/settings": "settings", "/api/arap-notify/test": "settings",
                     "/api/cost-report/settings": "settings", "/api/cost-report/job": "settings",
                     "/api/negative-report/settings": "settings", "/api/negative-report/job": "settings",
                     "/api/stockstop-report/settings": "settings", "/api/stockstop-report/job": "settings",
                     "/api/sevenrooms-report/settings": "settings", "/api/sevenrooms-report/job": "settings",
                     "/api/emcpriv/settings": "settings", "/api/emcpriv/check": "settings",
                     "/api/sevenrooms/settings": "settings", "/api/sevenrooms/test": "settings",
                     "/api/register/settings": "settings", "/api/register/run": "settings",
                     "/api/register/test": "settings", "/api/salesrec/settings": "settings",
                     "/api/kpi/mapping": "settings", "/api/dq/freeze": "settings", "/api/allocation": "settings",
                     "/api/receipts/config": "settings",
                     "/api/stockcount/upload": "actions", "/api/stockcount/delete": "actions",
                     "/api/sales/channel-profit/params": "settings",
                     "/api/brand-report/settings": "settings", "/api/brand-report/job": "settings",
                     "/api/sales-report/settings": "settings", "/api/sales-report/job": "settings",
                     "/api/pl-memo/settings": "settings", "/api/pl-memo/job": "settings",
                     "/api/recipients": "settings",
                     "/api/exceptions/email": "actions",
                     "/api/shrinkage/config": "settings", "/api/shrinkage/import": "settings",
                     "/api/shrinkage/count": "actions",
                     "/api/fiscal/fetch": "settings", "/api/fiscal/import": "settings",
                     "/api/fiscal/settings": "settings", "/api/fiscal/test": "settings", "/api/fiscal/probe": "settings",
                     "/api/users": "users", "/api/password": "view"}
client = BCClient()
_last, _last_lock = {}, threading.Lock()

DATA_GAPS = [
    {"kpi": "Exchange rates for the group view",
     "missing": "Currency Exchange Rates (page 483) is published as CurrencyExchangeRates; the group view reads it. "
                "Finance still has to confirm that the EUR rows are complete and approve the method.",
     "requirement": "Keep the EUR rate in Business Central for every day (or at least every month end), so the "
                    "result converts at the average rate and the balance sheet at the closing rate."},
    {"kpi": "Current and quick ratio",
     "missing": "Business Central does not say which loans (468, 46801), loans given (46802) and deposits (418*) "
                "fall due within twelve months.",
     "requirement": "Split those accounts into current and non-current (separate accounts, or a maturity schedule "
                    "Finance keeps), so current assets and current liabilities can be told apart."},
    {"kpi": "Who posted an entry, and when it was created or posted",
     "missing": "G/L Entries publishes no User ID, Created timestamp or Posting timestamp.",
     "requirement": "Extend the G_LEntries web service with User ID, SystemCreatedAt and SystemCreatedBy, "
                    "or publish G/L Registers (page 116), which carries User ID and creation time."},
    {"kpi": "Approval status and approver on invoices and journals",
     "missing": "Approval Entries is not published.",
     "requirement": "Publish 'Approval Entries' (page 658) with Document No., Approver ID, Status and Date-Time."},
    {"kpi": "Bank reconciliation status and unreconciled bank items",
     "missing": "No bank reconciliation page is published; bank ledger entries carry Open but no statement link.",
     "requirement": "Publish 'Bank Acc. Reconciliation' and 'Bank Acc. Reconciliation Lines'."},
    {"kpi": "Physical inventory counts and count variances",
     "missing": "No physical inventory journal or posted count entries are published.",
     "requirement": "Publish 'Phys. Inventory Ledger Entries' (page 5807) and the physical inventory journal."},
    {"kpi": "Intercompany matching between entities",
     "missing": "No IC partner ledger is published; only IC_Partner_Code on vendor and customer entries.",
     "requirement": "Publish 'IC Outbox/Inbox Transactions' or expose IC Partner balances per company."},
    {"kpi": "Cash-flow forecast, payment plans and expected receipt dates",
     "missing": "No cash flow forecast entries or payment schedules are published.",
     "requirement": "Publish 'Cash Flow Forecast Entries' (page 851) if the module is in use."},
    {"kpi": "Fixed asset register detail (class, custodian, useful life)",
     "missing": "Only FA ledger entries are exposed; the asset card fields are not.",
     "requirement": "Publish 'Fixed Asset List' with class, location, custodian and depreciation book."},
]


# ------------------------------------------------------------------ periods
def parse_range(anchor, today=None):
    """(from, to) of a chosen range "YYYY-MM-DD..YYYY-MM-DD": in order, never past today, at most three years."""
    today = today or date.today()
    a_txt, _, b_txt = (anchor or "").partition("..")
    a = date.fromisoformat(a_txt.strip()) if a_txt.strip() else today.replace(day=1)
    b = date.fromisoformat(b_txt.strip()) if b_txt.strip() else today
    if b < a:
        a, b = b, a
    b = min(b, today)
    a = min(a, b)
    if (b - a).days > 3 * 366:
        raise ValueError("A chosen range can be at most three years.")
    return a, b


def periods(anchor, mode, today=None):
    today = today or date.today()
    if mode == "range":
        # a chosen range: against the same number of days just before it, and the same dates last year
        c_from, c_to = parse_range(anchor, today)
        days = (c_to - c_from).days + 1
        p_to = c_from - timedelta(days=1)
        p_from = p_to - timedelta(days=days - 1)
        running = c_to >= today
        same_year = c_from.year == c_to.year
        label = (f"{c_from:%d %b}" + ("" if same_year else f" {c_from.year}") + f" – {c_to:%d %b %Y}"
                 + (" to date" if running else ""))
        method = f"Chosen dates ({days} days) against the {days} days just before."
        try:
            ly_from, ly_to = c_from.replace(year=c_from.year - 1), c_to.replace(year=c_to.year - 1)
        except ValueError:
            ly_from = c_from.replace(year=c_from.year - 1, day=28) if c_from.month == 2 else c_from.replace(year=c_from.year - 1)
            ly_to = c_to.replace(year=c_to.year - 1, day=28) if c_to.month == 2 else c_to.replace(year=c_to.year - 1)
        return c_from, c_to, p_from, p_to, ly_from, ly_to, running, label, method
    a = date.fromisoformat(anchor[:10]) if anchor else today
    if mode == "ytd":
        c_from, running = date(a.year, 1, 1), a.year == today.year
        c_to = today if running else date(a.year, 12, 31)
        p_from = date(a.year - 1, 1, 1)
        try:
            p_to = c_to.replace(year=c_to.year - 1)
        except ValueError:
            p_to = date(c_to.year - 1, 2, 28)
        label = f"{a.year}" + (" to date" if running else "")
        method = "Year to date against the same days of the previous year."
    elif mode == "quarter":
        q = (a.month - 1) // 3
        c_from = date(a.year, q * 3 + 1, 1)
        running = c_from <= today <= date(a.year, q * 3 + 3, monthrange(a.year, q * 3 + 3)[1])
        c_to = today if running else date(a.year, q * 3 + 3, monthrange(a.year, q * 3 + 3)[1])
        p_from = date(c_from.year - (1 if c_from.month <= 3 else 0), (c_from.month - 3) or 12, 1)
        p_to = min(p_from + timedelta(days=(c_to - c_from).days),
                   p_from.replace(day=monthrange(p_from.year, p_from.month)[1]))
        label = f"Q{q + 1} {a.year}" + (" to date" if running else "")
        method = "Quarter to date against the same number of days of the previous quarter."
    else:
        c_from = a.replace(day=1)
        running = (c_from.year, c_from.month) == (today.year, today.month)
        month_end = c_from.replace(day=monthrange(c_from.year, c_from.month)[1])
        c_to = today if running else month_end
        p_from = (c_from - timedelta(days=1)).replace(day=1)
        p_to = min(p_from + timedelta(days=(c_to - c_from).days),
                   p_from.replace(day=monthrange(p_from.year, p_from.month)[1]))
        label = f"{c_from:%B %Y}" + (" to date" if running else "")
        method = "Month to date against the same number of days of the previous month."
    try:
        ly_from, ly_to = c_from.replace(year=c_from.year - 1), c_to.replace(year=c_to.year - 1)
    except ValueError:
        ly_from, ly_to = date(c_from.year - 1, 3, 1), date(c_to.year - 1, 2, 28)
    return c_from, c_to, p_from, p_to, ly_from, ly_to, running, label, method


# ------------------------------------------------------------------ dashboard
def _safe(sources, label, call, fallback, required=True):
    """Runs one Business Central read. A source that fails or times out is reported as
    unavailable and the rest of the screen still loads."""
    t = time.time()
    try:
        value = call()
        sources.append({"source": label, "status": "ok", "ms": int((time.time() - t) * 1000)})
        return value
    except BCError as exc:
        print(f"[{datetime.now():%H:%M:%S}] {label}: {exc}", file=sys.stderr)
        sources.append({"source": label, "status": "unavailable", "required": required,
                        "ms": int((time.time() - t) * 1000), "reason": str(exc)[:300]})
        return fallback


_dash_cache, _dash_building = {}, set()
_dash_lock = threading.Lock()


def dashboard(company, anchor, mode, dimension, compare="prior"):
    """Answers from what is already held and reads the new figures behind the screen."""
    key = (company, anchor or "", mode, dimension or "", compare)
    if not config.SERVE_WHILE_REFRESHING:
        return _dashboard_build(company, anchor, mode, dimension, compare)
    with _dash_lock:
        held = _dash_cache.get(key)
        age = time.time() - held[0] if held else None
        if held and age < 60:
            return held[1]
        if held and key not in _dash_building:
            _dash_building.add(key)
            threading.Thread(target=_refresh_dashboard, args=(key, company, anchor, mode, dimension, compare),
                             daemon=True).start()
    if held:
        payload = dict(held[1])
        payload["meta"] = {**payload["meta"], "serving": "held figures, refreshing now",
                           "held_seconds": int(age)}
        return payload
    out = _dashboard_build(company, anchor, mode, dimension, compare)
    with _dash_lock:
        _dash_cache[key] = (time.time(), out)
        if len(_dash_cache) > 6:
            for k, _ in sorted(_dash_cache.items(), key=lambda kv: kv[1][0])[:len(_dash_cache) - 6]:
                _dash_cache.pop(k, None)
    return out


def _refresh_dashboard(key, company, anchor, mode, dimension, compare="prior"):
    try:
        out = _dashboard_build(company, anchor, mode, dimension, compare)
        with _dash_lock:
            _dash_cache[key] = (time.time(), out)
    except Exception as exc:                                     # noqa: BLE001
        print(f"[{datetime.now():%H:%M:%S}] background refresh failed: {exc}")
    finally:
        with _dash_lock:
            _dash_building.discard(key)


def _dashboard_build(company, anchor, mode, dimension, compare="prior", remember=True):
    t0 = time.time()
    settings = store.get_settings()
    c_from, c_to, p_from, p_to, ly_from, ly_to, running, label, method = periods(anchor, mode)
    age = max(config.REFRESH_SECONDS - 10, 30) if running else 900

    sources = []
    cur = _safe(sources, "Chart of accounts, current period",
                lambda: client.accounts(company, c_from.isoformat(), c_to.isoformat(), dimension, age), [])
    prev = _safe(sources, "Chart of accounts, comparison period",
                 lambda: client.accounts(company, p_from.isoformat(), p_to.isoformat(), dimension, 900), [], False)
    ly = _safe(sources, "Chart of accounts, last year",
               lambda: client.accounts(company, ly_from.isoformat(), ly_to.isoformat(), dimension, 3600), [], False)
    budget = _safe(sources, "Budget entries",
                   lambda: client.budget(company, c_from.isoformat(), c_to.isoformat(),
                                         settings["budget"].get("name") or ""), [], False)

    rules = {**finance.DEFAULT_ACCOUNT_RULES,
             **{k: v for k, v in settings["accounts"].items() if v}}
    kmap = kpilib.mapping(company)
    pl = finance.profit_and_loss(cur, prev, ly, budget, rules, mapping=kmap)
    if dimension:
        # a department alone: only what is posted on it; central costs are not in it (allocation.py)
        pl["partial"] = True
        pl["note"] = ("Partial: only what is posted on " + ", ".join(dimension.split(",")) + ". Head office, "
                      "marketing, IT, the central kitchen and warehouse are on their own departments, so this is a "
                      "result before central costs, not the EBITDA of the restaurant; its Contribution needs the rule in "
                      "Settings → Central costs to the restaurants. " + (pl.get("note") or ""))
    bs = finance.balance_sheet(cur, prev, ly, rules, result_rows=cur)
    today = date.today()
    ap = finance.payables(_safe(sources, "Open supplier entries",
                                lambda: client.open_vendor_entries(company), []), today, settings)
    ar = finance.receivables(_safe(sources, "Open customer entries",
                                   lambda: client.open_customer_entries(company), []), today, settings)
    bank = _safe(sources, "Bank ledger entries",
                 lambda: client.bank_entries(company, c_from.isoformat(), c_to.isoformat()), [], False)
    cash = finance.cash_position(cur, bank, settings, kmap)
    inv_placeholder = {"loaded": False, "exceptions": [],
                       "note": "Inventory is read when its screen is opened: it needs four snapshots of "
                               "15,000 item cards, which is too slow to do on every refresh."}
    gl = {"entries": [], "count": 0, "exceptions": [], "threshold": settings["thresholds"]["manual_journal"],
          "loaded": False,
          "note": "The ledger holds tens of thousands of entries a day, so it is read separately when the "
                  "G/L screen is opened, over a short window and capped."}

    exceptions = ap["exceptions"] + ar["exceptions"] + _statement_exceptions(pl, bs, cash, settings)
    rank = {"critical": 0, "warning": 1, "info": 2}
    exceptions.sort(key=lambda e: (rank.get(e["severity"], 3), -abs(e.get("impact") or 0)))

    meta = {"company": company, "dimension": dimension or "", "mode": mode, "period_label": label,
            "current": [c_from.isoformat(), c_to.isoformat()], "prior": [p_from.isoformat(), p_to.isoformat()],
            "last_year": [ly_from.isoformat(), ly_to.isoformat()], "running": running, "methodology": method,
            # which of the two bases the executive figures are compared against
            "compare": compare,
            "compare_range": ([ly_from.isoformat(), ly_to.isoformat()] if compare == "last_year"
                              else [p_from.isoformat(), p_to.isoformat()]),
            "compare_label": ("the same dates last year" if compare == "last_year"
                              else "the period before this one"),
            "compare_rule": ("Both sides cover the same calendar dates, so a part-month is compared with "
                             "the same part of last year, never with a full month. February 29 falls back "
                             "to February 28."
                             if compare == "last_year" else method),
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "currency": company_currency(company, settings),
            "currency_note": "Amounts are in the company's own posting currency, as Business Central "
                             "stores them. Nothing is converted here; the group view converts.",
            "refresh_seconds": config.REFRESH_SECONDS, "read_only": True}

    data = {"pl": pl, "balance_sheet": bs, "payables": ap, "receivables": ar, "cash": cash, "gl": gl,
            "exceptions": exceptions, "gaps": DATA_GAPS, "settings": settings, "sources": sources,
            "inventory": inv_placeholder,
            # what is in the Exceptions register already (found by a control or tracked by hand): the screens' Track
            "action_keys": sorted(exreg.tracked_keys(company)), "meta": meta}
    data["kpis"] = _headline(pl, bs, ap, ar, cash, exceptions)
    data["brief"] = brief.build(data, settings, meta)
    meta["query_ms"] = int((time.time() - t0) * 1000)
    if remember:
        with _last_lock:
            _last["data"] = data
            _last["company"] = company
    return data


def gl_screen(company, anchor, mode, window_days=None, threshold=None, remember=True):
    """G/L control, loaded on demand: a short window, capped rows, so BC is not asked to
    scan a month of a ledger that grows by ~38,000 entries a day."""
    settings = store.get_settings()
    c_from, c_to, *_ = periods(anchor, mode)
    days = int(window_days or config.GL_WINDOW_DAYS)
    win_from = max(c_from, c_to - timedelta(days=days - 1))
    th = int(threshold or settings["thresholds"]["manual_journal"])
    closed_before = None
    if settings.get("period_closed_before"):
        try:
            closed_before = date.fromisoformat(settings["period_closed_before"])
        except ValueError:
            closed_before = None
    sources = []
    entries = _safe(sources, "General ledger entries",
                    lambda: client.material_gl_entries(company, win_from.isoformat(), c_to.isoformat(),
                                                       th, config.GL_MAX_ROWS), [])
    out = finance.gl_control(entries, settings, win_from.isoformat(), c_to.isoformat(), closed_before)
    out.update({"loaded": True, "window": [win_from.isoformat(), c_to.isoformat()], "days": days,
                "threshold": th, "capped": len(entries) >= config.GL_MAX_ROWS, "sources": sources})
    if not remember:
        return out
    with _last_lock:
        d = _last.get("data")
        if d is not None:
            d["gl"] = out
            existing = {e["code"] for e in d["exceptions"]}
            d["exceptions"] = d["exceptions"] + [e for e in out["exceptions"] if e["code"] not in existing]
    return out


def _inventory_gl_balance(company, c_to, dimension, settings):
    """Balance of the inventory accounts named in Settings, for the reconciliation."""
    wanted = [a.strip() for a in (settings["inventory"].get("accounts") or "").split(",") if a.strip()]
    if not wanted:
        return None, "No inventory accounts are configured in Settings, so the G/L comparison is not shown."
    rows = client.accounts(company, c_to.replace(month=1, day=1).isoformat(), c_to.isoformat(), dimension, 900)
    total, seen = 0.0, []
    for r in rows:
        if str(r.get("No")) in wanted:
            total += float(r.get("Balance_at_Date") or 0)
            seen.append(str(r.get("No")))
    missing = [a for a in wanted if a not in seen]
    note = f"Accounts {', '.join(seen) or 'none matched'}" + (f"; not found: {', '.join(missing)}" if missing else "")
    return total, note


def inventory_screen(company, anchor, mode, dimension, location=None):
    """Inventory, read on demand: four snapshots of the item cards, then compared."""
    settings = store.get_settings()
    c_from, c_to, *_ = periods(anchor, mode)
    th = settings["thresholds"]
    sources = []
    window_days = max(int(th.get("stock_dead_days") or 90), 90)
    win_from = (c_to - timedelta(days=window_days)).isoformat()

    # every read is independent, so they run side by side instead of one after another
    def ledger():
        kind = client.hold_item_ledger(company, win_from, c_to.isoformat(), location)
        return kind
    jobs = {"cur": lambda: client.item_snapshot(company, c_to.isoformat(), location)}
    day_marks = sorted({int(th["stock_monitor_days"]), int(th["stock_slow_days"]), int(th["stock_dead_days"])})
    for days in day_marks:
        as_of = (c_to - timedelta(days=days)).isoformat()
        jobs[f"h{days}"] = (lambda a=as_of: client.item_snapshot(company, a, location, 86400))
    jobs["gl"] = lambda: _inventory_gl_balance(company, c_to, dimension, settings)
    jobs["ledger"] = ledger
    started = time.time()
    results = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {k: pool.submit(fn) for k, fn in jobs.items()}
        for k, fut in futures.items():
            try:
                results[k] = (fut.result(), None)
            except BCError as exc:
                results[k] = (None, exc)

    def took():
        return int((time.time() - started) * 1000)
    cur_rows, err = results["cur"]
    sources.append({"source": "Item cards, stock on hand", "status": "unavailable" if err else "ok",
                    "required": True, **({"reason": str(err)[:300]} if err else {})})
    cur = inventory.snapshot(cur_rows or [])
    history = {}
    for days in day_marks:
        rows, err = results[f"h{days}"]
        sources.append({"source": f"Item cards, {days} days earlier", "status": "unavailable" if err else "ok",
                        "required": False, **({"reason": str(err)[:300]} if err else {})})
        history[days] = inventory.snapshot(rows or [])
    gl_balance, gl_note = None, ""
    gl, err = results["gl"]
    if err:
        sources.append({"source": "Inventory accounts in the G/L", "status": "unavailable", "reason": str(err)[:200]})
    else:
        gl_balance, gl_note = gl
    # the posted movements behind the snapshots: a balance that has not changed does not
    # mean nothing moved, and the ledger is the only place that says which it is. They
    # are summed straight from the local file, a batch at a time.
    movements = None
    kind, err = results["ledger"]
    if err:
        sources.append({"source": "Posted stock movements", "status": "unavailable", "reason": str(err)[:200]})
    else:
        movements = inventory.movement_summary(localdb.iter_rows(kind, company, win_from, c_to.isoformat()))
        sources.append({"source": f"Posted stock movements, last {window_days} days", "status": "verified",
                        "rows": sum(m["entries"] for m in movements.values())})
    inv = inventory.overview(cur, history, settings, gl_balance, location or "", movements, c_to)
    inv["movement_basis"] = ("Posted ledger movements: an item counts as moving when something left it "
                             "or arrived, transfers included. A sales return receipt is a sale with a "
                             "positive quantity and counts as stock coming back in."
                             if movements is not None else
                             "Snapshot differences only: the ledger could not be read, so an item that "
                             "was shipped out and replaced looks untouched.")
    inv["gl_note"] = gl_note
    inv["as_of"] = c_to.isoformat()
    inv["sources"] = sources
    inv["exceptions"] = inventory.exceptions(inv, settings)
    inv["loaded"] = True
    inv["ms"] = took()
    with _last_lock:
        d = _last.get("data")
        if d is not None:
            d["inventory"] = inv
            existing = {e["code"] for e in d["exceptions"]}
            d["exceptions"] = d["exceptions"] + [e for e in inv["exceptions"] if e["code"] not in existing]
    return inv


# The inventory reads several snapshots and a quarter of stock movements, which can take
# longer than the proxy in front of the tower waits for one answer. So the read runs in
# the background and the screen asks again every few seconds until it is ready.
_inv_jobs, _inv_lock = {}, threading.Lock()
INV_KEEP_SECONDS = 1800


def inventory_request(company, anchor, mode, dimension, location=None, refresh=False):
    key = (company, anchor or "", mode or "month", dimension or "", location or "")
    now = time.time()
    with _inv_lock:
        job = _inv_jobs.get(key)
        if job and job["state"] == "running":
            return {"loaded": False, "pending": True, "elapsed": int(now - job["started"])}
        if job and job["state"] == "done" and not refresh and now - job["finished"] < INV_KEEP_SECONDS:
            return job["result"]
        if job and job["state"] == "failed" and not refresh:
            _inv_jobs.pop(key, None)
            return {"loaded": False, "error": job["error"]}
        _inv_jobs[key] = {"state": "running", "started": now}
        for k in [k for k, j in _inv_jobs.items() if j["state"] != "running"
                  and now - j.get("finished", now) > INV_KEEP_SECONDS]:
            _inv_jobs.pop(k, None)

    def run():
        try:
            result = inventory_screen(company, anchor, mode, dimension, location)
            state = {"state": "done", "result": result}
        except Exception as exc:                                 # noqa: BLE001
            traceback.print_exc()
            state = {"state": "failed", "error": f"The inventory could not be read: {str(exc)[:300]}"}
        with _inv_lock:
            _inv_jobs[key] = dict(state, started=now, finished=time.time())
    threading.Thread(target=run, daemon=True, name="inventory").start()
    return {"loaded": False, "pending": True, "elapsed": 0}


# ------------------------------------------------------------------ sales (Simphony)
_latest_cache = {}


def _latest(loc_ref):
    hit = _latest_cache.get(loc_ref)
    if hit and time.time() - hit[0] < 900:
        return hit[1]
    value = simphony.latest_business_date(loc_ref)
    _latest_cache[loc_ref] = (time.time(), value)
    return value


SALES_KEYS = ("net", "discounts", "gross", "checks", "guests", "voids", "returns", "service", "tax",
              "open_checks", "open_value", "error_corrects")


def sales_totals(rows):
    """Sales: the totals of the rows by currency."""
    totals = {}
    for r in rows:
        t = totals.setdefault(r["currency"] or "?", {"cur": dict.fromkeys(SALES_KEYS, 0),
                                                       "prev": dict.fromkeys(SALES_KEYS, 0), "locations": 0})
        t["locations"] += 1
        for k in SALES_KEYS:
            t["cur"][k] += r["cur"].get(k) or 0
            t["prev"][k] += r["prev"].get(k) or 0
    for t in totals.values():
        c, p = t["cur"], t["prev"]
        t["change_pct"] = (c["net"] - p["net"]) / abs(p["net"]) * 100 if p["net"] else None
        t["avg_check"] = c["net"] / c["checks"] if c["checks"] else None
        t["avg_guest"] = c["net"] / c["guests"] if c["guests"] else None
    return totals


def sales_cut(body, user, company):
    """Sales for a person limited to some locations: their rows (access.filter_payload), the totals and the days
    summed again from them, so the screen shows their restaurants as the whole of it."""
    out = access.filter_payload(body, user, company, scope_expand)
    if out is body or not isinstance(out.get("locations"), list) or not body.get("available"):
        return out
    rows = out["locations"]
    by_day = {}
    for r in rows:
        for d, net in (r.get("daily") or {}).items():
            day = by_day.setdefault(d, {"date": d, "net": {}, "checks": 0, "guests": 0})
            day["net"][r["currency"] or "?"] = day["net"].get(r["currency"] or "?", 0) + net
            cg = (r.get("daily_cg") or {}).get(d) or [0, 0]
            day["checks"] += cg[0]
            day["guests"] += cg[1]
    totals = sales_totals(rows)
    return dict(out, totals=totals, days=sorted(by_day.values(), key=lambda d: d["date"]), cut=True,
                currency=max(totals, key=lambda k: totals[k]["cur"]["net"]) if totals else body.get("currency"))


def sales_screen(anchor, mode, compare="prior"):
    """Net sales, checks and guests by location from Simphony, against the period before
    or the same dates last year."""
    if not simphony.info()["configured"]:
        return {"available": False, "configured": False,
                "error": "The Simphony connection is not set. Fill it in under Settings → Simphony sales."}
    c_from, c_to, p_from, p_to, ly_from, ly_to, running, label, method = periods(anchor, mode)
    if compare == "last_year":
        p_from, p_to = ly_from, ly_to
        method = "Same dates of the previous year."
    t0 = time.time()
    locs = [l for l in simphony.locations() if l.get("active", True)]
    fallback = (store.get_settings().get("group") or {}).get("reporting_currency") or "ALL"
    reg = {r["ref"]: r for r in locintel.registry()}
    for l in locs:
        # the registry in Settings wins (SALT Prishtina is EUR, Simphony gives no currency);
        # "L" and "LEK" are lek, so Vesper is no longer a currency of its own
        given = locintel.norm_currency(l["currency"])
        l["currency_given"] = bool(given) or bool(reg.get(l["locRef"], {}).get("currency"))
        l["currency"] = (reg.get(l["locRef"], {}).get("currency")) or given or fallback
    mapping = simphony.loc_map()
    for ref, r in reg.items():
        if r.get("bc") and ref not in mapping:
            mapping[ref] = r["bc"]
    latest = {}
    for loc, (value, err) in in_parallel_jobs({l["locRef"]: (lambda r=l["locRef"]: _latest(r)) for l in locs}).items():
        latest[loc] = value if not err else None

    def dates(a, b):
        out, d = [], a
        while d <= b:
            out.append(d.isoformat())
            d += timedelta(days=1)
        return out
    # Like for like: the current period stops at the last business day Simphony has
    # closed, so the period before is cut to the same number of days.
    closed = [v for v in latest.values() if v]
    if closed:
        last_closed = date.fromisoformat(max(closed))
        if last_closed < c_to:
            c_to = max(c_from, last_closed)
            p_to = min(p_to, p_from + (c_to - c_from))
    cur_days, prev_days = dates(c_from, c_to), dates(p_from, p_to)
    jobs = {}
    for l in locs:
        last = latest.get(l["locRef"])
        for d in cur_days + prev_days:
            if last and d > last:
                continue                         # that business day has not been closed yet
            jobs[(l["locRef"], d)] = (lambda r=l["locRef"], dd=d, lt=last: simphony.daily_totals(r, dd, lt))
    results = in_parallel_jobs(jobs, workers=4)
    simphony.flush_days()
    errors = sorted({str(err)[:200] for _, (_, err) in results.items() if err})

    rows, by_day = [], {}
    keys = ("net", "discounts", "gross", "checks", "guests", "voids", "returns", "service", "tax",
            "open_checks", "open_value", "error_corrects")
    for l in locs:
        cur = dict.fromkeys(keys, 0)
        prev = dict.fromkeys(keys, 0)
        read = missing = 0
        daily_cur, daily_prev, daily_cg = {}, {}, {}           # for Location intelligence's lines
        for d in cur_days + prev_days:
            value, err = results.get((l["locRef"], d), (None, None))
            if value is None:
                if (l["locRef"], d) in jobs:
                    missing += 1
                continue
            fig = simphony.day_figures(value.get("rvcs") or [])
            target = cur if d in cur_days else prev
            for k in keys:
                target[k] += fig[k]
            (daily_cur if d in cur_days else daily_prev)[d] = round(fig["net"])
            if d in cur_days:
                daily_cg[d] = [fig["checks"], fig["guests"]]
                read += 1
                day = by_day.setdefault(d, {"date": d, "net": {}, "checks": 0, "guests": 0})
                day["net"][l["currency"] or "?"] = day["net"].get(l["currency"] or "?", 0) + fig["net"]
                day["checks"] += fig["checks"]
                day["guests"] += fig["guests"]
        if not read and not cur["net"] and not prev["net"]:
            continue                             # a location that sold nothing in either period
        rg = reg.get(l["locRef"]) or {}
        rows.append({"locRef": l["locRef"], "name": l["name"], "currency": l["currency"],
                     "reg_name": rg.get("name") or "", "brand": rg.get("brand") or "", "seasonal": bool(rg.get("seasonal")),
                     "company": rg.get("company") or "", "in_registry": bool(rg),
                     "daily": daily_cur, "daily_prev": daily_prev, "daily_cg": daily_cg,
                     "bc": mapping.get(l["locRef"], ""), "latest": latest.get(l["locRef"]),
                     "days_read": read, "days_missing": missing, "cur": cur, "prev": prev,
                     "change_pct": ((cur["net"] - prev["net"]) / abs(prev["net"]) * 100) if prev["net"] else None,
                     "avg_check": cur["net"] / cur["checks"] if cur["checks"] else None,
                     "avg_guest": cur["net"] / cur["guests"] if cur["guests"] else None,
                     "discount_pct": cur["discounts"] / cur["gross"] * 100 if cur["gross"] else None})
    rows.sort(key=lambda r: -r["cur"]["net"])
    totals = sales_totals(rows)
    main_currency = max(totals, key=lambda k: totals[k]["cur"]["net"]) if totals else ""
    return {
        "available": True, "configured": True,
        "period": {"from": c_from.isoformat(), "to": c_to.isoformat(), "label": label, "running": running},
        "prior": {"from": p_from.isoformat(), "to": p_to.isoformat(), "method": method,
                  "label": "same dates last year" if compare == "last_year" else "the period before"},
        "locations": rows, "totals": totals, "currency": main_currency, "limits": locintel.thresholds(),
        "days": sorted(by_day.values(), key=lambda d: d["date"]),
        "errors": errors, "calls": len(jobs), "ms": int((time.time() - t0) * 1000),
        "definitions": {
            "net": "Net sales as Simphony totals them (netSlsTtl): after item and subtotal discounts, "
                   "before service charges. VAT is included (the tax collected is inside it).",
            "checks": "Checks counted by Simphony for the business day (chkCnt). Guests are the covers "
                      "entered at the POS (gstCnt), so they are only as good as the entry.",
            "open": "Checks rung on the day and still open: sales that are not yet final.",
            "currency": "Each location reports in its own currency; totals are kept apart by currency "
                        "and never converted. Where Simphony gives no currency for a location, the tower's "
                        "default currency is assumed.",
            "period": "The current period stops at the last business day Simphony has closed, and the "
                      "period before is cut to the same number of days, so the comparison is like for like.",
            "guests": "Average per guest is only meaningful where covers are entered at the POS; the "
                      "screen shows how many checks carry a guest count.",
        },
    }


def in_parallel_jobs(jobs, workers=6):
    """{key: callable} -> {key: (value, error)}; a failed read never stops the others."""
    out = {}
    if not jobs:
        return out
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {k: pool.submit(fn) for k, fn in jobs.items()}
        for k, f in futures.items():
            try:
                out[k] = (f.result(), None)
            except Exception as exc:                             # noqa: BLE001
                out[k] = (None, exc)
    return out


_sales_jobs, _sales_lock = {}, threading.Lock()


def sales_request(anchor, mode, refresh=False, compare="prior"):
    """Runs the Simphony read in the background, like the inventory: the first read of a
    period is one call per location and day, which can outlast the proxy."""
    compare = "last_year" if compare == "last_year" else "prior"
    key = (anchor or "", mode or "month", compare)
    now = time.time()
    with _sales_lock:
        job = _sales_jobs.get(key)
        if job and job["state"] == "running":
            return {"pending": True, "elapsed": int(now - job["started"])}
        if job and job["state"] == "done" and not refresh and now - job["finished"] < 600:
            return job["result"]
        if job and job["state"] == "failed" and not refresh:
            _sales_jobs.pop(key, None)
            return {"available": False, "configured": True, "error": job["error"]}
        _sales_jobs[key] = {"state": "running", "started": now}

    def run():
        try:
            result = sales_screen(anchor, mode, compare)
            state = {"state": "done", "result": result}
        except simphony.SimphonyError as exc:
            state = {"state": "failed", "error": str(exc)}
        except Exception as exc:                                 # noqa: BLE001
            traceback.print_exc()
            state = {"state": "failed", "error": f"The sales could not be read: {str(exc)[:300]}"}
        with _sales_lock:
            _sales_jobs[key] = dict(state, started=now, finished=time.time())
    threading.Thread(target=run, daemon=True, name="sales").start()
    return {"pending": True, "elapsed": 0}


_summary_cache = {}
_summary_lock = threading.Lock()
COGS_DAYS = 90


def stock_summary(company, anchor, mode):
    """The stock position for finance: value by location, the inventory accounts against
    the items posted to them, and days of inventory. Company-wide, whatever location or
    department is selected elsewhere, because the accounts are company-wide."""
    settings = store.get_settings()
    c_from, c_to, *_ = periods(anchor, mode)
    opening = c_from - timedelta(days=1)
    settled = c_to < date.today()
    key = (company, c_from.isoformat(), c_to.isoformat(),
           json.dumps(settings.get("inventory"), sort_keys=True))
    with _summary_lock:
        hit = _summary_cache.get(key)
        if hit and time.time() - hit[0] < (3600 if settled else 600):
            return hit[1]
    t0, sources = time.time(), []
    age = 86400 if settled else 900

    locations = _safe(sources, "Locations", lambda: client.locations(company), [])
    jobs = {
        "cards": ("Item cards at the period end, all locations",
                  lambda: client.item_snapshot(company, c_to.isoformat(), None, age)),
        "accounts": ("Inventory accounts for the period",
                     lambda: client.accounts(company, c_from.isoformat(), c_to.isoformat(), None, age)),
        "cogs": (f"Cost of goods sold, last {COGS_DAYS} days",
                 lambda: client.accounts(company, (c_to - timedelta(days=COGS_DAYS - 1)).isoformat(),
                                         c_to.isoformat(), None, age)),
    }
    for loc in locations:
        jobs[f"close::{loc}"] = (None, lambda l=loc: client.stock_by_location(company, l, c_to.isoformat(), age))
        jobs[f"open::{loc}"] = (None, lambda l=loc: client.stock_by_location(company, l, opening.isoformat(), 86400))
    got, failed = {}, []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {name: pool.submit(call) for name, (_, call) in jobs.items()}
        for name, fut in futures.items():
            label = jobs[name][0]
            t = time.time()
            try:
                got[name] = fut.result()
                if label:
                    sources.append({"source": label, "status": "ok", "ms": int((time.time() - t) * 1000)})
            except BCError as exc:
                got[name] = None
                if label:
                    sources.append({"source": label, "status": "unavailable", "reason": str(exc)[:300]})
                else:
                    failed.append(name.split("::", 1)[1])
    if locations:
        sources.append({"source": f"Stock by location, {len(locations)} locations at two dates",
                        "status": "unavailable" if failed else "ok",
                        "reason": ("Not read: " + ", ".join(sorted(set(failed)))) if failed else ""})
    if got.get("cards") is None or got.get("accounts") is None:
        return {"available": False, "sources": sources,
                "error": "Data unavailable or not verified in Business Central."}

    cards = inventory.snapshot(got["cards"])
    loc_close = {l: got[f"close::{l}"] for l in locations if got.get(f"close::{l}") is not None}
    loc_open = {l: got[f"open::{l}"] for l in locations if got.get(f"open::{l}") is not None}
    out = inventory.finance_summary(cards, loc_close, loc_open, got["accounts"], got.get("cogs") or [],
                                    settings, c_to.isoformat(), opening.isoformat(), COGS_DAYS)
    out.update({"available": True, "company": company, "period": [c_from.isoformat(), c_to.isoformat()],
                "settled": settled, "sources": sources, "missing_locations": sorted(set(failed)),
                "ms": int((time.time() - t0) * 1000)})
    with _summary_lock:
        _summary_cache[key] = (time.time(), out)
        if len(_summary_cache) > 6:
            for k, _ in sorted(_summary_cache.items(), key=lambda kv: kv[1][0])[:2]:
                _summary_cache.pop(k, None)
    return out


def closing_screen(company, anchor, mode):
    """Month-end checklist: the task state is the application's own, the automatic checks
    are read from BC so a task cannot be ticked while the underlying problem is open."""
    settings = store.get_settings()
    c_from, c_to, *_ = periods(anchor, mode)
    period = f"{c_from:%Y-%m}"
    tasks = store.closing_tasks(company, period, settings["owners"])
    with _last_lock:
        d = _last.get("data") or {}
    checks = []
    ap, ar = d.get("payables"), d.get("receivables")
    inv = d.get("inventory")
    if ap:
        checks.append({"check": "Overdue payables", "value": f"{ap['overdue']:,.0f}",
                       "status": "warning" if ap["overdue"] else "ok",
                       "detail": "Open supplier entries past their due date."})
    if ar:
        checks.append({"check": "Overdue receivables", "value": f"{ar['overdue']:,.0f}",
                       "status": "warning" if ar["overdue"] else "ok",
                       "detail": "Open customer entries past their due date."})
    if inv and inv.get("loaded"):
        neg = inv["totals"]["negative_items"]
        checks.append({"check": "Negative inventory", "value": str(neg),
                       "status": "critical" if neg else "ok",
                       "detail": "Items with a negative quantity on hand."})
        rec = inv.get("reconciliation")
        if rec:
            checks.append({"check": "Inventory against the G/L", "value": f"{rec['difference']:,.0f}",
                           "status": "warning" if abs(rec["difference"]) >= settings["thresholds"]["inventory_difference"] else "ok",
                           "detail": rec["method"]})
    else:
        checks.append({"check": "Inventory checks", "value": "not read",
                       "status": "info", "detail": "Open the Inventory screen once so these checks can run."})
    bs = d.get("balance_sheet")
    if bs:
        checks.append({"check": "Assets less liabilities and equity", "value": f"{bs['totals']['difference']:,.0f}",
                       "status": "warning" if abs(bs["totals"]["difference"]) >= settings["thresholds"]["materiality"] else "ok",
                       "detail": "The result accumulated in the income statement accounts is already "
                                 "carried into equity, so this line should now be nil."})
    done = sum(1 for t in tasks if t["status"] in ("Completed", "Reviewed"))
    return {"company": company, "period": period, "tasks": tasks, "checks": checks,
            "statuses": store.CLOSING_STATUSES,
            "progress": {"done": done, "total": len(tasks),
                         "pct": (done / len(tasks) * 100) if tasks else 0,
                         "open": len(tasks) - done},
            "note": "Tasks, owners and comments are stored by this application. Business Central is never "
                    "written to; the automatic checks above read it."}


def treasury_screen(company, anchor, mode, dimension, held=None):
    """Cash flow, bank activity and what falls due next. Read on demand. held: the company's dashboard figures
    (cash, payables, receivables) for the daily control run; else those on screen."""
    settings = store.get_settings()
    c_from, c_to, *_ = periods(anchor, mode)
    sources = []
    accounts = _safe(sources, "Chart of accounts, current period",
                     lambda: client.accounts(company, c_from.isoformat(), c_to.isoformat(), dimension, 900), [])
    bank = _safe(sources, "Bank ledger entries",
                 lambda: client.bank_entries(company, c_from.isoformat(), c_to.isoformat()), [], False)
    if held is None:
        with _last_lock:
            d = _last.get("data") or {}
    else:
        d = held
    cash_total = (d.get("cash") or {}).get("total", 0.0)
    ap_detail = (d.get("payables") or {}).get("detail", [])
    ar_detail = (d.get("receivables") or {}).get("detail", [])
    flow = treasury.cash_flow(accounts, settings)
    activity = treasury.bank_activity(bank)
    obl = treasury.obligations(ap_detail, ar_detail, cash_total)
    out = {"flow": flow, "activity": activity, "obligations": obl, "sources": sources, "loaded": True,
           "period": [c_from.isoformat(), c_to.isoformat()],
           "exceptions": treasury.exceptions(flow, obl, settings)}
    if held is None:
        _merge_exceptions("treasury", out)
    return out


def assets_screen(company, anchor, mode):
    settings = store.get_settings()
    c_from, c_to, *_ = periods(anchor, mode)
    sources = []
    rows = _safe(sources, "Fixed asset ledger entries",
                 lambda: client.fa_entries(company, c_from.isoformat(), c_to.isoformat()), [])
    out = assets.build(rows, settings, f"{c_from:%Y-%m}")
    out.update({"loaded": True, "sources": sources, "period": [c_from.isoformat(), c_to.isoformat()]})
    _merge_exceptions("assets", out)
    return out


def budget_screen(company, anchor, mode, dimension):
    """Actual against budget per account, with a run-rate view of the full year."""
    settings = store.get_settings()
    c_from, c_to, *_ = periods(anchor, mode)
    year_from = date(c_to.year, 1, 1)
    sources = []
    period_acc = _safe(sources, "Chart of accounts, period",
                       lambda: client.accounts(company, c_from.isoformat(), c_to.isoformat(), dimension, 900), [])
    ytd_acc = _safe(sources, "Chart of accounts, year to date",
                    lambda: client.accounts(company, year_from.isoformat(), c_to.isoformat(), dimension, 3600), [])
    name = settings["budget"].get("name") or ""
    b_period = _safe(sources, "Budget, period",
                     lambda: client.budget(company, c_from.isoformat(), c_to.isoformat(), name), [], False)
    b_year = _safe(sources, "Budget, full year",
                   lambda: client.budget(company, year_from.isoformat(), date(c_to.year, 12, 31).isoformat(),
                                         name), [], False)
    acc_rules = {**finance.DEFAULT_ACCOUNT_RULES,
                 **{k: v for k, v in settings["accounts"].items() if v}}
    kmap = kpilib.mapping(company)
    period_pl = finance.profit_and_loss(period_acc, None, None, b_period, acc_rules, mapping=kmap)
    ytd_pl = finance.profit_and_loss(ytd_acc, None, None, b_year, acc_rules, mapping=kmap)
    days_done = (c_to - year_from).days + 1
    run_rate = 365 / days_done if days_done else 0
    rows = []
    for line in ytd_pl["lines"]:
        if line["budget"] is None and not line["current"]:
            continue
        budget = line["budget"] or 0
        rows.append({**line, "remaining": budget - line["current"] if budget else None,
                     "forecast_year": line["current"] * run_rate,
                     "vs_budget_pct": ((line["current"] - budget) / abs(budget) * 100) if budget else None})
    rows.sort(key=lambda r: -abs(r["current"]))
    out = {"loaded": True, "sources": sources, "budget_name": name or "(all budgets)",
           "period": [c_from.isoformat(), c_to.isoformat()], "ytd": [year_from.isoformat(), c_to.isoformat()],
           "period_statement": period_pl["statement"], "ytd_statement": ytd_pl["statement"],
           "lines": rows[:150], "run_rate": run_rate, "days_done": days_done,
           "note": "Budget figures come from the G/L budget entries for the same accounts. The year-end "
                   "forecast is a straight run rate of the year to date, not a plan: it ignores seasonality."}
    return out


def company_currency(company, settings=None):
    """The currency a company posts in.

    Business Central keeps every amount of a company in that company's local currency,
    so the ledger figures for RKS and Zoi are already euro. Only the label was wrong.
    The mapping lives in Settings next to the group rates.
    """
    settings = settings or store.get_settings()
    mapping = {k.lower(): v.upper()
               for k, v in _parse_map(settings["group"].get("currencies")).items()}
    return mapping.get((company or "").lower(), config.CURRENCY)


def _parse_map(text):
    """'SALT=ALL, Zoi Greece=EUR' -> {'SALT': 'ALL', 'Zoi Greece': 'EUR'}"""
    out = {}
    for part in (text or "").split(","):
        if "=" in part:
            k, _, v = part.partition("=")
            out[k.strip()] = v.strip()
    return out


def kpi_mapping_screen(company):
    """The KPI mapping of a company with every posting account of its chart: the line it falls in, its year to
    date movement and its balance, the accounts not mapped and the two checks (net profit, balance sheet)."""
    m = kpilib.mapping(company)
    today = date.today()
    sources = []
    acc = _safe(sources, f"Chart of accounts, {company}",
                lambda: client.accounts(company, date(today.year, 1, 1).isoformat(), today.isoformat(), None, 900), [],
                False)
    line_of, rule_of = kpilib.classifier(m), kpilib.cluster_of(m)
    rows = []
    for r in acc:
        if r.get("Account_Type") != "Posting":
            continue
        line = line_of(r.get("No"))
        rule = rule_of(r.get("No"))
        st = "pl" if r.get("Income_Balance") == "Income Statement" else "bs"
        rows.append({"no": r.get("No"), "name": r.get("Name"), "statement": st, "line": line,
                     "pack_line": rule if rule in kpilib.CLUSTER else None,
                     "wrong_side": bool(line and kpilib.LINE[line]["statement"] != st),
                     "bc_category": (r.get("Account_Category") or "").strip(),
                     "ytd": round(float(r.get("Net_Change") or 0), 2),
                     "balance": round(float(r.get("Balance_at_Date") or 0), 2)})
    P = kpilib.pl_lines(acc, m) if acc else None
    B = kpilib.bs_lines(acc, m) if acc else None
    return {"company": company, "mapping": m, "definitions": kpilib.definitions(), "accounts": rows,
            "unmapped": [x for x in rows if not x["line"]], "wrong_side": [x for x in rows if x["wrong_side"]],
            "pl": {"lines": P["lines"], "net_profit": P["net_profit"], "check": P["check"],
                   "derived": P["derived"], "clusters": P["clusters"], "pack": P["pack"]} if P else None,
            "bs": {"lines": B["lines"], "totals": B["totals"]} if B else None,
            "kpis": kpilib.pl_kpis(P, m) if P else None,
            "period": [date(today.year, 1, 1).isoformat(), today.isoformat()], "sources": sources}


def capital_screen(company, anchor, mode):
    """Financial statements → Balance sheet and Liquidity & capital of one legal entity at the end of the period
    chosen, against the end of the month before and the end of last year: the balance sheet by line of the KPI
    mapping, the liquidity and debt figures, the equity bridge and what is owed to suppliers by due date. Never cut
    by location. Read only."""
    m = kpilib.mapping(company)
    _c_from, c_to, *_rest = periods(anchor, mode)
    today = date.today()
    at = min(c_to, today)
    pm, py = capital.dates(at)
    sources = []

    def chart(d, label, required=False):
        # one read gives both: the balance at d and the movement of the year up to d (the year's result)
        return _safe(sources, f"Chart of accounts at {d:%d.%m.%Y} ({label})",
                     lambda: client.accounts(company, date(d.year, 1, 1).isoformat(), d.isoformat(), None,
                                             max(config.REFRESH_SECONDS - 10, 30) if d >= today else 3600),
                     [], required)
    rows_now = chart(at, "the date chosen", True)
    rows_pm = chart(pm, "end of the month before")
    rows_py = rows_pm if pm == py else chart(py, "end of last year")
    if not rows_now:
        return {"loaded": True, "available": False, "company": company, "sources": sources,
                "error": "The chart of accounts could not be read from Business Central."}
    now = capital.position(rows_now, rows_now, m)
    P_pm = capital.position(rows_pm, rows_pm, m) if rows_pm else None
    P_py = capital.position(rows_py, rows_py, m) if rows_py else None
    approved = bool(m and m.get("status") == "Approved")
    p_ltm = None
    if approved:          # EBITDA of the last twelve months only once Finance has approved the mapping
        ltm = _safe(sources, "Chart of accounts, last 12 months",
                    lambda: client.accounts(company, (at - timedelta(days=364)).isoformat(), at.isoformat(), None,
                                            3600), [], False)
        p_ltm = kpilib.pl_lines(ltm, m) if ltm else None
    kpis, status = capital.liquidity(now, P_pm, P_py, m, p_ltm, approved)
    vendors = _safe(sources, "Open supplier entries", lambda: client.open_vendor_entries(company), [], False)
    try:
        companies = client.companies()
    except BCError:
        companies = []
    mat = capital.maturity(vendors, today, [c for c in companies if c.strip().upper() != company.strip().upper()])
    t = now["totals"]
    return {"loaded": True, "available": True, "company": company, "sources": sources,
            "dates": {"current": at.isoformat(), "month_end": pm.isoformat(), "year_end": py.isoformat()},
            "status": status,
            "mapping": {"status": (m or {}).get("status") or "Content required",
                        "approved_by": (m or {}).get("approved_by") or "",
                        "approved_at": (m or {}).get("approved_at") or ""},
            "statement": capital.statement(now, P_pm, P_py, m),
            "totals": {"assets": t["assets"] if m else None, "liabilities": t["liabilities"] if m else None,
                       "equity": t["equity"] if m else None, "difference": t["difference"] if m else None,
                       "month_end": P_pm["totals"] if (P_pm and m) else None,
                       "year_end": P_py["totals"] if (P_py and m) else None},
            "unmapped": [{"account": no, "name": n, "value": v} for no, n, v, _l in now["unmapped"]][:60],
            "liquidity": kpis, "equity_bridge": capital.equity_bridge(now, P_py, m), "maturity": mat,
            "note": "The balance sheet of the legal entity at the date chosen, by line of the KPI mapping (Finance → "
                    "Settings → KPI mapping), against the end of the month before and the end of last year. A "
                    "balance sheet is a position of the company, so it is never cut by location. Business Central "
                    "has never closed the income-statement accounts to equity: the result they hold is shown as the "
                    "result of prior years not closed and the profit or loss of the current year. Figures stay "
                    "Pilot until Finance approves the mapping."}

def _f(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _company_rate_status(company_list, c_to, sources):
    """The exchange rate of each company keeping its books in another currency than the group's, at c_to."""
    settings = store.get_settings()
    group = settings["group"]
    reporting = (group.get("reporting_currency") or "ALL").upper()
    currencies = {k.lower(): v.upper() for k, v in _parse_map(group.get("currencies")).items()}
    foreign = sorted({currencies.get(c.lower(), reporting) for c in company_list} - {reporting})
    if not foreign:
        return [], reporting
    src = next((c for c in [config.BC_COMPANY] + list(company_list)
                if currencies.get(c.lower(), reporting) == reporting), config.BC_COMPANY)
    rows = _safe(sources, f"Currency Exchange Rates, {src}", lambda: client.exchange_rates(src), [], False)
    series, _skipped, errs = fx.parse(rows)
    out = []
    for cur in foreign:
        rate, since = fx.closing(series.get(cur), c_to)
        out.append({"currency": cur, "rate": rate, "since": since.isoformat() if since else None, "source": src,
                    "error": "; ".join(errs)})
    return out, reporting


def dq_screen(company, anchor, mode, sync=True):
    """Finance tower → Data quality & reconciliation for one company and period: the checks of the KPI library
    (mapping, P&L and balance sheet), the ledgers against the G/L, inventory, Simphony ↔ BC, budget, FX and
    intercompany, and the frozen months. Read only; failed checks of an ended month go to the Exceptions register."""
    settings = store.get_settings()
    mat = float(settings["thresholds"].get("materiality") or 0)
    m = kpilib.mapping(company)
    c_from, c_to, *_ = periods(anchor, mode)
    today = date.today()
    end = min(c_to, today)
    ended = c_to < today
    period = f"{c_from:%Y-%m}" if mode == "month" else f"{c_from.isoformat()}..{c_to.isoformat()}"
    sources = []
    rows = _safe(sources, f"Chart of accounts {c_from:%d.%m.%Y}–{end:%d.%m.%Y}",
                 lambda: client.accounts(company, c_from.isoformat(), end.isoformat(), None, 900 if ended else 300),
                 [], True)
    if not rows:
        return {"loaded": True, "available": False, "company": company, "sources": sources,
                "error": "The chart of accounts could not be read from Business Central."}
    P, B = kpilib.pl_lines(rows, m), kpilib.bs_lines(rows, m)
    rules = (m or {}).get("rules") or {}
    checks = []
    st = (m or {}).get("status")
    checks.append(dq.row("mapping", "KPI mapping approved by Finance", "Status of the account mapping", "Approved",
                         st or "none", "Pass" if st == "Approved" else "Content required" if not m else "Info",
                         (f"Approved by {m.get('approved_by')} on {str(m.get('approved_at'))[:10]}." if st == "Approved"
                          else "No mapping for this company." if not m else
                          f"{st}: every figure of the library is Pilot until Finance approves the mapping."),
                         "settings", "text"))
    un_is = [u for u in P["unmapped"] if abs(u[2]) >= 0.5]
    checks.append(dq.row("is_unmapped", "Income-statement accounts outside the mapping",
                         f"{len(un_is)} accounts with postings in the period", 0.0,
                         round(sum(u[2] for u in un_is), 2), "Pass" if not un_is else "Difference",
                         "" if not un_is else "They are in the net profit but in no line: map them.", "settings"))
    checks.append(dq.row("pl_check", "P&L lines add up to the net profit",
                         "Σ of the lines against − Σ of every income-statement account (the trial balance of BC)",
                         P["net_profit"], round(P["net_profit"] + P["check"], 2),
                         "Pass" if abs(P["check"]) < 1 else "Difference", "", "pl"))
    un_bs = [u for u in B["unmapped"] if abs(u[2]) >= 0.5]
    checks.append(dq.row("bs_check", f"Balance sheet balances at {end:%d.%m.%Y}",
                         "Assets − liabilities − equity, the unclosed result in equity", 0.0,
                         B["totals"]["difference"] if m else None,
                         "Content required" if not m else "Pass" if abs(B["totals"]["difference"]) < 1 else "Difference",
                         "" if not un_bs else f"{len(un_bs)} balance-sheet accounts with a balance are outside the "
                                              f"mapping.", "bs"))
    # the supplier and customer ledgers hold only what is open now: compared with the G/L today
    rows_today = rows if end == today else _safe(
        sources, "Chart of accounts today",
        lambda: client.accounts(company, date(today.year, 1, 1).isoformat(), today.isoformat(), None, 300), [], False)
    Bt = kpilib.bs_lines(rows_today, m) if rows_today else None
    vend = _safe(sources, "Open supplier entries", lambda: client.open_vendor_entries(company), None, False)
    cust = _safe(sources, "Open customer entries", lambda: client.open_customer_entries(company), None, False)
    try:
        group = [c for c in client.companies() if c.strip().upper() != company.strip().upper()]
    except BCError:
        group = []
    ap_led = None if vend is None else round(-sum(_f(r.get("Remaining_Amt_LCY")) for r in vend), 2)
    ap_ic = 0.0 if vend is None else -sum(_f(r.get("Remaining_Amt_LCY")) for r in vend
                                          if any(g.upper() in str(r.get("Vendor_Name") or "").upper() for g in group))
    gl_ap = Bt["lines"]["payables"] if (Bt and str(rules.get("payables") or "").strip()) else None
    c = dq.ledger_check("ap_rec", "Trade payables against the supplier ledger", gl_ap, ap_led, mat, "ap",
                        "the open supplier entries", "Trade payables")
    if ap_ic:
        c["detail"] = (c["detail"] + f" Of the supplier entries, {ap_ic:,.0f} are owed to group companies.").strip()
    checks.append(c)
    ar_led = None if cust is None else round(sum(_f(r.get("Remaining_Amt_LCY")) for r in cust), 2)
    gl_ar = Bt["lines"]["receivables"] if (Bt and str(rules.get("receivables") or "").strip()) else None
    checks.append(dq.ledger_check("ar_rec", "Trade receivables against the customer ledger", gl_ar, ar_led, mat,
                                  "ar", "the open customer entries", "Trade receivables"))
    # inventory: the last read of the Inventory screen of this company (on hand today)
    with _last_lock:
        d = _last.get("data") or {}
    inv = d.get("inventory") if (d.get("meta") or {}).get("company") == company else None
    rec = (inv or {}).get("reconciliation") if inv and inv.get("loaded") else None
    if rec:
        th = float(settings["thresholds"].get("inventory_difference") or 0)
        checks.append(dq.row("inventory", "Inventory against the G/L",
                             "Stock on hand at item cost against the inventory accounts, today", rec["gl_balance"],
                             rec["inventory_value"], "Pass" if abs(rec["difference"]) < max(1, th) else "Difference",
                             rec["method"], "inventory"))
    else:
        checks.append(dq.row("inventory", "Inventory against the G/L",
                             "Stock on hand at item cost against the inventory accounts, today", None, None,
                             "Not read", "Open Inventory once for this company; the check uses its last read.",
                             "inventory"))
    sr = salesrec.held(company)
    if sr and sr.get("kpis"):
        k = sr["kpis"]
        checks.append(dq.row("sales_rec", "Simphony sales against Business Central",
                             f"Every restaurant and day {sr['from']} → {sr['to']}: Simphony, the POS import, the "
                             f"invoices", float(k["sim"]), float(k["imp"]),
                             "Pass" if not k["issues"] else "Difference",
                             f"{k['ok']} of {k['days']} restaurant-days agree; {k['issues']} issues "
                             f"(import gap {k['imp_gap']:,.0f}, invoice gap {k['inv_gap']:,.0f}).", "salesrec"))
    else:
        checks.append(dq.row("sales_rec", "Simphony sales against Business Central",
                             "Every restaurant and day: Simphony, the POS import, the invoices", None, None, "Not read",
                             "Open Control → Simphony ↔ BC once; the check uses its last read.", "salesrec"))
    bud_name = (settings.get("budget") or {}).get("name") or f"BUDGET{c_from.year}"
    bud = _safe(sources, f"Budget {bud_name}",
                lambda: client.budget(company, c_from.isoformat(), c_to.isoformat(), bud_name), [], False)
    bc = dq.budget_check(bud, m, P) if m else None
    if not bud:
        checks.append(dq.row("budget", f"Budget {bud_name} for the period", "Budget entries of the period", None,
                             None, "Content required", f"No entries of {bud_name} in the period.", "budget"))
    else:
        checks.append(dq.row("budget", f"Budget {bud_name} for the period",
                             f"{len(bud)} entries; budget on accounts outside the mapping", 0.0,
                             bc["unmapped_value"] if bc else None,
                             "Content required" if not bc else "Pass" if abs(bc["unmapped_value"]) < 1 else "Difference",
                             (f"Budget net revenue {bc['revenue']:,.0f}, net profit {bc['net_profit']:,.0f} through the "
                              f"same mapping." if bc else "Needs the KPI mapping."), "budget"))
    try:
        all_co = client.companies()
    except BCError:
        all_co = [company]
    rates, reporting = _company_rate_status(all_co, end, sources)
    if not rates:
        checks.append(dq.row("fx", "Exchange rates for the group view", "Companies in another currency", None,
                             "none", "Info", f"Every company keeps its books in {reporting}.", "group", "text"))
    for r in rates:
        checks.append(dq.row(f"fx_{r['currency'].lower()}", f"{r['currency']} rate at {end:%d.%m.%Y}",
                             f"Currency Exchange Rates of {r['source']} (page 483)", None,
                             None if r["rate"] is None else r["rate"],
                             "Pass" if r["rate"] is not None else "Content required",
                             (f"In force since {r['since']}; the method (average for the P&L, closing for the balance "
                              f"sheet) is a proposal until Finance approves it.") if r["rate"] is not None else
                             (f"No {r['currency']} rate in Business Central for this date. {r['error']}").strip(),
                             "group", "rate"))
    checks.append(dq.row("ic", "Intercompany eliminations", "Pairs of group accounts and the elimination method",
                         None, None, "Content required",
                         "Business Central has no IC partner on the accounts; Finance has to list the pairs and the "
                         "method. Until then the group view is combined, not consolidated.", "group", "text"))
    # the frozen month
    snap = dq.snapshot(company, period) if mode == "month" else None
    changes = []
    if snap:
        changes = dq.compare(snap, rows)
        checks.append(dq.row("frozen", f"{period} unchanged since it was frozen",
                             f"Trial balance now against the one frozen on {snap['at'][:16].replace('T', ' ')} by "
                             f"{snap['by']}", 0.0, round(sum(abs(x['net_change']) + abs(x['bal_change'])
                                                             for x in changes), 2),
                             "Pass" if not changes else "Difference",
                             "" if not changes else f"{len(changes)} accounts were posted to after the freeze.",
                             ""))
    elif mode == "month" and ended:
        checks.append(dq.row("frozen", f"{period} frozen", "A snapshot of the trial balance of the closed month",
                             None, None, "Info", "Not frozen yet: freeze it once the month is closed, so later "
                                                 "postings to it show here.", "", "text"))
    reg = None
    if sync and ended and mode == "month":
        try:
            reg = exreg.sync("data_quality", company, dq.findings(period, checks, changes),
                             in_scope=lambda x: (x.get("subject") or "").startswith(period + " "))
        except Exception:                                          # noqa: BLE001
            traceback.print_exc()
    frozen = []
    for sp in dq.snapshots(company)[:8]:
        frozen.append({"period": sp["period"], "at": sp["at"], "by": sp["by"], "entry_no": sp.get("entry_no"),
                       "from": sp["from"], "to": sp["to"]})
    counts = {s_: sum(1 for c in checks if c["status"] == s_) for s_ in dq.STATUSES}
    return {"loaded": True, "available": True, "company": company, "period": period, "mode": mode,
            "from": c_from.isoformat(), "to": end.isoformat(), "ended": ended, "checks": checks, "counts": counts,
            "unmapped": dq.unmapped_rows(P, B)[:80], "changes": changes[:80], "snapshot": snap and
            {k: snap[k] for k in ("at", "by", "entry_no", "from", "to")},
            "frozen": frozen, "register": reg, "sources": sources,
            "budget": bc and {k: bc[k] for k in ("rows", "accounts", "revenue", "net_profit", "unmapped_value")},
            "can_freeze": mode == "month" and ended,
            "note": "Each check says what is compared with what. The supplier and customer ledgers and the stock only "
                    "hold what is open today, so they are compared with the G/L today, whatever the period. Failed "
                    "checks of an ended month go to the Exceptions register (Data quality, owner: chief accountant). "
                    "Nothing is written to Business Central."}


def allocation_screen(company):
    """Finance → Settings → Central costs to the restaurants: the rule and what it gives on the latest closed month
    held by the Executive dashboard (a preview, whatever the status)."""
    r = allocation.rule(company)
    today = date.today()
    cube = executive.assemble(company, today.year, company_currency(company))
    if not cube.get("cells_held") and today.month == 1:
        cube = executive.assemble(company, today.year - 1, company_currency(company))
    acc = cube.get("accounts") or {}
    month = finloc._closed_month(cube, acc, f"{today:%Y-%m}") if cube.get("cells_held") else None
    rest = set(cube.get("restaurants") or [])
    rows, pool, total = [], None, None
    if month:
        tot = finloc._agg(cube["actual"], "*", [month], acc)
        direct, revenue, names = {}, {}, {}
        for d in cube.get("departments") or []:
            if d["code"].upper() not in rest:
                continue
            x = finloc._agg(cube["actual"], d["code"], [month], acc)
            if x["n"]:
                direct[d["code"]], revenue[d["code"]], names[d["code"]] = x["ebitda"], x["sales"], d["name"]
        preview = dict(r, status="Approved")
        pool, alloc, contrib = allocation.apply(preview, tot["ebitda"], direct, revenue)
        sh = allocation.shares(r, revenue)
        rows = sorted(({"dept": k, "name": names[k], "revenue": revenue[k], "direct": direct[k],
                        "share": round(sh.get(k, 0.0) * 100, 2), "fixed": (r.get("shares") or {}).get(k.upper()),
                        "allocated": round(alloc[k]), "contribution": round(contrib[k])} for k in direct),
                      key=lambda x: -x["revenue"])
        total = tot["ebitda"]
        others = [{"dept": d["code"], "name": d["name"], "result": finloc._agg(cube["actual"], d["code"], [month],
                                                                                acc)["ebitda"]}
                  for d in cube.get("departments") or [] if d["code"].upper() not in rest]
    else:
        others = []
    return {"company": company, "rule": r, "public": allocation.public(company), "methods": allocation.METHODS,
            "month": month, "restaurants": rows, "pool": None if pool is None else round(pool),
            "pool_departments": sorted([o for o in others if abs(o["result"]) >= 1], key=lambda o: o["result"])[:30],
            "company_ebitda": total, "registry": sorted(rest),
            "note": "The pool is everything of the company not on a restaurant department of the Location intelligence "
                    "registry: head office, marketing, IT, the central kitchen and warehouse, and what is posted "
                    "without a restaurant. Its result is spread over the restaurants by the rule, so the restaurants' "
                    "Contributions add up to the company EBITDA. Until Finance approves the rule, every restaurant "
                    "shows its result before central costs, marked Partial, and its Contribution is Content required. "
                    "The figures here are a preview on the latest closed month the Executive dashboard holds."}


def dq_freeze(company, anchor, action, actor):
    c_from, c_to, *_ = periods(anchor, "month")
    period = f"{c_from:%Y-%m}"
    if action == "unfreeze":
        return {"ok": dq.unfreeze(company, period, actor)}
    if c_to >= date.today():
        return {"error": "Only a month that has ended can be frozen."}
    entry_no = client.last_gl_entry_no(company)           # first the number, then the figures
    rows = client.accounts(company, c_from.isoformat(), c_to.isoformat(), None, 0)
    if not rows:
        return {"error": "The chart of accounts could not be read."}
    snap = dq.freeze(company, period, c_from.isoformat(), c_to.isoformat(), rows, entry_no, actor)
    return {"ok": True, "period": period, "entry_no": entry_no, "accounts": len(snap["accounts"])}


def dq_late(company, period, account):
    snap = dq.snapshot(company, period)
    if not snap:
        return {"error": f"{period} is not frozen."}
    rows = client.gl_after(company, snap.get("entry_no") or 0, account, snap["to"])
    return {"account": account, "period": period, "entry_no": snap.get("entry_no"),
            "entries": [{"entry": r.get("Entry_No"), "date": r.get("Posting_Date"), "document": r.get("Document_No"),
                         "type": r.get("Document_Type"), "source": r.get("Source_Code"),
                         "amount": _f(r.get("Amount"))} for r in rows],
            "total": round(sum(_f(r.get("Amount")) for r in rows), 2), "more": len(rows) >= 300}


def consolidation_screen(anchor, mode):
    """Every company side by side. Combined, not consolidated: nothing is eliminated between companies, and a
    company in another currency is left out until Business Central publishes its exchange rates (Finance's
    decision of 3 November 2026: the EUR rate comes from BC, not from a number typed in Settings)."""
    settings = store.get_settings()
    group = settings["group"]
    reporting = (group.get("reporting_currency") or "ALL").upper()
    currencies = {k.lower(): v.upper() for k, v in _parse_map(group.get("currencies")).items()}
    account_rules = {**finance.DEFAULT_ACCOUNT_RULES,
                     **{k: v for k, v in settings["accounts"].items() if v}}
    c_from, c_to, p_from, p_to, *_ = periods(anchor, mode)
    sources = []
    try:
        companies = client.companies()
    except BCError as exc:
        return {"error": str(exc), "companies": [], "loaded": True}
    rows = []
    keys = ("revenue", "gross", "opex", "ebitda", "net_profit", "assets", "liabilities", "equity")
    pl_keys = ("revenue", "gross", "opex", "ebitda", "net_profit")
    # the exchange rates of the company keeping its books in the reporting currency (SALT, in ALL)
    src = next((c for c in [config.BC_COMPANY] + list(companies)
                if currencies.get(c.lower(), reporting) == reporting), None)
    series, fx_errors = {}, []
    if src and any(currencies.get(c.lower(), reporting) != reporting for c in companies):
        fx_rows = _safe(sources, f"Currency Exchange Rates, {src}", lambda: client.exchange_rates(src), [], False)
        series, _skipped, fx_errors = fx.parse(fx_rows)
        if not fx_rows:
            fx_errors.append(f"Currency Exchange Rates of {src} could not be read or holds no rows.")
    for co in companies:
        acc = _safe(sources, f"Chart of accounts, {co}",
                    lambda c=co: client.accounts(c, c_from.isoformat(), c_to.isoformat(), None, 900), [], False)
        currency = currencies.get(co.lower(), reporting)
        if not acc:
            rows.append({"company": co, "available": False, "currency": currency})
            continue
        kmap = kpilib.mapping(co)
        pl = finance.profit_and_loss(acc, rules=account_rules, mapping=kmap)
        bs = kpilib.bs_lines(acc, kmap) if kmap else None
        figures = {"revenue": pl["kpis"]["revenue"] if kmap else None, "gross": pl["kpis"]["gross"],
                   "opex": pl["kpis"]["opex"] if kmap else None, "ebitda": pl["kpis"]["ebitda"],
                   "net_profit": pl["kpis"]["net_profit"],
                   "assets": bs["totals"]["assets"] if bs else None,
                   "liabilities": bs["totals"]["liabilities"] if bs else None,
                   "equity": bs["totals"]["equity"] if bs else None}
        if currency == reporting:
            rate = rate_bs = 1.0
            fxinfo = None
        else:
            ser = series.get(currency)
            rate = fx.average(ser, c_from, c_to)
            rate_bs, bs_from = fx.closing(ser, c_to)
            fxinfo = {"average": rate, "closing": rate_bs, "closing_from": bs_from.isoformat() if bs_from else None,
                      "source": f"Currency Exchange Rates of {src}",
                      "missing": None if (rate is not None and rate_bs is not None) else
                      (f"No {currency} rate in Business Central ({src}) for every day of the period" if ser else
                       f"No {currency} rate in Business Central ({src})")}
        ok_fx = rate is not None and rate_bs is not None
        rows.append({"company": co, "available": True, "currency": currency, "rate": rate, "rate_bs": rate_bs,
                     "fx": fxinfo,
                     "gross_margin": pl["kpis"]["gross_margin"], **figures,
                     "mapping": (kmap or {}).get("status") or "Content required",
                     "status": "Content required" if not kmap else ("Approved" if kmap.get("status") == "Approved" else "Pilot"),
                     "bs_difference": bs["totals"]["difference"] if bs else None,
                     "converted": {k: (v * (rate if k in pl_keys else rate_bs) if (ok_fx and v is not None) else None)
                                   for k, v in figures.items()},
                     "in_total": ok_fx})
    ok = [r for r in rows if r.get("available") and r.get("in_total")]
    excluded = [r["company"] for r in rows if r.get("available") and not r.get("in_total")]
    conv = [r for r in rows if r.get("fx") and r["in_total"]]
    rate_line = ("; ".join(f"{r['currency']}: average {r['rate']:,.4f}, closing {r['rate_bs']:,.4f} from "
                           f"{r['fx']['closing_from']}" for r in {x['currency']: x for x in conv}.values())
                 + f" (Business Central, Currency Exchange Rates of {src}; method Pilot until Finance approves it)"
                 if conv else ("Content required: " + "; ".join(fx_errors) if fx_errors else "none needed"))
    return {"loaded": True, "sources": sources, "rows": rows, "basis": "Combined, not consolidated",
            "period": [c_from.isoformat(), c_to.isoformat()],
            "reporting_currency": reporting, "rates": rate_line, "fx_errors": fx_errors,
            "excluded": excluded, "rate_note": "",
            "totals": {k: (sum(r["converted"][k] for r in ok)
                           if ok and all(r["converted"][k] is not None for r in ok) else None)
                       for k in keys} | {"companies": len(ok)},
            "note": f"Combined, not consolidated: the companies are added as they stand. Nothing is eliminated "
                    f"between companies, so intercompany sales, balances and investments are counted twice. A company "
                    f"in another currency is converted with the rates of Business Central (Currency Exchange Rates of "
                    f"{src}): the result at the average rate of the period (the rate in force on each day), the "
                    f"balance sheet at the closing rate of the last day. That method is a proposal until Finance "
                    f"approves it, and the two rates leave a translation difference that is not shown. A company with "
                    f"no rate for the period is left out. Every company follows its own KPI mapping; one without a "
                    f"mapping shows Content required."
                    + (f" Left out: {', '.join(excluded)}." if excluded else "")}


def _merge_exceptions(key, out):
    with _last_lock:
        d = _last.get("data")
        if d is None:
            return
        d[key] = out
        existing = {e["code"] for e in d["exceptions"]}
        d["exceptions"] = d["exceptions"] + [e for e in out.get("exceptions", []) if e["code"] not in existing]


def _headline(pl, bs, ap, ar, cash, exceptions):
    k = pl["kpis"]
    working_capital = cash["total"] + ar["total"] - ap["total"]
    return {"revenue": k["revenue"], "revenue_prev": k["revenue_prev"], "gross": k["gross"],
            "gross_margin": k["gross_margin"], "opex": k["opex"], "ebitda": k["ebitda"],
            "ebitda_margin": k["ebitda_margin"], "ebit": k["ebit"],
            "cash": cash["total"], "receivables": ar["total"], "payables": ap["total"],
            "receivables_overdue": ar["overdue"], "payables_overdue": ap["overdue"],
            "working_capital": working_capital, "assets": bs["totals"]["assets"],
            "equity": bs["totals"]["equity"],
            "critical": sum(1 for e in exceptions if e["severity"] == "critical"),
            "exceptions": len(exceptions)}


def _statement_exceptions(pl, bs, cash, settings):
    th = settings["thresholds"]
    out = []
    um = [u for u in pl.get("unmapped") or [] if u.get("value")]
    if um:
        out.append({"code": "kpi-unmapped", "severity": "warning", "area": "Data quality",
                    "rule": "Accounts not in the KPI mapping",
                    "title": f"{len(um)} income-statement accounts with postings are not in the KPI mapping",
                    "detail": "Their amounts are in the net profit but in no P&L line: "
                              + ", ".join(f"{u['account']} {u['name']} {u['value']:,.0f}" for u in um[:8]) + ".",
                    "impact": abs(sum(u["value"] for u in um)), "document": "",
                    "next_action": "Map them in Finance → Settings → KPI mapping.",
                    "drill": {"kind": "pl", "key": ""}})
    fallback = 0 if "library" in pl else sum(v for k, v in pl.get("classified_by", {}).items() if k != "Account Category")
    bs_fallback = sum(v for k, v in bs.get("classified_by", {}).items() if k != "Account Category")
    if fallback or bs_fallback:
        out.append({"code": "coa-category", "severity": "warning", "area": "Data quality",
                    "rule": "Account Category is not filled in",
                    "title": f"{fallback + bs_fallback} accounts have no Account Category in Business Central",
                    "detail": "Where the field is blank the account number decides the classification "
                              f"({fallback} in the P&L, {bs_fallback} on the balance sheet), and "
                              "balance-sheet accounts that the number does not cover are placed by the side "
                              "their balance sits on. Filling the field in BC removes the guesswork for "
                              "every report, not only this one.",
                    "impact": 0, "document": "",
                    "next_action": "Set Account Category on the chart of accounts for this company.",
                    "drill": {"kind": "pl", "key": ""}})
    for line in pl.get("summary") or pl["statement"]:      # the KPI lines, not every line of the pack
        v = line["vs_previous_pct"]
        if v is not None and abs(v) >= th["pl_variance_pct"] and abs(line["vs_previous"] or 0) >= th["materiality"]:
            out.append({"code": f"pl-{line['label'][:20]}", "severity": "warning", "area": "P&L",
                        "rule": "P&L variance above the threshold",
                        "title": f"{line['label']} {v:+.1f}% against the comparison period",
                        "detail": f"{line['label']} {line['current']:,.0f} against {line['previous']:,.0f}, "
                                  f"a movement of {line['vs_previous']:+,.0f}.",
                        "impact": abs(line["vs_previous"] or 0), "document": "",
                        "next_action": "Explain the movement before the management pack is issued.",
                        "drill": {"kind": "pl", "key": ""}})
        if line.get("vs_budget_pct") is not None and abs(line["vs_budget_pct"]) >= th["pl_variance_pct"] \
                and abs(line["vs_budget"] or 0) >= th["materiality"]:
            out.append({"code": f"bud-{line['label'][:20]}", "severity": "warning", "area": "P&L",
                        "rule": "Budget variance above the threshold",
                        "title": f"{line['label']} {line['vs_budget_pct']:+.1f}% against budget",
                        "detail": f"Actual {line['current']:,.0f} against budget {line['budget']:,.0f}.",
                        "impact": abs(line["vs_budget"] or 0), "document": "",
                        "next_action": "Confirm whether the variance is timing or permanent.",
                        "drill": {"kind": "pl", "key": ""}})
    for f in bs["flags"][:10]:
        out.append({"code": f"bs-{f['account']}", "severity": "warning", "area": "Balance sheet",
                    "rule": "Account balance on the unexpected side",
                    "title": f"{f['name']} ({f['account']}): {f['value']:,.0f}",
                    "detail": f["detail"], "impact": abs(f["value"]), "document": f["account"],
                    "next_action": "Reconcile the account and reclassify if needed.",
                    "drill": {"kind": "bs", "key": f["account"]}})
    if cash["total"] < th["cash_warning"]:
        out.append({"code": "cash-low", "severity": "critical", "area": "Cash",
                    "rule": "Cash below the warning level",
                    "title": f"Cash and bank at {cash['total']:,.0f}, below the warning level of "
                             f"{th['cash_warning']:,.0f}",
                    "detail": "Read from the cash and bank balance-sheet accounts at the end of the period.",
                    "impact": cash["total"], "document": "",
                    "next_action": "Review upcoming payments and collections with the CFO.",
                    "drill": {"kind": "cash", "key": ""}})
    return out


# ------------------------------------------------------------------ drill-down
EXC_FIELDS = ("code", "severity", "area", "rule", "title", "detail", "next_action", "impact", "document")


def send_exception_email(body, user):
    """(status, answer) of Exceptions → Send by email (exmail.py). The exception is taken from the figures this
    server holds for the company when it is there (the screen's), else from what the page sent."""
    company = str(body.get("company") or config.BC_COMPANY)
    code = str(body.get("code") or "")
    with _last_lock:
        held = _last.get("data") if _last.get("company") == company else None
    exc = next((e for e in (held or {}).get("exceptions") or [] if e.get("code") == code), None)
    sent = body.get("exception") or {}
    if exc is None:
        exc = {k: sent.get(k) for k in EXC_FIELDS}
        exc["code"] = code
    if not exc.get("title"):
        return 400, {"error": "The exception could not be found; refresh the screen and try again."}
    meta = (held or {}).get("meta") or {}
    cur = meta.get("current") or body.get("period") or ["", ""]
    period = f"{cur[0]} to {cur[1]}" if cur and cur[0] else ""
    records = None
    if body.get("records") and exc.get("drill") and held is not None:
        try:
            records = drill(exc["drill"].get("kind"), exc["drill"].get("key") or "")
        except Exception:                                          # noqa: BLE001
            records = None
    def assign(owner, comment):
        """The recipients own it in the Exceptions register: the item a control found, else one tracked by hand."""
        return exreg.track({"title": exc.get("title"), "description": f"{exc.get('detail') or ''}\nRecommended: {exc.get('next_action') or ''}",
                            "source_exception": code, "bc_document": exc.get("document") or "", "document_type": exc.get("area") or "",
                            "company": company, "impact": abs(float(exc.get("impact") or 0)), "owner": owner,
                            "priority": "Critical" if exc.get("severity") == "critical" else "High" if exc.get("severity") == "warning" else "Medium",
                            "management_comment": comment}, user.get("username", ""), user.get("email") or "")["id"]
    try:
        entry = exmail.send(exc, company, period, meta.get("currency") or company_currency(company),
                            [str(x) for x in body.get("to_roles") or []], [str(x) for x in body.get("to_emails") or []],
                            [str(x) for x in body.get("cc_roles") or []], [str(x) for x in body.get("cc_emails") or []],
                            str(body.get("note") or ""), user, records, bool(body.get("assign")), assign)
    except ValueError as exc_:
        return 400, {"error": str(exc_)}
    except RuntimeError as exc_:
        return 502, {"error": str(exc_)}
    return 200, {"ok": True, "sent": entry, "log": exmail.log(company)}


def drill(kind, key):
    with _last_lock:
        d = _last.get("data") or {}
        company = _last.get("company") or config.BC_COMPANY
    title, rows = kind, []
    if kind == "pl":
        title = "Profit and loss by account"
        rows = [{"Account": l["account"], "Name": l["name"], "Category": l["category"],
                 "Current": round(l["current"]), "Comparison": None if l["previous"] is None else round(l["previous"]),
                 "Last year": None if l["last_year"] is None else round(l["last_year"]),
                 "Budget": None if l["budget"] is None else round(l["budget"])}
                for l in d.get("pl", {}).get("lines", [])]
    elif kind == "bs":
        title = "Balance sheet by account"
        rows = [{"Account": l["account"], "Name": l["name"], "Category": l["category"],
                 "Balance": round(l["current"]), "Previous": round(l["previous"]),
                 "Movement": round(l["movement"]),
                 "Movement %": None if l["movement_pct"] is None else round(l["movement_pct"], 1)}
                for cat in d.get("balance_sheet", {}).get("groups", {}).values() for l in cat["lines"]
                if not key or l["account"] == key]
    elif kind in ("ap", "ar"):
        src = d.get("payables" if kind == "ap" else "receivables", {})
        title = "Open payables" if kind == "ap" else "Open receivables"
        rows = [{"Party": r["party"], "Document": r["document"], "Type": r["type"], "Posted": r["posting"],
                 "Due": r["due"], "Days overdue": r["days"], "Currency": r["currency"],
                 "Remaining": round(r["remaining"])} for r in src.get("detail", [])]
    elif kind == "cash":
        title = "Cash and bank"
        rows = [{"Account": a["account"], "Name": a["name"], "Balance": round(a["balance"]),
                 "Movement in period": round(a["movement"])} for a in d.get("cash", {}).get("accounts", [])]
    elif kind == "gl":
        title = "Material general ledger entries"
        rows = [{"Entry": e["entry"], "Posted": e["posting"], "Document date": e["document_date"],
                 "Document": e["document"], "Account": e["account"], "Name": e["account_name"],
                 "Source": e["source"], "Manual": "yes" if e["manual"] else "", "Amount": round(e["amount"])}
                for e in d.get("gl", {}).get("entries", [])]
    elif kind == "account":
        title = f"General ledger entries for account {key}"
        meta = d.get("meta", {})
        try:
            entries = client.gl_for_account(company, key, meta["current"][0], meta["current"][1])
        except BCError as exc:
            return {"title": title, "kind": kind, "key": key, "columns": ["Error"],
                    "rows": [{"Error": str(exc)}], "count": 1}
        rows = [{"Entry": e.get("Entry_No"), "Posted": e.get("Posting_Date"), "Document": e.get("Document_No"),
                 "Source": e.get("Source_Code"), "Description": e.get("G_L_Account_Name"),
                 "Amount": round(float(e.get("Amount") or 0))} for e in entries]
    elif kind.startswith("inventory"):
        inv = d.get("inventory") or {}
        which = kind.split("_")[-1]
        src = {"negative": inv.get("negative"), "zero": inv.get("zero_cost"),
               "risk": inv.get("risk"), "top": inv.get("top_items")}.get(which, inv.get("top_items")) or []
        title = {"negative": "Negative inventory", "zero": "Stock held at zero cost",
                 "risk": "Slow and non-moving stock", "top": "Inventory by value"}.get(which, "Inventory")
        rows = [{"Item": i["item"], "Description": i["description"], "Category": i["category"],
                 "Quantity": round(i["quantity"], 2), "UOM": i["uom"], "Unit cost": round(i["unit_cost"], 2),
                 "Value": round(i["value"]), "Class": i.get("class", ""),
                 "Unchanged for (days)": i.get("still_days")} for i in src]
    elif kind in ("payments", "receipts"):
        t = d.get("treasury") or {}
        title = "Payments due in the next seven days" if kind == "payments" else "Receipts due in the next seven days"
        rows = [{"Party": r["party"], "Document": r["document"], "Due": r["due"],
                 "Days overdue": r["days"], "Amount": round(r["amount"])}
                for r in (t.get("obligations", {}).get(kind) or [])]
    elif kind == "cashflow":
        t = d.get("treasury") or {}
        title = "Cash flow, indirect"
        rows = [{"Line": l["label"], "Amount": round(l["value"]), "How it is measured": l["note"]}
                for l in (t.get("flow", {}).get("lines") or [])]
    elif kind == "assets":
        a = d.get("assets") or {}
        title = "Fixed asset entries in the period"
        rows = [{"Asset": e["asset"], "Description": e["description"], "Class": e["class"], "Type": e["type"],
                 "Posted": e["posting"], "Document": e["document"], "Book": e["book"],
                 "Location": e["location"], "Amount": round(e["amount"])} for e in (a.get("entries") or [])]
    elif kind == "exceptions":
        title = "Exceptions"
        rows = [{"Severity": e["severity"], "Area": e.get("area", ""), "Rule": e["rule"], "Exception": e["title"],
                 "Impact": round(e.get("impact") or 0), "Recommended action": e.get("next_action", "")}
                for e in d.get("exceptions", [])]
    elif kind == "gaps":
        title = "Figures Business Central does not publish"
        rows = [{"KPI": g["kpi"], "What is missing": g["missing"], "Technical requirement": g["requirement"]}
                for g in DATA_GAPS]
    cols = list(rows[0].keys()) if rows else []
    return {"title": title, "kind": kind, "key": key, "columns": cols, "rows": rows[:500], "count": len(rows)}


arap = arap_notify.Notifier(client, company_currency, config.BC_COMPANY)
costrep = cost_report.Reporter(client, stock_summary, company_currency, config.BC_COMPANY)


_dx_cache = {}


def discharge_excluded(company, as_of, refresh=False):
    """The positive adjustments the negative stock email leaves out (Settings), read once an hour."""
    rules = negstock.exclusion_rules(store.get_settings().get("negative_report", {}).get("exclude_adjustments"))
    if not rules:
        return {}
    key = (company, as_of, tuple(rules))
    hit = _dx_cache.get(key)
    if hit and not refresh and time.time() - hit[0] < 3600:
        return hit[1]
    ex, errs = negstock.excluded_adjustments(client, company, rules, date.fromisoformat(as_of))
    if not errs:
        _dx_cache.clear()
        _dx_cache[key] = (time.time(), ex)
    return ex


def cost_report_status():
    return {"settings": costrep.settings(), "companies": costrep.companies(), "recipients": costrep.recipients(),
            "next_run": costrep.next_run(), "runs": cost_report.recent_runs(), "mail": arap_notify.mail_status(),
            "week": [d.isoformat() for d in cost_report.last_week()]}


negrep = negstock.Reporter(client, company_currency, config.BC_COMPANY)
ssrep = salestop_report.Reporter(client, company_currency, config.BC_COMPANY)
srrep = sevenrooms_report.Reporter(company_currency, _latest)


# ---------------------------------------------------------------- the exceptions register (exreg.py)
def register_after_negative(company, r, mv, m, excluded):
    """After every complete negative stock build of a company: its lines into the register, and Received vs
    discharged over 30 days from the same movement data (the same SHITJE adjustments left out)."""
    out = {"negative_stock": exreg.sync("negative_stock", company, exreg.from_negative(r))}
    try:
        du = recipeuse.analyse(company, mv, m, store.get_settings(), 30, excluded)
        out["discharge"] = exreg.sync("discharge", company, exreg.from_discharge(du))
    except Exception as exc:                                        # noqa: BLE001
        traceback.print_exc()
        out["discharge"] = {"error": str(exc)[:200]}
    return out


negrep.on_built = register_after_negative


def register_after_sales(company, r):
    """After every complete sales reconciliation: its exceptions into the register, for the days it covered."""
    if not r.get("complete"):
        return None
    return exreg.sync("sales_rec", company, salesrec.findings(r), in_scope=salesrec.in_scope(r))


salesrec.on_built = register_after_sales


def _billy_pub():
    S = billyrec.settings_of(store.get_settings())
    return {"days": S["days"], "window": S["window"], "tol": S["tol"], "late": S["late"],
            "rates": ", ".join(f"{x:g}" for x in sorted(S["rates"], reverse=True)), "shifts": S["shifts_text"],
            "map": S["map"], "operators": S["operators"], "exclude": ", ".join(S["exclude"]), "read_time": S["read_time"]}


def register_after_billy(company, r):
    """After every complete Simphony ↔ Billy read: its cases into the register, for the days it covered."""
    if not r.get("complete"):
        return None
    return exreg.sync("billy_rec", company, billyrec.findings(r), in_scope=billyrec.in_scope(r))


billyrec.on_built = register_after_billy


def register_after_sevenrooms(company, r):
    """After every complete SevenRooms ↔ Simphony read of the last 7 days: its cases into the register."""
    if not r.get("complete"):
        return None
    return exreg.sync("sevenrooms", company, srsim.findings(r), in_scope=srsim.in_scope(r))


srsim.on_built = register_after_sevenrooms


def register_after_receipts(company, r):
    """After every complete takings reconciliation: its cases into the register, for the days it covered."""
    if not r.get("complete"):
        return None
    return exreg.sync("receipts_rec", company, rcpt.findings(r), in_scope=rcpt.in_scope(r))


rcpt.on_built = register_after_receipts


RULE_SOURCES = {"ap_ar": ("Open supplier entries", "Open customer entries"),
                "statements": ("Chart of accounts, current period",),
                "gl": ("General ledger entries",), "treasury": ("Chart of accounts, current period", "Bank ledger entries")}


def _rule_findings(exceptions, location=""):
    """The exceptions of the Finance screens' rules as findings of the register: subject = the rule's code."""
    out = []
    for e in exceptions:
        code = str(e.get("code") or "")
        if not code:
            continue
        out.append({"subject": code, "kind": str(e.get("area") or code.split("-")[0])[:40], "location": location,
                    "title": e.get("title") or e.get("rule") or code,
                    "detail": " · ".join(x for x in (e.get("rule"), e.get("detail"),
                                                      ("To do: " + e["next_action"]) if e.get("next_action") else "") if x),
                    "severity": e.get("severity"), "value": abs(float(e.get("impact") or 0)),
                    "extra": {k: e.get(k) for k in ("area", "rule", "next_action", "document", "drill") if e.get(k)}})
    return out


def register_finance_rules(company, actor="control run"):
    """What Exceptions showed, kept in the register: the rules on the open supplier and customer entries (as of
    today), the P&L and balance sheet and the cash of the month in progress, and the material G/L entries of its
    last days. A group whose Business Central read failed is not synced, so nothing closes for a failed read."""
    d = _dashboard_build(company, None, "month", "", remember=False)
    gl = gl_screen(company, None, "month", remember=False)
    tr = treasury_screen(company, None, "month", "", held=d)
    failed = {x["source"] for part in (d, gl, tr) for x in part.get("sources") or [] if x.get("status") != "ok"}
    groups = {"ap_ar": [e for e in d["exceptions"] if str(e.get("code", "")).startswith(("ap-", "ar-"))],
              "statements": [e for e in d["exceptions"] if not str(e.get("code", "")).startswith(("ap-", "ar-"))],
              "gl": gl.get("exceptions") or [], "treasury": tr.get("exceptions") or []}
    out = {}
    for src, ex in groups.items():
        miss = [x for x in RULE_SOURCES[src] if x in failed]
        out[src] = ({"skipped": "Business Central did not answer: " + ", ".join(miss)} if miss
                    else exreg.sync(src, company, _rule_findings(ex), actor=actor))
    return out


def register_controls(actor="scheduler", force=False):
    """The daily control run: every company of the negative stock email, read once a day (the 07:30 email has
    usually read it already), the sales reconciliation Simphony ↔ BC of every company with restaurants, and
    Simphony ↔ Billy of every company with a Billy account."""
    out = []
    try:
        known = {c.upper() for c in client.companies()}
    except Exception:                                               # noqa: BLE001
        known = None
    for c in salesrec.companies():
        if known is not None and c.upper() not in known:
            continue
        if not force and exreg.synced_today("sales_rec", c):
            out.append({"company": c, "control": "sales_rec", "status": "already read today"})
            continue
        try:
            r = salesrec.build(client, c, _latest)
            salesrec.remember(c, r)
            reg = register_after_sales(c, r)
            out.append({"company": c, "control": "sales_rec", "status": "synced" if reg else "not synced",
                        "sales_rec": reg, "detail": "" if reg else "; ".join(r.get("errors") or [])[:300]})
        except Exception as exc:                                    # noqa: BLE001
            traceback.print_exc()
            out.append({"company": c, "control": "sales_rec", "status": "failed", "detail": str(exc)[:300]})
    for c in rcpt.companies():
        if known is not None and c.upper() not in known:
            continue
        if not force and exreg.synced_today("receipts_rec", c):
            out.append({"company": c, "control": "receipts_rec", "status": "already read today"})
            continue
        try:
            r = rcpt.build(client, c, _latest, in_parallel_jobs)
            rcpt.remember(c, r)
            reg = register_after_receipts(c, r)
            out.append({"company": c, "control": "receipts_rec", "status": "synced" if reg else "not synced",
                        "receipts_rec": reg, "detail": "" if reg else "; ".join(r.get("errors") or [])[:300]})
        except Exception as exc:                                    # noqa: BLE001
            traceback.print_exc()
            out.append({"company": c, "control": "receipts_rec", "status": "failed", "detail": str(exc)[:300]})
    for c in billyrec.companies():                    # Simphony ↔ Billy: every company with a Billy account (its NUIS)
        if not force and exreg.synced_today("billy_rec", c):
            out.append({"company": c, "control": "billy_rec", "status": "already read today"})
            continue
        try:
            r = billyrec.build(c, _latest)
            billyrec.remember(c, r)
            reg = register_after_billy(c, r)
            out.append({"company": c, "control": "billy_rec", "status": "synced" if reg else "not synced",
                        "billy_rec": reg, "detail": "" if reg else "; ".join(r.get("errors") or [])[:300]})
        except Exception as exc:                                    # noqa: BLE001
            traceback.print_exc()
            out.append({"company": c, "control": "billy_rec", "status": "failed", "detail": str(exc)[:300]})
    try:
        sr_companies = srsim.companies()           # SevenRooms ↔ Simphony: every company with a SevenRooms venue placed
    except Exception as exc:                                        # noqa: BLE001
        sr_companies = []
        out.append({"company": "", "control": "sevenrooms", "status": "failed", "detail": str(exc)[:300]})
    for c in sr_companies:
        if known is not None and c.upper() not in known:
            continue
        if not force and exreg.synced_today("sevenrooms", c):
            out.append({"company": c, "control": "sevenrooms", "status": "already read today"})
            continue
        try:
            frm, to = srsim.default_range()
            r = srsim.build(c, frm, to, _latest)
            reg = register_after_sevenrooms(c, r)
            r["register"] = reg
            srsim.remember(c, r)
            out.append({"company": c, "control": "sevenrooms", "status": "synced" if reg else "not synced",
                        "sevenrooms": reg, "detail": "" if reg else "; ".join(r.get("errors") or [])[:300]})
        except Exception as exc:                                    # noqa: BLE001
            traceback.print_exc()
            out.append({"company": c, "control": "sevenrooms", "status": "failed", "detail": str(exc)[:300]})
    # the rules of the Finance screens (Exceptions before): every company of the other controls, and the default one
    rule_cos = list(dict.fromkeys([config.BC_COMPANY] + list(negrep.companies()) + list(salesrec.companies())))
    for c in rule_cos:
        if known is not None and c.upper() not in known:
            continue
        if not force and exreg.synced_today("ap_ar", c):
            out.append({"company": c, "control": "rules", "status": "already read today"})
            continue
        try:
            reg = register_finance_rules(c)
            out.append({"company": c, "control": "rules", "status": "synced", "rules": reg})
        except Exception as exc:                                    # noqa: BLE001
            traceback.print_exc()
            out.append({"company": c, "control": "rules", "status": "failed", "detail": str(exc)[:300]})
    for c in negrep.companies():
        if not force and exreg.synced_today("negative_stock", c):
            out.append({"company": c, "status": "already read today"})
            continue
        try:
            r = negrep.build(c)
            if r.get("register"):
                out.append({"company": c, "status": "synced", **r["register"]})
            else:
                out.append({"company": c, "status": "not synced",
                            "detail": "Business Central did not answer in full: " + "; ".join(r.get("errors") or [])[:300]})
        except Exception as exc:                                    # noqa: BLE001
            traceback.print_exc()
            out.append({"company": c, "status": "failed", "detail": str(exc)[:300]})
    store.audit("controls", "exceptions register", actor, {}, {"companies": len(out)}, "")
    return out


def register_allow(user):
    """What a person limited to some companies or locations may see of the register; None = everything."""
    if not access.limited(user):
        return None
    cache = {}

    def ok(company, location):
        if not access.company_ok(user, company):
            return False
        if company not in cache:
            L = access.locations(user, company)
            cache[company] = None if L is None else scope_expand(company, L)
        return cache[company] is None or (location or "").upper() in cache[company]
    return ok


def register_status(user):
    me = (user.get("email") or "").strip().lower()
    return {"settings": exreg.settings(), "job": exreg.job(), "recent": exreg.recent_runs(),
            "mail": arap_notify.mail_status(), "me": me,
            "can_manage": auth.can(user, "actions") or auth.can(user, "settings"),
            "can_settings": auth.can(user, "settings")}


def negative_report_status():
    return {"settings": negrep.settings(), "companies": negrep.companies(), "recipients": negrep.recipients(),
            "next_run": negrep.next_run(), "runs": negstock.recent_runs(), "mail": arap_notify.mail_status(),
            "as_of": negrep.as_of().isoformat()}


brandrep = brand_report.Reporter(client, _latest, in_parallel_jobs, company_currency)


def restaurants_by_company():
    """{company: [{key, name, brand}]} of the restaurant registry, for Email recipients: the key is the
    restaurant's BC location code, else its department, else its Simphony reference (what the location
    emails of both towers look a location up by)."""
    out = {}
    try:
        rows = locintel.registry()
    except Exception:                                               # noqa: BLE001
        return out
    for r in rows:
        if not r.get("company"):
            continue
        key = (r.get("bc") or r.get("dept") or r.get("ref") or "").strip()
        if key:
            out.setdefault(r["company"], []).append({"key": key, "name": r.get("name") or key, "brand": r.get("brand") or ""})
    return out


salesrep = sales_report.Reporter(client, _latest, in_parallel_jobs, company_currency)


plmemo = pl_memo.Reporter(client, company_currency)


def pl_memo_status():
    s = pl_memo.settings()
    try:
        cos = client.companies()
    except Exception:                                               # noqa: BLE001
        cos = []
    m = pl_memo.report_month()
    return {"settings": s, "next_run": plmemo.next_run(), "runs": pl_memo.recent_runs(), "mail": arap_notify.mail_status(),
            "month": m, "companies": pl_memo.companies(s), "all_companies": cos,
            "plan": [{"companies": c, "to": to, "cc": cc} for c, to, cc in plmemo.plan()],
            "locations": [{"company": c, "dept": d, "name": n, "to": to, "cc": cc} for c, d, n, to, cc in plmemo.location_plan()]}


def sales_report_status():
    to, cc, source = salesrep.group_recipients()
    reg = sales_report.registry()
    return {"settings": sales_report.settings(), "next_run": salesrep.next_run(), "runs": sales_report.recent_runs(),
            "mail": arap_notify.mail_status(), "to": to, "cc": cc, "source": source,
            "locations": [{"ref": r["ref"], "name": r.get("name") or r["ref"], "brand": r.get("brand") or "",
                           "company": r.get("company") or ""} for r in reg],
            "location_recipients": [{"ref": row["ref"], "name": row.get("name") or row["ref"], "to": lto, "cc": lcc}
                                    for row, lto, lcc in salesrep.location_recipients()],
            "week": [d.isoformat() for d in brand_report.last_week()]}


def brand_report_status():
    br = brand_report.brands()
    return {"settings": brand_report.settings(), "next_runs": brandrep.next_runs(), "runs": brand_report.recent_runs(),
            "mail": arap_notify.mail_status(), "simphony": bool(simphony.info().get("configured")),
            "kinds": brand_report.KINDS,
            "brands": [{"brand": b, "locations": [r["name"] for r in rows],
                        "to": brand_report.recipients_of(b)[0]} for b, rows in br.items()],
            "cc": brand_report.recipients_of("")[1],
            "week": [d.isoformat() for d in brand_report.last_week()],
            "month": [d.isoformat() for d in brand_report.last_month()]}


def arap_status(user):
    return {"settings": arap.settings(), "companies": arap.companies(), "recipients": arap.recipients(),
            "next_run": arap.next_run(), "runs": arap_notify.recent_runs(), "mail": arap_notify.mail_status(),
            "test": arap.test_status(user["username"])}


# ---------------------------------------------------------------- access scope (access.py)
# downloads: the export action of the role (Admin → Roles & permissions → Actions)
EXPORT_PATHS = {"/api/inventory/items/xlsx", "/api/billyrec/xlsx", "/api/sales/prices/xlsx", "/api/sales/menu/xlsx", "/api/sales/channel-profit/xlsx", "/api/inventory/sku/xlsx", "/api/movement/xlsx", "/api/fiscal/xlsx", "/api/typology/xlsx",
                "/api/discharge/xlsx", "/api/stockcount/xlsx", "/api/shrinkage/xlsx", "/api/sales/stockstop/xlsx",
                "/api/recipes/xlsx", "/api/supplier-stock/statement"}
# Asked by anyone signed in, whatever their scope.
ACCESS_FREE = {"/api/session", "/api/logout", "/api/password", "/api/health", "/api/simphony/status", "/api/users",
               "/api/cache",
               # the exceptions register cuts every answer to the person's scope itself (register_allow)
               "/api/register", "/api/register/update"}
# The location screens: answered to a person limited to some locations, cut to them. Everything else shows the
# whole company and is refused to them.
ACCESS_LOCATION_PATHS = {"/api/finance/locations", "/api/cash/locations", "/api/kartela/locations",
                         "/api/inventory/locations", "/api/inventory/sku", "/api/inventory/sku/location",
                         "/api/inventory/sku/xlsx", "/api/inventory/items", "/api/inventory/items/xlsx", "/api/today/locations", "/api/assets/locations", "/api/locations",
                         "/api/locations/registry",
                         "/api/sales", "/api/sales/channels", "/api/sales/prices",
                         "/api/sales/prices/xlsx", "/api/sales/menu", "/api/sales/menu/xlsx",
                         "/api/sales/foodcost",
                         "/api/discharge", "/api/discharge/xlsx",
                         # Inventory: Movement & dead stock, Typology & turnover, Stock count analysis
                         "/api/movement", "/api/movement/rows", "/api/movement/xlsx",
                         "/api/typology", "/api/typology/xlsx", "/api/stockcount", "/api/stockcount/xlsx",
                         "/api/shrinkage", "/api/shrinkage/xlsx", "/api/shrinkage/overview", "/api/shrinkage/config",
                         "/api/shrinkage/count",
                         # Sales → Blocked by stock: cut to the person's locations
                         "/api/sales/stockstop", "/api/sales/stockstop/xlsx",
                         # Sales → SevenRooms ↔ Simphony: cut to the person's restaurants
                         "/api/sevenrooms", "/api/sevenrooms/xlsx",
                         # Control: Simphony ↔ BC and Takings POS ↔ BC, by restaurant
                         "/api/salesrec", "/api/receipts", "/api/billyrec", "/api/billyrec/xlsx", "/api/billyrec/rows",
                         # Budget & forecast → Location intelligence, by department
                         "/api/executive"}


def scope_expand(company, codes):
    """BC location codes → also the department and Simphony reference the registry gives the same restaurant."""
    out = set(codes)
    for r in locintel.registry():
        if (r.get("company") or "").upper() == (company or "").upper() and (r.get("bc") or "").upper() in codes:
            out |= {(r.get(k) or "").upper() for k in ("dept", "ref", "bc") if r.get(k)}
    return out


def simphony_refs_in_scope(user):
    """The Simphony locations (registry references, upper case) a person limited by access may see; None = all.
    A restaurant counts when its company is in the access and, for a company limited to some locations, its BC
    location or department is one of them."""
    if not access.limited(user):
        return None
    out = set()
    for r in locintel.registry():
        co = r.get("company") or ""
        if not co or not access.company_ok(user, co):
            continue
        L = access.locations(user, co)
        if L is None or ({(r.get(k) or "").upper() for k in ("bc", "dept", "ref")} & L):
            out.add((r.get("ref") or "").upper())
    out.discard("")
    return out


def scope_options():
    """Every company of Business Central and its locations (code, and the registry's restaurant when there is one)."""
    reg = locintel.registry()
    out, errors = [], []
    names = client.companies()
    # every company at once: one after the other, a cold read of a dozen companies outlasts identity's wait
    read = in_parallel_jobs({c: (lambda c=c: client.locations(c)) for c in names}, workers=8)
    for company in names:
        locs = []
        codes, exc = read.get(company, (None, None))
        if exc is not None or codes is None:
            codes = []
            errors.append(f"{company}: {str(exc)[:120]}")
        by_bc = {(r.get("bc") or "").upper(): r for r in reg
                 if (r.get("company") or "").upper() == company.upper() and r.get("bc")}
        for c in codes:
            r = by_bc.get(c.upper())
            locs.append({"code": c, "name": (r or {}).get("name") or "", "brand": (r or {}).get("brand") or "",
                         "restaurant": bool(r)})
        out.append({"company": company, "locations": locs})
    return {"companies": out, "errors": errors}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

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

    def _access(self, path, q, user, body=None):
        """The company and location scope of the person (access.py): a refusal to send, or None. Sets the filter
        that cuts the answer of a location screen to the locations in scope."""
        if not access.limited(user) or path in ACCESS_FREE:
            return None
        if body is not None and not body.get("company"):
            return None                     # a change that names no company (an action, a setting): the role decides
        company = q.get("company") or (body or {}).get("company") or config.BC_COMPANY
        if path == "/api/companies":
            self._scope_filter = lambda b: dict(b, companies=access.filter_companies(b.get("companies"), user))
            return None
        multi = path in ("/api/sales", "/api/sales/channels", "/api/sales/prices", "/api/sales/prices/xlsx", "/api/sales/menu",
                        "/api/sales/menu/xlsx", "/api/sales/foodcost", "/api/sales/channel-profit", "/api/sales/channel-profit/xlsx",
                        "/api/sales/channel-profit/params") or (path == "/api/today/locations" and q.get("scope") == "all")
        if not multi and not access.company_ok(user, company):
            return access.refuse_company(company)
        L = access.locations(user, company)
        if L is not None and not multi:
            if body is not None or path not in ACCESS_LOCATION_PATHS:
                return access.refuse_company_wide(user, company)
            if path in ("/api/inventory/sku/location", "/api/inventory/sku/xlsx"):
                code = (q.get("code") or "").upper()
                if code not in L:
                    return {"error": f"Choose one of your locations: {access.describe(user, company)}.",
                            "access": "location"}
        # the codes of the scope (BC code, department, Simphony reference): the screens that cut their answer
        # themselves (movement, typology, stock count, inventory) read them from here
        self._allowed = (scope_expand(company, L) | L) if L is not None else None
        if path == "/api/sales":                                    # Sales: their restaurants, totals again
            self._scope_filter = lambda b: sales_cut(b, user, company)
        elif path in ("/api/inventory/items", "/api/inventory/items/xlsx"):
            self._scope_filter = None           # every warehouse, by decision (Geri, 10 Nov 2026): to ask for transfers
        elif path in ("/api/inventory/sku/location", "/api/inventory/sku/xlsx"):   # one location of the scope
            self._scope_filter = lambda b: access.filter_payload(b, user, company, scope_expand, ("all_locations",), False)
        elif path in ("/api/discharge", "/api/discharge/xlsx"):     # its lines, interventions and squares; no company view
            self._scope_filter = lambda b: recipeuse.cut(b, self._allowed)
        elif path in ("/api/movement", "/api/movement/rows", "/api/movement/xlsx", "/api/typology", "/api/typology/xlsx",
                      "/api/stockcount", "/api/stockcount/xlsx", "/api/shrinkage", "/api/shrinkage/xlsx",
                      "/api/shrinkage/overview", "/api/shrinkage/config", "/api/shrinkage/count"):
            self._scope_filter = None                                   # cut by the handler (self._allowed)
        elif path in ("/api/sales/stockstop", "/api/sales/stockstop/xlsx"):
            self._scope_filter = lambda b: salestop.cut(b, self._allowed)
        elif path in ("/api/sevenrooms", "/api/sevenrooms/xlsx"):
            self._scope_filter = None                                   # cut by the handler (self._allowed)
        elif path == "/api/executive":
            self._scope_filter = lambda b: executive.cut(b, self._allowed)
        elif path == "/api/salesrec":
            self._scope_filter = lambda b: salesrec.cut(b, self._allowed)
        elif path == "/api/billyrec":
            self._scope_filter = lambda b: billyrec.cut(b, self._allowed)
        elif path in ("/api/billyrec/rows", "/api/billyrec/xlsx"):
            self._scope_filter = None                                   # cut by the handler (self._allowed)
        elif path == "/api/receipts":
            self._scope_filter = lambda b: dict(rcpt.cut(b, self._allowed), can_settings=False)
        else:
            self._scope_filter = lambda b: access.filter_payload(b, user, company, scope_expand)
        return None

    def _stockcount(self, q, user, xlsx):
        """Inventory → Stock count analysis: the month's valuation of a location against its count."""
        company = q.get("company") or config.BC_COMPANY
        month = q.get("month") or stockcount.default_month()
        try:
            stockcount.month_range(month)
        except (ValueError, TypeError):
            month = stockcount.default_month()
        location = (q.get("location") or "").strip()
        try:
            codes = client.locations(company)
        except BCError as exc:
            self._send(200, {"available": False, "error": str(exc)[:300]})
            return
        A = self._allowed                                   # a person limited to some locations: theirs only
        if A is not None:
            codes = [c for c in codes if c.upper() in A]
        names = {(r.get("bc") or "").upper(): r.get("name") for r in locintel.registry()
                 if (r.get("company") or "").upper() == company.upper() and r.get("bc")}
        today = date.today().replace(day=1)
        setup = {"company": company, "month": month, "default_month": stockcount.default_month(),
                 "months": [(today - timedelta(days=1 + 31 * i)).strftime("%Y-%m") for i in range(13)],
                 "locations": [{"code": c, "name": names.get(c.upper()) or ""} for c in codes],
                 "counts": [c for c in stockcount.counts(company) if A is None or (c.get("location") or "").upper() in A],
                 "can_upload": auth.can(user, "actions") and A is None,
                 "can_export": auth.can(user, "export")}
        if xlsx:
            hit = stockcount.request(client, company, location, month) if location in codes else {}
            if not hit.get("available"):
                self._send(409, {"error": "Read the analysis first."})
                return
            body = stockcount.render_xlsx(hit)
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header("Content-Disposition", f'attachment; filename="RHG_{company.replace(" ", "_")}_'
                             f'stock_count_{location.replace(" ", "_")}_{month}.xlsx"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if not location:
            self._send(200, {"available": False, "setup": setup})
            return
        if location not in codes:
            self._send(200, {"available": False, "setup": setup,
                             "error": f"{location} is not a location of {company}." if A is None
                             else f"Choose one of your locations: {', '.join(codes) or 'none'}."})
            return
        r = stockcount.request(client, company, location, month, q.get("refresh") == "1")
        self._send(200, dict(r, setup=setup))

    def _shrinkage(self, q, user, path):
        """Inventory → Shrinkage KPIs: a location's count differences by category, month by month; every location's
        count adjustments of a month (location empty); the categories and waste table (config)."""
        company = q.get("company") or config.BC_COMPANY
        try:
            codes = client.locations(company)
        except BCError as exc:
            self._send(200, {"available": False, "error": str(exc)[:300]})
            return
        A = self._allowed
        if A is not None:
            codes = [c for c in codes if c.upper() in A]
        if path == "/api/shrinkage/config":
            cfg = shrinkage.get_config()
            ov = cfg["overrides"].get(company) or {}
            try:
                cards = shrinkage.item_cards(client, company)
            except BCError:
                cards = {}
            rows = []
            for no, o in sorted(ov.items()):
                card = cards.get(no) or {}
                rows.append({"no": no, "desc": (card.get("Description") or "").strip(), "kat2": o["kat2"],
                             "by_map": shrinkage.category(card, cfg, company, overrides=False) or f"{shrinkage.bc_group(card)} (BC)",
                             "g4": card.get("Item_Category_Group_4") or "", "g5": card.get("Item_Category_Group_5") or "",
                             "source": o.get("source"), "by": o.get("by"), "at": o.get("at")})
            self._send(200, {"company": company, "categories": cfg["categories"], "map": cfg["map"],
                             "prefixes": cfg["prefixes"], "status": cfg["status"], "history": cfg["history"][:30],
                             "overrides": rows, "updated_at": cfg["updated_at"], "updated_by": cfg["updated_by"],
                             "can_edit": auth.can(user, "settings")})
            return
        names = {(r.get("bc") or "").upper(): r.get("name") for r in locintel.registry()
                 if (r.get("company") or "").upper() == company.upper() and r.get("bc")}
        today = date.today()
        setup = {"company": company, "locations": [{"code": c, "name": names.get(c.upper()) or ""} for c in codes],
                 "years": [today.year - i for i in range(3)], "default_month": stockcount.default_month(),
                 "months": [(today.replace(day=1) - timedelta(days=1 + 31 * i)).strftime("%Y-%m") for i in range(13)],
                 "can_settings": auth.can(user, "settings"), "can_export": auth.can(user, "export"),
                 "can_upload": auth.can(user, "actions") and A is None,     # as Stock count analysis: not by location
                 "status": shrinkage.get_config()["status"]}
        if path == "/api/shrinkage/overview":
            month = q.get("month") or stockcount.default_month()
            try:
                stockcount.month_range(month)
            except (ValueError, TypeError):
                month = stockcount.default_month()
            r = shrinkage.request_overview(client, company, month, q.get("refresh") == "1")
            if isinstance(r, dict) and r.get("locations") is not None and A is not None:
                r = dict(r, locations=[x for x in r["locations"] if x["location"].upper() in A])
            self._send(200, dict(r, setup=setup))
            return
        location = (q.get("location") or "").strip()
        try:
            year = int(q.get("year") or today.year)
        except ValueError:
            year = today.year
        if year not in setup["years"]:
            year = today.year
        if location not in codes:
            self._send(200, {"available": False, "setup": setup,
                             "error": "" if not location else f"Choose one of the locations: {', '.join(codes) or 'none'}."})
            return
        if path == "/api/shrinkage/xlsx":
            hit = shrinkage._held.held(f"{company}|{location}|{year}")
            if not hit or not hit.get("available"):
                self._send(409, {"error": "Read the analysis first."})
                return
            body = shrinkage.render_xlsx(hit)
            store.audit("export", "shrinkage", user["username"], None, {"location": location, "year": year}, "")
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            self.send_header("Content-Disposition", f'attachment; filename="RHG_{company.replace(" ", "_")}_'
                             f'shrinkage_{location.replace(" ", "_")}_{year}.xlsx"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        r = shrinkage.request(client, company, location, year, q.get("refresh") == "1")
        self._send(200, dict(r, setup=setup))

    def _service_call(self):
        """Another RHG service on the internal network (identity asking for Unicorn's facts)."""
        token = auth.SERVICE_TOKEN
        given = self.headers.get("X-Service-Token") or ""
        return bool(token) and hmac.compare_digest(given.encode(), token.encode())

    def _send(self, status, body, ctype="application/json; charset=utf-8"):
        if status == 200 and getattr(self, "_scope_filter", None) and isinstance(body, dict):
            body = self._scope_filter(body)
        payload = body if isinstance(body, bytes) else json.dumps(body, default=str).encode("utf-8")
        zipped = (len(payload) > 16 * 1024 and "gzip" in (self.headers.get("Accept-Encoding") or "")
                  and ctype.startswith("application/json"))
        # JSON only: the pages, scripts and styles of this tower pass through nginx's sub_filter, which rewrites
        # "/api/" into "/finance/api/" (or /purchasing/) and cannot read a compressed body (deploy/nginx/default.conf)
        if zipped:                                       # a large answer or page (index.html, Simphony ↔ Billy…) travels compressed
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
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:
            return {}

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
        self._allowed = None
        if url.path == "/internal/registry":
            # Purchasing → Negotiations: the restaurants' brand, BC location and department, to filter by brand
            if not self._service_call():
                self._send(403, {"error": "Forbidden."})
            else:
                self._send(200, {"registry": [{k: r.get(k) or "" for k in ("ref", "name", "brand", "company", "bc", "dept")}
                                              for r in locintel.registry()]})
            return
        if url.path == "/internal/scope-options":
            # identity's Admin → Accounts → access: the companies and their locations to choose from
            if not self._service_call():
                self._send(403, {"error": "Forbidden."})
            else:
                self._send(200, scope_options())
            return
        user = self._user()
        try:
            if url.path in ("/login", "/login.html"):
                # Sign-in is central now.
                self._redirect("/")
            elif url.path == "/api/session":
                self._send(200, {"user": user, "roles": auth.ROLES})
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
            elif url.path == "/api/dashboard":
                self._send(200, dashboard(q.get("company") or config.BC_COMPANY, q.get("period"),
                                          q.get("mode", "month"), q.get("dimension"),
                                          q.get("compare") or "prior"))
            elif url.path == "/api/gl":
                self._send(200, gl_screen(q.get("company") or config.BC_COMPANY, q.get("period"),
                                          q.get("mode", "month"), q.get("days"), q.get("threshold")))
            elif url.path == "/api/sales/channel-profit/params":
                self._send(200, {"config": chanprof.load_config(),
                                 "fields": [{"key": k, "label": lb, "unit": u} for k, lb, u in chanprof.CHANNEL_FIELDS]})
            elif url.path in ("/api/sales/stockstop", "/api/sales/stockstop/xlsx"):
                # Sales → Blocked by stock: the sales BC cannot post for want of stock, and what today's will block
                if not auth.can(user, "finance.s.sales.stockstop"):
                    self._send(403, {"error": "Your role does not include Sales blocked by stock."})
                    return
                company = q.get("company") or config.BC_COMPANY
                if url.path.endswith("/xlsx"):
                    hit = salestop._held.held(company)
                    if not hit or not hit.get("available"):
                        self._send(409, {"error": "Read the report first."})
                        return
                    body = salestop.render_xlsx(salestop.cut(hit, self._allowed))
                    store.audit("export", "salestop", user["username"], None, {"company": company}, "")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                    self.send_header("Content-Disposition", f'attachment; filename="RHG_{company.replace(" ", "_")}_'
                                     f'sales_blocked_by_stock_{date.today().isoformat()}.xlsx"')
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                r = salestop.request(client, company, q.get("refresh") == "1")
                self._send(200, dict(r, can_export=auth.can(user, "export")) if isinstance(r, dict) else r)
            elif url.path in ("/api/sevenrooms", "/api/sevenrooms/xlsx", "/api/sevenrooms/settings"):
                # Sales → SevenRooms ↔ Simphony: reservations, covers and feedback against Simphony's checks (srsim.py)
                if not auth.can(user, "finance.s.sales.sevenrooms"):
                    self._send(403, {"error": "Your role does not include SevenRooms ↔ Simphony."})
                    return
                if url.path.endswith("/settings"):           # the connection and the venues (Settings permission)
                    if not auth.can(user, "settings"):
                        self._send(403, {"error": "Your role does not include Settings."})
                        return
                    out = {"connection": sevenrooms.info(), "venues": [], "restaurants": []}
                    try:
                        if out["connection"]["configured"]:
                            regs = locintel.registry()
                            out["restaurants"] = [{"ref": r["ref"], "name": r.get("name") or r["ref"], "company": r.get("company") or ""}
                                                  for r in regs if r.get("ref")]
                            out["venues"] = [dict(v, ref=(r or {}).get("ref") or "", restaurant=(r or {}).get("name") or "", how=how)
                                             for v, r, how in srsim.place(sevenrooms.venues(), regs, out["connection"]["map"])]
                            out["note"] = sevenrooms._venues.get("note") or ""
                    except sevenrooms.Error as exc:
                        out["error"] = str(exc)[:300]
                    self._send(200, out)
                    return
                company = q.get("company") or config.BC_COMPANY
                frm, to = q.get("from") or None, q.get("to") or None
                if url.path.endswith("/xlsx"):
                    hit = srsim.held(company, frm, to) if frm and to else None
                    if not hit or "agg" not in hit:
                        self._send(409, {"error": "Read SevenRooms ↔ Simphony first."})
                        return
                    hit = srsim.cut(hit, self._allowed)
                    body = srsim.render_xlsx(hit)
                    store.audit("export", f"sevenrooms · {company}", user["username"], None, {"from": frm, "to": to}, "")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                    self.send_header("Content-Disposition", f'attachment; filename="RHG_{re.sub(r"[^A-Za-z0-9_-]+", "_", company)}_sevenrooms_simphony_{frm}_{to}.xlsx"')
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                try:
                    r = srsim.request(company, frm, to, q.get("refresh") == "1", _latest)
                except ValueError as exc:
                    r = {"available": False, "error": str(exc)}
                if isinstance(r, dict) and "agg" in r:
                    r = srsim.public(srsim.cut(r, self._allowed))
                self._send(200, dict(r, can_settings=auth.can(user, "settings"), configured=sevenrooms.info()["configured"])
                           if isinstance(r, dict) else r)
            elif url.path in ("/api/sales/channel-profit", "/api/sales/channel-profit/xlsx"):
                # Sales → Channel profitability: a P&L of every order type (Simphony) with costs from BC and parameters
                if not simphony.info()["configured"]:
                    self._send(200, {"error": "The Simphony connection is not set."})
                    return
                try:
                    per = periods(q.get("period"), q.get("mode", "month"))
                except ValueError as exc:
                    self._send(400, {"error": str(exc)})
                    return
                xlsx = url.path.endswith("xlsx")
                cfg = chanprof.load_config()
                prefixes = chanprof.bc_prefixes(cfg)
                refs = simphony_refs_in_scope(user)
                key = (per[0].isoformat(), per[1].isoformat(), tuple(sorted(refs)) if refs is not None else None, prefixes)
                ds = chanprof.request(per, _latest, client, key, refs, prefixes, q.get("refresh") == "1" and not xlsx)
                if ds.get("pending") or ds.get("available") is False:
                    if xlsx:
                        self._send(409, {"error": "The channels are still being read; try again in a moment."})
                    else:
                        self._send(200, ds)
                    return
                level = q.get("level") if q.get("level") in ("brand", "location", "all") else "all"
                view = "month" if q.get("view") == "month" else "channel"
                r = chanprof.analyse(ds, cfg, level, (q.get("key") or "")[:60], view,
                                     chanprof.channel_key(q.get("channel") or "")[:60])
                if not xlsx:
                    self._send(200, r)
                    return
                body = chanprof.render_xlsx(r)
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                safe = re.sub(r"[^A-Za-z0-9._-]+", "_", r["title"] + ("_" + r["channel"] if view == "month" else ""))[:50]
                self.send_header("Content-Disposition", f'attachment; filename="RHG_channel_profitability_{safe}_'
                                 f'{r["period"]["from"]}_{r["period"]["to"]}.xlsx"')
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                store.audit("export", f"channel profitability · {r['title']}", user["username"], {}, {}, r["period"]["from"])
            elif url.path in ("/api/sales/menu", "/api/sales/menu/xlsx"):
                # Sales → Menu engineering: popularity and margin of every menu item (Simphony + BC recipes)
                if not simphony.info()["configured"]:
                    self._send(200, {"error": "The Simphony connection is not set."})
                    return
                try:
                    per = periods(q.get("period"), q.get("mode", "month"))
                except ValueError as exc:
                    self._send(400, {"error": str(exc)})
                    return
                refs = simphony_refs_in_scope(user)
                key = (per[0].isoformat(), per[1].isoformat(), tuple(sorted(refs)) if refs is not None else None)
                ds = menueng.request(per, _latest, client, key, refs, q.get("refresh") == "1" and url.path == "/api/sales/menu")
                if ds.get("pending") or ds.get("available") is False:
                    self._send(200 if not url.path.endswith("xlsx") else 409, ds if not url.path.endswith("xlsx")
                               else {"error": "The menu items are still being read; try again in a moment."})
                    return
                level = q.get("level") if q.get("level") in ("brand", "location", "all") else "all"
                r = menueng.analyse(ds, level, (q.get("key") or "")[:60], q.get("section") or "family")
                if not url.path.endswith("xlsx"):
                    self._send(200, r)
                    return
                body = menueng.render_xlsx(r)
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                safe = re.sub(r"[^A-Za-z0-9._-]+", "_", r["title"])[:40]
                self.send_header("Content-Disposition", f'attachment; filename="RHG_menu_engineering_{safe}_'
                                 f'{r["period"]["from"]}_{r["period"]["to"]}.xlsx"')
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                store.audit("export", f"menu engineering · {r['title']}", user["username"], {}, {}, r["period"]["from"])
            elif url.path == "/api/sales/foodcost":
                # Sales → Menu & food cost: the weekly theoretical food cost % (Simphony × BC recipes) and the
                # monthly actual of the P&L (the Executive dashboard's months), for all RHG, a brand or a location
                if not simphony.info()["configured"]:
                    self._send(200, {"error": "The Simphony connection is not set."})
                    return
                try:
                    weeks = max(4, min(int(q.get("weeks") or 13), 52))
                except ValueError:
                    weeks = 13
                refs = simphony_refs_in_scope(user)
                per = menucost.trend_period(weeks)
                key = ("foodcost", per[0].isoformat(), per[1].isoformat(), tuple(sorted(refs)) if refs is not None else None)
                ds = menueng.request(per, _latest, client, key, refs, q.get("refresh") == "1")
                if ds.get("pending") or ds.get("available") is False:
                    self._send(200, ds)
                    return
                level = q.get("level") if q.get("level") in ("brand", "location", "all") else "all"
                r = menucost.weekly(ds, level, (q.get("key") or "")[:60])
                sel = r.pop("selection")
                if not auth.can(user, "finance.pl.view"):
                    r["actual"] = {"available": False, "reason": "Your role does not include the P&L."}
                elif not sel:
                    r["actual"] = {"available": False, "reason": "No location in this selection."}
                else:
                    try:
                        r["actual"] = menucost.actual(client, sel, level, per[0], company_currency, limited=refs is not None)
                    except BCError as exc:
                        r["actual"] = {"available": False, "reason": str(exc)[:200]}
                r["weeks_asked"] = weeks
                self._send(200, r)
            elif url.path in ("/api/sales/prices", "/api/sales/prices/xlsx"):
                # Sales → Menu prices: every active Simphony menu item price by location, revenue centre and level
                if not simphony.info()["configured"]:
                    self._send(200, {"error": "The Simphony connection is not set."})
                    return
                refs = simphony_refs_in_scope(user)
                xlsx = url.path.endswith("xlsx")
                try:
                    r = pricelist.request(refs, (q.get("location") or "")[:40], (q.get("level") or "")[:99],
                                          (q.get("group") or "")[:99], (q.get("q") or "")[:80],
                                          0 if xlsx else 600, q.get("refresh") == "1")
                except simphony.SimphonyError as exc:
                    self._send(502, {"error": str(exc)})
                    return
                if not xlsx:
                    self._send(200, r)
                    return
                body = pricelist.render_xlsx(r)
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                self.send_header("Content-Disposition",
                                 f'attachment; filename="Menu_prices_{date.today().isoformat()}.xlsx"')
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                store.audit("export", "menu prices", user["username"], {}, {"rows": r["count"]}, "")
            elif url.path == "/api/sales/channels":
                # Sales → Location intelligence: channels (order types), revenue centres, day parts and delivery
                # partners of every restaurant against the period before and last year (channels.py)
                if not simphony.info()["configured"]:
                    self._send(200, {"available": False, "error": "The Simphony connection is not set."})
                    return
                try:
                    per = periods(q.get("period"), q.get("mode", "month"))
                except ValueError:
                    self._send(400, {"error": "The period must be a date, e.g. 2026-10-01."})
                    return
                self._send(200, channels.request(per, _latest, (q.get("period") or "", q.get("mode", "month")),
                                                 q.get("refresh") == "1"))
            elif url.path == "/api/sales":
                self._send(200, sales_request(q.get("period"), q.get("mode", "month"), q.get("refresh") == "1",
                                              q.get("compare") or "prior"))
            elif url.path == "/api/locations/registry":
                # the restaurants of the company as the registry names them (Settings → Location intelligence): one
                # place's department, BC location and Simphony reference, so its pop-up finds it in every area
                co = q.get("company") or config.BC_COMPANY
                self._send(200, {"company": co, "locations": [
                    {k: r.get(k) or "" for k in ("ref", "name", "brand", "bc", "dept", "company")}
                    for r in locintel.registry() if (r.get("company") or "").upper() == co.upper()]})
            elif url.path == "/api/locations":
                try:
                    c_from, c_to, p_from, p_to, ly_from, ly_to, running, label, _ = periods(q.get("period"), "range" if q.get("mode") == "range" else "month")
                except ValueError:
                    self._send(400, {"error": "The period must be a date, e.g. 2026-10-01."})
                    return
                rights = {"purchases": auth.can(user, "finance.ap.view"),
                          "inventory": auth.can(user, "finance.inventory.view")}
                res = locintel.request(client, q.get("company") or config.BC_COMPANY,
                                       (c_from, c_to, p_from, p_to, ly_from, ly_to, running, label),
                                       _latest, in_parallel_jobs, rights, q.get("refresh") == "1")
                if isinstance(res, dict) and res.get("locations") and auth.can(user, "finance.s.register"):
                    # the open items of the Exceptions register on each restaurant (BC location, department or
                    # Simphony reference), counted now, not held with the read
                    res = dict(res, locations=[dict(l) for l in res["locations"]], register=True)
                    allow = register_allow(user)
                    by_co = {}
                    for l in res["locations"]:
                        co = l.get("company") or ""
                        if not co or (allow and not allow(co, l.get("bc") or "")):
                            continue
                        cnt = by_co.setdefault(co, exreg.counts_by_location(co))
                        keys = {(l.get(k) or "").upper() for k in ("bc", "dept", "ref") if l.get(k)}
                        tot = {"open": 0, "critical": 0, "overdue": 0, "value": 0.0}
                        for k in keys:
                            for f in tot:
                                tot[f] += (cnt.get(k) or {}).get(f, 0)
                        l["exceptions"] = dict(tot, value=round(tot["value"]))
                self._send(200, res)
            elif url.path == "/api/assets/locations":
                try:
                    period = periods(q.get("period"), q.get("mode") or "month")[:8]
                except ValueError:
                    self._send(400, {"error": "The period must be a date, e.g. 2026-10-01."})
                    return
                company = q.get("company") or config.BC_COMPANY
                self._send(200, faloc.request(client, company, period, company_currency(company), q.get("refresh") == "1"))
            elif url.path == "/api/today/locations":
                company = q.get("company") or config.BC_COMPANY
                scope = "all" if q.get("scope") == "all" else "company"
                self._send(200, todayloc.request(client, company, scope, company_currency, _latest, in_parallel_jobs,
                                              q.get("refresh") == "1"))
            elif url.path in ("/api/supplier-stock", "/api/supplier-stock/statement"):
                if not auth.can(user, "finance.ap.view"):
                    self._send(403, {"error": "Your role does not include Payables."})
                    return
                company = q.get("company") or config.BC_COMPANY
                if url.path.endswith("/statement"):
                    r = supstock.held(company)
                    no = q.get("vendor") or ""
                    if not r or not any(x["no"] == no for x in r["suppliers"]):
                        self._send(404, {"error": "Open Suppliers & stock first, then the statement."})
                        return
                    name = next(x["name"] for x in r["suppliers"] if x["no"] == no)
                    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{name}_{r['as_of']}")[:80]
                    if q.get("format") == "pdf":
                        body, ctype, ext = supstock.statement_pdf(r, no, company_currency(company)), "application/pdf", "pdf"
                    else:
                        body, ctype, ext = (supstock.statement_xlsx(r, no, company_currency(company)),
                                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx")
                    self.send_response(200)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Disposition", f'attachment; filename="Statement_{safe}.{ext}"')
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    store.audit("export", f"supplier statement · {name}", user["username"], {}, {"format": ext}, company)
                    return
                self._send(200, supstock.request(client, company, 18, q.get("refresh") == "1"))
            elif url.path == "/api/kartela/locations":
                try:
                    period = periods(q.get("period"), "range" if q.get("mode") == "range" else "month")[:8]
                except ValueError:
                    self._send(400, {"error": "The period must be a date, e.g. 2026-10-01."})
                    return
                rights = {"ap": auth.can(user, "finance.ap.view"), "ar": auth.can(user, "finance.ar.view")}
                if not (rights["ap"] or rights["ar"]):
                    self._send(403, {"error": "Your role includes neither Payables nor Receivables."})
                    return
                company = q.get("company") or config.BC_COMPANY
                self._send(200, kartloc.request(client, company, period, company_currency(company), rights,
                                                q.get("refresh") == "1"))
            elif url.path == "/api/cash/locations":
                try:
                    period = periods(q.get("period"), "range" if q.get("mode") == "range" else "month")[:8]
                except ValueError:
                    self._send(400, {"error": "The period must be a date, e.g. 2026-10-01."})
                    return
                company = q.get("company") or config.BC_COMPANY
                self._send(200, cashloc.request(client, company, period, company_currency(company), _latest,
                                                in_parallel_jobs, q.get("refresh") == "1"))
            elif url.path == "/api/finance/locations":
                try:
                    period = periods(q.get("period"), "range" if q.get("mode") == "range" else "month")[:8]
                except ValueError:
                    self._send(400, {"error": "The period must be a date, e.g. 2026-10-01."})
                    return
                company = q.get("company") or config.BC_COMPANY
                rights = {"pl": auth.can(user, "finance.pl.view"), "ap": auth.can(user, "finance.ap.view"),
                          "ar": auth.can(user, "finance.ar.view")}
                self._send(200, finloc.build(client, company, period, company_currency(company), _latest,
                                             in_parallel_jobs, rights, q.get("refresh") == "1"))
            elif url.path == "/api/executive":
                company = q.get("company") or config.BC_COMPANY
                try:
                    year = int(q.get("year") or date.today().year)
                except ValueError:
                    year = date.today().year
                year = max(date.today().year - 3, min(year, date.today().year))
                res = executive.request(client, company, year, company_currency(company), q.get("refresh") == "1")
                # the restaurants of the registry on this company's departments, for Budget → Location
                # intelligence to name and group the departments by brand (as the other location screens)
                reg = [{"dept": r.get("dept") or r.get("bc"), "name": r["name"], "brand": r.get("brand") or "",
                        "ref": r["ref"], "seasonal": bool(r.get("seasonal"))}
                       for r in locintel.registry() if (r.get("company") or "").upper() == company.upper()
                       and (r.get("dept") or r.get("bc"))]
                self._send(200, dict(res, registry=reg))
            elif url.path == "/api/arap-notify":
                self._send(200, arap_status(user))
            elif url.path == "/api/arap-notify/pdf":
                name, pdf = arap.pdf(q.get("company") or arap.companies()[0])
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Disposition", f'inline; filename="{name}"')
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(pdf)))
                self.end_headers()
                self.wfile.write(pdf)
            elif url.path == "/api/cost-report":
                self._send(200, cost_report_status())
            elif url.path == "/api/salesrec":
                if not auth.can(user, "finance.s.salesrec"):
                    self._send(403, {"error": "Your role does not include Simphony ↔ BC."})
                    return
                company = q.get("company") or config.BC_COMPANY
                try:
                    r = salesrec.request(client, company, _latest, q.get("refresh") == "1")
                except ValueError as exc:
                    r = {"available": False, "error": str(exc)}
                self._send(200, dict(r, companies=salesrec.companies()) if isinstance(r, dict) else r)
            elif url.path in ("/api/billyrec", "/api/billyrec/xlsx", "/api/billyrec/rows"):
                # Control → Simphony ↔ Billy: every check against its fiscalized invoice, tills, shifts, sales book
                if not auth.can(user, "finance.s.billyrec"):
                    self._send(403, {"error": "Your role does not include Simphony ↔ Billy."})
                    return
                company = q.get("company") or (billyrec.companies() or [config.BC_COMPANY])[0]
                frm, to = q.get("from") or None, q.get("to") or None      # a range asked on the screen
                if url.path.endswith(("xlsx", "rows")):
                    r = billyrec.held(company, billyrec.key_of(company, frm, to))
                    if not r:
                        self._send(409, {"error": "Read Simphony ↔ Billy first." if not frm else "Read this range first."})
                        return
                    keep = billyrec._keep_of(r, self._allowed)
                    f = {k: q.get(k) or "" for k in ("ref", "st", "k", "q")}
                    if url.path.endswith("rows"):
                        # a page of one table; a newer read than the screen's: the screen reads the totals again
                        if q.get("gen") and q.get("gen") != r["gen"]:
                            self._send(409, {"error": "A newer read is in.", "stale": True})
                            return
                        try:
                            self._send(200, billyrec.rows(r, q.get("kind") or "", keep, offset=q.get("offset") or 0,
                                                          limit=q.get("limit") or 400, **f))
                        except ValueError as exc:
                            self._send(400, {"error": str(exc)})
                        return
                    if q.get("part") == "returns":           # Returns tracking with its filters (its Export)
                        body = billyrec.render_returns_xlsx(r, keep, **f)
                        name = xlsxtable.filename(f"RHG {company} returns tracking {r['days'][0]} {r['days'][-1]}")
                        what = f"returns tracking Billy · {company}"
                    else:
                        body = billyrec.render_xlsx(r, keep)
                        name = f"RHG_libri_shitjes_billy_{re.sub(r'[^A-Za-z0-9_-]+', '_', company)}_{r['days'][0]}_{r['days'][-1]}.xlsx"
                        what = f"sales book Billy · {company}"
                    self.send_response(200)
                    self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                    self.send_header("Content-Disposition", f'attachment; filename="{name}"')
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    store.audit("export", what, user["username"], {}, {}, r["days"][0])
                    return
                try:
                    r = billyrec.request(company, _latest, q.get("refresh") == "1", frm, to)
                except ValueError as exc:
                    r = {"available": False, "error": str(exc)}
                if isinstance(r, dict) and r.get("gen"):
                    r = billyrec.public(billyrec.cut(r, self._allowed))     # cut first: the totals are the scope's
                    self._scope_filter = None
                self._send(200, dict(r, companies=billyrec.companies(), connection=billy.info()["configured"],
                                     can_settings=auth.can(user, "settings")) if isinstance(r, dict) else r)
            elif url.path == "/api/billy/sample":
                # Settings: the first invoices of a day as Billy gives them, to check how its fields come (read-only)
                if not auth.can(user, "settings"):
                    self._send(403, {"error": "Your role does not include Settings."})
                    return
                acct = billy.account(q.get("company") or "")
                if not acct:
                    self._send(200, {"error": "This company has no Billy account with a password."})
                    return
                try:
                    day = date.fromisoformat(q.get("day") or (date.today() - timedelta(days=1)).isoformat())
                    self._send(200, billy.sample(acct, "fiscal" if q.get("src") == "fiscal" else "bridge", day,
                                                 max(1, min(int(q.get("n") or 3), 10)), (q.get("fic") or "")[:80] or None))
                except (ValueError, billy.BillyError) as exc:
                    self._send(200, {"error": str(exc)[:300]})
            elif url.path == "/api/billy/settings":
                if not auth.can(user, "settings"):
                    self._send(403, {"error": "Your role does not include Settings."})
                    return
                self._send(200, {"connection": billy.info(), "settings": _billy_pub(), "companies": salesrec.companies()})
            elif url.path == "/api/kpi/mapping":
                self._send(200, kpi_mapping_screen(q.get("company") or config.BC_COMPANY))
            elif url.path == "/api/allocation":
                self._send(200, allocation_screen(q.get("company") or config.BC_COMPANY))
            elif url.path == "/api/register":
                if not auth.can(user, "finance.s.register"):
                    self._send(403, {"error": "Your role does not include the Exceptions register."})
                    return
                owner = q.get("owner") or ""
                if q.get("mine") == "1":
                    owner = (user.get("email") or "").strip() or "(nobody has this address)"
                d = exreg.query(q.get("status") or "active", owner, q.get("source") or "", q.get("company") or "",
                                q.get("location") or "", q.get("q") or "", register_allow(user))
                self._send(200, dict(d, **register_status(user)))
            elif url.path == "/api/negative-report":
                self._send(200, negative_report_status())
            elif url.path in ("/api/negative-report/job", "/api/negative-report/preview"):
                job = negrep.job(q.get("id") or "", user["username"])
                if not job:
                    self._send(404, {"error": "This report is no longer held; build it again."})
                elif url.path.endswith("/job"):
                    self._send(200, {k: v for k, v in job.items() if k != "html"})
                elif job.get("html"):
                    self._send(200, job["html"].encode("utf-8"), "text/html; charset=utf-8")
                else:
                    msg = ("Reading Business Central: stock by location, the item ledger, open purchase orders and "
                           "transfers… the first read can take several minutes." if job["status"] == "running"
                           else html.escape(job.get("detail") or ""))
                    self._send(200, (f"<!doctype html><meta http-equiv='refresh' content='5'><p style='font-family:sans-serif;"
                                     f"padding:20px'>{msg}</p>").encode("utf-8"), "text/html; charset=utf-8")
            elif url.path == "/api/emcpriv":
                # Control → EMC privileges: the Simphony users, their roles and what each role allows (emcpriv.py)
                if not auth.can(user, "finance.s.emcpriv"):
                    self._send(403, {"error": "Your role does not include EMC privileges."})
                    return
                try:
                    r = emcpriv.request(q.get("refresh") == "1")
                except emcpriv.Error as exc:
                    r = {"available": False, "error": str(exc)}
                self._send(200, dict(r, connection=emcpriv.info(), can_settings=auth.can(user, "settings")))
            elif url.path == "/api/kartela/entries":
                # Kartela: the open entries of one supplier or customer, when its row is opened
                kind = q.get("kind")
                if kind not in ("ap", "ar"):
                    self._send(400, {"error": "kind is ap or ar."})
                    return
                if not auth.can(user, f"finance.s.kartela.{kind}"):
                    self._send(403, {"error": "Your role does not include this screen."})
                    return
                company = q.get("company") or config.BC_COMPANY
                rows = client.open_vendor_entries(company) if kind == "ap" else client.open_customer_entries(company)
                self._send(200, {"kind": kind, "company": company, "party": q.get("party") or "",
                                 "entries": finance.party_entries(rows, kind, q.get("party") or "", date.today())})
            elif url.path == "/api/stockstop-report":
                self._send(200, salestop_report.status(ssrep))
            elif url.path == "/api/sevenrooms-report":
                self._send(200, sevenrooms_report.status(srrep))
            elif url.path in ("/api/stockstop-report/job", "/api/stockstop-report/preview",
                              "/api/sevenrooms-report/job", "/api/sevenrooms-report/preview"):
                sr = url.path.startswith("/api/sevenrooms-report")
                job = (srrep if sr else ssrep).job(q.get("id") or "", user["username"])
                if not job:
                    self._send(404, {"error": "This report is no longer held; build it again."})
                elif url.path.endswith("/job"):
                    self._send(200, {k: v for k, v in job.items() if k != "html"})
                elif job.get("html"):
                    self._send(200, job["html"].encode("utf-8"), "text/html; charset=utf-8")
                else:
                    msg = (("Reading SevenRooms and Simphony…" if sr else
                            "Reading the open assembly orders in Business Central and today's sales in Simphony…")
                           if job["status"] == "running" else html.escape(job.get("detail") or ""))
                    self._send(200, (f"<!doctype html><meta http-equiv='refresh' content='5'><p style='font-family:sans-serif;"
                                     f"padding:20px'>{msg}</p>").encode("utf-8"), "text/html; charset=utf-8")
            elif url.path == "/api/brand-report":
                self._send(200, brand_report_status())
            elif url.path == "/api/sales-report":
                self._send(200, sales_report_status())
            elif url.path == "/api/pl-memo":
                self._send(200, pl_memo_status())
            elif url.path in ("/api/pl-memo/job", "/api/pl-memo/preview"):
                job = plmemo.job(q.get("id") or "", user["username"])
                if not job:
                    self._send(404, {"error": "This memo is no longer held; build it again."})
                elif url.path.endswith("/job"):
                    self._send(200, {k: v for k, v in job.items() if k != "html"})
                elif job.get("html"):
                    self._send(200, job["html"].encode("utf-8"), "text/html; charset=utf-8")
                else:
                    msg = ("Reading the P&L of every company from Business Central… the first read can take several minutes."
                           if job["status"] == "running" else html.escape(job.get("detail") or ""))
                    self._send(200, (f"<!doctype html><meta http-equiv='refresh' content='5'><p style='font-family:sans-serif;"
                                     f"padding:20px'>{msg}</p>").encode("utf-8"), "text/html; charset=utf-8")
            elif url.path in ("/api/sales-report/job", "/api/sales-report/preview"):
                job = salesrep.job(q.get("id") or "", user["username"])
                if not job:
                    self._send(404, {"error": "This report is no longer held; build it again."})
                elif url.path.endswith("/job"):
                    self._send(200, {k: v for k, v in job.items() if k != "html"})
                elif job.get("html"):
                    self._send(200, job["html"].encode("utf-8"), "text/html; charset=utf-8")
                else:
                    msg = ("Reading Simphony (every restaurant, last week, the week before and last year) and the budget "
                           "from Business Central… the first read can take several minutes." if job["status"] == "running"
                           else html.escape(job.get("detail") or ""))
                    self._send(200, (f"<!doctype html><meta http-equiv='refresh' content='5'><p style='font-family:sans-serif;"
                                     f"padding:20px'>{msg}</p>").encode("utf-8"), "text/html; charset=utf-8")
            elif url.path in ("/api/brand-report/job", "/api/brand-report/preview"):
                job = brandrep.job(q.get("id") or "", user["username"])
                if not job:
                    self._send(404, {"error": "This report is no longer held; build it again."})
                elif url.path.endswith("/job"):
                    self._send(200, {k: v for k, v in job.items() if k != "html"})
                elif job.get("html"):
                    self._send(200, job["html"].encode("utf-8"), "text/html; charset=utf-8")
                else:
                    msg = ("Reading Simphony (every location, the period, the week before and last year) and the P&L "
                           "from Business Central… the first read can take several minutes." if job["status"] == "running"
                           else html.escape(job.get("detail") or ""))
                    self._send(200, (f"<!doctype html><meta http-equiv='refresh' content='5'><p style='font-family:sans-serif;"
                                     f"padding:20px'>{msg}</p>").encode("utf-8"), "text/html; charset=utf-8")
            elif url.path in ("/api/cost-report/job", "/api/cost-report/preview"):
                job = costrep.job(q.get("id") or "", user["username"])
                if not job:
                    self._send(404, {"error": "This report is no longer held; build it again."})
                elif url.path.endswith("/job"):
                    self._send(200, {k: v for k, v in job.items() if k != "html"})
                elif job.get("html"):
                    self._send(200, job["html"].encode("utf-8"), "text/html; charset=utf-8")
                else:
                    msg = ("Reading Business Central: stock, movements, recipes and item cards of last week… "
                           "the first read can take several minutes." if job["status"] == "running"
                           else html.escape(job.get("detail") or ""))
                    self._send(200, (f"<!doctype html><meta http-equiv='refresh' content='5'><p style='font-family:sans-serif;"
                                     f"padding:20px'>{msg}</p>").encode("utf-8"), "text/html; charset=utf-8")
            elif url.path == "/api/arap-notify/preview":
                company = q.get("company") or arap.companies()[0]
                self._send(200, arap.preview(company).encode("utf-8"), "text/html; charset=utf-8")
            elif url.path in ("/api/movement", "/api/movement/rows", "/api/movement/xlsx"):
                if not auth.can(user, "finance.inventory.view"):
                    self._send(403, {"error": "Your role does not include Inventory."})
                    return
                company = q.get("company") or config.BC_COMPANY
                try:
                    days = int(q.get("days") or 90)
                except ValueError:
                    days = 90
                if url.path == "/api/movement":
                    r = movement.request(client, company, q.get("as_of"), days, q.get("refresh") == "1")
                    self._send(200, r if r.get("pending") or r.get("available") is False
                               else movement.public(movement.cut(r, self._allowed)))
                    return
                r = movement.held(company, q.get("as_of") or "", days)
                if not r or r.get("available") is False:
                    self._send(409, {"error": "Read the movement report first."})
                    return
                r = movement.cut(r, self._allowed)               # a person limited to some locations: theirs only
                if url.path == "/api/movement/rows":
                    self._send(200, movement.select_rows(r, q.get("location") or "", q.get("class") or "",
                                                         q.get("category") or "", q.get("q") or "",
                                                         q.get("rnc") == "1", q.get("sort") or "value",
                                                         min(2000, int(q.get("limit") or 500))))
                else:
                    body = movement.render_xlsx(r)
                    self.send_response(200)
                    self.send_header("Content-Type",
                                     "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                    self.send_header("Content-Disposition", f'attachment; filename="RHG_{company.replace(" ", "_")}_'
                                     f'stock_movement_{r["as_of"]}.xlsx"')
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
            elif url.path in ("/api/inventory/items", "/api/inventory/items/xlsx"):
                # Inventory → Item cards → Stock by location: every warehouse, to everyone who opens the screen
                if not auth.can(user, "finance.inventory.view"):
                    self._send(403, {"error": "Your role does not include Inventory."})
                    return
                company = q.get("company") or config.BC_COMPANY
                try:
                    as_of = date.fromisoformat((q.get("date") or "")[:10]) if q.get("date") else date.today()
                except ValueError:
                    self._send(400, {"error": "The date must look like 2026-11-10."})
                    return
                as_of = min(as_of, date.today())
                if url.path == "/api/inventory/items":
                    self._send(200, itemstock.request(client, company, as_of, q.get("refresh") == "1"))
                    return
                r = itemstock._held.held(f"{company}|{as_of.isoformat()}")
                if not r or not r.get("available"):
                    self._send(409, {"error": "Read the stock first."})
                    return
                body = itemstock.render_xlsx(r, {"location": q.get("location") or "", "show": q.get("show") or "stock"})
                name = f'RHG_{company}_Stock_by_location_{(q.get("location") or "all")}_{as_of.isoformat()}.xlsx'
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                self.send_header("Content-Disposition", f'attachment; filename="{re.sub(r"[^A-Za-z0-9._-]+", "_", name)}"')
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                store.audit("export", "Item cards · stock by location", user["username"], {},
                            {"location": q.get("location") or "all", "date": as_of.isoformat()}, company)
            elif url.path in ("/api/inventory/sku", "/api/inventory/sku/location", "/api/inventory/sku/xlsx"):
                if not auth.can(user, "finance.inventory.view"):
                    self._send(403, {"error": "Your role does not include Inventory."})
                    return
                company = q.get("company") or config.BC_COMPANY
                if url.path == "/api/inventory/sku":
                    self._send(200, skuplan.request(client, company, q.get("refresh") == "1"))
                    return
                r = skuplan.held(company)
                if not r:
                    self._send(409, {"error": "Open SKU planning first."})
                elif url.path == "/api/inventory/sku/location":
                    rows = skuplan.location_rows(r, q.get("code") or "")
                    self._send(200 if rows else 404, rows or {"error": "No such location in SKU planning."})
                else:
                    sts = [x for x in (q.get("statuses") or "").split(",") if x in skuplan.STATUS_LABEL] or None
                    body = skuplan.render_xlsx(r, q.get("code") or None, sts)
                    name = f'RHG_{company.replace(" ", "_")}_SKU_{(q.get("code") or "all").replace(" ", "_")}_{r["window"]["to"]}.xlsx'
                    self.send_response(200)
                    self.send_header("Content-Type",
                                     "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                    self.send_header("Content-Disposition", f'attachment; filename="{re.sub(r"[^A-Za-z0-9._-]+", "_", name)}"')
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    store.audit("export", "SKU planning", user["username"], {}, {"location": q.get("code") or "all"}, company)
            elif url.path.startswith("/api/fiscal"):
                if not auth.can(user, "finance.ap.view"):
                    self._send(403, {"error": "Your role does not include Payables."})
                    return
                company = config.FISK_COMPANY
                mod = fiscal_sales if q.get("side") == "sales" else fiscal
                if url.path == "/api/fiscal":
                    self._send(200, mod.request(client, company, q.get("from"), q.get("to"),
                                                q.get("refresh") == "1"))
                elif url.path == "/api/fiscal/status":
                    self._send(200, fiscal.status(company))
                elif url.path == "/api/fiscal/job":
                    self._send(200, {"job": fiscal.job(q.get("id") or "")})
                elif url.path == "/api/fiscal/xlsx":
                    r = mod.request(client, company, q.get("from"), q.get("to"))
                    if r.get("pending") or r.get("available") is False:
                        self._send(409, {"error": "The report is not ready yet."})
                        return
                    body = mod.render_xlsx(r)
                    self.send_response(200)
                    self.send_header("Content-Type",
                                     "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                    self.send_header("Content-Disposition", f'attachment; filename="{mod.xlsx_name(r)}"')
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self._send(404, {"error": "Not found"})
            elif url.path in ("/api/typology", "/api/typology/xlsx"):
                if not auth.can(user, "finance.inventory.view"):
                    self._send(403, {"error": "Your role does not include Inventory."})
                    return
                company = q.get("company") or config.BC_COMPANY
                try:
                    days = int(q.get("days") or 90)
                except ValueError:
                    days = 90
                if url.path == "/api/typology":
                    r = movement.request(client, company, q.get("as_of"), days, q.get("refresh") == "1")
                else:
                    r = movement.held(company, q.get("as_of") or "", days)
                    if not r:
                        self._send(409, {"error": "Read the report first."})
                        return
                if r.get("pending") or r.get("available") is False:
                    self._send(200, r)
                    return
                m = recipes.master_cached(company) or {}
                t = typology.analyse(movement.cut(r, self._allowed), m.get("items") or {}, store.get_settings())
                if url.path == "/api/typology":
                    self._send(200, t)
                else:
                    body = typology.render_xlsx(t)
                    self.send_response(200)
                    self.send_header("Content-Type",
                                     "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                    self.send_header("Content-Disposition", f'attachment; filename="RHG_{company.replace(" ", "_")}_'
                                     f'typology_turnover_{t["as_of"]}.xlsx"')
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
            elif url.path in ("/api/discharge", "/api/discharge/xlsx"):
                if not auth.can(user, "finance.inventory.view"):
                    self._send(403, {"error": "Your role does not include Inventory."})
                    return
                company = q.get("company") or config.BC_COMPANY
                try:
                    period = int(q.get("days") or 30)
                except ValueError:
                    period = 30
                key = (company, period)
                if url.path.endswith("/xlsx"):
                    r = recipeuse.held(key)
                    if not r:
                        self._send(409, {"error": "Read the report first."})
                        return
                    if getattr(self, "_scope_filter", None):
                        r = self._scope_filter(r)                # a person limited to some locations: theirs only
                    body = recipeuse.render_xlsx(r)
                    self.send_response(200)
                    self.send_header("Content-Type",
                                     "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                    self.send_header("Content-Disposition", f'attachment; filename="RHG_{company.replace(" ", "_")}_'
                                     f'received_vs_discharged_{r["as_of"]}_{r["period"]}d.xlsx"')
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                mv = movement.request(client, company, None, 90, q.get("refresh") == "1")
                if mv.get("pending") or mv.get("available") is False:
                    self._send(200, mv)
                    return
                m = recipes.master_cached(company) or {}
                excluded = discharge_excluded(company, mv["as_of"], q.get("refresh") == "1")
                r = recipeuse.analyse(company, mv, m, store.get_settings(), period, excluded)
                recipeuse.remember(key, r)
                self._send(200, r)
            elif url.path in ("/api/recipes", "/api/recipes/xlsx"):
                if not auth.can(user, "finance.inventory.view"):
                    self._send(403, {"error": "Your role does not include Inventory."})
                    return
                company = q.get("company") or config.BC_COMPANY
                try:
                    r = recipes.request(client, company, q.get("from"), q.get("to"),
                                        q.get("refresh") == "1" and url.path == "/api/recipes")
                except ValueError as exc:
                    self._send(400, {"error": str(exc)})
                    return
                if url.path == "/api/recipes":
                    self._send(200, r)
                elif r.get("pending") or r.get("available") is False:
                    self._send(409, {"error": "The report is still being read; try again in a moment."})
                else:
                    body = recipes.render_xlsx(r)
                    self.send_response(200)
                    self.send_header("Content-Type",
                                     "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                    self.send_header("Content-Disposition", f'attachment; filename="RHG_{company.replace(" ", "_")}_'
                                     f'recipes_{r["period"]["from"]}_{r["period"]["to"]}.xlsx"')
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
            elif url.path == "/api/inventory/locations":
                if not auth.can(user, "finance.inventory.view"):
                    self._send(403, {"error": "Your role does not include Inventory."})
                    return
                self._send(200, invloc.build(client, q.get("company") or config.BC_COMPANY, q.get("period"),
                                             q.get("mode", "month"), stock_summary, q.get("refresh") == "1"))
            elif url.path == "/api/simphony/status":
                self._send(200, simphony.info())
            elif url.path == "/api/inventory":
                self._send(200, inventory_request(q.get("company") or config.BC_COMPANY, q.get("period"),
                                                  q.get("mode", "month"), q.get("dimension"), q.get("location"),
                                                  q.get("refresh") == "1"))
            elif url.path == "/api/stock-summary":
                self._send(200, stock_summary(q.get("company") or config.BC_COMPANY, q.get("period"),
                                              q.get("mode", "month")))
            elif url.path == "/api/closing":
                self._send(200, closing_screen(q.get("company") or config.BC_COMPANY, q.get("period"),
                                               q.get("mode", "month")))
            elif url.path == "/api/treasury":
                self._send(200, treasury_screen(q.get("company") or config.BC_COMPANY, q.get("period"),
                                                q.get("mode", "month"), q.get("dimension")))
            elif url.path == "/api/assets":
                self._send(200, assets_screen(q.get("company") or config.BC_COMPANY, q.get("period"),
                                              q.get("mode", "month")))
            elif url.path == "/api/budget":
                self._send(200, budget_screen(q.get("company") or config.BC_COMPANY, q.get("period"),
                                              q.get("mode", "month"), q.get("dimension")))
            elif url.path == "/api/consolidation":
                self._send(200, consolidation_screen(q.get("period"), q.get("mode", "month")))
            elif url.path == "/api/receipts":
                if not auth.can(user, "finance.s.receipts"):
                    self._send(403, {"error": "Your role does not include Takings POS ↔ BC."})
                    return
                company = q.get("company") or config.BC_COMPANY
                try:
                    r = rcpt.request(client, company, _latest, in_parallel_jobs, q.get("refresh") == "1")
                except ValueError as exc:
                    r = {"available": False, "error": str(exc)}
                self._send(200, dict(r, companies=rcpt.companies(), can_settings=auth.can(user, "settings"))
                           if isinstance(r, dict) else r)
            elif url.path in ("/api/shrinkage", "/api/shrinkage/xlsx", "/api/shrinkage/overview", "/api/shrinkage/config"):
                if not auth.can(user, "finance.s.inventory.shrinkage"):
                    self._send(403, {"error": "Your role does not include Shrinkage KPIs."})
                    return
                self._shrinkage(q, user, url.path)
            elif url.path in ("/api/stockcount", "/api/stockcount/xlsx"):
                if not auth.can(user, "finance.s.inventory.stockcount"):
                    self._send(403, {"error": "Your role does not include Stock count analysis."})
                    return
                self._stockcount(q, user, url.path.endswith("/xlsx"))
            elif url.path == "/api/dq":
                self._send(200, dq_screen(q.get("company") or config.BC_COMPANY, q.get("period"),
                                          q.get("mode", "month")))
            elif url.path == "/api/dq/late":
                try:
                    self._send(200, dq_late(q.get("company") or config.BC_COMPANY, q.get("period") or "",
                                            q.get("account") or ""))
                except BCError as exc:
                    self._send(200, {"error": str(exc)[:300]})
            elif url.path == "/api/capital":
                self._send(200, capital_screen(q.get("company") or config.BC_COMPANY, q.get("period"),
                                               q.get("mode", "month")))
            elif url.path == "/api/exceptions/email":
                company = q.get("company") or config.BC_COMPANY
                roles, err = exmail.targets(company)
                self._send(200, {"roles": roles, "error": err, "by_area": {k: {"to": v[0], "cc": v[1]} for k, v in exmail.BY_AREA.items()},
                                 "default": {"to": exmail.DEFAULT[0], "cc": exmail.DEFAULT[1]}, "log": exmail.log(company),
                                 "mail": arap_notify.mail_status(), "can_send": auth.can(user, "actions"),
                                 "me": {"name": user.get("name") or user.get("username"), "email": user.get("email") or ""}})
            elif url.path == "/api/drill":
                self._send(200, drill(q.get("kind", "pl"), q.get("key", "")))
            elif url.path == "/api/recipients/locations":
                try:
                    locs = client.locations(q.get("company") or config.BC_COMPANY)
                except Exception as exc:                         # noqa: BLE001
                    self._send(200, {"locations": [], "error": str(exc)[:200]})
                    return
                self._send(200, {"locations": sorted(set(locs))})
            elif url.path == "/api/recipients":
                d = recipients.directory(max_age=0)
                try:
                    companies = client.companies()
                except Exception:                                # noqa: BLE001
                    companies = []
                self._send(200, dict(d or {}, companies=companies, tower="Finance", restaurants=restaurants_by_company(),
                                     error="" if d else ("Purchasing could not be reached: " + recipients.last_error())))
            elif url.path == "/api/companies":
                self._send(200, {"companies": client.companies(), "default": config.BC_COMPANY,
                                 "dimensions": client.dimension_values(q.get("company") or config.BC_COMPANY)})
            elif url.path == "/api/actions":
                # the Action center is the Exceptions register now: its items tracked by hand
                d = exreg.query("all", source="manual", allow=register_allow(user))
                self._send(200, {"actions": d["items"], "moved": "The actions are in the Exceptions register.",
                                 "statuses": exreg.STATUSES})
            elif url.path == "/api/settings":
                self._send(200, dict(store.get_settings(), locations_registry_default=locintel.DEFAULT_REGISTRY))
            elif url.path == "/api/audit":
                self._send(200, {"audit": store.get_audit()})
            elif url.path == "/api/cache":
                with _dash_lock:
                    held = [{"company": k[0], "period": k[1] or "current", "mode": k[2],
                             "dimension": k[3] or "all", "age_seconds": int(time.time() - v[0])}
                            for k, v in _dash_cache.items()]
                self._send(200, {"local_file": localdb.stats(), "screens_held": held,
                                 "warm_at": config.WARM_AT, "memory_mb": memuse.process_mb(),
                                 "note": "Only rows that cannot change are kept on disk: general ledger, "
                                         "fixed-asset and bank entries. Supplier and customer entries are "
                                         "not, because their open flag and remaining amount change when an "
                                         "invoice is paid, and the account balances are recalculated by BC "
                                         "on every read."})
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
        if url.path == "/internal/unicorn/facts":
            # Unicorn (identity) asks for the figures of its morning brief; answered from a background read.
            if not self._service_call():
                self._send(403, {"error": "Forbidden."})
                return
            try:
                day = date.fromisoformat(str(body.get("date"))[:10]) if body.get("date") else None
                self._send(200, unicorn_facts.request(
                    client, _latest, in_parallel_jobs, company_currency,
                    lambda co: dashboard(co, "", "month", ""), arap.digest, day, bool(body.get("refresh"))))
            except ValueError:
                self._send(400, {"error": "Bad date."})
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
        self._allowed = None
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
        needed = WRITE_PERMISSIONS.get(url.path) or ("actions" if url.path.startswith("/api/actions") else None)
        if needed and not auth.can(user, needed):
            self._send(403, {"error": f"Your role ({user['role']}) cannot change this."})
            return
        actor = user["username"]
        try:
            if url.path == "/api/exceptions/email":
                self._send(*send_exception_email(body, user))
            elif url.path == "/api/shrinkage/count":
                # the month's count form of a location (the same file as Stock count analysis): the KPIs follow it
                if not auth.can(user, "finance.s.inventory.shrinkage"):
                    self._send(403, {"error": "Your role does not include Shrinkage KPIs."})
                    return
                company, location = body.get("company") or config.BC_COMPANY, str(body.get("location") or "").strip()
                month = str(body.get("month") or "")
                try:
                    stockcount.month_range(month)
                except (ValueError, TypeError):
                    self._send(400, {"error": "Choose the month the count closes."})
                    return
                codes = client.locations(company)
                if self._allowed is not None:
                    codes = [c for c in codes if c.upper() in self._allowed]
                if not location or location not in codes:
                    self._send(400, {"error": "Choose the location of the count first."})
                    return
                if body.get("delete"):
                    ok = stockcount.delete(company, location, month, actor)
                    shrinkage._held.forget()
                    self._send(200, {"ok": ok})
                    return
                try:
                    parsed = stockcount.parse(stockcount.decode(body.get("data")), body.get("sheet") or None)
                except ValueError as exc:
                    self._send(400, {"error": str(exc)})
                    return
                c = stockcount.save(company, location, month, parsed, body.get("filename"), actor)
                shrinkage._held.forget()
                self._send(200, {"ok": True, "sheet": c["sheet"], "sheets": c["sheets"], "month_on_form": c["month"],
                                 "lines": len(c["rows"]), "counted_lines": c["counted_lines"],
                                 "warnings": c["warnings"][:10]})
            elif url.path in ("/api/shrinkage/config", "/api/shrinkage/import"):
                company = body.get("company") or config.BC_COMPANY
                try:
                    if url.path.endswith("/import"):
                        data = stockcount.decode(body.get("data"))
                        res = shrinkage.import_excel(company, data, actor, shrinkage.item_cards(client, company))
                        self._send(200, {"ok": True, "import": res})
                    else:
                        patch = {k: body[k] for k in ("categories", "map", "prefixes", "overrides", "status") if k in body}
                        shrinkage.save_config(company, patch, actor, str(body.get("note") or ""))
                        self._send(200, {"ok": True})
                except ValueError as exc:
                    self._send(400, {"error": str(exc)})
            elif url.path == "/api/actions":
                # Track: an item of the Exceptions register, tracked by hand (or the one already there)
                try:
                    self._send(200, exreg.track(body or {}, actor, user.get("email") or ""))
                except ValueError as exc:
                    self._send(400, {"error": str(exc)})
            elif url.path.startswith("/api/actions/"):
                self._send(410, {"error": "The Action center is now the Exceptions register: change the item there."})
            elif url.path == "/api/settings":
                self._send(200, store.save_settings(body, actor))
            elif url.path == "/api/sales/channel-profit/params":
                self._send(200, {"config": chanprof.save_config(body or {}, actor),
                                 "fields": [{"key": k, "label": lb, "unit": u} for k, lb, u in chanprof.CHANNEL_FIELDS]})
            elif url.path in ("/api/billy/settings", "/api/billy/test"):
                if self.headers.get("X-Requested-With") != "RHG-Finance":
                    self._send(403, {"error": "This request must come from the Finance tower."})
                    return
                if url.path.endswith("/settings"):
                    try:
                        conn = billy.save(body["connection"], actor) if isinstance(body.get("connection"), dict) else billy.info()
                        if isinstance(body.get("settings"), dict):
                            rec = {k: str(v)[:4000] for k, v in body["settings"].items() if k in billyrec.DEFAULTS}
                            store.save_settings({"billy_rec": rec}, actor)
                            with billyrec._lock:             # the next read applies the new settings
                                billyrec._results.clear()
                        self._send(200, {"connection": conn, "settings": _billy_pub()})
                    except (ValueError, KeyError) as exc:
                        self._send(400, {"error": str(exc)})
                    return
                acct = billy.account(body.get("company") or "")
                if not acct:
                    self._send(200, {"ok": False, "error": "This company has no Billy account with a password."})
                    return
                try:
                    with billy._tok_lock:
                        billy._tokens.pop(acct["nuis"], None)
                    billy._token(acct)
                    store.audit("billy test", "connection", actor, {}, {"nuis": acct["nuis"]}, "")
                    self._send(200, {"ok": True, "nuis": acct["nuis"]})
                except billy.BillyError as exc:
                    self._send(200, {"ok": False, "error": str(exc)})
            elif url.path in ("/api/simphony/settings", "/api/simphony/test"):
                if self.headers.get("X-Requested-With") != "RHG-Finance":
                    self._send(403, {"error": "This request must come from the Finance tower."})
                    return
                if url.path.endswith("/settings"):
                    try:
                        self._send(200, simphony.save(body, actor))
                    except ValueError as exc:
                        self._send(400, {"error": str(exc)})
                    return
                try:
                    simphony._token(force=True)
                    locs = simphony.locations(max_age=0)
                    first = locs[0]["locRef"] if locs else None
                    latest = simphony.latest_business_date(first) if first else None
                    store.audit("simphony test", "connection", actor, {}, {"locations": len(locs)}, "")
                    self._send(200, {"ok": True, "locations": locs, "latest_bus_dt": latest,
                                     "status": simphony.info()})
                except simphony.SimphonyError as exc:
                    self._send(200, {"ok": False, "error": str(exc), "status": simphony.info()})
            elif url.path in ("/api/cost-report/settings", "/api/cost-report/job"):
                if self.headers.get("X-Requested-With") != "RHG-Finance":
                    self._send(403, {"error": "This request must come from the Finance tower."})
                    return
                if url.path.endswith("/settings"):
                    _, bad = arap_notify.parse_emails(body.get("to"))
                    _, bad_cc = arap_notify.parse_emails(body.get("cc"))
                    if bad or bad_cc:
                        self._send(400, {"error": "Not an email address: " + ", ".join(bad + bad_cc)})
                        return
                    store.save_settings({"cost_report": body}, actor)
                    self._send(200, cost_report_status())
                    return
                only, bad = arap_notify.parse_emails(body.get("to"))
                if bad:
                    self._send(400, {"error": "Not an email address: " + ", ".join(bad)})
                    return
                company = body.get("company") or costrep.companies()[0]
                self._send(202, costrep.start_job(company, actor, only or None))
            elif url.path == "/api/kpi/mapping":
                if self.headers.get("X-Requested-With") != "RHG-Finance":
                    self._send(403, {"error": "This request must come from the Finance tower."})
                    return
                action = body.get("action") or "save"
                if action not in ("save", "approve", "reset"):
                    self._send(400, {"error": "Unknown action."})
                    return
                company = body.get("company") or config.BC_COMPANY
                rules = body.get("rules") if isinstance(body.get("rules"), dict) else None
                kpilib.save(company, rules, user.get("name") or actor, action, str(body.get("note") or ""),
                            body.get("groups") if isinstance(body.get("groups"), dict) else None)
                with _dash_lock:
                    _dash_cache.clear()
                self._send(200, kpi_mapping_screen(company))
            elif url.path in ("/api/stockcount/upload", "/api/stockcount/delete"):
                if not auth.can(user, "finance.s.inventory.stockcount"):
                    self._send(403, {"error": "Your role does not include Stock count analysis."})
                    return
                company, location = body.get("company") or config.BC_COMPANY, str(body.get("location") or "").strip()
                month = str(body.get("month") or "")
                try:
                    stockcount.month_range(month)
                except (ValueError, TypeError):
                    self._send(400, {"error": "Choose the month the count closes."})
                    return
                if not location or location not in client.locations(company):
                    self._send(400, {"error": "Choose the location of the count."})
                    return
                if url.path.endswith("/delete"):
                    self._send(200, {"ok": stockcount.delete(company, location, month, actor)})
                    return
                try:
                    parsed = stockcount.parse(stockcount.decode(body.get("data")), body.get("sheet") or None)
                except ValueError as exc:
                    self._send(400, {"error": str(exc)})
                    return
                c = stockcount.save(company, location, month, parsed, body.get("filename"), actor)
                self._send(200, {"ok": True, "sheet": c["sheet"], "sheets": c["sheets"], "meta": c["meta"],
                                 "month_on_form": c["month"], "lines": len(c["rows"]),
                                 "counted_lines": c["counted_lines"], "warnings": c["warnings"]})
            elif url.path == "/api/receipts/config":
                if self.headers.get("X-Requested-With") != "RHG-Finance":
                    self._send(403, {"error": "This request must come from the Finance tower."})
                    return
                company = body.get("company") or config.BC_COMPANY
                c = rcpt.save_config(company, body.get("params") or {}, body.get("tenders") or {},
                                     user.get("name") or actor)
                self._send(200, {"ok": True, "config": c})
            elif url.path == "/api/allocation":
                if self.headers.get("X-Requested-With") != "RHG-Finance":
                    self._send(403, {"error": "This request must come from the Finance tower."})
                    return
                company = body.get("company") or config.BC_COMPANY
                try:
                    allocation.save(company, body.get("method") or "revenue", body.get("shares") or {},
                                    user.get("name") or actor, body.get("action") or "save", str(body.get("note") or ""))
                except ValueError as exc:
                    self._send(400, {"error": str(exc)})
                    return
                with _dash_lock:
                    _dash_cache.clear()
                self._send(200, allocation_screen(company))
            elif url.path == "/api/dq/freeze":
                if self.headers.get("X-Requested-With") != "RHG-Finance":
                    self._send(403, {"error": "This request must come from the Finance tower."})
                    return
                try:
                    r = dq_freeze(body.get("company") or config.BC_COMPANY, body.get("period"),
                                  body.get("action") or "freeze", user.get("name") or actor)
                except BCError as exc:
                    r = {"error": str(exc)[:300]}
                self._send(200 if not r.get("error") else 400, r)
            elif url.path == "/api/register/update":
                if self.headers.get("X-Requested-With") != "RHG-Finance":
                    self._send(403, {"error": "This request must come from the Finance tower."})
                    return
                if not auth.can(user, "finance.s.register"):
                    self._send(403, {"error": "Your role does not include the Exceptions register."})
                    return
                allow = register_allow(user)
                item = exreg.get(str(body.get("id") or ""))
                found = [item] if item and (allow is None or allow(item["company"], item["location"])) else []
                if not found:
                    self._send(404, {"error": "This exception is not in the register, or not in your scope."})
                    return
                me = (user.get("email") or "").strip().lower()
                mine = bool(me) and me in [o.lower() for o in (found[0]["owner"] or "").split(", ")]
                if not mine and not (auth.can(user, "actions") or auth.can(user, "settings")):
                    self._send(403, {"error": "Only its owner, or a role with actions, can change this item."})
                    return
                patch = {k: body[k] for k in ("status", "comment", "owner", "due") if k in body}
                try:
                    self._send(200, {"item": exreg.update(body.get("id"), patch, actor)})
                except (KeyError, ValueError) as exc:
                    self._send(400, {"error": str(exc).strip("'\"")})
            elif url.path == "/api/salesrec/settings":
                if self.headers.get("X-Requested-With") != "RHG-Finance":
                    self._send(403, {"error": "This request must come from the Finance tower."})
                    return
                store.save_settings({"sales_rec": body}, actor)
                self._send(200, {"settings": salesrec.settings_of(store.get_settings())})
            elif url.path in ("/api/register/settings", "/api/register/run", "/api/register/test"):
                if self.headers.get("X-Requested-With") != "RHG-Finance":
                    self._send(403, {"error": "This request must come from the Finance tower."})
                    return
                if url.path.endswith("/settings"):
                    store.save_settings({"exception_register": body}, actor)
                    self._send(200, register_status(user))
                elif url.path.endswith("/run"):
                    self._send(202, {"job": exreg.start_job(actor)})
                else:
                    to, bad = arap_notify.parse_emails(body.get("to") or user.get("email") or "")
                    if bad or not to:
                        self._send(400, {"error": "Give the address to send the test to."
                                         if not bad else "Not an email address: " + ", ".join(bad)})
                        return
                    try:
                        self._send(200, {"sent": exreg.send_digests(to, body.get("owner") or None, actor)})
                    except ValueError as exc:
                        self._send(400, {"error": str(exc)})
            elif url.path in ("/api/negative-report/settings", "/api/negative-report/job"):
                if self.headers.get("X-Requested-With") != "RHG-Finance":
                    self._send(403, {"error": "This request must come from the Finance tower."})
                    return
                if url.path.endswith("/settings"):
                    bad = arap_notify.parse_emails(body.get("to"))[1] + arap_notify.parse_emails(body.get("cc"))[1]
                    if bad:
                        self._send(400, {"error": "Not an email address: " + ", ".join(bad)})
                        return
                    store.save_settings({"negative_report": body}, actor)
                    self._send(200, negative_report_status())
                    return
                only, bad = arap_notify.parse_emails(body.get("to"))
                if bad:
                    self._send(400, {"error": "Not an email address: " + ", ".join(bad)})
                    return
                company = body.get("company") or negrep.companies()[0]
                self._send(202, negrep.start_job(company, actor, only or None))
            elif url.path in ("/api/sevenrooms/settings", "/api/sevenrooms/test"):
                if self.headers.get("X-Requested-With") != "RHG-Finance":
                    self._send(403, {"error": "This request must come from the Finance tower."})
                    return
                try:
                    if url.path.endswith("/settings"):
                        conn = sevenrooms.save(body, actor)
                        srsim._held.forget()                 # the next read applies the account and the mapping
                        self._send(200, {"connection": conn})
                    else:
                        self._send(200, sevenrooms.test())
                except (ValueError, sevenrooms.Error) as exc:
                    self._send(400, {"error": str(exc)[:300]})
            elif url.path in ("/api/emcpriv/settings", "/api/emcpriv/check"):
                if self.headers.get("X-Requested-With") != "RHG-Finance":
                    self._send(403, {"error": "This request must come from the Finance tower."})
                    return
                try:
                    if url.path.endswith("/settings"):
                        self._send(200, {"connection": emcpriv.save(body, actor)})
                    else:
                        self._send(200, emcpriv.check())
                except (ValueError, emcpriv.Error) as exc:
                    self._send(400, {"error": str(exc)[:300]})
            elif url.path == "/api/export/email":
                if self.headers.get("X-Requested-With") != "RHG-Finance":
                    self._send(403, {"error": "This request must come from the Finance tower."})
                    return
                if not auth.can(user, "export"):
                    self._send(403, {"error": "Your role cannot export (needs finance.export)."})
                    return
                self._export_email(body, user, "Finance", lambda to, cc, subj, h, t, fname, ctype, data, reply:
                                   arap_notify.send_mail(to, cc, subj, h, t, [(fname, ctype, data)],
                                                         source="Finance · export by email", reply_to=reply))
            elif url.path == "/api/xlsx/table":
                # one table of a screen, as the person sees it (xlsxtable.py): nothing is read from BC here
                if self.headers.get("X-Requested-With") != "RHG-Finance":
                    self._send(403, {"error": "This request must come from the Finance tower."})
                    return
                if not auth.can(user, "export"):
                    self._send(403, {"error": "Your role cannot export (needs finance.export)."})
                    return
                fname = xlsxtable.filename(body.get("name"))
                ctype = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                try:
                    data = xlsxtable.render(body.get("sheets"), body.get("notes") or [])
                    if body.get("format") == "pdf":             # Export → PDF: the same workbook laid out (xlsxpdf.py)
                        data = xlsxpdf.render(data, str(body.get("name") or "")[:150])
                        fname, ctype = xlsxpdf.pdf_name(fname), "application/pdf"
                except ValueError as exc:
                    self._send(400, {"error": str(exc)})
                    return
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
                store.audit("export", "table · " + str(body.get("name") or "")[:120], actor, {},
                            {"rows": sum(len(x.get("rows") or []) for x in body.get("sheets") or [] if isinstance(x, dict))}, "")
            elif url.path in ("/api/stockstop-report/settings", "/api/stockstop-report/job",
                              "/api/sevenrooms-report/settings", "/api/sevenrooms-report/job"):
                if self.headers.get("X-Requested-With") != "RHG-Finance":
                    self._send(403, {"error": "This request must come from the Finance tower."})
                    return
                sr = url.path.startswith("/api/sevenrooms-report")
                rep, key, mod = (srrep, "sevenrooms_report", sevenrooms_report) if sr else (ssrep, "stockstop_report", salestop_report)
                if url.path.endswith("/settings"):
                    bad = arap_notify.parse_emails(body.get("to"))[1] + arap_notify.parse_emails(body.get("cc"))[1]
                    if bad:
                        self._send(400, {"error": "Not an email address: " + ", ".join(bad)})
                        return
                    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(body.get("time") or "")):
                        self._send(400, {"error": "The time must be HH:MM, for example 09:30."})
                        return
                    store.save_settings({key: {k: body.get(k) for k in
                                               ("to", "cc", "days", "time", "companies", "email_lines", "enabled")}}, actor)
                    self._send(200, mod.status(rep))
                    return
                only, bad = arap_notify.parse_emails(body.get("to"))
                if bad:
                    self._send(400, {"error": "Not an email address: " + ", ".join(bad)})
                    return
                company = body.get("company") or (rep.companies() or [""])[0]
                if not company:
                    self._send(400, {"error": "No company has a SevenRooms venue yet."})
                    return
                self._send(202, rep.start_job(company, actor, only or None))
            elif url.path == "/api/recipients":
                if self.headers.get("X-Requested-With") != "RHG-Finance":
                    self._send(403, {"error": "This request must come from the Finance tower."})
                    return
                try:
                    d = recipients.save(body, actor)
                except (ValueError, requests.RequestException) as exc:
                    self._send(400, {"error": str(exc)[:300]})
                    return
                self._send(200, dict(d, companies=client.companies(), tower="Finance", restaurants=restaurants_by_company()))
            elif url.path in ("/api/pl-memo/settings", "/api/pl-memo/job"):
                if self.headers.get("X-Requested-With") != "RHG-Finance":
                    self._send(403, {"error": "This request must come from the Finance tower."})
                    return
                if url.path.endswith("/settings"):
                    bad = arap_notify.parse_emails(body.get("to"))[1] + arap_notify.parse_emails(body.get("cc"))[1]
                    if bad:
                        self._send(400, {"error": "Not an email address: " + ", ".join(bad)})
                        return
                    store.save_settings({"pl_memo": body}, actor)
                    self._send(200, pl_memo_status())
                    return
                only, bad = arap_notify.parse_emails(body.get("to"))
                if bad:
                    self._send(400, {"error": "Not an email address: " + ", ".join(bad)})
                    return
                target = str(body.get("target") or "all").strip()
                ok_targets = {"all"} | set(pl_memo.companies()) | {f"{c}|{d}" for c, d, _, _, _ in plmemo.location_plan()}
                try:
                    ok_targets |= set(client.companies())
                except Exception:                                   # noqa: BLE001
                    pass
                if target not in ok_targets:
                    self._send(400, {"error": "Choose all companies, one company or a restaurant with its own people."})
                    return
                self._send(202, plmemo.start_job(target, actor, only or None))
            elif url.path in ("/api/sales-report/settings", "/api/sales-report/job"):
                if self.headers.get("X-Requested-With") != "RHG-Finance":
                    self._send(403, {"error": "This request must come from the Finance tower."})
                    return
                if url.path.endswith("/settings"):
                    bad = arap_notify.parse_emails(body.get("to"))[1] + arap_notify.parse_emails(body.get("cc"))[1]
                    if bad:
                        self._send(400, {"error": "Not an email address: " + ", ".join(bad)})
                        return
                    store.save_settings({"sales_report": body}, actor)
                    self._send(200, sales_report_status())
                    return
                only, bad = arap_notify.parse_emails(body.get("to"))
                if bad:
                    self._send(400, {"error": "Not an email address: " + ", ".join(bad)})
                    return
                target = str(body.get("target") or "all").strip()
                if target != "all" and target not in {r["ref"] for r in sales_report.registry()}:
                    self._send(400, {"error": "Choose all restaurants or one restaurant."})
                    return
                self._send(202, salesrep.start_job(target, actor, only or None))
            elif url.path in ("/api/brand-report/settings", "/api/brand-report/job"):
                if self.headers.get("X-Requested-With") != "RHG-Finance":
                    self._send(403, {"error": "This request must come from the Finance tower."})
                    return
                if url.path.endswith("/settings"):
                    bad = arap_notify.parse_emails(body.get("cc"))[1]
                    for line in str(body.get("recipients") or "").splitlines():
                        bad += arap_notify.parse_emails(line.partition(":")[2])[1]
                    if bad:
                        self._send(400, {"error": "Not an email address: " + ", ".join(bad)})
                        return
                    store.save_settings({"brand_report": body}, actor)
                    self._send(200, brand_report_status())
                    return
                only, bad = arap_notify.parse_emails(body.get("to"))
                if bad:
                    self._send(400, {"error": "Not an email address: " + ", ".join(bad)})
                    return
                kind, brand = body.get("kind") or "weekly", str(body.get("brand") or "").strip().upper()
                if kind not in brand_report.KINDS or brand not in brand_report.brands():
                    self._send(400, {"error": "Choose a brand and a report."})
                    return
                self._send(202, brandrep.start_job(kind, brand, actor, only or None))
            elif url.path in ("/api/arap-notify/settings", "/api/arap-notify/test"):
                if self.headers.get("X-Requested-With") != "RHG-Finance":
                    self._send(403, {"error": "This request must come from the Finance tower."})
                    return
                if url.path.endswith("/settings"):
                    to, bad = arap_notify.parse_emails(body.get("to"))
                    cc, bad_cc = arap_notify.parse_emails(body.get("cc"))
                    if bad or bad_cc:
                        self._send(400, {"error": "Not an email address: " + ", ".join(bad + bad_cc)})
                        return
                    store.save_settings({"arap_notify": body}, actor)
                    self._send(200, arap_status(user))
                    return
                only, bad = arap_notify.parse_emails(body.get("to"))
                if bad:
                    self._send(400, {"error": "Not an email address: " + ", ".join(bad)})
                    return
                company = body.get("company") or arap.companies()[0]
                self._send(200, {"test": arap.start_test(company, only or None, actor)})
            elif url.path.startswith("/api/fiscal/"):
                if self.headers.get("X-Requested-With") != "RHG-Finance":
                    self._send(403, {"error": "This request must come from the Finance tower."})
                    return
                company = config.FISK_COMPANY
                if url.path == "/api/fiscal/fetch":
                    kind = "fetch sales" if body.get("side") == "sales" else "fetch"
                    self._send(200, {"job": fiscal.start(kind, client, company, actor)})
                elif url.path == "/api/fiscal/import":
                    try:
                        data = base64.b64decode(body.get("data") or "", validate=True)
                    except ValueError:
                        self._send(400, {"error": "The file could not be read."})
                        return
                    if not data or len(data) > 25 * 1024 * 1024:
                        self._send(400, {"error": "Choose the Self-Care purchases book (.xlsx, up to 25 MB)."})
                        return
                    try:
                        res = fiscal.import_book(company, data, str(body.get("name") or "")[:200])
                    except ValueError as exc:
                        self._send(400, {"error": str(exc)})
                        return
                    store.audit("fiscal import", company, actor, {}, res, "")
                    self._send(200, res)
                elif url.path == "/api/fiscal/settings":
                    to, bad = arap_notify.parse_emails(body.get("to"))
                    cc, bad_cc = arap_notify.parse_emails(body.get("cc"))
                    if bad or bad_cc:
                        self._send(400, {"error": "Not an email address: " + ", ".join(bad + bad_cc)})
                        return
                    store.save_settings({"fiscal": body}, actor)
                    self._send(200, fiscal.status(company))
                elif url.path == "/api/fiscal/probe":
                    # one reading request to the e-invoice service, to see what it answers
                    try:
                        self._send(200, cis_client.probe(body.get("eic"), body.get("party") or "BUYER",
                                                         body.get("from"), body.get("to")))
                    except ValueError as exc:
                        self._send(400, {"error": str(exc)})
                    except Exception as exc:                     # noqa: BLE001
                        self._send(200, {"error": str(exc)[:500]})
                elif url.path == "/api/fiscal/test":
                    only, bad = arap_notify.parse_emails(body.get("to"))
                    if bad:
                        self._send(400, {"error": "Not an email address: " + ", ".join(bad)})
                        return
                    self._send(200, {"job": fiscal.start("test", client, company, actor, to=only or None)})
                else:
                    self._send(404, {"error": "Not found"})
            elif url.path == "/api/closing":
                updated = store.update_closing(body.get("company") or config.BC_COMPANY, body.get("period"),
                                               body.get("id"), body, actor)
                self._send(200 if updated else 404, updated or {"error": "Task not found"})
            else:
                self._send(404, {"error": "Not found"})
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._send(500, {"error": f"Unexpected error: {exc}"})


def _warm(company):
    """Reads what the first person in the morning will open."""
    today = date.today()
    first = today.replace(day=1)
    prev_end = first - timedelta(days=1)
    started = time.time()
    done = 0
    for label, call in (
            ("this month", lambda: client.accounts(company, first.isoformat(), today.isoformat(), None, 900)),
            ("last month", lambda: client.accounts(company, prev_end.replace(day=1).isoformat(),
                                                   prev_end.isoformat(), None, 900)),
            ("suppliers", lambda: client.open_vendor_entries(company)),
            ("customers", lambda: client.open_customer_entries(company)),
    ):
        try:
            call()
            done += 1
        except Exception as exc:                                 # noqa: BLE001
            print(f"  warm-up: {label} failed, {str(exc)[:120]}")
    print(f"[{datetime.now():%H:%M:%S}] warm-up for {company}: {done} of 4 sources in "
          f"{time.time() - started:.0f}s")


def _warm_loop():
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
        print("WARNING: BC_USERNAME is empty in .env - Business Central will refuse the connection.")
    server = ThreadingHTTPServer((config.APP_HOST, config.APP_PORT), Handler)
    host = "127.0.0.1" if config.APP_HOST in ("0.0.0.0", "") else config.APP_HOST
    print(f"Finance & Inventory Control Tower running at http://{host}:{config.APP_PORT}")
    print(f"Business Central: {config.BC_BASE_URL}  company: {config.BC_COMPANY}  (read-only)")
    threading.Thread(target=_warm_loop, daemon=True).start()
    arap.start()
    costrep.start()
    negrep.start()
    ssrep.start()
    srrep.start()
    exreg.configure(mailer=lambda to, cc, subject, body, text: arap_notify.send_mail(
        to, cc, subject, body, text, source="Finance · exceptions register"))
    try:
        moved = exreg.migrate_actions(store.list_actions())
        if moved:
            print(f"Exceptions register: {moved} actions of the Action center moved in.")
    except Exception:                                               # noqa: BLE001
        traceback.print_exc()
    exreg.start(register_controls)
    billyrec.start(_latest)                 # Simphony ↔ Billy: the daily read before the 08:00 reports
    brandrep.start()
    salesrep.start()
    plmemo.start()
    fiscal.start_scheduler(client, config.FISK_COMPANY)
    if "--no-browser" not in sys.argv:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://{host}:{config.APP_PORT}")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")


if __name__ == "__main__":
    main()
