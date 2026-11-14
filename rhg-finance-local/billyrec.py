"""Control → Simphony ↔ Billy: every Simphony check against the invoice Billy fiscalized for it, every restaurant and
business day of the last weeks, with the sales book that Self-Care gives by hand.

Sources (both only read)
  * Simphony: the closed guest checks of each restaurant and business day (BI API getGuestChecks, by the closing
    business date): number, closing time, total with VAT, employee, revenue centre, workstation, the tenders and the
    check information lines; kept in data/guest_checks.db (closed days are not read again).
  * Billy (billy.py): the invoices of the company's NUIS that went through the bridge (with their IIC, and their FIC
    once fiscalized) and those of the fiscalization service (VAT groups, buyer, payment methods).

Where an invoice belongs: its business unit (businUnitCode) and till (tcrcode) are the restaurant of the mapping in
Settings ("code = Simphony reference | label"; a till's line wins over its unit's). A code not mapped is placed where
its invoices match the checks of one restaurant best (proposed, shown as such); a till whose invoices match no check,
like a kiosk with its own software, stays a till of its unit with no Simphony checks.

Matching, each invoice used once, restaurant by restaurant:
  1. the invoice's IIC, FIC or number written in the check's information lines or reference;
  2. the same total (within the tolerance) and the invoice issued within the time window of the check's closing,
     the nearest first; then, for a check still alone, the same total issued while the check was open (the bill
     fiscalized when asked for, the check closed when paid), the latest first;
A check left is "Not fiscalized" (or still "Pending" on the last business day); an invoice left is "No Simphony check";
an invoice of the bridge with no FIC did not reach the Tax Administration. Fiscalized more than the late minutes after
the check closed is "Late", more than 48 hours (the offline limit) "Over 48 h".

Shifts and operators: the checks by their employee and the invoices by their operator code, in the shifts of Settings
(by the local time of closing or issue); an employee's operator is the one most of their matched checks carry.
Summary invoices: the invoices that carry a list of IIC references (sumInvIICRefs), with which of those are found.
The sales book: every invoice of the fiscalization service in the columns of the law (exempt, without VAT, zero rate
and exports, taxable and VAT by rate), with the checks of its mapping: the totals, the VAT groups, the rates and the
buyer. Nothing is written to Simphony, Billy or the Tax Administration.

The period is analysed a few days at a time (parts, see build), with the invoices of the three days after each part,
so 30 to 60 days fit in the tower's memory; the rows of the tables are kept on disk (data/billyrec_rows.db) and the
screen reads them a page at a time.
"""
import ctypes
import gc
import os
import gzip
import io
import json
import re
import sqlite3
import threading
import time
import traceback
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta

import billy
import locintel
import simphony
import store

DB_PATH = store.DATA_DIR / "guest_checks.db"
CHECK_VERSION = 2               # 2: the cancel, return, void, error correct, reopen, transfer and split marks of a check
DEFAULTS = {"days": "7", "window": "20", "tol": "1", "late": "60", "rates": "20, 10, 6, 0",
            "shifts": "06:00-16:00 Morning; 16:00-06:00 Evening", "map": "", "operators": "", "exclude": "", "read_time": "07:30"}
STATUS = {"ok": "Fiscalized", "late": "Fiscalized late", "late48": "Fiscalized after 48 h", "missing": "Not fiscalized",
          "pending": "Pending", "zero": "No amount"}
INV_STATUS = {"matched": "With a Simphony check", "no_check": "No Simphony check", "no_fic": "Not fiscalized (no FIC)",
              "other_till": "Till without Simphony", "summary": "Summary invoice"}
ERRORS = {"total": "Total ≠ value without VAT + VAT", "base_sum": "VAT groups ≠ value without VAT",
          "vat_sum": "VAT of the groups ≠ VAT of the invoice", "vat_rate": "VAT ≠ value × rate",
          "rate": "Rate not in the law", "exempt_vat": "Exempt group with VAT", "no_group": "No VAT group, no exemption",
          "buyer_id": "Buyer NUIS not valid", "b2b_cash": "Business buyer without an e-invoice",
          "not_in_vat": "Issuer not in VAT on the invoice", "corrective": "Negative invoice not corrective",
          "no_fic": "No FIC", "no_detail": "VAT groups not given by Billy (split from the totals)",
          "mixed": "More than one VAT rate: groups needed"}
INFO = ("no_fic", "no_detail")                      # not a mapping error of the book
KEEP = 3 * 3600                 # the days end yesterday: a read holds for three hours (Read again reads at once)
DETAIL_CAP = 4000               # invoices read in full per read (the rest the next read)
LATE_LIMIT = 72 * 3600
OPEN_LIMIT = 12 * 3600          # an invoice issued while its check was open: at most this long before the closing
SIM_WORKERS = 6                 # Simphony days read at the same time (Reporting and Analytics answers each in seconds)
_billy_gate = threading.Semaphore(3)
_TOKEN = re.compile(r"[A-Z0-9][A-Z0-9/_-]{5,}")          # a check is looked for in invoices fiscalized up to three days after it closed
_results, _jobs, _lock, _db_lock = {}, {}, threading.Lock(), threading.Lock()
_conn = None
on_built = None


def settings_of(settings):
    s = dict(DEFAULTS, **{k: v for k, v in ((settings or {}).get("billy_rec") or {}).items() if v not in (None,)})

    def num(k, lo, hi):
        try:
            return max(lo, min(hi, float(s[k])))
        except (TypeError, ValueError):
            return float(DEFAULTS[k])
    rates = sorted({round(float(x), 2) for x in re.findall(r"\d+(?:\.\d+)?", str(s.get("rates") or DEFAULTS["rates"]))})
    return {"days": int(num("days", 1, 62)), "window": num("window", 1, 240), "tol": num("tol", 0, 1000),
            "late": num("late", 1, 2880), "rates": rates or [20.0, 10.0, 6.0, 0.0], "shifts": shifts(s.get("shifts")),
            "shifts_text": str(s.get("shifts") or DEFAULTS["shifts"]), "map": str(s.get("map") or ""),
            "operators": str(s.get("operators") or ""),
            # restaurants of the company not fiscalized with its NUIS (a franchise): left out of the match
            "exclude": sorted({x.upper() for x in re.split(r"[\s,;]+", str(s.get("exclude") or "")) if x}),
            # the daily read of every company, before the 08:00 reports (empty: no daily read)
            "read_time": str(s.get("read_time") or "").strip()[:5] if re.fullmatch(r"\d{1,2}:\d{2}", str(s.get("read_time") or "").strip()) else ""}


def shifts(text):
    """'06:00-16:00 Morning; 16:00-06:00 Evening' → [(from minutes, to minutes, name)]."""
    out = []
    for part in re.split(r"[;\n]+", text or DEFAULTS["shifts"]):
        m = re.match(r"\s*(\d{1,2}):?(\d{2})\s*-\s*(\d{1,2}):?(\d{2})\s*(.*)", part)
        if m:
            a, b = int(m[1]) * 60 + int(m[2]), int(m[3]) * 60 + int(m[4])
            out.append((a, b, m[5].strip() or f"{m[1]}:{m[2]}–{m[3]}:{m[4]}"))
    return out or [(0, 1440, "All day")]


def shift_of(stamp, S):
    try:
        t = int(stamp[11:13]) * 60 + int(stamp[14:16])
    except (TypeError, ValueError):
        return "–"
    for a, b, name in S["shifts"]:
        if (a <= t < b) if a < b else (t >= a or t < b):
            return name
    return "Other"


def mapping(text):
    """'code = locRef | label' lines → {code: (ref, label)}; 'operator code = employee number' apart."""
    out = {}
    for line in (text or "").splitlines():
        line = line.split("#", 1)[0].strip()
        if "=" not in line:
            continue
        code, _, rest = line.partition("=")
        ref, _, label = rest.partition("|")
        if code.strip() and ref.strip():
            out[code.strip().lower()] = (ref.strip(), label.strip())
    return out


def companies(settings=None):
    return billy.companies()


def restaurants(company, settings=None):
    return [r for r in locintel.registry(settings) if (r.get("company") or "").upper() == (company or "").upper()]


# ---------------------------------------------------------------- Simphony guest checks, kept by day
def _db():
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript("""
            CREATE TABLE IF NOT EXISTS chk (ref TEXT, day TEXT, id TEXT, j TEXT, PRIMARY KEY (ref, day, id));
            CREATE TABLE IF NOT EXISTS chkdays (ref TEXT, day TEXT, final INTEGER, at REAL, PRIMARY KEY (ref, day));
            CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
        """)
        v = _conn.execute("SELECT v FROM meta WHERE k='check_version'").fetchone()
        if not v or int(v[0]) < CHECK_VERSION:          # the checks kept before carry fewer fields: read the days again
            _conn.execute("UPDATE chkdays SET final=0, at=0")
            _conn.execute("INSERT OR REPLACE INTO meta VALUES ('check_version', ?)", (str(CHECK_VERSION),))
            _conn.commit()
    return _conn


def _txt(v):
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return " ".join(_txt(x) for x in v)
    if isinstance(v, dict):
        return " ".join(_txt(x) for x in v.values())
    return str(v)


def _flag(v):
    return v is True or str(v).lower() in ("true", "1", "y", "yes")


def check_of(g):
    """A Simphony guest check → the fields the match uses, and what was voided, returned, error corrected, reopened,
    transferred or split on it (Returns tracking)."""
    lines = g.get("detailLines") or []
    tenders, ws, info = Counter(), None, [_txt(g.get("chkInfo")), _txt(g.get("chkRef")), _txt(g.get("posChkGUID"))]
    ev = {"vd": 0.0, "vd_n": 0, "rt": 0.0, "rt_n": 0, "ec": 0.0, "ec_n": 0, "reasons": set(), "mgr": set()}
    for d in lines:
        mi = d.get("menuItem") if isinstance(d.get("menuItem"), dict) else {}
        amt = abs(float(d.get("dspTtl") or 0))
        kind = ("rt" if _flag(mi.get("returnFlag")) or _flag(d.get("returnFlag")) or _flag(d.get("rtrnFlag"))
                else "vd" if _flag(d.get("vdFlag")) else "ec" if d.get("errorCorrect") or _flag(d.get("errCorFlag")) else "")
        if kind:
            ev[kind] += amt
            ev[kind + "_n"] += 1
            r = d.get("reasonCodeNum") or d.get("rsnCodeNum") or d.get("reasonCode")
            if r not in (None, ""):
                ev["reasons"].add(str(r))
            if d.get("mgrEmpNum") not in (None, ""):
                ev["mgr"].add(str(d.get("mgrEmpNum")))
        if ws is None and d.get("wsNum") is not None:
            ws = d.get("wsNum")
        tm = d.get("tenderMedia")
        if tm:
            tenders[str(tm.get("tmedNum") if isinstance(tm, dict) else tm)] += float(d.get("dspTtl") or 0)
            if d.get("wsNum") is not None:
                ws = d.get("wsNum")
        if d.get("refInfo1") or d.get("refInfo2"):
            info.append(_txt(d.get("refInfo1")) + " " + _txt(d.get("refInfo2")))
    # the check's own totals of returns and error corrects, by any of the names the API versions use
    for k, v in g.items():
        kl = str(k).lower()
        if kl.endswith("ttl") and isinstance(v, (int, float)):
            if "rtrn" in kl or "return" in kl:
                ev["rt"] = max(ev["rt"], abs(float(v)))
            elif "errcor" in kl:
                ev["ec"] = max(ev["ec"], abs(float(v)))
            elif kl == "vdttl":
                ev["vd"] = max(ev["vd"], abs(float(v)))
    mark = {"vd": round(ev["vd"], 2), "vd_n": ev["vd_n"], "rt": round(ev["rt"], 2), "rt_n": ev["rt_n"],
            "ec": round(ev["ec"], 2), "ec_n": ev["ec_n"], "reasons": sorted(ev["reasons"])[:6], "mgr": sorted(ev["mgr"])[:4],
            "reopen": _flag(g.get("reopnClsdChkFlag")), "reopen_at": (g.get("reopnClsdChkOpnLcl") or "")[:19],
            "from": g.get("reopnFrmChk"), "to": g.get("reopnToChk"), "xfer": g.get("xferStatus") or "",
            "xfer_to": g.get("xferToChkNum"), "split": g.get("splitOut") or 0, "added": g.get("addedIn") or 0,
            "upd": (g.get("lastUpdatedLcl") or g.get("lastTransLcl") or "")[:19], "prints": g.get("numChkPrntd") or 0}
    return {"v": CHECK_VERSION, **{k: v for k, v in mark.items() if v not in (0, 0.0, "", None, [], False)},
            "id": str(g.get("guestCheckId") or g.get("chkNum")), "num": g.get("chkNum"), "name": g.get("chkName") or "",
            "opn": (g.get("opnLcl") or "")[:19], "clsd": (g.get("clsdLcl") or g.get("reopnClsdChkClsdLcl") or "")[:19],
            "total": float(g.get("chkTtl") or 0), "sub": float(g.get("subTtl") or 0), "tax": float(g.get("taxCollTtl") or 0),
            "disc": float(g.get("dscTtl") or 0), "rvc": g.get("rvcNum"), "emp": g.get("empNum"), "ws": ws,
            "cancel": bool(g.get("cancelFlag")), "info": " ".join(x for x in info if x).strip()[:400],
            "tenders": {k: round(v, 2) for k, v in tenders.items() if v}}


def read_checks(ref, day, latest=None):
    with _db_lock:
        hit = _db().execute("SELECT final, at FROM chkdays WHERE ref=? AND day=?", (ref, day)).fetchone()
    if hit and (hit[0] or time.time() - hit[1] < KEEP):
        return False
    j = simphony.call("getGuestChecks", {"locRef": ref, "clsdBusDt": day, "clsdGuestChecksOnly": True}) or {}
    rows = [check_of(g) for g in j.get("guestChecks") or [] if isinstance(g, dict)]
    # closed: two days before the restaurant's latest business date (today when Simphony could not say), never read again
    final = day <= ((date.fromisoformat(latest) if latest else date.today()) - timedelta(days=2)).isoformat()
    with _db_lock:
        db = _db()
        db.execute("DELETE FROM chk WHERE ref=? AND day=?", (ref, day))
        db.executemany("INSERT OR REPLACE INTO chk VALUES (?,?,?,?)",
                       [(ref, day, c["id"], json.dumps(c, separators=(",", ":"))) for c in rows])
        db.execute("INSERT OR REPLACE INTO chkdays VALUES (?,?,?,?)", (ref, day, int(final), time.time()))
        db.commit()
    return True


def checks_held(ref, a, b):
    with _db_lock:
        rows = _db().execute("SELECT day, j FROM chk WHERE ref=? AND day>=? AND day<=?", (ref, a, b)).fetchall()
    return [dict(json.loads(j), day=d, ref=ref) for d, j in rows]


# ---------------------------------------------------------------- the match
def _ts(s):
    try:
        return datetime.fromisoformat(s[:19]).timestamp()
    except (TypeError, ValueError):
        return None


def _match(checks, invs, S, used, late_step=True, early=True, hold=frozenset()):
    """Pairs checks with invoices (in place: c['inv'] = invoice key); returns the pairs. early: steps 1 and 2;
    late_step: step 3, which leaves alone the invoices in hold (those the next part's checks take at once)."""
    pairs = []
    win = S["window"] * 60
    if early:
        by_key = {}
        for i in invs:
            for k in (i["iic"], i["fic"], i["num"]):
                if k and len(k) >= 6:
                    by_key[k.upper()] = i
        for c in checks:                                    # 1. the fiscal reference written on the check
            if c.get("inv") or not c["info"]:
                continue
            for tok in _TOKEN.findall(c["info"].upper()):     # each word of the check's lines looked up, not scanned
                i = by_key.get(tok)
                if i and i["_k"] not in used:
                    used.add(i["_k"]); c["inv"] = i["_k"]; c["how"] = "reference"; pairs.append((c, i))
                    break
    # the invoices by rounded amount and hour: a check looks only at the hours its window covers, so the work grows
    # with the days read, not with their square (a common amount gathers thousands of invoices over 30 days)
    by_amt = defaultdict(list)
    for i in invs:
        if i["_k"] not in used and i["total"] is not None and i["_t"] is not None:
            by_amt[(round(i["total"]), int(i["_t"] // 3600))].append(i)
    cand = []                                            # 2. the same total, issued near the closing
    for c in (checks if early else ()):
        if c.get("inv") or c["_t"] is None:
            continue
        h0, h1 = int((c["_t"] - win) // 3600), int((c["_t"] + win) // 3600)
        for a in range(round(c["total"] - S["tol"]) - 1, round(c["total"] + S["tol"]) + 2):
            for h in range(h0, h1 + 1):
                for i in by_amt.get((a, h), ()):
                    if abs(i["total"] - c["total"]) > S["tol"] + 0.005:
                        continue
                    dt = i["_t"] - c["_t"]
                    if abs(dt) <= win:
                        cand.append((abs(dt), c["id"], i["_k"], c, i))
    cand.sort(key=lambda x: (x[0], x[1], x[2]))
    for _d, _cid, _ik, c, i in cand:
        if c.get("inv") or i["_k"] in used:
            continue
        used.add(i["_k"]); c["inv"] = i["_k"]; c["how"] = "amount and time"; pairs.append((c, i))
    # 2b. the bill fiscalized while the check was still open (printed when the guest asked for it, the check closed
    # when paid, after the window): the same total, issued after the opening, the latest first (QA 14 Nov 2026:
    # SALT's checks closed after midnight showed "Not fiscalized" with every invoice fiscalized in Billy)
    opened = []
    for c in (checks if early else ()):
        o = c.get("_o")
        if c.get("inv") or c["_t"] is None or o is None or o >= c["_t"] - win:
            continue
        o = max(o, c["_t"] - OPEN_LIMIT)
        h0, h1 = int(o // 3600), int((c["_t"] - win) // 3600)
        for a in range(round(c["total"] - S["tol"]) - 1, round(c["total"] + S["tol"]) + 2):
            for h in range(h0, h1 + 1):
                for i in by_amt.get((a, h), ()):
                    if i["_k"] in used or abs(i["total"] - c["total"]) > S["tol"] + 0.005:
                        continue
                    if o <= i["_t"] < c["_t"] - win:
                        opened.append((c["_t"] - i["_t"], c["id"], i["_k"], c, i))
    opened.sort(key=lambda x: (x[0], x[1], x[2]))
    for _d, _cid, _ik, c, i in opened:
        if c.get("inv") or i["_k"] in used:
            continue
        used.add(i["_k"]); c["inv"] = i["_k"]; c["how"] = "amount, fiscalized while the check was open"; pairs.append((c, i))
    # 3. fiscalized late: the same total, issued after the window and up to three days after the closing
    late = []
    for c in (checks if late_step else ()):
        if c.get("inv") or c["_t"] is None:
            continue
        h0, h1 = int((c["_t"] + win) // 3600), int((c["_t"] + LATE_LIMIT) // 3600)
        for a in range(round(c["total"] - S["tol"]) - 1, round(c["total"] + S["tol"]) + 2):
            for h in range(h0, h1 + 1):
                for i in by_amt.get((a, h), ()):
                    if i["_k"] in used or i["_k"] in hold or abs(i["total"] - c["total"]) > S["tol"] + 0.005:
                        continue
                    dt = i["_t"] - c["_t"]
                    if win < dt <= LATE_LIMIT:
                        late.append((dt, c["id"], i["_k"], c, i))
    late.sort(key=lambda x: (x[0], x[1], x[2]))
    for _d, _cid, _ik, c, i in late:
        if c.get("inv") or i["_k"] in used:
            continue
        used.add(i["_k"]); c["inv"] = i["_k"]; c["how"] = "amount, fiscalized later"; pairs.append((c, i))
    return pairs


def _place(invs, checks_by_ref, M, S, own=None):
    """Each invoice → (restaurant ref, how): the mapping of its till, else of its unit, else proposed by matching.
    Proposals go till by till (a unit can hold the tills of several restaurants): the restaurant whose checks its
    invoices match best, when it holds at least 5% of them (orders are many) and three times the next restaurant.
    own: the invoices a proposal counts (those of the part's own days; the others are placed, not counted)."""
    loc = {}
    unmapped = defaultdict(list)
    for i in invs:
        hit = M.get(i["tcr"]) or M.get(i["bu"])
        if hit:
            loc[i["_k"]] = (hit[0], "mapped")
        else:
            unmapped[i["tcr"] or i["bu"] or "?"].append(i)
    proposals = {}
    for code, lst in unmapped.items():
        scores = []
        for ref, checks in checks_by_ref.items():
            used = set()
            cs = [{"id": c["id"], "total": c["total"], "_t": c["_t"], "info": c["info"], "inv": None} for c in checks]
            pairs = _match(cs, lst, S, used, late_step=False)
            scores.append((len(pairs), ref))
        scores.sort(reverse=True)
        best, second = (scores + [(0, None), (0, None)])[:2]
        if best[1] and best[0] >= max(5, 0.05 * len(lst)) and best[0] >= 3 * second[0]:
            # the part's checks matched (some to invoices of the next days), of the part's own invoices
            proposals[code] = (best[1], best[0], sum(1 for i in lst if own is None or own(i)))
            for i in lst:
                loc[i["_k"]] = (best[1], "proposed")
        else:
            for i in lst:
                loc[i["_k"]] = ("", "unmapped")
    return loc, proposals


# ---------------------------------------------------------------- the sales book
def book_row(i, S):
    """An invoice → its sales book columns and the errors of its mapping to them."""
    errs, cols = [], {"exempt": 0.0, "no_vat": 0.0, "zero": 0.0}
    rates = S["rates"]
    for r in rates:
        if r > 0:
            cols[f"b{r:g}"], cols[f"v{r:g}"] = 0.0, 0.0
    tot, wo, vat = i.get("total"), i.get("wo_vat"), i.get("vat")
    taxes = i.get("taxes") or []
    if not taxes:
        errs.append("no_detail")                         # Billy's list gives no VAT groups: split from the totals
        if tot is not None and vat:
            base = tot - vat
            r = vat / base * 100 if base else None
            near = min((x for x in rates if x > 0), key=lambda x: abs(x - r), default=None) if r is not None else None
            if near is not None and abs(near - r) <= 0.6:
                cols[f"b{near:g}"] += base; cols[f"v{near:g}"] += vat
            else:
                errs.append("mixed")                     # more than one rate on the invoice: the groups are needed
                top = max((x for x in rates if x > 0), default=None)
                if top is not None:
                    cols[f"b{top:g}"] += base; cols[f"v{top:g}"] += vat
        elif tot is not None:
            cols["no_vat" if i.get("in_vat") is False else "zero"] += tot
    for rate, exempt, base, v, _n in taxes:
        if exempt:
            cols["exempt"] += base
            if v and abs(v) > 0.5:
                errs.append("exempt_vat")
            continue
        if rate is None:
            cols["no_vat"] += base
            if i.get("in_vat") is not False:
                errs.append("no_group")
            continue
        if rate not in rates:
            errs.append("rate")
            continue
        if rate == 0:
            cols["zero"] += base
            continue
        cols[f"b{rate:g}"] += base; cols[f"v{rate:g}"] += v
        if abs(base * rate / 100 - v) > max(1.0, abs(v) * 0.005):
            errs.append("vat_rate")
    if taxes:
        if wo is not None and abs(sum(t[2] for t in taxes) - wo) > 1:
            errs.append("base_sum")
        if vat is not None and abs(sum(t[3] or 0 for t in taxes) - vat) > 1:
            errs.append("vat_sum")
    if tot is not None and wo is not None and abs((wo or 0) + (vat or 0) - tot) > 1:
        errs.append("total")
    bid = (i.get("buyer_id") or "").upper()
    if bid and (i.get("buyer_type") or "NUIS").upper() == "NUIS":
        if not billy.NUIS.match(bid):
            errs.append("buyer_id")
        elif not i.get("einv"):
            errs.append("b2b_cash")
    if i.get("in_vat") is False:
        errs.append("not_in_vat")
    if tot is not None and tot < 0 and not i.get("corr"):
        errs.append("corrective")
    if not i.get("fic"):
        errs.append("no_fic")
    return {k: round(v, 2) for k, v in cols.items()}, sorted(set(errs))


# ---------------------------------------------------------------- build, part by part
# The tower has 450 MB (deploy/docker-compose.yml) and one invoice of the analysis holds about 7 KB with its check:
# the whole of 30 or 50 days at once does not fit (1.1 GB and 1.85 GB at SALT, measured 10 Nov 2026). So the period
# is analysed a part of CHUNK_DAYS days at a time (5: about 210 MB at SALT's size, whatever the days; 7: 260 MB),
# oldest first, each with the invoices of OVERLAP_DAYS days after it
# (a check fiscalized up to 72 hours later); the rows of every table go to disk (data/billyrec_rows.db) and the
# screen asks for them a page at a time; in memory stays only what the screen shows at once (totals, restaurants by
# day, tills, shifts). An invoice belongs to the part of the day it was issued: an invoice of the next days taken by
# a check of this part is passed on as matched, and one the next part's checks take at once is left to them.
CHUNK_DAYS = max(1, int(os.environ.get("BILLYREC_CHUNK_DAYS") or 5))
OVERLAP_DAYS = 3
ROWS_FILE = "billyrec_rows.db"


def _dates(a, b):
    out, d = [], a
    while d <= b:
        out.append(d)
        d += timedelta(days=1)
    return out


def parts(days, size=None):
    size = size or CHUNK_DAYS
    return [days[k:k + size] for k in range(0, len(days), size)]


def load_invoices(nuis, a, b):
    """{key: invoice} of both methods between two Tirana days, one per IIC: the fiscal service's detail over the
    bridge's row, the bridge's FIC status kept; each with the day it is kept under (_d)."""
    inv = {}
    for src in ("bridge", "fiscal"):
        for day, x in billy.iter_held(nuis, src, a, b):
            k = x["iic"] or x["fic"] or x["num"]
            keep = inv.get(k) if src == "fiscal" else None
            if keep is not None:
                fic = x["fic"] or keep["fic"]
                for f, v in x.items():
                    if v not in (None, "", [], {}) and f not in ("src", "fic"):
                        keep[f] = v
                keep.update(src="fiscal", in_bridge=True, fic=fic)
            else:
                x.update(_k=k, _d=day, in_bridge=src == "bridge")
                inv[k] = x
    return inv


def corrective_index(nuis, a, b):
    """{IIC referred to: [corrective invoices]} of the whole period: a corrective issued days after the check closes
    its event by reference even when it falls in a later part. Correctives are few: found by their text first."""
    out = {}
    for src in ("bridge", "fiscal"):
        for day, x in billy.iter_held(nuis, src, a, b, where=billy.CORRECTIVE_SQL):
            k = x["iic"] or x["fic"] or x["num"]
            keep = out.get(k) if src == "fiscal" else None
            if keep is not None:
                fic = x["fic"] or keep["fic"]
                keep.update({f: v for f, v in x.items() if v not in (None, "", [], {}) and f not in ("src", "fic")})
                keep.update(src="fiscal", fic=fic)
            else:
                x.update(_k=k, _d=day)
                out[k] = x
    idx = defaultdict(list)
    for x in out.values():
        if x.get("corr_ref") and not x.get("order") and (x.get("corr") or (x.get("total") or 0) < -0.005):
            x["_t"] = _ts(x["issue"])
            idx[x["corr_ref"].upper()].append(x)
    return dict(idx)


def apply_details(nuis, inv):
    """The invoices already read in full (kept in data/billy_v2.db) into the part: the payment methods and VAT groups
    Billy's lists leave out, and the orders. An invoice that closes a check lists the IICs of the orders it closes,
    and an order says ORDER: those are orders, not invoices. Returns the IICs read in full."""
    full = billy.details_held(nuis, [x["iic"] for x in inv.values() if x["iic"]])
    if not full:
        return set()
    closes, orders = {}, set()
    for k, d in full.items():
        c = billy.compact(d, "fiscal")
        if c["order"]:
            orders.add(k)
        else:
            for ref in c["refs"]:
                closes[ref.upper()] = k                  # this order is closed by invoice k
    orders |= set(closes)
    comp = {k: billy.compact(d, "fiscal") for k, d in full.items()}
    del full
    for x in inv.values():
        c = comp.get(x["iic"])
        if c:
            x["pays"] = c["pays"]
            if c["taxes"]:
                x["taxes"] = c["taxes"]
            if c["refs"] and all(ref.upper() in orders for ref in c["refs"]):
                x["orders_of"], x["refs"] = c["refs"], []      # it closes orders: a plain invoice, not a summary
            else:
                x["refs"] = c["refs"] or x.get("refs") or []
        if x["iic"] in orders:
            x["order"], x["closed_by"] = True, closes.get(x["iic"], "")
    return set(comp)


MAX_RANGE = 62


def check_range(frm, to, today=None):
    """A range asked on the screen → (first day, last day); its last day yesterday at the latest, MAX_RANGE days."""
    today = today or date.today()
    try:
        a, b = date.fromisoformat(str(frm)[:10]), date.fromisoformat(str(to)[:10])
    except ValueError:
        raise ValueError("Choose the first and the last day of the range.") from None
    if b >= today:
        raise ValueError("The range ends yesterday at the latest: today is not closed yet.")
    if a > b:
        raise ValueError("The first day is after the last one.")
    if (b - a).days + 1 > MAX_RANGE:
        raise ValueError(f"At most {MAX_RANGE} days at a time.")
    return a, b


def key_of(company, frm=None, to=None):
    """The held read: the company's (the last days of Settings), or of a range asked on the screen."""
    return f"{company}|{frm}|{to}" if frm and to else company


def build(company, latest_fn, progress=None, today=None, frm=None, to=None):
    """A read of the company: the last days of Settings, or the range frm–to (a range of its own: the invoices
    of the three days after it are read too, for the checks fiscalized late)."""
    today = today or date.today()
    settings = store.get_settings()
    S = settings_of(settings)
    acct = billy.account(company)
    if not acct:
        raise ValueError(f"{company} has no Billy account with a password (Finance → Settings → Billy).")
    regs = [r for r in restaurants(company, settings) if (r.get("ref") or "").upper() not in S["exclude"]]
    yday = today - timedelta(days=1)
    if frm or to:
        days = _dates(*check_range(frm, to, today))
    else:
        days = _dates(yday - timedelta(days=S["days"] - 1), yday)
    end = min(days[-1] + timedelta(days=OVERLAP_DAYS), yday)      # the days read: the period and the days after it,
    lead = _dates(days[0] - timedelta(days=OVERLAP_DAYS), days[0] - timedelta(days=1))   # and the days before it
    read_days = lead + _dates(days[0], end)
    say = progress or (lambda *_: None)
    errors = []
    # Billy (both methods, day by day) and Simphony (each restaurant's checks, day by day) read at the same time
    sim_on = bool(simphony.info().get("configured"))
    latest = {}
    if sim_on and regs:
        for r in regs:
            try:
                latest[r["ref"]] = latest_fn(r["ref"])
            except Exception:                                       # noqa: BLE001
                latest[r["ref"]] = None
    try:
        billy._token(acct)                                          # one sign-in before the reads in parallel
    except billy.BillyError as exc:
        raise ValueError(str(exc)) from exc
    jobs = [("billy", src, d) for d in read_days for src in ("bridge", "fiscal")]
    if sim_on:
        jobs += [("sim", r["ref"], d.isoformat()) for r in regs for d in read_days
                 if not latest.get(r["ref"]) or d.isoformat() <= latest[r["ref"]]]
    n_sim = sum(1 for j in jobs if j[0] == "sim")
    # a job is one day of one source: the days of Settings × (Billy's two methods + each restaurant in Simphony)
    say(f"{len(read_days)} days · {len(jobs):,} reads: {len(jobs) - n_sim:,} of Billy, {n_sim:,} of Simphony "
        f"({len({j[1] for j in jobs if j[0] == 'sim'})} restaurants)")

    def run(job):
        kind, x, d = job
        if kind == "billy":
            with _billy_gate:                 # a Billy day is ~5,000 invoices in memory: three at a time at most
                return billy.read_day(acct, x, d, today)
        return read_checks(x, d, latest.get(x))
    # Billy and Simphony each in their own pool, side by side: in one shared pool the Billy days waiting for the
    # gate held workers the Simphony days could have used. The newest days first: they are the ones read again.
    jobs.sort(key=lambda j: str(j[2]), reverse=True)
    with ThreadPoolExecutor(max_workers=3) as bpool, ThreadPoolExecutor(max_workers=SIM_WORKERS) as spool:
        futs = {(bpool if j[0] == "billy" else spool).submit(run, j): j for j in jobs}
        for n, f in enumerate(as_completed(futs), 1):
            kind, x, d = futs[f]
            try:
                f.result()
            except billy.BillyError as exc:
                errors.append(f"Billy {x} {d}: {str(exc)[:160]}")
            except Exception as exc:                                # noqa: BLE001
                if len(errors) < 12:
                    errors.append(f"{'Simphony' if kind == 'sim' else 'Billy'} {x} {d}: {str(exc)[:140]}")
            if n % 10 == 0:
                say(f"{len(read_days)} days · {n:,} of {len(jobs):,} reads (days × Billy's two methods and the restaurants)")
    nuis = acct["nuis"]
    gen = f"{re.sub(r'[^A-Za-z0-9_-]+', '_', company)}-{time.time():.3f}"
    acc = _Acc(company, nuis, days, S, regs)
    W = _Writer(gen, acc.keys)
    P = parts(days)
    # the days before the period, matched first and not shown: their checks fiscalized late take the invoices of
    # the period's first days, which are then not counted as without a check (as a read of more days would do)
    P = [lead] + P
    try:
        corr_idx = corrective_index(nuis, lead[0], yday)      # to yesterday: a corrective days after a range
        pre_used, ret_used, budget, left = set(), set(), DETAIL_CAP, 0
        for n, part in enumerate(P):
            a, z = part[0], part[-1]
            hi = min(z + timedelta(days=OVERLAP_DAYS), end)
            tag = "the days before · " if n == 0 else f"part {n} of {len(P) - 1} · " if len(P) > 2 else ""
            inv = load_invoices(nuis, a, hi)
            full = apply_details(nuis, inv)
            checks = {r["ref"]: checks_held(r["ref"], a.isoformat(), z.isoformat()) for r in regs} if sim_on else {}
            nxt = None
            if sim_on and hi > z:              # the next part's checks, to leave them the invoices they take at once
                nxt = {r["ref"]: [{"id": c["id"], "total": c["total"], "info": c["info"], "_t": _ts(c["clsd"]),
                                   "_o": _ts(c.get("opn") or ""), "inv": None}
                                  for c in checks_held(r["ref"], (z + timedelta(days=1)).isoformat(), hi.isoformat())
                                  if not c["cancel"] and abs(c["total"]) >= 0.01] for r in regs}
            say(f"{tag}matching {sum(map(len, checks.values())):,} checks with {len(inv):,} invoices of {len(part)} days")
            # Pending: the last business day (when Simphony does not say which), yesterday for a range too
            args = dict(last_day=yday.isoformat(), pre_used=pre_used, nxt=nxt, ret_used=ret_used, corr_index=corr_idx,
                        number_of=lambda k: billy.number_of(nuis, k, lead[0], end))
            res = analyse(company, nuis, regs, part, inv, checks, latest, S, today, **args)
            # Orders. Billy's lists leave out the payment method and the references (payMethods, sumInvIICRefs
            # null). In full, the invoice that closes a check (cash, card, account) lists the IICs of the orders it
            # closes. So the invoices matched to a check, at the restaurants with table service (documents without
            # a check at least half the checks, or orders found already), are read in full once: each tells its
            # orders at once, a few hundred calls instead of one per order. Then the part is matched again.
            nchk = Counter(c["ref"] for c in res["checks"] if c["status"] != "zero")
            rest = {ref for ref, k in res["nochk"].items() if ref and k >= max(20, 0.5 * nchk.get(ref, 0))} | res["order_refs"]
            cand = {c["iic"]: c["fic"] for c in res["checks"] if c["ref"] in rest and c["fic"] and c["iic"]}
            todo = [k for k in cand if k not in full][:max(0, budget)] if n else []
            if todo:
                budget -= len(todo)
                say(f"Billy · {len(todo)} invoices in full, for their orders")

                def one(k):
                    billy.keep_detail(nuis, k, billy.detail(acct, cand[k]))
                with ThreadPoolExecutor(max_workers=8) as pool:
                    futs = [pool.submit(one, k) for k in todo]
                    for m, f in enumerate(as_completed(futs), 1):
                        try:
                            f.result()
                        except Exception as exc:                       # noqa: BLE001
                            if len(errors) < 12:
                                errors.append(f"Billy invoice in full: {str(exc)[:140]}")
                        if m % 50 == 0:
                            say(f"Billy · invoices in full, for their orders ({m} of {len(todo)})")
                new = apply_details(nuis, inv)
                if new - full:
                    full = new
                    say(f"{tag}matching again, with the orders apart")
                    del res
                    res = analyse(company, nuis, regs, part, inv, checks, latest, S, today, **args)
            if n:                                    # the days before: what they take is passed on, nothing shown
                left += len([k for k in cand if k not in full])
                W.put(res)
                acc.add(res)
            pre_used, ret_used = res["forward"], ret_used | res["ret_used"]
            del inv, checks, nxt, res, cand
            _trim()
        acc.finish(W)
        W.done()
    except BaseException:
        W.drop()
        raise
    note = (f"{left} invoice{'' if left == 1 else 's'} still to be read in full for their orders; "
            f"the next read continues.") if left else ""
    r = acc.result(gen)
    if sim_on:
        say("Simphony · checks still open")
        r["open"] = open_checks([x["ref"] for x in regs], days, latest, errors)
        r["kpis"] = _kpis(r, None)
    nb, nf = billy.counts(nuis, "bridge", days[0], days[-1]), billy.counts(nuis, "fiscal", days[0], days[-1])
    r.update(errors=errors, checked_at=datetime.now().isoformat(timespec="seconds"), complete=not errors,
             simphony=sim_on, orders_note=note,
             range={"from": days[0].isoformat(), "to": days[-1].isoformat(), "custom": bool(frm or to)},
             # the days Billy gave no invoice for (before what Billy keeps, or a day closed)
             empty_days=[d.isoformat() for d in days if not nb.get(d.isoformat()) and not nf.get(d.isoformat())])
    _trim()
    return r


def open_checks(refs, days, latest, errors=None):
    """{ref: {day: [checks, value]}} still open at the end of each business day (operations daily totals:
    chkOpnCnt, chkOpnTtl): a check left open is not fiscalized (QA 14 Nov 2026: the KPI belongs here, not on Sales).
    The days are those Simphony keeps (simphony.daily_totals), read once."""
    out = {}
    jobs = [(r, d.isoformat()) for r in refs for d in days if not latest.get(r) or d.isoformat() <= latest[r]]

    def one(job):
        r, d = job
        rv = (simphony.daily_totals(r, d, latest.get(r)) or {}).get("rvcs") or []
        return (sum(int(x.get("chkOpnCnt") or 0) for x in rv), round(sum(float(x.get("chkOpnTtl") or 0) for x in rv), 2))
    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = {pool.submit(one, j): j for j in jobs}
        for f in as_completed(futs):
            r, d = futs[f]
            try:
                n, v = f.result()
            except Exception as exc:                                # noqa: BLE001
                if errors is not None and len(errors) < 12:
                    errors.append(f"Simphony {r} {d}, open checks: {str(exc)[:120]}")
                continue
            if n:
                out.setdefault(r, {})[d] = [n, v]
    simphony.flush_days()
    return out


def _trim():
    """The tower has 450 MB: after a part the lists of invoices and checks are freed and handed back to the system."""
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass


def analyse(company, nuis, regs, days, inv, checks, latest, S, today, last_day=None, pre_used=frozenset(), nxt=None,
            ret_used=frozenset(), corr_index=None, number_of=None):
    """One part of the period. days: its own days; inv: {key: invoice} (load_invoices), with those of the days
    after it; checks: {ref: the checks of its days}. pre_used: the invoices the part before took (matched, not
    taken again); nxt: {ref: the next days' checks}; last_day: the last day of the whole period (Pending).
    Returns its rows (only the invoices of its own days) and what is added up across the parts."""
    M = mapping(S["map"])
    names = {r["ref"]: r.get("name") or r["ref"] for r in regs}
    for code, (ref, label) in M.items():
        names.setdefault(ref, ref)
    own_last = days[-1].isoformat()
    last_day = last_day or own_last

    def own(x):
        return (x.get("_d") or (x.get("issue") or "")[:10]) <= own_last
    orders = [x for x in inv.values() if x.get("order")]       # payment method ORDER: an order, not an invoice
    invs = [x for x in inv.values() if not x.get("order")]
    for i in invs:
        i["_t"] = _ts(i["issue"])
    for ref, lst in checks.items():
        for c in lst:
            c["_t"] = _ts(c["clsd"])
            c["_o"] = _ts(c.get("opn") or "")
            c["inv"] = None
            c.pop("how", None)
    sold = {ref: [c for c in lst if not c["cancel"] and abs(c["total"]) >= 0.01] for ref, lst in checks.items()}
    loc_of, proposals = _place(invs, sold, M, S, own)
    for i in invs:
        i["loc"], i["placed"] = loc_of.get(i["_k"], ("", "unmapped"))
    # match restaurant by restaurant: steps 1 and 2 (reference, amount and time), then the next part's checks take
    # what they take at once, then step 3 (fiscalized later) with the rest
    used = set(pre_used)
    by_loc = defaultdict(list)
    for i in invs:
        by_loc[i["loc"]].append(i)
    for ref, lst in sold.items():
        _match(lst, by_loc.get(ref, []), S, used, late_step=False)
    # the next part's checks of the days after: what they take at once (steps 1 and 2) is left to them, and step 3
    # runs over this part's checks and theirs together, nearest first, as one read of the whole period would; only
    # this part's pairs are kept (the next part matches its own checks again)
    hold, nx, spare = set(), {}, set()
    for ref, lst in (nxt or {}).items():
        cand = [i for i in by_loc.get(ref, []) if not own(i) and i["_k"] not in used]
        if cand and lst:
            u, cs = set(used), [dict(c, id="next " + str(c["id"])) for c in lst]
            _match(cs, cand, S, u, late_step=False)
            hold |= u - used
            nx[ref] = [c for c in cs if not c.get("inv")]
    for ref, lst in sold.items():
        extra = nx.get(ref, [])
        _match(lst + extra, by_loc.get(ref, []), S, used, early=False, hold=hold)
        for c in extra:
            if c.get("inv"):
                used.discard(c["inv"])
                spare.add(c["inv"])
    inv_by = {i["_k"]: i for i in invs}
    out_checks = []
    for ref, lst in checks.items():
        for c in lst:
            i = inv_by.get(c["inv"]) if c.get("inv") else None
            if abs(c["total"]) < 0.01 or c["cancel"]:
                st, delay = "zero", None
            elif i:
                delay = round((i["_t"] - c["_t"]) / 60, 1) if i["_t"] is not None and c["_t"] is not None else None
                st = "late48" if delay is not None and delay > 48 * 60 else "late" if delay is not None and delay > S["late"] else "ok"
                if not i.get("fic"):
                    st = "missing"
                    c["how"] = "its invoice has no FIC"
            else:
                delay = None
                st = "pending" if c["day"] >= (latest.get(ref) or last_day) else "missing"
            out_checks.append({"ref": ref, "day": c["day"], "num": c["num"], "id": c["id"], "clsd": c["clsd"],
                               "total": round(c["total"], 2), "emp": c["emp"], "rvc": c["rvc"], "ws": c["ws"],
                               "shift": shift_of(c["clsd"], S), "status": st, "how": c.get("how") or "",
                               "iic": i["iic"] if i else "", "fic": i["fic"] if i else "", "inv_num": i["num"] if i else "",
                               "issued": i["issue"] if i else "", "delay_min": delay, "tcr": i["tcr"] if i else "",
                               "op": i["op"] if i else ""})
    matched_inv = {c["inv"] for lst in checks.values() for c in lst if c.get("inv")} | set(pre_used)
    refs_with_checks = {ref for ref, lst in sold.items() if lst}
    tills_with_match = {inv_by[k]["tcr"] for k in matched_inv if k in inv_by}
    for i in invs:
        if not i.get("fic"):
            st = "no_fic"
        elif i.get("refs"):
            st = "summary"
        elif i["_k"] in matched_inv:
            st = "matched"
        elif i["loc"] not in refs_with_checks or (i["tcr"] and i["tcr"] not in tills_with_match):
            st = "other_till"
        else:
            st = "no_check"
        i["status"] = st
    mine = [i for i in invs if own(i)]                          # the invoices of this part's days
    # tills and units, the kiosks included
    tills = {}
    for i in mine:
        t = tills.setdefault(i["tcr"] or "?", {"tcr": i["tcr"] or "?", "bu": i["bu"], "ref": i["loc"], "placed": i["placed"],
                                              "label": (M.get(i["tcr"]) or M.get(i["bu"]) or ("", ""))[1],
                                              "invoices": 0, "value": 0.0, "matched": 0, "no_fic": 0, "first": i["issue"],
                                              "last": i["issue"], "ops": set()})
        t["invoices"] += 1
        t["value"] += i["total"] or 0
        t["matched"] += 1 if i["status"] == "matched" else 0
        t["no_fic"] += 1 if i["status"] == "no_fic" else 0
        t["first"], t["last"] = min(t["first"], i["issue"]), max(t["last"], i["issue"])
        if i["op"]:
            t["ops"].add(i["op"])
    # shifts and operators: an employee's operator is the one most of their matched checks carry (over every part:
    # the checks are kept by employee here, their operator decided at the end)
    emp_op = defaultdict(Counter)
    for ref, lst in checks.items():
        for c in lst:
            if c.get("inv") and inv_by.get(c["inv"], {}).get("op"):
                emp_op[(ref, c["emp"])][inv_by[c["inv"]]["op"]] += 1
    by_emp = {}
    for c in out_checks:
        if c["status"] == "zero":
            continue
        g = by_emp.setdefault((c["ref"], c["shift"], c["emp"]), {"checks": 0, "sim": 0.0, "not_fisc": 0.0, "not_fisc_n": 0})
        g["checks"] += 1
        g["sim"] += c["total"]
        if c["status"] in ("missing", "pending"):
            g["not_fisc"] += c["total"]
            g["not_fisc_n"] += 1
    grid = {}
    for i in mine:
        if i["status"] in ("other_till", "summary"):
            continue
        g = grid.setdefault((i["loc"], shift_of(i["issue"], S), i["op"] or "–"), _cell(i["loc"], shift_of(i["issue"], S), i["op"] or "–"))
        g["invoices"] += 1
        g["fisc"] += (i["total"] or 0) if i.get("fic") else 0
        if i["status"] == "no_check":
            g["no_check"] += i["total"] or 0
            g["no_check_n"] += 1
    # summary invoices: which of their references are found is answered once every part is in (_Acc.finish)
    summary = []
    for i in mine:
        if i.get("refs"):
            summary.append({"iic": i["iic"], "fic": i["fic"], "num": i["num"], "issue": i["issue"], "total": i["total"],
                            "type": i["type"], "tcr": i["tcr"], "ref": i["loc"], "refs": len(i["refs"]),
                            "_refs": list(i["refs"]), "fiscalized": bool(i.get("fic"))})
    # the sales book
    book, err_count = [], Counter()
    for i in sorted(mine, key=lambda x: x["issue"] or ""):
        cols, errs = book_row(i, S)
        for e in errs:
            err_count[e] += 1
        book.append({"issue": i["issue"], "ord": i["ord"], "num": i["num"], "iic": i["iic"], "fic": i["fic"],
                     "buyer": i["buyer"], "buyer_id": i["buyer_id"], "type": i["type"], "total": i["total"],
                     "bu": i["bu"], "tcr": i["tcr"], "op": i["op"], "ref": i["loc"], "einv": i["einv"], "eic": i["eic"],
                     **cols, "errors": errs})
    # orders: placed like invoices, and whether an invoice of the same total follows on the same till
    orders = [o for o in orders if own(o)]
    oloc, _p = _place(orders, {}, M, S) if orders else ({}, {})
    by_till_total = defaultdict(list)
    for i in invs:
        if i["_t"] is not None and i["total"] is not None:
            by_till_total[(i["tcr"], round(i["total"]))].append(i["_t"])
    out_orders, till_ref = [], {t["tcr"]: t["ref"] for t in tills.values()}
    by_iic = {i["iic"]: i for i in invs}
    for o in sorted(orders, key=lambda x: x["issue"] or ""):
        t0 = _ts(o["issue"])
        nxt_t = [t for t in by_till_total.get((o["tcr"], round(o["total"] or 0)), []) if t0 is not None and 0 <= t - t0 <= 24 * 3600]
        closer = by_iic.get(o.get("closed_by") or "")
        if closer and closer["_t"] is not None and t0 is not None:
            nxt_t = [closer["_t"]]                       # the invoice that names this order among its references
        out_orders.append({"issue": o["issue"], "num": o["num"], "iic": o["iic"], "fic": o["fic"], "total": o["total"],
                           "tcr": o["tcr"], "bu": o["bu"], "op": o["op"], "ref": till_ref.get(o["tcr"]) or oloc.get(o["_k"], ("", ""))[0],
                           "pays": ", ".join(p[0] for p in o.get("pays") or []) or ("ORDER" if o.get("closed_by") else ""),
                           "closed_by": closer["num"] or closer["iic"][:12] if closer else "", "invoiced": bool(nxt_t),
                           "after_min": round((min(nxt_t) - t0) / 60) if nxt_t else None})
    returns = track_returns(checks, invs, inv_by, S, latest, last_day, ret_used=ret_used, corr_index=corr_index,
                            took=pre_used, own=own, spare=hold | spare, number_of=number_of)
    # restaurants and days
    locs = []
    checks_of_ref, invs_of_ref = defaultdict(list), defaultdict(list)
    for c in out_checks:
        checks_of_ref[c["ref"]].append(c)
    for i in mine:
        invs_of_ref[i["loc"]].append(i)
    for ref in sorted(set(checks) | {i["loc"] for i in mine if i["loc"]}):
        cs = checks_of_ref.get(ref, [])
        iv = invs_of_ref.get(ref, [])
        cd, idd = defaultdict(list), defaultdict(list)          # by day once, not the whole list for every day
        for c in cs:
            if c["status"] != "zero":
                cd[c["day"]].append(c)
        for i in iv:
            idd[(i["issue"] or "")[:10]].append(i)
        cells = {}
        for d in days:
            ds = d.isoformat()
            dc, di = cd.get(ds, []), idd.get(ds, [])
            cells[ds] = {"checks": len(dc), "sim": round(sum(c["total"] for c in dc), 2),
                         "fisc": round(sum(i["total"] or 0 for i in di if i.get("fic") and i["status"] not in ("other_till", "summary")), 2),
                         "missing": sum(1 for c in dc if c["status"] == "missing"),
                         "missing_value": round(sum(c["total"] for c in dc if c["status"] == "missing"), 2),
                         "late": sum(1 for c in dc if c["status"] in ("late", "late48")),
                         "no_check": sum(1 for i in di if i["status"] == "no_check"),
                         "pending": sum(1 for c in dc if c["status"] == "pending")}
        locs.append({"ref": ref, "name": names.get(ref, ref), "cells": cells,
                     "bc": next((r.get("bc") for r in regs if r["ref"] == ref), "") or "",
                     "dept": next((r.get("dept") for r in regs if r["ref"] == ref), "") or "",
                     "checks": sum(1 for c in cs if c["status"] != "zero"),
                     "missing": sum(1 for c in cs if c["status"] == "missing"),
                     "missing_value": sum(c["total"] for c in cs if c["status"] == "missing")})
    no_check = sorted(({"iic": i["iic"], "fic": i["fic"], "num": i["num"], "issue": i["issue"], "total": i["total"],
                        "tcr": i["tcr"], "bu": i["bu"], "op": i["op"], "ref": i["loc"], "status": i["status"],
                        "shift": shift_of(i["issue"], S)}
                       for i in mine if i["status"] in ("no_check", "no_fic")), key=lambda x: x["issue"] or "")
    own_keys = {i["_k"] for i in mine} | {o["_k"] for o in orders}
    return {"checks": out_checks, "invoices": no_check, "book": book, "orders": out_orders, "summary": summary,
            "events": returns["events"], "loose": returns["loose"], "marks": returns["marks"],
            "iics": [i["iic"] for i in mine if i["iic"]], "locations": locs, "tills": tills, "grid": grid,
            "by_emp": by_emp, "emp_op": emp_op, "names": set(names),
            "proposals": proposals, "err_count": err_count, "refs_with_checks": refs_with_checks,
            "other_till": Counter(i["loc"] for i in mine if i["status"] == "other_till"),
            "nochk": Counter(i["loc"] for i in mine if i["status"] == "no_check"),
            "order_refs": {o["ref"] for o in out_orders if o["ref"]},
            # the invoices of the next days taken here (by a check, or as a check's own corrective): passed on
            "forward": {k for k in used if k not in own_keys}, "ret_used": returns["used"]}

# ---------------------------------------------------------------- returns tracking
RET_STATUS = {"closed_ref": "Closed: corrective invoice refers to the original", "closed_self": "Closed: the return check's own corrective invoice",
              "closed_amount": "Closed: corrective invoice of the same amount", "unfiscalized": "Unfiscalized return",
              "pending": "Pending", "no_effect": "No fiscal effect"}
RET_KIND = {"return": "Return", "cancel": "Cancel", "reopen": "Reopen", "change": "Change", "void": "Void before closing",
            "ec": "Error correct before closing"}


def track_returns(checks, invs, inv_by, S, latest, last_day, ret_used=frozenset(), corr_index=None, took=frozenset(), own=None,
                  spare=frozenset(), number_of=None):
    """Every cancel, return, reopen or change of a Simphony check, and the corrective invoice in Billy that closes it.

    Needs a corrective (the invoice already fiscalized must be corrected):
      * return: a check with returned items (Simphony's return flag, or the check's returns total) or a negative total;
        the amount returned;
      * cancel: a cancelled check that has an invoice (its IIC or number on the check);
      * reopen: a closed check reopened (to a new check, or reopened in place) whose first invoice exists: that invoice;
      * change: a check whose invoice is larger than the check (beyond the tolerance): the difference.
    A corrective is a Billy invoice marked corrective or with a negative total. It closes the event:
      1. by reference: its correctiveInv names the IIC of the event's invoice (any day of the period: corr_index);
      2. the return check's own invoice, when it is a corrective (a return fiscalized at closing);
      3. by amount: a corrective of the same restaurant, issued from an hour before to 72 hours after, of the same
         amount within the tolerance (nearest first; a lead, not a proof).
    None: "Unfiscalized return" (Pending on the last business day). Voids and error corrects before closing are
    listed with no fiscal effect: nothing was fiscalized yet.
    Part by part: ret_used, the correctives an earlier part used; took, the invoices it matched to its checks; own,
    whether an invoice is of this part's days (a corrective without an event is listed by its own part); spare, the
    next days' invoices the next part's checks take as their own (not taken here by amount), nor is a corrective
    that names an invoice of the next days (its event is there); number_of: the number of an invoice of the period
    by its IIC (a corrective without an event naming an invoice of an earlier part)."""
    tol = S["tol"]
    own = own or (lambda x: True)
    corr = [i for i in invs if (i.get("corr") or (i.get("total") or 0) < -0.005) and i["_k"] not in ret_used]
    corr_keys = {i["_k"] for i in invs if i.get("corr") or (i.get("total") or 0) < -0.005}
    if corr_index is None:
        by_ref = defaultdict(list)
        for i in corr:
            if i.get("corr_ref"):
                by_ref[i["corr_ref"].upper()].append(i)
    else:
        by_ref = corr_index
    key_of = {}
    for i in invs:
        for k in (i["iic"], i["fic"], i["num"]):
            if k and len(k) >= 6:
                key_of[k.upper()] = i
    by_num = {(ref, c["day"], c["num"]): c for ref, lst in checks.items() for c in lst}
    events = []

    def ev(ref, c, kind, amount, orig=None, note=""):
        events.append({"ref": ref, "day": c["day"], "num": c["num"], "id": c["id"], "name": c.get("name") or "",
                       "opn": c.get("opn") or "", "clsd": c.get("clsd") or "", "at": c.get("reopen_at") or c.get("clsd") or "",
                       "_t": _ts(c.get("reopen_at") or c.get("clsd") or ""), "emp": c.get("emp"), "kind": kind,
                       "amount": round(amount, 2), "check_total": round(c["total"], 2), "orig": orig, "note": note,
                       "reasons": c.get("reasons") or [], "mgr": c.get("mgr") or [], "self": None})

    for ref, lst in checks.items():
        for c in lst:
            own_inv = inv_by.get(c["inv"]) if c.get("inv") else None
            if own_inv is None and c["cancel"] and c["info"]:          # a cancelled check is not matched: its IIC on it
                own_inv = next((key_of[t] for t in _TOKEN.findall(c["info"].upper()) if t in key_of), None)
            neg = c["total"] < -tol
            if neg or c.get("rt", 0) > tol:
                e_amt = max(-c["total"] if neg else 0.0, c.get("rt", 0.0))
                ev(ref, c, "return", e_amt, note=" · ".join(x for x in ("negative check" if neg else "",
                                                                         f"{c['rt_n']} returned items" if c.get("rt_n") else "") if x))
                if own_inv is not None and own_inv["_k"] in corr_keys:
                    events[-1]["self"] = own_inv
            if c["cancel"]:
                if own_inv is not None and own_inv["_k"] not in corr_keys:
                    ev(ref, c, "cancel", own_inv["total"] or 0, own_inv, "cancelled after its invoice")
                else:
                    ev(ref, c, "cancel", abs(c["total"]), None, "nothing fiscalized for it")
            if c.get("to") or c.get("reopen"):
                first = None
                if c.get("to"):                          # reopened to a new check: this check's invoice is the first
                    first = own_inv
                    nx = by_num.get((ref, c["day"], c["to"]))
                    note = f"reopened as check {c['to']}" + ("" if nx else " (not in the days read)")
                else:                                    # reopened in place: an invoice of the restaurant with no check,
                    t0, t1 = _ts(c.get("opn") or ""), _ts(c.get("reopen_at") or "")    # issued before the reopening
                    if t0 is not None and t1 is not None:
                        cand = [i for i in invs if i["loc"] == ref and i.get("status") == "no_check" and i["_t"] is not None
                                and t0 <= i["_t"] <= t1 + S["window"] * 60 and i["_k"] not in corr_keys]
                        cand.sort(key=lambda i: abs((i["total"] or 0) - c["total"]))
                        first = cand[0] if cand else None
                    note = "reopened after closing" + (f" at {c['reopen_at'][11:16]}" if c.get("reopen_at") else "")
                if first is not None:
                    ev(ref, c, "reopen", first["total"] or 0, first, note)
                else:
                    ev(ref, c, "reopen", c["total"], None, note + "; no invoice before the reopening")
            if own_inv is not None and own_inv["_k"] not in corr_keys and not c["cancel"] and (own_inv["total"] or 0) - c["total"] > tol:
                ev(ref, c, "change", (own_inv["total"] or 0) - c["total"], own_inv, "the invoice is larger than the check")
            if c.get("vd", 0) > tol and not (c["cancel"] or c.get("to") or c.get("reopen")):
                ev(ref, c, "void", c["vd"], None, f"{c.get('vd_n') or ''} voided lines".strip())
            if c.get("ec", 0) > tol and not (c["cancel"] or c.get("to") or c.get("reopen")):
                ev(ref, c, "ec", c["ec"], None, f"{c.get('ec_n') or ''} error corrected lines".strip())
    # what closes each event
    used = set(ret_used)
    for e in events:
        if e["kind"] in ("void", "ec") or (e["kind"] in ("cancel", "reopen") and e["orig"] is None):
            e["status"] = "no_effect"
            continue
        e["status"] = None
        o = e["orig"]
        if o is not None:
            hit = next((i for k in (o["iic"], o["fic"]) if k for i in by_ref.get(k.upper(), []) if i["_k"] not in used), None)
            if hit is not None:
                used.add(hit["_k"])
                e["status"], e["corr"] = "closed_ref", hit
                continue
        if e["self"] is not None and e["self"]["_k"] not in used:
            used.add(e["self"]["_k"])
            e["status"], e["corr"] = "closed_self", e["self"]
    later = {i["_k"] for i in corr if i.get("corr_ref") and key_of.get(i["corr_ref"].upper()) is not None
             and not own(key_of[i["corr_ref"].upper()])}
    for e in sorted((e for e in events if e["status"] is None), key=lambda e: e["_t"] or 0):
        cand = [i for i in corr if i["_k"] not in used and i["_k"] not in spare and i["_k"] not in later
                and i["loc"] in (e["ref"], "") and i["_t"] is not None
                and e["_t"] is not None and -3600 <= i["_t"] - e["_t"] <= LATE_LIMIT
                and abs(abs(i["total"] or 0) - e["amount"]) <= tol]
        if cand:
            hit = min(cand, key=lambda i: abs(i["_t"] - e["_t"]))
            used.add(hit["_k"])
            e["status"], e["corr"] = "closed_amount", hit
        else:
            e["status"] = "pending" if e["day"] >= (latest.get(e["ref"]) or last_day) else "unfiscalized"
    # correctives no event accounts for (and no check took as its own invoice), of this part's days
    took = {c["inv"] for lst in checks.values() for c in lst if c.get("inv")} | set(took)
    loose = [i for i in corr if i["_k"] not in used and i["_k"] not in took and own(i)]
    out = []
    for e in events:
        o, k = e.pop("orig"), e.pop("corr", None)
        e.pop("self", None)
        e.pop("_t", None)
        e["orig_num"], e["orig_iic"], e["orig_total"] = (o["num"], o["iic"], o["total"]) if o else ("", "", None)
        if k is not None:
            e.update(corr_num=k["num"], corr_iic=k["iic"], corr_fic=k["fic"], corr_issue=k["issue"], corr_total=k["total"],
                     corr_marked=bool(k.get("corr")), corr_ref=k.get("corr_ref") or "")
        out.append(e)
    out.sort(key=lambda e: (e["status"] != "unfiscalized", e["day"], e["ref"], str(e["num"])))
    loose_rows = sorted(({"iic": i["iic"], "fic": i["fic"], "num": i["num"], "issue": i["issue"], "total": i["total"],
                          "ref": i["loc"], "tcr": i["tcr"], "op": i["op"], "marked": bool(i.get("corr")),
                          "corr_ref": i.get("corr_ref") or "",
                          "refers_to": (key_of.get((i.get("corr_ref") or "").upper()) or {}).get("num")
                                       or (number_of(i["corr_ref"]) if number_of and i.get("corr_ref") else "") or ""}
                         for i in loose), key=lambda x: x["issue"] or "")
    # whether Simphony gave the marks at all in these days (they need Reporting and Analytics 20.1.16 or later)
    marks = {"marked_checks": sum(1 for lst in checks.values() for c in lst if c.get("v") == CHECK_VERSION),
             "with_marks": sum(1 for lst in checks.values() for c in lst if any(c.get(k) for k in ("rt", "vd", "ec", "reopen", "to", "from")))}
    R = {"events": out, "loose": loose_rows, "status": RET_STATUS, "kinds": RET_KIND, "marks": marks, "used": used - set(ret_used)}
    R["kpis"] = dict(_ret_kpis(_ret_agg(out, loose_rows)), **marks)
    return R


def _ret_agg(events, loose):
    a = {"st": Counter(e["status"] for e in events), "kind": Counter(e["kind"] for e in events if e["status"] != "no_effect"),
         "unf_value": sum(e["amount"] for e in events if e["status"] == "unfiscalized"),
         "loose": len(loose), "loose_value": sum(x["total"] or 0 for x in loose)}
    a["st"], a["kind"] = dict(a["st"]), dict(a["kind"])
    return a


def _ret_kpis(a):
    st = a.get("st") or {}
    need = sum(v for k, v in st.items() if k != "no_effect")
    return {"events": need, "closed": sum(v for k, v in st.items() if k.startswith("closed")),
            "closed_ref": st.get("closed_ref", 0), "closed_amount": st.get("closed_amount", 0),
            "closed_self": st.get("closed_self", 0), "unfiscalized": st.get("unfiscalized", 0),
            "unfiscalized_value": round(a.get("unf_value") or 0, 2), "pending": st.get("pending", 0),
            "no_effect": st.get("no_effect", 0), "loose": a.get("loose") or 0, "loose_value": round(a.get("loose_value") or 0, 2),
            "by_kind": {k: v for k, v in (a.get("kind") or {}).items() if v}}



def _cell(ref, shift, op):
    return {"ref": ref, "shift": shift, "op": op, "checks": 0, "sim": 0.0, "not_fisc": 0.0, "not_fisc_n": 0,
            "invoices": 0, "fisc": 0.0, "no_check": 0.0, "no_check_n": 0, "emps": set()}


def _int(v):
    try:
        return int(str(v).strip())
    except ValueError:
        return str(v).strip()


DEFINITIONS = {
    "sources": "Simphony: the closed guest checks of each business day (getGuestChecks), total with VAT. Billy: the "
               "invoices of the company's NUIS through the bridge (IIC, and the FIC once fiscalized) and of the "
               "fiscalization service (VAT groups, buyer). Both only read.",
    "placing": "An invoice belongs to the restaurant its till (tcrcode) or business unit (businUnitCode) is mapped to "
               "in Settings; a code not mapped is proposed where its invoices match one restaurant's checks best. A "
               "till whose invoices match no check (a kiosk with its own software) is listed apart. Restaurants not "
               "fiscalized with the company's NUIS (a franchise) are left out in Settings.",
    "match": "First the invoice's IIC, FIC or number written on the check; then the same total within the tolerance "
             "and issued within the window of the check's closing time, the nearest first; then the same total issued "
             "while the check was open (the bill fiscalized when asked for, the check closed when paid). Each invoice "
             "is used once.",
    "status": "Not fiscalized: a check with no invoice, or with an invoice of the bridge that has no FIC. Late: "
              "fiscalized more than the late minutes after the check closed; over 48 hours is beyond the offline limit. "
              "The last business day is Pending.",
    "shifts": "Checks by the employee who closed them, invoices by their operator code, in the shifts of Settings by "
              "the local time; an employee's operator is the one most of their matched checks carry, unless Settings "
              "names it.",
    "book": "Every invoice in the columns of the sales book: exempt supplies, supplies without VAT, zero rate and "
            "exports, and the taxable value and VAT of each rate in the law. Checked: total = value + VAT, the VAT "
            "groups add up, VAT = value × rate, the rate is in the law, an exempt group carries no VAT, a buyer's NUIS "
            "is valid and a business buyer gets an e-invoice, a negative invoice is corrective, every invoice has a FIC.",
    "parts": f"Long periods are matched {CHUNK_DAYS} days at a time, each with the invoices of the {OVERLAP_DAYS} days after "
             f"it, so a check fiscalized up to {LATE_LIMIT // 3600} hours later is still found; an invoice is counted on the day "
             "it was issued. The tables come from the server a page at a time; the Excel has every row.",
    "returns": "Returns tracking: every cancel, return, reopen or change of a Simphony check (its cancel flag, returned "
               "items or negative total, reopened after closing or to a new check, an invoice larger than the check), "
               "and the corrective invoice in Billy that closes it: first one whose reference (correctiveInv) names the "
               "IIC of the check's invoice; then the return check's own corrective invoice; then a corrective of the same "
               "restaurant and amount issued from an hour before to 72 hours after (a lead, not a proof). None: an "
               "unfiscalized return. A cancel or reopen with nothing fiscalized before it, and voids or error corrects "
               "before closing, have no fiscal effect. The marks need Simphony Reporting and Analytics 20.1.16 or later.",
}


# ---------------------------------------------------------------- the rows on disk, the totals in memory
# A read (a "gen") writes every row of its tables to data/billyrec_rows.db: checks, invoices without a check or a
# FIC, the sales book, orders, returns events and correctives without an event. Its result (held in memory and in
# billyrec_<company>.json.gz) keeps only the totals, the restaurants by day, the tills, shifts and operators, the
# summary invoices and the gen; the screen asks for the rows a page at a time, the Excel streams them. A read is
# registered (gens) only when it is complete; then the reads before it are deleted.
COLS = {"checks": ["ref", "day", "num", "id", "clsd", "total", "emp", "rvc", "ws", "shift", "status", "how", "iic", "fic",
                   "inv_num", "issued", "delay_min", "tcr", "op"],
        "invoices": ["iic", "fic", "num", "issue", "total", "tcr", "bu", "op", "ref", "status", "shift"],
        "orders": ["issue", "num", "iic", "fic", "total", "tcr", "bu", "op", "ref", "pays", "closed_by", "invoiced", "after_min"],
        "loose": ["iic", "fic", "num", "issue", "total", "ref", "tcr", "op", "marked", "corr_ref", "refers_to"]}
BOOK_HEAD = ["issue", "ord", "num", "iic", "fic", "buyer", "buyer_id", "type", "total", "bu", "tcr", "op", "ref", "einv", "eic"]
ROW_KINDS = ("checks", "invoices", "book", "orders", "events", "loose")
RET_CHIPS = {"need": "st<>'no_effect'", "unfiscalized": "st='unfiscalized'", "pending": "st='pending'",
             "closed": "st IN ('closed_ref','closed_self')", "amount": "st='closed_amount'", "no_effect": "st='no_effect'", "all": ""}
_rlocal = threading.local()


def book_keys(rates):
    return BOOK_HEAD + ["exempt", "no_vat", "zero"] + [k for r in rates if r > 0 for k in (f"b{r:g}", f"v{r:g}")] + ["errors"]


def _rows_path():
    return store.DATA_DIR / ROWS_FILE


def _rcon():
    """A connection of this thread (the screen's requests read while a read writes: WAL)."""
    con = getattr(_rlocal, "con", None)
    if con is None or getattr(_rlocal, "path", None) != _rows_path():
        _rows_path().parent.mkdir(exist_ok=True)
        con = sqlite3.connect(_rows_path(), timeout=60, check_same_thread=False)
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript("""
            CREATE TABLE IF NOT EXISTS gens (gen TEXT PRIMARY KEY, company TEXT, at REAL);
            CREATE TABLE IF NOT EXISTS rw (gen TEXT, kind TEXT, ref TEXT, st TEXT, k TEXT, e TEXT, s TEXT, q TEXT, j TEXT);
            CREATE INDEX IF NOT EXISTS rw_g ON rw (gen, kind, st, s);
            CREATE TABLE IF NOT EXISTS iics (gen TEXT, iic TEXT);
            CREATE INDEX IF NOT EXISTS iics_g ON iics (gen, iic);
        """)
        _rlocal.con, _rlocal.path = con, _rows_path()
    return con


def _pack(kind, row, keys=None):
    return json.dumps([row.get(k) for k in (keys or COLS[kind])], separators=(",", ":"), default=str)


def _unpack(kind, j, keys=None):
    v = json.loads(j)
    return dict(zip(keys or COLS[kind], v)) if isinstance(v, list) else v


def _up(*xs):
    return " ".join(str(x) for x in xs if x not in (None, "")).upper()


class _Writer:
    """The rows of one read, part by part, in a connection of its own."""

    def __init__(self, gen, keys):
        self.gen, self.keys = gen, keys
        _rcon()                                         # the tables
        self.con = sqlite3.connect(_rows_path(), timeout=60)
        self.con.execute("PRAGMA journal_mode=WAL")

    def put(self, res):
        g, B = self.gen, self.keys
        out = []
        for c in res["checks"]:
            out.append((g, "checks", c["ref"], c["status"], "", "", (c["day"] or "") + (c["clsd"] or ""),
                        _up(c["num"], c["iic"], c["fic"], c["inv_num"]), _pack("checks", c)))
        for i in res["invoices"]:
            out.append((g, "invoices", i["ref"], i["status"], "", "", i["issue"] or "", None, _pack("invoices", i)))
        for b in res["book"]:
            out.append((g, "book", b["ref"], "case" if any(e not in INFO for e in b["errors"]) else "", "",
                        "," + ",".join(b["errors"]) + ",", b["issue"] or "", None, _pack("book", b, B)))
        for o in res["orders"]:
            out.append((g, "orders", o["ref"], "open" if not o["invoiced"] else "done", "", "", o["issue"] or "", None,
                        _pack("orders", o)))
        for e in res["events"]:
            out.append((g, "events", e["ref"], e["status"], e["kind"], "",
                        f"{0 if e['status'] == 'unfiscalized' else 1}|{e['day']}|{e['ref']}|{str(e['num']).rjust(12)}",
                        _up(e["num"], e["orig_num"], e["orig_iic"], e.get("corr_num"), e.get("corr_iic"), e["name"]),
                        json.dumps(e, separators=(",", ":"), default=str)))
        for x in res["loose"]:
            out.append((g, "loose", x["ref"], "", "", "", x["issue"] or "", None, _pack("loose", x)))
        with self.con:
            self.con.executemany("INSERT INTO rw VALUES (?,?,?,?,?,?,?,?,?)", out)
            self.con.executemany("INSERT INTO iics VALUES (?,?)", [(g, k) for k in res["iics"]])

    def found(self, refs):
        """Of the IICs given, those among the invoices of the period."""
        refs, out = sorted({x.upper() for x in refs}), set()
        for k in range(0, len(refs), 500):
            part = refs[k:k + 500]
            out |= {r for (r,) in self.con.execute(f"SELECT iic FROM iics WHERE gen=? AND iic IN ({','.join('?' * len(part))})",
                                                     (self.gen, *part))}
        return out

    def done(self):
        with self.con:
            self.con.execute("DELETE FROM iics WHERE gen=?", (self.gen,))
        self.con.close()

    def drop(self):
        try:
            with self.con:
                self.con.execute("DELETE FROM rw WHERE gen=?", (self.gen,))
                self.con.execute("DELETE FROM iics WHERE gen=?", (self.gen,))
            self.con.close()
        except sqlite3.Error:
            traceback.print_exc()


def _zero_cell():
    return {"checks": 0, "sim": 0, "fisc": 0, "missing": 0, "missing_value": 0, "late": 0, "no_check": 0, "pending": 0}


def _add(dst, src):
    for k, v in src.items():
        if isinstance(v, dict):
            _add(dst.setdefault(k, {}), v)
        elif v:
            dst[k] = dst.get(k, 0) + v
        else:
            dst.setdefault(k, 0)


class _Acc:
    """What stays in memory across the parts: totals by restaurant, the restaurants by day, tills, shifts and
    operators, summary invoices, and the cases the register needs."""

    def __init__(self, company, nuis, days, S, regs):
        self.company, self.nuis, self.days, self.S = company, nuis, days, S
        self.agg, self.locs, self.tills, self.grid, self.props = {}, {}, {}, {}, {}
        self.summary, self.err, self.refs, self.ret_refs, self.rwc = [], Counter(), set(), set(), set()
        self.marks = Counter()
        self.nofic, self.unf, self.berr = {}, {}, {}
        self.by_emp, self.emp_op, self.names = {}, {}, set()
        self.keys = book_keys(S["rates"])

    def _a(self, ref):
        return self.agg.setdefault(ref or "", {})

    def add(self, res):
        part = {}

        def a(ref):
            return part.setdefault(ref or "", {"checks": 0, "fiscalized": 0, "missing": 0, "missing_value": 0.0, "late": 0,
                                               "late48": 0, "invoices": 0, "no_check": 0, "no_check_value": 0.0, "no_fic": 0,
                                               "no_fic_value": 0.0, "other_till": 0, "summary": 0, "book_errors": 0,
                                               "book_nodetail": 0, "orders": 0, "orders_value": 0.0, "orders_open": 0,
                                               "be": {}, "bt": {}, "ret": {"st": {}, "kind": {}, "unf_value": 0.0, "loose": 0, "loose_value": 0.0}})
        for c in res["checks"]:
            self.refs.add(c["ref"])
            if c["status"] in ("zero", "pending"):
                continue
            x = a(c["ref"])
            x["checks"] += 1
            x["fiscalized"] += c["status"] in ("ok", "late", "late48")
            if c["status"] == "missing":
                x["missing"] += 1
                x["missing_value"] += c["total"]
            x["late"] += c["status"] == "late"
            x["late48"] += c["status"] == "late48"
        for i in res["invoices"]:
            x = a(i["ref"])
            x[i["status"]] += 1
            x[i["status"] + "_value"] += i["total"] or 0
            if i["status"] == "no_fic":
                d = self.nofic.setdefault((i["issue"] or "")[:10], {"n": 0, "value": 0.0, "ex": []})
                d["n"] += 1
                d["value"] += i["total"] or 0
                if len(d["ex"]) < 8:
                    d["ex"].append(f"{i['num'] or i['iic'][:10]} {i['total'] or 0:,.0f}")
        for ref, n in res["other_till"].items():
            a(ref)["other_till"] += n
        for b in res["book"]:
            x = a(b["ref"])
            x["invoices"] += 1
            case = [e for e in b["errors"] if e not in INFO]
            x["book_errors"] += bool(case)
            x["book_nodetail"] += "no_detail" in b["errors"]
            for e in b["errors"]:
                x["be"][e] = x["be"].get(e, 0) + 1
            for k in self.keys:
                if k == "total" or k in ("exempt", "no_vat", "zero") or re.fullmatch(r"[bv]\d+(\.\d+)?", k):
                    x["bt"][k] = x["bt"].get(k, 0) + (b.get(k) or 0)
            for e in case:
                d = self.berr.setdefault(e, {"n": 0, "value": 0.0, "ex": []})
                d["n"] += 1
                d["value"] += b["total"] or 0
                if len(d["ex"]) < 8:
                    d["ex"].append(f"{b['num'] or b['iic'][:10]} {b['total'] or 0:,.0f}")
        for s in res["summary"]:
            a(s["ref"])["summary"] += 1
            self.summary.append(s)
        for o in res["orders"]:
            x = a(o["ref"])
            x["orders"] += 1
            x["orders_value"] += o["total"] or 0
            x["orders_open"] += not o["invoiced"]
        by_ref = defaultdict(lambda: ([], []))
        for e in res["events"]:
            by_ref[e["ref"]][0].append(e)
            self.ret_refs.add(e["ref"])
            if e["status"] == "unfiscalized":
                d = self.unf.setdefault((e["ref"], e["day"]), {"n": 0, "value": 0.0, "ex": []})
                d["n"] += 1
                d["value"] += e["amount"]
                if len(d["ex"]) < 8:
                    d["ex"].append([e["num"], e["kind"], e["amount"]])
        for x in res["loose"]:
            by_ref[x["ref"]][1].append(x)
        for ref, (ev, lo) in by_ref.items():
            _add(a(ref)["ret"], _ret_agg(ev, lo))
        for ref, x in part.items():
            _add(self._a(ref), x)
        self.err.update(res["err_count"])
        self.marks.update(res["marks"])
        self.rwc |= res["refs_with_checks"]
        for L in res["locations"]:
            m = self.locs.setdefault(L["ref"], {"ref": L["ref"], "name": L["name"], "cells": {}, "bc": L["bc"], "dept": L["dept"],
                                                "checks": 0, "missing": 0, "missing_value": 0.0})
            m["cells"].update(L["cells"])
            for k in ("checks", "missing", "missing_value"):
                m[k] += L[k]
        for t in res["tills"].values():
            m = self.tills.get(t["tcr"])
            if m is None:
                self.tills[t["tcr"]] = t
                continue
            for k in ("invoices", "value", "matched", "no_fic"):
                m[k] += t[k]
            m["first"], m["last"] = min(m["first"], t["first"]), max(m["last"], t["last"])
            m["ops"] |= t["ops"]
        for key, g in res["by_emp"].items():
            m = self.by_emp.setdefault(key, {"checks": 0, "sim": 0.0, "not_fisc": 0.0, "not_fisc_n": 0})
            for k in m:
                m[k] += g[k]
        for key, c in res["emp_op"].items():
            self.emp_op.setdefault(key, Counter()).update(c)
        self.names |= res["names"]
        for key, g in res["grid"].items():
            m = self.grid.get(key)
            if m is None:
                self.grid[key] = g
                continue
            for k in ("checks", "sim", "not_fisc", "not_fisc_n", "invoices", "fisc", "no_check", "no_check_n"):
                m[k] += g[k]
            m["emps"] |= g["emps"]
        for code, (ref, matched, n) in res["proposals"].items():
            p = self.props.setdefault(code, {"by": Counter(), "invoices": 0})
            p["by"][ref] += matched
            p["invoices"] += n

    def finish(self, W):
        refs = [x for s in self.summary for x in s["_refs"]]
        have = W.found(refs) if refs else set()
        for s in self.summary:
            R = s.pop("_refs")
            s["found"] = sum(1 for x in R if x.upper() in have)
            s["missing"] = [x for x in R if x.upper() not in have][:20]
        self.summary.sort(key=lambda s: s["issue"] or "")
        for L in self.locs.values():
            L["cells"] = {d.isoformat(): L["cells"].get(d.isoformat()) or _zero_cell() for d in self.days}
            L["missing_value"] = round(L["missing_value"], 2)
        for t in self.tills.values():
            t["value"] = round(t["value"], 2)
            t["ops"] = len(t["ops"])
            t["kind"] = "kiosk / other till" if t["ref"] and t["matched"] == 0 and t["ref"] in self.rwc else (
                "till of Simphony" if t["matched"] else "not mapped" if not t["ref"] else "no checks to compare")
        emp_op = self.emp_op
        for line in self.S["operators"].splitlines():
            line = line.split("#", 1)[0]
            if "=" in line:
                op, _, emp = line.partition("=")
                for ref in self.names:
                    emp_op[(ref, _int(emp))] = Counter({op.strip().lower(): 10 ** 9})
        op_of = {k: v.most_common(1)[0][0] for k, v in emp_op.items() if v}
        cells = {}
        for (ref, shift, emp), x in self.by_emp.items():         # the checks' side, as a single read would add it
            op = op_of.get((ref, emp)) or f"employee {emp}"
            g = cells.setdefault((ref, shift, op), _cell(ref, shift, op))
            for k in x:
                g[k] += x[k]
            g["emps"].add(emp)
        for key, g in self.grid.items():                         # the invoices' side
            m = cells.setdefault(key, g)
            if m is not g:
                for k in ("invoices", "fisc", "no_check", "no_check_n"):
                    m[k] += g[k]
        ops = []
        for g in cells.values():
            g["emps"] = sorted(str(e) for e in g["emps"] if e is not None)
            g["diff"] = round(g["fisc"] - g["sim"], 2)
            for k in ("sim", "fisc", "not_fisc", "no_check"):
                g[k] = round(g[k], 2)
            ops.append(g)
        ops.sort(key=lambda g: (g["ref"], g["shift"], -abs(g["diff"])))
        self.ops = ops

    def result(self, gen):
        S = self.S
        props = {}
        for code, p in self.props.items():
            ref, matched = p["by"].most_common(1)[0]
            props[code] = {"ref": ref, "matched": matched, "invoices": p["invoices"]}
        R = {"status": RET_STATUS, "kinds": RET_KIND, "marks": dict(self.marks), "refs": sorted(self.ret_refs)}
        r = {"gen": gen, "company": self.company, "nuis": self.nuis, "days": [d.isoformat() for d in self.days],
             "settings": {k: v for k, v in S.items() if k != "shifts"} | {"shift_names": [s[2] for s in S["shifts"]]},
             "agg": self.agg, "locations": sorted(self.locs.values(), key=lambda L: L["ref"]),
             "tills": sorted(self.tills.values(), key=lambda t: (t["ref"], t["tcr"])), "proposals": props,
             "operators": self.ops, "summary": self.summary, "book_errors": dict(self.err), "book_keys": self.keys,
             "returns": R, "refs": sorted(self.refs), "parts": len(parts(self.days)),
             "find": {"nofic": [[d, v["n"], round(v["value"], 2), v["ex"]] for d, v in sorted(self.nofic.items())],
                      "unf": [[ref, d, v["n"], round(v["value"], 2), v["ex"]] for (ref, d), v in sorted(self.unf.items())],
                      "book": {e: [v["n"], round(v["value"], 2), v["ex"]] for e, v in self.berr.items()}},
             "info_errors": list(INFO), "status": STATUS, "inv_status": INV_STATUS, "error_labels": ERRORS,
             "rates": S["rates"], "definitions": DEFINITIONS}
        r["kpis"] = _kpis(r, None)
        R["kpis"] = _ret_view(r, None)[0]
        return r


def _sum_agg(r, keep):
    t = {}
    for ref, a in (r.get("agg") or {}).items():
        if keep is None or ref in keep:
            _add(t, a)
    return t


def _kpis(r, keep):
    t = _sum_agg(r, keep)
    g = lambda k: t.get(k) or 0
    tills = r.get("tills") or []
    return {"checks": g("checks"), "fiscalized": g("fiscalized"), "missing": g("missing"),
            "missing_value": round(g("missing_value"), 2), "late": g("late"), "late48": g("late48"),
            "invoices": g("invoices"), "no_check": g("no_check"), "no_check_value": round(g("no_check_value"), 2),
            "no_fic": g("no_fic"), "no_fic_value": round(g("no_fic_value"), 2), "other_till": g("other_till"),
            "summary": g("summary"), "book_errors": g("book_errors"), "book_nodetail": g("book_nodetail"),
            "orders": g("orders"), "orders_value": round(g("orders_value"), 2), "orders_open": g("orders_open"),
            "open_checks": sum(v[0] for ref, by in (r.get("open") or {}).items() if keep is None or ref in keep
                               for v in by.values()),
            "open_value": round(sum(v[1] for ref, by in (r.get("open") or {}).items() if keep is None or ref in keep
                                    for v in by.values()), 2),
            "unmapped": sum(1 for x in tills if not x["ref"]) if keep is None else 0,
            "proposed": len(r.get("proposals") or {}) if keep is None else 0}


def _ret_view(r, keep):
    """(the returns kpis, the counts of the screen's chips) of the scope."""
    a = _sum_agg(r, keep).get("ret") or {}
    k = dict(_ret_kpis(a), **((r.get("returns") or {}).get("marks") or {}))
    st = a.get("st") or {}
    chips = {"need": k["events"], "unfiscalized": k["unfiscalized"], "pending": k["pending"],
             "closed": st.get("closed_ref", 0) + st.get("closed_self", 0), "amount": st.get("closed_amount", 0),
             "no_effect": k["no_effect"], "all": sum(st.values())}
    return k, chips


# ---------------------------------------------------------------- the scope of a person
def _keep_of(r, allowed):
    if allowed is None or not isinstance(r, dict) or not isinstance(r.get("locations"), list):
        return None
    A = {str(x).strip().upper() for x in allowed}
    return {L["ref"] for L in r["locations"] if {str(L.get(k) or "").upper() for k in ("ref", "bc", "dept")} & A}


def cut(r, allowed):
    keep = _keep_of(r, allowed)
    if keep is None or "gen" not in r:
        return r
    ok = lambda x: x.get("ref") in keep
    return dict(r, locations=[L for L in r["locations"] if L["ref"] in keep], tills=[t for t in r["tills"] if ok(t)],
                operators=[o for o in r["operators"] if ok(o)], summary=[s for s in r["summary"] if ok(s)], proposals={},
                refs=[x for x in r["refs"] if x in keep], cut=True, keep=sorted(keep),
                returns=dict(r["returns"], refs=[x for x in r["returns"]["refs"] if x in keep]))


def public(r):
    """What the screen needs at once, of the scope (cut first): the totals, the chips' counts, the sales book's
    totals. The rows come a page at a time (rows); the register's cases stay on the server."""
    if not isinstance(r, dict) or "gen" not in r:
        return r
    keep = set(r["keep"]) if r.get("keep") is not None else None
    t = _sum_agg(r, keep)
    k, chips = _ret_view(r, keep)
    out = {x: v for x, v in r.items() if x not in ("agg", "find")}
    out["open"] = {ref: v for ref, v in (r.get("open") or {}).items() if keep is None or ref in keep}
    out.update(kpis=_kpis(r, keep), book_count=t.get("invoices") or 0, book_cases=t.get("book_errors") or 0,
               book_errors={e: n for e, n in (t.get("be") or {}).items() if n} if keep is not None else r["book_errors"],
               book_totals={x: round(v, 2) for x, v in (t.get("bt") or {}).items()},
               orders_n={"open": t.get("orders_open") or 0, "done": (t.get("orders") or 0) - (t.get("orders_open") or 0),
                         "all": t.get("orders") or 0},
               returns=dict(r["returns"], kpis=k, chips=chips))
    return out


def _where(r, kind, keep, f):
    w, a = ["gen=?", "kind=?"], [r["gen"], kind]
    if keep is not None:
        w.append(f"ref IN ({','.join('?' * len(keep))})" if keep else "0")
        a += sorted(keep)
    if f.get("ref"):
        w.append("ref=?")
        a.append(f["ref"])
    st = f.get("st") or ""
    if kind == "checks" and st != "all":
        w.append("st=?")
        a.append(st or "missing")
    elif kind == "book" and st and st != "all":
        if st == "*":
            w.append("st='case'")
        else:
            w.append("instr(e, ?) > 0")
            a.append("," + st + ",")
    elif kind == "orders" and st in ("open", "done"):
        w.append("st=?")
        a.append(st)
    elif kind == "events":
        if RET_CHIPS.get(st or "need"):
            w.append(RET_CHIPS[st or "need"])
        if f.get("k"):
            w.append("k=?")
            a.append(f["k"])
    q = (f.get("q") or "").strip().upper()
    if q and kind in ("checks", "events"):
        w.append("instr(q, ?) > 0")
        a.append(q)
    return " AND ".join(w), a


def rows(r, kind, keep=None, offset=0, limit=400, **f):
    """A page of one table of the read r, of the scope, with the screen's filters: {rows, total}."""
    if kind not in ROW_KINDS:
        raise ValueError("Unknown table.")
    where, a = _where(r, kind, keep, f)
    con = _rcon()
    total = con.execute(f"SELECT COUNT(*) FROM rw WHERE {where}", a).fetchone()[0]
    order = "s DESC, rowid DESC" if kind == "checks" else "s, rowid"
    lim, off = max(1, min(int(limit or 400), 5000)), max(0, int(offset or 0))
    keys = r.get("book_keys") if kind == "book" else None
    out = [_unpack(kind, j, keys) for (j,) in con.execute(f"SELECT j FROM rw WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
                                                          (*a, lim, off))]
    return {"rows": out, "total": total, "offset": off, "gen": r["gen"]}


def iter_rows(r, kind, keep=None, **f):
    """Every row of one table, one at a time (the Excel)."""
    where, a = _where(r, kind, keep, dict(f, st=f.get("st", "all" if kind == "checks" else "")))
    keys = r.get("book_keys") if kind == "book" else None
    cur = _rcon().execute(f"SELECT j FROM rw WHERE {where} ORDER BY {'s DESC, rowid DESC' if kind == 'checks' else 's, rowid'}", a)
    while True:
        part = cur.fetchmany(1000)
        if not part:
            break
        for (j,) in part:
            yield _unpack(kind, j, keys)


def expand(r):
    """The read as one dict with every row (tests, and the comparison with a single pass)."""
    out = public(r)
    out.update({k: list(iter_rows(r, k, st="all")) for k in ("checks", "invoices", "book", "orders")})
    out["returns"] = dict(out["returns"], events=list(iter_rows(r, "events", st="all")), loose=list(iter_rows(r, "loose")))
    return out


# ---------------------------------------------------------------- held results and background reads
def _file(company):
    return store.DATA_DIR / f"billyrec_{re.sub(r'[^A-Za-z0-9_-]+', '_', company)}.json.gz"


def _registered(gen):
    try:
        return bool(_rcon().execute("SELECT 1 FROM gens WHERE gen=?", (gen,)).fetchone())
    except sqlite3.Error:
        return False


def held(company, key=None):
    key = key or company
    with _lock:
        hit = _results.get(key)
    if hit:
        return hit[1]
    try:                                                     # the last read, kept on disk across restarts
        with gzip.open(_file(key), "rt", encoding="utf-8") as f:
            r = json.load(f)
    except (OSError, ValueError):
        return None
    if not r.get("gen") or not _registered(r["gen"]):       # a read of before the rows were kept on disk: read again
        return None
    with _lock:
        _results.setdefault(key, (0.0, r))                  # age 0: shown at once, read again in the background
    return r


KEEP_RANGES = 3                 # ranges asked on the screen kept per company (the oldest dropped)


def _forget(con, key):
    for (g,) in con.execute("SELECT gen FROM gens WHERE company=?", (key,)).fetchall():
        with con:
            con.execute("DELETE FROM rw WHERE gen=?", (g,))
            con.execute("DELETE FROM gens WHERE gen=?", (g,))
    with _lock:
        _results.pop(key, None)
    try:
        _file(key).unlink()
    except OSError:
        pass


def remember(company, r, key=None):
    """Holds the read (in memory, on disk) under its key (key_of), and deletes the reads before it of the same key;
    of the ranges asked on the screen, the KEEP_RANGES most recent stay."""
    key = key or company
    con = _rcon()
    with con:
        con.execute("INSERT OR REPLACE INTO gens VALUES (?,?,?)", (r["gen"], key, time.time()))
    with _lock:
        _results[key] = (time.time(), r)
    try:
        tmp = _file(key).with_suffix(".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=5) as f:
            json.dump(r, f, default=str)
        tmp.replace(_file(key))
    except OSError:
        traceback.print_exc()
    try:                                                     # the reads before it, of the same key
        old = [g for (g,) in con.execute("SELECT gen FROM gens WHERE company=? AND gen<>?", (key, r["gen"]))]
        for g in old:
            with con:
                con.execute("DELETE FROM rw WHERE gen=?", (g,))
                con.execute("DELETE FROM gens WHERE gen=?", (g,))
        if key != company:
            ranges = [k for (k,) in con.execute("SELECT company FROM gens WHERE substr(company, 1, ?)=? "
                                                "GROUP BY company ORDER BY MAX(at) DESC", (len(company) + 1, company + "|"))]
            for k in ranges[KEEP_RANGES:]:
                _forget(con, k)
    except sqlite3.Error:
        traceback.print_exc()


def _sweep():
    """At start: the rows of reads that never finished (the tower stopped during a read)."""
    try:
        con = _rcon()
        with con:
            con.execute("DELETE FROM rw WHERE gen NOT IN (SELECT gen FROM gens)")
            con.execute("DELETE FROM iics")
    except sqlite3.Error:
        traceback.print_exc()


def request(company, latest_fn, refresh=False, frm=None, to=None):
    """The last read at once; a new read in the background when it is older than KEEP or asked for. While it runs
    the last read comes back with reading: True (the screen keeps showing it and asks again). frm–to: a range of its
    own (check_range), held apart from the company's read; it does not touch the exceptions register, and once every
    day of it and the three after are closed it is not read again unless asked."""
    custom = bool(frm or to)
    if custom:
        a, b = check_range(frm, to)
        frm, to = a.isoformat(), b.isoformat()
    key = key_of(company, frm, to)
    closed = custom and b + timedelta(days=OVERLAP_DAYS) <= date.today() - timedelta(days=2)
    last = held(company, key)
    with _lock:
        hit = _results.get(key)
        job = _jobs.get(key)
        running = bool(job and job["state"] == "running")
        fresh = hit and (time.time() - hit[0] < KEEP or closed)
        if job and job["state"] == "failed" and not refresh and time.time() - job["at"] < 60:
            _jobs.pop(key, None)
            if not last:
                return {"available": False, "error": job["error"]}
            return dict(last, reading=False, read_error=job["error"])
        if not running and (refresh or not fresh):
            job = _jobs[key] = {"state": "running", "stage": "starting", "at": time.time(), "error": ""}
            running = True

            def go():
                try:
                    r = build(company, latest_fn, progress=lambda st: job.update(stage=st), frm=frm, to=to)
                    if on_built and not custom:              # the register follows the company's read only
                        job.update(stage="updating the exceptions register")
                        try:
                            r["register"] = on_built(company, r)
                        except Exception:                          # noqa: BLE001
                            traceback.print_exc()
                    remember(company, r, key)
                    job["state"] = "done"
                except Exception as exc:                           # noqa: BLE001
                    traceback.print_exc()
                    job.update(state="failed", error=str(exc)[:300], at=time.time())
            threading.Thread(target=go, daemon=True, name=f"billyrec-{company}").start()
        stage = job["stage"] if running and job else ""
    if last:
        return dict(last, reading=running, stage=stage)
    return {"pending": True, "stage": stage or "starting"} if running else {"available": False, "error": "Not read yet."}


# ---------------------------------------------------------------- the register
def findings(r):
    """Exceptions for exreg.sync (source "billy_rec"): not fiscalized checks by restaurant and day, invoices without
    a FIC, unfiscalized returns, and the sales book errors by kind."""
    out = []
    for L in r["locations"]:
        for d, c in L["cells"].items():
            if c["missing"]:
                out.append({"subject": f"{d} missing {L['ref']}", "kind": "missing", "location": L["bc"] or L["ref"],
                            "title": f"{L['name']} {d}: {c['missing']} checks not fiscalized",
                            "detail": f"{c['missing']} Simphony checks for {c['missing_value']:,.0f} have no fiscalized invoice "
                                      f"in Billy (window {r['settings']['window']:g} min, tolerance {r['settings']['tol']:g}).",
                            "severity": "critical", "value": abs(c["missing_value"])})
    F = r.get("find") or {}
    for d, n, value, ex in F.get("nofic") or []:
        out.append({"subject": f"{d} no_fic", "kind": "no_fic", "location": "",
                    "title": f"{d}: {n} invoices of the bridge without a FIC",
                    "detail": "Issued at the till, not fiscalized with the Tax Administration: " + ", ".join(ex),
                    "severity": "critical", "value": abs(value)})
    names = {L["ref"]: (L["name"], L["bc"] or L["ref"]) for L in r["locations"]}
    kinds = (r.get("returns") or {}).get("kinds") or RET_KIND
    for ref, d, n, value, ex in F.get("unf") or []:              # returns tracking: by restaurant and day
        nm, loc = names.get(ref, (ref, ref))
        out.append({"subject": f"{d} unfisc_return {ref}", "kind": "unfisc_return", "location": loc,
                    "title": f"{nm} {d}: {n} unfiscalized returns",
                    "detail": "Cancelled, returned, reopened or changed in Simphony with no corrective invoice in Billy: " +
                              ", ".join(f"check {num} {kinds.get(kind, kind).lower()} {amount:,.0f}" for num, kind, amount in ex),
                    "severity": "critical", "value": round(value, 2)})
    for e, (n, value, ex) in (F.get("book") or {}).items():
        out.append({"subject": f"book {e} {r['days'][0]}", "kind": "book_" + e, "location": "",
                    "title": f"Sales book: {ERRORS[e]} ({n} invoices)", "detail": ", ".join(ex),
                    "severity": "warning", "value": abs(value)})
    return out


def in_scope(r):
    days = set(r["days"])
    return lambda x: (x["subject"] or "")[:10] in days or (x["subject"] or "").startswith("book ")


# ---------------------------------------------------------------- the Excel: the sales book and the cases
BOOK_COLS = [("issue", "Data dhe ora"), ("ord", "Nr. rendor"), ("num", "Nr. i faturës"), ("iic", "NSLF (IIC)"),
             ("fic", "NIVF (FIC)"), ("buyer", "Blerësi"), ("buyer_id", "NIPT i blerësit"), ("type", "Pagesa"),
             ("total", "Vlera totale me TVSH"), ("exempt", "Shitje të përjashtuara"), ("no_vat", "Shitje pa TVSH"),
             ("zero", "Shitje me 0% / eksporte")]


def render_xlsx(r, keep=None):
    """The workbook of the read, of the scope: written row by row (write-only), so the sales book of 50 days does
    not sit in memory."""
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter
    wb = Workbook(write_only=True)
    bold = Font(bold=True)

    def head(ws, header):
        row = []
        for h in header:
            c = WriteOnlyCell(ws, value=h)
            c.font = bold
            row.append(c)
        ws.append(row)

    rates = sorted((x for x in r["rates"] if x > 0), reverse=True)
    cols = BOOK_COLS + [c for x in rates for c in ((f"b{x:g}", f"Vlera e tatueshme {x:g}%"), (f"v{x:g}", f"TVSH {x:g}%"))] + \
        [("bu", "Njësia (BU)"), ("tcr", "Arka (TCR)"), ("op", "Operatori"), ("ref", "Restoranti"), ("eic", "EIC"), ("errors", "Gabime")]

    def new(title, header, pre=()):
        ws = wb.create_sheet(title)
        for i in range(len(header)):
            ws.column_dimensions[get_column_letter(i + 1)].width = 16
        for line in pre:
            ws.append([line])
        head(ws, header)
        return ws
    ws = new("Libri i Shitjes", [c[1] for c in cols], [f"Libri i Shitjes · {r['company']} · NIPT {r['nuis']} · {r['days'][0]} – {r['days'][-1]}"])
    labels = r["error_labels"]
    for b in iter_rows(r, "book", keep):
        ws.append([", ".join(labels.get(e, e) for e in b["errors"]) if k == "errors" else
                   (b.get(k) or "").replace("T", " ") if k == "issue" else b.get(k) for k, _ in cols])
    bt = _sum_agg(r, keep).get("bt") or {}
    ws.append(["Totali"] + [""] * 7 + [round(bt.get(k) or 0, 2) for k, _ in cols[8:-6]])
    w2 = new("Checks not fiscalized", ["Restaurant", "Business day", "Check", "Closed", "Total", "Employee", "Shift", "Status"])
    for st in ("missing", "late48"):
        for c in iter_rows(r, "checks", keep, st=st):
            w2.append([c["ref"], c["day"], c["num"], c["clsd"], c["total"], c["emp"], c["shift"], r["status"][c["status"]]])
    w3 = new("Invoices without check", ["Issued", "Number", "IIC", "FIC", "Total", "Business unit", "Till", "Operator", "Restaurant", "Status"])
    for i in iter_rows(r, "invoices", keep):
        w3.append([i["issue"], i["num"], i["iic"], i["fic"], i["total"], i["bu"], i["tcr"], i["op"], i["ref"], r["inv_status"][i["status"]]])
    w4 = new("Shift & operator", ["Restaurant", "Shift", "Operator", "Employees", "Checks", "Simphony", "Fiscalized", "Difference",
                                  "Not fiscalized", "Invoices without check"])
    for o in r["operators"]:
        if keep is None or o["ref"] in keep:
            w4.append([o["ref"], o["shift"], o["op"], ", ".join(o["emps"]), o["checks"], o["sim"], o["fisc"], o["diff"],
                       o["not_fisc"], o["no_check"]])
    w6 = new("Orders", ["Issued", "Number", "IIC", "FIC", "Total", "Till", "Operator", "Restaurant", "Payment", "Invoice after (min)"])
    for x in iter_rows(r, "orders", keep):
        w6.append([x["issue"], x["num"], x["iic"], x["fic"], x["total"], x["tcr"], x["op"], x["ref"], x["pays"],
                   x["after_min"] if x["invoiced"] else "no invoice of the same total"])
    R = r.get("returns") or {}
    w7 = new("Returns tracking", ["Restaurant", "Business day", "Check", "Name", "Closed", "Event", "Note", "Amount", "Check total", "Invoice",
                                  "Invoice IIC", "Corrective invoice", "Corrective IIC", "Corrective issued", "Corrective total",
                                  "Marked corrective", "Status", "Reason codes", "Manager"])
    for e in iter_rows(r, "events", keep, st="all"):
        w7.append([e["ref"], e["day"], e["num"], e["name"], e["clsd"], (R.get("kinds") or {}).get(e["kind"], e["kind"]), e["note"],
                   e["amount"], e["check_total"], e["orig_num"], e["orig_iic"], e.get("corr_num", ""), e.get("corr_iic", ""),
                   e.get("corr_issue", ""), e.get("corr_total"), ("yes" if e.get("corr_marked") else "no") if e.get("corr_iic") else "",
                   (R.get("status") or {}).get(e["status"], e["status"]), ", ".join(e["reasons"]), ", ".join(e["mgr"])])
    w8 = new("Correctives without event", ["Issued", "Number", "IIC", "FIC", "Total", "Restaurant", "Till", "Operator", "Marked corrective",
                                           "Refers to IIC", "Refers to invoice"])
    for x in iter_rows(r, "loose", keep):
        w8.append([x["issue"], x["num"], x["iic"], x["fic"], x["total"], x["ref"], x["tcr"], x["op"], "yes" if x["marked"] else "no",
                   x["corr_ref"], x["refers_to"]])
    w5 = new("Tills", ["Till (TCR)", "Business unit", "Restaurant", "Placed", "Label", "Kind", "Invoices", "Value", "With a check", "No FIC"])
    for t in r["tills"]:
        if keep is None or t["ref"] in keep:
            w5.append([t["tcr"], t["bu"], t["ref"], t["placed"], t["label"], t["kind"], t["invoices"], t["value"], t["matched"], t["no_fic"]])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


RET_HEAD = ["Restaurant", "Business day", "Check", "Name", "Closed", "Event", "Note", "Amount", "Check total", "Invoice", "Invoice IIC",
            "Invoice total", "Corrective invoice", "Corrective IIC", "Corrective issued", "Corrective total", "Marked corrective",
            "Status", "Reason · manager"]
RET_FORMATS = ["text", "date", "text", "text", "text", "text", "text", "num", "num", "text", "text", "num", "text", "text", "text",
               "num", "text", "text", "text"]


def render_returns_xlsx(r, keep=None, **f):
    """Returns tracking with the screen's filters (its Export): the events, as the table shows them."""
    import xlsxtable
    R = r.get("returns") or {}
    kinds, status = R.get("kinds") or RET_KIND, R.get("status") or RET_STATUS
    rows_ = [[e["ref"], e["day"], str(e["num"]), e["name"], e["clsd"], kinds.get(e["kind"], e["kind"]), e["note"], e["amount"],
              e["check_total"], e["orig_num"], e["orig_iic"], e["orig_total"], e.get("corr_num") or "", e.get("corr_iic") or "",
              e.get("corr_issue") or "", e.get("corr_total"), ("yes" if e.get("corr_marked") else "no") if e.get("corr_iic") else "",
              status.get(e["status"], e["status"]), " · ".join(x for x in (", ".join(e["reasons"]), ", ".join(e["mgr"])) if x)]
             for e in iter_rows(r, "events", keep, **f)]
    period = f"{r['days'][0]} to {r['days'][-1]}"
    return xlsxtable.render([{"title": "Returns tracking", "header": RET_HEAD, "formats": RET_FORMATS, "rows": rows_}],
                            [f"Returns tracking · {r['company']} · NUIS {r['nuis']} · {period}; Simphony and Billy, read only.",
                             DEFINITIONS["returns"]])


# ---------------------------------------------------------------- the daily read
_daily = {"done": None}


def start(latest_fn):
    """Every day at the read time of Settings (default 07:30): every company with a Billy account is read, its
    result held and its cases put in the register, so the 08:00 reports and the first look of the day are fresh."""
    _sweep()

    def loop():
        while True:
            try:
                S = settings_of(store.get_settings())
                now = datetime.now()
                if S["read_time"] and _daily["done"] != now.date():
                    hh, mm = (int(x) for x in S["read_time"].split(":"))
                    at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                    if at <= now <= at + timedelta(hours=6):
                        _daily["done"] = now.date()
                        for c in companies():
                            try:
                                r = build(c, latest_fn)
                                if on_built:
                                    try:
                                        r["register"] = on_built(c, r)
                                    except Exception:                  # noqa: BLE001
                                        traceback.print_exc()
                                remember(c, r)
                            except Exception:                          # noqa: BLE001
                                traceback.print_exc()
            except Exception:                                          # noqa: BLE001
                traceback.print_exc()
            time.sleep(60)
    threading.Thread(target=loop, daemon=True, name="billyrec-daily").start()

