"""Unicorn, the business analyst of pulse.

Every morning (UNICORN_TIME, default 08:00, every day) Unicorn asks Finance and Purchasing for yesterday's facts
(/internal/unicorn/facts on each tower, with the service token), has Claude write the morning brief from them and
posts it in the Unicorn chat, where management (permission unicorn.use) reads it and asks questions. Answers
use the facts of the latest brief and the conversation; Unicorn says so when a figure is not among them.

What leaves the server: the summary figures of the facts (sales, P&L, cash, payables and receivables, purchase
figures by company and restaurant, supplier and customer names of the largest balances) go to the Anthropic API
(ANTHROPIC_API_KEY in identity's .env). No passwords, no document lines, no personal data of guests.

Storage (identity.db): unicorn_messages (the brief is shared, username "*"; a question and its answer belong to
the person who asked), unicorn_facts (the facts of each day), unicorn_runs (the morning run of each day),
unicorn_seen (the last message each person has seen).
"""
import json
import threading
import time
import traceback
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

import config
import store
import unicorn_agent as agent
import unicorn_detect as detect
import unicorn_registers as reg

MODEL = config.get("UNICORN_MODEL", "claude-sonnet-5-5")
API_KEY = config.get("ANTHROPIC_API_KEY", "")
API_URL = config.get("ANTHROPIC_URL", "https://api.anthropic.com/v1/messages")
RUN_AT = config.get("UNICORN_TIME", "08:00")
LANGUAGE = config.get("UNICORN_LANGUAGE", "Albanian")
FINANCE_URL = config.get("FINANCE_URL", "http://finance:8766").rstrip("/")
PURCHASING_URL = config.get("PURCHASING_URL", "http://purchasing:8765").rstrip("/")
WAIT_MINUTES = int(config.get("UNICORN_WAIT_MINUTES", "45"))
TRIES, RETRY_MINUTES = 3, 20
# Sonnet 5.5 thinks before it writes (adaptive thinking, on by default) and the thinking counts against
# max_tokens: the budgets below leave room for both. UNICORN_EFFORT: low, medium, high (the API default), xhigh, max.
EFFORT = config.get("UNICORN_EFFORT", "medium")
BRIEF_TOKENS = int(config.get("UNICORN_BRIEF_TOKENS", "16000"))
CHAT_TOKENS = int(config.get("UNICORN_CHAT_TOKENS", "8000"))
MAX_QUESTION = 2000
HISTORY = 16

_run_lock = threading.Lock()
_running = {"day": None, "stage": "", "started": None}
_asking = set()
_ask_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS unicorn_messages (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  at        TEXT NOT NULL,
  username  TEXT NOT NULL,          -- "*" = the morning brief, seen by everyone allowed
  author    TEXT NOT NULL,          -- "unicorn" or "user"
  kind      TEXT NOT NULL,          -- "brief", "chat" or "note"
  day       TEXT,                   -- the day of the facts used
  text      TEXT NOT NULL,
  meta      TEXT
);
CREATE INDEX IF NOT EXISTS ix_unicorn_user ON unicorn_messages(username, id);
CREATE TABLE IF NOT EXISTS unicorn_facts (day TEXT PRIMARY KEY, facts TEXT NOT NULL, at TEXT);
CREATE TABLE IF NOT EXISTS unicorn_runs (day TEXT PRIMARY KEY, state TEXT, tries INTEGER DEFAULT 0, at TEXT,
                                         error TEXT, message_id INTEGER);
CREATE TABLE IF NOT EXISTS unicorn_seen (username TEXT PRIMARY KEY, last_id INTEGER);
"""


def init():
    with store._lock:
        store.connect().executescript(SCHEMA)
        store.connect().commit()
    reg.init()


def enabled():
    return bool(API_KEY)


# ---------------------------------------------------------------- the persona
# The master prompt (Geri's "UNICORN — RHG Executive Intelligence Agent", v2.0) ships next to this file; a copy in
# data/unicorn_prompt.md on the server, when present, replaces it without a deploy.
PROMPT_FILE = config.BASE_DIR / "unicorn_prompt.md"
PROMPT_OVERRIDE = config.BASE_DIR / "data" / "unicorn_prompt.md"


def master_prompt():
    for f in (PROMPT_OVERRIDE, PROMPT_FILE):
        try:
            text = f.read_text(encoding="utf-8").strip()
            if text:
                return text
        except OSError:
            continue
    return "You are UNICORN, the executive intelligence agent of Revo Hospitality Group (RHG), inside pulse."


def deployment_note():
    """What Unicorn can actually reach in this deployment, and the pulse house style. Read with the master prompt."""
    return f"""
# DEPLOYMENT IN THIS VERSION OF PULSE (read together with the master prompt above; where they differ, this wins)

What you can see today:
- Each morning pulse gathers a FACTS file (JSON) from two towers, for the day before:
  * Finance: Simphony sales per restaurant (net of VAT) for that day against the same weekday last week, the same
    weekday last year and the budget of the day, and the month to date against budget and the same dates last
    year, with discounts, voids and error corrects as a share of gross; the P&L of the latest closed month per
    company (sales, food, labour, other costs, EBITDA, against budget and last year, year to date, forecast, the
    largest account variances); cash and bank balances; payables and receivables (open, overdue, beyond the
    critical limit, largest names, exceptions); stock classes and goods received but never used when held.
  * Purchasing: spend of the month to date against the comparison period, price effects, goods received not
    invoiced, the tower's own brief (developments, risks, actions), the largest price movers, exceptions without an
    owner, invoices posted the day before without a purchase order.
- Facts listed under "errors", or missing, are UNAVAILABLE. Say exactly what is missing; never fill it with zero.
- Read-only TOOLS (when given in this conversation): list_restaurants, kpi_definitions, sales, sales_by_day,
  pl_month, payables_receivables, food_safety (Crunchtime), purchases_overview, supplier_summary, item_prices,
  invoices_without_po, and pulse's registers (open_findings, rules_in_force, decision_history). Use them to answer
  with fresh figures and to investigate across systems (sales ↔ P&L ↔ purchases ↔ food safety): read before you
  conclude, read narrowly (one company, one restaurant, the days that matter), and name the tool and period of
  every figure. A tool answer with "available": false or "error" is UNAVAILABLE, never zero. SevenRooms, Odoo
  and Microsoft 365 have no tool yet: point to the pulse screen (Sales → SevenRooms, People & Productivity).
- pulse keeps a Findings register, a Decisions register, an Action tracker and a Rules library. Detectors without
  AI raise findings every morning from the rules in force. You may record a finding (raise_finding) or propose a
  decision (submit_proposal) or a new version of a rule (propose_rule_change); a person approves, refuses, owns
  and closes. You read, investigate and propose; you never send a message, change a rule, approve a decision or
  write to Business Central, Simphony, Odoo, Crunchtime or any other source.
- Rules marked Approved are RHG's; rules marked Proposed are pulse's defaults awaiting approval: say so when a
  finding rests on one. A finding explained by an approved decision or rule is not a new problem: say "within the
  approved decision".
- The people you talk to are RHG's management (CEO, CFO, COO and administrators) with access to the whole group.
- History of exceptions: the previous morning brief is given to you; use it for "changes since the previous
  briefing".

House style of pulse (from its brand book):
- Write the platform's name as "pulse", always in lowercase, even at the start of a sentence.
- Numbers first: lead with the figure and the comparison ("Shitjet dje 4.2 M ALL, +12% ndaj vitit të kaluar").
- Short and actionable, one message per sentence, direct verbs; respectful ("ju"); no hype, jokes, exclamation
  marks or emojis. State the source and the day or month of every figure.
- Never add amounts in different currencies (ALL and EUR stay apart). Round: "4.2 M", "850 k", whole percentages.
- Label material figures VERIFIED, CALCULATED, ESTIMATED or UNAVAILABLE where it matters (not on every number).
- Severity tags in square brackets: [CRITICAL], [HIGH], [MEDIUM], [LOW].
- Markdown only: "##" headings, bullet or numbered lists, **bold** for the key figure; simple pipe tables only when
  a comparison needs one. Default language: {LANGUAGE}; answer in the language the person writes in.
"""


def system_prompt():
    return master_prompt() + "\n\n" + deployment_note()


# The brief is asked as the chat is (which answers reliably): the FACTS first, Unicorn acknowledging them, then the
# request with the previous briefing at the end - long material first, the question last.
FACTS_TURN = """<facts day="{day}" weekday="{weekday}">
{facts}
</facts>"""

BRIEF_ASK = """<previous_briefing day="{prev_day}">
{previous}
</previous_briefing>

Write today's RHG EXECUTIVE BRIEFING (daily) from the FACTS of {day} ({weekday}) above, for management.

Structure (section 15 of your prompt), in {language}, at most about 500 words:
- First line: the single most important thing to know this morning, with its figure.
- ## 1. Performanca e grupit
- ## 2. Lokacionet që kërkojnë vëmendje
- ## 3. Rreziqet financiare
- ## 4. Përjashtimet në blerje
- ## 5. Inventari dhe kontrolli i kostos
- ## 6. Çështje operacionale
- ## 7. Vendimet e rekomanduara — at most five, each as: Issue → Evidence → Impact → Recommendation → Priority → Owner
Focus on what changed since the previous briefing; repeat an unchanged issue only if it is still material, and say
it is still open. Leave out a section with nothing material and say so in one line; if its facts are unavailable,
say exactly which. Write the briefing itself now, starting with the first line."""


# ---------------------------------------------------------------- Claude
def _claude_once(messages, max_tokens, system):
    if not API_KEY:
        raise RuntimeError("Unicorn has no Anthropic API key (ANTHROPIC_API_KEY in identity's .env).")
    payload = {"model": MODEL, "max_tokens": max_tokens, "system": system or system_prompt(), "messages": messages}
    if EFFORT:
        payload["output_config"] = {"effort": EFFORT}
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(API_URL, data=body, method="POST", headers={
        "content-type": "application/json", "x-api-key": API_KEY, "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=280) as r:
            j = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = (json.loads(exc.read() or b"{}").get("error") or {}).get("message") or ""
        except ValueError:
            pass
        raise RuntimeError(f"The Anthropic API answered {exc.code}: {detail[:200]}") from None
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError(f"The Anthropic API could not be reached: {exc}") from None
    text = "".join(b.get("text", "") for b in j.get("content") or [] if b.get("type") == "text").strip()
    kinds = ",".join(b.get("type") or "?" for b in j.get("content") or []) or "none"
    if j.get("stop_reason") == "max_tokens":
        # never post a cut-off answer: thinking and text ran out of room
        raise CutOff(f"The answer was cut off at {max_tokens} tokens (blocks {kinds}).")
    if not text:
        raise EmptyAnswer(f"The Anthropic API gave an empty answer (stop_reason {j.get('stop_reason')}, blocks {kinds}).")
    return text, j.get("usage") or {}


class EmptyAnswer(RuntimeError):
    pass


class CutOff(RuntimeError):
    pass


def claude(messages, max_tokens=None, system=None):
    """One call to the Messages API. An empty answer is asked once more; an answer cut off at max_tokens is asked
    once more with twice the room. Only a whole answer is returned."""
    max_tokens = max_tokens or CHAT_TOKENS
    try:
        return _claude_once(messages, max_tokens, system)
    except EmptyAnswer:
        time.sleep(3)
        return _claude_once(messages, max_tokens, system)
    except CutOff:
        return _claude_once(messages, min(max_tokens * 2, 64000), system)


# ---------------------------------------------------------------- the towers' facts
def _internal(base, body):
    req = urllib.request.Request(base + "/internal/unicorn/facts", data=json.dumps(body).encode("utf-8"),
                                 method="POST", headers={"Content-Type": "application/json",
                                                         "X-Service-Token": config.SERVICE_TOKEN})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read() or b"{}")


def _wait_for(base, body, what, stage):
    t0 = time.time()
    first = True
    while True:
        try:
            r = _internal(base, {**body, "refresh": first})
        except (urllib.error.URLError, OSError, ValueError) as exc:
            r = {"available": False, "error": str(exc)}
        first = False
        if r.get("pending"):
            stage(f"{what}: {r.get('stage') or 'reading'}")
            if time.time() - t0 > WAIT_MINUTES * 60:
                return {"available": False, "error": f"{what} was still reading after {WAIT_MINUTES} minutes."}
            time.sleep(15)
            continue
        return r


def gather(day, stage=lambda t: None):
    fin = _wait_for(FINANCE_URL, {"date": day.isoformat()}, "Finance", stage)
    companies = fin.get("companies") or []
    pur = _wait_for(PURCHASING_URL, {"date": day.isoformat(), "companies": companies}, "Purchasing", stage)
    return {"day": day.isoformat(), "weekday": day.strftime("%A"), "finance": fin, "purchasing": pur}


def _compact(facts):
    return json.dumps(facts, ensure_ascii=False, separators=(",", ":"), default=str)


# ---------------------------------------------------------------- messages
def _post(username, author, kind, text, day=None, meta=None):
    cur = store.run("INSERT INTO unicorn_messages(at, username, author, kind, day, text, meta) VALUES(?,?,?,?,?,?,?)",
                    (store.now(), username, author, kind, day, text, json.dumps(meta or {})))
    return cur.lastrowid


def _row(r):
    return {"id": r["id"], "at": r["at"], "author": r["author"], "kind": r["kind"], "day": r["day"],
            "text": r["text"], "mine": r["username"] != "*"}


def messages(username, before=None, limit=60):
    if before:
        rows = store.q("""SELECT * FROM unicorn_messages WHERE (username='*' OR username=?) AND id < ?
                          ORDER BY id DESC LIMIT ?""", (username, int(before), limit))
    else:
        rows = store.q("""SELECT * FROM unicorn_messages WHERE username='*' OR username=?
                          ORDER BY id DESC LIMIT ?""", (username, limit))
    return [_row(r) for r in reversed(rows)]


def mark_seen(username, last_id):
    store.run("""INSERT INTO unicorn_seen(username, last_id) VALUES(?,?)
                 ON CONFLICT(username) DO UPDATE SET last_id=MAX(last_id, excluded.last_id)""", (username, last_id))


def unread(username):
    seen = store.one("SELECT last_id FROM unicorn_seen WHERE username=?", (username,))
    last = seen["last_id"] if seen else 0
    r = store.one("SELECT COUNT(*) AS n FROM unicorn_messages WHERE username='*' AND author='unicorn' AND id > ?",
                  (last,))
    return r["n"] if r else 0


def latest_facts():
    r = store.one("SELECT day, facts FROM unicorn_facts ORDER BY day DESC LIMIT 1")
    if not r:
        return None, None
    try:
        return r["day"], json.loads(r["facts"])
    except ValueError:
        return r["day"], None


# ---------------------------------------------------------------- the morning brief
def run(day, actor="scheduler"):
    """Gathers the facts of the day, writes the brief, posts it. Returns the message id."""
    with _run_lock:
        if _running["day"]:
            raise RuntimeError("Unicorn is already writing a brief.")
        _running.update(day=day.isoformat(), stage="Asking the towers for the figures", started=time.time())
    try:
        store.run("""INSERT INTO unicorn_runs(day, state, tries, at) VALUES(?, 'running', 1, ?)
                     ON CONFLICT(day) DO UPDATE SET state='running', tries=tries+1, at=excluded.at, error=NULL""",
                  (day.isoformat(), store.now()))
        facts = gather(day, lambda t: _running.update(stage=t))
        fin_ok = not (facts["finance"].get("available") is False)
        if not fin_ok and facts["purchasing"].get("available") is False:
            raise RuntimeError(f"No figures: Finance – {facts['finance'].get('error')}; "
                               f"Purchasing – {facts['purchasing'].get('error')}")
        store.run("""INSERT INTO unicorn_facts(day, facts, at) VALUES(?,?,?)
                     ON CONFLICT(day) DO UPDATE SET facts=excluded.facts, at=excluded.at""",
                  (day.isoformat(), _compact(facts), store.now()))
        _running["stage"] = "Detecting findings"
        found = _detect(facts)
        _running["stage"] = "Writing the brief"
        prev = store.one("""SELECT day, text FROM unicorn_messages WHERE username='*' AND kind='brief' AND day < ?
                            ORDER BY id DESC LIMIT 1""", (day.isoformat(),))
        text, usage = claude([
            {"role": "user", "content": FACTS_TURN.format(day=day.isoformat(), weekday=day.strftime("%A"),
                                                          facts=_compact(facts))},
            {"role": "assistant", "content": f"I have read the facts of {day.isoformat()}."},
            {"role": "user", "content": _registers_turn(found).strip() + "\n\n" + BRIEF_ASK.format(
                day=day.isoformat(), weekday=day.strftime("%A"), language=LANGUAGE,
                prev_day=prev["day"] if prev else "none",
                previous=prev["text"] if prev else "(this is the first briefing)")}],
            max_tokens=BRIEF_TOKENS)
        mid = _post("*", "unicorn", "brief", text, day.isoformat(),
                    {"model": MODEL, "usage": usage, "actor": actor})
        store.run("UPDATE unicorn_runs SET state='done', at=?, message_id=? WHERE day=?",
                  (store.now(), mid, day.isoformat()))
        store.audit("unicorn brief", actor, day.isoformat(), {"message": mid, "usage": usage})
        _running["stage"] = "Investigating the largest new findings"
        try:
            done = investigate_new(actor)
            if done:
                _post("*", "unicorn", "note", _investigation_note(done), day.isoformat(), {"investigated": done})
        except Exception:                                           # noqa: BLE001
            traceback.print_exc()
        return mid
    except Exception as exc:                                        # noqa: BLE001
        traceback.print_exc()
        store.run("UPDATE unicorn_runs SET state='failed', at=?, error=? WHERE day=?",
                  (store.now(), str(exc)[:500], day.isoformat()))
        raise
    finally:
        _running.update(day=None, stage="", started=None)


def run_now(actor):
    if not enabled():
        raise RuntimeError("Unicorn has no Anthropic API key yet (ANTHROPIC_API_KEY in identity's .env).")
    if _running["day"]:
        return {"running": True, "stage": _running["stage"]}
    day = date.today() - timedelta(days=1)
    threading.Thread(target=lambda: _safe(run, day, actor), daemon=True, name="unicorn-run").start()
    return {"running": True, "stage": "Starting"}


def _safe(fn, *a):
    try:
        fn(*a)
    except Exception:                                               # noqa: BLE001
        pass


def _safe_counts():
    try:
        return reg.counts()
    except Exception:                                               # noqa: BLE001
        return {}


def state():
    last = store.one("SELECT * FROM unicorn_runs ORDER BY day DESC LIMIT 1")
    return {"enabled": enabled(), "model": MODEL, "time": RUN_AT, "registers": _safe_counts(),
            "running": {"day": _running["day"], "stage": _running["stage"],
                        "seconds": int(time.time() - _running["started"]) if _running["started"] else 0}
            if _running["day"] else None,
            "last_run": dict(last) if last else None}


def _due(now):
    if not enabled():
        return None
    hh, mm = (int(x) for x in RUN_AT.split(":"))
    if (now.hour, now.minute) < (hh, mm):
        return None
    day = now.date() - timedelta(days=1)
    r = store.one("SELECT * FROM unicorn_runs WHERE day=?", (day.isoformat(),))
    if not r:
        return day
    if r["state"] in ("done", "running"):
        return None
    if (r["tries"] or 0) >= TRIES:
        return None
    try:
        last = datetime.fromisoformat(r["at"])
    except (TypeError, ValueError):
        return day
    return day if now - last >= timedelta(minutes=RETRY_MINUTES) else None


def loop():
    # a run left "running" by a restart is taken as failed
    store.run("UPDATE unicorn_runs SET state='failed', error='Interrupted by a restart' WHERE state='running'")
    # after a deploy the towers start with identity: give them time before the first scheduled read
    time.sleep(int(config.get("UNICORN_START_DELAY", "180")))
    while True:
        try:
            day = _due(datetime.now())
            if day and not _running["day"]:
                try:
                    run(day)
                except Exception:                                   # noqa: BLE001
                    pass
        except Exception:                                           # noqa: BLE001
            traceback.print_exc()
        time.sleep(60)


def start():
    init()
    threading.Thread(target=loop, daemon=True, name="unicorn").start()



# ---------------------------------------------------------------- detection, investigation, follow-up (Unicorn 2.0)
_inv_lock = threading.Lock()
_investigating = set()
SEV_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _detect(facts):
    """The detectors over the morning's facts and the measurement of open actions. Never stops the brief."""
    try:
        seen, new = detect.run(facts)
        measured = detect.follow_up(facts)
        return {"seen": seen, "new": new, "measured": measured}
    except Exception:                                               # noqa: BLE001
        traceback.print_exc()
        return {"seen": [], "new": [], "measured": 0, "error": "the detectors failed"}


def _registers_turn(found):
    """What the registers hold this morning, for the brief: new findings, proposals waiting, overdue actions."""
    new = [reg.finding(i) for i in (found or {}).get("new") or []]
    new = sorted([f for f in new if f], key=lambda f: (SEV_RANK.get(f["severity"], 9), -(f["impact"] or 0)))[:12]
    waiting = reg.decisions("Proposed")[:8]
    overdue = [a for a in reg.actions("open") if a["overdue"]][:8]
    if not (new or waiting or overdue):
        return "\n\n<registers>No new finding, no proposal waiting and no overdue action this morning.</registers>"
    slim = lambda f: {k: f[k] for k in ("id", "title", "location_name", "company", "period", "value", "limit", "unit",  # noqa: E731
                                        "impact", "currency", "severity", "seen_count")}
    reg_json = json.dumps({"new_findings": [slim(f) for f in new],
                           "proposals_waiting_for_approval": [{k: d[k] for k in ("id", "title", "impact", "currency", "proposed_at")} for d in waiting],
                           "overdue_actions": [{k: a[k] for k in ("id", "title", "owner", "due", "baseline", "latest", "effect", "unit")} for a in overdue]},
                          ensure_ascii=False, default=str)
    return ("\n\n<registers>\n" + reg_json + "\n</registers>\nIn section 7 cite the findings by their number "
            "(e.g. [F12]) and mention the proposals waiting and the overdue actions with their owners.")


INVESTIGATE_ASK = """<finding id="{id}">
{finding}
</finding>
<history_of_similar_findings>
{history}
</history_of_similar_findings>

Investigate finding F{id} for RHG management, as the CFO, COO and controller would. Today is {today}.
1. Check it is real: read the figures again with the tools (the same restaurant or company and period, and the
   comparison that explains it). If the data is missing or wrong, say so and propose no business decision.
2. Look for the cause across systems: sales (checks, guests, average check, discounts, voids, the days), the P&L
   by department, purchases and item prices of what drives the cost, food safety (spoil, yield, refused products),
   payables and receivables. Read narrowly; stop when the cause is clear or the reads run out.
3. Check rules_in_force and the history above: an approved rule or decision that explains it means no new
   decision (say so in no_action_reason); a reason people gave before for refusing a similar proposal must be
   weighed.
4. Finish with submit_proposal: facts (each with its tool and period) apart from the hypothesis, one decision with
   an action verb, the impact in money with its formula, the approver role, the days to carry it out, the KPI and
   its target. Write in {language}."""


def investigate(fid, actor="unicorn"):
    """One finding: Claude reads with the tools and ends with a proposal (or why none is needed)."""
    f = reg.finding(fid)
    if not f:
        raise ValueError("No such finding.")
    if f["status"] not in ("New", "Investigating"):
        raise ValueError("Only a new finding is investigated (this one is " + f["status"] + ").")
    with _inv_lock:
        if fid in _investigating:
            raise RuntimeError("Unicorn is already investigating this finding.")
        _investigating.add(fid)
    try:
        reg.set_finding(fid, "Investigating", actor)
        maxc, _st, _v = reg.rule_value("agent.max_tool_calls", {"n": 12})
        ctx = {"context": f"finding:{fid}", "actor": actor}
        hist = reg.similar_history(f)
        ask_text = INVESTIGATE_ASK.format(id=fid, finding=json.dumps(f, ensure_ascii=False, default=str),
                                          history=json.dumps(hist, ensure_ascii=False, default=str) if hist else "none",
                                          today=date.today().isoformat(), language=LANGUAGE)
        text, prop, usage, calls = agent.loop(system_prompt(), [{"role": "user", "content": ask_text}],
                                              agent.investigation_specs(), ctx, MODEL, CHAT_TOKENS, EFFORT,
                                              int(maxc.get("n") or 12), finish="submit_proposal")
        meta = {"model": MODEL, "usage": usage, "tool_calls": calls}
        if not prop:
            reg.set_finding(fid, "New", "unicorn", "Investigation ended without a proposal: " + (text or "")[:500])
            return {"finding": fid, "proposal": None}
        if prop.get("no_action_reason") and not prop.get("decision"):
            reg.set_finding(fid, "Closed", "unicorn", "No decision needed: " + prop["no_action_reason"][:1500])
            return {"finding": fid, "proposal": None, "no_action": prop["no_action_reason"]}
        if not prop.get("decision"):
            prop["decision"] = prop.get("no_action_reason") or "Review the finding"
        did = reg.propose(fid, prop, "unicorn", meta)
        return {"finding": fid, "proposal": did, "impact": prop.get("impact_amount"), "title": prop.get("decision")}
    except Exception as exc:                                        # noqa: BLE001
        traceback.print_exc()
        try:
            reg.set_finding(fid, "New", "unicorn", f"Investigation failed: {str(exc)[:300]}")
        except Exception:                                           # noqa: BLE001
            pass
        raise
    finally:
        with _inv_lock:
            _investigating.discard(fid)


def investigate_new(actor="scheduler"):
    """The n new findings with the largest impact (rule agent.investigate_per_day), investigated one by one."""
    if not enabled():
        return []
    n, _st, _v = reg.rule_value("agent.investigate_per_day", {"n": 3})
    todo = [f for f in reg.findings("New", limit=200) if not f["rule"].startswith("chat.")]
    todo.sort(key=lambda f: (SEV_RANK.get(f["severity"], 9), -(f["impact"] or 0)))
    done = []
    for f in todo[:max(0, int(n.get("n") or 0))]:
        try:
            done.append(investigate(f["id"], actor))
        except Exception as exc:                                    # noqa: BLE001
            done.append({"finding": f["id"], "error": str(exc)[:200]})
    return done


def investigate_now(fid, actor):
    if not enabled():
        raise RuntimeError("Unicorn has no Anthropic API key yet (ANTHROPIC_API_KEY in identity's .env).")
    f = reg.finding(fid)
    if not f or f["status"] not in ("New",):
        raise ValueError("Only a new finding can be investigated.")
    threading.Thread(target=lambda: _safe(investigate, int(fid), actor), daemon=True, name="unicorn-investigate").start()
    return {"started": True}


def _investigation_note(done):
    props = [d for d in done if d.get("proposal")]
    lines = [f"Hetova {len(done)} gjetjet e reja me ndikimin më të madh; {len(props)} propozime presin miratim "
             "te Unicorn → Decisions & actions."]
    for d in done:
        f = reg.finding(d["finding"])
        name = f"F{d['finding']} · {f['title']} · {f['location_name']}" if f else f"F{d['finding']}"
        if d.get("proposal"):
            imp = f" · ndikimi i pritur {d['impact']:,.0f}" if isinstance(d.get("impact"), (int, float)) else ""
            lines.append(f"- **{name}**: {d.get('title') or ''}{imp}")
        elif d.get("no_action"):
            lines.append(f"- {name}: pa vendim të ri ({d['no_action'][:160]})")
        else:
            lines.append(f"- {name}: hetimi nuk përfundoi ({d.get('error') or 'pa propozim'})")
    return "\n".join(lines)

# ---------------------------------------------------------------- the chat
def ask(user, text):
    """A question from a person: stored, answered from the latest facts and their conversation."""
    text = (text or "").strip()
    if not text:
        raise ValueError("Write a question first.")
    if len(text) > MAX_QUESTION:
        raise ValueError(f"Keep the question under {MAX_QUESTION} characters.")
    if not enabled():
        raise RuntimeError("Unicorn has no Anthropic API key yet (ANTHROPIC_API_KEY in identity's .env).")
    who = user["username"]
    with _ask_lock:
        if who in _asking:
            raise RuntimeError("Unicorn is still answering your previous question.")
        _asking.add(who)
    try:
        qid = _post(who, "user", "chat", text)
        day, facts = latest_facts()
        brief = store.one("SELECT text FROM unicorn_messages WHERE username='*' AND kind='brief' ORDER BY id DESC LIMIT 1")
        history = store.q("""SELECT author, text FROM unicorn_messages WHERE username=? AND kind='chat' AND id < ?
                             ORDER BY id DESC LIMIT ?""", (who, qid, HISTORY))
        msgs = []
        if facts:
            msgs.append({"role": "user", "content": f"FACTS of {day} (JSON, gathered this morning):\n{_compact(facts)}"})
            msgs.append({"role": "assistant", "content": brief["text"] if brief else "I have read the facts."})
        else:
            msgs.append({"role": "user", "content": "No morning facts have been gathered yet (the first brief has not "
                                                    "run). Read the figures you need with the tools; without a tool "
                                                    "for it, say the figure is unavailable."})
            msgs.append({"role": "assistant", "content": "Understood."})
        for h in reversed(history):
            role = "assistant" if h["author"] == "unicorn" else "user"
            if msgs[-1]["role"] == role:
                msgs[-1]["content"] += "\n\n" + h["text"]
            else:
                msgs.append({"role": role, "content": h["text"]})
        asker = f"{user.get('name') or who}, role {user.get('role') or '?'}"
        q = f"[{date.today().isoformat()} · question from {asker}; facts of {day or 'none yet'}]\n{text}"
        if msgs[-1]["role"] == "user":
            msgs[-1]["content"] += "\n\n" + q
        else:
            msgs.append({"role": "user", "content": q})
        try:
            perms = user.get("permissions") or []
            maxc, _st, _v = reg.rule_value("agent.max_tool_calls", {"n": 12})
            answer, _x, usage, calls = agent.loop(system_prompt(), msgs, agent.chat_specs(perms),
                                                  {"context": f"chat:{qid}", "actor": who}, MODEL, CHAT_TOKENS, EFFORT,
                                                  int(maxc.get("n") or 12))
            if not answer:
                raise EmptyAnswer("Unicorn gave an empty answer.")
            usage["tool_calls"] = calls
        except Exception as exc:                                    # noqa: BLE001
            nid = _post(who, "unicorn", "note", f"I could not answer just now: {exc}")
            return [_row(store.one("SELECT * FROM unicorn_messages WHERE id=?", (i,))) for i in (qid, nid)]
        aid = _post(who, "unicorn", "chat", answer, day, {"model": MODEL, "usage": usage})
        return [_row(store.one("SELECT * FROM unicorn_messages WHERE id=?", (i,))) for i in (qid, aid)]
    finally:
        with _ask_lock:
            _asking.discard(who)
