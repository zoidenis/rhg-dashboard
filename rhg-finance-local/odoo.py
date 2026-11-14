"""Odoo (rhg-group.odoo.com): the hours worked in each restaurant, only read, to set against Simphony's sales.

RHG's Odoo holds the people side only (Odoo 16 Enterprise: Employees, Attendances, Planning, Payroll, Contracts;
no Point of Sale, no Sales, no Accounting; read 14 Nov 2026). What the tower takes from it:
  * hr.attendance: every check-in / check-out (live, since Dec 2024) with the restaurant it counts for (rhg_unit_id,
    the top department of the unit), its review state (rhg_state) and whether it is overtime (rhg_is_overtime);
  * planning.slot: the shifts planned (allocated hours) by restaurant, absences apart (rhg_absence_kind);
  * hr.employee → hr.department: only to tell front of house, back of house and management apart.
The hours are summed as soon as they are read, by restaurant, business day, hour and section; no name, employee
number, wage or contract is kept or sent to a screen. An attendance rejected in the review is left out of the hours.

The connection: Odoo's external API (JSON-RPC, /jsonrpc) with an Odoo user's login and an API key (Odoo → My Profile →
Account Security → New API Key). The key is typed in Finance → Sales → Labour → Connection by someone with the Settings
permission and stays in data/odoo.json on the server (0600); the screen only learns whether it is set. Only the read
methods below are ever called: nothing is written to Odoo.

Odoo keeps times in UTC; they are taken to Tirana time. A business day starts at DAY_START (05:00): the hours worked
after midnight count for the day before, as Simphony counts its sales.
"""
import json
import os
import re
import tempfile
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

import store

CONFIG_FILE = store.DATA_DIR / "odoo.json"
DAYS_FILE = store.DATA_DIR / "odoo_days.json"
TZ = ZoneInfo("Europe/Tirane")
DAY_START = 5
TIMEOUT = 60
READ_METHODS = {"search_read", "read_group", "search_count", "fields_get", "read"}
SECTIONS = ("FOH", "BOH", "MGMT", "Other")
DEFAULTS = {"url": "", "db": "", "login": "", "key": "", "key_saved_at": "", "map": "", "thresholds": ""}
PUBLIC = ("url", "db", "login", "map", "thresholds")
# Odoo restaurant (rhg_unit_id) → restaurant of Finance → Settings → Location intelligence (its reference). A line in
# the mapping of the screen replaces any of these; a unit with no restaurant (the warehouse, the central kitchen, the
# head office) is shown apart, as support.
DEFAULT_MAP = {"SALT TIRANA": "SALT", "SALT PRISHTINA": "SALTPRIS", "SALT GREEN COAST": "SALT_GC", "LIFT": "LIFT",
               "VESPER": "VES", "SIN": "SIN", "LE DISTRICT": "District", "LE DISTRICT TIA": "OPA_TI",
               "OPA BLLOK": "OPA_B", "OPA ARENA": "OPA_A", "OPA 9 KATESHET": "OPA_9KAT", "OPA QTU": "OPA_Q",
               "OPA TEG": "OPA_TE", "OPA SHKODER": "OPA_SK"}
LONG_H = 14                     # an attendance longer than this is counted, and flagged to check (a check-out closed late)
OPEN_FORGOTTEN_H = 16           # an attendance still open this long after its check-in is a forgotten check-out
FINAL_AFTER = 8                 # days: reviews and corrections are done within a week; then a day is not read again
_lock, _days_lock, _uid_lock = threading.Lock(), threading.Lock(), threading.Lock()
_uid = {"key": None, "uid": None}
_mem = {"days": None, "dirty": 0}
_sections = {"at": 0.0, "emp": {}, "units": {}}


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
    fd, tmp = tempfile.mkstemp(prefix=".odoo-", dir=store.DATA_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(cfg, out, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, CONFIG_FILE)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def info():
    """What the screen may see: never the key."""
    with _lock:
        cfg = _read()
    return {k: cfg.get(k, "") for k in PUBLIC} | {
        "has_key": bool(cfg.get("key")), "key_saved_at": cfg.get("key_saved_at") or "",
        "configured": bool(cfg.get("url") and cfg.get("db") and cfg.get("login") and cfg.get("key")),
        "default_map": DEFAULT_MAP}


def save(patch, actor):
    """The connection and the mapping; the key only when typed (empty keeps it)."""
    if not isinstance(patch, dict):
        raise ValueError("The settings must be an object.")
    with _lock:
        cfg = _read()
        before = (cfg["url"], cfg["db"], cfg["login"], cfg["key"])
        if "url" in patch:
            v = str(patch["url"] or "").strip().rstrip("/")
            if v and not re.fullmatch(r"https://[A-Za-z0-9.-]+(:\d+)?", v):
                raise ValueError("The Odoo address is https:// and the host, e.g. https://rhg-group.odoo.com.")
            cfg["url"] = v
        if "db" in patch:
            v = str(patch["db"] or "").strip()
            if v and not re.fullmatch(r"[A-Za-z0-9._-]{1,120}", v):
                raise ValueError("That does not look like an Odoo database name.")
            cfg["db"] = v
        if "login" in patch:
            v = str(patch["login"] or "").strip()
            if len(v) > 200 or any(c in v for c in "\r\n"):
                raise ValueError("The login is one line.")
            cfg["login"] = v
        if "map" in patch:
            cfg["map"] = str(patch["map"] or "")[:8000]
        if "thresholds" in patch:
            cfg["thresholds"] = str(patch["thresholds"] or "")[:4000]
            thresholds(cfg["thresholds"])          # refuses a line it cannot read
        key = patch.get("key", "")
        if not isinstance(key, str) or len(key) > 400 or any(c in key for c in "\r\n "):
            raise ValueError("The API key is one word, on one line.")
        if key:
            cfg["key"] = key
            cfg["key_saved_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        if (cfg["url"], cfg["db"], cfg["login"], cfg["key"]) != before:
            with _uid_lock:
                _uid.update(key=None, uid=None)
        _write(cfg)
    store.audit("settings", "odoo", actor, None, {k: cfg.get(k) for k in ("url", "db", "login")}
                | {"key_changed": bool(key), "map_lines": len([x for x in cfg["map"].splitlines() if x.strip()])}, "")
    return info()


def _rpc(cfg, service, method, args):
    try:
        r = requests.post(cfg["url"] + "/jsonrpc", timeout=TIMEOUT,
                          json={"jsonrpc": "2.0", "method": "call", "id": int(time.time()),
                                "params": {"service": service, "method": method, "args": args}})
    except requests.RequestException as exc:
        raise Error(f"Odoo could not be reached: {str(exc)[:160]}") from exc
    if r.status_code != 200:
        raise Error(f"Odoo answered {r.status_code}.")
    try:
        j = r.json()
    except ValueError as exc:
        raise Error("Odoo did not answer with JSON: check the address.") from exc
    if j.get("error"):
        e = j["error"]
        msg = ((e.get("data") or {}).get("message") or e.get("message") or "error").strip()
        raise Error(f"Odoo: {msg[:240]}")
    return j.get("result")


def _session():
    with _lock:
        cfg = _read()
    if not (cfg["url"] and cfg["db"] and cfg["login"] and cfg["key"]):
        raise Error("The Odoo connection is not set: Sales → Labour → Connection.")
    sig = (cfg["url"], cfg["db"], cfg["login"], cfg["key"])
    with _uid_lock:
        if _uid["key"] != sig:
            uid = _rpc(cfg, "common", "login", [cfg["db"], cfg["login"], cfg["key"]])
            if not uid:
                raise Error("Odoo refused the login and API key.")
            _uid.update(key=sig, uid=uid)
        return cfg, _uid["uid"]


def call(model, method, args=None, kwargs=None):
    """One read of Odoo. Only the read methods: anything else is refused here, before it leaves the tower."""
    if method not in READ_METHODS:
        raise Error(f"Odoo method {method} is not a read: refused.")
    cfg, uid = _session()
    return _rpc(cfg, "object", "execute_kw", [cfg["db"], uid, cfg["key"], model, method, args or [], kwargs or {}])


def test():
    """Signs in afresh, says who and what it can read (counts only)."""
    with _uid_lock:
        _uid.update(key=None, uid=None)
    cfg, uid = _session()
    out = {"ok": True, "uid": uid, "version": (_rpc(cfg, "common", "version", []) or {}).get("server_version")}
    for model, name in (("hr.attendance", "attendances"), ("planning.slot", "planned shifts"), ("hr.employee", "employees")):
        try:
            out[name] = call(model, "search_count", [[]])
        except Error as exc:
            out[name] = None
            out.setdefault("warnings", []).append(f"{name}: {exc}")
    out["units"] = sorted(units().values())
    return out


# ---------------------------------------------------------------- units and sections
def _section(path):
    p = (path or "").upper()
    if "BACK OF THE HOUSE" in p or "/ BOH" in p or p.endswith("BOH") or "KITCHEN" in p or "KUZHIN" in p:
        return "BOH"
    if "FRONT OF THE HOUSE" in p or "/ FOH" in p or p.endswith("FOH") or "SERVICE" in p:
        return "FOH"
    if "MANAGEMENT" in p:
        return "MGMT"
    return "Other"


def _load_sections(max_age=6 * 3600):
    """{employee id: section} and {unit department id: name}, kept six hours. The employee ids stay here, in memory."""
    if time.time() - _sections["at"] < max_age and _sections["emp"]:
        return _sections
    deps = call("hr.department", "search_read", [[]], {"fields": ["complete_name", "parent_id"],
                                                         "context": {"active_test": False}})
    path = {d["id"]: d.get("complete_name") or "" for d in deps}
    unit_names = {d["id"]: (d.get("complete_name") or "").strip() for d in deps if not d.get("parent_id")}
    emps = call("hr.employee", "search_read", [[["active", "in", [True, False]]]],
                {"fields": ["department_id"], "context": {"active_test": False}})
    emp = {e["id"]: _section(path.get((e.get("department_id") or [None])[0])) for e in emps}
    _sections.update(at=time.time(), emp=emp, units=unit_names)
    return _sections


def units():
    return dict(_load_sections()["units"])


# ---------------------------------------------------------------- time
def _utc(s):
    return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) if s else None


def _bounds(day):
    """The business day in UTC: DAY_START local to DAY_START the next day."""
    a = datetime(day.year, day.month, day.day, DAY_START, tzinfo=TZ)
    return a.astimezone(timezone.utc), (a + timedelta(days=1)).astimezone(timezone.utc)


def _odoo(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _spread(start, end, add):
    """add(business day, clock hour, hours) for each local clock hour that start..end (aware datetimes) covers."""
    t = start
    while t < end:
        loc = t.astimezone(TZ)
        nxt = (loc.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)).astimezone(timezone.utc)
        stop = min(nxt, end)
        h = (stop - t).total_seconds() / 3600
        bday = (loc - timedelta(hours=DAY_START)).date()
        add(bday, loc.hour, h)
        t = stop


# ---------------------------------------------------------------- days, summed as read
def _days():
    if _mem["days"] is None:
        try:
            _mem["days"] = json.loads(DAYS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _mem["days"] = {}
    return _mem["days"]


def _save_days():
    try:
        tmp = DAYS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_days(), separators=(",", ":")), encoding="utf-8")
        tmp.replace(DAYS_FILE)
        _mem["dirty"] = 0
    except OSError:
        pass


def _blank_unit():
    return {"hours": {s: 0.0 for s in SECTIONS}, "by_hour": [[0.0] * 4 for _ in range(24)], "heads": [0] * 24,
            "overtime": 0.0, "planned": 0.0, "planned_by_hour": [0.0] * 24, "people": 0, "attendances": 0,
            "open": 0, "forgotten": 0, "long": 0, "states": {}, "rejected_hours": 0.0}


def read_range(a, b, now=None):
    """Reads a..b (business days) from Odoo and keeps each day summed by restaurant. Returns {day: {unit: …}}."""
    now = now or datetime.now(timezone.utc)
    sec = _load_sections()
    emp_sec, unit_name = sec["emp"], sec["units"]
    lo, _ = _bounds(a)
    _, hi = _bounds(b)
    att = call("hr.attendance", "search_read",
               [["&", ["check_in", "<", _odoo(hi)], "|", ["check_out", "=", False], ["check_out", ">", _odoo(lo)]]],
               {"fields": ["check_in", "check_out", "rhg_unit_id", "rhg_state", "rhg_is_overtime", "employee_id"],
                "context": {"active_test": False}})
    slots = call("planning.slot", "search_read",
                 [[["start_datetime", "<", _odoo(hi)], ["end_datetime", ">", _odoo(lo)]]],
                 {"fields": ["start_datetime", "end_datetime", "allocated_percentage", "rhg_unit_id", "department_id",
                             "rhg_absence_kind", "employee_id"]})
    out = defaultdict(lambda: defaultdict(_blank_unit))
    heads = defaultdict(set)                     # (day, unit, hour) → employee ids, only to count heads; dropped
    people = defaultdict(set)
    for r in att:
        unit = unit_name.get((r.get("rhg_unit_id") or [None])[0]) or (r.get("rhg_unit_id") or [None, ""])[1] or "No restaurant"
        ci, co = _utc(r.get("check_in")), _utc(r.get("check_out"))
        if not ci:
            continue
        bday = (ci.astimezone(TZ) - timedelta(hours=DAY_START)).date()
        emp = (r.get("employee_id") or [None])[0]
        s_i = SECTIONS.index(emp_sec.get(emp, "Other"))
        u = out[bday.isoformat()][unit]
        u["attendances"] += 1
        st = r.get("rhg_state") or ""
        u["states"][st] = u["states"].get(st, 0) + 1
        if not co:
            if (now - ci).total_seconds() / 3600 > OPEN_FORGOTTEN_H:
                u["forgotten"] += 1               # no hours: the check-out was forgotten, nobody knows when they left
                continue
            u["open"] += 1
            co = now                              # on shift now: counted to this minute
        if (co - ci).total_seconds() / 3600 > LONG_H:
            u["long"] += 1
        if st == "rejected":
            u["rejected_hours"] += max(0.0, (co - ci).total_seconds() / 3600)
            continue
        if emp:
            people[(bday.isoformat(), unit)].add(emp)

        def add(day, hr, h, unit=unit, emp=emp, s_i=s_i, ot=bool(r.get("rhg_is_overtime"))):
            if day < a or day > b:
                return
            x = out[day.isoformat()][unit]
            x["hours"][SECTIONS[s_i]] += h
            x["by_hour"][hr][s_i] += h
            if ot:
                x["overtime"] += h
            if emp:
                heads[(day.isoformat(), unit, hr)].add(emp)
        _spread(max(ci, lo), min(co, hi), add)
    for r in slots:
        if r.get("rhg_absence_kind"):
            continue
        unit = unit_name.get((r.get("rhg_unit_id") or [None])[0]) or (r.get("rhg_unit_id") or [None, ""])[1] or "No restaurant"
        s, e = _utc(r.get("start_datetime")), _utc(r.get("end_datetime"))
        if not s or not e:
            continue
        pct = float(r.get("allocated_percentage") or 100) / 100

        def addp(day, hr, h, unit=unit, pct=pct):
            if day < a or day > b:
                return
            x = out[day.isoformat()][unit]
            x["planned"] += h * pct
            x["planned_by_hour"][hr] += h * pct
        _spread(max(s, lo), min(e, hi), addp)
    for (day, unit, hr), ids in heads.items():
        out[day][unit]["heads"][hr] = len(ids)
    for (day, unit), ids in people.items():
        out[day][unit]["people"] = len(ids)
    res = {}
    d = a
    while d <= b:
        k = d.isoformat()
        units_ = {u: _round(v) for u, v in out.get(k, {}).items()}
        res[k] = units_
        d += timedelta(days=1)
    return res


def _round(u):
    u["hours"] = {k: round(v, 2) for k, v in u["hours"].items()}
    u["by_hour"] = [[round(x, 2) for x in h] for h in u["by_hour"]]
    u["planned_by_hour"] = [round(x, 2) for x in u["planned_by_hour"]]
    for k in ("overtime", "planned", "rejected_hours"):
        u[k] = round(u[k], 2)
    return u


def days(a, b, force=False, today=None, progress=None):
    """{day: {unit: summed figures}} for a..b: a day kept is not read again once final (FINAL_AFTER days back); the
    running and recent days are read again after 5 minutes (today) or an hour (the last week, still reviewed)."""
    today = today or (datetime.now(TZ) - timedelta(hours=DAY_START)).date()
    b = min(b, today)
    want, out = [], {}
    d = a
    with _days_lock:
        held = _days()
        while d <= b:
            k = d.isoformat()
            hit = held.get(k)
            age = time.time() - (hit or {}).get("at", 0)
            keep = 300 if d >= today else 3600
            if hit and not force and (hit.get("final") or age < keep):
                out[k] = hit["units"]
            else:
                want.append(d)
            d += timedelta(days=1)
    # contiguous runs of days to read, a week at a time
    runs, cur = [], []
    for d in want:
        if cur and ((d - cur[-1]).days != 1 or len(cur) >= 7):
            runs.append(cur)
            cur = []
        cur.append(d)
    if cur:
        runs.append(cur)
    for i, run in enumerate(runs):
        if progress:
            progress(f"Odoo: days {run[0].isoformat()} to {run[-1].isoformat()} ({i + 1} of {len(runs)})")
        got = read_range(run[0], run[-1])
        with _days_lock:
            held = _days()
            for k, v in got.items():
                held[k] = {"at": time.time(), "units": v,
                           "final": (today - date.fromisoformat(k)).days >= FINAL_AFTER}
                out[k] = v
            _save_days()
    return out


def mapping(text):
    """{Odoo unit (upper case): restaurant reference}: the defaults, then the lines of the screen (unit = reference;
    "unit = -" leaves a unit out of the restaurants)."""
    m = {k.upper(): v for k, v in DEFAULT_MAP.items()}
    for line in str(text or "").splitlines():
        line = line.split("#", 1)[0]
        if "=" not in line:
            continue
        k, _, v = line.rpartition("=")
        k, v = k.strip().upper(), v.strip()
        if k:
            m[k] = "" if v in ("", "-") else v
    return m


# ---------------------------------------------------------------- the workforce (employees and contracts), summed
_wf = {"at": 0.0, "value": None}


def _weekly_hours(cal):
    """Hours a week of a working schedule, from its name ("Standard 40 hours/week"); None when it says no number
    of hours a week (a 24-hour schedule, "Company Schedule"): an FTE is not counted from it."""
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*hours?\s*/\s*week", cal.get("name") or "", re.I)
    if m:
        v = float(m.group(1).replace(",", "."))
        return v if 0 < v <= 60 else None
    return None


def workforce(max_age=1800, today=None):
    """Headcount, hires, leavers and transfers by restaurant and month from the employees and their contracts; FTE
    from the contracts' working schedules; the gaps of the employee cards. Kept half an hour. Only counts leave here."""
    if _wf["value"] and time.time() - _wf["at"] < max_age:
        return _wf["value"]
    today = today or date.today()
    sec = _load_sections()
    deps = call("hr.department", "search_read", [[]], {"fields": ["complete_name", "parent_id"], "context": {"active_test": False}})
    parent = {d["id"]: (d.get("parent_id") or [None])[0] for d in deps}
    names = {d["id"]: (d.get("complete_name") or "").strip() for d in deps}

    def top(dep_id):
        seen = 0
        while dep_id and parent.get(dep_id) and seen < 10:
            dep_id, seen = parent[dep_id], seen + 1
        return names.get(dep_id, "") if dep_id else ""
    emps = call("hr.employee", "search_read", [[["active", "in", [True, False]]]],
                {"fields": ["active", "company_id", "department_id", "job_id", "parent_id", "work_location_id",
                            "first_contract_date", "departure_date", "contract_id"], "context": {"active_test": False}})
    cons = call("hr.contract", "search_read", [[["state", "in", ["open", "close"]]]],
                {"fields": ["employee_id", "date_start", "date_end", "state", "department_id", "resource_calendar_id",
                            "company_id"], "context": {"active_test": False}})
    cals = {c["id"]: _weekly_hours(c) for c in call("resource.calendar", "search_read", [[]], {"fields": ["name"]})}
    by_emp = defaultdict(list)
    for c in cons:
        e = (c.get("employee_id") or [None])[0]
        if e and c.get("date_start"):
            by_emp[e].append(c)
    for v in by_emp.values():
        v.sort(key=lambda c: c["date_start"])
    # month ends of the last 24 months and the running month (to today)
    months = []
    y, m = today.year, today.month
    for _ in range(25):
        months.append(f"{y}-{m:02d}")
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    months.reverse()

    def month_end(k):
        yy, mm = int(k[:4]), int(k[5:7])
        nxt = date(yy + (mm == 12), mm % 12 + 1, 1) - timedelta(days=1)
        return min(nxt, today)
    head = defaultdict(lambda: defaultdict(int))      # month → unit → employees under contract at the month end
    hires, leavers, transfers = (defaultdict(lambda: defaultdict(int)) for _ in range(3))
    for e, cs in by_emp.items():
        units_ = [top((c.get("department_id") or [None])[0]) or "No department" for c in cs]
        for k in months:
            d = month_end(k).isoformat()
            for c, u in zip(cs, units_):
                if c["date_start"] <= d and (not c.get("date_end") or c["date_end"] >= d):
                    head[k][u] += 1
                    break
        first = cs[0]["date_start"][:7]
        hires[first][units_[0]] += 1
        last = cs[-1]
        if last.get("date_end") and last["date_end"] < today.isoformat() and last["state"] == "close":
            leavers[last["date_end"][:7]][units_[-1]] += 1
        for prev, nxt, up, un in zip(cs, cs[1:], units_, units_[1:]):
            if up != un:
                transfers[nxt["date_start"][:7]][f"{up} → {un}"] += 1
    now_units = defaultdict(lambda: {"employees": 0, "sections": defaultdict(int), "jobs": defaultdict(int),
                                     "fte": 0.0, "fte_unknown": 0, "no_manager": 0, "no_location": 0,
                                     "no_job": 0, "no_contract": 0, "company": ""})
    emp_sec = sec["emp"]
    for e in emps:
        if not e.get("active"):
            continue
        u = top((e.get("department_id") or [None])[0]) or "No department"
        x = now_units[u]
        x["employees"] += 1
        x["company"] = x["company"] or ((e.get("company_id") or [None, ""])[1])
        x["sections"][emp_sec.get(e["id"], "Other")] += 1
        x["jobs"][(e.get("job_id") or [None, "No position"])[1]] += 1
        x["no_manager"] += 0 if e.get("parent_id") else 1
        x["no_location"] += 0 if e.get("work_location_id") else 1
        x["no_job"] += 0 if e.get("job_id") else 1
        cs = [c for c in by_emp.get(e["id"], []) if c["state"] == "open"]
        if not cs:
            x["no_contract"] += 1
            x["fte_unknown"] += 1
            continue
        h = cals.get((cs[-1].get("resource_calendar_id") or [None])[0])
        if h is None:
            x["fte_unknown"] += 1
        else:
            x["fte"] += h / 40
    value = {
        "read_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "months": months,
        "headcount": {k: dict(head[k]) for k in months},
        "hires": {k: dict(hires[k]) for k in months if k in hires},
        "leavers": {k: dict(leavers[k]) for k in months if k in leavers},
        "transfers": {k: dict(transfers[k]) for k in months if k in transfers},
        "now": {u: dict(v, sections=dict(v["sections"]), jobs=dict(v["jobs"]), fte=round(v["fte"], 1))
                for u, v in now_units.items()},
        "totals": {"active": sum(1 for e in emps if e.get("active")), "archived": sum(1 for e in emps if not e.get("active")),
                   "contracts": len(cons), "no_department": sum(1 for e in emps if e.get("active") and not e.get("department_id"))},
        "history_from": min((c["date_start"] for c in cons), default=None),
    }
    _wf.update(at=time.time(), value=value)
    return value


def thresholds(text):
    """{reference or "*": {"splh_low": x, "splh_high": y}} from lines "reference = low, high" (sales per labour hour;
    an empty side leaves it unset)."""
    out = {}
    for n, line in enumerate(str(text or "").splitlines(), 1):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if "=" not in line:
            raise ValueError(f"Thresholds, line {n}: write reference = low, high.")
        k, _, v = line.partition("=")
        parts = [x.strip().replace(" ", "") for x in v.split(",")]
        try:
            lo = float(parts[0]) if parts and parts[0] else 0.0
            hi = float(parts[1]) if len(parts) > 1 and parts[1] else 0.0
        except ValueError as exc:
            raise ValueError(f"Thresholds, line {n}: the two figures are numbers.") from exc
        out[k.strip()] = {"splh_low": lo, "splh_high": hi}
    return out
