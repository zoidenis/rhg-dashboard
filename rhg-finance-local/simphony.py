"""Sales from Oracle Simphony, read through the Business Intelligence (BI) API.

Read-only, like Business Central: the tower only asks Reporting and Analytics for
totals and never writes anything back to Simphony.

Sign-in is OpenID Connect with PKCE, as Oracle describes it for BI API accounts:
authorize -> signin (account name, password, enterprise short name) -> token. The ID
token lasts 14 days and the refresh token 28; the tower renews the ID token a few
days before it runs out and only signs in again when the refresh token is gone. The
API account's password expires every 60 days in Reporting and Analytics.

The connection is entered in Settings and kept in data/simphony.json on the server.
The password and the tokens never leave the server: the screen only learns whether
they are set.

Daily totals are kept in data/simphony_days.json. A business date that closed more
than two days before the latest one is final and is not read again; the most recent
days are re-read when they are older than a quarter of an hour.
"""
import base64
import hashlib
import json
import os
import re
import secrets
import tempfile
import threading
import time
from datetime import date, timedelta
from urllib.parse import parse_qs, urlsplit

import requests

import store

CONFIG_FILE = store.DATA_DIR / "simphony.json"
DAYS_FILE = store.DATA_DIR / "simphony_days.json"
REDIRECT_URI = "apiaccount://callback"
TIMEOUT = 60
APP_NAME = "RHG Control Towers"

_lock = threading.Lock()          # config file and tokens
_days_lock = threading.Lock()     # daily totals file
_auth_lock = threading.Lock()     # one sign-in at a time

PUBLIC_FIELDS = ("auth_server", "app_server", "org", "client_id", "username", "loc_map")
SECRET_FIELDS = ("password", "id_token", "refresh_token")
DEFAULTS = {"auth_server": "", "app_server": "", "org": "", "client_id": "", "username": "",
            "loc_map": "", "password": "", "password_saved_at": "",
            "id_token": "", "refresh_token": "", "token_at": 0, "expires_in": 0}


class SimphonyError(Exception):
    pass


# ---------------------------------------------------------------- settings

def _read():
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return dict(DEFAULTS, **(data if isinstance(data, dict) else {}))
    except (OSError, ValueError):
        return dict(DEFAULTS)


def _write(cfg):
    fd, tmp = tempfile.mkstemp(prefix=".simphony-", dir=store.DATA_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(cfg, out, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, CONFIG_FILE)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _ready(cfg):
    return all(cfg.get(k) for k in ("auth_server", "app_server", "org", "client_id", "username", "password"))


def info():
    """What the screen may see. No password, no token."""
    with _lock:
        cfg = _read()
    missing = [label for key, label in (("auth_server", "Authentication server"),
                                        ("app_server", "Application server"),
                                        ("org", "Enterprise short name"), ("client_id", "Client ID"),
                                        ("username", "API account name"), ("password", "Password"))
               if not cfg.get(key)]
    age = None
    if cfg.get("password_saved_at"):
        try:
            age = (date.today() - date.fromisoformat(cfg["password_saved_at"])).days
        except ValueError:
            age = None
    token_left = None
    if cfg.get("id_token") and cfg.get("token_at"):
        token_left = round((cfg["token_at"] + float(cfg.get("expires_in") or 0) - time.time()) / 86400, 1)
    return {k: cfg.get(k, "") for k in PUBLIC_FIELDS} | {
        "configured": not missing, "missing": missing,
        "has_password": bool(cfg.get("password")),
        "password_age_days": age,
        "password_warning": age is not None and age >= 50,
        "signed_in": bool(cfg.get("id_token")), "token_days_left": token_left,
    }


_URL = re.compile(r"^https://[A-Za-z0-9.-]+(:\d+)?/?$")


def save(patch, actor):
    if not isinstance(patch, dict):
        raise ValueError("The Simphony settings must be an object.")
    with _lock:
        before = _read()
        cfg = dict(before)
        for key in PUBLIC_FIELDS:
            if key not in patch:
                continue
            value = patch[key]
            if not isinstance(value, str):
                raise ValueError(f"{key} must be text.")
            value = value.strip()
            if key in ("auth_server", "app_server"):
                if value and not _URL.match(value):
                    raise ValueError("A server must be the https:// address only, without a path, "
                                     "e.g. https://ors-idm.mte5.oraclerestaurants.com")
                value = value.rstrip("/")
            if key == "org" and value and not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", value):
                raise ValueError("The enterprise short name is letters and digits only, e.g. RHG.")
            if key == "client_id" and value and not re.fullmatch(r"[A-Za-z0-9+/=_.-]{10,400}", value):
                raise ValueError("That does not look like a Client ID.")
            if key == "loc_map":
                value = value[:2000]
            cfg[key] = value
        password = patch.get("password", "")
        if not isinstance(password, str) or len(password) > 256 or any(c in password for c in "\r\n"):
            raise ValueError("Invalid password.")
        identity_changed = any(cfg[k] != before[k] for k in ("auth_server", "org", "client_id", "username"))
        if patch.get("clear_password") is True:
            cfg["password"], cfg["password_saved_at"] = "", ""
        elif password:
            cfg["password"], cfg["password_saved_at"] = password, date.today().isoformat()
        elif identity_changed and before["username"] and cfg["username"] != before["username"]:
            cfg["password"], cfg["password_saved_at"] = "", ""   # never reuse a password for another account
        if identity_changed or password or patch.get("clear_password"):
            cfg.update(id_token="", refresh_token="", token_at=0, expires_in=0)
        _write(cfg)
    store.audit("settings", "simphony connection", actor,
                {k: before.get(k) for k in PUBLIC_FIELDS} | {"has_password": bool(before["password"])},
                {k: cfg.get(k) for k in PUBLIC_FIELDS} | {"has_password": bool(cfg["password"])},
                "Password changed" if before["password"] != cfg["password"] else "")
    return info()


def loc_map():
    """{locRef: BC location or company code} from the Settings text '1234=SALT, 1235=LIFT'."""
    out = {}
    for part in re.split(r"[,;\n]+", _read().get("loc_map") or ""):
        if "=" in part:
            k, _, v = part.partition("=")
            if k.strip() and v.strip():
                out[k.strip()] = v.strip()
    return out


# ---------------------------------------------------------------- sign-in

def _b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _sign_in(cfg):
    """The full PKCE flow. Returns the token response."""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    base = f"{cfg['auth_server']}/oidc-provider/v1/oauth2"
    s = requests.Session()
    try:
        r = s.get(f"{base}/authorize", timeout=TIMEOUT, allow_redirects=False,
                  params={"response_type": "code", "client_id": cfg["client_id"], "scope": "openid",
                          "redirect_uri": REDIRECT_URI, "code_challenge": challenge,
                          "code_challenge_method": "S256"})
        if r.status_code >= 400:
            raise SimphonyError(f"Simphony refused the Client ID ({r.status_code}): {_msg(r)}")
        r = s.post(f"{base}/signin", timeout=TIMEOUT,
                   data={"username": cfg["username"], "password": cfg["password"], "orgname": cfg["org"]})
        try:
            body = r.json()
        except ValueError:
            body = {}
        if body.get("nextOp") == "expired":
            raise SimphonyError("The API account's password has expired. Reset it from the Reporting and "
                                "Analytics sign-in page (Can't sign in?) and save the new one in Settings.")
        if r.status_code >= 400 or not body.get("success"):
            raise SimphonyError("Simphony refused the API account name or password"
                                f" ({r.status_code}: {_msg(r)}). Too many wrong passwords lock the account "
                                "for 30 minutes.")
        code = parse_qs(urlsplit(body.get("redirectUrl") or "").query).get("code", [""])[0]
        if not code:
            raise SimphonyError("Simphony signed in but returned no authorization code.")
        r = s.post(f"{base}/token", timeout=TIMEOUT,
                   data={"scope": "openid", "grant_type": "authorization_code", "client_id": cfg["client_id"],
                         "code_verifier": verifier, "code": code, "redirect_uri": REDIRECT_URI})
        if r.status_code >= 400:
            raise SimphonyError(f"Simphony did not issue a token ({r.status_code}): {_msg(r)}")
        return r.json()
    except requests.RequestException as exc:
        raise SimphonyError(f"Could not reach Simphony at {cfg['auth_server']}: {exc}") from exc


def _refresh(cfg):
    try:
        r = requests.post(f"{cfg['auth_server']}/oidc-provider/v1/oauth2/token", timeout=TIMEOUT,
                          data={"scope": "openid", "grant_type": "refresh_token", "client_id": cfg["client_id"],
                                "refresh_token": cfg["refresh_token"], "redirect_uri": REDIRECT_URI})
    except requests.RequestException as exc:
        raise SimphonyError(f"Could not reach Simphony at {cfg['auth_server']}: {exc}") from exc
    if r.status_code >= 400:
        return None
    return r.json()


def _msg(r):
    try:
        j = r.json()
        return str(j.get("message") or j.get("error") or j)[:200]
    except ValueError:
        return (r.text or "")[:200]


def _token(force=False):
    """A valid ID token, renewed 3 days before it runs out."""
    with _auth_lock:
        with _lock:
            cfg = _read()
        if not _ready(cfg):
            raise SimphonyError("The Simphony connection is not complete in Settings.")
        now = time.time()
        expires = float(cfg.get("token_at") or 0) + float(cfg.get("expires_in") or 0)
        if not force and cfg.get("id_token") and expires - now > 3 * 86400:
            return cfg
        tokens = None
        if cfg.get("refresh_token") and now - float(cfg.get("token_at") or 0) < 27 * 86400:
            tokens = _refresh(cfg)
        if not tokens:
            tokens = _sign_in(cfg)
        with _lock:
            current = _read()
            current.update(id_token=tokens.get("id_token") or "",
                           refresh_token=tokens.get("refresh_token") or current.get("refresh_token") or "",
                           token_at=time.time(), expires_in=float(tokens.get("expires_in") or 1209600))
            _write(current)
        return current


def call(api, body=None, _retry=True):
    """POST {app_server}/bi/v1/{org}/{api}. Raises SimphonyError with a readable reason."""
    cfg = _token()
    payload = dict(body or {}, applicationName=APP_NAME)
    try:
        r = requests.post(f"{cfg['app_server']}/bi/v1/{cfg['org']}/{api}", json=payload, timeout=TIMEOUT,
                          headers={"Authorization": f"Bearer {cfg['id_token']}"})
    except requests.RequestException as exc:
        raise SimphonyError(f"Could not reach Simphony at {cfg['app_server']}: {exc}") from exc
    if r.status_code == 401 and _retry:
        _token(force=True)
        return call(api, body, _retry=False)
    if r.status_code == 204 or not (r.text or "").strip():
        return {}
    if r.status_code >= 400:
        raise SimphonyError(f"{api} returned {r.status_code}: {_msg(r)}")
    try:
        return r.json()
    except ValueError as exc:
        raise SimphonyError(f"{api} did not return JSON.") from exc


# ---------------------------------------------------------------- reads

_loc_cache = {"at": 0, "rows": None}


def locations(max_age=6 * 3600):
    if _loc_cache["rows"] is not None and time.time() - _loc_cache["at"] < max_age:
        return _loc_cache["rows"]
    rows = (call("getLocationDimensions") or {}).get("locations") or []
    out = [{"locRef": str(l.get("locRef")), "name": l.get("name") or str(l.get("locRef")),
            "currency": l.get("curr") or "", "active": l.get("active", True), "tz": l.get("tz") or "",
            "opened": l.get("openDt") or ""} for l in rows if l.get("locRef") is not None]
    _loc_cache.update(at=time.time(), rows=out)
    return out


def latest_business_date(loc_ref):
    j = call("getLatestBusDt", {"locRef": loc_ref}) or {}
    return j.get("latestBusDt")


_days_mem = {"rows": None, "dirty": 0, "saved_at": 0.0}


def _days():
    """The daily totals held, read from the file once and then kept in memory: a year of
    locations is thousands of days, and rewriting the whole file per day read was the
    slow part of a first read."""
    if _days_mem["rows"] is None:
        try:
            _days_mem["rows"] = json.loads(DAYS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _days_mem["rows"] = {}
    return _days_mem["rows"]


def _save_days(force=False):
    if not _days_mem["dirty"] or (not force and time.time() - _days_mem["saved_at"] < 15):
        return
    tmp = DAYS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(_days_mem["rows"], separators=(",", ":")), encoding="utf-8")
    tmp.replace(DAYS_FILE)
    _days_mem.update(dirty=0, saved_at=time.time())


def flush_days():
    with _days_lock:
        _save_days(force=True)


def held_day(loc_ref, bus_dt):
    """A day already read, or None; never calls Simphony."""
    with _days_lock:
        return _days().get(f"{loc_ref}|{bus_dt}")


def daily_totals(loc_ref, bus_dt, latest=None):
    """Operations daily totals of one location and business date, from the file when final."""
    key = f"{loc_ref}|{bus_dt}"
    with _days_lock:
        hit = _days().get(key)
    final_before = (date.fromisoformat(latest) - timedelta(days=2)).isoformat() if latest else None
    if hit and (hit.get("final") or time.time() - hit.get("at", 0) < 900):
        return hit
    j = call("getOperationsDailyTotals", {"locRef": loc_ref, "busDt": bus_dt}) or {}
    row = {"at": time.time(), "rvcs": j.get("revenueCenters") or [],
           "final": bool(final_before and bus_dt <= final_before)}
    with _days_lock:
        _days()[key] = row
        _days_mem["dirty"] += 1
        _save_days()
    return row


_tmed_cache = {}


def tender_dims(loc_ref, max_age=6 * 3600):
    """{tender number: {name, type, subType, cat, mstrName}} of one location (getTenderMediaDimensions)."""
    hit = _tmed_cache.get(loc_ref)
    if hit and time.time() - hit[0] < max_age:
        return hit[1]
    rows = (call("getTenderMediaDimensions", {"locRef": loc_ref}) or {}).get("tenderMedias") or []
    value = {int(t["num"]): {k: t.get(k) for k in ("name", "type", "subType", "cat", "mstrName")}
             for t in rows if t.get("num") is not None}
    _tmed_cache[loc_ref] = (time.time(), value)
    return value


def tender_totals(loc_ref, bus_dt, latest=None):
    """{tender number: [total, count]} of one location and business date (getTenderMediaDailyTotals),
    every revenue centre together; kept with the daily totals, final days are not read again."""
    key = f"T|{loc_ref}|{bus_dt}"
    with _days_lock:
        hit = _days().get(key)
    final_before = (date.fromisoformat(latest) - timedelta(days=2)).isoformat() if latest else None
    if hit and (hit.get("final") or time.time() - hit.get("at", 0) < 900):
        return hit["t"]
    j = call("getTenderMediaDailyTotals", {"locRef": loc_ref, "busDt": bus_dt}) or {}
    t = {}
    for rvc in j.get("revenueCenters") or []:
        for m in rvc.get("tenderMedias") or []:
            if m.get("tmedNum") is None:
                continue
            x = t.setdefault(str(int(m["tmedNum"])), [0.0, 0])
            x[0] += float(m.get("ttl") or 0)
            x[1] += int(m.get("cnt") or 0)
    row = {"at": time.time(), "t": t, "final": bool(final_before and bus_dt <= final_before)}
    with _days_lock:
        _days()[key] = row
        _days_mem["dirty"] += 1
        _save_days()
    return t


def _sum(rvcs, field):
    return sum(float(r.get(field) or 0) for r in rvcs)


def day_figures(rvcs):
    net = _sum(rvcs, "netSlsTtl")
    disc = abs(_sum(rvcs, "itmDscTtl")) + abs(_sum(rvcs, "subDscTtl"))
    return {"net": net, "discounts": disc, "gross": net + disc,
            "checks": int(_sum(rvcs, "chkCnt")), "guests": int(_sum(rvcs, "gstCnt")),
            "voids": abs(_sum(rvcs, "vdTtl")) + abs(_sum(rvcs, "mngrVdTtl")),
            "returns": abs(_sum(rvcs, "rtnTtl")), "service": _sum(rvcs, "svcTtl"),
            "tax": _sum(rvcs, "taxCollTtl"),
            "open_checks": int(_sum(rvcs, "chkOpnCnt")), "open_value": _sum(rvcs, "chkOpnTtl"),
            "error_corrects": abs(_sum(rvcs, "errCorTtl"))}
