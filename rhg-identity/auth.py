"""Accounts, sessions and permission checks.

Passwords are stored only as a PBKDF2-HMAC-SHA256 hash with a per-user salt.
Sessions are random 32-byte tokens kept in SQLite and sent as an HttpOnly
cookie; every state-changing request must also carry the session's CSRF token,
which a cookie alone cannot supply.
"""
import hashlib
import hmac
import json
import re
import secrets
import time

import config
import permissions
import store


# ---------------------------------------------------------------- passwords
def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 salt.encode("utf-8"), config.PBKDF2_ITERATIONS)
    return salt, digest.hex()


def verify_password(password, salt, expected):
    _, got = hash_password(password, salt)
    return hmac.compare_digest(got, expected)


def password_problem(password):
    """Returns a message when the password is too weak, else None."""
    if len(password or "") < config.MIN_PASSWORD:
        return f"The password must be at least {config.MIN_PASSWORD} characters."
    checks = (re.search(r"[a-z]", password), re.search(r"[A-Z]", password),
              re.search(r"[0-9]", password))
    if not all(checks):
        return "The password must contain lower case, upper case and a digit."
    return None


# ---------------------------------------------------------------- accounts
def public(row):
    perms = store.role_permissions(row["role"])
    return {"username": row["username"], "name": row["name"], "email": row["email"],
            "role": row["role"], "active": bool(row["active"]),
            "must_change": bool(row["must_change"]), "last_login": row["last_login"],
            "permissions": perms}


def find(username):
    return store.one("SELECT * FROM users WHERE username=?", ((username or "").strip().lower(),))


def create_user(username, name, email, role, password, actor, ip=""):
    username = (username or "").strip().lower()
    if not username:
        return None, "A username is required."
    if not store.one("SELECT name FROM roles WHERE name=?", (role,)):
        return None, "Unknown role."
    problem = password_problem(password)
    if problem:
        return None, problem
    if find(username):
        return None, "That username already exists."
    salt, digest = hash_password(password)
    store.run("""INSERT INTO users(username,name,email,role,salt,hash,active,must_change,
                                   created_at,created_by,password_set_at)
                 VALUES(?,?,?,?,?,?,1,1,?,?,?)""",
              (username, (name or username).strip()[:80], (email or "").strip()[:120], role,
               salt, digest, store.now(), actor, store.now()))
    store.audit("user created", actor, username, {"role": role}, ip)
    return public(find(username)), None


def update_user(username, patch, actor, ip=""):
    row = find(username)
    if not row:
        return None, "No such user."
    sets, args = [], []

    if "role" in patch:
        if not store.one("SELECT name FROM roles WHERE name=?", (patch["role"],)):
            return None, "Unknown role."
        if row["role"] == "Administrator" and patch["role"] != "Administrator" \
                and _admin_count() <= 1:
            return None, "This is the last administrator; promote someone else first."
        sets.append("role=?"); args.append(patch["role"])
    if "name" in patch:
        sets.append("name=?"); args.append(str(patch["name"])[:80])
    if "email" in patch:
        sets.append("email=?"); args.append(str(patch["email"])[:120])
    if "active" in patch:
        if not patch["active"] and row["role"] == "Administrator" and _admin_count() <= 1:
            return None, "This is the last active administrator."
        sets.append("active=?"); args.append(1 if patch["active"] else 0)
        if patch["active"]:
            sets += ["failures=0", "locked_until=NULL"]
    if patch.get("password"):
        problem = password_problem(patch["password"])
        if problem:
            return None, problem
        salt, digest = hash_password(patch["password"])
        sets += ["salt=?", "hash=?", "must_change=1", "failures=0", "locked_until=NULL",
                 "password_set_at=?"]
        args += [salt, digest, store.now()]
    if patch.get("unlock"):
        sets += ["failures=0", "locked_until=NULL"]

    if not sets:
        return public(row), None
    sets += ["updated_at=?", "updated_by=?"]
    args += [store.now(), actor, row["username"]]
    store.run(f"UPDATE users SET {','.join(sets)} WHERE username=?", args)

    # A password change, a role change or a disable ends that user's sessions.
    if patch.get("password") or "role" in patch or patch.get("active") is False:
        store.run("DELETE FROM sessions WHERE username=?", (row["username"],))
    store.audit("user updated", actor, row["username"],
                {k: v for k, v in patch.items() if k != "password"} | (
                    {"password": "reset"} if patch.get("password") else {}), ip)
    return public(find(row["username"])), None


def delete_user(username, actor, ip=""):
    row = find(username)
    if not row:
        return False, "No such user."
    if row["role"] == "Administrator" and _admin_count() <= 1:
        return False, "This is the last administrator."
    store.run("DELETE FROM sessions WHERE username=?", (row["username"],))
    store.run("DELETE FROM users WHERE username=?", (row["username"],))
    store.audit("user deleted", actor, row["username"], {}, ip)
    return True, None


def _admin_count():
    r = store.one("SELECT COUNT(*) n FROM users WHERE role='Administrator' AND active=1")
    return r["n"] if r else 0


def list_users():
    return [public(r) for r in store.q("SELECT * FROM users ORDER BY username")]


# ---------------------------------------------------------------- sign in
def login(username, password, ip="", agent=""):
    username = (username or "").strip().lower()
    row = find(username)
    generic = "Wrong username or password."

    if not row:
        # Spend comparable time so a missing user is not obvious from timing.
        hash_password(password or "x")
        store.audit("login failed", username, username, {"reason": "no such user"}, ip)
        return None, None, generic
    if not row["active"]:
        store.audit("login failed", username, username, {"reason": "disabled"}, ip)
        return None, None, "This account is disabled."
    if row["locked_until"] and row["locked_until"] > time.time():
        mins = int((row["locked_until"] - time.time()) / 60) + 1
        return None, None, f"The account is locked. Try again in {mins} minute(s)."

    if not verify_password(password or "", row["salt"], row["hash"]):
        failures = row["failures"] + 1
        locked = time.time() + config.LOCK_MINUTES * 60 if failures >= config.MAX_FAILURES else None
        store.run("UPDATE users SET failures=?, locked_until=? WHERE username=?",
                  (failures, locked, username))
        store.audit("login failed", username, username, {"failures": failures}, ip)
        if locked:
            return None, None, f"Too many attempts. The account is locked for {config.LOCK_MINUTES} minutes."
        left = config.MAX_FAILURES - failures
        return None, None, f"{generic} {left} attempt(s) left before the account locks."

    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    store.run("""INSERT INTO sessions(token,username,csrf,created,last_seen,ip,user_agent)
                 VALUES(?,?,?,?,?,?,?)""",
              (token, username, csrf, time.time(), time.time(), ip, (agent or "")[:200]))
    store.run("UPDATE users SET failures=0, locked_until=NULL, last_login=? WHERE username=?",
              (store.now(), username))
    store.audit("login", username, username, {}, ip)
    return token, csrf, None


def session(token):
    """Returns the signed-in user, or None. Enforces both time limits."""
    if not token:
        return None
    s = store.one("SELECT * FROM sessions WHERE token=?", (token,))
    if not s:
        return None
    if time.time() - s["created"] > config.SESSION_HOURS * 3600 \
            or time.time() - s["last_seen"] > config.IDLE_MINUTES * 60:
        store.run("DELETE FROM sessions WHERE token=?", (token,))
        return None
    row = find(s["username"])
    if not row or not row["active"]:
        store.run("DELETE FROM sessions WHERE token=?", (token,))
        return None
    store.run("UPDATE sessions SET last_seen=? WHERE token=?", (time.time(), token))
    u = public(row)
    u["csrf"] = s["csrf"]
    return u


def logout(token, actor="", ip=""):
    if token:
        store.run("DELETE FROM sessions WHERE token=?", (token,))
        store.audit("logout", actor, actor, {}, ip)


def change_password(username, current, new, ip=""):
    row = find(username)
    if not row or not verify_password(current or "", row["salt"], row["hash"]):
        return False, "The current password is wrong."
    problem = password_problem(new)
    if problem:
        return False, problem
    if verify_password(new, row["salt"], row["hash"]):
        return False, "The new password must differ from the current one."
    salt, digest = hash_password(new)
    store.run("""UPDATE users SET salt=?,hash=?,must_change=0,password_set_at=?
                 WHERE username=?""", (salt, digest, store.now(), row["username"]))
    store.audit("password changed", username, username, {}, ip)
    return True, None


def can(user, permission):
    return bool(user) and permission in (user.get("permissions") or [])


def bootstrap():
    """Creates the first administrator on an empty installation."""
    if store.one("SELECT username FROM users LIMIT 1"):
        return None
    pw = secrets.token_urlsafe(12)
    salt, digest = hash_password(pw)
    store.run("""INSERT INTO users(username,name,email,role,salt,hash,active,must_change,
                                   created_at,created_by,password_set_at)
                 VALUES('admin','Administrator','','Administrator',?,?,1,1,?,'system',?)""",
              (salt, digest, store.now(), store.now()))
    store.audit("bootstrap", "system", "admin", {}, "")
    return pw
