"""Cash & bank → Location intelligence: what each restaurant took at the till next to what reached the
cash boxes and the banks in Business Central, for the month chosen.

Per location (the restaurant registry of Settings → Location intelligence, this company's rows):
  * Simphony: the payments of every business day by tender media (getTenderMediaDailyTotals), put in
    groups by the tender's category in Simphony (cash, credit and debit cards, on account, vouchers and
    gift cards, comps and staff meals, other); paid out and paid in apart; and the net sales of the day;
  * Business Central: the bank account ledger entries posted on the restaurant's department (through the
    dimension set): what came in to the cash boxes (ARKA ...), to the banks (card settlements and
    deposits) and to online and wallet accounts (Paysera, POK), and what went out (paid outs, fees);
  * the comparison: cash taken against cash received in BC, cards against bank and online receipts,
    day by day, and the days with takings and nothing posted.
Card settlements and cash deposits can arrive a day or two later, so the month is the measure; a day
shows where to look. Movements on no department, or on one that is not a restaurant, are listed apart.
Read-only on both sides; the reads run in the background and the screen asks again.
"""
import threading
import time
import traceback
from datetime import date, timedelta

import locintel
import simphony
import store

_jobs, _lock = {}, threading.Lock()
KEEP = 600
_sets = {}
GROUPS = ("cash", "card", "account", "voucher", "comp", "other")
GROUP_LABEL = {"cash": "Cash", "card": "Cards", "account": "On account", "voucher": "Vouchers & gift cards",
               "comp": "Comps & staff meals", "other": "Other"}


def tender_group(t):
    """The group of a Simphony tender: its category (cat), else its subtype, else its name."""
    try:
        typ = int(t.get("type") or 1)
    except (TypeError, ValueError):
        typ = 1
    if typ == 2:
        return "paid_out"
    if typ == 3:
        return "paid_in"
    cat, sub = t.get("cat"), t.get("subType")
    if cat == 2 or sub == 1:
        return "cash"
    if cat in (4, 5) or sub == 4:
        return "card"
    if cat == 10 or sub in (5, 7):
        return "account"
    if cat in (1, 6, 7, 8, 9) or sub in (3, 6):
        return "voucher"
    if cat == 15 or sub in (2, 8, 9):
        return "comp"
    n = f"{t.get('name') or ''} {t.get('mstrName') or ''}".upper()
    if any(k in n for k in ("CASH", "KESH", "CONTANT", "LEK", "EURO CASH")):
        return "cash"
    if any(k in n for k in ("CARD", "VISA", "MASTER", "KART", "DEBIT", "CREDIT", "POS", "AMEX", "MAESTRO")):
        return "card"
    if any(k in n for k in ("ROOM", "ACCOUNT", "LLOGARI", "CORPORATE", "INVOICE", "FATUR", "KREDI")):
        return "account"
    if any(k in n for k in ("VOUCHER", "GIFT", "KUPON", "COUPON")):
        return "voucher"
    if any(k in n for k in ("COMP", "STAFF", "EMPLOY", "STAF", "OFERT", "FALAS")):
        return "comp"
    return "other"


def account_kind(no, name):
    """cash box, bank, or online and wallet account, from the BC bank account's name."""
    n = f"{no or ''} {name or ''}".upper()
    if "ARKA" in n or "CASH" in n or "KESH" in n:
        return "cash"
    if any(k in n for k in ("PAYSERA", "POK", "WOLT", "PAYPAL", "STRIPE")):
        return "online"
    return "bank"


def _dim_sets(client, company):
    hit = _sets.get(company)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    value = client.dimension_sets(company)
    _sets[company] = (time.time(), value)
    return value


def _days(a, b):
    out, d = [], a
    while d <= b:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _empty_pos():
    return {**{g: 0.0 for g in GROUPS}, "paid_out": 0.0, "paid_in": 0.0, "net": 0.0, "checks": 0, "days": 0,
            "tenders": {}, "daily": {}}


def _empty_bc():
    return {"cash": 0.0, "bank": 0.0, "online": 0.0, "out": 0.0, "accounts": {}, "daily": {}, "entries": 0}


def build(client, company, period, currency, latest_fn, parallel_fn, progress):
    settings = store.get_settings()
    c_from, c_to, running = period[0], period[1], period[6]
    if running:
        c_to = min(c_to, date.today() - timedelta(days=1))
    if c_to < c_from:
        c_to = c_from
    days = _days(c_from, c_to)
    reg = [r for r in locintel.registry(settings) if (r.get("company") or "").upper() == (company or "").upper()]
    by_dept = {}
    for r in reg:
        d = r.get("dept") or r.get("bc")
        if d:
            by_dept.setdefault(d, r)
    errors, out = [], {}

    def loc(code, name=None):
        L = out.get(code)
        if L is None:
            r = by_dept.get(code)
            L = out[code] = {"name": (r or {}).get("name") or name or code or "Not on a location",
                             "group": "restaurant" if r else ("unassigned" if not code else "support"),
                             "brand": (r or {}).get("brand") or "", "ref": (r or {}).get("ref") or "",
                             "currency": (r or {}).get("currency") or currency, "seasonal": bool((r or {}).get("seasonal")),
                             "pos": None, "bc": None}
        return L

    # ---- Simphony: tenders and net sales, every location and day
    sim_on = bool(simphony.info().get("configured"))
    if sim_on:
        progress("Simphony: tender media of every location")
        refs = [r for r in reg if r.get("ref") and (r.get("dept") or r.get("bc"))]
        latest = {}
        for ref, (v, e) in parallel_fn({r["ref"]: (lambda x=r["ref"]: latest_fn(x)) for r in refs}).items():
            latest[ref] = v if not e else None
        dims = {}
        for ref, (v, e) in parallel_fn({r["ref"]: (lambda x=r["ref"]: simphony.tender_dims(x)) for r in refs}, workers=4).items():
            if e:
                errors.append(f"Simphony {ref}: tender media could not be read ({str(e)[:120]})")
            dims[ref] = v or {}
        jobs = {}
        for r in refs:
            lt = latest.get(r["ref"])
            for d in days:
                if lt and d > lt:
                    continue
                jobs[("t", r["ref"], d)] = (lambda x=r["ref"], dd=d, l=lt: simphony.tender_totals(x, dd, l))
                jobs[("o", r["ref"], d)] = (lambda x=r["ref"], dd=d, l=lt: simphony.daily_totals(x, dd, l))
        progress(f"Simphony: {len(jobs) // 2} location-days")
        res = parallel_fn(jobs, workers=4)
        simphony.flush_days()
        failed = 0
        for (kind, ref, d), (v, e) in res.items():
            r = next(x for x in refs if x["ref"] == ref)
            L = loc(r.get("dept") or r.get("bc"))
            P = L["pos"] = L["pos"] or _empty_pos()
            if e or v is None:
                failed += 1
                continue
            day = P["daily"].setdefault(d, {"cash": 0.0, "card": 0.0, "other": 0.0, "net": 0.0})
            if kind == "o":
                f = simphony.day_figures(v.get("rvcs") or [])
                P["net"] += f["net"]
                P["checks"] += f["checks"]
                day["net"] += f["net"]
                if f["net"]:
                    P["days"] += 1
                continue
            for num, (ttl, cnt) in v.items():
                t = dims.get(ref, {}).get(int(num)) or {"name": f"Tender {num}"}
                g = tender_group(t)
                if g in ("paid_out", "paid_in"):
                    P[g] += abs(ttl)
                else:
                    P[g] += ttl
                    day[g if g in ("cash", "card") else "other"] += ttl
                x = P["tenders"].setdefault(t.get("name") or num, [g, 0.0, 0])
                x[1] += ttl
                x[2] += cnt
        if failed:
            errors.append(f"Simphony: {failed} location-days could not be read; they are left out.")
    else:
        errors.append("Simphony is not set up (Settings → Simphony): the till side is missing.")

    # ---- Business Central: receipts and payments on the department
    progress("Business Central: bank and cash-box movements")
    try:
        sets = _dim_sets(client, company)
        rows = client.bank_entries_dim(company, c_from.isoformat(), c_to.isoformat())
        for e in rows:
            dept = sets.get(int(e.get("Dimension_Set_ID") or 0), "")
            L = loc(dept)
            B = L["bc"] = L["bc"] or _empty_bc()
            dr, cr = float(e.get("Debit_Amount_LCY") or 0), float(e.get("Credit_Amount_LCY") or 0)
            if not dr and not cr:
                amt = float(e.get("Amount_LCY") or 0)
                dr, cr = max(amt, 0.0), max(-amt, 0.0)
            kind = account_kind(e.get("Bank_Account_No"), e.get("Bank_Account_Name"))
            B["entries"] += 1
            B[kind] += dr
            B["out"] += cr
            a = B["accounts"].setdefault(e.get("Bank_Account_Name") or e.get("Bank_Account_No") or "?", [kind, 0.0, 0.0])
            a[1] += dr
            a[2] += cr
            d = str(e.get("Posting_Date") or "")[:10]
            if dr:
                day = B["daily"].setdefault(d, {"cash": 0.0, "bank": 0.0, "online": 0.0})
                day[kind] += dr
    except Exception as exc:                                       # noqa: BLE001
        traceback.print_exc()
        errors.append(f"Business Central bank ledger: {str(exc)[:200]}")

    # ---- the comparison and the alerts
    tol = float((settings.get("locations") or {}).get("posting_gap_pct") or 3)
    for code, L in out.items():
        P, B = L["pos"], L["bc"]
        rec, al = {}, []
        if P:
            P["taken"] = P["cash"] + P["card"]
            P["tenders"] = sorted(([k, v[0], round(v[1]), v[2]] for k, v in P["tenders"].items()), key=lambda x: -abs(x[2]))
            for k in GROUPS + ("paid_out", "paid_in", "net", "taken"):
                P[k] = round(P[k])
        if B:
            B["received"] = B["cash"] + B["bank"] + B["online"]
            B["accounts"] = sorted(([k, v[0], round(v[1]), round(v[2])] for k, v in B["accounts"].items()), key=lambda x: -x[2])
            for k in ("cash", "bank", "online", "out", "received"):
                B[k] = round(B[k])
        if P and L["group"] == "restaurant":
            bc_cash = (B or {}).get("cash", 0)
            bc_noncash = (B or {}).get("bank", 0) + (B or {}).get("online", 0)
            rec = {"cash_gap": bc_cash - P["cash"], "card_gap": bc_noncash - P["card"],
                   "total_gap": (B or {}).get("received", 0) - P["taken"]}
            rec["cash_gap_pct"] = round(rec["cash_gap"] / P["cash"] * 100, 1) if P["cash"] else None
            rec["card_gap_pct"] = round(rec["card_gap"] / P["card"] * 100, 1) if P["card"] else None
            rec["total_gap_pct"] = round(rec["total_gap"] / P["taken"] * 100, 1) if P["taken"] else None
            # days with takings at the till and no receipt at all in BC on the department
            bdays = set((B or {}).get("daily", {}).keys())
            rec["unposted"] = sorted(d for d, x in P["daily"].items() if (x["cash"] + x["card"]) > 0 and d not in bdays)
            if rec["cash_gap_pct"] is not None and rec["cash_gap_pct"] < -tol:
                al.append(["crit", f"Cash received in BC {rec['cash_gap_pct']:+.0f}% against cash taken"])
            elif rec["cash_gap_pct"] is not None and rec["cash_gap_pct"] > tol:
                al.append(["warn", f"More cash received in BC than taken ({rec['cash_gap_pct']:+.0f}%)"])
            if rec["card_gap_pct"] is not None and abs(rec["card_gap_pct"]) > tol * 2:
                al.append(["warn", f"Bank and online receipts {rec['card_gap_pct']:+.0f}% against cards"])
            if len(rec["unposted"]) > 2:
                al.append(["warn", f"{len(rec['unposted'])} days with takings and nothing posted in BC"])
            if P["paid_out"] and P["taken"] and P["paid_out"] > P["taken"] * 0.05:
                al.append(["info", f"Paid out at the till {P['paid_out'] / P['taken'] * 100:.0f}% of takings"])
        L["rec"], L["alerts"] = rec, al
        if B:
            B["daily"] = {d: {k: round(v) for k, v in x.items()} for d, x in sorted(B["daily"].items())}
        if P:
            P["daily"] = {d: {k: round(v) for k, v in x.items()} for d, x in sorted(P["daily"].items())}
    out = {k: v for k, v in out.items() if (v["pos"] and (v["pos"]["taken"] or v["pos"]["net"])) or (v["bc"] and v["bc"]["entries"])}
    return {"company": company, "currency": currency, "locations": out, "errors": errors,
            "period": {"from": c_from.isoformat(), "to": c_to.isoformat(), "label": period[7], "running": running,
                       "days": len(days)},
            "simphony": sim_on, "tolerance": tol, "groups": GROUP_LABEL, "definitions": DEFINITIONS}


def request(client, company, period, currency, latest_fn, parallel_fn, refresh=False):
    key = (company or "", period[0].isoformat(), period[1].isoformat())
    now = time.time()
    with _lock:
        job = _jobs.get(key)
        if job and job["state"] == "running":
            return {"pending": True, "stage": job.get("stage", ""), "elapsed": int(now - job["started"])}
        if job and job["state"] == "done" and not refresh and now - job["finished"] < KEEP:
            return job["result"]
        if job and job["state"] == "failed" and not refresh and now - job["finished"] < 60:
            return {"available": False, "error": job["error"]}
        job = _jobs[key] = {"state": "running", "started": now, "stage": "Starting"}

    def progress(text):
        with _lock:
            job["stage"] = text

    def run():
        try:
            r = build(client, company, period, currency, latest_fn, parallel_fn, progress)
            with _lock:
                job.update(state="done", finished=time.time(), result=r)
        except Exception as exc:                                   # noqa: BLE001
            traceback.print_exc()
            with _lock:
                job.update(state="failed", finished=time.time(), error=str(exc)[:300])
    threading.Thread(target=run, daemon=True, name="cashloc").start()
    return {"pending": True, "stage": "Starting", "elapsed": 0}


DEFINITIONS = {
    "pos": "Simphony: the payments of the business days of the period by tender media, in groups by the tender's "
           "category in Simphony (cash; credit and debit cards; on account, room and corporate charges; vouchers "
           "and gift cards; comps and staff meals; other). Takings = cash + cards. Paid out and paid in are the "
           "till's pickups and loans. A running month stops yesterday.",
    "bc": "Business Central: bank account ledger entries of the period posted on the restaurant's department "
          "(dimension set): received = debits, split by account into cash boxes (ARKA), banks (card settlements, "
          "deposits, transfers) and online and wallet accounts (Paysera, POK); out = credits on the department "
          "(paid outs, fees, refunds).",
    "rec": "Cash gap = cash received in the cash boxes − cash taken at the till; card gap = bank and online "
           "receipts − card takings. Settlements and deposits can post a day or two later, so the month is the "
           "measure. Days not posted = days with takings and no receipt at all on the department.",
}
