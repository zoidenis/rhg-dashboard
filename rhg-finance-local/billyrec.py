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
     the nearest first.
A check left is "Not fiscalized" (or still "Pending" on the last business day); an invoice left is "No Simphony check";
an invoice of the bridge with no FIC did not reach the Tax Administration. Fiscalized more than the late minutes after
the check closed is "Late", more than 48 hours (the offline limit) "Over 48 h".

Shifts and operators: the checks by their employee and the invoices by their operator code, in the shifts of Settings
(by the local time of closing or issue); an employee's operator is the one most of their matched checks carry.
Summary invoices: the invoices that carry a list of IIC references (sumInvIICRefs), with which of those are found.
The sales book: every invoice of the fiscalization service in the columns of the law (exempt, without VAT, zero rate
and exports, taxable and VAT by rate), with the checks of its mapping: the totals, the VAT groups, the rates and the
buyer. Nothing is written to Simphony, Billy or the Tax Administration.
"""
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
DEFAULTS = {"days": "7", "window": "20", "tol": "1", "late": "60", "rates": "20, 10, 6, 0",
            "shifts": "06:00-16:00 Morning; 16:00-06:00 Evening", "map": "", "operators": "", "exclude": ""}
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
KEEP = 900
LATE_LIMIT = 72 * 3600          # a check is looked for in invoices fiscalized up to three days after it closed
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
            "exclude": sorted({x.upper() for x in re.split(r"[\s,;]+", str(s.get("exclude") or "")) if x})}


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
        """)
    return _conn


def _txt(v):
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return " ".join(_txt(x) for x in v)
    if isinstance(v, dict):
        return " ".join(_txt(x) for x in v.values())
    return str(v)


def check_of(g):
    """A Simphony guest check → the fields the match uses."""
    lines = g.get("detailLines") or []
    tenders, ws, info = Counter(), None, [_txt(g.get("chkInfo")), _txt(g.get("chkRef")), _txt(g.get("posChkGUID"))]
    for d in lines:
        if ws is None and d.get("wsNum") is not None:
            ws = d.get("wsNum")
        tm = d.get("tenderMedia")
        if tm:
            tenders[str(tm.get("tmedNum") if isinstance(tm, dict) else tm)] += float(d.get("dspTtl") or 0)
            if d.get("wsNum") is not None:
                ws = d.get("wsNum")
        if d.get("refInfo1") or d.get("refInfo2"):
            info.append(_txt(d.get("refInfo1")) + " " + _txt(d.get("refInfo2")))
    return {"id": str(g.get("guestCheckId") or g.get("chkNum")), "num": g.get("chkNum"), "name": g.get("chkName") or "",
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
    final = bool(latest and day <= (date.fromisoformat(latest) - timedelta(days=2)).isoformat())
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


def _match(checks, invs, S, used, late_step=True):
    """Pairs checks with invoices (in place: c['inv'] = invoice key); returns the pairs."""
    pairs = []
    by_key = {}
    for i in invs:
        for k in (i["iic"], i["fic"], i["num"]):
            if k and len(k) >= 6:
                by_key[k.upper()] = i
    for c in checks:                                    # 1. the fiscal reference written on the check
        if c.get("inv") or not c["info"]:
            continue
        up = c["info"].upper()
        for k, i in by_key.items():
            if i["_k"] not in used and k in up:
                used.add(i["_k"]); c["inv"] = i["_k"]; c["how"] = "reference"; pairs.append((c, i))
                break
    win = S["window"] * 60
    cand = []                                            # 2. the same total, issued near the closing
    by_amt = defaultdict(list)
    for i in invs:
        if i["_k"] not in used and i["total"] is not None:
            by_amt[round(i["total"])].append(i)
    for c in checks:
        if c.get("inv") or c["_t"] is None:
            continue
        for a in range(round(c["total"] - S["tol"]) - 1, round(c["total"] + S["tol"]) + 2):
            for i in by_amt.get(a, ()):
                if i["_t"] is None or abs(i["total"] - c["total"]) > S["tol"] + 0.005:
                    continue
                dt = i["_t"] - c["_t"]
                if abs(dt) <= win:
                    cand.append((abs(dt), c["id"], i["_k"], c, i))
    cand.sort(key=lambda x: (x[0], x[1], x[2]))
    for _d, _cid, _ik, c, i in cand:
        if c.get("inv") or i["_k"] in used:
            continue
        used.add(i["_k"]); c["inv"] = i["_k"]; c["how"] = "amount and time"; pairs.append((c, i))
    # 3. fiscalized late: the same total, issued after the window and up to three days after the closing
    late = []
    for c in (checks if late_step else ()):
        if c.get("inv") or c["_t"] is None:
            continue
        for a in range(round(c["total"] - S["tol"]) - 1, round(c["total"] + S["tol"]) + 2):
            for i in by_amt.get(a, ()):
                if i["_k"] in used or i["_t"] is None or abs(i["total"] - c["total"]) > S["tol"] + 0.005:
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


def _place(invs, checks_by_ref, M, S):
    """Each invoice → (restaurant ref, how): the mapping of its till, else of its unit, else proposed by matching.
    Proposals go till by till (a unit can hold the tills of several restaurants): the restaurant whose checks its
    invoices match best, when it holds at least 15% of them and twice as many as the next restaurant."""
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
            cs = [dict(c, inv=None) for c in checks]
            scores.append((len(_match(cs, lst, S, used, late_step=False)), ref))
        scores.sort(reverse=True)
        best, second = (scores + [(0, None), (0, None)])[:2]
        if best[1] and best[0] >= max(3, 0.15 * len(lst)) and best[0] >= 2 * second[0]:
            proposals[code] = (best[1], best[0], len(lst))
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


# ---------------------------------------------------------------- build
def _dates(a, b):
    out, d = [], a
    while d <= b:
        out.append(d)
        d += timedelta(days=1)
    return out


def build(company, latest_fn, progress=None, today=None):
    today = today or date.today()
    settings = store.get_settings()
    S = settings_of(settings)
    acct = billy.account(company)
    if not acct:
        raise ValueError(f"{company} has no Billy account with a password (Finance → Settings → Billy).")
    regs = [r for r in restaurants(company, settings) if (r.get("ref") or "").upper() not in S["exclude"]]
    b = today - timedelta(days=1)
    days = _dates(b - timedelta(days=S["days"] - 1), b)
    say = progress or (lambda *_: None)
    errors = []
    # Billy: both methods, day by day
    for src in ("bridge", "fiscal"):
        say(f"Billy · {'invoices of the bridge' if src == 'bridge' else 'invoices of the fiscalization service'}")
        for d in days:
            try:
                billy.read_day(acct, src, d, today)
            except billy.BillyError as exc:
                errors.append(f"Billy {src} {d}: {str(exc)[:160]}")
                if "address" in str(exc) or "did not accept" in str(exc):
                    raise ValueError(str(exc)) from exc
    # Simphony: the closed checks of the company's restaurants
    sim_on = bool(simphony.info().get("configured"))
    latest = {}
    if sim_on and regs:
        say("Simphony · guest checks by day")
        for r in regs:
            try:
                latest[r["ref"]] = latest_fn(r["ref"])
            except Exception:                                       # noqa: BLE001
                latest[r["ref"]] = None
        jobs = [(r["ref"], d.isoformat()) for r in regs for d in days if not latest.get(r["ref"]) or d.isoformat() <= latest[r["ref"]]]
        with ThreadPoolExecutor(max_workers=4) as pool:
            futs = {pool.submit(read_checks, ref, d, latest.get(ref)): (ref, d) for ref, d in jobs}
            for n, f in enumerate(as_completed(futs), 1):
                try:
                    f.result()
                except Exception as exc:                            # noqa: BLE001
                    if len(errors) < 12:
                        errors.append(f"Simphony {futs[f][0]} {futs[f][1]}: {str(exc)[:140]}")
                if n % 10 == 0:
                    say(f"Simphony · guest checks by day ({n} of {len(jobs)})")
    a_, b_ = days[0].isoformat(), days[-1].isoformat()
    bridge = billy.held(acct["nuis"], "bridge", days[0], days[-1])
    fiscal = billy.held(acct["nuis"], "fiscal", days[0], days[-1])
    checks = {r["ref"]: checks_held(r["ref"], a_, b_) for r in regs} if sim_on else {}
    r = analyse(company, acct["nuis"], regs, days, bridge, fiscal, checks, latest, S, today)
    r.update(errors=errors, checked_at=datetime.now().isoformat(timespec="seconds"), complete=not errors,
             simphony=sim_on)
    return r


def analyse(company, nuis, regs, days, bridge, fiscal, checks, latest, S, today):
    M = mapping(S["map"])
    names = {r["ref"]: r.get("name") or r["ref"] for r in regs}
    for code, (ref, label) in M.items():
        names.setdefault(ref, ref)
    # one invoice per IIC: the fiscal service's detail over the bridge's row, the bridge's FIC status kept
    inv = {}
    for x in bridge:
        x = dict(x)
        x["_k"] = x["iic"] or x["fic"] or x["num"]
        x["in_bridge"] = True
        inv[x["_k"]] = x
    for x in fiscal:
        k = x["iic"] or x["fic"] or x["num"]
        if k in inv:
            keep = inv[k]
            merged = dict(keep, **{f: x[f] for f in x if x[f] not in (None, "", [], {}) and f not in ("src", "fic")})
            merged.update(src="fiscal", in_bridge=True, fic=x["fic"] or keep["fic"])
            inv[k] = merged
        else:
            inv[k] = dict(x, _k=k, in_bridge=False)
    orders = [x for x in inv.values() if x.get("order")]       # payment method ORDER: an order, not an invoice
    invs = [x for x in inv.values() if not x.get("order")]
    for i in invs:
        i["_t"] = _ts(i["issue"])
    for ref, lst in checks.items():
        for c in lst:
            c["_t"] = _ts(c["clsd"])
            c["inv"] = None
    sold = {ref: [c for c in lst if not c["cancel"] and abs(c["total"]) >= 0.01] for ref, lst in checks.items()}
    loc_of, proposals = _place(invs, sold, M, S)
    for i in invs:
        i["loc"], i["placed"] = loc_of.get(i["_k"], ("", "unmapped"))
    # match restaurant by restaurant
    used = set()
    by_loc = defaultdict(list)
    for i in invs:
        by_loc[i["loc"]].append(i)
    for ref, lst in sold.items():
        _match(lst, by_loc.get(ref, []), S, used)
    inv_by = {i["_k"]: i for i in invs}
    last_day = days[-1].isoformat()
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
    matched_inv = {c["inv"] for lst in checks.values() for c in lst if c.get("inv")}
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
    # tills and units, the kiosks included
    tills = {}
    for i in invs:
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
    for t in tills.values():
        t["value"] = round(t["value"], 2)
        t["ops"] = len(t["ops"])
        t["kind"] = "kiosk / other till" if t["ref"] and t["matched"] == 0 and t["ref"] in refs_with_checks else (
            "till of Simphony" if t["matched"] else "not mapped" if not t["ref"] else "no checks to compare")
    # shifts and operators: an employee's operator is the one most of their matched checks carry
    emp_op = defaultdict(Counter)
    for ref, lst in checks.items():
        for c in lst:
            if c.get("inv") and inv_by.get(c["inv"], {}).get("op"):
                emp_op[(ref, c["emp"])][inv_by[c["inv"]]["op"]] += 1
    for line in S["operators"].splitlines():
        line = line.split("#", 1)[0]
        if "=" in line:
            op, _, emp = line.partition("=")
            for ref in names:
                emp_op[(ref, _int(emp))] = Counter({op.strip().lower(): 10 ** 9})
    op_of = {k: v.most_common(1)[0][0] for k, v in emp_op.items() if v}
    grid = {}
    for c in out_checks:
        if c["status"] == "zero":
            continue
        op = op_of.get((c["ref"], c["emp"])) or f"employee {c['emp']}"
        g = grid.setdefault((c["ref"], c["shift"], op), _cell(c["ref"], c["shift"], op))
        g["checks"] += 1
        g["sim"] += c["total"]
        if c["status"] in ("missing", "pending"):
            g["not_fisc"] += c["total"]
            g["not_fisc_n"] += 1
        g["emps"].add(c["emp"])
    for i in invs:
        if i["status"] in ("other_till", "summary"):
            continue
        g = grid.setdefault((i["loc"], shift_of(i["issue"], S), i["op"] or "–"), _cell(i["loc"], shift_of(i["issue"], S), i["op"] or "–"))
        g["invoices"] += 1
        g["fisc"] += (i["total"] or 0) if i.get("fic") else 0
        if i["status"] == "no_check":
            g["no_check"] += i["total"] or 0
            g["no_check_n"] += 1
    ops = []
    for g in grid.values():
        g["emps"] = sorted(str(e) for e in g["emps"] if e is not None)
        g["diff"] = round(g["fisc"] - g["sim"], 2)
        for k in ("sim", "fisc", "not_fisc", "no_check"):
            g[k] = round(g[k], 2)
        ops.append(g)
    ops.sort(key=lambda g: (g["ref"], g["shift"], -abs(g["diff"])))
    # summary invoices and their references
    iics = {i["iic"] for i in invs if i["iic"]}
    summary = []
    for i in invs:
        if i.get("refs"):
            found = [x for x in i["refs"] if x.upper() in iics]
            summary.append({"iic": i["iic"], "fic": i["fic"], "num": i["num"], "issue": i["issue"], "total": i["total"],
                            "type": i["type"], "tcr": i["tcr"], "ref": i["loc"], "refs": len(i["refs"]), "found": len(found),
                            "missing": [x for x in i["refs"] if x.upper() not in iics][:20], "fiscalized": bool(i.get("fic"))})
    # the sales book
    book, err_count = [], Counter()
    for i in sorted(invs, key=lambda x: x["issue"] or ""):
        cols, errs = book_row(i, S)
        for e in errs:
            err_count[e] += 1
        book.append({"issue": i["issue"], "ord": i["ord"], "num": i["num"], "iic": i["iic"], "fic": i["fic"],
                     "buyer": i["buyer"], "buyer_id": i["buyer_id"], "type": i["type"], "total": i["total"],
                     "bu": i["bu"], "tcr": i["tcr"], "op": i["op"], "ref": i["loc"], "einv": i["einv"], "eic": i["eic"],
                     **cols, "errors": errs})
    # orders: placed like invoices, and whether an invoice of the same total follows on the same till
    oloc, _p = _place(orders, {}, M, S) if orders else ({}, {})
    by_till_total = defaultdict(list)
    for i in invs:
        if i["_t"] is not None and i["total"] is not None:
            by_till_total[(i["tcr"], round(i["total"]))].append(i["_t"])
    out_orders, till_ref = [], {t["tcr"]: t["ref"] for t in tills.values()}
    for o in sorted(orders, key=lambda x: x["issue"] or ""):
        t0 = _ts(o["issue"])
        nxt = [t for t in by_till_total.get((o["tcr"], round(o["total"] or 0)), []) if t0 is not None and 0 <= t - t0 <= 24 * 3600]
        out_orders.append({"issue": o["issue"], "num": o["num"], "iic": o["iic"], "fic": o["fic"], "total": o["total"],
                           "tcr": o["tcr"], "bu": o["bu"], "op": o["op"], "ref": till_ref.get(o["tcr"]) or oloc.get(o["_k"], ("", ""))[0],
                           "pays": ", ".join(p[0] for p in o.get("pays") or []), "invoiced": bool(nxt),
                           "after_min": round((min(nxt) - t0) / 60) if nxt else None})
    # restaurants and days
    locs = []
    for ref in sorted(set(checks) | {i["loc"] for i in invs if i["loc"]}):
        cs = [c for c in out_checks if c["ref"] == ref]
        iv = [i for i in invs if i["loc"] == ref]
        cells = {}
        for d in days:
            ds = d.isoformat()
            dc = [c for c in cs if c["day"] == ds and c["status"] != "zero"]
            di = [i for i in iv if (i["issue"] or "")[:10] == ds]
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
                     "missing_value": round(sum(c["total"] for c in cs if c["status"] == "missing"), 2)})
    counted = [c for c in out_checks if c["status"] not in ("zero", "pending")]
    kpis = {"checks": len(counted), "fiscalized": sum(1 for c in counted if c["status"] in ("ok", "late", "late48")),
            "missing": sum(1 for c in counted if c["status"] == "missing"),
            "missing_value": round(sum(c["total"] for c in counted if c["status"] == "missing"), 2),
            "late": sum(1 for c in counted if c["status"] == "late"), "late48": sum(1 for c in counted if c["status"] == "late48"),
            "invoices": len(invs), "no_check": sum(1 for i in invs if i["status"] == "no_check"),
            "no_check_value": round(sum(i["total"] or 0 for i in invs if i["status"] == "no_check"), 2),
            "no_fic": sum(1 for i in invs if i["status"] == "no_fic"),
            "no_fic_value": round(sum(i["total"] or 0 for i in invs if i["status"] == "no_fic"), 2),
            "other_till": sum(1 for i in invs if i["status"] == "other_till"),
            "summary": len(summary), "book_errors": sum(1 for x in book if [e for e in x["errors"] if e not in INFO]),
            "book_nodetail": sum(1 for x in book if "no_detail" in x["errors"]),
            "orders": len(out_orders), "orders_value": round(sum(o["total"] or 0 for o in out_orders), 2),
            "orders_open": sum(1 for o in out_orders if not o["invoiced"]),
            "unmapped": sum(1 for t in tills.values() if not t["ref"]), "proposed": len(proposals)}
    no_check = sorted(({"iic": i["iic"], "fic": i["fic"], "num": i["num"], "issue": i["issue"], "total": i["total"],
                        "tcr": i["tcr"], "bu": i["bu"], "op": i["op"], "ref": i["loc"], "status": i["status"],
                        "shift": shift_of(i["issue"], S)}
                       for i in invs if i["status"] in ("no_check", "no_fic")), key=lambda x: x["issue"] or "")
    return {"company": company, "nuis": nuis, "days": [d.isoformat() for d in days], "settings": {k: v for k, v in S.items() if k != "shifts"} | {"shift_names": [s[2] for s in S["shifts"]]},
            "kpis": kpis, "locations": locs, "checks": out_checks, "invoices": no_check, "tills": sorted(tills.values(), key=lambda t: (t["ref"], t["tcr"])),
            "proposals": {k: {"ref": v[0], "matched": v[1], "invoices": v[2]} for k, v in proposals.items()},
            "operators": ops, "summary": summary, "book": book, "book_errors": dict(err_count), "orders": out_orders,
            "info_errors": list(INFO),
            "status": STATUS, "inv_status": INV_STATUS, "error_labels": ERRORS, "rates": S["rates"],
            "definitions": DEFINITIONS}


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
             "and issued within the window of the check's closing time, the nearest first. Each invoice is used once.",
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
}


# ---------------------------------------------------------------- the scope of a person
def cut(r, allowed):
    if allowed is None or not isinstance(r, dict) or not isinstance(r.get("locations"), list):
        return r
    A = {str(x).strip().upper() for x in allowed}
    keep = {L["ref"] for L in r["locations"] if {str(L.get(k) or "").upper() for k in ("ref", "bc", "dept")} & A}
    ok = lambda x: x.get("ref") in keep
    return dict(r, locations=[L for L in r["locations"] if L["ref"] in keep], checks=[c for c in r["checks"] if ok(c)],
                invoices=[i for i in r["invoices"] if ok(i)], tills=[t for t in r["tills"] if ok(t)],
                operators=[o for o in r["operators"] if ok(o)], summary=[s for s in r["summary"] if ok(s)],
                book=[b for b in r["book"] if ok(b)], orders=[o for o in r.get("orders") or [] if ok(o)], proposals={}, cut=True,
                kpis=dict(r["kpis"], **_kpis_of(r, keep)))


def _kpis_of(r, keep):
    cs = [c for c in r["checks"] if c["ref"] in keep and c["status"] not in ("zero", "pending")]
    iv = [i for i in r["invoices"] if i["ref"] in keep]
    return {"checks": len(cs), "fiscalized": sum(1 for c in cs if c["status"] in ("ok", "late", "late48")),
            "missing": sum(1 for c in cs if c["status"] == "missing"),
            "missing_value": round(sum(c["total"] for c in cs if c["status"] == "missing"), 2),
            "late": sum(1 for c in cs if c["status"] == "late"), "late48": sum(1 for c in cs if c["status"] == "late48"),
            "no_check": sum(1 for i in iv if i["status"] == "no_check"),
            "no_check_value": round(sum(i["total"] or 0 for i in iv if i["status"] == "no_check"), 2),
            "no_fic": sum(1 for i in iv if i["status"] == "no_fic"),
            "no_fic_value": round(sum(i["total"] or 0 for i in iv if i["status"] == "no_fic"), 2),
            "invoices": sum(1 for b in r["book"] if b["ref"] in keep), "unmapped": 0, "proposed": 0,
            "summary": sum(1 for s in r["summary"] if s["ref"] in keep),
            "book_errors": sum(1 for b in r["book"] if b["ref"] in keep and [e for e in b["errors"] if e not in INFO]),
            "orders": sum(1 for o in r.get("orders") or [] if o["ref"] in keep),
            "orders_value": round(sum(o["total"] or 0 for o in r.get("orders") or [] if o["ref"] in keep), 2),
            "orders_open": sum(1 for o in r.get("orders") or [] if o["ref"] in keep and not o["invoiced"])}


# ---------------------------------------------------------------- held results and background reads
def held(company):
    with _lock:
        hit = _results.get(company)
    return hit[1] if hit else None


def remember(company, r):
    with _lock:
        _results[company] = (time.time(), r)


def request(company, latest_fn, refresh=False):
    with _lock:
        hit = _results.get(company)
        job = _jobs.get(company)
        if hit and not refresh and time.time() - hit[0] < KEEP and not (job and job["state"] == "running"):
            return hit[1]
        if job and job["state"] == "running":
            return {"pending": True, "stage": job["stage"]}
        if job and job["state"] == "failed" and not refresh and time.time() - job["at"] < 60:
            _jobs.pop(company, None)
            return {"available": False, "error": job["error"]}
        job = _jobs[company] = {"state": "running", "stage": "starting", "at": time.time(), "error": ""}

    def go():
        try:
            r = build(company, latest_fn, progress=lambda st: job.update(stage=st))
            if on_built:
                try:
                    r["register"] = on_built(company, r)
                except Exception:                                  # noqa: BLE001
                    traceback.print_exc()
            remember(company, r)
            job["state"] = "done"
        except Exception as exc:                                   # noqa: BLE001
            traceback.print_exc()
            job.update(state="failed", error=str(exc)[:300], at=time.time())
    threading.Thread(target=go, daemon=True, name=f"billyrec-{company}").start()
    return {"pending": True, "stage": "starting"}


# ---------------------------------------------------------------- the register
def findings(r):
    """Exceptions for exreg.sync (source "billy_rec"): not fiscalized checks by restaurant and day, invoices without
    a FIC, and the sales book errors by kind."""
    out = []
    for L in r["locations"]:
        for d, c in L["cells"].items():
            if c["missing"]:
                out.append({"subject": f"{d} missing {L['ref']}", "kind": "missing", "location": L["bc"] or L["ref"],
                            "title": f"{L['name']} {d}: {c['missing']} checks not fiscalized",
                            "detail": f"{c['missing']} Simphony checks for {c['missing_value']:,.0f} have no fiscalized invoice "
                                      f"in Billy (window {r['settings']['window']:g} min, tolerance {r['settings']['tol']:g}).",
                            "severity": "critical", "value": abs(c["missing_value"])})
    by_day = defaultdict(list)
    for i in r["invoices"]:
        if i["status"] == "no_fic":
            by_day[(i["issue"] or "")[:10]].append(i)
    for d, lst in by_day.items():
        out.append({"subject": f"{d} no_fic", "kind": "no_fic", "location": "",
                    "title": f"{d}: {len(lst)} invoices of the bridge without a FIC",
                    "detail": "Issued at the till, not fiscalized with the Tax Administration: " +
                              ", ".join(f"{x['num'] or x['iic'][:10]} {x['total'] or 0:,.0f}" for x in lst[:8]),
                    "severity": "critical", "value": abs(sum(x["total"] or 0 for x in lst))})
    errs = defaultdict(list)
    for b in r["book"]:
        for e in b["errors"]:
            if e not in INFO:
                errs[e].append(b)
    for e, lst in errs.items():
        out.append({"subject": f"book {e} {r['days'][0]}", "kind": "book_" + e, "location": "",
                    "title": f"Sales book: {ERRORS[e]} ({len(lst)} invoices)",
                    "detail": ", ".join(f"{b['num'] or b['iic'][:10]} {b['total'] or 0:,.0f}" for b in lst[:8]),
                    "severity": "warning", "value": abs(sum(b["total"] or 0 for b in lst))})
    return out


def in_scope(r):
    days = set(r["days"])
    return lambda x: (x["subject"] or "")[:10] in days or (x["subject"] or "").startswith("book ")


# ---------------------------------------------------------------- the Excel: the sales book and the cases
BOOK_COLS = [("issue", "Data dhe ora"), ("ord", "Nr. rendor"), ("num", "Nr. i faturës"), ("iic", "NSLF (IIC)"),
             ("fic", "NIVF (FIC)"), ("buyer", "Blerësi"), ("buyer_id", "NIPT i blerësit"), ("type", "Pagesa"),
             ("total", "Vlera totale me TVSH"), ("exempt", "Shitje të përjashtuara"), ("no_vat", "Shitje pa TVSH"),
             ("zero", "Shitje me 0% / eksporte")]


def render_xlsx(r):
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    ws = wb.active
    ws.title = "Libri i Shitjes"
    rates = sorted((x for x in r["rates"] if x > 0), reverse=True)
    cols = BOOK_COLS + [c for x in rates for c in ((f"b{x:g}", f"Vlera e tatueshme {x:g}%"), (f"v{x:g}", f"TVSH {x:g}%"))] + \
        [("bu", "Njësia (BU)"), ("tcr", "Arka (TCR)"), ("op", "Operatori"), ("ref", "Restoranti"), ("eic", "EIC"), ("errors", "Gabime")]
    ws.append([f"Libri i Shitjes · {r['company']} · NIPT {r['nuis']} · {r['days'][0]} – {r['days'][-1]}"])
    ws.append([c[1] for c in cols])
    for c in ws[2]:
        c.font = Font(bold=True)
    for b in r["book"]:
        ws.append([", ".join(r["error_labels"].get(e, e) for e in b["errors"]) if k == "errors" else
                   (b.get(k) or "").replace("T", " ") if k == "issue" else b.get(k) for k, _ in cols])
    tot = ["Totali"] + [""] * 7 + [round(sum((b.get(k) or 0) for b in r["book"]), 2) for k, _ in cols[8:-6]]
    ws.append(tot)
    w2 = wb.create_sheet("Checks not fiscalized")
    w2.append(["Restaurant", "Business day", "Check", "Closed", "Total", "Employee", "Shift", "Status"])
    for c in r["checks"]:
        if c["status"] in ("missing", "late48"):
            w2.append([c["ref"], c["day"], c["num"], c["clsd"], c["total"], c["emp"], c["shift"], r["status"][c["status"]]])
    w3 = wb.create_sheet("Invoices without check")
    w3.append(["Issued", "Number", "IIC", "FIC", "Total", "Business unit", "Till", "Operator", "Restaurant", "Status"])
    for i in r["invoices"]:
        w3.append([i["issue"], i["num"], i["iic"], i["fic"], i["total"], i["bu"], i["tcr"], i["op"], i["ref"], r["inv_status"][i["status"]]])
    w4 = wb.create_sheet("Shift & operator")
    w4.append(["Restaurant", "Shift", "Operator", "Employees", "Checks", "Simphony", "Fiscalized", "Difference",
               "Not fiscalized", "Invoices without check"])
    for o in r["operators"]:
        w4.append([o["ref"], o["shift"], o["op"], ", ".join(o["emps"]), o["checks"], o["sim"], o["fisc"], o["diff"],
                   o["not_fisc"], o["no_check"]])
    w6 = wb.create_sheet("Orders")
    w6.append(["Issued", "Number", "IIC", "FIC", "Total", "Till", "Operator", "Restaurant", "Payment", "Invoice after (min)"])
    for x in r.get("orders") or []:
        w6.append([x["issue"], x["num"], x["iic"], x["fic"], x["total"], x["tcr"], x["op"], x["ref"], x["pays"],
                   x["after_min"] if x["invoiced"] else "no invoice of the same total"])
    w5 = wb.create_sheet("Tills")
    w5.append(["Till (TCR)", "Business unit", "Restaurant", "Placed", "Label", "Kind", "Invoices", "Value", "With a check", "No FIC"])
    for t in r["tills"]:
        w5.append([t["tcr"], t["bu"], t["ref"], t["placed"], t["label"], t["kind"], t["invoices"], t["value"], t["matched"], t["no_fic"]])
    for sh in wb.worksheets:
        for col in sh.columns:
            sh.column_dimensions[col[0].column_letter].width = 16
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
