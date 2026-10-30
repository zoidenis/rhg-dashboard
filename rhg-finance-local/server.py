"""Finance & Inventory Control Tower - local server (Phase 1).

Business Central is read only. Actions, thresholds and the audit trail are the
application's own and live under ./data.
"""
import base64
import json
import sys
import threading
import time
import traceback
import webbrowser
from calendar import monthrange
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import arap_notify
import assets
import auth
import brief
import config
import localdb
import memuse
import movement
import typology
import executive
import fiscal
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
                "login.html": "text/html; charset=utf-8"}
PUBLIC_PATHS = {"/login", "/login.html", "/api/login", "/api/session", "/brand.css", "/logo.svg", "/logo.png", "/icon.svg", "/favicon.ico", "/manifest.webmanifest", "/sw.js"}
WRITE_PERMISSIONS = {"/api/settings": "settings", "/api/actions": "actions", "/api/closing": "closing",
                     "/api/simphony/settings": "settings", "/api/simphony/test": "settings",
                     "/api/arap-notify/settings": "settings", "/api/arap-notify/test": "settings",
                     "/api/fiscal/fetch": "settings", "/api/fiscal/import": "settings",
                     "/api/fiscal/settings": "settings", "/api/fiscal/test": "settings",
                     "/api/users": "users", "/api/password": "view"}
client = BCClient()
_last, _last_lock = {}, threading.Lock()

DATA_GAPS = [
    {"kpi": "Exchange rates for the group view",
     "missing": "The published currency service is empty in every company, so rates cannot be read from BC.",
     "requirement": "Publish 'Currency Exchange Rates' (page 21) so the group view uses BC's own rates, with "
                    "the average rate for the result and the closing rate for the balance sheet. Until then "
                    "the rate is entered in Settings and shown on the screen."},
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
def periods(anchor, mode, today=None):
    today = today or date.today()
    a = date.fromisoformat(anchor) if anchor else today
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


def _dashboard_build(company, anchor, mode, dimension, compare="prior"):
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
    pl = finance.profit_and_loss(cur, prev, ly, budget, rules)
    bs = finance.balance_sheet(cur, prev, ly, rules, result_rows=cur)
    today = date.today()
    ap = finance.payables(_safe(sources, "Open supplier entries",
                                lambda: client.open_vendor_entries(company), []), today, settings)
    ar = finance.receivables(_safe(sources, "Open customer entries",
                                   lambda: client.open_customer_entries(company), []), today, settings)
    bank = _safe(sources, "Bank ledger entries",
                 lambda: client.bank_entries(company, c_from.isoformat(), c_to.isoformat()), [], False)
    cash = finance.cash_position(cur, bank, settings)
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
            "actions": store.list_actions(), "action_keys": sorted(store.open_keys()), "meta": meta}
    data["kpis"] = _headline(pl, bs, ap, ar, cash, exceptions)
    data["brief"] = brief.build(data, settings, meta)
    meta["query_ms"] = int((time.time() - t0) * 1000)
    with _last_lock:
        _last["data"] = data
        _last["company"] = company
    return data


def gl_screen(company, anchor, mode, window_days=None, threshold=None):
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
    for l in locs:
        l["currency_given"] = bool(l["currency"])
        l["currency"] = l["currency"] or fallback
    mapping = simphony.loc_map()
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
            if d in cur_days:
                read += 1
                day = by_day.setdefault(d, {"date": d, "net": {}, "checks": 0, "guests": 0})
                day["net"][l["currency"] or "?"] = day["net"].get(l["currency"] or "?", 0) + fig["net"]
                day["checks"] += fig["checks"]
                day["guests"] += fig["guests"]
        if not read and not cur["net"] and not prev["net"]:
            continue                             # a location that sold nothing in either period
        rows.append({"locRef": l["locRef"], "name": l["name"], "currency": l["currency"],
                     "bc": mapping.get(l["locRef"], ""), "latest": latest.get(l["locRef"]),
                     "days_read": read, "days_missing": missing, "cur": cur, "prev": prev,
                     "change_pct": ((cur["net"] - prev["net"]) / abs(prev["net"]) * 100) if prev["net"] else None,
                     "avg_check": cur["net"] / cur["checks"] if cur["checks"] else None,
                     "avg_guest": cur["net"] / cur["guests"] if cur["guests"] else None,
                     "discount_pct": cur["discounts"] / cur["gross"] * 100 if cur["gross"] else None})
    rows.sort(key=lambda r: -r["cur"]["net"])
    totals = {}
    for r in rows:
        t = totals.setdefault(r["currency"] or "?", {"cur": dict.fromkeys(keys, 0), "prev": dict.fromkeys(keys, 0),
                                                       "locations": 0})
        t["locations"] += 1
        for k in keys:
            t["cur"][k] += r["cur"][k]
            t["prev"][k] += r["prev"][k]
    for t in totals.values():
        c, p = t["cur"], t["prev"]
        t["change_pct"] = (c["net"] - p["net"]) / abs(p["net"]) * 100 if p["net"] else None
        t["avg_check"] = c["net"] / c["checks"] if c["checks"] else None
        t["avg_guest"] = c["net"] / c["guests"] if c["guests"] else None
    main_currency = max(totals, key=lambda k: totals[k]["cur"]["net"]) if totals else ""
    return {
        "available": True, "configured": True,
        "period": {"from": c_from.isoformat(), "to": c_to.isoformat(), "label": label, "running": running},
        "prior": {"from": p_from.isoformat(), "to": p_to.isoformat(), "method": method,
                  "label": "same dates last year" if compare == "last_year" else "the period before"},
        "locations": rows, "totals": totals, "currency": main_currency,
        "days": sorted(by_day.values(), key=lambda d: d["date"]),
        "errors": errors, "calls": len(jobs), "ms": int((time.time() - t0) * 1000),
        "definitions": {
            "net": "Net sales as Simphony totals them (netSlsTtl): after item and subtotal discounts, "
                   "excluding tax and service charges.",
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


def treasury_screen(company, anchor, mode, dimension):
    """Cash flow, bank activity and what falls due next. Read on demand."""
    settings = store.get_settings()
    c_from, c_to, *_ = periods(anchor, mode)
    sources = []
    accounts = _safe(sources, "Chart of accounts, current period",
                     lambda: client.accounts(company, c_from.isoformat(), c_to.isoformat(), dimension, 900), [])
    bank = _safe(sources, "Bank ledger entries",
                 lambda: client.bank_entries(company, c_from.isoformat(), c_to.isoformat()), [], False)
    with _last_lock:
        d = _last.get("data") or {}
    cash_total = (d.get("cash") or {}).get("total", 0.0)
    ap_detail = (d.get("payables") or {}).get("detail", [])
    ar_detail = (d.get("receivables") or {}).get("detail", [])
    flow = treasury.cash_flow(accounts, settings)
    activity = treasury.bank_activity(bank)
    obl = treasury.obligations(ap_detail, ar_detail, cash_total)
    out = {"flow": flow, "activity": activity, "obligations": obl, "sources": sources, "loaded": True,
           "period": [c_from.isoformat(), c_to.isoformat()],
           "exceptions": treasury.exceptions(flow, obl, settings)}
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
    period_pl = finance.profit_and_loss(period_acc, None, None, b_period, acc_rules)
    ytd_pl = finance.profit_and_loss(ytd_acc, None, None, b_year, acc_rules)
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


def consolidation_screen(anchor, mode):
    """Every company side by side, converted into the reporting currency with the rates
    from Settings. Business Central publishes no exchange-rate service here, so the rate
    is stated on screen rather than assumed."""
    settings = store.get_settings()
    group = settings["group"]
    reporting = (group.get("reporting_currency") or "ALL").upper()
    currencies = {k.lower(): v.upper() for k, v in _parse_map(group.get("currencies")).items()}
    rates = {}
    for k, v in _parse_map(group.get("rates")).items():
        try:
            rates[k.strip().upper()] = float(v)
        except ValueError:
            continue
    rates[reporting] = 1.0
    account_rules = {**finance.DEFAULT_ACCOUNT_RULES,
                     **{k: v for k, v in settings["accounts"].items() if v}}
    c_from, c_to, p_from, p_to, *_ = periods(anchor, mode)
    sources = []
    try:
        companies = client.companies()
    except BCError as exc:
        return {"error": str(exc), "companies": [], "loaded": True}
    rows, missing_rates = [], set()
    for co in companies:
        acc = _safe(sources, f"Chart of accounts, {co}",
                    lambda c=co: client.accounts(c, c_from.isoformat(), c_to.isoformat(), None, 900), [], False)
        currency = currencies.get(co.lower(), reporting)
        if not acc:
            rows.append({"company": co, "available": False, "currency": currency})
            continue
        pl = finance.profit_and_loss(acc, rules=account_rules)
        bs = finance.balance_sheet(acc, rules=account_rules, result_rows=acc)
        rate = rates.get(currency)
        if rate is None:
            missing_rates.add(currency)
        figures = {"revenue": pl["kpis"]["revenue"], "gross": pl["kpis"]["gross"],
                   "opex": pl["kpis"]["opex"], "ebitda": pl["kpis"]["ebitda"],
                   "assets": bs["totals"]["assets"], "liabilities": bs["totals"]["liabilities"],
                   "equity": bs["totals"]["equity"]}
        rows.append({"company": co, "available": True, "currency": currency, "rate": rate,
                     "gross_margin": pl["kpis"]["gross_margin"], **figures,
                     # a withheld figure stays withheld after conversion; it is never zero
                     "converted": {k: (v * rate if (rate is not None and v is not None) else None)
                                   for k, v in figures.items()},
                     "in_total": rate is not None})
    ok = [r for r in rows if r.get("available") and r.get("in_total")]
    excluded = [r["company"] for r in rows if r.get("available") and not r.get("in_total")]
    rate_line = ", ".join(f"1 {c} = {rates[c]:,.2f} {reporting}" for c in sorted(rates) if c != reporting) or "none set"
    return {"loaded": True, "sources": sources, "rows": rows,
            "period": [c_from.isoformat(), c_to.isoformat()],
            "reporting_currency": reporting, "rates": rate_line, "excluded": excluded,
            "rate_note": group.get("rate_note") or "",
            "totals": {k: (sum(r["converted"][k] for r in ok)
                           if all(r["converted"][k] is not None for r in ok) else None)
                       for k in ("revenue", "gross", "opex", "ebitda", "assets", "liabilities", "equity")}
                      | {"companies": len(ok)},
            "note": f"Figures are converted into {reporting} at the rates set in Settings ({rate_line}), because "
                    f"Business Central publishes no exchange-rate service here. One rate is applied to both the "
                    f"result and the balance sheet, which is a management view, not a statutory translation: "
                    f"that would need the average rate for the result and the closing rate for the balance sheet. "
                    f"Nothing is eliminated between companies, so intercompany sales, balances and margins are "
                    f"still counted twice."
                    + (f" Excluded for want of a rate: {', '.join(excluded)}." if excluded else "")}


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
    fallback = sum(v for k, v in pl.get("classified_by", {}).items() if k != "Account Category")
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
    for line in pl["statement"]:
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


def arap_status(user):
    return {"settings": arap.settings(), "companies": arap.companies(), "recipients": arap.recipients(),
            "next_run": arap.next_run(), "runs": arap_notify.recent_runs(), "mail": arap_notify.mail_status(),
            "test": arap.test_status(user["username"])}


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

    def _send(self, status, body, ctype="application/json; charset=utf-8"):
        payload = body if isinstance(body, bytes) else json.dumps(body, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
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

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
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
            elif url.path == "/api/sales":
                self._send(200, sales_request(q.get("period"), q.get("mode", "month"), q.get("refresh") == "1",
                                              q.get("compare") or "prior"))
            elif url.path == "/api/executive":
                company = q.get("company") or config.BC_COMPANY
                try:
                    year = int(q.get("year") or date.today().year)
                except ValueError:
                    year = date.today().year
                year = max(date.today().year - 3, min(year, date.today().year))
                self._send(200, executive.request(client, company, year, company_currency(company),
                                                  q.get("refresh") == "1"))
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
                    self._send(200, r if r.get("pending") or r.get("available") is False else movement.public(r))
                    return
                r = movement.held(company, q.get("as_of") or "", days)
                if not r or r.get("available") is False:
                    self._send(409, {"error": "Read the movement report first."})
                elif url.path == "/api/movement/rows":
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
                t = typology.analyse(r, m.get("items") or {}, store.get_settings())
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
            elif url.path == "/api/drill":
                self._send(200, drill(q.get("kind", "pl"), q.get("key", "")))
            elif url.path == "/api/companies":
                self._send(200, {"companies": client.companies(), "default": config.BC_COMPANY,
                                 "dimensions": client.dimension_values(q.get("company") or config.BC_COMPANY)})
            elif url.path == "/api/actions":
                self._send(200, {"actions": store.list_actions(), "statuses": store.STATUSES,
                                 "priorities": store.PRIORITIES})
            elif url.path == "/api/settings":
                self._send(200, store.get_settings())
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
    fiscal.start_scheduler(client, config.FISK_COMPANY)
    if "--no-browser" not in sys.argv:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://{host}:{config.APP_PORT}")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")


if __name__ == "__main__":
    main()
