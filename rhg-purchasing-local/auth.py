"""Accounts, sessions and permissions for the Control Tower.

Passwords are never stored: only a PBKDF2-HMAC-SHA256 hash with a per-user salt
and 240,000 iterations. Sessions are random 32-byte tokens held server side and
sent to the browser in an HttpOnly cookie, so page scripts cannot read them.

This protects the application from the people who can reach it. It is not
transport security: the app speaks plain HTTP, so if it is published beyond this
PC (APP_HOST=0.0.0.0) the password crosses the network in clear text. Put it
behind HTTPS before doing that.
"""
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from datetime import datetime

import config

USERS_FILE = config.BASE_DIR / "data" / "users.json"
ITERATIONS = 240_000
SESSION_HOURS = 12
MAX_FAILURES = 5
LOCK_MINUTES = 15

ROLES = {
    "Administrator": {
        "description": "Everything, including accounts and thresholds.",
        "can": ["view", "actions", "settings", "users", "export"],
    },
    "Purchasing director": {
        "description": "All screens, manages actions and thresholds; cannot manage accounts.",
        "can": ["view", "actions", "settings", "export"],
    },
    "Buyer": {
        "description": "All screens and actions; cannot change thresholds or accounts.",
        "can": ["view", "actions", "export"],
    },
    "Viewer": {
        "description": "Read only, including exports.",
        "can": ["view", "export"],
    },
}

_lock = threading.Lock()
_sessions = {}          # token -> {username, role, created, last_seen}


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _read():
    try:
        return json.loads(USERS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"users": []}


def _write(data):
    USERS_FILE.parent.mkdir(exist_ok=True)
    tmp = USERS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(USERS_FILE)


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), ITERATIONS)
    return salt, digest.hex()


def _find(data, username):
    return next((u for u in data["users"] if u["username"].lower() == (username or "").lower()), None)


def bootstrap():
    """Creates the first administrator on an empty installation and returns the
    one-time password, which is printed to the console and must be changed at
    first sign-in. Nothing is created if accounts already exist."""
    with _lock:
        data = _read()
        if data["users"]:
            return None
        password = secrets.token_urlsafe(9)
        salt, digest = hash_password(password)
        data["users"].append({
            "username": "admin", "name": "Administrator", "role": "Administrator",
            "salt": salt, "hash": digest, "active": True, "must_change": True,
            "created_at": _now(), "created_by": "installation", "last_login": None,
            "failures": 0, "locked_until": None,
        })
        _write(data)
        return password


def list_users():
    data = _read()
    return [{k: u.get(k) for k in ("username", "name", "role", "active", "must_change",
                                   "created_at", "created_by", "last_login", "locked_until")}
            for u in data["users"]]


def create_user(username, name, role, password, actor):
    username = (username or "").strip().lower()
    if not username or not password:
        return None, "A username and a password are required."
    if role not in ROLES:
        return None, "Unknown role."
    if len(password) < 8:
        return None, "The password must be at least 8 characters."
    with _lock:
        data = _read()
        if _find(data, username):
            return None, "That username already exists."
        salt, digest = hash_password(password)
        user = {"username": username, "name": (name or username).strip(), "role": role,
                "salt": salt, "hash": digest, "active": True, "must_change": True,
                "created_at": _now(), "created_by": actor, "last_login": None,
                "failures": 0, "locked_until": None}
        data["users"].append(user)
        _write(data)
    return {k: user[k] for k in ("username", "name", "role", "active")}, None


def update_user(username, patch, actor):
    """Role, name, active flag, password reset and unlock."""
    with _lock:
        data = _read()
        user = _find(data, username)
        if not user:
            return None, "No such user."
        if "role" in patch:
            if patch["role"] not in ROLES:
                return None, "Unknown role."
            admins = [u for u in data["users"] if u["role"] == "Administrator" and u["active"]]
            if user["role"] == "Administrator" and patch["role"] != "Administrator" and len(admins) <= 1:
                return None, "This is the last active administrator; promote someone else first."
            user["role"] = patch["role"]
        if "name" in patch:
            user["name"] = str(patch["name"])[:80]
        if "active" in patch:
            admins = [u for u in data["users"] if u["role"] == "Administrator" and u["active"]]
            if not patch["active"] and user["role"] == "Administrator" and len(admins) <= 1:
                return None, "This is the last active administrator."
            user["active"] = bool(patch["active"])
            if user["active"]:
                user["failures"], user["locked_until"] = 0, None
        if patch.get("password"):
            if len(patch["password"]) < 8:
                return None, "The password must be at least 8 characters."
            user["salt"], user["hash"] = hash_password(patch["password"])
            user["must_change"] = True
            user["failures"], user["locked_until"] = 0, None
        if patch.get("unlock"):
            user["failures"], user["locked_until"] = 0, None
        user["updated_at"], user["updated_by"] = _now(), actor
        _write(data)
        # a changed or disabled account loses its open sessions immediately
        for token, s in list(_sessions.items()):
            if s["username"] == user["username"] and (patch.get("password") or not user["active"]
                                                      or "role" in patch):
                _sessions.pop(token, None)
        return {k: user[k] for k in ("username", "name", "role", "active")}, None


def change_own_password(username, current, new):
    if len(new or "") < 8:
        return False, "The new password must be at least 8 characters."
    with _lock:
        data = _read()
        user = _find(data, username)
        if not user:
            return False, "No such user."
        _, digest = hash_password(current, user["salt"])
        if not hmac.compare_digest(digest, user["hash"]):
            return False, "The current password is not right."
        user["salt"], user["hash"] = hash_password(new)
        user["must_change"] = False
        user["updated_at"] = _now()
        _write(data)
    return True, None


def authenticate(username, password):
    """Returns (session_token, user, error)."""
    with _lock:
        data = _read()
        user = _find(data, username)
        if not user or not user.get("active"):
            return None, None, "Wrong username or password."
        locked = user.get("locked_until")
        if locked and locked > _now():
            return None, None, f"The account is locked until {locked[11:16]} after too many attempts."
        _, digest = hash_password(password or "", user["salt"])
        if not hmac.compare_digest(digest, user["hash"]):
            user["failures"] = int(user.get("failures") or 0) + 1
            if user["failures"] >= MAX_FAILURES:
                user["locked_until"] = datetime.fromtimestamp(
                    time.time() + LOCK_MINUTES * 60).isoformat(timespec="seconds")
            _write(data)
            left = max(0, MAX_FAILURES - user["failures"])
            return None, None, ("Wrong username or password."
                                + (f" {left} attempt(s) left before the account locks." if left else
                                   f" The account is locked for {LOCK_MINUTES} minutes."))
        user["failures"], user["locked_until"] = 0, None
        user["last_login"] = _now()
        _write(data)
        token = secrets.token_urlsafe(32)
        _sessions[token] = {"username": user["username"], "role": user["role"],
                            "name": user.get("name") or user["username"],
                            "created": time.time(), "last_seen": time.time()}
        public = {"username": user["username"], "name": user.get("name"), "role": user["role"],
                  "must_change": bool(user.get("must_change")), "can": ROLES[user["role"]]["can"]}
        return token, public, None


def session(token):
    if not token:
        return None
    with _lock:
        s = _sessions.get(token)
        if not s:
            return None
        if time.time() - s["created"] > SESSION_HOURS * 3600:
            _sessions.pop(token, None)
            return None
        s["last_seen"] = time.time()
        data = _read()
        user = _find(data, s["username"])
        if not user or not user.get("active"):
            _sessions.pop(token, None)
            return None
        return {"username": user["username"], "name": user.get("name"), "role": user["role"],
                "must_change": bool(user.get("must_change")), "can": ROLES[user["role"]]["can"]}


def logout(token):
    with _lock:
        _sessions.pop(token, None)


def can(user, permission):
    return bool(user) and permission in (user.get("can") or [])


def active_sessions():
    with _lock:
        return [{"username": s["username"], "role": s["role"],
                 "since": datetime.fromtimestamp(s["created"]).isoformat(timespec="seconds"),
                 "last_seen": datetime.fromtimestamp(s["last_seen"]).isoformat(timespec="seconds")}
                for s in _sessions.values()]
