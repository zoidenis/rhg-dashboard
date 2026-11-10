"""SevenRooms: the reservations, covers and guest feedback of each venue, only read (API 2.4, GET calls only: no
reservation, client or tag is ever written).

Sign-in: POST /auth with the API account's client_id and client_secret → a token for 24 hours, sent as the
Authorization header (no "Bearer"). The account is issued by SevenRooms for an approved use (partnership request) at
the level of a venue or of the venue group; it is entered on the screen by someone with the Settings permission and
stays in data/sevenrooms.json on the server (the screen only learns whether the secret is set).

Servers: EU production (venues kept on the European server), US production, Demo (test credentials only).

Reservations: GET /reservations/export by venue and reservation date, 400 a page (cursor), at most 4,000 in one
sequence: so one venue and one day at a time. Kept in data/sevenrooms.db by venue and day (a day closed three days
ago is not read again). What is kept of a reservation is what the analysis needs: no name, phone, email, address,
birthday or notes of the guest; the client is kept only as a code (a hash of SevenRooms' client id) to count the
guests who come back.

Feedback: GET /venues/{venue_id}/feedback by the date the reservation was made (start_date, end_date).
"""
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
from datetime import date, timedelta

import requests

import store

CONFIG_FILE = store.DATA_DIR / "sevenrooms.json"
DB_FILE = "sevenrooms.db"
SERVERS = {"eu": ("EU production", "https://api.eu.sevenrooms.com/2_4"),
           "us": ("US production", "https://api.sevenrooms.com/2_4"),
           "demo": ("Demo (test credentials)", "https://demo.sevenrooms.com/api-ext/2_4")}
DEFAULTS = {"env": "eu", "client_id": "", "client_secret": "", "secret_saved_at": "", "venue_group_id": "",
            "map": "", "token": "", "token_until": 0}
PUBLIC = ("env", "client_id", "venue_group_id", "map")
TIMEOUT = 60
PAGE = 400
KEEP = 900                      # a day not yet closed is read again after 15 minutes
FINAL_AFTER = 3                 # days: statuses (no-show, completed) are set the night after; then a day is closed
_lock, _db_lock, _token_lock = threading.Lock(), threading.Lock(), threading.Lock()
_conn = None
_venues = {"at": 0.0, "list": None}


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
    fd, tmp = tempfile.mkstemp(prefix=".sevenrooms-", dir=store.DATA_DIR)
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
    return {k: cfg.get(k, "") for k in PUBLIC} | {
        "servers": {k: v[0] for k, v in SERVERS.items()}, "has_secret": bool(cfg.get("client_secret")),
        "secret_saved_at": cfg.get("secret_saved_at") or "",
        "configured": bool(cfg.get("client_id") and cfg.get("client_secret"))}


def save(patch, actor):
    """The connection and the venue mapping; the secret only when typed (empty keeps it)."""
    if not isinstance(patch, dict):
        raise ValueError("The settings must be an object.")
    with _lock:
        cfg = _read()
        before = (cfg["env"], cfg["client_id"], cfg["client_secret"], cfg["venue_group_id"])
        if "env" in patch:
            if patch["env"] not in SERVERS:
                raise ValueError("Choose the SevenRooms server: EU, US or Demo.")
            cfg["env"] = patch["env"]
        if "client_id" in patch:
            v = str(patch["client_id"] or "").strip()
            if v and not re.fullmatch(r"[A-Za-z0-9._~+/=-]{6,300}", v):
                raise ValueError("That does not look like a client ID.")
            cfg["client_id"] = v
        if "venue_group_id" in patch:
            v = str(patch["venue_group_id"] or "").strip()
            if v and not re.fullmatch(r"[A-Za-z0-9_-]{6,300}", v):
                raise ValueError("That does not look like a venue group ID.")
            cfg["venue_group_id"] = v
        if "map" in patch:
            cfg["map"] = str(patch["map"] or "")[:8000]
        sec = patch.get("client_secret", "")
        if not isinstance(sec, str) or len(sec) > 400 or any(c in sec for c in "\r\n"):
            raise ValueError("The client secret is one line.")
        if sec:
            cfg["client_secret"] = sec
            cfg["secret_saved_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        if (cfg["env"], cfg["client_id"], cfg["client_secret"], cfg["venue_group_id"]) != before:
            cfg["token"], cfg["token_until"] = "", 0        # another account or server: sign in again
            _venues.update(at=0.0, list=None)
        _write(cfg)
    store.audit("settings", "sevenrooms", actor, None, {k: cfg.get(k) for k in ("env", "client_id", "venue_group_id")}
                | {"secret_changed": bool(sec), "map_lines": len([x for x in cfg["map"].splitlines() if x.strip()])}, "")
    return info()


def _base(cfg):
    return SERVERS.get(cfg.get("env") or "eu", SERVERS["eu"])[1]


def _token(force=False):
    with _token_lock:
        with _lock:
            cfg = _read()
        if not (cfg.get("client_id") and cfg.get("client_secret")):
            raise Error("SevenRooms is not connected: enter the API account in Connection & mapping.")
        if not force and cfg.get("token") and time.time() < float(cfg.get("token_until") or 0):
            return cfg["token"], _base(cfg)
        try:
            r = requests.post(_base(cfg) + "/auth", data={"client_id": cfg["client_id"], "client_secret": cfg["client_secret"]},
                              timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise Error(f"SevenRooms could not be reached ({type(exc).__name__}).") from None
        j = _json(r)
        tok = ((j.get("data") or {}).get("token") or "") if isinstance(j, dict) else ""
        if r.status_code != 200 or not tok:
            raise Error("SevenRooms refused the API account: " + _msg(j, r))
        with _lock:
            cfg = _read()
            cfg["token"], cfg["token_until"] = tok, time.time() + 23 * 3600     # 24 hours, an hour to spare
            _write(cfg)
        return tok, _base(cfg)


def _json(r):
    try:
        return r.json()
    except ValueError:
        return {}


def _msg(j, r):
    return str((j or {}).get("msg") or f"HTTP {r.status_code}")[:200]


def get(path, params=None):
    """One GET of the API, signed in; waits and tries again on 429 and 503 (SevenRooms asks for a back-off)."""
    tok, base = _token()
    wait = 2
    for attempt in range(6):
        try:
            r = requests.get(base + path, params=params or {}, headers={"Authorization": tok}, timeout=TIMEOUT)
        except requests.RequestException as exc:
            if attempt == 5:
                raise Error(f"SevenRooms could not be reached ({type(exc).__name__}).") from None
            time.sleep(wait)
            wait *= 2
            continue
        if r.status_code == 401 and attempt == 0:
            tok, base = _token(force=True)
            continue
        if r.status_code in (429, 503) and attempt < 5:
            time.sleep(wait)
            wait *= 2
            continue
        j = _json(r)
        if r.status_code == 204:
            return {}
        if r.status_code != 200:
            raise Error(f"SevenRooms {path.split('?')[0]}: " + _msg(j, r))
        return j.get("data") if isinstance(j, dict) else {}
    raise Error("SevenRooms is busy: try again in a few minutes.")


def _pages(path, params, key="results"):
    """Every page of a paged answer (cursor)."""
    out, cursor = [], None
    for _ in range(200):
        p = dict(params, limit=PAGE)
        if cursor:
            p["cursor"] = cursor
        d = get(path, p) or {}
        rows = d.get(key) or []
        out += [x for x in rows if isinstance(x, dict)]
        cursor = d.get("cursor")
        if not cursor or not rows:
            break
    return out


# ---------------------------------------------------------------- venues
def venues(max_age=6 * 3600, force=False):
    """[{id, name, internal_name, external_venue_id, currency, group}] of the account (cached)."""
    if not force and _venues["list"] is not None and time.time() - _venues["at"] < max_age:
        return _venues["list"]
    with _lock:
        cfg = _read()
    p = {"venue_group_id": cfg["venue_group_id"]} if cfg.get("venue_group_id") else {}
    rows = _pages("/venues", p)
    out = [{"id": v.get("id") or "", "name": v.get("name") or "", "internal_name": v.get("internal_name") or "",
            "external_venue_id": v.get("external_venue_id") or "", "currency": v.get("currency_code") or "",
            "group": v.get("venue_group_name") or "", "prefix": v.get("unique_confirmation_prefix") or ""}
           for v in rows if v.get("id")]
    _venues.update(at=time.time(), list=out)
    return out


def test():
    """Signs in afresh and lists the venues the account sees."""
    _token(force=True)
    return {"ok": True, "venues": venues(force=True)}


# ---------------------------------------------------------------- reservations, kept by venue and day
def _db():
    global _conn
    if _conn is None:
        store.DATA_DIR.mkdir(exist_ok=True)
        _conn = sqlite3.connect(store.DATA_DIR / DB_FILE, check_same_thread=False, timeout=30)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript("""
            CREATE TABLE IF NOT EXISTS res (venue TEXT, day TEXT, id TEXT, j TEXT, PRIMARY KEY (venue, day, id));
            CREATE TABLE IF NOT EXISTS resdays (venue TEXT, day TEXT, final INTEGER, at REAL, n INTEGER, PRIMARY KEY (venue, day));
            CREATE TABLE IF NOT EXISTS fb (venue TEXT, k TEXT, rday TEXT, j TEXT, PRIMARY KEY (venue, k));
            CREATE TABLE IF NOT EXISTS fbreads (venue TEXT PRIMARY KEY, a TEXT, b TEXT, at REAL);
        """)
    return _conn


def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def client_code(cid):
    """SevenRooms' client id as a short code: the same guest, the same code; the id itself is not kept."""
    return hashlib.sha256(("rhg-sr|" + str(cid)).encode()).hexdigest()[:12] if cid else ""


def check_numbers(text):
    """'1234, 1240' (free form, or filled by the POS integration) → ['1234', '1240']."""
    return [x for x in re.findall(r"[A-Za-z0-9-]+", str(text or "")) if any(c.isdigit() for c in x)][:20]


def slim(r):
    """A reservation as SevenRooms gives it → the fields kept (no personal data of the guest)."""
    tickets = []
    for t in r.get("pos_tickets") or []:
        if isinstance(t, dict):
            tickets.append({"code": str(t.get("code") or ""), "pos_id": str(t.get("local_pos_ticket_id") or ""),
                            "total": _num(t.get("total")), "subtotal": _num(t.get("subtotal")), "tax": _num(t.get("tax")),
                            "table": str(t.get("table_no") or ""), "start": str(t.get("start_time") or "")[:19],
                            "end": str(t.get("end_time") or "")[:19]})
    tables = r.get("table_numbers")
    tags = [str(t.get("tag_display") or t.get("tag") or "") for t in (r.get("tags") or []) if isinstance(t, dict)]
    return {"id": str(r.get("id") or ""), "ref": str(r.get("reference_code") or ""), "venue": str(r.get("venue_id") or ""),
            "date": str(r.get("date") or "")[:10], "time": str(r.get("time_slot_iso") or r.get("arrival_time") or "")[:8],
            "real": str(r.get("real_datetime_of_slot") or "")[:19], "shift": str(r.get("shift_category") or ""),
            "guests": _int(r.get("max_guests")) or 0, "arrived": _int(r.get("arrived_guests")),
            "status": str(r.get("status") or ""), "status_display": str(r.get("status_display") or ""),
            "simple": str(r.get("status_simple") or ""), "walk_in": bool(r.get("is_walk_in")),
            "source": str(r.get("booked_by") or "")[:80], "created": str(r.get("created") or "")[:19],
            "updated": str(r.get("updated") or "")[:19], "deleted": bool(r.get("deleted")),
            "duration": _int(r.get("duration")), "seated": str(r.get("seated_time") or "")[:12],
            "left": str(r.get("left_time") or "")[:12],
            "tables": ", ".join(str(x) for x in tables) if isinstance(tables, list) else str(tables or ""),
            "area": str(r.get("venue_seating_area_name") or ""), "server": str(r.get("served_by") or "")[:60],
            "vip": bool(r.get("is_vip")), "tags": [t for t in tags if t][:12],
            "checks": check_numbers(r.get("check_numbers")), "tickets": tickets,
            "gross": _num(r.get("total_gross_payment")), "net": _num(r.get("total_net_payment")),
            "comps": _num(r.get("comps")), "prepay": _num(r.get("prepayment_total")) or _num(r.get("prepayment")),
            "rating": _num(r.get("rating")), "client": client_code(r.get("client_id") or r.get("venue_group_client_id"))}


def read_day(venue_id, day, today=None):
    """One venue's reservations of one day, unless held (a closed day is never read again)."""
    today = today or date.today()
    ds = day.isoformat() if isinstance(day, date) else str(day)
    with _db_lock:
        hit = _db().execute("SELECT final, at FROM resdays WHERE venue=? AND day=?", (venue_id, ds)).fetchone()
    if hit and (hit[0] or time.time() - hit[1] < KEEP):
        return False
    rows = _pages("/reservations/export", {"venue_id": venue_id, "from_date": ds, "to_date": ds})
    keep = [slim(r) for r in rows]
    final = ds <= (today - timedelta(days=FINAL_AFTER)).isoformat()
    with _db_lock:
        db = _db()
        db.execute("DELETE FROM res WHERE venue=? AND day=?", (venue_id, ds))
        db.executemany("INSERT OR REPLACE INTO res VALUES (?,?,?,?)",
                       [(venue_id, ds, x["id"] or x["ref"], json.dumps(x, separators=(",", ":"))) for x in keep if x["id"] or x["ref"]])
        db.execute("INSERT OR REPLACE INTO resdays VALUES (?,?,?,?,?)", (venue_id, ds, int(final), time.time(), len(keep)))
        db.commit()
    return True


def held(venue_id, a, b):
    """The reservations held of a venue between two days (by reservation date)."""
    with _db_lock:
        rows = _db().execute("SELECT j FROM res WHERE venue=? AND day>=? AND day<=?", (venue_id, a.isoformat(), b.isoformat())).fetchall()
    return [json.loads(j) for (j,) in rows]


def read_feedback(venue_id, a, b, today=None):
    """The feedback of a venue for reservations of a..b: asked by the date reservations were made (60 days before
    a, to b), kept by its reservation; read again after 15 minutes."""
    lo = a - timedelta(days=60)
    with _db_lock:
        hit = _db().execute("SELECT a, b, at FROM fbreads WHERE venue=?", (venue_id,)).fetchone()
    if hit and hit[0] <= lo.isoformat() and hit[1] >= b.isoformat() and time.time() - hit[2] < KEEP:
        return False
    d = get(f"/venues/{venue_id}/feedback", {"start_date": lo.isoformat(), "end_date": b.isoformat()}) or {}
    rows = []
    for f in d.get("reservation_feedback") or []:
        if not isinstance(f, dict):
            continue
        x = {k: _num(f.get(k)) for k in ("overall", "food", "drinks", "service", "ambience")}
        x.update(type=str(f.get("feedback_type") or ""), res=str(f.get("reservation_id") or ""), order=str(f.get("order_id") or ""),
                 rday=str(f.get("reservation_date") or "")[:10], received=str(f.get("received_date") or "")[:19],
                 recommend=str(f.get("recommend_to_friend") or ""))
        rows.append(x)
    with _db_lock:
        db = _db()
        db.executemany("INSERT OR REPLACE INTO fb VALUES (?,?,?,?)",
                       [(venue_id, (x["res"] or x["order"]) + "|" + x["received"], x["rday"], json.dumps(x)) for x in rows])
        db.execute("INSERT OR REPLACE INTO fbreads VALUES (?,?,?,?)", (venue_id, lo.isoformat(), b.isoformat(), time.time()))
        db.commit()
    return True


def feedback_held(venue_id, a, b):
    with _db_lock:
        rows = _db().execute("SELECT j FROM fb WHERE venue=? AND rday>=? AND rday<=?", (venue_id, a.isoformat(), b.isoformat())).fetchall()
    return [json.loads(j) for (j,) in rows]
