"""Session checks, delegated to the central identity service.

This tower holds no accounts and no passwords any more: it takes the platform
cookie, asks identity who the caller is, and is told which permissions they
hold. Sign-in, the user manager and the audit trail all live at the root of
dashboard.rhg.al.

The old per-app rights ("view", "actions", "closing", "settings", "users",
"export") are still what server.py asks for, so they are mapped here onto the
platform's granular permissions rather than rewriting every call site.
"""
import json
import os
import threading
import time
import urllib.error
import urllib.request

APP = os.environ.get("RHG_APP", "finance")          # "finance" or "purchasing"
IDENTITY_URL = os.environ.get("IDENTITY_URL", "http://identity:8767")
SERVICE_TOKEN = os.environ.get("SERVICE_TOKEN", "")
COOKIE_NAME = os.environ.get("COOKIE_NAME", "rhg_session")

# The old right -> the platform permission that now carries it.
RIGHT_MAP = {
    "view":     f"{APP}.view",
    "actions":  f"{APP}.actions.manage",
    "settings": f"{APP}.settings.manage",
    "export":   f"{APP}.export",
    "closing":  "finance.closing.manage",
    "users":    "admin.users.manage",
}

# Kept so server.py's /api/session response still has a shape the page expects.
ROLES = {}

SESSION_HOURS = int(os.environ.get("SESSION_HOURS", "12"))

# A validated session is held briefly so a dashboard that fires twenty requests
# does not ask identity twenty times. Short enough that a revoked session stops
# working almost at once.
_cache, _cache_lock = {}, threading.Lock()
CACHE_SECONDS = 20


def _validate(token):
    req = urllib.request.Request(
        f"{IDENTITY_URL}/internal/validate",
        data=json.dumps({"token": token}).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Service-Token": SERVICE_TOKEN},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return (json.loads(r.read() or b"{}") or {}).get("user")
    except (urllib.error.URLError, OSError, ValueError):
        # Identity unreachable: refuse rather than let anyone through.
        return None


def session(token):
    if not token:
        return None
    now = time.time()
    with _cache_lock:
        hit = _cache.get(token)
        if hit and now - hit[0] < CACHE_SECONDS:
            return hit[1]
    user = _validate(token)
    with _cache_lock:
        if len(_cache) > 500:
            for k, v in list(_cache.items()):
                if now - v[0] > CACHE_SECONDS:
                    _cache.pop(k, None)
        _cache[token] = (now, user)
    return user


def can(user, right):
    """True when the signed-in user holds the permission behind an old right."""
    if not user:
        return False
    needed = RIGHT_MAP.get(right, right)
    return needed in (user.get("permissions") or [])


def cookie_name():
    return COOKIE_NAME


# --- the old local account API is gone; these keep server.py importable and
#     send anyone who reaches them to the platform's own user manager.
_MOVED = "Accounts are managed centrally. Use Administration at the site root."


def bootstrap():
    return None


def authenticate(*_a, **_k):
    return None, None, _MOVED


def create_user(*_a, **_k):
    return None, _MOVED


def update_user(*_a, **_k):
    return None, _MOVED


def change_own_password(*_a, **_k):
    return False, _MOVED


def logout(token):
    """Signing out is central: drop the cached copy so this tower stops
    honouring the session immediately, and let the page go to the root."""
    with _cache_lock:
        _cache.pop(token, None)


def list_users():
    return []


def active_sessions():
    return []
