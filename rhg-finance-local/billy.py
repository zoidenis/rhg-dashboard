"""Billy Bridge (Nexia Albania): the sales invoices the tills fiscalized, read through its REST API (v3.0, GET methods).

Read-only. Billy fiscalizes the POS checks of every till (TCR), the kiosks included, with the Tax Administration; the
tower only asks it for what was issued and never sends, changes or fiscalizes anything. The only endpoints it may
call are in ALLOWED.

  * POST /api/v1/auth {username, nuis, password} → {"authorization": "Bearer …"}: one sign-in per NUIS (company);
  * POST /api/v1/get-sales-invoices {FIC, TimeFrom, TimeTo}: the invoices that went through the bridge, with their
    IIC and, once fiscalized, their FIC (an invoice without a FIC did not reach the Tax Administration);
  * POST /api/v1/get-invoices {FIC, Type: "SALES", Start, End}: the invoices of the fiscalization service, with the VAT
    groups (sameTaxes), the payment methods and the buyer: the source of the sales book.

The connection (address, and per company the NUIS, the user and the password) is entered in Finance → Settings and
kept in data/billy.json on the server, readable by the server only; the screen learns only whether a password is set.
Invoices are kept by day in data/billy.db: a day two days old or more is final and is not read again, a recent one
after a quarter of an hour.
"""
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
from datetime import date, datetime, timedelta, timezone

import requests

import store

CONFIG_FILE = store.DATA_DIR / "billy.json"
DB_PATH = store.DATA_DIR / "billy.db"
TIMEOUT = 90
KEEP = 900
ALLOWED = ("auth", "get-sales-invoices", "get-invoices")          # reading only: nothing is ever fiscalized from here
NUIS = re.compile(r"^[A-Z]\d{8}[A-Z]$")
_URL = re.compile(r"^https://[A-Za-z0-9.-]+(:\d+)?/?$")
_lock, _db_lock, _tok_lock = threading.Lock(), threading.Lock(), threading.Lock()
_tokens = {}                     # nuis → (authorization header, at)
_conn = None


class BillyError(Exception):
    pass


# ---------------------------------------------------------------- the connection, kept on the server
def _read():
    try:
        d = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(cfg):
    fd, tmp = tempfile.mkstemp(prefix=".billy-", dir=store.DATA_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(cfg, out, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, CONFIG_FILE)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def info():
    """What the screen may see: no password, no token."""
    with _lock:
        cfg = _read()
    accts = [{"company": a.get("company", ""), "nuis": a.get("nuis", ""), "username": a.get("username", ""),
              "has_password": bool(a.get("password")), "password_saved_at": a.get("password_saved_at", "")}
             for a in cfg.get("accounts") or []]
    return {"base_url": cfg.get("base_url", ""), "accounts": accts,
            "configured": bool(cfg.get("base_url")) and any(a["has_password"] and a["username"] and a["nuis"] for a in accts)}


def save(patch, actor):
    """patch: {base_url, accounts: [{company, nuis, username, password?, clear_password?}]}. A password left empty
    keeps the one held for the same NUIS and user; another user never inherits it."""
    if not isinstance(patch, dict):
        raise ValueError("The Billy settings must be an object.")
    with _lock:
        before = _read()
        cfg = dict(before)
        if "base_url" in patch:
            url = str(patch.get("base_url") or "").strip().rstrip("/")
            if url and not _URL.match(url + "/"):
                raise ValueError("The Billy address must be the https:// address only, without /api/v1.")
            cfg["base_url"] = url
        if "accounts" in patch:
            held = {(a.get("nuis"), a.get("username")): a for a in before.get("accounts") or []}
            out = []
            for a in patch.get("accounts") or []:
                if not isinstance(a, dict):
                    raise ValueError("Every Billy account is an object.")
                nuis = str(a.get("nuis") or "").strip().upper()
                user = str(a.get("username") or "").strip()[:120]
                company = str(a.get("company") or "").strip()[:60]
                if not nuis and not user:
                    continue
                if not NUIS.match(nuis):
                    raise ValueError(f"{nuis or 'A NUIS'} is not a NUIS (a letter, eight digits, a letter).")
                pw = a.get("password") or ""
                if not isinstance(pw, str) or len(pw) > 256 or any(c in pw for c in "\r\n"):
                    raise ValueError("Invalid password.")
                old = held.get((nuis, user)) or {}
                rec = {"company": company, "nuis": nuis, "username": user,
                       "password": "" if a.get("clear_password") else (pw or old.get("password", "")),
                       "password_saved_at": date.today().isoformat() if pw else old.get("password_saved_at", "")}
                out.append(rec)
            cfg["accounts"] = out
        _write(cfg)
    with _tok_lock:
        _tokens.clear()
    pub = lambda c: {"base_url": c.get("base_url", ""),
                     "accounts": [{k: a.get(k) for k in ("company", "nuis", "username")} | {"has_password": bool(a.get("password"))}
                                  for a in c.get("accounts") or []]}
    store.audit("settings", "billy connection", actor, pub(before), pub(cfg),
                "Password changed" if [a.get("password") for a in before.get("accounts") or []]
                != [a.get("password") for a in cfg.get("accounts") or []] else "")
    return info()


def account(company):
    """The account of a BC company, else None."""
    with _lock:
        cfg = _read()
    for a in cfg.get("accounts") or []:
        if (a.get("company") or "").upper() == (company or "").upper() and a.get("password"):
            return dict(a, base_url=cfg.get("base_url", ""))
    return None


def companies():
    return [a["company"] for a in info()["accounts"] if a["company"] and a["has_password"]]


# ---------------------------------------------------------------- the calls
def _post(acct, name, body, auth=True):
    if name not in ALLOWED:
        raise BillyError(f"{name} is not a reading method; the tower does not call it.")
    if not acct.get("base_url"):
        raise BillyError("The Billy address is not set (Finance → Settings → Billy).")
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = _token(acct)
    try:
        r = requests.post(f"{acct['base_url']}/api/v1/{name}", json=body, headers=headers, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise BillyError(f"Could not reach Billy at {acct['base_url']}: {exc}") from exc
    if r.status_code == 401 and auth:
        with _tok_lock:
            _tokens.pop(acct["nuis"], None)
        headers["Authorization"] = _token(acct)
        r = requests.post(f"{acct['base_url']}/api/v1/{name}", json=body, headers=headers, timeout=TIMEOUT)
    if r.status_code >= 400:
        raise BillyError(f"{name} returned {r.status_code}: {(r.text or '')[:200]}")
    try:
        j = r.json() if (r.text or "").strip() else []
    except ValueError as exc:
        raise BillyError(f"{name} did not return JSON.") from exc
    if isinstance(j, dict) and j.get("Status") is False:
        e = j.get("Error") or {}
        raise BillyError(f"{name}: {e.get('ErrorCode', '')} {e.get('ErrorText', '') or e.get('ErrorText ', '')}".strip())
    return j


def _token(acct):
    with _tok_lock:
        hit = _tokens.get(acct["nuis"])
        if hit and time.time() - hit[1] < 50 * 60:
            return hit[0]
    j = _post(acct, "auth", {"username": acct["username"], "nuis": acct["nuis"], "password": acct["password"]}, auth=False)
    tok = (j or {}).get("authorization") if isinstance(j, dict) else None
    if not tok:
        raise BillyError("Billy did not accept the user, NUIS and password of this company.")
    tok = tok if tok.lower().startswith("bearer ") else f"Bearer {tok}"
    with _tok_lock:
        _tokens[acct["nuis"]] = (tok, time.time())
    return tok


# ---------------------------------------------------------------- Albania's time (CET/CEST), without a time zone database
def _last_sunday(y, m):
    d = date(y, m, 31)
    return d - timedelta(days=(d.weekday() + 1) % 7)


def tirana(dt_utc):
    """An aware UTC datetime → naive local time of Tirana (the EU summer time rule)."""
    y = dt_utc.year
    start = datetime(y, 3, _last_sunday(y, 3).day, 1, tzinfo=timezone.utc)
    end = datetime(y, 10, _last_sunday(y, 10).day, 1, tzinfo=timezone.utc)
    off = 2 if start <= dt_utc < end else 1
    return (dt_utc + timedelta(hours=off)).replace(tzinfo=None)


def local(text):
    """'2024-01-07T20:25:10.000+00:00' (any offset) → 'YYYY-MM-DDTHH:MM:SS' in Tirana, or '' ."""
    if not text:
        return ""
    try:
        dt = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return str(text)[:19]
    if dt.tzinfo is None:
        return dt.isoformat(timespec="seconds")
    return tirana(dt.astimezone(timezone.utc)).isoformat(timespec="seconds")


def _stamp(d, end=False):
    """A Tirana day's first or last second with its offset, as Billy takes it."""
    t = datetime(d.year, d.month, d.day, 23 if end else 0, 59 if end else 0, 59 if end else 0)
    off = 2 if datetime(d.year, 3, _last_sunday(d.year, 3).day) <= t < datetime(d.year, 10, _last_sunday(d.year, 10).day) else 1
    return t.isoformat() + f"+0{off}:00"


# ---------------------------------------------------------------- one invoice, as the tower keeps it
def _f(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def compact(x, source):
    """A Billy invoice (either method) → the fields the reconciliation and the sales book use."""
    buyer = x.get("buyer") or {}
    taxes = []
    for t in ((x.get("sameTaxes") or {}).get("sameTax") or []) if isinstance(x.get("sameTaxes"), dict) else []:
        taxes.append([_f(t.get("vatrate")), t.get("exemptFromVAT"), _f(t.get("priceBefVAT")) or 0.0,
                      _f(t.get("vatamt")) or 0.0, t.get("numOfItems")])
    refs = x.get("sumInvIICRefs")
    if isinstance(refs, dict):
        refs = refs.get("sumInvIICRef") or []
    refs = [str(r.get("iic") if isinstance(r, dict) else r) for r in (refs or []) if r]
    pays = []
    for p in ((x.get("payMethods") or {}).get("payMethod") or []) if isinstance(x.get("payMethods"), dict) else []:
        pays.append([p.get("type") or "", _f(p.get("amt")) or 0.0])
    return {"src": source, "iic": (x.get("iic") or "").upper(), "fic": x.get("fic") or "", "num": x.get("invNum") or "",
            "ord": x.get("invOrdNum"), "issue": local(x.get("issueDateTime")), "sent": local(x.get("sendDateTime")),
            "total": _f(x.get("totPrice")), "wo_vat": _f(x.get("totPriceWoVAT")), "vat": _f(x.get("totVATAmt")),
            "type": x.get("typeOfInv") or "", "tcr": (x.get("tcrcode") or "").lower(),
            "bu": (x.get("businUnitCode") or "").lower(), "op": (x.get("operatorCode") or "").lower(),
            "buyer_id": (buyer.get("idnum") or "") if isinstance(buyer, dict) else "",
            "buyer_type": (buyer.get("idtype") or "") if isinstance(buyer, dict) else "",
            "buyer": (buyer.get("name") or "") if isinstance(buyer, dict) else "",
            "einv": bool(x.get("isEinvoice")), "eic": x.get("eic") or "", "corr": bool(x.get("correctiveInv")),
            "refs": refs, "taxes": taxes, "pays": pays, "reverse": bool(x.get("isReverseCharge")),
            "simplified": bool(x.get("isSimplifiedInv")), "in_vat": x.get("isIssuerInVAT"),
            "tax_free": _f(x.get("taxFreeAmt")), "markup": _f(x.get("markUpAmt"))}


# ---------------------------------------------------------------- the daily store
def _db():
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript("""
            CREATE TABLE IF NOT EXISTS inv (nuis TEXT, src TEXT, day TEXT, k TEXT, j TEXT, PRIMARY KEY (nuis, src, k));
            CREATE INDEX IF NOT EXISTS inv_day ON inv (nuis, src, day);
            CREATE TABLE IF NOT EXISTS days (nuis TEXT, src TEXT, day TEXT, final INTEGER, at REAL, n INTEGER,
                                             PRIMARY KEY (nuis, src, day));
        """)
    return _conn


def read_day(acct, src, day, today=None):
    """Reads one Tirana day of one method unless held; returns how many invoices it holds."""
    today = today or date.today()
    with _db_lock:
        hit = _db().execute("SELECT final, at, n FROM days WHERE nuis=? AND src=? AND day=?",
                            (acct["nuis"], src, day.isoformat())).fetchone()
    if hit and (hit[0] or time.time() - hit[1] < KEEP):
        return hit[2]
    if src == "bridge":
        rows = _post(acct, "get-sales-invoices", {"FIC": None, "TimeFrom": _stamp(day), "TimeTo": _stamp(day, True)})
    else:
        rows = _post(acct, "get-invoices", {"FIC": None, "Type": "SALES", "Start": _stamp(day), "End": _stamp(day, True)})
    if isinstance(rows, dict):
        rows = rows.get("invoices") or rows.get("Invoices") or rows.get("data") or []
    keep = []
    for x in rows or []:
        if not isinstance(x, dict):
            continue
        c = compact(x, src)
        k = c["iic"] or c["fic"] or c["num"]
        if k:
            keep.append((acct["nuis"], src, (c["issue"] or day.isoformat())[:10], k, json.dumps(c, separators=(",", ":"))))
    final = day <= today - timedelta(days=2)
    with _db_lock:
        db = _db()
        db.execute("DELETE FROM inv WHERE nuis=? AND src=? AND day=?", (acct["nuis"], src, day.isoformat()))
        db.executemany("INSERT OR REPLACE INTO inv VALUES (?,?,?,?,?)", keep)
        db.execute("INSERT OR REPLACE INTO days VALUES (?,?,?,?,?,?)",
                   (acct["nuis"], src, day.isoformat(), int(final), time.time(), len(keep)))
        db.commit()
    return len(keep)


def held(nuis, src, a, b):
    """The invoices held of a NUIS and method between two days (Tirana dates)."""
    with _db_lock:
        rows = _db().execute("SELECT j FROM inv WHERE nuis=? AND src=? AND day>=? AND day<=?",
                             (nuis, src, a.isoformat(), b.isoformat())).fetchall()
    return [json.loads(j) for (j,) in rows]
