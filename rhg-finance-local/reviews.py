"""Guest reviews of the units from Google and Wolt, for the Reviews KPI of Bonus management (bonus.py). Only read; no
reviewer's name is kept, only the rating, the date and the comment (cut to 500 characters).

Google, two ways (the first one connected wins for a unit):
  * Google Business Profile API (exact): every review of the locations the RHG account manages, with its date and its
    stars, read every hour (the whole history the first time). The month's figure = the reviews written in the month:
    their count and their average. Needs the account that manages the profiles: an OAuth client of a Google Cloud
    project with the Business Profile APIs approved, and a refresh token given once by that account.
  * Google Places API (approximate): the public overall rating and the number of reviews, one snapshot a day (≈150
    calls a month for five units). The month's count is exact (the number at the month end less the number at its
    start); the rating is the overall one, rounded by Google to one decimal, not the month's own: the average of a
    month's reviews cannot be worked out from it (± 0.05 on 3,000 reviews is ± 150 stars). Marked approximate.
Wolt: the official consumer reviews integration (developer.wolt.com, "Consumer reviews"): Wolt posts each review to a
webhook of ours, signed with HMAC-SHA256 of the body (header WOLT-SIGNATURE) with a secret we give Wolt; switched on
per venue by the Wolt account manager. The goods review (1–5) is counted.
"""
import hashlib
import hmac
import json
import secrets as pysecrets
import sqlite3
import threading
import time
import traceback
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

import store

CONFIG_FILE = store.DATA_DIR / "reviews.json"
DB_FILE = store.DATA_DIR / "reviews.db"
TZ = ZoneInfo("Europe/Tirane")
TIMEOUT = 40
STARS = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5}
# the OPA units on Google Maps (Geri, 22 Nov 2026): Simphony reference = Google place ID
DEFAULT_GOOGLE_MAP = """\
OPA_9KAT = ChIJOS3KgXQxUBMRPvjaSR3KF50   # OPA - Greek Street Food (9 Katëshet)
OPA_B    = ChIJS4oQUEkxUBMRv8S5DQsJ7Uw   # OPA - Greek Street Food (Bllok)
OPA_A    = ChIJh7FSX8ExUBMRAPFapNooNW4   # OPA - Greek Street Food (Stadiumi)
OPA_TE   = ChIJBZe7xlgxUBMR-WQqbKCUzGk   # OPA - Greek Street Food (TEG)
OPA_Q    = ChIJzxNipWIvUBMRfvtH1axSHaM   # OPA - Greek Street Food (QTU)
"""
DEFAULTS = {"google": {"api_key": "", "client_id": "", "client_secret": "", "refresh_token": "", "map": DEFAULT_GOOGLE_MAP},
            "wolt": {"secret": "", "map": ""}}
SECRETS = {"google": ("api_key", "client_secret", "refresh_token"), "wolt": ("secret",)}
_lock, _db_lock = threading.Lock(), threading.Lock()
_state = {"google_sync": None, "google_error": "", "places_sync": None, "places_error": "", "running": False}


# ---------------------------------------------------------------- settings
def _read():
    c = store._read(CONFIG_FILE, {})
    out = json.loads(json.dumps(DEFAULTS))
    for k in out:
        out[k].update((c or {}).get(k) or {})
    return out


def info():
    """The settings as the screen may see them: no secret, only whether each is saved."""
    c = _read()
    g, w = c["google"], c["wolt"]
    return {"google": {"client_id": g["client_id"], "map": g["map"], "api_key_set": bool(g["api_key"]),
                       "client_secret_set": bool(g["client_secret"]), "refresh_token_set": bool(g["refresh_token"]),
                       "business_profile": bool(g["client_id"] and g["client_secret"] and g["refresh_token"]),
                       "places": bool(g["api_key"])},
            "wolt": {"map": w["map"], "secret_set": bool(w["secret"])},
            "state": dict(_state), "seen": seen()}


def save(patch, actor):
    with _lock:
        c = _read()
        before = {k: {kk: ("•••" if kk in SECRETS[k] and vv else vv) for kk, vv in v.items()} for k, v in c.items()}
        for part in ("google", "wolt"):
            p = (patch or {}).get(part) or {}
            for k, v in p.items():
                if k not in c[part]:
                    continue
                v = str(v if v is not None else "").strip()
                if k in SECRETS[part] and not v:
                    continue                      # a secret left blank keeps the one saved
                c[part][k] = v[:4000]
            if (patch or {}).get(part, {}).get("new_secret") and part == "wolt":
                c["wolt"]["secret"] = pysecrets.token_hex(32)     # 256 bits, as Wolt asks (≥128)
        store._write(CONFIG_FILE, c)
    store.audit("review sources saved", "reviews", actor, before, {k: {kk: ("•••" if kk in SECRETS[k] and vv else vv)
                                                                        for kk, vv in v.items()} for k, v in c.items()})
    return info()


def wolt_secret():
    """Shown once to whoever sets the connection, to give to Wolt."""
    return _read()["wolt"]["secret"]


def mapping(text):
    """'ref = id' lines → {id: ref} (an id: a Google place ID, a Business Profile location, a Wolt venue ID)."""
    out = {}
    for line in str(text or "").splitlines():
        line = line.split("#", 1)[0].strip()
        if "=" in line:
            ref, _, key = line.partition("=")
            if ref.strip() and key.strip():
                out[key.strip()] = ref.strip()
    return out


# ---------------------------------------------------------------- the store
def _db():
    c = sqlite3.connect(DB_FILE, timeout=30)
    c.execute("CREATE TABLE IF NOT EXISTS rv (src TEXT, key TEXT, rid TEXT, at TEXT, day TEXT, rating REAL, comment TEXT,"
              " PRIMARY KEY (src, rid))")
    c.execute("CREATE INDEX IF NOT EXISTS rv_key ON rv (src, key, day)")
    c.execute("CREATE TABLE IF NOT EXISTS snap (src TEXT, key TEXT, day TEXT, rating REAL, n INTEGER, PRIMARY KEY (src, key, day))")
    return c


def _local_day(iso):
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00")[:26] + ("+00:00" if str(iso).endswith("Z") and "." in str(iso) else ""))
    except ValueError:
        t = datetime.fromisoformat(str(iso)[:19]).replace(tzinfo=timezone.utc)
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(TZ).date().isoformat()


def _parse_time(iso):
    s = str(iso or "").strip()
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    if "." in s:                                   # nanoseconds (Wolt) → microseconds
        head, _, tail = s.partition(".")
        frac = ""
        for ch in tail:
            if not ch.isdigit():
                break
            frac += ch
        rest = tail[len(frac):]
        s = f"{head}.{frac[:6]}{rest}"
    t = datetime.fromisoformat(s)
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _put(src, key, rid, when, rating, comment):
    t = _parse_time(when)
    if t is None or rating is None:
        return
    with _db_lock:
        db = _db()
        db.execute("INSERT OR REPLACE INTO rv VALUES (?,?,?,?,?,?,?)",
                   (src, key, rid, t.astimezone(timezone.utc).isoformat(), t.astimezone(TZ).date().isoformat(),
                    float(rating), str(comment or "")[:500]))
        db.commit()
        db.close()


def seen():
    """What each source has brought so far, by key: {src: [{key, n, last}]} (to map a Wolt venue as it appears)."""
    try:
        with _db_lock:
            db = _db()
            rows = db.execute("SELECT src, key, COUNT(*), MAX(day) FROM rv GROUP BY src, key").fetchall()
            snaps = db.execute("SELECT src, key, MAX(day), COUNT(*) FROM snap GROUP BY src, key").fetchall()
            db.close()
    except sqlite3.Error:
        return {}
    out = {}
    for src, key, n, last in rows:
        out.setdefault(src, []).append({"key": key, "n": n, "last": last})
    for src, key, last, n in snaps:
        out.setdefault(src + "_snapshots", []).append({"key": key, "n": n, "last": last})
    return out


# ---------------------------------------------------------------- Google Business Profile (exact)
def _token(g):
    r = requests.post("https://oauth2.googleapis.com/token", timeout=TIMEOUT, data={
        "client_id": g["client_id"], "client_secret": g["client_secret"], "refresh_token": g["refresh_token"],
        "grant_type": "refresh_token"})
    j = r.json() if r.content else {}
    if r.status_code != 200 or not j.get("access_token"):
        raise RuntimeError(f"Google sign-in refused: {j.get('error_description') or j.get('error') or r.status_code}")
    return j["access_token"]


def _get(url, tok, params=None):
    r = requests.get(url, headers={"Authorization": f"Bearer {tok}"}, params=params or {}, timeout=TIMEOUT)
    j = r.json() if r.content else {}
    if r.status_code != 200:
        msg = ((j.get("error") or {}).get("message") if isinstance(j.get("error"), dict) else j.get("error")) or r.status_code
        raise RuntimeError(f"Google answered {r.status_code}: {str(msg)[:200]}")
    return j


def gbp_locations(g=None):
    """[{account, location, title, place_id, address}] of every Business Profile location the account manages."""
    g = g or _read()["google"]
    tok = _token(g)
    out = []
    accs = _get("https://mybusinessaccountmanagement.googleapis.com/v1/accounts", tok).get("accounts") or []
    for a in accs:
        page = None
        while True:
            p = {"readMask": "name,title,storefrontAddress,metadata", "pageSize": 100}
            if page:
                p["pageToken"] = page
            j = _get(f"https://mybusinessbusinessinformation.googleapis.com/v1/{a['name']}/locations", tok, p)
            for L in j.get("locations") or []:
                addr = L.get("storefrontAddress") or {}
                out.append({"account": a["name"], "location": L["name"], "title": L.get("title") or "",
                            "place_id": (L.get("metadata") or {}).get("placeId") or "",
                            "address": ", ".join(addr.get("addressLines") or [])})
            page = j.get("nextPageToken")
            if not page:
                break
    return out


def sync_gbp(full=False):
    """The reviews of every location the account manages, newest first, down to the last sync (two days back) or
    the whole history the first time."""
    c = _read()["google"]
    if not (c["client_id"] and c["client_secret"] and c["refresh_token"]):
        return 0
    tok = _token(c)
    with _db_lock:
        db = _db()
        last = db.execute("SELECT MAX(at) FROM rv WHERE src='google'").fetchone()[0]
        db.close()
    stop = None if (full or not last) else (_parse_time(last) - timedelta(days=2))
    n = 0
    for L in gbp_locations(c):
        key = L["place_id"] or L["location"]
        loc_id = L["location"].split("/", 1)[1]
        page = None
        while True:
            p = {"pageSize": 50, "orderBy": "updateTime desc"}
            if page:
                p["pageToken"] = page
            j = _get(f"https://mybusiness.googleapis.com/v4/{L['account']}/locations/{loc_id}/reviews", tok, p)
            older = False
            for rv in j.get("reviews") or []:
                upd = _parse_time(rv.get("updateTime") or rv.get("createTime"))
                if stop and upd and upd < stop:
                    older = True
                    break
                _put("google", key, rv.get("reviewId") or rv.get("name"), rv.get("createTime"),
                     STARS.get(rv.get("starRating")), rv.get("comment"))
                n += 1
            page = j.get("nextPageToken")
            if older or not page:
                break
    _state["google_sync"] = datetime.now().isoformat(timespec="seconds")
    _state["google_error"] = ""
    return n


# ---------------------------------------------------------------- Google Places (approximate)
def snapshot_places(force=False):
    """One snapshot a day of each mapped place: the public rating and number of reviews."""
    c = _read()["google"]
    if not c["api_key"]:
        return 0
    today = datetime.now(TZ).date().isoformat()
    n = 0
    errors = []
    for pid in mapping(c["map"]):
        if not pid.startswith("ChI") and not pid.startswith("Gh"):
            continue
        with _db_lock:
            db = _db()
            have = db.execute("SELECT 1 FROM snap WHERE src='places' AND key=? AND day=?", (pid, today)).fetchone()
            db.close()
        if have and not force:
            continue
        r = requests.get(f"https://places.googleapis.com/v1/places/{pid}", timeout=TIMEOUT,
                         headers={"X-Goog-Api-Key": c["api_key"], "X-Goog-FieldMask": "rating,userRatingCount"})
        j = r.json() if r.content else {}
        if r.status_code != 200:
            errors.append(f"{pid}: {((j.get('error') or {}).get('message') or r.status_code)}"[:200])
            continue
        with _db_lock:
            db = _db()
            db.execute("INSERT OR REPLACE INTO snap VALUES ('places',?,?,?,?)", (pid, today, j.get("rating"), j.get("userRatingCount")))
            db.commit()
            db.close()
        n += 1
    _state["places_sync"] = datetime.now().isoformat(timespec="seconds")
    _state["places_error"] = "; ".join(errors)
    return n


# ---------------------------------------------------------------- Wolt webhook
def wolt_verify(raw, signature):
    s = _read()["wolt"]["secret"]
    if not s or not signature:
        return False
    want = hmac.new(s.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, signature.strip().lower())


def wolt_receive(raw):
    """A notification of Wolt (already verified): the goods review of an order, kept by its venue."""
    j = json.loads(raw.decode("utf-8"))
    if j.get("type") != "order_review.notification":
        return False
    order = j.get("order") or {}
    g = (j.get("review") or {}).get("goods_review") or {}
    if g.get("rating") is None:
        return False
    _put("wolt", str(order.get("venue_id") or ""), str(j.get("id") or order.get("id")), j.get("created_at"),
         g.get("rating"), g.get("comment"))
    return True


# ---------------------------------------------------------------- the month of a unit
def monthly(ref, a, b, sources=("google", "wolt")):
    """{channel: {value, count, approximate, detail}} of a unit for the days a..b (local dates)."""
    c = _read()
    gm, wm = mapping(c["google"]["map"]), mapping(c["wolt"]["map"])
    gkeys = [k for k, r in gm.items() if r == ref]
    wkeys = [k for k, r in wm.items() if r == ref]
    out = {}
    with _db_lock:
        db = _db()
        try:
            if "google" in sources and gkeys:
                q = ",".join("?" * len(gkeys))
                n_all = db.execute(f"SELECT COUNT(*) FROM rv WHERE src='google' AND key IN ({q})", gkeys).fetchone()[0]
                if n_all:
                    n, avg = db.execute(f"SELECT COUNT(*), AVG(rating) FROM rv WHERE src='google' AND key IN ({q}) AND day>=? AND day<=?",
                                        gkeys + [a.isoformat(), b.isoformat()]).fetchone()
                    out["google"] = {"value": round(avg, 2) if n else None, "count": n, "approximate": False,
                                     "detail": f"Google: {n} review(s) written in the month, Business Profile"}
                else:
                    pids = [k for k in gkeys if k.startswith(("ChI", "Gh"))]
                    if pids:
                        q2 = ",".join("?" * len(pids))
                        end = db.execute(f"SELECT key, day, rating, n FROM snap WHERE src='places' AND key IN ({q2}) AND day<=? ORDER BY day DESC",
                                         pids + [b.isoformat()]).fetchall()
                        start = db.execute(f"SELECT key, day, rating, n FROM snap WHERE src='places' AND key IN ({q2}) AND day<? ORDER BY day DESC",
                                           pids + [a.isoformat()]).fetchall()
                        first = db.execute(f"SELECT key, day, rating, n FROM snap WHERE src='places' AND key IN ({q2}) AND day>=? AND day<=? ORDER BY day",
                                           pids + [a.isoformat(), b.isoformat()]).fetchall()
                        e = end[0] if end else None
                        s = start[0] if start else (first[0] if first else None)
                        if e and e[1] >= a.isoformat():
                            cnt = (e[3] or 0) - (s[3] or 0) if s else None
                            since = "" if start else f" (counted from {s[1]}, the first snapshot)"
                            out["google"] = {"value": e[2], "count": cnt, "approximate": True, "partial": not start,
                                             "detail": f"Google overall rating {e[2]} on {e[1]} (Places API, not only this month's); "
                                                       f"{cnt if cnt is not None else '?'} new review(s){since}"}
                        else:
                            # a month before the snapshots began: the nearest snapshot after it (within two months),
                            # the rating only; the month's number of reviews is not known
                            after = db.execute(f"SELECT key, day, rating, n FROM snap WHERE src='places' AND key IN ({q2}) AND day>? AND day<=? ORDER BY day",
                                               pids + [b.isoformat(), (b + timedelta(days=62)).isoformat()]).fetchall()
                            if after and after[0][2] is not None:
                                x = after[0]
                                out["google"] = {"value": x[2], "count": None, "approximate": True,
                                                 "detail": f"Google overall rating {x[2]} on {x[1]}, the nearest snapshot after the month "
                                                           f"(Places API began then): the month's own reviews and their number are not known"}
            if "wolt" in sources and wkeys:
                q = ",".join("?" * len(wkeys))
                n, avg = db.execute(f"SELECT COUNT(*), AVG(rating) FROM rv WHERE src='wolt' AND key IN ({q}) AND day>=? AND day<=?",
                                    wkeys + [a.isoformat(), b.isoformat()]).fetchone()
                out["wolt"] = {"value": round(avg, 2) if n else None, "count": n, "approximate": False,
                               "detail": f"Wolt: {n} review(s) of the goods in the month"}
        finally:
            db.close()
    return out


def recent(ref, a, b, limit=30):
    """The month's reviews of a unit with a comment, the lowest first (for the unit's pop-up)."""
    c = _read()
    keys = [(s, k) for s, m in (("google", mapping(c["google"]["map"])), ("wolt", mapping(c["wolt"]["map"]))) for k, r in m.items() if r == ref]
    if not keys:
        return []
    with _db_lock:
        db = _db()
        rows = []
        for s, k in keys:
            rows += db.execute("SELECT src, day, rating, comment FROM rv WHERE src=? AND key=? AND day>=? AND day<=? AND comment<>''",
                               (s, k, a.isoformat(), b.isoformat())).fetchall()
        db.close()
    rows.sort(key=lambda r: (r[2], r[1]))
    return [{"src": r[0], "day": r[1], "rating": r[2], "comment": r[3]} for r in rows[:limit]]


# ---------------------------------------------------------------- the loop
def run_once():
    if _state["running"]:
        return
    _state["running"] = True
    try:
        try:
            sync_gbp()
        except Exception as exc:                                    # noqa: BLE001
            _state["google_error"] = str(exc)[:300]
        try:
            snapshot_places()
        except Exception as exc:                                    # noqa: BLE001
            _state["places_error"] = str(exc)[:300]
    finally:
        _state["running"] = False


def start():
    def loop():
        time.sleep(60)
        while True:
            try:
                run_once()
            except Exception:                                       # noqa: BLE001
                traceback.print_exc()
            time.sleep(3600)
    threading.Thread(target=loop, daemon=True, name="reviews").start()
