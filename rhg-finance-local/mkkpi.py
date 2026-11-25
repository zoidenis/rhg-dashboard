"""Marketing → Campaigns (Geri, 25 Nov 2026): did the campaigns work? nl360's ads, Google listing and social media of
every brand and OPA unit, month by month, against the sales Simphony recorded at its tills.

The KPIs (approved by Geri):
  * marketing % of sales = ad spend (Meta + Google, nl360, euros → lek at the group rate of Settings) ÷ Simphony net
    sales of the month;
  * sales lift = the brand's (unit's) sales growth against the same month last year − the growth of the rest of RHG in
    that month, in points: growth the market alone does not explain;
  * cost per guest and per check = ad spend ÷ Simphony guests (checks); how checks moved against last year;
  * the ads funnel: clicks → reservations (SevenRooms venues) and direction requests → checks: cost per click, per
    reservation and per direction request; reservations per 100 clicks;
  * also: sales per lek of ads (MER), nl360's estimated return (estimated ads revenue ÷ spend, an estimate, not tracked
    sales), Google listing actions per 100 checks, new Google reviews per 1,000 checks.
A month with ad spend is a success when its lift is 5 points or more ("Working"), a failure at −5 or less ("Not
working"), else "Neutral"; no spend: "No ads"; no sales last year: not judged.
Simphony: the days already read (the same file as Sales); the days missing are read behind, a few at a time. Nothing is
written to Simphony or nl360.
"""
import calendar
import re
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import locintel
import menueng
import simphony
import store

LIFT = 5.0
_job = {"running": False, "done": 0, "total": 0, "at": 0.0}
_lock = threading.Lock()


def _at(arr, k):
    if isinstance(arr, list):
        return arr[k] if 0 <= k < len(arr) else None
    if isinstance(arr, dict):
        return arr.get(str(k))
    return None


def _ym_shift(ym, years):
    y, m = int(ym[:4]), int(ym[5:7])
    return f"{y + years:04d}-{m:02d}"


def _days(ym):
    y, m = int(ym[:4]), int(ym[5:7])
    return [date(y, m, d).isoformat() for d in range(1, calendar.monthrange(y, m)[1] + 1)]


def _key(s):
    return re.sub(r"[^A-Z0-9]+", "", str(s or "").upper())


def entities(pub, reg):
    """Every nl360 brand and OPA unit with the Simphony locations it stands for: its units' tills, else the registry's
    restaurants of the same brand (or name)."""
    out, unmatched = [], []
    for b in pub.get("brands") or []:
        tills = [l.get("till") for l in b.get("locs") or [] if l.get("till")]
        if tills:
            refs = tills
        else:
            names = {_key(b.get("id")), _key(b.get("name")), _key(str(b.get("name") or "").split(" ")[0])} - {""}
            refs = [r["ref"] for r in reg if _key(r.get("brand")) in names or _key(r.get("name")) == _key(b.get("name"))]
        if not refs:
            unmatched.append(b.get("name") or b.get("id"))
        out.append({"id": b.get("id"), "kind": "brand", "name": b.get("name"), "parent": None, "refs": sorted(set(refs)),
                    "src": b})
        for l in b.get("locs") or []:
            if l.get("till"):
                out.append({"id": f"{b.get('id')}/{l.get('slug')}", "kind": "unit", "name": l.get("name"), "parent": b.get("id"),
                            "refs": [l["till"]], "src": l})
    return out, unmatched


def sim_months(refs, months):
    """{(ref, ym): {net, checks, guests, days, held}} from the days already read; and the days still to read."""
    today = date.today().isoformat()
    out, missing = {}, []
    for ref in refs:
        for ym in months:
            t = {"net": 0.0, "checks": 0, "guests": 0, "days": 0, "held": 0}
            for d in _days(ym):
                if d >= today:
                    continue
                t["days"] += 1
                h = simphony.held_day(ref, d)
                if h is None:
                    missing.append((ref, d))
                    continue
                f = simphony.day_figures(h.get("rvcs") or [])
                t["net"] += f["net"]
                t["checks"] += f["checks"]
                t["guests"] += f["guests"]
                t["held"] += 1
            out[(ref, ym)] = t
    return out, missing


def read_behind(missing):
    """Read the missing days of Simphony in the background, four at a time; one job at a time."""
    with _lock:
        if _job["running"] or not missing:
            return
        _job.update(running=True, done=0, total=len(missing), at=time.time())

    def go():
        try:
            latest = {}
            for ref in {r for r, _d in missing}:
                try:
                    latest[ref] = simphony.latest_business_date(ref)
                except Exception:                                    # noqa: BLE001
                    latest[ref] = None

            def one(rd):
                ref, d = rd
                if latest.get(ref) and d > latest[ref]:
                    return
                try:
                    simphony.daily_totals(ref, d, latest.get(ref))
                except Exception:                                    # noqa: BLE001
                    pass
                with _lock:
                    _job["done"] += 1
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(one, missing))
            simphony.flush_days()
        except Exception:                                            # noqa: BLE001
            traceback.print_exc()
        finally:
            with _lock:
                _job["running"] = False
    threading.Thread(target=go, daemon=True, name="marketing-simphony").start()


def eur_rate(settings, pub=None):
    """Lek for one euro: the group rates of Settings (EUR=…), else nl360's own rate."""
    reporting, rates = menueng._rates(settings)
    if reporting == "ALL" and rates.get("EUR"):
        return float(rates["EUR"]), "the group rate of Settings"
    if reporting == "EUR" and rates.get("ALL"):
        return 1 / float(rates["ALL"]), "the group rate of Settings"
    fx = (pub or {}).get("fx_rate")
    if fx:
        return float(fx), "nl360's rate"
    return None, ""


def _num(x):
    try:
        return float(x) if x is not None else None
    except (TypeError, ValueError):
        return None


def build(pub, settings=None, read=True):
    settings = settings or store.get_settings()
    if not pub.get("available"):
        return {"available": False, "error": pub.get("error") or "No marketing data yet."}
    months = pub.get("months") or []
    reg = locintel.registry(settings)
    ents, unmatched = entities(pub, reg)
    refs = sorted({r for e in ents for r in e["refs"]})
    need = sorted(set(months) | {_ym_shift(m, -1) for m in months})
    sim, missing = sim_months(refs, need)
    if read and missing:
        read_behind(missing)
    rate, rate_src = eur_rate(settings, pub)
    all_refs = set(refs)

    def tot(rs, ym, f):
        return sum(sim[(r, ym)][f] for r in rs if (r, ym) in sim)

    def complete(rs, ym):
        return all(sim[(r, ym)]["held"] >= sim[(r, ym)]["days"] for r in rs if (r, ym) in sim)

    out = []
    for e in ents:
        s, rs, rows = e["src"], e["refs"], []
        others = all_refs - set(rs)
        for k, ym in enumerate(months):
            ly = _ym_shift(ym, -1)
            a = _at(s.get("ads"), k) or {}
            spend_eur = (_num(a.get("metaSpend")) or 0) + (_num(a.get("gSpend")) or 0) if a else None
            spend = spend_eur * rate if spend_eur is not None and rate else None
            sales, sales_ly = tot(rs, ym, "net") if rs else None, tot(rs, ly, "net") if rs else None
            checks, checks_ly, guests = tot(rs, ym, "checks"), tot(rs, ly, "checks"), tot(rs, ym, "guests")
            g = (sales / sales_ly - 1) * 100 if sales and sales_ly else None
            o, o_ly = tot(others, ym, "net"), tot(others, ly, "net")
            gg = (o / o_ly - 1) * 100 if o and o_ly else None
            lift = g - gg if g is not None and gg is not None else None
            clicks = (_num(a.get("metaClicks")) or 0) + (_num(a.get("gClicks")) or 0) if a else None
            res, dirs, est = _num(a.get("res")), _num(a.get("gDirs")), _num(a.get("estRev"))
            gbp = s.get("gbp") or {}
            inter = _num(_at(gbp.get("total"), k))
            rv = (gbp.get("reviews") or {})
            new_rev = _num(_at(rv.get("n"), k))
            if not spend:
                verdict = "no_ads"
            elif not rs or not sales_ly or lift is None:
                verdict = "no_ly"
            elif lift >= LIFT:
                verdict = "working"
            elif lift <= -LIFT:
                verdict = "not_working"
            else:
                verdict = "neutral"

            def per(x, y, m=1):
                return round(x / y * m, 2) if x is not None and y else None
            rows.append({"ym": ym, "sales": round(sales, 0) if sales else None, "sales_ly": round(sales_ly, 0) if sales_ly else None,
                         "growth": round(g, 1) if g is not None else None, "group_growth": round(gg, 1) if gg is not None else None,
                         "lift": round(lift, 1) if lift is not None else None, "spend_eur": round(spend_eur, 2) if spend_eur is not None else None,
                         "spend": round(spend, 0) if spend is not None else None, "mkt_pct": per(spend, sales, 100),
                         "checks": checks or None, "checks_ly": checks_ly or None, "guests": guests or None,
                         "checks_growth": round((checks / checks_ly - 1) * 100, 1) if checks and checks_ly else None,
                         "cost_guest": per(spend, guests), "cost_check": per(spend, checks),
                         "clicks": clicks, "res": res, "dirs": dirs, "cpc": per(spend, clicks), "cost_res": per(spend, res),
                         "cost_dir": per(spend, dirs), "res_rate": per(res, clicks, 100),
                         "mer": per(sales, spend), "est_roas": per(est, spend_eur), "est": est,
                         "inter": inter, "inter_100": per(inter, checks, 100), "reviews": new_rev, "reviews_1000": per(new_rev, checks, 1000),
                         "complete": complete(rs, ym) and complete(rs, ly), "verdict": verdict})
        out.append({"id": e["id"], "kind": e["kind"], "name": e["name"], "parent": e["parent"], "refs": rs, "months": rows})
    with _lock:
        job = dict(_job)
    return {"available": True, "months": months, "entities": out, "rate": rate, "rate_source": rate_src, "lift": LIFT,
            "unmatched": unmatched, "reading": {"running": job["running"], "done": job["done"], "total": job["total"],
                                                "missing": len(missing)},
            "definitions": DEFINITIONS}


DEFINITIONS = {
    "mkt_pct": "Marketing % of sales = ad spend (Meta + Google, nl360) in lek ÷ Simphony net sales of the month.",
    "lift": "Sales lift = the sales growth against the same month last year − the growth of the rest of RHG in that "
            "month, in points: growth the market alone does not explain. Working at +5 points or more with ad spend; "
            "not working at −5 or less; neutral in between.",
    "cost_guest": "Cost per guest = ad spend ÷ Simphony guests (covers entered at the POS); cost per check the same with "
                  "checks.",
    "funnel": "The ads funnel: clicks → reservations (SevenRooms venues) and direction requests (Google) → checks at the "
              "tills: cost per click, per reservation, per direction request; reservations per 100 clicks.",
    "mer": "Sales per lek of ads (MER) = Simphony net sales ÷ ad spend. nl360's estimated return = its estimated ads "
           "revenue ÷ ad spend: an estimate from ad results, not tracked sales.",
    "gbp": "Google listing actions per 100 checks and new Google reviews per 1,000 checks: how the free listing turns "
           "into visits and reviews.",
    "sources": "Simphony: net sales, checks and guests of the restaurants the brand stands for (its units' tills, else "
               "the restaurants of the registry with its brand). nl360: everything else. Euros to lek at the group rate.",
}
