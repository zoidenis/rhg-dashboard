"""Application-owned storage: finance actions, thresholds and an audit trail.

Business Central stays strictly read-only. Everything the Purchasing Director
creates here (actions, comments, thresholds) lives in JSON files next to the app.
"""
import json
import re
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
        "stock_deadstock_days": 180,     # dead stock: no qualifying consumption this long
        "stock_new_days": 30,            # a first receipt this recent makes a new item
        "stock_value_floor": 10000,      # ignore small balances in the slow-stock lists
        "inventory_difference": 100000,  # inventory against G/L difference worth an exception
    },
    "owners": {"default": "", "accounting": "", "treasury": "", "receivables": "",
               "payables": "", "inventory": ""},
    "budget": {"name": ""},
    "inventory": {"accounts": "", "locations": "",
                  # share of the value written down per stock class; a proposal, not a posting
                  "provision_rates": "Slow-moving=25, Non-moving=50",
                  # inventory accounts left out of days of inventory: held, not consumed
                  "dio_exclude": "327, 372",
                  # item categories that make an item a PN (intermediate product, needs a recipe);
                  # an assumption until Finance confirms what PN means
                  "pn_categories": "PRODUKT I NDERMJETEM",
                  # POS technical items that are not products (no recipe): item numbers or
                  # descriptions, a trailing * means "starts with"
                  "pos_technical_items": "FIRE*, 1 LEK*, NO EXTRA*",
                  # movement and dead stock: where transfers out count as use (they supply the
                  # restaurants), seasonal items or categories, perishable categories and their
                  # monitor / slow / non-moving / dead thresholds in days, and whether negative
                  # adjustments count as consumption (no, until the adjustment types are agreed)
                  "distribution_locations": "MAGAZINE, CENTRAL", "seasonal_items": "",
                  "perishable_categories": "", "perishable_days": "7, 14, 30, 60",
                  "count_negative_adjustments": "no",
                  # left out of movement and dead stock: equipment / small-inventory locations and
                  # goods in transit (* is a wildcard), and categories that are not consumed stock
                  "movement_exclude_locations": "*_IM, *_IN, TRANSIT",
                  "movement_exclude_categories": "INVENTAR I IMET",
                  # typology & turnover: nature of each item type (Group 2), safety days added to the
                  # delivery cycle, family targets that replace it (FAMILY=days), whether negative
                  # adjustments count as use for operating supplies, and the fresh families whose
                  # cover beyond the count threshold (days) means the book stock needs a count
                  "turnover_natures": "LP=ingredients, PN=ingredients, LP E SHITSHME=resale, "
                                      "I SHITSHEM=resale, AMBALAZH=supplies, PRODUKT PASTRIMI=supplies, "
                                      "BAR=supplies, RESTORANT=supplies, LENDE DJEGESE=supplies, "
                                      "MJETE KANCELARIE=supplies, MATERIALE NDIHMESE=supplies, "
                                      "MIREMBAJTJE=supplies, UNIFORMA=equipment, II=equipment",
                  "turnover_safety_days": "2", "turnover_targets": "",
                  "turnover_supplies_adjustments": "yes",
                  "turnover_fresh_families": "PERIME&FRUTE, MISH&SHPENDE, PESHK&PROD.DETI, BULMET&DJATHE, "
                                             "PROD BUKE&FURRE",
                  "turnover_fresh_check_days": "30"},
    # Fallback rules for companies where Account Category was never filled in
    "accounts": {"income_prefixes": "", "cogs_prefixes": "", "expense_prefixes": "", "equity_prefixes": ""},
    # Business Central publishes no exchange-rate service here, so the group view uses
    # the rates entered below and always states which rate it used.
    "group": {"reporting_currency": "ALL",
              "currencies": "SALT=ALL, TBM=ALL, RKS Revo Hospitality Group=EUR, Zoi Greece=EUR",
              "rates": "EUR=100", "rate_note": ""},
    # Executive dashboard (Control center): which accounts make sales, food cost, labour and
    # the items below EBITDA. "641*" means every account starting 641. Blank = the default.
    "executive": {"sales": "", "food": "", "labor": "", "below": ""},
    # Daily email with the receivables and payables exceptions (sent through Purchasing's mail server)
    "arap_notify": {"enabled": False, "to": "", "cc": "", "time": "08:00", "working_days_only": True,
                    "companies": "", "send_empty": True},
    # weekly cost control email (inventory exceptions and an action list) for the week before:
    # the day (0 = Monday) and time it goes out, recipients and companies
    "cost_report": {"enabled": False, "to": "", "cc": "", "day": "0", "time": "08:30", "companies": ""},
    # weekly and monthly performance email of every brand (sales and costs against budget and last
    # year): one email per brand to its own addresses ("BRAND: emails" lines), finance and operations in Cc;
    # monthly goes twice: provisional on the first working day, final on final_day
    "brand_report": {"weekly": False, "monthly": False, "day": "0", "time": "09:00", "final_day": "10",
                     "cc": "", "recipients": ""},
    # fiscalized purchase invoices (Tax Administration / Self-Care) against BC: daily read and email
    "fiscal": {"enabled": False, "to": "", "cc": "", "time": "07:30", "working_days_only": True,
               "days": "45", "grace_days": "7", "tolerance": "1", "send_empty": False,
               # the NIPT of the BC company being reconciled; when the certificate is another NIPT's,
               # the screen says so (the service answers for the certificate's holder only)
               "company_nipt": ""},
    # Location intelligence: the registry (Simphony location | name | brand, BC company, location,
    # POS customer, area, map position) is empty here, which means the built-in list in
    # locintel.py; the thresholds that raise a restaurant's alerts
    "locations": {"registry": "", "food_cost_target": "30", "cover_days": "30", "error_corrects_pct": "5",
                  "discounts_pct": "8", "voids_pct": "2", "sales_drop_pct": "15",
                  # Inventory → Location intelligence: slow / non-moving / dead stock as a share of the
                  # stock on hand, and the recipe coverage of the items sold, that raise an alert
                  "slow_stock_pct": "15", "recipe_coverage_pct": "90",
                  # Finance → Location intelligence: prime cost (food + labour) as a share of sales, EBITDA
                  # below budget, and the gap between Simphony and the sales posted in BC, that raise an alert
                  "prime_cost_pct": "65", "budget_gap_pct": "10", "posting_gap_pct": "3"},
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
    merged["executive"].update(s.get("executive", {}))
    merged["arap_notify"].update(s.get("arap_notify", {}))
    merged["cost_report"].update(s.get("cost_report", {}))
    merged["brand_report"].update(s.get("brand_report", {}))
    merged["fiscal"].update(s.get("fiscal", {}))
    merged["locations"].update(s.get("locations", {}))
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
                current.setdefault("inventory", {})[k] = str(v)[:1000]
        for k, v in (patch.get("group") or {}).items():
            if k in DEFAULT_SETTINGS["group"]:
                current.setdefault("group", {})[k] = str(v)[:400]
        for k, v in (patch.get("arap_notify") or {}).items():
            if k in DEFAULT_SETTINGS["arap_notify"]:
                if isinstance(DEFAULT_SETTINGS["arap_notify"][k], bool):
                    v = bool(v)
                elif k == "time":
                    v = str(v)[:5] if re.match(r"^([01]\d|2[0-3]):[0-5]\d$", str(v)) else "08:00"
                else:
                    v = str(v)[:400]
                current.setdefault("arap_notify", {})[k] = v
        for k, v in (patch.get("cost_report") or {}).items():
            if k in DEFAULT_SETTINGS["cost_report"]:
                if isinstance(DEFAULT_SETTINGS["cost_report"][k], bool):
                    v = bool(v)
                elif k == "time":
                    v = str(v)[:5] if re.match(r"^([01]\d|2[0-3]):[0-5]\d$", str(v)) else "08:30"
                elif k == "day":
                    v = str(v) if str(v) in ("0", "1", "2", "3", "4", "5", "6") else "0"
                else:
                    v = str(v)[:400]
                current.setdefault("cost_report", {})[k] = v
        for k, v in (patch.get("brand_report") or {}).items():
            if k in DEFAULT_SETTINGS["brand_report"]:
                if isinstance(DEFAULT_SETTINGS["brand_report"][k], bool):
                    v = bool(v)
                elif k == "time":
                    v = str(v)[:5] if re.match(r"^([01]\d|2[0-3]):[0-5]\d$", str(v)) else "09:00"
                elif k == "day":
                    v = str(v) if str(v) in ("0", "1", "2", "3", "4", "5", "6") else "0"
                elif k == "final_day":
                    try:
                        v = str(max(2, min(28, int(float(v)))))
                    except (TypeError, ValueError):
                        v = "10"
                elif k == "recipients":
                    v = str(v)[:4000]
                else:
                    v = str(v)[:400]
                current.setdefault("brand_report", {})[k] = v
        for k, v in (patch.get("fiscal") or {}).items():
            if k in DEFAULT_SETTINGS["fiscal"]:
                if isinstance(DEFAULT_SETTINGS["fiscal"][k], bool):
                    v = bool(v)
                elif k == "time":
                    v = str(v)[:5] if re.match(r"^([01]\d|2[0-3]):[0-5]\d$", str(v)) else "07:30"
                elif k in ("days", "grace_days", "tolerance"):
                    try:
                        v = str(max(0, min(400, float(v))))
                    except (TypeError, ValueError):
                        continue
                    v = v[:-2] if v.endswith(".0") else v
                else:
                    v = str(v)[:400]
                current.setdefault("fiscal", {})[k] = v
        for k, v in (patch.get("locations") or {}).items():
            if k in DEFAULT_SETTINGS["locations"]:
                if k == "registry":
                    v = str(v).replace("\r\n", "\n")[:8000]
                else:
                    try:
                        v = str(max(0.0, min(1000.0, float(v))))
                    except (TypeError, ValueError):
                        continue
                    v = v[:-2] if v.endswith(".0") else v
                current.setdefault("locations", {})[k] = v
        for k, v in (patch.get("executive") or {}).items():
            if k in DEFAULT_SETTINGS["executive"]:
                current.setdefault("executive", {})[k] = str(v)[:600]
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
