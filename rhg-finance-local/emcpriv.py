"""Control → EMC privileges: what one Simphony user can do in EMC and at the POS against another.

Read from the Simphony Configuration and Content API (CCAPI), only read (get calls, never add, update or delete):
  * getEmployees: each employee's name, EMC user name and roles, each role at a level of the hierarchy (enterprise,
    property, revenue centre); the PIN and the other personal fields Simphony returns are dropped on reading and
    never kept;
  * getRoles: each role's EMC control level, its EMC modules with view / edit / add / delete, its field controls
    (editable / view only / excluded), its actions and operations privileges (the POS: voids, returns, reopening
    closed checks, discounts…), and the hierarchy it sees.
A user's privileges are those of all of their roles together (a right any role gives, the user has). Two users are
compared privilege by privilege: only the first, only the second, both, or different (a module one can only view
and the other can edit).

The CCAPI needs its own API account, made in Reporting and Analytics (API Account Type: Simphony Configuration
API), signed in the same way as the BI API account of Settings → Simphony (OpenID Connect with PKCE). The account is
entered on the screen by someone with the Settings permission; the password and the tokens stay in
data/simphony_cc.json on the server and the screen only learns whether they are set.
"""
import gzip
import json
import os
import re
import tempfile
import threading
import time
from datetime import date, datetime

import requests

import heldjob
import simphony
import store

CONFIG_FILE = store.DATA_DIR / "simphony_cc.json"
PUBLIC = ("auth_server", "cc_server", "org", "client_id", "username")
DEFAULTS = {"auth_server": "", "cc_server": "", "org": "", "client_id": "", "username": "", "password": "",
            "password_saved_at": "", "id_token": "", "refresh_token": "", "token_at": 0, "expires_in": 0}
KEEP = 6 * 3600
TIMEOUT = 90
PAGE = 200
# the employee fields kept: nothing else (the PIN, IDs, payroll, fingerprints are dropped as they come)
EMP_KEEP = ("objectNum", "firstName", "lastName", "checkName", "userName", "email", "isDeleted", "terminationDate",
            "level", "group", "roles", "emcVisibility")
_lock, _auth_lock = threading.Lock(), threading.Lock()
_held = heldjob.Held("emcpriv", KEEP)
_URL = re.compile(r"^https://[A-Za-z0-9.-]+(:\d+)?/?$")


class Error(Exception):
    pass


# ---------------------------------------------------------------- the account
def _read():
    try:
        d = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return dict(DEFAULTS, **(d if isinstance(d, dict) else {}))
    except (OSError, ValueError):
        return dict(DEFAULTS)


def _write(cfg):
    fd, tmp = tempfile.mkstemp(prefix=".simphony-cc-", dir=store.DATA_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(cfg, out, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, CONFIG_FILE)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _effective(cfg):
    """The account, with the authentication server and enterprise of Settings → Simphony when left blank."""
    bi = simphony.info()
    return dict(cfg, auth_server=cfg.get("auth_server") or bi.get("auth_server") or "", org=cfg.get("org") or bi.get("org") or "")


def info():
    with _lock:
        cfg = _read()
    eff = _effective(cfg)
    missing = [label for k, label in (("auth_server", "Authentication server"), ("cc_server", "Configuration API server"),
                                      ("org", "Enterprise short name"), ("client_id", "Client ID"),
                                      ("username", "API account name"), ("password", "Password")) if not eff.get(k)]
    return {k: cfg.get(k, "") for k in PUBLIC} | {"auth_default": simphony.info().get("auth_server") or "",
                                                  "org_default": simphony.info().get("org") or "",
                                                  "configured": not missing, "missing": missing,
                                                  "has_password": bool(cfg.get("password")),
                                                  "password_saved_at": cfg.get("password_saved_at") or "",
                                                  "signed_in": bool(cfg.get("id_token"))}


def save(patch, actor):
    if not isinstance(patch, dict):
        raise ValueError("The settings must be an object.")
    with _lock:
        before = _read()
        cfg = dict(before)
        for k in PUBLIC:
            if k not in patch:
                continue
            v = patch[k]
            if not isinstance(v, str):
                raise ValueError(f"{k} must be text.")
            v = v.strip()
            if k in ("auth_server", "cc_server") and v:
                if not _URL.match(v):
                    raise ValueError("A server is the https:// address only, without a path.")
                v = v.rstrip("/")
            if k == "org" and v and not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", v):
                raise ValueError("The enterprise short name is letters and digits only.")
            if k == "client_id" and v and not re.fullmatch(r"[A-Za-z0-9+/=_.-]{10,400}", v):
                raise ValueError("That does not look like a Client ID.")
            if k == "username":
                v = v[:120]
            cfg[k] = v
        pw = patch.get("password", "")
        if not isinstance(pw, str) or len(pw) > 256 or any(c in pw for c in "\r\n"):
            raise ValueError("Invalid password.")
        changed = any(cfg[k] != before[k] for k in ("auth_server", "org", "client_id", "username"))
        if patch.get("clear_password") is True:
            cfg["password"], cfg["password_saved_at"] = "", ""
        elif pw:
            cfg["password"], cfg["password_saved_at"] = pw, date.today().isoformat()
        elif changed and before["username"] and cfg["username"] != before["username"]:
            cfg["password"], cfg["password_saved_at"] = "", ""          # never another account's password
        if changed or pw or patch.get("clear_password"):
            cfg.update(id_token="", refresh_token="", token_at=0, expires_in=0)
        _write(cfg)
    store.audit("settings", "simphony configuration api", actor,
                {k: before.get(k) for k in PUBLIC} | {"has_password": bool(before["password"])},
                {k: cfg.get(k) for k in PUBLIC} | {"has_password": bool(cfg["password"])},
                "Password changed" if before["password"] != cfg["password"] else "")
    _held.forget()
    return info()


def _token(force=False):
    with _auth_lock:
        with _lock:
            cfg = _read()
        eff = _effective(cfg)
        if not all(eff.get(k) for k in ("auth_server", "cc_server", "org", "client_id", "username", "password")):
            raise Error("The Configuration API account is not complete: fill it in on this screen (Connection).")
        now = time.time()
        if not force and cfg.get("id_token") and float(cfg.get("token_at") or 0) + float(cfg.get("expires_in") or 0) - now > 3 * 86400:
            return eff | {"id_token": cfg["id_token"]}
        tokens = None
        try:
            if cfg.get("refresh_token") and now - float(cfg.get("token_at") or 0) < 27 * 86400:
                tokens = simphony._refresh(eff)
            if not tokens:
                tokens = simphony._sign_in(eff)                 # the same OpenID Connect flow as the BI API account
        except simphony.SimphonyError as exc:
            raise Error(str(exc).replace("Reporting and Analytics sign-in page", "Reporting and Analytics sign-in page "
                                         "(the Configuration API account)")) from exc
        with _lock:
            cur = _read()
            cur.update(id_token=tokens.get("id_token") or "", refresh_token=tokens.get("refresh_token") or cur.get("refresh_token") or "",
                       token_at=time.time(), expires_in=float(tokens.get("expires_in") or 1209600))
            _write(cur)
        return eff | {"id_token": cur["id_token"]}


def _post(path, body, _retry=True):
    cfg = _token()
    try:
        r = requests.post(f"{cfg['cc_server']}/config/sim/{path}", json=body, timeout=TIMEOUT,
                          headers={"Authorization": f"Bearer {cfg['id_token']}", "Simphony-OrgShortName": cfg["org"],
                                   "Content-Type": "application/json"})
    except requests.RequestException as exc:
        raise Error(f"Could not reach the Configuration API at {cfg['cc_server']}: {exc}") from exc
    if r.status_code == 401 and _retry:
        _token(force=True)
        return _post(path, body, False)
    if r.status_code == 405 or (r.status_code >= 400 and "text/html" in (r.headers.get("Content-Type") or "")):
        # not the Configuration API: usually the BI API's application server or the sign-in server was entered
        raise Error(f"{path} answered {r.status_code}: {cfg['cc_server']} is not the Configuration API. The Configuration "
                    "API server is the Simphony base URL shown in EMC → Enterprise → Enterprise Parameters (for example "
                    "https://…-ohsim.oracleindustry.com), not the Reporting and Analytics, BI API or sign-in address.")
    if r.status_code >= 400:
        raise Error(f"{path} answered {r.status_code}: {simphony._msg(r)}")
    try:
        return r.json()
    except ValueError as exc:
        raise Error(f"{path} did not return JSON.") from exc


def _all(paths, body, progress, what, clean=None):
    """Every page of a get call (offset / limit / hasMore); the first of the paths (v1, then v2) that answers."""
    last = None
    for path in paths:
        out, offset = [], 0
        try:
            while True:
                j = _post(path, dict(body, offset=offset, limit=PAGE))
                items = j.get("items") if isinstance(j, dict) else j
                items = [x for x in (items or []) if isinstance(x, dict)]
                out += [clean(x) for x in items] if clean else items
                progress(f"{what}: {len(out):,}")
                if not (isinstance(j, dict) and j.get("hasMore")) or not items:
                    return out
                offset += len(items)
        except Error as exc:
            last = exc
            if " answered 404" not in str(exc):
                raise
    raise last or Error(f"{what} could not be read.")


def _emp(x):
    e = {k: x.get(k) for k in EMP_KEEP if k in x}
    e["roles"] = [{k: r.get(k) for k in ("roleObjNum", "roleName", "hierUnitId", "hierUnitObjNum", "hierUnitName")}
                  for r in (x.get("roles") or []) if isinstance(r, dict)]
    return e


# ---------------------------------------------------------------- the read
def build(progress=lambda s: None):
    progress("signing in to the Configuration API")
    try:
        roles = _all(["v1/employees/getRoles", "v2/employees/getRoles"], {"includeAll": "detail"}, progress, "roles")
    except Error as exc:                                   # an older release without includeAll: the default detail
        if " answered 400" not in str(exc):
            raise
        roles = _all(["v1/employees/getRoles", "v2/employees/getRoles"], {}, progress, "roles")
    emps = _all(["v1/employees/getEmployees", "v2/employees/getEmployees2"],
                {"include": ",".join(EMP_KEEP)}, progress, "employees", _emp)
    return model(emps, roles) | {"available": True, "read_at": datetime.now().isoformat(timespec="seconds")}


def _rights(m):
    p = m.get("privilegesExt") or {}
    out = {k for k in ("view", "edit", "add", "delete") if p.get(k) is True}
    if not out and isinstance(m.get("privileges"), int):        # a bit mask when the extended form is absent
        bits = m["privileges"]
        out = {k for i, k in enumerate(("view", "edit", "add", "delete")) if bits & (1 << i)}
    return out


def _field(c):
    p = c.get("privilegesExt") or {}
    return "editable" if p.get("editable") else "view only" if p.get("viewOnly") else "excluded" if p.get("exclude") else ""


def model(emps, roles):
    """The roles as privilege maps, and the employees with their roles: what the screen compares."""
    R = {}
    for r in roles:
        key = str(r.get("objectNum"))
        priv = {}
        for m in r.get("emcModules") or []:
            rt = _rights(m)
            if rt:
                priv[f"emc:{m.get('moduleNum')}"] = sorted(rt)
        for kind in ("operations", "actions"):
            for o in r.get(kind) or []:
                if o.get("enable") in (True, 1, "true", "True"):
                    priv[f"{'op' if kind == 'operations' else 'act'}:{o.get('privilegeNum')}"] = True
        for f in r.get("fields") or []:
            for c in f.get("controls") or []:
                lv = _field(c)
                if lv:
                    priv[f"fld:{f.get('moduleNum')}:{c.get('controlNum')}"] = lv
        for v in r.get("visibility") or []:
            priv[f"vis:{v.get('hierUnitId') or v.get('hierUnitObjNum')}"] = True
        R[key] = {"num": r.get("objectNum"), "name": r.get("name") or "", "level": r.get("emcControlLevel"),
                  "rvc_security": bool(r.get("enableRVCLevelSecurity")), "priv": priv}
    names = {}
    for r in roles:
        for m in r.get("emcModules") or []:
            names[f"emc:{m.get('moduleNum')}"] = ("EMC module", m.get("moduleName") or f"Module {m.get('moduleNum')}")
        for o in r.get("operations") or []:
            names[f"op:{o.get('privilegeNum')}"] = ("Operations", o.get("name") or f"Privilege {o.get('privilegeNum')}")
        for o in r.get("actions") or []:
            names[f"act:{o.get('privilegeNum')}"] = ("Actions", o.get("name") or f"Privilege {o.get('privilegeNum')}")
        for f in r.get("fields") or []:
            for c in f.get("controls") or []:
                names[f"fld:{f.get('moduleNum')}:{c.get('controlNum')}"] = (
                    "Field", f"{f.get('moduleName') or f.get('moduleNum')} · {c.get('controlName') or c.get('controlNum')}")
        for v in r.get("visibility") or []:
            names[f"vis:{v.get('hierUnitId') or v.get('hierUnitObjNum')}"] = (
                "Visibility", v.get("hierUnitName") or f"Hierarchy {v.get('hierUnitObjNum') or v.get('hierUnitId')}")
    E = []
    for e in emps:
        if e.get("isDeleted"):
            continue
        rs = [r for r in e.get("roles") or [] if str(r.get("roleObjNum")) in R]
        E.append({"num": e.get("objectNum"), "name": " ".join(x for x in (e.get("firstName"), e.get("lastName")) if x) or e.get("checkName") or "",
                  "user": e.get("userName") or "", "emc": bool(e.get("userName")), "ended": e.get("terminationDate") or "",
                  "roles": [{"num": r.get("roleObjNum"), "name": r.get("roleName") or R[str(r.get("roleObjNum"))]["name"],
                             "where": r.get("hierUnitName") or str(r.get("hierUnitObjNum") or "")} for r in rs]})
    E.sort(key=lambda x: (not x["emc"], x["name"].lower()))
    return {"roles": R, "names": names, "employees": E,
            "counts": {"employees": len(E), "emc_users": sum(1 for e in E if e["emc"]), "roles": len(R)}}


RANK = {"view": 1, "add": 2, "edit": 3, "delete": 4}
FLD = {"excluded": 0, "view only": 1, "editable": 2}


def effective(m, emp):
    """A user's privileges: every right of any of their roles (the widest wins)."""
    out = {}
    for r in emp["roles"]:
        for k, v in (m["roles"].get(str(r["num"])) or {}).get("priv", {}).items():
            if isinstance(v, list):
                out[k] = sorted(set(out.get(k) or []) | set(v), key=lambda x: RANK.get(x, 9))
            elif isinstance(v, str):
                cur = out.get(k)
                out[k] = v if cur is None or FLD.get(v, -1) > FLD.get(cur, -1) else cur
            else:
                out[k] = True
    return out


def compare(m, a, b):
    """Privilege by privilege: only a, only b, both, different."""
    A, B = effective(m, a), effective(m, b)
    rows = []
    for k in sorted(set(A) | set(B), key=lambda k: (k.split(":")[0], (m["names"].get(k) or ("", k))[1].lower())):
        va, vb = A.get(k), B.get(k)
        st = "a" if vb is None else "b" if va is None else "same" if va == vb else "diff"
        cat, name = m["names"].get(k) or ("Other", k)
        rows.append({"key": k, "cat": cat, "name": name, "a": va, "b": vb, "st": st})
    return rows


# ---------------------------------------------------------------- held and requested
def request(refresh=False):
    if not info()["configured"]:
        return {"available": False, "setup": True, "error": "The Configuration API account is not set yet."}
    return _held.request("all", lambda p: build(p), refresh,
                         note="Reading the employees and roles from the Simphony Configuration API…")


def check():
    """Connection check: signs in and reads one page of roles (read only)."""
    j = _post("v1/employees/getRoles", {"offset": 0, "limit": 1})
    n = j.get("totalResults") if isinstance(j, dict) else None
    return {"ok": True, "roles": n}
