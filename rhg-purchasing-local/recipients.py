"""Who receives each report, company by company.

One directory for both towers: for every Business Central company (SALT, TBM, RKS Revo Hospitality Group,
Zoi Greece, ...) the people in each role (purchasing coordinator, cost controller, finance, ...), plus a
row for the whole group that is added to every company. Each report goes to the roles set for it, To or
Cc. The directory lives here, next to the mail server; Finance reads it through /internal/recipients and
saves through the same path.

When a company has nobody in the roles of a report, the report keeps the addresses set in its own
Settings, as before the directory existed. When it has people in Cc only, the general To stays.
"""
import json
import re
import threading
from datetime import datetime

import store

FILE = store.DATA_DIR / "recipients.json"
_lock = threading.Lock()
GROUP = "*"
EMAIL_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")

ROLES = [("purchasing_coordinator", "Purchasing coordinator"),
         ("purchasing_manager", "Purchasing manager"),
         ("cost_controller", "Cost controller"),
         ("finance", "Finance · payables and receivables"),
         ("accounting", "Chief accountant · fiscal"),
         ("operations", "Operations"),
         ("management", "Management")]
# key, label, tower, To roles, Cc roles (the defaults; changed in Settings)
REPORTS = [("purchasing_daily", "Invoices without a PO · daily", "Purchasing",
            ["purchasing_coordinator"], ["purchasing_manager", "cost_controller"]),
           ("purchasing_weekly", "Purchasing report · weekly", "Purchasing",
            ["purchasing_manager", "purchasing_coordinator"], ["cost_controller"]),
           ("savings_monthly", "Price savings · monthly", "Purchasing",
            ["purchasing_manager", "purchasing_coordinator"], ["cost_controller"]),
           ("arap_daily", "Payables and receivables exceptions · daily", "Finance", ["finance"], ["accounting"]),
           ("cost_weekly", "Cost control · weekly", "Finance", ["cost_controller"], ["purchasing_manager"]),
           ("fiscal_daily", "Fiscal invoices against BC · daily", "Finance", ["accounting"], ["finance"]),
           ("brand_performance", "Brand performance · weekly and monthly", "Finance",
            [], ["finance", "operations"])]
ROLE_KEYS = {k for k, _ in ROLES}
REPORT_KEYS = {r[0] for r in REPORTS}


def parse(text):
    good, bad = [], []
    for part in re.split(r"[,;\s]+", text or ""):
        part = part.strip()
        if part:
            (good if EMAIL_RE.match(part) else bad).append(part)
    return good, bad


def load():
    with _lock:
        try:
            d = json.loads(FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = {}
    d.setdefault("people", {})
    routing = d.get("routing") or {}
    d["routing"] = {k: {"to": list((routing.get(k) or {}).get("to", to)), "cc": list((routing.get(k) or {}).get("cc", cc))}
                    for k, _, _, to, cc in REPORTS}
    return d


def public():
    """The directory and what it means, for both towers' screens and for Finance's reports."""
    d = load()
    return {"people": d["people"], "routing": d["routing"], "group": GROUP,
            "roles": [{"key": k, "label": label} for k, label in ROLES],
            "reports": [{"key": k, "label": label, "tower": tower, "default": {"to": to, "cc": cc}}
                        for k, label, tower, to, cc in REPORTS],
            "updated_at": d.get("updated_at"), "updated_by": d.get("updated_by")}


def save(patch, actor):
    """patch: {"people": {company: {role: "emails"}}, "routing": {report: {"to": [roles], "cc": [roles]}}}.
    Raises ValueError on an address that is not one."""
    people, bad = {}, []
    for company, roles in (patch.get("people") or {}).items():
        company = str(company).strip()[:80]
        if not company or not isinstance(roles, dict):
            continue
        row = {}
        for role, text in roles.items():
            if role not in ROLE_KEYS:
                continue
            good, wrong = parse(str(text or "")[:600])
            bad += wrong
            if good:
                row[role] = ", ".join(dict.fromkeys(good))
        if row:
            people[company] = row
    if bad:
        raise ValueError("Not an email address: " + ", ".join(bad[:8]))
    routing = {}
    for k, _, _, to, cc in REPORTS:
        r = (patch.get("routing") or {}).get(k)
        if not isinstance(r, dict):
            routing[k] = {"to": to, "cc": cc}
            continue
        t = [x for x in dict.fromkeys(r.get("to") or []) if x in ROLE_KEYS]
        c = [x for x in dict.fromkeys(r.get("cc") or []) if x in ROLE_KEYS and x not in t]
        routing[k] = {"to": t, "cc": c}
    before = load()
    d = {"people": people, "routing": routing, "updated_at": datetime.now().isoformat(timespec="seconds"),
         "updated_by": actor}
    with _lock:
        tmp = FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(FILE)
    store.audit("settings", "email recipients by company", actor,
                {"people": before.get("people"), "routing": before.get("routing")},
                {"people": people, "routing": routing})
    return public()


def resolve_in(d, report, companies, general_to=(), general_cc=(), promote_cc=True):
    """{"to", "cc", "source"} of one report for the companies it covers (one, or the companies of a brand).
    The same function sits in Finance's recipients.py; keep the two alike."""
    people = {str(k).strip().upper(): v for k, v in (d.get("people") or {}).items()}
    route = (d.get("routing") or {}).get(report) or {"to": [], "cc": []}

    def emails(roles):
        out = []
        for c in [*companies, GROUP]:
            row = people.get(str(c or "").strip().upper()) or {}
            for role in roles:
                out += parse(row.get(role, ""))[0]
        return list(dict.fromkeys(out))
    to, cc = emails(route.get("to") or []), emails(route.get("cc") or [])
    general_to, general_cc = list(general_to or []), list(general_cc or [])
    if not to and not cc:
        return {"to": general_to, "cc": [x for x in general_cc if x not in general_to], "source": "general"}
    if not to:
        to = general_to or (cc[:1] if promote_cc else [])
    return {"to": to, "cc": [x for x in cc if x not in to], "source": "company"}


def resolve(report, companies, general_to=(), general_cc=(), promote_cc=True):
    return resolve_in(load(), report, companies, general_to, general_cc, promote_cc)
