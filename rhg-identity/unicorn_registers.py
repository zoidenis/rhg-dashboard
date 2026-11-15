"""Unicorn's registers in pulse (Layer 2 of the Unicorn 2.0 architecture): findings, decisions, the action
tracker, the rules library and the trail of every tool Unicorn used.

What is kept here is pulse's own record: nothing is written to Business Central, Simphony, SevenRooms, Billy, the
Tax Administration, Odoo or Crunchtime.

  * Findings: a problem a detector found (or a person or Unicorn raised), with its rule, restaurant, period,
    severity, the measured value against its limit, the estimated impact and the evidence.
    Status: New → Investigating → Proposed → Closed, or Dismissed (with a reason). A finding seen again on a later
    day is the same finding (same rule, company, location and period) and only its last-seen date moves.
  * Decisions: what Unicorn proposes for a finding (the decision, why, the expected impact and how it is computed,
    alternatives, risks, confidence, the role that should approve). A person approves it (owner, deadline, target)
    or refuses it with a reason. Unicorn never approves.
  * Actions: the corrective measures of an approved decision: owner, deadline, the KPI that should move, its value
    when approved (baseline), the target, the latest value Unicorn measured and the effect against the baseline.
  * Rules: thresholds, policies, approved decisions and business knowledge, versioned. A rule changes only by a
    person holding unicorn.rules.manage; Unicorn may only propose a new version, which waits for approval. The
    detectors use the latest approved version, else the proposed default (and say so on the finding).
  * Tool calls: every read Unicorn made, with its arguments, for the audit trail.
"""
import json
import re
from datetime import date, datetime, timedelta

import store

SCHEMA = """
CREATE TABLE IF NOT EXISTS unicorn_findings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT UNIQUE NOT NULL,           -- rule|company|location|period: the same problem seen again is one finding
  rule TEXT NOT NULL, capability TEXT NOT NULL,
  company TEXT, location TEXT, location_name TEXT, period TEXT,
  title TEXT NOT NULL, detail TEXT,
  metric TEXT, value REAL, limit_value REAL, unit TEXT,
  impact REAL, currency TEXT, severity TEXT,
  status TEXT NOT NULL DEFAULT 'New',
  evidence TEXT,                      -- JSON: the figures the detector read, and the rule version
  first_seen TEXT, last_seen TEXT, seen_count INTEGER DEFAULT 1,
  decision_id INTEGER, note TEXT, updated_at TEXT, updated_by TEXT
);
CREATE INDEX IF NOT EXISTS ix_uf_status ON unicorn_findings(status, last_seen);
CREATE TABLE IF NOT EXISTS unicorn_decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  finding_id INTEGER, title TEXT NOT NULL, rationale TEXT, hypothesis TEXT, confidence TEXT,
  evidence TEXT,                      -- JSON list of {fact, source}
  impact REAL, currency TEXT, impact_formula TEXT, alternatives TEXT, risks TEXT,
  approver_role TEXT, due_days INTEGER, kpi TEXT, target TEXT,
  status TEXT NOT NULL DEFAULT 'Proposed',
  proposed_at TEXT, proposed_by TEXT, decided_at TEXT, decided_by TEXT, decision_note TEXT,
  owner TEXT, due TEXT, meta TEXT
);
CREATE TABLE IF NOT EXISTS unicorn_actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  decision_id INTEGER, title TEXT NOT NULL, owner TEXT, due TEXT,
  status TEXT NOT NULL DEFAULT 'Open',
  rule TEXT, company TEXT, location TEXT, metric TEXT, unit TEXT,
  baseline REAL, target REAL, latest REAL, latest_at TEXT, effect REAL,
  notes TEXT,                         -- JSON list of {at, by, text}
  created_at TEXT, created_by TEXT, closed_at TEXT, closed_by TEXT
);
CREATE TABLE IF NOT EXISTS unicorn_rules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT NOT NULL, version INTEGER NOT NULL,
  kind TEXT NOT NULL,                 -- threshold, policy, decision, knowledge
  title TEXT NOT NULL, text TEXT, value TEXT, applies TEXT,
  status TEXT NOT NULL,               -- Proposed, Approved, Retired
  created_at TEXT, created_by TEXT, approved_at TEXT, approved_by TEXT, reason TEXT,
  UNIQUE(key, version)
);
CREATE TABLE IF NOT EXISTS unicorn_tool_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT, context TEXT, actor TEXT, tool TEXT, args TEXT, ok INTEGER, ms INTEGER, summary TEXT
);
CREATE INDEX IF NOT EXISTS ix_utc_ctx ON unicorn_tool_calls(context);
"""

FINDING_STATUSES = ("New", "Investigating", "Proposed", "Closed", "Dismissed")
DECISION_STATUSES = ("Proposed", "Approved", "Refused", "Done")
ACTION_STATUSES = ("Open", "In progress", "Done", "Cancelled")
RULE_KINDS = ("threshold", "policy", "decision", "knowledge")
SEVERITIES = ("critical", "high", "medium", "low")
CAPABILITIES = {
    "financial": "Financial Intelligence",
    "purchasing": "Purchasing & Cost Intelligence",
    "inventory": "Inventory & Recipe Intelligence",
    "restaurant": "Restaurant Performance Intelligence",
    "executive": "Executive Decision Intelligence",
    "operations": "Operations & Food Safety Intelligence",
    "people": "People & Labour Intelligence",
    "data": "Data Integrity & Control",
}

# The first thresholds: proposals for RHG to approve (CFO / COO), not RHG policy until approved.
DEFAULT_RULES = [
    ("sales.drop_vs_last_year", "threshold", "Shitjet e një dite kundër të njëjtës ditë të javës vitin e kaluar",
     "Gjetje kur shitjet neto (pa TVSH) të një restoranti bien më shumë se pct% kundër të njëjtës ditë të javës "
     "52 javë më parë.", {"pct": -15}),
    ("sales.mtd_vs_budget", "threshold", "Shitjet e muajit deri sot kundër buxhetit",
     "Gjetje kur shitjet e muajit deri sot janë më shumë se pct% nën buxhetin e të njëjtave ditë.", {"pct": -10}),
    ("sales.discounts_pct", "threshold", "Zbritjet si pjesë e shitjeve bruto",
     "Gjetje kur zbritjet e një dite kalojnë max% të shitjeve bruto të restorantit.", {"max": 8}),
    ("sales.voids_pct", "threshold", "Void-et si pjesë e shitjeve bruto",
     "Gjetje kur void-et (punonjës + menaxher) e një dite kalojnë max% të shitjeve bruto.", {"max": 2}),
    ("pl.food_vs_budget", "threshold", "Food cost % kundër buxhetit",
     "Gjetje kur food cost % i një departamenti në muajin e fundit të mbyllur kalon buxhetin me më shumë se pts pikë.",
     {"pts": 2}),
    ("pl.labor_vs_budget", "threshold", "Labour cost % kundër buxhetit",
     "Gjetje kur labour cost % i një departamenti në muajin e fundit të mbyllur kalon buxhetin me më shumë se pts pikë.",
     {"pts": 2}),
    ("pl.ebitda_vs_budget", "threshold", "EBITDA kundër buxhetit",
     "Gjetje kur EBITDA e një departamenti në muajin e fundit të mbyllur është më shumë se pct% nën buxhet.",
     {"pct": -15}),
    ("apar.payables_beyond_critical", "threshold", "Detyrime përtej afatit kritik",
     "Gjetje kur një kompani ka detyrime ndaj furnitorëve përtej afatit kritik (Kartela) mbi min.", {"min": 0}),
    ("apar.receivables_beyond_critical", "threshold", "Të arkëtueshme përtej afatit kritik",
     "Gjetje kur një kompani ka të arkëtueshme përtej afatit kritik mbi min.", {"min": 0}),
    ("purchasing.price_mover", "threshold", "Rritje çmimi blerjeje",
     "Gjetje kur kostoja mesatare e një artikulli rritet të paktën pct% dhe ndikimi kalon min_impact në monedhën "
     "e kompanisë.", {"pct": 10, "min_impact": 50000}),
    ("purchasing.invoices_without_po", "threshold", "Fatura pa PO",
     "Gjetje kur faturat e postuara pa urdhër blerjeje kalojnë max% të faturave të një dite.", {"max": 5}),
    ("agent.investigate_per_day", "threshold", "Sa gjetje heton Unicorn çdo ditë",
     "Unicorn heton me mjete dhe propozon vendim për n gjetjet e reja me ndikimin më të madh çdo mëngjes.", {"n": 3}),
    ("agent.max_tool_calls", "threshold", "Kufiri i mjeteve për një hetim",
     "Numri maksimal i leximeve që Unicorn bën për një hetim ose një pyetje; kur mbaron, ndalon dhe e thotë.",
     {"n": 12}),
]


def init():
    with store._lock:
        conn = store.connect()
        conn.executescript(SCHEMA)
        conn.commit()
    have = {r["key"] for r in store.q("SELECT DISTINCT key FROM unicorn_rules")}
    for key, kind, title, text, value in DEFAULT_RULES:
        if key not in have:
            store.run("""INSERT INTO unicorn_rules(key, version, kind, title, text, value, applies, status, created_at,
                         created_by, reason) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                      (key, 1, kind, title, text, json.dumps(value), "all", "Proposed", store.now(),
                       "pulse (default)", "Prag i propozuar nga pulse; e miraton CFO ose COO."))


def _j(v, default=None):
    try:
        return json.loads(v) if v else default
    except ValueError:
        return default


# ---------------------------------------------------------------- rules
def _rule_row(r):
    return {"id": r["id"], "key": r["key"], "version": r["version"], "kind": r["kind"], "title": r["title"],
            "text": r["text"] or "", "value": _j(r["value"], {}), "applies": r["applies"] or "all",
            "status": r["status"], "created_at": r["created_at"], "created_by": r["created_by"],
            "approved_at": r["approved_at"], "approved_by": r["approved_by"], "reason": r["reason"] or ""}


def rules(with_history=False):
    """The rules in force: per key the latest approved version, else the latest proposed one (with any newer
    proposal waiting); with_history gives every version."""
    rows = [_rule_row(r) for r in store.q("SELECT * FROM unicorn_rules ORDER BY key, version")]
    if with_history:
        return rows
    by = {}
    for r in rows:
        by.setdefault(r["key"], []).append(r)
    res = []
    for key, vs in by.items():
        appr = [v for v in vs if v["status"] == "Approved"]
        prop = [v for v in vs if v["status"] == "Proposed"]
        cur = appr[-1] if appr else (prop[-1] if prop else None)
        if cur is None:
            continue                                    # every version retired
        pend = prop[-1] if appr and prop and prop[-1]["version"] > appr[-1]["version"] else None
        res.append(dict(cur, pending=pend, versions=len(vs)))
    return sorted(res, key=lambda r: (r["kind"], r["key"]))


def rule_value(key, default=None):
    """(value dict, status, version) of the rule in force; default when there is none."""
    for r in rules():
        if r["key"] == key:
            return r["value"], r["status"], r["version"]
    return (default or {}), "Missing", 0


def save_rule(patch, actor, approve=False):
    """A new version of a rule (or a new rule). approve=True (a person with unicorn.rules.manage) puts it in force;
    else it waits as a proposal (Unicorn's own proposals)."""
    key = str(patch.get("key") or "").strip()
    if not re.fullmatch(r"[a-z][a-z0-9_]*(\.[a-z0-9_]+)*", key) or len(key) > 80:
        raise ValueError("The key is lower case words with dots, e.g. sales.discounts_pct or policy.po_required.")
    kind = patch.get("kind") or "threshold"
    if kind not in RULE_KINDS:
        raise ValueError("Kind: threshold, policy, decision or knowledge.")
    title = str(patch.get("title") or "").strip()[:200]
    if not title:
        raise ValueError("Write a title.")
    value = patch.get("value") or {}
    if isinstance(value, str):
        try:
            value = json.loads(value) if value.strip() else {}
        except ValueError as exc:
            raise ValueError("The value must be JSON, e.g. {\"max\": 8}.") from exc
    if not isinstance(value, dict):
        raise ValueError("The value must be an object, e.g. {\"max\": 8}.")
    for k, v in value.items():
        if not isinstance(v, (int, float, str, bool)) or isinstance(v, str) and len(v) > 200:
            raise ValueError(f"{k}: a number, a word or yes/no.")
    last = store.one("SELECT MAX(version) AS v FROM unicorn_rules WHERE key=?", (key,))
    ver = ((last["v"] if last else 0) or 0) + 1
    status = "Approved" if approve else "Proposed"
    now = store.now()
    store.run("""INSERT INTO unicorn_rules(key, version, kind, title, text, value, applies, status, created_at,
                 created_by, approved_at, approved_by, reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (key, ver, kind, title, str(patch.get("text") or "")[:4000], json.dumps(value),
               str(patch.get("applies") or "all")[:200], status, now, actor, now if approve else None,
               actor if approve else None, str(patch.get("reason") or "")[:1000]))
    store.audit("unicorn rule", actor, key, {"version": ver, "status": status, "value": value})
    return {"key": key, "version": ver, "status": status}


def decide_rule(rule_id, approve, actor, reason=""):
    r = store.one("SELECT * FROM unicorn_rules WHERE id=?", (int(rule_id),))
    if not r:
        raise ValueError("No such rule version.")
    if r["status"] != "Proposed":
        raise ValueError("Only a proposed version can be approved or set aside.")
    store.run("UPDATE unicorn_rules SET status=?, approved_at=?, approved_by=?, reason=? WHERE id=?",
              ("Approved" if approve else "Retired", store.now(), actor, (reason or r["reason"] or "")[:1000], r["id"]))
    store.audit("unicorn rule " + ("approved" if approve else "set aside"), actor, r["key"], {"version": r["version"]})


def retire_rule(key, actor, reason=""):
    n = store.run("UPDATE unicorn_rules SET status='Retired', reason=? WHERE key=? AND status IN ('Approved','Proposed')",
                  (str(reason or "Retired")[:1000], key)).rowcount
    store.audit("unicorn rule retired", actor, key, {"reason": reason})
    return n


# ---------------------------------------------------------------- findings
def _finding_row(r):
    return {"id": r["id"], "key": r["key"], "rule": r["rule"], "capability": r["capability"],
            "capability_label": CAPABILITIES.get(r["capability"], r["capability"]),
            "company": r["company"] or "", "location": r["location"] or "", "location_name": r["location_name"] or "",
            "period": r["period"] or "", "title": r["title"], "detail": r["detail"] or "",
            "metric": r["metric"] or "", "value": r["value"], "limit": r["limit_value"], "unit": r["unit"] or "",
            "impact": r["impact"], "currency": r["currency"] or "", "severity": r["severity"] or "medium",
            "status": r["status"], "evidence": _j(r["evidence"], {}), "first_seen": r["first_seen"],
            "last_seen": r["last_seen"], "seen_count": r["seen_count"], "decision_id": r["decision_id"],
            "note": r["note"] or "", "updated_at": r["updated_at"], "updated_by": r["updated_by"] or ""}


def upsert_finding(f, day):
    """A finding from a detector: new, or the same open one seen again. Returns (id, is_new)."""
    key = "|".join(str(f.get(k) or "") for k in ("rule", "company", "location", "period"))
    old = store.one("SELECT * FROM unicorn_findings WHERE key=?", (key,))
    now = store.now()
    if old:
        if old["status"] in ("Closed", "Dismissed"):
            return old["id"], False                     # a person closed it: it stays closed for that period
        store.run("""UPDATE unicorn_findings SET value=?, limit_value=?, impact=?, severity=?, detail=?, evidence=?,
                     last_seen=?, seen_count=seen_count+1, updated_at=? WHERE id=?""",
                  (f.get("value"), f.get("limit"), f.get("impact"), f.get("severity"), f.get("detail"),
                   json.dumps(f.get("evidence") or {}, ensure_ascii=False, default=str), day, now, old["id"]))
        return old["id"], False
    cur = store.run("""INSERT INTO unicorn_findings(key, rule, capability, company, location, location_name, period,
                       title, detail, metric, value, limit_value, unit, impact, currency, severity, status, evidence,
                       first_seen, last_seen, updated_at, updated_by)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (key, f["rule"], f["capability"], f.get("company"), f.get("location"), f.get("location_name"),
                     f.get("period"), f["title"], f.get("detail"), f.get("metric"), f.get("value"), f.get("limit"),
                     f.get("unit"), f.get("impact"), f.get("currency"), f.get("severity") or "medium", "New",
                     json.dumps(f.get("evidence") or {}, ensure_ascii=False, default=str), day, day, now, "unicorn"))
    return cur.lastrowid, True


def finding(fid):
    r = store.one("SELECT * FROM unicorn_findings WHERE id=?", (int(fid),))
    return _finding_row(r) if r else None


def findings(status=None, capability=None, q=None, limit=300):
    sql, args = "SELECT * FROM unicorn_findings WHERE 1=1", []
    if status == "open":
        sql += " AND status IN ('New','Investigating','Proposed')"
    elif status:
        sql += " AND status=?"
        args.append(status)
    if capability:
        sql += " AND capability=?"
        args.append(capability)
    if q:
        sql += " AND (title LIKE ? OR location_name LIKE ? OR location LIKE ? OR company LIKE ?)"
        args += [f"%{q}%"] * 4
    sql += """ ORDER BY CASE status WHEN 'New' THEN 0 WHEN 'Investigating' THEN 1 WHEN 'Proposed' THEN 2 ELSE 3 END,
               CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
               last_seen DESC, COALESCE(impact, 0) DESC LIMIT ?"""
    args.append(int(limit))
    return [_finding_row(r) for r in store.q(sql, tuple(args))]


def set_finding(fid, status, actor, note=""):
    if status not in FINDING_STATUSES:
        raise ValueError("Unknown status.")
    f = store.one("SELECT * FROM unicorn_findings WHERE id=?", (int(fid),))
    if not f:
        raise ValueError("No such finding.")
    if status == "Dismissed" and not (note or "").strip():
        raise ValueError("Say why the finding is dismissed: Unicorn learns from the reason.")
    store.run("UPDATE unicorn_findings SET status=?, note=?, updated_at=?, updated_by=? WHERE id=?",
              (status, (note or f["note"] or "")[:2000], store.now(), actor, f["id"]))
    store.audit("unicorn finding " + status.lower(), actor, str(f["id"]), {"note": note})


def similar_history(f, limit=8):
    """What people decided before on the same rule and place: dismissals and refusals with their reasons, and the
    effect of approved actions. Given to Unicorn before it investigates."""
    out = []
    for r in store.q("""SELECT id, period, status, note, impact FROM unicorn_findings
                        WHERE rule=? AND COALESCE(location,'')=? AND id<>? AND status IN ('Dismissed','Closed')
                        ORDER BY id DESC LIMIT ?""", (f["rule"], f.get("location") or "", f.get("id") or 0, limit)):
        out.append({"finding": r["id"], "period": r["period"], "status": r["status"], "reason": r["note"]})
    for r in store.q("""SELECT d.id, d.title, d.status, d.decision_note, a.effect, a.baseline, a.latest, a.status AS ast
                        FROM unicorn_decisions d JOIN unicorn_findings f ON f.id=d.finding_id
                        LEFT JOIN unicorn_actions a ON a.decision_id=d.id
                        WHERE f.rule=? AND COALESCE(f.location,'')=? AND d.status IN ('Refused','Approved','Done')
                        ORDER BY d.id DESC LIMIT ?""", (f["rule"], f.get("location") or "", limit)):
        out.append({"decision": r["id"], "title": r["title"], "status": r["status"], "reason": r["decision_note"],
                    "action_status": r["ast"], "baseline": r["baseline"], "latest": r["latest"], "effect": r["effect"]})
    return out


# ---------------------------------------------------------------- decisions
def _decision_row(r):
    return {"id": r["id"], "finding_id": r["finding_id"], "title": r["title"], "rationale": r["rationale"] or "",
            "hypothesis": r["hypothesis"] or "", "confidence": r["confidence"] or "",
            "evidence": _j(r["evidence"], []), "impact": r["impact"], "currency": r["currency"] or "",
            "impact_formula": r["impact_formula"] or "", "alternatives": _j(r["alternatives"], []),
            "risks": r["risks"] or "", "approver_role": r["approver_role"] or "", "due_days": r["due_days"],
            "kpi": r["kpi"] or "", "target": r["target"] or "", "status": r["status"],
            "proposed_at": r["proposed_at"], "proposed_by": r["proposed_by"] or "", "decided_at": r["decided_at"],
            "decided_by": r["decided_by"] or "", "decision_note": r["decision_note"] or "", "owner": r["owner"] or "",
            "due": r["due"] or "", "meta": _j(r["meta"], {})}


def propose(fid, p, actor="unicorn", meta=None):
    """Unicorn's proposal for a finding (from its submit_proposal tool)."""
    f = store.one("SELECT * FROM unicorn_findings WHERE id=?", (int(fid),)) if fid else None
    title = str(p.get("decision") or p.get("title") or "").strip()[:300]
    if not title:
        raise ValueError("A proposal needs the decision.")
    conf = str(p.get("confidence") or "").lower()
    conf = conf if conf in ("high", "medium", "low") else "medium"
    try:
        impact = float(p["impact_amount"]) if p.get("impact_amount") is not None else None
    except (TypeError, ValueError):
        impact = None
    try:
        due_days = max(1, min(180, int(p.get("due_days") or 14)))
    except (TypeError, ValueError):
        due_days = 14
    cur = store.run("""INSERT INTO unicorn_decisions(finding_id, title, rationale, hypothesis, confidence, evidence, impact,
                       currency, impact_formula, alternatives, risks, approver_role, due_days, kpi, target, status,
                       proposed_at, proposed_by, meta) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (f["id"] if f else None, title, str(p.get("summary") or p.get("rationale") or "")[:4000],
                     str(p.get("hypothesis") or "")[:2000], conf,
                     json.dumps(p.get("evidence") or [], ensure_ascii=False)[:20000], impact,
                     str(p.get("impact_currency") or (f["currency"] if f else "") or "")[:8],
                     str(p.get("impact_formula") or "")[:1000],
                     json.dumps(p.get("alternatives") or [], ensure_ascii=False)[:6000], str(p.get("risks") or "")[:2000],
                     str(p.get("approver_role") or "")[:80], due_days, str(p.get("kpi") or "")[:200],
                     str(p.get("target") or "")[:200], "Proposed", store.now(), actor,
                     json.dumps(meta or {}, ensure_ascii=False, default=str)))
    if f:
        store.run("UPDATE unicorn_findings SET status='Proposed', decision_id=?, updated_at=?, updated_by=? WHERE id=?",
                  (cur.lastrowid, store.now(), actor, f["id"]))
    store.audit("unicorn proposal", actor, str(cur.lastrowid), {"finding": f["id"] if f else None, "impact": impact})
    return cur.lastrowid


def decision(did):
    r = store.one("SELECT * FROM unicorn_decisions WHERE id=?", (int(did),))
    return _decision_row(r) if r else None


def decisions(status=None, limit=200):
    sql, args = "SELECT * FROM unicorn_decisions", []
    if status:
        sql += " WHERE status=?"
        args.append(status)
    sql += " ORDER BY CASE status WHEN 'Proposed' THEN 0 WHEN 'Approved' THEN 1 ELSE 2 END, id DESC LIMIT ?"
    args.append(int(limit))
    return [_decision_row(r) for r in store.q(sql, tuple(args))]


def _date(v, what):
    try:
        return date.fromisoformat(str(v)[:10]).isoformat()
    except ValueError as exc:
        raise ValueError(f"{what}: a date, e.g. 2026-12-15.") from exc


def approve(did, actor, owner, due, target=None, note="", actions=None):
    """A person approves a proposal: owner, deadline, target; the decision becomes one action (or the actions
    listed), each starting from the finding's measured value as its baseline."""
    d = store.one("SELECT * FROM unicorn_decisions WHERE id=?", (int(did),))
    if not d:
        raise ValueError("No such decision.")
    if d["status"] != "Proposed":
        raise ValueError("Only a proposed decision can be approved.")
    owner = str(owner or "").strip()[:120]
    if not owner:
        raise ValueError("Name the owner who carries the decision out.")
    due = _date(due, "Deadline")
    f = store.one("SELECT * FROM unicorn_findings WHERE id=?", (d["finding_id"],)) if d["finding_id"] else None
    try:
        tgt = float(target) if target not in (None, "") else None
    except (TypeError, ValueError) as exc:
        raise ValueError("The target is a number in the unit of the finding (e.g. 8 for 8%).") from exc
    now = store.now()
    store.run("""UPDATE unicorn_decisions SET status='Approved', decided_at=?, decided_by=?, decision_note=?, owner=?,
                 due=? WHERE id=?""", (now, actor, str(note or "")[:2000], owner, due, d["id"]))
    titles = [str(t).strip()[:300] for t in (actions or []) if str(t).strip()] or [d["title"]]
    ids = []
    for t in titles:
        cur = store.run("""INSERT INTO unicorn_actions(decision_id, title, owner, due, status, rule, company, location,
                           metric, unit, baseline, target, notes, created_at, created_by)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (d["id"], t, owner, due, "Open", f["rule"] if f else None, f["company"] if f else None,
                         f["location"] if f else None, f["metric"] if f else None, f["unit"] if f else None,
                         f["value"] if f else None, tgt, "[]", now, actor))
        ids.append(cur.lastrowid)
    store.audit("unicorn decision approved", actor, str(d["id"]), {"owner": owner, "due": due, "actions": ids})
    return ids


def delegate(fid, actor, owner, due, task="", target=None, note=""):
    """A person hands a finding straight to an owner, without waiting for Unicorn's investigation: a decision taken
    by that person (approved at once) and its action, followed like any other (baseline = the finding's value).
    Returns (decision id, [action ids])."""
    f = store.one("SELECT * FROM unicorn_findings WHERE id=?", (int(fid),))
    if not f:
        raise ValueError("No such finding.")
    if f["status"] in ("Closed", "Dismissed"):
        raise ValueError("Reopen the finding first: it is " + f["status"].lower() + ".")
    if f["decision_id"]:
        d = store.one("SELECT status FROM unicorn_decisions WHERE id=?", (f["decision_id"],))
        if d and d["status"] == "Proposed":
            raise ValueError(f"Unicorn's proposal D{f['decision_id']} is waiting: approve it with its owner, or refuse it first.")
    if not str(owner or "").strip():
        raise ValueError("Choose the owner.")
    _date(due, "Deadline")
    ev = _j(f["evidence"], {})
    did = propose(f["id"], {"decision": str(task or "").strip() or f"Take action on: {f['title']}",
                            "summary": str(note or "").strip() or "Delegated by a person from the finding.",
                            "confidence": "high", "impact_amount": f["impact"],
                            "impact_formula": ev.get("impact_formula") or "", "kpi": f["metric"] or "",
                            "target": "" if target in (None, "") else str(target)},
                  actor=actor, meta={"delegated": True})
    ids = approve(did, actor, owner, due, target, note)
    store.audit("unicorn finding delegated", actor, str(f["id"]), {"owner": str(owner)[:120], "due": due, "decision": did})
    return did, ids


def refuse(did, actor, reason):
    d = store.one("SELECT * FROM unicorn_decisions WHERE id=?", (int(did),))
    if not d:
        raise ValueError("No such decision.")
    if d["status"] != "Proposed":
        raise ValueError("Only a proposed decision can be refused.")
    if not (reason or "").strip():
        raise ValueError("Say why: Unicorn learns from the reason and lowers similar proposals.")
    store.run("UPDATE unicorn_decisions SET status='Refused', decided_at=?, decided_by=?, decision_note=? WHERE id=?",
              (store.now(), actor, reason.strip()[:2000], d["id"]))
    if d["finding_id"]:
        store.run("UPDATE unicorn_findings SET status='Dismissed', note=?, updated_at=?, updated_by=? WHERE id=?",
                  ("Proposal refused: " + reason.strip()[:1900], store.now(), actor, d["finding_id"]))
    store.audit("unicorn decision refused", actor, str(d["id"]), {"reason": reason})


# ---------------------------------------------------------------- actions
def _action_row(r):
    return {"id": r["id"], "decision_id": r["decision_id"], "title": r["title"], "owner": r["owner"] or "",
            "due": r["due"] or "", "status": r["status"], "rule": r["rule"] or "", "company": r["company"] or "",
            "location": r["location"] or "", "metric": r["metric"] or "", "unit": r["unit"] or "",
            "baseline": r["baseline"], "target": r["target"], "latest": r["latest"], "latest_at": r["latest_at"],
            "effect": r["effect"], "notes": _j(r["notes"], []), "created_at": r["created_at"],
            "created_by": r["created_by"] or "", "closed_at": r["closed_at"], "closed_by": r["closed_by"] or "",
            "overdue": bool(r["due"] and r["status"] in ("Open", "In progress") and r["due"] < date.today().isoformat())}


def actions(status=None, limit=300):
    sql, args = "SELECT * FROM unicorn_actions", []
    if status == "open":
        sql += " WHERE status IN ('Open','In progress')"
    elif status:
        sql += " WHERE status=?"
        args.append(status)
    sql += " ORDER BY CASE status WHEN 'Open' THEN 0 WHEN 'In progress' THEN 1 ELSE 2 END, due, id DESC LIMIT ?"
    args.append(int(limit))
    return [_action_row(r) for r in store.q(sql, tuple(args))]


def update_action(aid, actor, status=None, note=None, owner=None, due=None):
    a = store.one("SELECT * FROM unicorn_actions WHERE id=?", (int(aid),))
    if not a:
        raise ValueError("No such action.")
    notes = _j(a["notes"], [])
    if note and str(note).strip():
        notes.append({"at": store.now(), "by": actor, "text": str(note).strip()[:2000]})
    st = a["status"]
    if status:
        if status not in ACTION_STATUSES:
            raise ValueError("Status: Open, In progress, Done or Cancelled.")
        st = status
    closed = st in ("Done", "Cancelled")
    store.run("""UPDATE unicorn_actions SET status=?, notes=?, owner=?, due=?, closed_at=?, closed_by=? WHERE id=?""",
              (st, json.dumps(notes[-100:], ensure_ascii=False), (str(owner).strip()[:120] if owner else a["owner"]),
               (_date(due, "Deadline") if due else a["due"]), store.now() if closed and not a["closed_at"] else a["closed_at"],
               actor if closed and not a["closed_by"] else a["closed_by"], a["id"]))
    # the decision is done when all its actions are closed
    if a["decision_id"]:
        left = store.one("SELECT COUNT(*) AS n FROM unicorn_actions WHERE decision_id=? AND status IN ('Open','In progress')",
                         (a["decision_id"],))
        if left and left["n"] == 0:
            store.run("UPDATE unicorn_decisions SET status='Done' WHERE id=? AND status='Approved'", (a["decision_id"],))
            d = store.one("SELECT finding_id FROM unicorn_decisions WHERE id=?", (a["decision_id"],))
            if d and d["finding_id"]:
                store.run("UPDATE unicorn_findings SET status='Closed', updated_at=?, updated_by=? WHERE id=? AND status='Proposed'",
                          (store.now(), actor, d["finding_id"]))
    store.audit("unicorn action", actor, str(a["id"]), {"status": st, "note": note})


def measure_action(aid, value, day):
    """Unicorn's measurement of an action's KPI (from the morning's figures); effect = latest − baseline."""
    a = store.one("SELECT * FROM unicorn_actions WHERE id=?", (int(aid),))
    if not a or value is None:
        return
    eff = round(value - a["baseline"], 4) if a["baseline"] is not None else None
    store.run("UPDATE unicorn_actions SET latest=?, latest_at=?, effect=? WHERE id=?", (value, day, eff, a["id"]))


# ---------------------------------------------------------------- tool trail
def log_tool(context, actor, tool, args, ok, ms, summary):
    store.run("INSERT INTO unicorn_tool_calls(at, context, actor, tool, args, ok, ms, summary) VALUES(?,?,?,?,?,?,?,?)",
              (store.now(), context, actor, tool, json.dumps(args, ensure_ascii=False, default=str)[:4000],
               1 if ok else 0, int(ms), str(summary or "")[:500]))


def tool_calls(context, limit=60):
    return [{"at": r["at"], "tool": r["tool"], "args": _j(r["args"], {}), "ok": bool(r["ok"]), "ms": r["ms"],
             "summary": r["summary"] or ""}
            for r in store.q("SELECT * FROM unicorn_tool_calls WHERE context=? ORDER BY id LIMIT ?", (context, limit))]


def counts():
    f = {r["status"]: r["n"] for r in store.q("SELECT status, COUNT(*) AS n FROM unicorn_findings GROUP BY status")}
    d = {r["status"]: r["n"] for r in store.q("SELECT status, COUNT(*) AS n FROM unicorn_decisions GROUP BY status")}
    over = store.one("""SELECT COUNT(*) AS n FROM unicorn_actions WHERE status IN ('Open','In progress') AND due < ?""",
                     (date.today().isoformat(),))
    opn = store.one("SELECT COUNT(*) AS n FROM unicorn_actions WHERE status IN ('Open','In progress')")
    rp = store.one("SELECT COUNT(*) AS n FROM unicorn_rules WHERE status='Proposed' AND created_by='unicorn'")
    return {"findings_open": sum(f.get(s, 0) for s in ("New", "Investigating", "Proposed")), "findings": f,
            "decisions_proposed": d.get("Proposed", 0), "decisions": d, "actions_open": opn["n"] if opn else 0,
            "actions_overdue": over["n"] if over else 0, "rules_proposed_by_unicorn": rp["n"] if rp else 0}
