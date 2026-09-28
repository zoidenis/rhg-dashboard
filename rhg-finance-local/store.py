"""Application-owned storage: finance actions, thresholds and an audit trail.

Business Central stays strictly read-only. Everything the Purchasing Director
creates here (actions, comments, thresholds) lives in JSON files next to the app.
"""
import json
import threading
import uuid
from datetime import datetime, timedelta, date
from pathlib import Path

import config

DATA_DIR = config.BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
ACTIONS_FILE = DATA_DIR / "actions.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
AUDIT_FILE = DATA_DIR / "audit.json"

_lock = threading.Lock()

STATUSES = ["New", "Acknowledged", "In progress", "Waiting for approval",
            "Waiting for supplier", "Resolved", "Dismissed", "Overdue"]
PRIORITIES = ["Critical", "High", "Medium", "Low"]

DEFAULT_SETTINGS = {
    "thresholds": {
        "materiality": 50000,            # smallest amount worth a management exception (ALL)
        "pl_variance_pct": 5,            # P&L variance against comparison or budget
        "bs_movement_pct": 10,           # balance-sheet movement worth review
        "manual_journal": 100000,        # manual journal worth review
        "prior_period_days": 30,         # posting long after the document date
        "ap_critical_days": 60,          # payable overdue beyond this is critical
        "ar_warning_days": 30,
        "ar_critical_days": 60,
        "concentration_pct": 35,         # single supplier or customer share
        "cash_warning": 1000000,         # cash below this is a liquidity warning
        "stock_monitor_days": 30,        # no net movement for this long: monitor
        "stock_slow_days": 60,           # slow moving
        "stock_dead_days": 90,           # non moving
        "stock_value_floor": 10000,      # ignore small balances in the slow-stock lists
        "inventory_difference": 100000,  # inventory against G/L difference worth an exception
    },
    "owners": {"default": "", "accounting": "", "treasury": "", "receivables": "",
               "payables": "", "inventory": ""},
    "budget": {"name": ""},
    "inventory": {"accounts": "", "locations": ""},
    # Fallback rules for companies where Account Category was never filled in
    "accounts": {"income_prefixes": "", "cogs_prefixes": "", "expense_prefixes": "", "equity_prefixes": ""},
    # Business Central publishes no exchange-rate service here, so the group view uses
    # the rates entered below and always states which rate it used.
    "group": {"reporting_currency": "ALL",
              "currencies": "SALT=ALL, TBM=ALL, RKS Revo Hospitality Group=EUR, Zoi Greece=EUR",
              "rates": "EUR=100", "rate_note": ""},
    "period_closed_before": "",          # postings before this date are inside a closed period
    "exclude_weekends": True,
    "updated_at": None,
}


def _read(path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fallback


def _write(path, payload):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
    tmp.replace(path)


def _now():
    return datetime.now().isoformat(timespec="seconds")


# ---- settings ----

def get_settings():
    with _lock:
        s = _read(SETTINGS_FILE, {})
    merged = json.loads(json.dumps(DEFAULT_SETTINGS))
    merged["thresholds"].update(s.get("thresholds", {}))
    merged["owners"].update(s.get("owners", {}))
    merged["budget"].update(s.get("budget", {}))
    merged["inventory"].update(s.get("inventory", {}))
    merged["accounts"].update(s.get("accounts", {}))
    merged["group"].update(s.get("group", {}))
    for k in ("exclude_weekends", "updated_at", "period_closed_before"):
        if k in s:
            merged[k] = s[k]
    return merged


def save_settings(patch, actor="director"):
    with _lock:
        current = _read(SETTINGS_FILE, json.loads(json.dumps(DEFAULT_SETTINGS)))
        before = json.loads(json.dumps(current))
        th = current.setdefault("thresholds", {})
        for k, v in (patch.get("thresholds") or {}).items():
            if k in DEFAULT_SETTINGS["thresholds"]:
                try:
                    th[k] = float(v)
                except (TypeError, ValueError):
                    continue
        for k, v in (patch.get("owners") or {}).items():
            current.setdefault("owners", {})[k] = str(v)[:80]
        for k, v in (patch.get("budget") or {}).items():
            if k in DEFAULT_SETTINGS["budget"]:
                current.setdefault("budget", {})[k] = str(v)[:200]
        for k, v in (patch.get("inventory") or {}).items():
            if k in DEFAULT_SETTINGS["inventory"]:
                current.setdefault("inventory", {})[k] = str(v)[:400]
        for k, v in (patch.get("group") or {}).items():
            if k in DEFAULT_SETTINGS["group"]:
                current.setdefault("group", {})[k] = str(v)[:400]
        for k, v in (patch.get("accounts") or {}).items():
            if k in DEFAULT_SETTINGS["accounts"]:
                current.setdefault("accounts", {})[k] = str(v)[:200]
        if "period_closed_before" in patch:
            current["period_closed_before"] = str(patch["period_closed_before"])[:10]
        if "exclude_weekends" in patch:
            current["exclude_weekends"] = bool(patch["exclude_weekends"])
        current["updated_at"] = _now()
        _write(SETTINGS_FILE, current)
        _audit_unlocked("settings", "thresholds", actor, before.get("thresholds"), current.get("thresholds"))
    return get_settings()


# ---- audit ----

def _audit_unlocked(kind, target, actor, before, after, comment=""):
    log = _read(AUDIT_FILE, [])
    log.append({"ts": _now(), "kind": kind, "target": target, "actor": actor,
                "before": before, "after": after, "comment": comment})
    _write(AUDIT_FILE, log[-2000:])


def audit(kind, target, actor, before, after, comment=""):
    with _lock:
        _audit_unlocked(kind, target, actor, before, after, comment)


def get_audit(limit=200):
    with _lock:
        return list(reversed(_read(AUDIT_FILE, [])))[:limit]


# ---- actions ----

def _all_actions():
    return _read(ACTIONS_FILE, [])


def _refresh_overdue(actions):
    today = date.today().isoformat()
    for a in actions:
        if a["status"] in ("Resolved", "Dismissed"):
            continue
        if a.get("due_date") and a["due_date"] < today and a["status"] != "Overdue":
            a["status"] = "Overdue"
    return actions


def list_actions():
    with _lock:
        actions = _refresh_overdue(_all_actions())
        _write(ACTIONS_FILE, actions)
    return sorted(actions, key=lambda a: (PRIORITIES.index(a.get("priority", "Medium"))
                                          if a.get("priority") in PRIORITIES else 9,
                                          a.get("due_date") or "9999"))


def create_action(payload, actor="director"):
    due_default = (date.today() + timedelta(days=3)).isoformat()
    action = {
        "id": uuid.uuid4().hex[:10],
        "title": (payload.get("title") or "Untitled action")[:160],
        "description": payload.get("description", "")[:2000],
        "source_exception": payload.get("source_exception", ""),
        "bc_document": payload.get("bc_document", ""),
        "document_type": payload.get("document_type", ""),
        "company": payload.get("company", ""),
        "location": payload.get("location", ""),
        "supplier": payload.get("supplier", ""),
        "impact": float(payload.get("impact") or 0),
        "priority": payload.get("priority") if payload.get("priority") in PRIORITIES else "Medium",
        "owner": payload.get("owner", ""),
        "created_at": _now(),
        "created_by": actor,
        "due_date": payload.get("due_date") or due_default,
        "status": "New",
        "management_comment": payload.get("management_comment", ""),
        "resolution_comment": "",
        "evidence": payload.get("evidence") or [],
        "confidence": payload.get("confidence", "medium"),
        "history": [{"ts": _now(), "actor": actor, "from": "", "to": "New", "comment": "Created"}],
    }
    with _lock:
        actions = _all_actions()
        actions.append(action)
        _write(ACTIONS_FILE, actions)
        _audit_unlocked("action", action["id"], actor, None, {"status": "New", "title": action["title"]})
    return action


def update_action(action_id, patch, actor="director"):
    with _lock:
        actions = _all_actions()
        for a in actions:
            if a["id"] != action_id:
                continue
            before = {k: a.get(k) for k in ("status", "owner", "due_date", "priority")}
            comment = patch.get("comment", "")
            if patch.get("status") in STATUSES and patch["status"] != a["status"]:
                a["history"].append({"ts": _now(), "actor": actor, "from": a["status"],
                                     "to": patch["status"], "comment": comment})
                a["status"] = patch["status"]
            for field in ("owner", "due_date", "management_comment", "resolution_comment"):
                if field in patch:
                    a[field] = str(patch[field])[:2000]
            if patch.get("priority") in PRIORITIES:
                a["priority"] = patch["priority"]
            if comment and not patch.get("status"):
                a["history"].append({"ts": _now(), "actor": actor, "from": a["status"],
                                     "to": a["status"], "comment": comment})
            _write(ACTIONS_FILE, actions)
            _audit_unlocked("action", action_id, actor, before,
                            {k: a.get(k) for k in ("status", "owner", "due_date", "priority")}, comment)
            return a
    return None


CLOSING_FILE = DATA_DIR / "closing.json"

CLOSING_TEMPLATE = [
    ("Purchase invoices posted", "payables"), ("Sales postings completed", "accounting"),
    ("Bank reconciliation completed", "treasury"), ("Cash reconciliation completed", "treasury"),
    ("Accounts payable reconciled to the G/L", "payables"),
    ("Accounts receivable reconciled to the G/L", "receivables"),
    ("Inventory movements posted", "inventory"), ("Negative inventory resolved", "inventory"),
    ("Goods received not invoiced reviewed", "payables"),
    ("Physical count differences reviewed", "inventory"),
    ("Accruals posted", "accounting"), ("Prepayments reviewed", "accounting"),
    ("Depreciation posted", "accounting"), ("Fixed asset additions reviewed", "accounting"),
    ("Intercompany balances reconciled", "accounting"), ("Payroll postings completed", "accounting"),
    ("Tax accounts reviewed", "accounting"), ("Suspense and clearing accounts reconciled", "accounting"),
    ("Foreign exchange revaluation completed", "accounting"),
    ("Inventory reconciled to the G/L", "inventory"),
    ("P&L reviewed", "default"), ("Balance sheet reviewed", "default"),
    ("Management pack generated", "default"), ("Period closed", "default"),
]
CLOSING_STATUSES = ["Not started", "In progress", "Waiting for information", "Waiting for approval",
                    "Completed", "Reviewed", "Reopened"]


def closing_tasks(company, period, owners=None):
    """Returns the checklist for one company and period, creating it from the template
    on first use. Task state is the application's own; BC is never written to."""
    with _lock:
        book = _read(CLOSING_FILE, {})
        key = f"{company}|{period}"
        if key not in book:
            book[key] = [{"id": f"{i:02d}", "task": t, "area": area,
                          "owner": (owners or {}).get(area, "") or (owners or {}).get("default", ""),
                          "reviewer": "", "status": "Not started", "comment": "", "completed_at": None,
                          "history": []} for i, (t, area) in enumerate(CLOSING_TEMPLATE, 1)]
            _write(CLOSING_FILE, book)
        return book[key]


def update_closing(company, period, task_id, patch, actor="finance"):
    with _lock:
        book = _read(CLOSING_FILE, {})
        key = f"{company}|{period}"
        tasks = book.get(key)
        if not tasks:
            return None
        for t in tasks:
            if t["id"] != task_id:
                continue
            before = {k: t.get(k) for k in ("status", "owner", "reviewer")}
            if patch.get("status") in CLOSING_STATUSES and patch["status"] != t["status"]:
                t["history"].append({"ts": _now(), "actor": actor, "from": t["status"],
                                     "to": patch["status"], "comment": patch.get("comment", "")})
                t["status"] = patch["status"]
                t["completed_at"] = _now() if patch["status"] in ("Completed", "Reviewed") else None
            for f in ("owner", "reviewer", "comment"):
                if f in patch:
                    t[f] = str(patch[f])[:300]
            _write(CLOSING_FILE, book)
            _audit_unlocked("closing", f"{key}/{task_id}", actor, before,
                            {k: t.get(k) for k in ("status", "owner", "reviewer")}, patch.get("comment", ""))
            return t
    return None


def open_keys():
    """Source-exception keys that already have a live action, so the UI doesn't offer duplicates."""
    return {a["source_exception"] for a in list_actions()
            if a["source_exception"] and a["status"] not in ("Resolved", "Dismissed")}
