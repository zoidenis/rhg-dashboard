"""SQLite storage for accounts, roles, sessions and the audit trail.

Sessions live here rather than in a dict, so a restart or a second worker does
not sign everyone out - the behaviour the two control towers had before.
"""
import json
import sqlite3
import threading
import time
from datetime import datetime

import config
import permissions

_lock = threading.RLock()
_conn = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  username      TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  email         TEXT,
  role          TEXT NOT NULL,
  salt          TEXT NOT NULL,
  hash          TEXT NOT NULL,
  active        INTEGER NOT NULL DEFAULT 1,
  must_change   INTEGER NOT NULL DEFAULT 1,
  failures      INTEGER NOT NULL DEFAULT 0,
  locked_until  REAL,
  created_at    TEXT, created_by TEXT,
  updated_at    TEXT, updated_by TEXT,
  last_login    TEXT,
  password_set_at TEXT
);
CREATE TABLE IF NOT EXISTS roles (
  name        TEXT PRIMARY KEY,
  description TEXT,
  permissions TEXT NOT NULL,     -- JSON array
  system      INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
  token       TEXT PRIMARY KEY,
  username    TEXT NOT NULL,
  csrf        TEXT NOT NULL,
  created     REAL NOT NULL,
  last_seen   REAL NOT NULL,
  ip          TEXT,
  user_agent  TEXT
);
CREATE TABLE IF NOT EXISTS audit (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  at        TEXT NOT NULL,
  actor     TEXT,
  action    TEXT NOT NULL,
  subject   TEXT,
  detail    TEXT,
  ip        TEXT
);
-- Layout of a tower: the order of its sections and the ones hidden. scope "role" (owner = role
-- name) is what a role sees; scope "user" (owner = username) is one person's own arrangement.
CREATE TABLE IF NOT EXISTS layouts (
  app         TEXT NOT NULL,
  scope       TEXT NOT NULL,
  owner       TEXT NOT NULL,
  data        TEXT NOT NULL,     -- JSON
  updated_at  TEXT,
  updated_by  TEXT,
  PRIMARY KEY (app, scope, owner)
);
CREATE INDEX IF NOT EXISTS ix_sessions_user ON sessions(username);
CREATE INDEX IF NOT EXISTS ix_audit_at ON audit(at);
"""


def connect():
    global _conn
    with _lock:
        if _conn is None:
            config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA foreign_keys=ON")
            _conn.executescript(SCHEMA)
            _seed_roles(_conn)
            _conn.commit()
        return _conn


def _columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _added_path():
    return config.DB_PATH.parent / "screens_added.json"


def _added_read():
    try:
        return list(json.loads(_added_path().read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return []


def _added_write(given):
    try:
        _added_path().parent.mkdir(parents=True, exist_ok=True)
        _added_path().write_text(json.dumps(sorted(set(given))), encoding="utf-8")
    except OSError:
        pass


def _seed_roles(conn):
    # columns added after the first release: the department of a role, the access scope of an account
    # (companies and locations; NULL = everything)
    if "department" not in _columns(conn, "roles"):
        conn.execute("ALTER TABLE roles ADD COLUMN department TEXT")
    if "scope" not in _columns(conn, "roles"):           # the companies and locations of a role; NULL = everything
        conn.execute("ALTER TABLE roles ADD COLUMN scope TEXT")
    if "scope" not in _columns(conn, "users"):
        conn.execute("ALTER TABLE users ADD COLUMN scope TEXT")
    if "scope" not in _columns(conn, "sessions"):        # a report session cut to the report's locations
        conn.execute("ALTER TABLE sessions ADD COLUMN scope TEXT")
    if "lang" not in _columns(conn, "users"):            # the language of pulse for the person: sq (default), en
        conn.execute("ALTER TABLE users ADD COLUMN lang TEXT")
    have = {r["name"] for r in conn.execute("SELECT name FROM roles")}
    for name, spec in permissions.DEFAULT_ROLES.items():
        if name not in have:
            conn.execute("INSERT INTO roles(name,description,permissions,system,department) VALUES(?,?,?,?,?)",
                         (name, spec["description"], json.dumps(spec["permissions"]),
                          1 if spec["system"] else 0, spec.get("department") or "Other"))
    # roles stored before screens existed: they get the screens their data rights allowed, and a department
    for r in conn.execute("SELECT name, permissions, department FROM roles").fetchall():
        held = json.loads(r["permissions"] or "[]")
        if not any(".s." in p for p in held):
            add = [p for p in permissions.screens_for_rights(held) if p not in held]
            if add:
                conn.execute("UPDATE roles SET permissions=? WHERE name=?", (json.dumps(held + add), r["name"]))
        if not r["department"]:
            dep = (permissions.DEFAULT_ROLES.get(r["name"]) or {}).get("department") or "Other"
            conn.execute("UPDATE roles SET department=? WHERE name=?", (dep, r["name"]))
    # screens added to the catalogue later: given once to the roles that held the screen they grew out of
    # (data/screens_added.json remembers what was given, so a screen taken away later stays away)
    given = _added_read()
    for new, like in permissions.ADDED_SCREENS.items():
        if new in given:
            continue
        for r in conn.execute("SELECT name, permissions FROM roles WHERE system=0").fetchall():
            held = json.loads(r["permissions"] or "[]")
            likes = like if isinstance(like, (list, tuple)) else (like,)
            if any(x in held for x in likes) and new not in held:
                conn.execute("UPDATE roles SET permissions=? WHERE name=?", (json.dumps(held + [new]), r["name"]))
        given.append(new)
        _added_write(given)
    # screens taken out of the catalogue (after ADDED_SCREENS gave their holders the screen that replaces them)
    gone = set(permissions.REMOVED_SCREENS)
    for r in conn.execute("SELECT name, permissions FROM roles").fetchall():
        held = json.loads(r["permissions"] or "[]")
        if gone & set(held):
            conn.execute("UPDATE roles SET permissions=? WHERE name=?", (json.dumps([p for p in held if p not in gone]), r["name"]))
    # A system role (Administrator) holds every permission, including ones added to the
    # catalogue after it was stored.
    for r in conn.execute("SELECT name, permissions FROM roles WHERE system=1").fetchall():
        held = json.loads(r["permissions"] or "[]")
        missing = [p for p in permissions.PERMISSIONS if p not in held]
        if missing:
            conn.execute("UPDATE roles SET permissions=? WHERE name=?", (json.dumps(held + missing), r["name"]))


def now():
    return datetime.now().isoformat(timespec="seconds")


def q(sql, args=()):
    with _lock:
        return connect().execute(sql, args).fetchall()


def one(sql, args=()):
    rows = q(sql, args)
    return rows[0] if rows else None


def run(sql, args=()):
    with _lock:
        c = connect()
        cur = c.execute(sql, args)
        c.commit()
        return cur


def audit(action, actor, subject="", detail=None, ip=""):
    run("INSERT INTO audit(at,actor,action,subject,detail,ip) VALUES(?,?,?,?,?,?)",
        (now(), actor or "", action, subject or "", json.dumps(detail or {}), ip or ""))


def role_scope(name):
    r = one("SELECT scope FROM roles WHERE name=?", (name,))
    return r["scope"] if r else None


def role_permissions(name):
    r = one("SELECT permissions FROM roles WHERE name=?", (name,))
    return json.loads(r["permissions"]) if r else []


def purge_sessions():
    """Drops sessions past their absolute or idle limit."""
    cutoff_abs = time.time() - config.SESSION_HOURS * 3600
    cutoff_idle = time.time() - config.IDLE_MINUTES * 60
    run("DELETE FROM sessions WHERE created < ? OR last_seen < ?", (cutoff_abs, cutoff_idle))
