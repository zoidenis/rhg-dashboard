"""SMTP settings entered in Purchasing, kept outside the public settings API."""
import json
import os
import re
import tempfile
import threading
from urllib.parse import urlsplit

import config
import store

SMTP_FILE = store.DATA_DIR / "smtp.json"
_lock = threading.Lock()
_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}\Z")
_EMAIL = re.compile(r"[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+\Z")
_FIELDS = ("host", "port", "security", "user", "from", "public_url", "password")


def _defaults():
    return {"host": config.SMTP_HOST, "port": config.SMTP_PORT,
            "security": config.SMTP_SECURITY, "user": config.SMTP_USER,
            "from": config.SMTP_FROM, "public_url": config.PUBLIC_URL,
            "password": config.SMTP_PASSWORD}


def _read():
    try:
        data = json.loads(SMTP_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}


def _merged():
    values = _defaults()
    values.update({k: v for k, v in _read().items() if k in _FIELDS})
    return values


def current():
    with _lock:
        return _merged()


def _public(values):
    return {k: values[k] for k in _FIELDS if k != "password"} | {
        "has_password": bool(values["password"]),
        "configured": ready(values),
    }


def ready(values):
    return bool(values["host"] and values["from"]
                and (not values["user"] or (values["password"] and values["security"] != "none")))


def info():
    return _public(current())


def save(patch, actor):
    """Save a full or partial form. A blank password keeps the old one."""
    if not isinstance(patch, dict):
        raise ValueError("Mail server settings must be an object.")
    unknown = set(patch) - set(_FIELDS) - {"clear_password"}
    if unknown:
        raise ValueError("Unknown mail server setting.")

    with _lock:
        before = _merged()
        after = dict(before)
        for key in ("host", "security", "user", "from", "public_url"):
            if key in patch:
                if not isinstance(patch[key], str):
                    raise ValueError(f"{key} must be text.")
                after[key] = patch[key].strip()
        if "port" in patch:
            try:
                after["port"] = int(patch["port"])
            except (TypeError, ValueError):
                raise ValueError("Port must be a number.") from None
        if not 1 <= after["port"] <= 65535:
            raise ValueError("Port must be between 1 and 65535.")
        if after["security"] not in ("starttls", "ssl", "none"):
            raise ValueError("Security must be STARTTLS, SSL or none.")
        if after["user"] and after["security"] == "none":
            raise ValueError("SMTP authentication requires STARTTLS or SSL.")
        if after["host"] and not _HOST.fullmatch(after["host"]):
            raise ValueError("Enter a mail server name without a URL or port.")
        if len(after["user"]) > 254 or any(c in after["user"] for c in "\r\n"):
            raise ValueError("Invalid mail server username.")
        if after["from"] and not _EMAIL.fullmatch(after["from"]):
            raise ValueError("Enter a valid From email address.")
        url = urlsplit(after["public_url"])
        if (url.scheme not in ("http", "https") or not url.hostname
                or url.username or url.password or url.path not in ("", "/")
                or url.query or url.fragment):
            raise ValueError("Public URL must be the site's root address, such as https://dashboard.rhg.al.")
        after["public_url"] = after["public_url"].rstrip("/")

        password = patch.get("password", "")
        if not isinstance(password, str) or len(password) > 1024 or any(c in password for c in "\r\n"):
            raise ValueError("Invalid mail server password.")
        if patch.get("clear_password") is True:
            after["password"] = ""
        elif password:
            after["password"] = password.strip()
        elif after["host"] != before["host"] or after["user"] != before["user"] or not after["user"]:
            # A password for another mailbox or server must never be reused silently.
            after["password"] = ""

        fd, tmp = tempfile.mkstemp(prefix=".smtp-", dir=store.DATA_DIR)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                json.dump(after, out, indent=1)
                out.write("\n")
            os.replace(tmp, SMTP_FILE)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    store.audit("smtp settings", "mail server", actor, _public(before), _public(after),
                "Password changed" if before["password"] != after["password"] else "")
    return _public(after)
