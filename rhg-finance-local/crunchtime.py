"""Crunchtime Ops Execution (formerly Zenput): RHG's checklists, food safety logs, waste and yield forms and the
temperature sensors, only read (GET calls only: no form, task, location or user is ever written).

API: https://ops-execution.crunchtime.com/api with the header X-API-TOKEN (the token of an Ops Execution user, on
its Settings page). The token is typed in Finance → Operations & Food Safety → Connection by someone with the
Settings permission and stays in data/crunchtime.json on the server (0600); the screen only learns whether it is set.

Read (RHG sh.p.k, audit 15 Nov 2026): 240 form templates (Operations 193, Safety 18, HR 6…), 15 locations, 16 Monnit
sensors (walk-in coolers and freezers, readings in °F), 3,200 tasks. Endpoints:
  * /v1/forms/list_templates/ — the forms, their category and submission counts;
  * /v3/submissions/?form_template_id&date_start&date_end&limit&offset — the submissions of a form (100 a page);
  * /v3/tasks/?status&date_due_start&date_due_end — the tasks (checklists due) and whether they were done, late;
  * /v2/sensors/sensors/ — each sensor's last reading, online, battery (no history in the public API);
  * /v3/locations/ — the locations.
What is kept of a submission is what the reports need: the location, the local date, the form, and the answers that
are measurements or findings (temperatures, yes/no, quantities, product, supplier, invoice number, reason). The
submitter's name, signatures, photos and free text about people are not kept.
"""
import json
import os
import re
import tempfile
import threading
import time
from datetime import date, datetime, timedelta

import requests

import store

CONFIG_FILE = store.DATA_DIR / "crunchtime.json"
CACHE_DIR = store.DATA_DIR / "crunchtime"
BASE = "https://ops-execution.crunchtime.com/api"
TIMEOUT = 60
PAGE = 100
DEFAULTS = {"token": "", "token_saved_at": "", "map": "", "thresholds": ""}
PUBLIC = ("map", "thresholds")
# Crunchtime location → restaurant of Finance → Settings → Location intelligence (its reference); a line of the screen
# replaces one. Warehouse, central kitchen and head office have no restaurant: shown apart, as support.
DEFAULT_MAP = {"SALT REST": "SALT", "LIFT RESTAURANT": "LIFT", "VESPER": "VES", "OPA BLLOK": "OPA_B",
               "OPA ARENA CENTER": "OPA_A", "OPA -9 KATËSHET": "OPA_9KAT", "OPA QTU": "OPA_Q", "OPA TEG": "OPA_TE",
               "OPA-SHKODER": "OPA_SK", "LE DISTRICT TIRANA INTERNATIONAL AIRPORT": "OPA_TI", "LE - DISTRICT": "District",
               "SALT KOSOVE": "SALTPRIS"}
# RHG's own limits, as its Temperature Logs form asks them: a cooler above 5 °C, a freezer above −10 °C is out.
DEFAULT_LIMITS = {"cooler_max": 5.0, "freezer_max": -10.0, "hot_min": 63.0}
_lock, _cache_lock = threading.Lock(), threading.Lock()
_mem = {}


class Error(Exception):
    pass


# ---------------------------------------------------------------- the connection
def _read():
    try:
        d = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return dict(DEFAULTS, **(d if isinstance(d, dict) else {}))
    except (OSError, ValueError):
        return dict(DEFAULTS)


def _write(cfg):
    fd, tmp = tempfile.mkstemp(prefix=".crunchtime-", dir=store.DATA_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(cfg, out, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, CONFIG_FILE)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def info():
    with _lock:
        cfg = _read()
    return {k: cfg.get(k, "") for k in PUBLIC} | {"has_token": bool(cfg.get("token")),
                                                   "token_saved_at": cfg.get("token_saved_at") or "",
                                                   "configured": bool(cfg.get("token")), "default_map": DEFAULT_MAP,
                                                   "limits": limits(cfg.get("thresholds"))}


def limits(text):
    """The temperature limits: the defaults, then lines "cooler_max = 5" (°C)."""
    out = dict(DEFAULT_LIMITS)
    for line in str(text or "").splitlines():
        line = line.split("#", 1)[0]
        if "=" in line:
            k, _, v = line.partition("=")
            k = k.strip().lower()
            if k in out:
                try:
                    out[k] = float(v.strip().replace(",", "."))
                except ValueError as exc:
                    raise ValueError(f"Limits: {k} must be a number.") from exc
    return out


def save(patch, actor):
    if not isinstance(patch, dict):
        raise ValueError("The settings must be an object.")
    with _lock:
        cfg = _read()
        for k in ("map", "thresholds"):
            if k in patch:
                cfg[k] = str(patch[k] or "")[:8000]
        limits(cfg["thresholds"])
        tok = patch.get("token", "")
        if not isinstance(tok, str) or len(tok) > 200 or not re.fullmatch(r"[A-Za-z0-9_-]*", tok):
            raise ValueError("The API token is one word of letters and digits.")
        if tok:
            cfg["token"] = tok
            cfg["token_saved_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        _write(cfg)
    with _cache_lock:
        _mem.clear()
    store.audit("settings", "crunchtime", actor, None, {"token_changed": bool(tok),
                "map_lines": len([x for x in cfg["map"].splitlines() if x.strip()])}, "")
    return info()


def get(path, params=None):
    """One GET of the API. Nothing else is ever sent."""
    with _lock:
        tok = _read().get("token")
    if not tok:
        raise Error("The Crunchtime connection is not set: Operations & Food Safety → Connection.")
    try:
        r = requests.get(BASE + path, params=params or {}, timeout=TIMEOUT,
                         headers={"X-API-TOKEN": tok, "Accept": "application/json"})
    except requests.RequestException as exc:
        raise Error(f"Crunchtime could not be reached: {str(exc)[:160]}") from exc
    if r.status_code in (401, 403):
        raise Error("Crunchtime refused the API token.")
    if r.status_code != 200:
        raise Error(f"Crunchtime answered {r.status_code} on {path}.")
    try:
        return r.json()
    except ValueError as exc:
        raise Error("Crunchtime did not answer with JSON.") from exc


def _cached(key, max_age, fn):
    with _cache_lock:
        hit = _mem.get(key)
    if hit and time.time() - hit[0] < max_age:
        return hit[1]
    v = fn()
    with _cache_lock:
        _mem[key] = (time.time(), v)
    return v


def templates(max_age=1800):
    return _cached("templates", max_age, lambda: (get("/v1/forms/list_templates/", {"count": "true", "limit": 1000})
                                                  .get("results") or []))


def locations(max_age=3600):
    def go():
        j = get("/v3/locations/", {"limit": 500})
        return [{"id": x.get("id"), "name": (x.get("name") or "").strip(), "tags": [t.get("name") for t in x.get("tags") or []]}
                for x in j.get("data") or []]
    return _cached("locations", max_age, go)


def sensors(max_age=300):
    def go():
        out, off = [], 0
        while True:
            j = get("/v2/sensors/sensors/", {"limit": 100, "offset": off})
            d = j.get("data") or []
            out += d
            off += len(d)
            if not d or off >= ((j.get("meta") or {}).get("count") or 0):
                break
        return out
    return _cached("sensors", max_age, go)


def test():
    with _cache_lock:
        _mem.clear()
    T, L, S = templates(), locations(), sensors()
    return {"ok": True, "forms": len(T), "locations": [x["name"] for x in L], "sensors": len(S)}


# ---------------------------------------------------------------- submissions and tasks, by month
def _months(a, b):
    out, cur = [], date(a.year, a.month, 1)
    while cur <= b:
        nxt = date(cur.year + (cur.month == 12), cur.month % 12 + 1, 1)
        out.append((max(cur, a), min(nxt - timedelta(days=1), b), cur.strftime("%Y-%m")))
        cur = nxt
    return out


def _disk(name):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / name


def _held(name, a, b, fn, today=None):
    """A month's read kept on disk: re-read after 15 minutes while it is recent (the last 10 days), else kept."""
    today = today or date.today()
    f = _disk(f"{name}_{a.isoformat()}_{b.isoformat()}.json")
    recent = (today - b).days <= 10
    try:
        st = f.stat()
        if not recent or time.time() - st.st_mtime < 900:
            return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    v = fn()
    try:
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(v, separators=(",", ":")), encoding="utf-8")
        tmp.replace(f)
    except OSError:
        pass
    return v


def _pages(path, params, cap=20000):
    out, off = [], 0
    while True:
        j = get(path, dict(params, limit=PAGE, offset=off))
        d = j.get("data") or []
        out += d
        off += len(d)
        if not d or off >= min(cap, (j.get("meta") or {}).get("count") or 0):
            break
    return out


KEEP_TYPES = {"number", "temperature", "yesno", "select", "account", "formula", "range", "equipment_temperature",
              "ingredient_temperature", "sensor_temperature", "datetime"}
KEEP_TEXT = re.compile(r"produkt|product|furnitor|supplier|fatur|invoice|arsye|reason|problem|pajisje|equipment|"
                       r"masa|measure", re.I)


def slim(s):
    """What is kept of a submission: [location id, location name, local date-time, task?, answers]; answers are
    [type, title (no HTML), value] for measurements and findings, and the text of products, suppliers, invoices,
    reasons and equipment numbers. Names, signatures and photos are dropped."""
    sm = s.get("smetadata") or {}
    loc = sm.get("location") or {}
    ans = []
    for x in s.get("answers") or []:
        t = x.get("field_type")
        title = re.sub(r"<[^>]+>", " ", x.get("title") or "")
        title = re.sub(r"\s+", " ", title).strip()[:240]
        if t in KEEP_TYPES or (t == "text" and KEEP_TEXT.search(title) and not re.search(r"name|emri i|submitter|menaxher|pranoi|perpunoi", title, re.I)):
            v = x.get("value")
            if t == "datetime":
                continue
            if t == "account" and not loc:
                loc = {"id": v}
            ans.append([t, title, v])
    return [loc.get("id"), (loc.get("name") or "").strip(), (sm.get("date_submitted_local") or sm.get("date_submitted") or "")[:16],
            bool(sm.get("task")), ans]


def submissions(template_id, a, b):
    """[slim submission] of one form in a..b (local dates), read month by month."""
    out = []
    for x, y, _ in _months(a, b):
        out += _held(f"sub{template_id}", x, y, lambda x=x, y=y: [slim(s) for s in _pages(
            "/v3/submissions/", {"form_template_id": template_id, "date_start": x.isoformat(), "date_end": y.isoformat()})])
    return out


def tasks(a, b):
    """The tasks due in a..b: [location id, location name, title, project id, due (local date), done?, late?, open?]."""
    out = []
    for x, y, _ in _months(a, b):
        def go(x=x, y=y):
            rows = _pages("/v3/tasks/", {"status": "all_tasks", "date_due_start": x.isoformat(), "date_due_end": y.isoformat()})
            return [[(t.get("account") or {}).get("id"), ((t.get("account") or {}).get("name") or "").strip(),
                     (t.get("title") or "")[:160], (t.get("project") or {}).get("id"), (t.get("date_due") or "")[:16],
                     t.get("current_state") == "complete" or bool(t.get("is_closed")), bool(t.get("is_completed_late")),
                     bool(t.get("is_active")) and not t.get("is_closed") and not t.get("deleted"), bool(t.get("deleted"))]
                    for t in rows]
        out += _held("tasks", x, y, go)
    return out


def mapping(text):
    m = {k.upper(): v for k, v in DEFAULT_MAP.items()}
    for line in str(text or "").splitlines():
        line = line.split("#", 1)[0]
        if "=" in line:
            k, _, v = line.rpartition("=")
            if k.strip():
                m[k.strip().upper()] = "" if v.strip() in ("", "-") else v.strip()
    return m
