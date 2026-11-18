"""Notifications: what changed on the tasks of a person, wherever it was changed.

Every item of the Executive Action Center (the exceptions registers of Finance and Purchasing, Unicorn's actions and
proposals) is compared with how it was the last time: a new owner, a comment, a change of status or deadline, a
deadline that comes near or passes, an escalation. Each change becomes a notification for the people it concerns,
never for the person who made it:

  * the owners of the item;
  * the people who took part in it (who commented or handed it over) for comments and changes of status;
  * the previous owner when it is handed over;
  * the head of the department (L1) and the executives (L2) of the Action Center when it is escalated;
  * the people who approve Unicorn's decisions for a new proposal.

Changes made in the Action Center, in the towers' own registers or by the daily controls are all seen, because the
comparison reads the items, not the screens. The first comparison only records how things are (no flood of old
news), and an item that reappears after a tower did not answer is not new unless it was found in the last 3 days.
Items created by the daily controls in one go come as one notification per person ("12 new items for you").

In the app: a bell beside the logo on every page (unread count, the list, mark as read) and the page
/notifications (every notification, filters, the person's own choices). By email, when the Action Center's managers
switch it on: one email every 10 minutes at most with what is new for the person, only of the kinds the person chose.
Kept 90 days.
"""
import json
import threading
import time
import traceback
from datetime import date, datetime, timedelta

import config
import store

KINDS = {
    "assigned": "Assigned to you",
    "handed_over": "Handed over to someone else",
    "comment": "Comments",
    "status": "Status changes",
    "closed": "Closed",
    "reopened": "Reopened",
    "due": "Deadline changed",
    "due_soon": "Deadline is near",
    "overdue": "Overdue",
    "escalated": "Escalations",
    "new": "New items for you",
    "proposal": "Proposals to approve",
}
# what a person gets unless they change it: everything in the app; by email only what is addressed to them
DEFAULT_PREFS = {k: {"app": True, "email": k in ("assigned", "comment", "escalated", "proposal")} for k in KINDS}
SYSTEM_ACTORS = {"", "pulse", "scheduler", "system", "admin:scheduler", "daily control", "the data"}
GROUP_NEW = 3                 # more new items than this for one person in one pass → one notification
NEW_DAYS = 3                  # an item first seen longer ago than this is not "new" when it appears
KEEP_DAYS = 90
SCAN_SECONDS = int(config.get("NOTIFY_SCAN_SECONDS", "120"))
EMAIL_MINUTES = int(config.get("NOTIFY_EMAIL_MINUTES", "10"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS notifications (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  username  TEXT NOT NULL,
  at        TEXT NOT NULL,
  kind      TEXT NOT NULL,
  uid       TEXT,
  title     TEXT,
  data      TEXT,                -- JSON: actor, text, status, due, level, n, company, location, severity, tower
  link      TEXT,
  read_at   TEXT,
  emailed_at TEXT
);
CREATE INDEX IF NOT EXISTS notifications_user ON notifications(username, id);
CREATE INDEX IF NOT EXISTS notifications_unread ON notifications(username, read_at);
CREATE TABLE IF NOT EXISTS notif_snap (uid TEXT PRIMARY KEY, data TEXT NOT NULL, seen TEXT);
CREATE TABLE IF NOT EXISTS notif_prefs (username TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notif_meta (key TEXT PRIMARY KEY, value TEXT);
"""

_ready = False
_lock = threading.RLock()
_scan_lock = threading.Lock()
_poke = threading.Event()


def _init():
    global _ready
    if _ready:
        return
    with store._lock:
        store.connect().executescript(SCHEMA)
        store.connect().commit()
    _ready = True


def _meta(key, default=None):
    _init()
    r = store.one("SELECT value FROM notif_meta WHERE key=?", (key,))
    return json.loads(r["value"]) if r else default


def _set_meta(key, value):
    _init()
    store.run("INSERT INTO notif_meta(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
              (key, json.dumps(value)))


# ---------------------------------------------------------------- settings and a person's choices
def settings():
    s = _meta("settings", {}) or {}
    return {"email_enabled": bool(s.get("email_enabled", False))}


def save_settings(patch, actor):
    s = settings()
    if "email_enabled" in patch:
        s["email_enabled"] = bool(patch["email_enabled"])
    _set_meta("settings", s)
    store.audit("notifications settings", actor, "", s)
    return s


def prefs(username):
    _init()
    r = store.one("SELECT data FROM notif_prefs WHERE username=?", (username,))
    got = json.loads(r["data"]) if r else {}
    return {k: {"app": bool((got.get(k) or {}).get("app", d["app"])), "email": bool((got.get(k) or {}).get("email", d["email"]))}
            for k, d in DEFAULT_PREFS.items()}


def save_prefs(username, patch):
    p = prefs(username)
    for k, v in (patch or {}).items():
        if k in p and isinstance(v, dict):
            for c in ("app", "email"):
                if c in v:
                    p[k][c] = bool(v[c])
    store.run("INSERT INTO notif_prefs(username, data) VALUES(?,?) ON CONFLICT(username) DO UPDATE SET data=excluded.data",
              (username, json.dumps(p)))
    return p


# ---------------------------------------------------------------- reading and marking
def _row(r):
    d = json.loads(r["data"] or "{}")
    return {"id": r["id"], "at": r["at"], "kind": r["kind"], "uid": r["uid"], "title": r["title"], "link": r["link"],
            "read": bool(r["read_at"]), **{k: d.get(k) for k in ("actor", "text", "status", "from_status", "due", "level", "n",
                                                                 "company", "location", "severity", "tower", "to")}}


def unread(username):
    _init()
    return store.one("SELECT COUNT(*) AS n FROM notifications WHERE username=? AND read_at IS NULL", (username,))["n"]


def latest_id(username):
    _init()
    r = store.one("SELECT MAX(id) AS m FROM notifications WHERE username=?", (username,))
    return r["m"] or 0


def listing(username, only_unread=False, before=None, kind=None, limit=50, after=None):
    _init()
    sql, args = "SELECT * FROM notifications WHERE username=?", [username]
    if only_unread:
        sql += " AND read_at IS NULL"
    if before:
        sql += " AND id<?"
        args.append(int(before))
    if after:
        sql += " AND id>?"
        args.append(int(after))
    if kind and kind in KINDS:
        sql += " AND kind=?"
        args.append(kind)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit or 50), 200)))
    return [_row(r) for r in store.q(sql, args)]


def mark_read(username, ids=None, everything=False, read=True):
    _init()
    now = store.now() if read else None
    if everything:
        cur = store.run("UPDATE notifications SET read_at=? WHERE username=? AND read_at IS NULL", (now, username))
    else:
        ids = [int(i) for i in (ids or []) if str(i).isdigit()][:500]
        if not ids:
            return 0
        cur = store.run(f"UPDATE notifications SET read_at=? WHERE username=? AND id IN ({','.join('?' * len(ids))})",
                        (now, username, *ids))
    return cur.rowcount


# ---------------------------------------------------------------- the comparison
def _ts(h):
    return str(h.get("ts") or h.get("at") or "")


def _note(h):
    return str(h.get("note") or h.get("text") or h.get("comment") or "").strip()


def _snap(x):
    hist = x.get("history") or []
    last = max((_ts(h) for h in hist), default="")
    return {"status": x["status"], "owners": sorted(x.get("owners") or []), "due": x.get("due") or "",
            "esc": int(x.get("escalation") or 0), "overdue": bool(x.get("overdue")), "due_soon": bool(x.get("due_soon")),
            "last": last, "last_n": sum(1 for h in hist if _ts(h) == last),
            "title": x.get("title") or "", "active": bool(x.get("active"))}


def _new_history(x, p):
    """The entries of an item's history written since the snapshot p: later than its last time, or at that same
    second beyond the ones already counted (two changes in the same second)."""
    hist = x.get("history") or []
    last, n = p.get("last") or "", int(p.get("last_n") or 0)
    same = [h for h in hist if _ts(h) == last]
    return [h for h in hist if _ts(h) > last] + (same[n:] if last else [])


def _actor_emails(by, by_user):
    """The emails of the person who made a change ("Name <email>", an email, or a username of pulse)."""
    import actioncenter
    got = actioncenter._emails(by)
    if not got and by:
        u = by_user.get(str(by).strip().lower())
        if u:
            got = [u]
    return set(got)


def _is_system(by):
    b = str(by or "").strip().lower()
    return b in SYSTEM_ACTORS or b.startswith("pulse") or b.startswith("scheduler")


def _actor_name(by, U):
    import actioncenter
    if _is_system(by):
        return ""
    e = actioncenter._emails(by)
    if e and e[0] in U:
        return U[e[0]]["name"]
    s = str(by or "")
    return s.split("<")[0].strip() or s


def _people(x):
    """Emails of the people who took part in an item: its history's human authors."""
    import actioncenter
    out = []
    for h in x.get("history") or []:
        if not _is_system(h.get("by")):
            out += actioncenter._emails(h.get("by"))
    return out


def _escalation_people(x, level):
    import actioncenter
    s = actioncenter.settings()["escalation"]
    out = []
    if level >= 1:
        out += actioncenter.role_people(s["heads"].get(x["department"], "coo"), x["company"], x.get("location") or "")
    if level >= 2:
        for r in s["executives"]:
            out += actioncenter.role_people(r, x["company"], x.get("location") or "")
    return out


def compare(items, snaps, U, *, baseline=True, today=None, approvers=None, escalation_people=None):
    """The notifications that the changes between snaps (uid → snapshot) and items make: a list of
    (email, kind, item, data). Pure: no reading, no writing (escalation_people and approvers are given)."""
    today = today or date.today().isoformat()
    by_user = {u["username"].lower(): e for e, u in U.items()}
    new_since = (date.fromisoformat(today) - timedelta(days=NEW_DAYS)).isoformat()
    out = []

    def send(to, kind, x, actor_mails, data):
        for e in dict.fromkeys(to):
            if e in U and e not in actor_mails:
                out.append((e, kind, x, data))

    for x in items:
        s, p = _snap(x), snaps.get(x["uid"])
        owners = s["owners"]
        if p is None:
            if not baseline or not s["active"] or (x.get("first_seen") or today) < new_since:
                continue
            if x["status"] == "Awaiting approval":
                send(approvers or [], "proposal", x, set(), {})
            else:
                send(owners, "new", x, set(), {})
            continue
        hist = sorted(_new_history(x, p), key=_ts)
        last_by = next((h.get("by") for h in reversed(hist)), "")
        actor_mails = _actor_emails(last_by, by_user) if not _is_system(last_by) else set()
        actor = _actor_name(last_by, U)
        human_notes = [(h.get("by"), _note(h)) for h in hist if _note(h) and not _is_system(h.get("by"))
                       and not _note(h).startswith(("Owner", "Due:", "SLA due date", "Verified", "Moved from"))]
        added = [o for o in owners if o not in p["owners"]]
        removed = [o for o in p["owners"] if o not in owners]
        text = human_notes[-1][1][:300] if human_notes else ""
        if added:
            send(added, "assigned", x, actor_mails, {"actor": actor, "text": text})
        if removed:
            send(removed, "handed_over", x, actor_mails,
                 {"actor": actor, "to": ", ".join(U[o]["name"] if o in U else o for o in owners) or "the role"})
        watchers = owners + _people(x)
        notes = list(human_notes) if not added else []
        # a change of status and the comment that came with it, from the same person: one notification
        said = ""
        if s["status"] != p["status"] and notes and notes[-1][0] == last_by:
            said = notes.pop()[1][:300]
        for by, note in notes:
            send(watchers, "comment", x, _actor_emails(by, by_user), {"actor": _actor_name(by, U), "text": note[:300]})
        if s["status"] != p["status"]:
            extra = {"text": said} if said else {}
            if s["status"] == "Closed":
                send(watchers, "closed", x, actor_mails, {"actor": actor or "", "from_status": p["status"], **extra})
            elif not p.get("active") and s["active"]:
                send(watchers, "reopened", x, actor_mails, {"actor": actor, "from_status": p["status"], **extra})
            elif s["status"] != "Awaiting approval":
                send(watchers, "status", x, actor_mails, {"actor": actor, "status": s["status"], "from_status": p["status"], **extra})
        if s["due"] and p["due"] and s["due"] != p["due"] and s["active"] and not added:
            send(owners, "due", x, actor_mails, {"actor": actor, "due": s["due"]})
        if s["active"] and s["due_soon"] and not p.get("due_soon") and not s["overdue"]:
            send(owners, "due_soon", x, set(), {"due": s["due"]})
        if s["active"] and s["overdue"] and not p.get("overdue"):
            send(owners, "overdue", x, set(), {"due": s["due"]})
        if s["active"] and s["esc"] > (p.get("esc") or 0):
            send(owners, "escalated", x, set(), {"level": s["esc"], "due": s["due"]})
            others = [e for e in (escalation_people(x, s["esc"]) if escalation_people else []) if e not in owners]
            send(others, "escalated", x, set(), {"level": s["esc"], "due": s["due"], "to": "you"})
    return out


def _link(x, can_ac):
    from urllib.parse import quote
    if can_ac:
        return "/actions#item/" + quote(x["uid"], safe="")
    return x.get("link") or "/actions"


def scan(trigger=""):
    """One comparison: read the items, write the notifications, keep the new picture. Returns how many were made."""
    import actioncenter
    import auth
    if not _scan_lock.acquire(blocking=False):
        _poke.set()
        return 0
    try:
        _init()
        d = actioncenter.collect(None)
        items, U = d["items"], actioncenter.users()
        snaps = {r["uid"]: json.loads(r["data"]) for r in store.q("SELECT uid, data FROM notif_snap")}
        baseline = bool(_meta("baseline", False))
        rows = {e: store.one("SELECT * FROM users WHERE username=?", (u["username"],)) for e, u in U.items()}
        pub = {e: auth.public(r) for e, r in rows.items() if r}
        approvers = [e for e, u in pub.items() if auth.can(u, "unicorn.decisions.approve")]
        can_ac = {e: any(auth.can(u, f"platform.s.actions.{t}") for t in ("executive", "departments", "mine"))
                  for e, u in pub.items()}
        events = compare(items, snaps, U, baseline=baseline, approvers=approvers, escalation_people=_escalation_people)
        prefs_of = {}
        now = store.now()
        made = 0
        # the items the daily controls made in one go: one notification per person when there are many
        news = {}
        for e, kind, x, data in events:
            if kind == "new":
                news.setdefault(e, []).append(x)
        for e, xs in news.items():
            if len(xs) > GROUP_NEW:
                events = [ev for ev in events if not (ev[0] == e and ev[1] == "new")]
                top = sorted(xs, key=lambda x: actioncenter.RANK.get(x["severity"], 9))[0]
                events.append((e, "new", dict(top, uid="", title=f"{len(xs)} new items"),
                               {"n": len(xs), "severity": top["severity"], "group": True}))
        with store._lock:
            c = store.connect()
            for e, kind, x, data in events:
                u = U[e]["username"]
                pr = prefs_of.setdefault(u, prefs(u))
                if not pr[kind]["app"] and not pr[kind]["email"]:
                    continue
                link = "/actions#mine" if data.get("group") else _link(x, can_ac.get(e))
                body = dict(data, company=x.get("company"), location=x.get("location"), severity=x.get("severity"),
                            tower=x.get("tower"))
                c.execute("INSERT INTO notifications(username, at, kind, uid, title, data, link, read_at) VALUES(?,?,?,?,?,?,?,?)",
                          (u, now, kind, x.get("uid") or "", (x.get("title") or "")[:300], json.dumps(body), link,
                           None if pr[kind]["app"] else now))
                made += 1
            for x in items:
                c.execute("INSERT INTO notif_snap(uid, data, seen) VALUES(?,?,?) ON CONFLICT(uid) DO UPDATE SET data=excluded.data, seen=excluded.seen",
                          (x["uid"], json.dumps(_snap(x)), now))
            old = (datetime.now() - timedelta(days=30)).isoformat(timespec="seconds")
            c.execute("DELETE FROM notif_snap WHERE seen < ?", (old,))
            c.execute("DELETE FROM notifications WHERE at < ?",
                      ((datetime.now() - timedelta(days=KEEP_DAYS)).isoformat(timespec="seconds"),))
            c.commit()
        if not baseline and items:
            _set_meta("baseline", True)
        _set_meta("last_scan", {"at": now, "items": len(items), "made": made, "trigger": trigger, "errors": d["errors"][:5]})
        return made
    finally:
        _scan_lock.release()


def poke():
    """A change was just made in pulse: compare again in a few seconds (not now: the tower writes first)."""
    _poke.set()


# ---------------------------------------------------------------- email
def _send_emails():
    """One email per person with what is new for them (of the kinds they chose by email), at most every
    EMAIL_MINUTES; only when the Action Center's managers switched it on."""
    import actioncenter
    import html
    if not settings()["email_enabled"]:
        return 0
    _init()
    since = (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds")
    rows = store.q("SELECT n.*, u.email, u.name FROM notifications n JOIN users u ON u.username = n.username "
                   "WHERE n.emailed_at IS NULL AND n.at >= ? AND u.active=1 AND u.email IS NOT NULL ORDER BY n.id", (since,))
    by_user = {}
    for r in rows:
        by_user.setdefault(r["username"], []).append(r)
    sent = 0
    site = config.get("PUBLIC_URL", "https://dashboard.rhg.al").rstrip("/")
    for user, rs in by_user.items():
        pr = prefs(user)
        mine = [r for r in rs if pr.get(r["kind"], {}).get("email") and not r["read_at"]]
        ids = [r["id"] for r in rs]
        if mine:
            lines = []
            for r in mine:
                n = _row(r)
                where = (" · " + html.escape(n["company"])) if n.get("company") else ""
                said = ("<div style='margin-top:4px'>" + html.escape(n["text"]) + "</div>") if n.get("text") else ""
                href = site + html.escape(n["link"] or "/notifications")
                lines.append("<tr><td style='padding:8px 10px;border-bottom:1px solid #e3e1d9'>"
                             f"<b>{html.escape(sentence(n))}</b>"
                             f"<div style='color:#5f636b;font-size:13px'>{html.escape(n['title'] or '')}{where}</div>{said}"
                             f"<div style='margin-top:6px'><a href='{href}'>Open it in pulse</a></div></td></tr>")
            body = (f"<div style='font-family:Inter,Arial,sans-serif;color:#16181d'><p>Hello {html.escape(rs[0]['name'] or '')},</p>"
                    f"<p>What changed on your tasks in pulse:</p><table style='border-collapse:collapse;width:100%;max-width:640px'>{''.join(lines)}</table>"
                    f"<p style='color:#5f636b;font-size:12px'>Choose what reaches you by email in pulse → Notifications → My choices.</p></div>")
            text = "\n".join(f"- {sentence(_row(r))}: {r['title']}" for r in mine)
            subject = f"[pulse] {sentence(_row(mine[0]))}: {mine[0]['title']}"[:180] if len(mine) == 1 else f"[pulse] {len(mine)} updates on your tasks"
            try:
                actioncenter._send_mail([rs[0]["email"]], [], subject, body, text)
                sent += 1
            except Exception:                                       # noqa: BLE001
                traceback.print_exc()
                continue
        store.run(f"UPDATE notifications SET emailed_at=? WHERE id IN ({','.join('?' * len(ids))})", (store.now(), *ids))
    return sent


def sentence(n):
    """The notification in one English line (the email; the page builds its own, translated)."""
    a = n.get("actor") or ""
    k = n["kind"]
    if k == "assigned":
        return f"{a} assigned you" if a else "Assigned to you"
    if k == "handed_over":
        return f"{a} handed it over to {n.get('to') or 'someone else'}" if a else f"Handed over to {n.get('to') or 'someone else'}"
    if k == "comment":
        return f"{a} commented" if a else "New comment"
    if k == "status":
        return f"{a} moved it to {n.get('status')}" if a else f"Moved to {n.get('status')}"
    if k == "closed":
        return f"{a} closed it" if a else "Closed: no longer found in the data"
    if k == "reopened":
        return f"{a} reopened it" if a else "Reopened"
    if k == "due":
        return f"{a} moved the deadline to {n.get('due')}" if a else f"Deadline moved to {n.get('due')}"
    if k == "due_soon":
        return f"Due on {n.get('due')}"
    if k == "overdue":
        return f"Overdue since {n.get('due')}"
    if k == "escalated":
        return f"Escalated to you (L{n.get('level')})" if n.get("to") == "you" else f"Escalated L{n.get('level')}"
    if k == "new":
        return f"{n.get('n')} new items for you" if n.get("n") else "New item for you"
    if k == "proposal":
        return "Proposal waiting for your approval"
    return KINDS.get(k, k)


# ---------------------------------------------------------------- the loop
def loop():
    time.sleep(int(config.get("NOTIFY_START_DELAY", "200")))
    last_mail = 0.0
    while True:
        try:
            scan("timer")
        except Exception:                                           # noqa: BLE001
            traceback.print_exc()
        try:
            if time.time() - last_mail >= EMAIL_MINUTES * 60:
                last_mail = time.time()
                _send_emails()
        except Exception:                                           # noqa: BLE001
            traceback.print_exc()
        if _poke.wait(SCAN_SECONDS):
            _poke.clear()
            time.sleep(4)                       # the tower writes, then the comparison reads


def start():
    threading.Thread(target=loop, daemon=True, name="notifications").start()
