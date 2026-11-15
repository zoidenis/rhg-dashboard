"""Executive Action Center: the one place where every automated control of pulse ends, with its owner, its
deadline and its progress, for the CEO, the CFO and the heads of Finance, Purchasing, Warehouse and Operations.

It sits over what already exists and does not replace it:
  * the Exceptions registers of Finance and Purchasing (the controls of the towers: negative stock, received vs
    discharged, Simphony ↔ BC, takings, Billy, SevenRooms, payables and receivables, statements, purchase orders,
    prices and contracts, invoices without PO, items tracked by hand), read through /internal/register;
  * Unicorn's registers: the actions of approved decisions and the proposals waiting for approval.
Each item keeps living where it was found: a status change, a comment, a new owner or deadline made here is sent
back to its register (/internal/register/update), which keeps its history.

Two matrices drive it:
  * the SLA matrix: days to the deadline by department and severity. The towers' registers read it
    (/internal/actioncenter/policy) to date every new item; "Apply the SLA" re-dates the open items nobody
    re-dated by hand;
  * the responsibility matrix: the role that owns each type of item (the registers' rules) and the people in
    each role, by company and restaurant (Settings → Email recipients by company, kept in Purchasing), who are
    pulse accounts (matched by their email).

Escalation: an item past its deadline goes to the head of its department after `level1_days`, and to the
executives after `level2_days`. The combined daily email (one per owner, every tower and Unicorn together, a
reminder for what is due tomorrow, the escalations in Cc) replaces the registers' own digests when it is on.

Kept in data/actioncenter.json (the matrices, the email settings, the runs). Nothing is written to Business
Central or any other source system.
"""
import html
import json
import threading
import time
import traceback
from datetime import date, datetime, timedelta

import requests

import config
import store
import unicorn_registers as ureg

FILE = config.BASE_DIR / "data" / "actioncenter.json"
FINANCE_URL = config.get("FINANCE_URL", "http://finance:8766").rstrip("/")
PURCHASING_URL = config.get("PURCHASING_URL", "http://purchasing:8765").rstrip("/")
PUBLIC_URL = (config.get("PUBLIC_URL", "https://dashboard.rhg.al") or "").rstrip("/")
TOWERS = {"finance": ("Finance", FINANCE_URL), "purchasing": ("Purchasing", PURCHASING_URL)}

DEPARTMENTS = ["Finance", "Purchasing", "Warehouse", "Operations", "Executive"]
SEVERITIES = ["critical", "high", "medium", "low"]
DEFAULT_SLA = {d: {"critical": 2, "high": 5, "medium": 7, "low": 10} for d in DEPARTMENTS}
DEPARTMENT_OF_ROLE = {"purchasing_coordinator": "Purchasing", "purchasing_manager": "Purchasing", "inputer": "Finance",
                      "finance": "Finance", "accounting": "Finance", "cost_controller": "Finance",
                      "stock_manager": "Warehouse", "operations": "Operations", "unit_manager": "Operations",
                      "head_chef": "Operations", "executive_chef": "Operations", "coo": "Executive",
                      "cfo": "Executive", "ceo": "Executive", "management": "Executive", "others": "Executive"}
DEPARTMENT_OF_CAPABILITY = {"financial": "Finance", "purchasing": "Purchasing", "inventory": "Warehouse",
                            "restaurant": "Operations", "operations": "Operations", "people": "Operations",
                            "executive": "Executive", "data": "Finance"}
# the department of a role of Administration (Admin → Roles: its department) in the Action Center
DEPARTMENT_OF_ADMIN = {"finance": "Finance", "purchasing": "Purchasing", "sales": "Operations", "operations": "Operations",
                       "hr": "Operations", "warehouse": "Warehouse", "inventory": "Warehouse", "management": "Executive"}
SEVERITY_OF_REGISTER = {"critical": "critical", "warning": "high", "info": "low"}
ACTIVE = ("Open", "In progress", "Awaiting verification", "Awaiting approval")
DEFAULTS = {
    "sla": DEFAULT_SLA,
    "escalation": {"level1_days": 1, "level2_days": 3,
                   "heads": {"Finance": "cfo", "Purchasing": "purchasing_manager", "Warehouse": "coo",
                             "Operations": "coo", "Executive": "ceo"},
                   "executives": ["ceo", "cfo"]},
    "digest": {"enabled": False, "time": "08:30", "reminder_days": 1},
}
_lock = threading.RLock()
_cache = {}


# ---------------------------------------------------------------- settings
def _read():
    try:
        return json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write(d):
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(FILE)


def settings():
    d = _read()
    s = json.loads(json.dumps(DEFAULTS))
    for dept, row in (d.get("sla") or {}).items():
        if dept in s["sla"]:
            for sev, v in (row or {}).items():
                if sev in SEVERITIES and isinstance(v, int) and 0 < v <= 180:
                    s["sla"][dept][sev] = v
    e = d.get("escalation") or {}
    for k in ("level1_days", "level2_days"):
        if isinstance(e.get(k), int) and 0 <= e[k] <= 60:
            s["escalation"][k] = e[k]
    s["escalation"]["heads"].update({k: v for k, v in (e.get("heads") or {}).items() if k in DEPARTMENTS and v})
    if isinstance(e.get("executives"), list) and e["executives"]:
        s["escalation"]["executives"] = [str(x) for x in e["executives"]][:10]
    g = d.get("digest") or {}
    s["digest"]["enabled"] = bool(g.get("enabled"))
    if isinstance(g.get("time"), str) and len(g["time"]) == 5 and g["time"][2] == ":":
        s["digest"]["time"] = g["time"]
    if isinstance(g.get("reminder_days"), int) and 0 <= g["reminder_days"] <= 7:
        s["digest"]["reminder_days"] = g["reminder_days"]
    s["owner_rules"] = {k: v for k, v in (d.get("owner_rules") or {}).items()
                        if isinstance(v, str) and (v.startswith("admin:") or v.startswith("dir:"))}
    s["updated_at"], s["updated_by"] = d.get("updated_at"), d.get("updated_by")
    s["last_digest"] = d.get("last_digest")
    return s


def save_settings(patch, actor):
    """patch: {"sla": {dept: {sev: days}}, "escalation": {...}, "digest": {...}} (any part)."""
    with _lock:
        d = _read()
        if "sla" in patch:
            sla = {}
            for dept in DEPARTMENTS:
                row = (patch["sla"] or {}).get(dept) or {}
                sla[dept] = {}
                for sev in SEVERITIES:
                    try:
                        v = int(row.get(sev, DEFAULT_SLA[dept][sev]))
                    except (TypeError, ValueError) as exc:
                        raise ValueError(f"SLA {dept} · {sev}: a number of days.") from exc
                    if not 0 < v <= 180:
                        raise ValueError(f"SLA {dept} · {sev}: between 1 and 180 days.")
                    sla[dept][sev] = v
            d["sla"] = sla
        if "escalation" in patch:
            e = patch["escalation"] or {}
            out = {}
            for k in ("level1_days", "level2_days"):
                try:
                    out[k] = int(e.get(k, DEFAULTS["escalation"][k]))
                except (TypeError, ValueError) as exc:
                    raise ValueError("Escalation: days as numbers.") from exc
            if out["level2_days"] < out["level1_days"]:
                raise ValueError("Escalation: the executives come after the head of department.")
            out["heads"] = {k: str(v)[:40] for k, v in (e.get("heads") or {}).items() if k in DEPARTMENTS and v}
            out["executives"] = [str(x)[:40] for x in (e.get("executives") or DEFAULTS["escalation"]["executives"])][:10]
            d["escalation"] = out
        if "digest" in patch:
            g = patch["digest"] or {}
            t = str(g.get("time") or "08:30")
            try:
                datetime.strptime(t, "%H:%M")
            except ValueError as exc:
                raise ValueError("Email time: HH:MM, e.g. 08:30.") from exc
            d["digest"] = {"enabled": bool(g.get("enabled")), "time": t,
                           "reminder_days": max(0, min(7, int(g.get("reminder_days") or 1)))}
        d["updated_at"], d["updated_by"] = store.now(), actor
        _write(d)
    store.audit("action center settings", actor, "", {k: patch.get(k) for k in ("sla", "escalation", "digest") if k in patch})
    return settings()


def admin_roles():
    """The roles of Administration (Admin → Roles & permissions) with their department and their active accounts
    (email, name, access scope): a role of the responsibility matrix as people are given it in Administration."""
    import auth
    people = {}
    for r in store.q("SELECT * FROM users WHERE active=1"):
        if r["email"]:
            people.setdefault(r["role"], []).append({"email": r["email"].strip().lower(), "name": r["name"],
                                                     "username": r["username"], "scope": auth.effective_scope(r)})
    out = []
    for r in store.q("SELECT name, department FROM roles ORDER BY name"):
        out.append({"name": r["name"], "department": r["department"] or "Other",
                    "ac_department": DEPARTMENT_OF_ADMIN.get((r["department"] or "").strip().lower(), "Executive"),
                    "people": sorted(people.get(r["name"], []), key=lambda p: p["name"])})
    return out


def policy():
    """What the towers' registers read: the SLA matrix, whether the combined email replaces theirs, the owner
    of each type of item set in the responsibility matrix and the accounts of the roles of Administration."""
    s = settings()
    A = admin_roles()
    return {"sla": s["sla"], "combined_digest": s["digest"]["enabled"], "owner_rules": s["owner_rules"],
            "admin_people": {r["name"]: [{"email": p["email"], "scope": p["scope"]} for p in r["people"]] for r in A},
            "admin_departments": {r["name"]: r["ac_department"] for r in A}}


def save_owner_rule(tower, source, kind, target, actor):
    """The owner of a type of item: "admin:<role of Administration>", "dir:<role of Email recipients>", or ""
    for the register's own rule. The registers take it within 5 minutes; "Apply" hands over the open items."""
    if tower not in [n for n, _ in TOWERS.values()] or not source:
        raise ValueError("Unknown register or type of item.")
    if target and not (target.startswith("admin:") or target.startswith("dir:")):
        raise ValueError("Choose a role of Administration or of Email recipients.")
    if target.startswith("admin:") and target[6:] not in {r["name"] for r in admin_roles()}:
        raise ValueError("That role is not in Administration.")
    key = f"{tower}|{source}|{kind or '*'}"
    with _lock:
        d = _read()
        rules = d.get("owner_rules") or {}
        if target:
            rules[key] = target[:120]
        else:
            rules.pop(key, None)
        d["owner_rules"], d["updated_at"], d["updated_by"] = rules, store.now(), actor
        _write(d)
    store.audit("action center owner rule", actor, key, {"owner": target or "the register's rule"})
    return settings()["owner_rules"]


def role_label(role, labels):
    if str(role).startswith("admin:"):
        return role[6:]
    return labels.get(role, role or "")


def department_of(role, default, admin=None):
    if str(role).startswith("admin:"):
        return (admin or {}).get(role[6:], "Executive")
    return DEPARTMENT_OF_ROLE.get(role or "", default)


# ---------------------------------------------------------------- reading the registers
def _tower(key, max_age=60):
    name, base = TOWERS[key]
    with _lock:
        c = _cache.get(key)
        if c and time.time() - c["at"] < max_age:
            return c["value"]
    try:
        r = requests.get(base + "/internal/register", timeout=25, headers={"X-Service-Token": config.SERVICE_TOKEN})
        value = r.json() if r.ok else {"error": f"{name} answered {r.status_code}"}
    except (requests.RequestException, ValueError) as exc:
        old = (_cache.get(key) or {}).get("value")
        value = dict(old, stale=True) if old and not old.get("error") else {"error": f"{name} could not be reached: {str(exc)[:120]}"}
    with _lock:
        _cache[key] = {"at": time.time(), "value": value}
    return value


def forget():
    with _lock:
        _cache.clear()


def users():
    """{email (lower): {username, name, role}} of the active pulse accounts."""
    out = {}
    for r in store.q("SELECT username, name, email, role FROM users WHERE active=1"):
        if r["email"]:
            out[r["email"].strip().lower()] = {"username": r["username"], "name": r["name"], "role": r["role"]}
    return out


def _directory():
    """Settings → Email recipients by company (Purchasing): the people in each role, by company and location."""
    with _lock:
        c = _cache.get("directory")
        if c and time.time() - c["at"] < 120:
            return c["value"]
    try:
        r = requests.get(PURCHASING_URL + "/internal/recipients", timeout=15,
                         headers={"X-Service-Token": config.SERVICE_TOKEN})
        value = r.json() if r.ok else None
    except (requests.RequestException, ValueError):
        value = None
    if value is None:
        value = (_cache.get("directory") or {}).get("value") or {}
    with _lock:
        _cache["directory"] = {"at": time.time(), "value": value}
    return value


def _emails(text):
    import re
    return [x.strip().lower() for x in re.split(r"[,;\s]+", text or "") if "@" in x]


def role_people(role, company, location=""):
    """The emails in a role for a company (its location row first, else the company and the whole-group rows)."""
    d = _directory()
    locs = next((v for k, v in (d.get("locations") or {}).items() if str(k).upper() == str(company or "").upper()), {})
    if location:
        row = next((v for k, v in locs.items() if str(k).upper() == str(location).upper()), {})
        got = _emails(row.get(role, ""))
        if got:
            return got
    people = {str(k).strip().upper(): v for k, v in (d.get("people") or {}).items()}
    out = []
    for c in (company, "*"):
        out += _emails((people.get(str(c or "").strip().upper()) or {}).get(role, ""))
    return list(dict.fromkeys(out))


def _due_from_sla(first_seen, dept, sev, sla):
    try:
        return (date.fromisoformat(str(first_seen)[:10]) + timedelta(days=sla[dept][sev])).isoformat()
    except (ValueError, KeyError, TypeError):
        return None


def _finish(x, today, s, U):
    """Overdue, escalation level, owners as accounts: the parts every item shares."""
    esc = s["escalation"]
    x["active"] = x["status"] in ACTIVE
    due = x.get("due") or ""
    x["overdue"] = x["active"] and bool(due) and due < today
    x["days_overdue"] = (date.fromisoformat(today) - date.fromisoformat(due)).days if x["overdue"] else 0
    soon = (date.fromisoformat(today) + timedelta(days=s["digest"]["reminder_days"])).isoformat()
    x["due_soon"] = x["active"] and bool(due) and today <= due <= soon
    x["escalation"] = 2 if x["days_overdue"] >= max(1, esc["level2_days"]) else 1 if x["days_overdue"] >= max(1, esc["level1_days"]) else 0
    x["on_time"] = (bool(x.get("closed_at")) and bool(due) and str(x["closed_at"])[:10] <= due) if x["status"] == "Closed" else None
    owners = x.get("owners") or []
    x["owner_accounts"] = [dict(U[o], email=o) for o in owners if o in U]
    x["owners_without_account"] = [o for o in owners if o not in U]
    return x


def collect(scope=None):
    """Every item of the registers and of Unicorn, unified. scope: the person's access (companies, locations)."""
    s, today, U = settings(), date.today().isoformat(), users()
    admin_dept = {r["name"]: r["ac_department"] for r in admin_roles()}
    items, sources, errors, runs = [], {}, [], []
    for key, (name, _base) in TOWERS.items():
        d = _tower(key) or {}
        if d.get("error"):
            errors.append(d["error"])
            continue
        if d.get("stale"):
            errors.append(f"{name}: showing the last list read; the tower did not answer just now")
        roles = d.get("roles") or {}
        for src, label in (d.get("sources") or {}).items():
            sources[f"{key}:{src}"] = f"{name} · {label}"
        runs += [dict(r, tower=name) for r in d.get("runs") or []]
        for r in d.get("items") or []:
            dept = department_of(r.get("role") or "", "Finance" if key == "finance" else "Purchasing", admin_dept)
            sev = SEVERITY_OF_REGISTER.get(r.get("severity"), "high")
            st = "Closed" if r["status"] == "Closed" else r["status"]
            x = {"uid": f"{key}:{r['id']}", "origin": key, "tower": name, "ref": r["id"],
                 "source": f"{key}:{r['source']}", "source_label": f"{name} · {(d.get('sources') or {}).get(r['source'], r['source'])}",
                 "department": dept, "kind": r.get("kind") or "", "company": r.get("company") or "",
                 "location": r.get("location") or "", "title": r.get("title") or "", "detail": r.get("detail") or "",
                 "severity": sev, "value": r.get("value"), "role": r.get("role") or "",
                 "role_label": role_label(r.get("role") or "", roles),
                 "owners": _emails(r.get("owner")), "owner_set_by_hand": bool(r.get("owner_set")),
                 "status": st, "first_seen": (r.get("first_seen") or "")[:10], "due": r.get("due") or "",
                 "sla_due": _due_from_sla(r.get("first_seen"), dept, sev, s["sla"]),
                 "closed_at": r.get("closed_at"), "closed_by": r.get("closed_by"), "note": r.get("note") or "",
                 "age": r.get("age"), "reopened": r.get("reopened") or 0, "history": r.get("history") or [],
                 "link": d.get("link") or "", "manual": r.get("source") == "manual"}
            items.append(_finish(x, today, s, U))
    # Unicorn: the actions of approved decisions, the proposals waiting for a person
    try:
        F = {f["id"]: f for f in ureg.findings(None, limit=2000)}
        D = {d["id"]: d for d in ureg.decisions(limit=1000)}
        for a in ureg.actions(limit=1000):
            d = D.get(a["decision_id"]) or {}
            f = F.get(d.get("finding_id")) or {}
            dept = DEPARTMENT_OF_CAPABILITY.get(f.get("capability"), "Executive")
            sev = f.get("severity") if f.get("severity") in SEVERITIES else "medium"
            st = {"Done": "Closed", "Cancelled": "Dismissed"}.get(a["status"], a["status"])
            own = _emails(a["owner"]) or [e for e, u in U.items() if a["owner"] and a["owner"].strip().lower() in
                                          (u["name"].lower(), u["username"].lower())]
            x = {"uid": f"unicorn:A{a['id']}", "origin": "unicorn", "tower": "Unicorn", "ref": a["id"],
                 "source": "unicorn:action", "source_label": "Unicorn · approved decision", "department": dept,
                 "kind": f.get("rule") or "", "company": a["company"] or f.get("company") or "",
                 "location": a["location"] or "", "title": a["title"],
                 "detail": (f"KPI {a['metric']}: {a['baseline']} → target {a['target']}"
                            + (f", latest {a['latest']} ({a['latest_at']})" if a["latest"] is not None else "")) if a["metric"] else (d.get("rationale") or "")[:300],
                 "severity": sev, "value": d.get("impact"), "role": "", "role_label": a["owner"],
                 "owners": own, "owner_text": a["owner"], "status": st, "first_seen": (a["created_at"] or "")[:10],
                 "due": a["due"], "sla_due": a["due"], "closed_at": a["closed_at"], "closed_by": a["closed_by"],
                 "note": (a["notes"][-1]["text"] if a["notes"] else ""), "age": None, "reopened": 0,
                 "history": a["notes"][-6:], "link": "/unicorn#decisions", "manual": False,
                 "kpi": {"metric": a["metric"], "unit": a["unit"], "baseline": a["baseline"], "target": a["target"],
                         "latest": a["latest"], "effect": a["effect"]}}
            items.append(_finish(x, today, s, U))
        for d in D.values():
            if d["status"] != "Proposed":
                continue
            f = F.get(d["finding_id"]) or {}
            sev = f.get("severity") if f.get("severity") in SEVERITIES else "medium"
            x = {"uid": f"unicorn:D{d['id']}", "origin": "unicorn", "tower": "Unicorn", "ref": d["id"],
                 "source": "unicorn:proposal", "source_label": "Unicorn · proposal waiting for approval",
                 "department": "Executive", "kind": f.get("rule") or "", "company": f.get("company") or "",
                 "location": f.get("location") or "", "title": d["title"], "detail": (d["rationale"] or "")[:400],
                 "severity": sev, "value": d["impact"], "role": "", "role_label": d["approver_role"] or "Executive",
                 "owners": [], "status": "Awaiting approval", "first_seen": (d["proposed_at"] or "")[:10],
                 "due": _due_from_sla(d["proposed_at"], "Executive", sev, s["sla"]) or "", "sla_due": None,
                 "closed_at": None, "closed_by": None, "note": "", "age": None, "reopened": 0, "history": [],
                 "link": "/unicorn#decisions", "manual": False}
            items.append(_finish(x, today, s, U))
        sources.update({"unicorn:action": "Unicorn · approved decision", "unicorn:proposal": "Unicorn · proposal waiting"})
    except Exception as exc:                                        # noqa: BLE001
        traceback.print_exc()
        errors.append(f"Unicorn: {str(exc)[:160]}")
    if scope and scope.get("companies", "*") != "*":
        comps = {c.upper() for c in scope["companies"]}
        items = [x for x in items if not x["company"] or x["company"].upper() in comps]
    for co, locs in ((scope or {}).get("locations") or {}).items():
        allowed = {str(v).upper() for v in locs}
        items = [x for x in items if x["company"].upper() != co.upper() or not x["location"] or x["location"].upper() in allowed]
    return {"items": items, "sources": sources, "errors": errors, "runs": sorted(runs, key=lambda r: r.get("at") or "", reverse=True)[:40],
            "settings": s, "today": today}


RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def stats(items, today=None):
    """The executive figures: totals, by department, by owner, on-time closing in the last 30 days."""
    today = today or date.today().isoformat()
    d30 = (date.fromisoformat(today) - timedelta(days=30)).isoformat()
    d7 = (date.fromisoformat(today) - timedelta(days=7)).isoformat()
    act = [x for x in items if x["active"]]
    closed30 = [x for x in items if x["status"] == "Closed" and str(x.get("closed_at") or "")[:10] >= d30]

    def block(xs, cl):
        timed = [x for x in cl if x["on_time"] is not None]
        ages = sorted((date.fromisoformat(str(x["closed_at"])[:10]) - date.fromisoformat(x["first_seen"])).days
                      for x in cl if x.get("closed_at") and x.get("first_seen"))
        return {"open": len(xs), "overdue": sum(x["overdue"] for x in xs),
                "critical": sum(x["severity"] == "critical" for x in xs),
                "due_soon": sum(x["due_soon"] for x in xs), "escalated": sum(x["escalation"] > 0 for x in xs),
                "awaiting_approval": sum(x["status"] == "Awaiting approval" for x in xs),
                "no_owner": sum(1 for x in xs if not x["owners"] and x["status"] != "Awaiting approval"),
                "closed_7": sum(1 for x in cl if str(x.get("closed_at") or "")[:10] >= d7),
                "closed_30": len(cl),
                "on_time_pct": round(sum(1 for x in timed if x["on_time"]) / len(timed) * 100) if timed else None,
                "median_days_to_close": ages[len(ages) // 2] if ages else None,
                "oldest_days": max(((date.fromisoformat(today) - date.fromisoformat(x["first_seen"])).days
                                    for x in xs if x.get("first_seen")), default=None)}
    out = {"total": block(act, closed30), "departments": {}, "owners": []}
    for dept in DEPARTMENTS:
        out["departments"][dept] = block([x for x in act if x["department"] == dept],
                                         [x for x in closed30 if x["department"] == dept])
    owners = {}
    for x in items:
        keys = x["owners"] or (["(role) " + (x["role_label"] or "nobody")] if x["status"] != "Awaiting approval" else [])
        for o in keys:
            O = owners.setdefault(o, {"owner": o, "name": "", "open": 0, "overdue": 0, "critical": 0, "closed_30": 0,
                                      "on_time": 0, "timed": 0, "departments": set()})
            O["departments"].add(x["department"])
            if x["active"]:
                O["open"] += 1
                O["overdue"] += x["overdue"]
                O["critical"] += x["severity"] == "critical"
            elif x in closed30:
                O["closed_30"] += 1
                if x["on_time"] is not None:
                    O["timed"] += 1
                    O["on_time"] += bool(x["on_time"])
    U = users()
    for o in owners.values():
        o["name"] = (U.get(o["owner"]) or {}).get("name") or ""
        o["account"] = o["owner"] in U
        o["departments"] = sorted(o["departments"])
        o["on_time_pct"] = round(o["on_time"] / o["timed"] * 100) if o["timed"] else None
    out["owners"] = sorted((o for o in owners.values() if o["open"] or o["closed_30"]),
                           key=lambda o: (-o["overdue"], -o["open"], o["owner"]))
    return out


def sort_items(items):
    return sorted(items, key=lambda x: (not x["active"], -x["escalation"], not x["overdue"], RANK.get(x["severity"], 9),
                                        x.get("due") or "9999", -(abs(x["value"] or 0))))


# ---------------------------------------------------------------- changing an item
def may_change(user, x, can):
    """The owner, a role that manages the Action Center, or the tower's own action right."""
    me = (user.get("email") or "").strip().lower()
    if me and me in x["owners"]:
        return True
    if can(user, "actioncenter.manage"):
        return True
    return can(user, {"finance": "finance.actions.manage", "purchasing": "purchasing.actions.manage",
                      "unicorn": "unicorn.decisions.approve"}[x["origin"]])


def find(uid, scope=None):
    return next((x for x in collect(scope)["items"] if x["uid"] == uid), None)


def update(uid, patch, user, can):
    """status (In progress, Awaiting verification, Dismissed, Open, Closed for items by hand), comment, owner
    (an email of a pulse account), due. Goes back to the register the item came from."""
    x = find(uid, user.get("scope"))
    if not x:
        raise ValueError("This item is not in the Action Center, or not in your access.")
    if not may_change(user, x, can):
        raise PermissionError("Only its owner, a role that manages the Action Center or the tower's actions changes it.")
    if x["status"] == "Awaiting approval":
        raise ValueError("A proposal is approved or refused in Unicorn → Decisions & actions.")
    if patch.get("due") and not (can(user, "actioncenter.manage") or can(user, {"finance": "finance.actions.manage",
                                 "purchasing": "purchasing.actions.manage", "unicorn": "unicorn.decisions.approve"}[x["origin"]])):
        raise PermissionError("The deadline comes from the SLA; only a manager moves it.")
    actor = f"{user.get('name') or user['username']} <{user.get('email') or user['username']}>"
    if x["origin"] in TOWERS:
        body = {"id": x["ref"], "actor": actor,
                "patch": {k: patch[k] for k in ("status", "comment", "owner", "due") if patch.get(k) not in (None, "")}}
        try:
            r = requests.post(TOWERS[x["origin"]][1] + "/internal/register/update", timeout=30, json=body,
                              headers={"X-Service-Token": config.SERVICE_TOKEN})
            j = r.json() if r.content else {}
        except (requests.RequestException, ValueError) as exc:
            raise RuntimeError(f"{x['tower']} could not be reached: {exc}") from exc
        if not r.ok:
            raise ValueError(j.get("error") or f"{x['tower']} answered {r.status_code}.")
        with _lock:
            _cache.pop(x["origin"], None)
    else:
        st = {"Closed": "Done", "Dismissed": "Cancelled"}.get(patch.get("status"), patch.get("status"))
        if st and st not in ureg.ACTION_STATUSES:
            raise ValueError("An action is Open, In progress, Done or Cancelled.")
        U = users()
        owner = patch.get("owner")
        if owner and owner.lower() in U:
            owner = U[owner.lower()]["name"] + " <" + owner.lower() + ">"
        ureg.update_action(x["ref"], actor, st or None, patch.get("comment"), owner or None, patch.get("due") or None)
    store.audit("action center", user["username"], uid, {k: patch.get(k) for k in ("status", "owner", "due") if patch.get(k)})
    return find(uid, user.get("scope"))


def apply_sla(actor):
    out = {}
    for key, (name, base) in TOWERS.items():
        try:
            r = requests.post(base + "/internal/register/apply-sla", timeout=60, json={"actor": actor},
                              headers={"X-Service-Token": config.SERVICE_TOKEN})
            out[name] = (r.json() or {}).get("changed") if r.ok else f"answered {r.status_code}"
        except (requests.RequestException, ValueError) as exc:
            out[name] = f"not reached: {str(exc)[:80]}"
    forget()
    store.audit("action center apply sla", actor, "", out)
    return out


# ---------------------------------------------------------------- the responsibility matrix
def matrix():
    """The roles by department, the people in each role by company and location (with their pulse account), and
    which role owns which type of item in each register."""
    d = _directory()
    U = users()
    roles = [{"key": r["key"], "label": r["label"], "department": DEPARTMENT_OF_ROLE.get(r["key"], "Executive")}
             for r in d.get("roles") or []]
    people = []
    for co, row in (d.get("people") or {}).items():
        for role, text in (row or {}).items():
            for e in _emails(text):
                people.append({"company": co, "location": "", "role": role, "email": e,
                               "account": U.get(e, {}).get("name") or "", "username": U.get(e, {}).get("username") or ""})
    for co, locs in (d.get("locations") or {}).items():
        for loc, row in (locs or {}).items():
            for role, text in (row or {}).items():
                for e in _emails(text):
                    people.append({"company": co, "location": loc, "role": role, "email": e,
                                   "account": U.get(e, {}).get("name") or "", "username": U.get(e, {}).get("username") or ""})
    A = admin_roles()
    admin_dept = {r["name"]: r["ac_department"] for r in A}
    rules_set = settings()["owner_rules"]
    owner_rules = []
    for key in TOWERS:
        t = _tower(key) or {}
        name = TOWERS[key][0]
        labels = dict(t.get("roles") or {})
        counts = {}
        for x in t.get("items") or []:
            if x["status"] in ACTIVE:
                counts[(x["source"], x.get("kind") or "")] = counts.get((x["source"], x.get("kind") or ""), 0) + 1
        for rr in t.get("owner_rules") or []:
            k = f"{name}|{rr['source']}|{rr['kind']}"
            cur = rules_set.get(k) or ""
            role = cur[6:] if cur.startswith("admin:") else cur[4:] if cur.startswith("dir:") else rr["role"]
            label = cur[6:] if cur.startswith("admin:") else labels.get(role, role)
            n = sum(v for (s_, k_), v in counts.items() if s_ == rr["source"] and (rr["kind"] == "*" or k_ == rr["kind"]))
            owner_rules.append({"tower": name, "source": rr["source"], "kind": rr["kind"],
                                "source_label": (t.get("sources") or {}).get(rr["source"], rr["source"]),
                                "default_role": rr["role"], "default_label": labels.get(rr["role"], rr["role"]),
                                "owner": cur, "owner_label": label, "set": bool(cur),
                                "department": department_of(cur or rr["role"], "Finance", admin_dept), "items": n})
    return {"roles": roles, "people": sorted(people, key=lambda p: (p["company"] != "*", p["company"], p["location"], p["role"])),
            "admin_roles": [{k: r[k] for k in ("name", "department", "ac_department")}
                            | {"people": [{"name": p["name"], "email": p["email"], "access": _scope_text(p["scope"])} for p in r["people"]]}
                            for r in A],
            "owner_rules": owner_rules,
            "accounts": [{"email": e, "name": u["name"], "username": u["username"], "role": u["role"]} for e, u in sorted(U.items(), key=lambda kv: kv[1]["name"])],
            "updated_at": d.get("updated_at"), "updated_by": d.get("updated_by")}


def _scope_text(scope):
    comps = (scope or {}).get("companies", "*")
    if comps == "*":
        return "every company"
    locs = (scope or {}).get("locations") or {}
    return "; ".join(f"{c}" + (f" ({', '.join(locs[c])})" if c in locs else "") for c in comps)


def save_people(company, location, role, emails, actor):
    """Puts these emails in a role of a company (or of one of its locations) in the shared directory."""
    d = _directory()
    if not d:
        raise RuntimeError("The directory in Purchasing could not be read.")
    if role not in {r["key"] for r in d.get("roles") or []}:
        raise ValueError("Unknown role.")
    emails = [e for e in dict.fromkeys(e.strip().lower() for e in emails if "@" in e)]
    patch = {"people": json.loads(json.dumps(d.get("people") or {})), "locations": json.loads(json.dumps(d.get("locations") or {})),
             "routing": d.get("routing") or {}}
    if location:
        patch["locations"].setdefault(company, {}).setdefault(location, {})[role] = ", ".join(emails)
    else:
        patch["people"].setdefault(company, {})[role] = ", ".join(emails)
    try:
        r = requests.post(PURCHASING_URL + "/internal/recipients", timeout=20, json={"data": patch, "actor": actor},
                          headers={"X-Service-Token": config.SERVICE_TOKEN})
        j = r.json() if r.content else {}
    except (requests.RequestException, ValueError) as exc:
        raise RuntimeError(f"Purchasing could not be reached: {exc}") from exc
    if not r.ok:
        raise ValueError(j.get("error") or f"Purchasing answered {r.status_code}.")
    with _lock:
        _cache.pop("directory", None)
    store.audit("action center matrix", actor, f"{company}/{location or '*'}/{role}", {"emails": emails})
    return matrix()


# ---------------------------------------------------------------- the combined daily email
def _send_mail(to, cc, subject, body, text):
    r = requests.post(PURCHASING_URL + "/internal/mail", timeout=90,
                      headers={"X-Service-Token": config.SERVICE_TOKEN},
                      json={"to": to, "cc": cc, "subject": subject, "html": body, "text": text,
                            "source": "Executive Action Center", "attachments": [], "reply_to": ""})
    j = r.json() if r.content else {}
    if not r.ok:
        raise RuntimeError(j.get("error") or f"Purchasing answered {r.status_code}.")


def _digest_html(owner_name, items):
    e = html.escape
    navy, ink, muted, line, red, amber = "#08303f", "#0e2733", "#5c7482", "#d9dcdd", "#c0392b", "#b26a00"
    td = f"padding:6px 8px;border-bottom:1px solid #f0f1f2;font-size:12px;color:{ink};vertical-align:top"
    groups = [("Past the deadline", [x for x in items if x["overdue"]]), ("Due soon", [x for x in items if x["due_soon"]]),
              ("Open", [x for x in items if not x["overdue"] and not x["due_soon"]])]
    parts = []
    for name, xs in groups:
        if not xs:
            continue
        rows = "".join(
            f"<tr><td style='{td};white-space:nowrap;color:{red if x['overdue'] else amber if x['severity'] == 'critical' else muted}'>"
            f"{e(x['severity'].title())}{' · ' + str(x['days_overdue']) + ' d late' if x['overdue'] else ''}</td>"
            f"<td style='{td}'>{e(x['department'])}<div style='color:{muted}'>{e(x['source_label'])}</div></td>"
            f"<td style='{td}'>{e(x['company'])}{' · ' + e(x['location']) if x['location'] else ''}</td>"
            f"<td style='{td}'><b>{e(x['title'])}</b><div style='color:{muted}'>{e((x['detail'] or '')[:240])}</div></td>"
            f"<td style='{td};white-space:nowrap'>{e(x['status'])}</td><td style='{td};white-space:nowrap'>{e(x['due'] or '')}</td></tr>"
            for x in xs[:80])
        parts.append(f"<h3 style='font-size:14px;color:{ink};margin:18px 0 6px'>{e(name)} ({len(xs)})</h3>"
                     f"<table role='presentation' cellspacing='0' cellpadding='0' style='width:100%;border-collapse:collapse;border:1px solid {line}'>{rows}</table>")
    over = sum(x["overdue"] for x in items)
    link = (f"<p style='margin:18px 0 0'><a href='{e(PUBLIC_URL)}/actions#mine' style='background:#0c475e;color:#fff;"
            "text-decoration:none;padding:10px 18px;border-radius:8px;font-size:13px;font-weight:600'>Open my items</a></p>") if PUBLIC_URL else ""
    body = f"""<!doctype html><html><head><meta charset="utf-8"></head><body style="margin:0;background:#ecebeb;font-family:Figtree,Segoe UI,Arial,sans-serif">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:920px">
<tr><td style="background:{navy};color:#fff;padding:18px 22px;border-radius:12px 12px 0 0">
<div style="font-size:11px;letter-spacing:.12em;color:#a0d8ea">REVO HOSPITALITY GROUP · PULSE · EXECUTIVE ACTION CENTER</div>
<div style="font-size:20px;font-weight:700;margin-top:4px">{e(owner_name)}, your open items</div>
<div style="font-size:12px;color:#cfe3ea;margin-top:4px">{len(items)} open · {over} past the deadline · {date.today():%d %b %Y}</div></td></tr>
<tr><td style="background:#fff;padding:20px 22px;border-radius:0 0 12px 12px">
<p style="font-size:13px;color:{ink};margin:0">Every item comes from a control of pulse (Finance, Purchasing) or from a decision approved in Unicorn. Deadlines follow the SLA of your department; an item past its deadline goes to the head of the department and then to the executives.</p>
{''.join(parts)}{link}
<p style="color:{muted};font-size:11px;margin:22px 0 0;border-top:1px solid {line};padding-top:12px">Sent by pulse to the owners of open items (the responsibility matrix: Settings → Email recipients by company). Nothing was changed in Business Central or any other system.</p>
</td></tr></table></td></tr></table></body></html>"""
    text = f"Your open items: {len(items)}, {over} past the deadline.\n" + "\n".join(
        f"- [{'LATE ' + str(x['days_overdue']) + 'd' if x['overdue'] else x['status']}] {x['department']} · {x['company']} {x['location']}: {x['title']} (due {x['due']})"
        for x in items[:200])
    return body, text


def send_digests(actor="scheduler", test_to=None):
    """One email per owner of open items (every register and Unicorn); escalations in Cc: the head of the
    department after level1_days late, the executives after level2_days. test_to: one owner's email to these."""
    data = collect()
    s, U = data["settings"], users()
    esc = s["escalation"]
    by = {}
    for x in data["items"]:
        if not x["active"] or x["status"] == "Awaiting approval":
            continue
        for o in x["owners"]:
            by.setdefault(o, []).append(x)
    if test_to:
        if not by:
            raise ValueError("No open item has an owner yet: fill the responsibility matrix.")
        pick = next(iter(sorted(by, key=lambda o: -len(by[o]))))
        by = {pick: by[pick]}
    sent = []
    for who, xs in sorted(by.items()):
        xs = sort_items(xs)
        cc = []
        for x in xs:
            if x["escalation"] >= 1:
                cc += role_people(esc["heads"].get(x["department"], "coo"), x["company"])
            if x["escalation"] >= 2:
                for r in esc["executives"]:
                    cc += role_people(r, x["company"])
        cc = [c for c in dict.fromkeys(cc) if c != who]
        name = (U.get(who) or {}).get("name") or who
        body, text = _digest_html(name, xs)
        over = sum(x["overdue"] for x in xs)
        subject = f"[pulse] Your items: {len(xs)} open, {over} past the deadline"
        try:
            if test_to:
                _send_mail(test_to, [], "[TEST] " + subject + f" ({who})", body, text)
            else:
                _send_mail([who], cc, subject, body, text)
            sent.append({"owner": who, "items": len(xs), "overdue": over, "cc": [] if test_to else cc, "status": "sent"})
        except Exception as exc:                                    # noqa: BLE001
            sent.append({"owner": who, "items": len(xs), "status": "failed", "detail": str(exc)[:200]})
    with _lock:
        d = _read()
        d["last_digest"] = {"at": store.now(), "by": actor, "test": bool(test_to), "sent": sent[:200]}
        _write(d)
    store.audit("action center email" + (" (test)" if test_to else ""), actor, "", {"sent": len(sent)})
    return sent


_state = {"done": None}


def loop():
    time.sleep(int(config.get("ACTIONCENTER_START_DELAY", "240")))
    while True:
        try:
            s = settings()
            if s["digest"]["enabled"]:
                now = datetime.now()
                hh, mm = (int(v) for v in s["digest"]["time"].split(":"))
                start = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                last = (s.get("last_digest") or {})
                done_today = (last.get("at") or "")[:10] == now.date().isoformat() and not last.get("test")
                if start <= now <= start + timedelta(hours=6) and not done_today and _state["done"] != now.date():
                    _state["done"] = now.date()
                    send_digests()
        except Exception:                                           # noqa: BLE001
            traceback.print_exc()
        time.sleep(60)


def start():
    threading.Thread(target=loop, daemon=True, name="action-center").start()
