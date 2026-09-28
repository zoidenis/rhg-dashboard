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


def _seed_roles(conn):
    have = {r["name"] for r in conn.execute("SELECT name FROM roles")}
    for name, spec in permissions.DEFAULT_ROLES.items():
        if name not in have:
            conn.execute("INSERT INTO roles(name,description,permissions,system) VALUES(?,?,?,?)",
                         (name, spec["description"], json.dumps(spec["permissions"]),
                          1 if spec["system"] else 0))


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


def role_permissions(name):
    r = one("SELECT permissions FROM roles WHERE name=?", (name,))
    return json.loads(r["permissions"]) if r else []


def purge_sessions():
    """Drops sessions past their absolute or idle limit."""
    cutoff_abs = time.time() - config.SESSION_HOURS * 3600
    cutoff_idle = time.time() - config.IDLE_MINUTES * 60
    run("DELETE FROM sessions WHERE created < ? OR last_seen < ?", (cutoff_abs, cutoff_idle))
