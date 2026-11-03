"""The exceptions register: every exception the controls find is kept, with an owner, a due date and a status,
until the data shows it is gone.

  * A control run (sync) hands over what it finds for a scope (a source, a company, the locations it read). A new
    finding opens an item (Open), with an owner from Email recipients (the role of its type, at its location,
    else the company) and a due date by severity. One found again keeps its history and gets its last seen,
    value and detail updated.
  * An item of that scope the run no longer finds is closed as verified: the data says it is fixed. An owner
    who marks it fixed puts it in "Awaiting verification"; the next run closes it, or opens it again with a note.
    A closed item that comes back is reopened and counted.
  * Owners move their items (In progress, Fixed – verify, Dismissed with a reason), comment and can hand an item to
    someone else; every change is in its history and in the audit trail.
  * A daily email to every owner lists their open items, overdue first; items overdue beyond the escalation days
    go to the management roles in Cc.
Kept in data/exceptions.db. Nothing is written to Business Central or Simphony.
"""
import html
import json
import sqlite3
import threading
import time
import traceback
from datetime import date, datetime, timedelta

import arap_notify
import config
import recipients
import store

DB_PATH = store.DATA_DIR / "exceptions.db"
STATUSES = ("Open", "In progress", "Awaiting verification", "Closed", "Dismissed")
ACTIVE = ("Open", "In progress", "Awaiting verification")
SEVERITY = ("critical", "warning", "info")
SLA = {"critical": 2, "warning": 5, "info": 10}
# source → label, and type → the role that owns it (Email recipients roles)
SOURCES = {"negative_stock": "Negative stock", "discharge": "Received vs discharged", "sales_rec": "Simphony ↔ BC",
           "data_quality": "Data quality"}
OWNER_ROLE = {
    ("negative_stock", "stock"): "stock_manager", ("negative_stock", "cost"): "cost_controller",
    ("negative_stock", "purchasing"): "purchasing_manager",
    ("discharge", "*"): "cost_controller",
    # the sales reconciliation: import and posting of the POS sales to the chief accountant, the takings to finance,
    # the menu item mapping (BC item, recipe) to the cost controller
    ("sales_rec", "not_imported"): "accounting", ("sales_rec", "import_diff"): "accounting",
    ("sales_rec", "not_posted"): "accounting", ("sales_rec", "invoice_diff"): "accounting",
    ("sales_rec", "unpaid"): "finance", ("sales_rec", "*"): "cost_controller",
    # Data quality & reconciliation: the checks of a closed month and the postings after its freeze
    ("data_quality", "*"): "accounting",
}
ROLE_LABEL = {"stock_manager": "Stock manager", "cost_controller": "Cost controller",
              "purchasing_manager": "Purchasing manager", "finance": "Finance", "accounting": "Chief accountant"}
_lock = threading.Lock()
_conn = None


def _db():
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript("""
            CREATE TABLE IF NOT EXISTS ex (
                id TEXT PRIMARY KEY, source TEXT, kind TEXT, company TEXT, location TEXT, subject TEXT, title TEXT,
                detail TEXT, severity TEXT, value REAL, role TEXT, owner TEXT, owner_set INTEGER DEFAULT 0,
                status TEXT, first_seen TEXT, last_seen TEXT, due TEXT, closed_at TEXT, closed_by TEXT, note TEXT,
                reopened INTEGER DEFAULT 0, history TEXT);
            CREATE INDEX IF NOT EXISTS ex_scope ON ex (source, company, status);
            CREATE TABLE IF NOT EXISTS runs (source TEXT, company TEXT, at TEXT, found INTEGER, opened INTEGER,
                                             closed INTEGER, reopened INTEGER);
        """)
    return _conn


def _now():
    return datetime.now().isoformat(timespec="seconds")


def owner_of(source, kind, company, location):
    """(role, "email, email") of a finding: the role of its type at its location in Email recipients, else at the
    company (the company row and the group row)."""
    role = OWNER_ROLE.get((source, kind)) or OWNER_ROLE.get((source, "*")) or "cost_controller"
    d = recipients.directory() or {}
    emails = []
    locs = next((v for k, v in (d.get("locations") or {}).items() if str(k).upper() == str(company or "").upper()), {})
    row = next((v for k, v in locs.items() if str(k).upper() == str(location or "").upper()), {}) if location else {}
    emails = recipients.parse(row.get(role, ""))[0]
    if not emails:
        people = {str(k).strip().upper(): v for k, v in (d.get("people") or {}).items()}
        for c in (company, recipients.GROUP):
            emails += recipients.parse((people.get(str(c or "").strip().upper()) or {}).get(role, ""))[0]
    return role, ", ".join(dict.fromkeys(emails))


def _row(r):
    keys = ("id", "source", "kind", "company", "location", "subject", "title", "detail", "severity", "value", "role",
            "owner", "owner_set", "status", "first_seen", "last_seen", "due", "closed_at", "closed_by", "note",
            "reopened", "history")
    x = dict(zip(keys, r))
    x["history"] = json.loads(x["history"] or "[]")
    return x


def sync(source, company, findings, locations=None, actor="control run", in_scope=None):
    """findings: [{"subject", "kind", "title", "detail", "severity", "value", "location"}] of one run. locations: the
    locations the run read (None = the whole company); in_scope(item) → bool: the items the run covered (its days);
    only items of that scope are closed when not found."""
    now, today = _now(), date.today()
    opened = closed = reopened = 0
    seen, best = set(), {}
    for f in findings:                                             # one finding per item and location: the largest
        k = (f.get("location") or "", str(f["subject"]))
        if k not in best or abs(float(f.get("value") or 0)) > abs(float(best[k].get("value") or 0)):
            best[k] = f
    findings = list(best.values())
    with _lock:
        db = _db()
        for f in findings:
            loc = f.get("location") or ""
            eid = f"{source}:{company}:{loc}:{f['subject']}"
            seen.add(eid)
            sev = f.get("severity") if f.get("severity") in SEVERITY else "warning"
            old = db.execute("SELECT * FROM ex WHERE id=?", (eid,)).fetchone()
            if old is None:
                role, owner = owner_of(source, f.get("kind") or "", company, loc)
                hist = [{"ts": now, "by": actor, "to": "Open", "note": "Found"}]
                db.execute("INSERT INTO ex VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,'Open',?,?,?,NULL,NULL,'',0,?)",
                           (eid, source, f.get("kind") or "", company, loc, str(f["subject"]), f.get("title") or "",
                            f.get("detail") or "", sev, float(f.get("value") or 0), role, owner, now, now,
                            (today + timedelta(days=SLA[sev])).isoformat(), json.dumps(hist)))
                opened += 1
                continue
            x = _row(old)
            hist = x["history"]
            status, due, rp = x["status"], x["due"], x["reopened"]
            if status == "Closed":
                status, rp, due = "Open", rp + 1, (today + timedelta(days=SLA[sev])).isoformat()
                hist.append({"ts": now, "by": actor, "to": "Open", "note": "Found again after it was closed"})
                reopened += 1
            elif status == "Awaiting verification":
                status = "Open"
                hist.append({"ts": now, "by": actor, "to": "Open", "note": "Still found after it was marked fixed"})
            if status != x["status"] and x["status"] == "Closed":
                db.execute("UPDATE ex SET closed_at=NULL, closed_by=NULL, note='' WHERE id=?", (eid,))
            role, owner = x["role"], x["owner"]
            if not x["owner_set"]:
                role, owner = owner_of(source, f.get("kind") or x["kind"], company, loc)
            db.execute("UPDATE ex SET kind=?, title=?, detail=?, severity=?, value=?, role=?, owner=?, status=?, "
                       "last_seen=?, due=?, reopened=?, history=? WHERE id=?",
                       (f.get("kind") or x["kind"], f.get("title") or x["title"], f.get("detail") or "", sev,
                        float(f.get("value") or 0), role, owner, status, now, due, rp, json.dumps(hist[-60:]), eid))
        q = "SELECT * FROM ex WHERE source=? AND company=? AND status IN ('Open','In progress','Awaiting verification')"
        for r in db.execute(q, (source, company)).fetchall():
            x = _row(r)
            if x["id"] in seen or (locations is not None and x["location"] not in locations) \
                    or (in_scope is not None and not in_scope(x)):
                continue
            x["history"].append({"ts": now, "by": actor, "to": "Closed", "note": "Verified: no longer found in the data"})
            db.execute("UPDATE ex SET status='Closed', closed_at=?, closed_by=?, note=?, history=? WHERE id=?",
                       (now, actor, "Verified by the data", json.dumps(x["history"][-60:]), x["id"]))
            closed += 1
        db.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?)", (source, company, now, len(findings), opened, closed, reopened))
        db.execute("DELETE FROM runs WHERE rowid NOT IN (SELECT rowid FROM runs ORDER BY rowid DESC LIMIT 500)")
        db.commit()
    return {"found": len(findings), "opened": opened, "closed": closed, "reopened": reopened}


def get(eid):
    with _lock:
        r = _db().execute("SELECT * FROM ex WHERE id=?", (eid,)).fetchone()
    return _row(r) if r else None


def update(eid, patch, actor):
    """An owner's change: status (In progress, Awaiting verification, Dismissed, Open), owner, comment."""
    now = _now()
    with _lock:
        db = _db()
        r = db.execute("SELECT * FROM ex WHERE id=?", (eid,)).fetchone()
        if r is None:
            raise KeyError("This exception is no longer in the register.")
        x = _row(r)
        comment = str(patch.get("comment") or "").strip()[:1000]
        status = patch.get("status")
        sets = {}
        if status and status != x["status"]:
            if status not in ("Open", "In progress", "Awaiting verification", "Dismissed"):
                raise ValueError("A status can be Open, In progress, Awaiting verification or Dismissed; the data closes an item.")
            if status == "Dismissed" and not comment:
                raise ValueError("Say why it is dismissed.")
            if x["status"] in ("Closed",) and status != "Open":
                raise ValueError("A closed item can only be opened again.")
            sets["status"] = status
            if status == "Dismissed":
                sets.update(closed_at=now, closed_by=actor, note=comment)
            elif x["status"] in ("Closed", "Dismissed"):
                sev = x["severity"] if x["severity"] in SLA else "warning"
                sets.update(closed_at=None, closed_by=None, note="",
                            due=(date.today() + timedelta(days=SLA[sev])).isoformat())
            x["history"].append({"ts": now, "by": actor, "to": status, "note": comment})
        elif comment:
            x["history"].append({"ts": now, "by": actor, "to": x["status"], "note": comment})
        if "owner" in patch and str(patch["owner"]).strip() != x["owner"]:
            good, bad = recipients.parse(str(patch["owner"]))
            if bad:
                raise ValueError("Not an email address: " + ", ".join(bad))
            sets.update(owner=", ".join(good), owner_set=1 if good else 0)
            x["history"].append({"ts": now, "by": actor, "to": x["status"], "note": f"Owner: {', '.join(good) or 'back to the role'}"})
            if not good:
                sets["owner"] = owner_of(x["source"], x["kind"], x["company"], x["location"])[1]
        if not sets and not comment:
            return x
        sets["history"] = json.dumps(x["history"][-60:])
        db.execute(f"UPDATE ex SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), eid))
        db.commit()
        r = db.execute("SELECT * FROM ex WHERE id=?", (eid,)).fetchone()
    store.audit("exception", eid, actor, {"status": x["status"]}, {k: v for k, v in sets.items() if k != "history"}, comment)
    return _row(r)


def counts_by_location(company):
    """{LOCATION: {"open", "critical", "overdue", "value"}} of the active items of a company (location upper case)."""
    today = date.today().isoformat()
    with _lock:
        rows = _db().execute("SELECT location, severity, due, value FROM ex WHERE UPPER(company)=? AND status IN (?,?,?)",
                             (str(company or "").upper(), *ACTIVE)).fetchall()
    out = {}
    for loc, sev, due, value in rows:
        o = out.setdefault(str(loc or "").upper(), {"open": 0, "critical": 0, "overdue": 0, "value": 0.0})
        o["open"] += 1
        o["critical"] += sev == "critical"
        o["overdue"] += bool(due) and due < today
        o["value"] += abs(value or 0)
    return out


def query(status="active", owner="", source="", company="", location="", q="", allow=None, limit=500):
    """The items of a filter, the counts and the owners' table. allow(company, location) → bool: what a person
    limited to some companies or locations may see; None = everything."""
    today = date.today().isoformat()
    with _lock:
        rows = [_row(r) for r in _db().execute("SELECT * FROM ex").fetchall()]
        runs = _db().execute("SELECT source, company, MAX(at) FROM runs GROUP BY source, company").fetchall()
    if allow is not None:
        rows = [x for x in rows if allow(x["company"], x["location"])]
    for x in rows:
        x["overdue"] = x["status"] in ACTIVE and bool(x["due"]) and x["due"] < today
        end = (x["closed_at"] or "")[:10] if x["status"] in ("Closed", "Dismissed") else today
        x["age"] = (date.fromisoformat(end or today) - date.fromisoformat(x["first_seen"][:10])).days
    week = (date.today() - timedelta(days=7)).isoformat()
    active = [x for x in rows if x["status"] in ACTIVE]
    closed7 = [x for x in rows if x["status"] == "Closed" and (x["closed_at"] or "") >= week]
    ages = sorted(x["age"] for x in rows if x["status"] == "Closed" and x["closed_at"])
    kpis = {"open": len(active), "overdue": sum(1 for x in active if x["overdue"]),
            "new_today": sum(1 for x in rows if x["first_seen"][:10] == today),
            "awaiting": sum(1 for x in active if x["status"] == "Awaiting verification"),
            "closed_7": len(closed7), "reopened": sum(1 for x in active if x["reopened"]),
            "median_days": ages[len(ages) // 2] if ages else None,
            "value_open": round(sum(abs(x["value"]) for x in active), 0)}
    owners = {}
    for x in rows:
        for o in (x["owner"] or "(nobody)").split(", "):
            O = owners.setdefault(o, {"owner": o, "open": 0, "overdue": 0, "oldest": 0, "closed_7": 0, "value": 0.0})
            if x["status"] in ACTIVE:
                O["open"] += 1
                O["overdue"] += x["overdue"]
                O["oldest"] = max(O["oldest"], x["age"])
                O["value"] += abs(x["value"])
            elif x["status"] == "Closed" and (x["closed_at"] or "") >= week:
                O["closed_7"] += 1
    sel = rows
    if status == "active":
        sel = [x for x in sel if x["status"] in ACTIVE]
    elif status == "overdue":
        sel = [x for x in sel if x["overdue"]]
    elif status in STATUSES:
        sel = [x for x in sel if x["status"] == status]
    if owner:
        sel = [x for x in sel if owner.lower() in (x["owner"] or "(nobody)").lower()]
    if source:
        sel = [x for x in sel if x["source"] == source]
    if company:
        sel = [x for x in sel if x["company"] == company]
    if location:
        sel = [x for x in sel if x["location"] == location]
    if q:
        ql = q.lower()
        sel = [x for x in sel if ql in (x["title"] + " " + x["subject"] + " " + x["detail"]).lower()]
    rank = {"critical": 0, "warning": 1, "info": 2}
    sel.sort(key=lambda x: (not x["overdue"], x["status"] not in ACTIVE, rank.get(x["severity"], 3), -abs(x["value"])))
    return {"kpis": kpis, "owners": sorted((o for o in owners.values() if o["open"] or o["closed_7"]),
                                           key=lambda o: (-o["overdue"], -o["open"])),
            "items": sel[:limit], "total": len(sel), "statuses": STATUSES, "sources": SOURCES,
            "roles": ROLE_LABEL, "sla": SLA,
            "companies": sorted({x["company"] for x in rows}), "locations": sorted({x["location"] for x in rows if x["location"]}),
            "runs": [{"source": s, "company": c, "at": a} for s, c, a in runs]}


# ---------------------------------------------------------------- findings of the controls
def from_negative(r):
    """Findings of a negative stock report (negstock.analyse)."""
    out = []
    for x in r["lines"]:
        out.append({"subject": x["item"], "kind": x["owner"], "location": x["location"],
                    "title": f"{x['desc'] or x['item']} below zero: {x['qty']:g} {x['unit']}",
                    "detail": f"{x['cause']}. {x['why']}", "severity": "critical" if (x["days"] or 99) > 3 else "warning",
                    "value": abs(x["value"])})
    return out


def from_discharge(du):
    """Findings of Received vs discharged (recipeuse.analyse): its interventions."""
    labels = {"not_received": "Discharged, not received", "not_used": "Received, not discharged",
              "over": "Discharged far more than received", "under": "Discharged far less than received"}
    out = []
    for a in du["interventions"]:
        out.append({"subject": a["item"], "kind": a["status"], "location": a["location"],
                    "title": f"{a['desc'] or a['item']}: {labels.get(a['status'], a['status']).lower()}"
                             + (f" (↔ {a['pair']})" if a.get("pair") else ""),
                    "detail": a["action"], "severity": "critical" if a["status"] in ("not_received", "over") else "warning",
                    "value": abs(a["value"] or 0)})
    return out


# ---------------------------------------------------------------- the owners' daily email
def _digest(owner, items, public_url):
    e = html.escape
    navy, ink, muted, line, red, amber = "#08303f", "#0e2733", "#5c7482", "#d9dcdd", "#c0392b", "#b26a00"
    td = f"padding:6px 8px;border-bottom:1px solid #f0f1f2;font-size:12px;color:{ink};vertical-align:top"
    over = [x for x in items if x["overdue"]]
    rows = "".join(
        f"<tr><td style='{td};color:{red if x['overdue'] else amber if x['severity'] == 'critical' else muted};white-space:nowrap'>"
        f"{'Overdue' if x['overdue'] else x['severity'].title()}</td><td style='{td}'>{e(x['company'])} · {e(x['location'])}</td>"
        f"<td style='{td}'><b>{e(x['title'])}</b><div style='color:{muted}'>{e(x['detail'][:300])}</div></td>"
        f"<td style='{td};white-space:nowrap'>{e(x['status'])}</td><td style='{td};white-space:nowrap'>{e(x['due'] or '')}</td>"
        f"<td style='{td};text-align:right'>{x['age']} d</td></tr>" for x in items[:150])
    link = (f"<p style='margin:18px 0 0'><a href='{e(public_url)}/finance/#register' style='background:#0c475e;color:#fff;"
            f"text-decoration:none;padding:10px 18px;border-radius:8px;font-size:13px;font-weight:600'>Open the register</a></p>") if public_url else ""
    body = f"""<!doctype html><html><head><meta charset="utf-8"></head><body style="margin:0;background:#ecebeb;font-family:Figtree,Segoe UI,Arial,sans-serif">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:900px">
<tr><td style="background:{navy};color:#fff;padding:18px 22px;border-radius:12px 12px 0 0">
<div style="font-size:11px;letter-spacing:.12em;color:#a0d8ea">REVO HOSPITALITY GROUP · PULSE</div>
<div style="font-size:20px;font-weight:700;margin-top:4px">Your open exceptions</div>
<div style="font-size:12px;color:#cfe3ea;margin-top:4px">{len(items)} open · {len(over)} overdue · {date.today():%d %b %Y}</div></td></tr>
<tr><td style="background:#fff;padding:20px 22px;border-radius:0 0 12px 12px">
<p style="font-size:13px;color:{ink};margin:0 0 12px">Each item closes by itself when Business Central or Simphony no longer show it. Mark it in progress, or fixed so the next run can verify it, from the register.</p>
<table role="presentation" cellspacing="0" cellpadding="0" style="width:100%;border-collapse:collapse;border:1px solid {line}">
<tr>{''.join(f"<th style='text-align:left;padding:6px 8px;background:#e4f3f9;font-size:11.5px'>{h}</th>" for h in ('', 'Where', 'Exception', 'Status', 'Due', 'Age'))}</tr>{rows}</table>
{f"<p style='font-size:12px;color:{muted}'>{len(items) - 150} more in the register.</p>" if len(items) > 150 else ''}{link}
<p style="color:{muted};font-size:11px;margin:22px 0 0;border-top:1px solid {line};padding-top:12px">Sent by Pulse RHG to the owners of open exceptions (Settings → Email recipients by company). Nothing was changed in Business Central or Simphony.</p>
</td></tr></table></td></tr></table></body></html>"""
    text = f"Your open exceptions: {len(items)}, {len(over)} overdue.\n" + "\n".join(
        f"- [{'OVERDUE' if x['overdue'] else x['status']}] {x['company']} {x['location']}: {x['title']} (due {x['due']})" for x in items[:150])
    return body, text


def settings():
    s = store.get_settings().get("exception_register") or {}
    try:
        esc = max(0, int(float(s.get("escalate_days") or 3)))
    except ValueError:
        esc = 3
    return {"controls": s.get("controls", True) not in (False, "false", "0", ""), "enabled": bool(s.get("enabled")),
            "time": s.get("time") or "08:15", "escalate_days": esc, "escalate_roles": s.get("escalate_roles") or "coo, cfo"}


def synced_today(source, company):
    with _lock:
        r = _db().execute("SELECT MAX(at) FROM runs WHERE source=? AND company=?", (source, company)).fetchone()
    return bool(r and r[0] and r[0][:10] == date.today().isoformat())


def recent_runs(limit=20):
    with _lock:
        rows = _db().execute("SELECT * FROM runs ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()
    return [dict(zip(("source", "company", "at", "found", "opened", "closed", "reopened"), r)) for r in rows]


def send_digests(test_to=None, owner=None, actor="scheduler"):
    """One email per owner of active items; escalation: items overdue beyond escalate_days put the management roles
    of their company in Cc. test_to: send only one owner's email (owner, else the first) to these addresses."""
    s = settings()
    data = query("active", limit=100000)
    by = {}
    for x in data["items"]:
        for o in (x["owner"] or "").split(", "):
            if o:
                by.setdefault(o, []).append(x)
    if test_to:
        pick = owner if owner in by else next(iter(sorted(by)), None)
        if pick is None:
            raise ValueError("No open item has an owner yet: set the roles in Settings → Email recipients by company.")
        by = {pick: by[pick]}
    public = (config.PUBLIC_URL or "").rstrip("/")
    d = recipients.directory() or {}
    people = {str(k).strip().upper(): v for k, v in (d.get("people") or {}).items()}
    roles = [r.strip() for r in s["escalate_roles"].replace(";", ",").split(",") if r.strip()]
    limit = (date.today() - timedelta(days=s["escalate_days"])).isoformat()
    sent = []
    for who, items in sorted(by.items()):
        cc = []
        for x in items:
            if x["overdue"] and x["due"] < limit:
                for c in (x["company"], recipients.GROUP):
                    for r in roles:
                        cc += recipients.parse((people.get(str(c).upper()) or {}).get(r, ""))[0]
        cc = [c for c in dict.fromkeys(cc) if c != who]
        body, text = _digest(who, items, public)
        over = sum(1 for x in items if x["overdue"])
        subject = f"[Pulse RHG] Your exceptions: {len(items)} open, {over} overdue"
        try:
            if test_to:
                arap_notify.send_mail(test_to, [], "[TEST] " + subject + f" ({who})", body, text)
            else:
                arap_notify.send_mail([who], cc, subject, body, text)
            sent.append({"to": ", ".join(test_to) if test_to else who, "owner": who, "items": len(items),
                         "overdue": over, "cc": [] if test_to else cc, "status": "sent"})
        except Exception as exc:                                   # noqa: BLE001
            sent.append({"to": who, "owner": who, "items": len(items), "status": "failed", "detail": str(exc)[:200]})
    store.audit("email", "exception register digests" + (" (test)" if test_to else ""), actor, {}, {"sent": len(sent)}, "")
    return sent


_state = {"done": None, "controls": None, "job": None}
_job_lock = threading.Lock()


def run_controls(actor="scheduler", force=False):
    """The controls of the day, through the function the server gave (it reads Business Central and syncs); a
    company already synced today by the negative stock email is not read again unless forced."""
    fn = _state["controls"]
    if fn is None:
        return []
    return fn(actor, force)


def start_job(actor):
    """Run the controls now (and nothing else), in the background; one at a time."""
    with _job_lock:
        j = _state["job"]
        if j and j["state"] == "running":
            return j
        j = _state["job"] = {"state": "running", "started": _now(), "by": actor, "result": None}

    def go():
        try:
            j["result"] = run_controls(actor, force=True)
            j["state"] = "done"
        except Exception as exc:                                   # noqa: BLE001
            traceback.print_exc()
            j.update(state="failed", error=str(exc)[:300])
        j["ended"] = _now()
    threading.Thread(target=go, daemon=True, name="exception-controls").start()
    return j


def job():
    return _state["job"]


def loop():
    while True:
        try:
            s = settings()
            now = datetime.now()
            hh, _, mm = s["time"].partition(":")
            start = now.replace(hour=int(hh or 8), minute=int(mm or 0), second=0, microsecond=0)
            if (s["controls"] or s["enabled"]) and start <= now <= start + timedelta(hours=8) \
                    and _state["done"] != now.date():
                _state["done"] = now.date()
                if s["controls"]:
                    run_controls()
                if s["enabled"]:
                    send_digests()
        except Exception:                                          # noqa: BLE001
            traceback.print_exc()
        time.sleep(60)


def start(controls=None):
    """controls(actor, force) → [results]: the server's daily control run."""
    _state["controls"] = controls
    threading.Thread(target=loop, daemon=True, name="exception-register").start()
