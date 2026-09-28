"""Application-owned storage: actions, SLA settings and an audit trail.

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
        "receipt_to_invoice_hours": 48,
        "po_open_days": 7,
        "invoice_posting_late_days": 7,
        "price_review_pct": 5,
        "price_critical_pct": 10,
        "price_critical_impact": 20000,
        "exception_spend": 100000,
        "min_spend_for_mover": 5000,
        "location_spread_pct": 20,
        "supplier_concentration_pct": 35,
        "requisition_warning_hours": 24,
        "requisition_critical_hours": 48,
        "price_above_contract_pct": 2,
        "stock_cover_days": 45,
        "stock_dead_days": 90,
        "stock_value_floor": 10000,
        "adjustment_pct": 5,
    },
    "owners": {"default": "", "price": "", "invoices": "", "data_quality": "", "requisitions": ""},
    "budget": {"name": "", "cogs_accounts": "6051,60111"},
    # Business Central holds each company's amounts in that company's own currency,
    # so this map only decides the label; nothing is converted.
    "currencies": {"map": "SALT=ALL, TBM=ALL, RKS Revo Hospitality Group=EUR, Zoi Greece=EUR",
                   "default": "ALL"},
    # Purchase-type classification. These are application settings, not Business Central
    # data: BC has no purchase-type field. Anything not mapped stays Unclassified.
    "classification": {"group3": "", "group2": "", "item_categories": "", "service_accounts": "",
                       "capex_accounts": "", "service_prefix": "", "prepayment_prefix": "",
                       "goods_prefixes": ""},
    # Delivery rhythm per location: lead / cycle / safety, in days. "*" is every location
    # that is not named. MAGAZINE holds imported goods, so it carries a long lead time.
    "stock": {"cadence": "*=2/3/2, MAGAZINE=45/30/10",
              "owners": "Stock Manager=, Unit Manager=, Purchasing Coordinator=, Cost Controller="},
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
    merged["classification"].update(s.get("classification", {}))
    merged["currencies"].update(s.get("currencies", {}))
    merged["stock"].update(s.get("stock", {}))
    for k in ("exclude_weekends", "updated_at"):
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
        for k, v in (patch.get("classification") or {}).items():
            if k in DEFAULT_SETTINGS["classification"]:
                current.setdefault("classification", {})[k] = str(v)[:4000]
        for k, v in (patch.get("currencies") or {}).items():
            if k in DEFAULT_SETTINGS["currencies"]:
                current.setdefault("currencies", {})[k] = str(v)[:400]
        for k, v in (patch.get("stock") or {}).items():
            if k in DEFAULT_SETTINGS["stock"]:
                current.setdefault("stock", {})[k] = str(v)[:600]
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


def open_keys():
    """Source-exception keys that already have a live action, so the UI doesn't offer duplicates."""
    return {a["source_exception"] for a in list_actions()
            if a["source_exception"] and a["status"] not in ("Resolved", "Dismissed")}
