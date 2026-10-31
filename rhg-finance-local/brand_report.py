"""Brand performance email: weekly and monthly, one email per brand, for finance and operations.

Each brand (SALT, LIFT, OPA, VESPER, ... from the restaurant registry in Settings) gets its own email,
sent to the brand's own addresses with finance and operations in Cc:

* weekly, for the week before (Monday to Sunday): sales of every location (Simphony, excluding VAT)
  against the budget for those days, the week before and the same weekdays last year; checks, guests and
  the average check; sales by day against last year; the month to date against budget and last year,
  and the costs posted so far this month (Business Central) against budget and last year;
* monthly, for the month before, twice: on the first working day (provisional: sales are final, the
  costs are what is posted by then) and on the day set in Settings (final, once payroll and stock usage
  are posted): the P&L of the brand (sales, food and beverage, labour, prime cost, other costs, EBITDA)
  against budget and last year, the month and the year to date, and every location.

The budget is the G/L budget by department (the Executive dashboard's cube); a week takes the days of
it that fall in each month. Simphony sales are compared excluding VAT (net - tax collected), as the
budget is. Business Central is only read; the email goes through the Purchasing mail server.
"""
import html
import json
import threading
import time
import traceback
import uuid
from calendar import monthrange
from datetime import date, datetime, timedelta

import arap_notify
import config
import executive
import finloc
import locintel
import recipients
import simphony
import store

STATE_FILE = store.DATA_DIR / "brand_report.json"
_state_lock, _run_lock = threading.Lock(), threading.Lock()
WAIT = 40 * 60
KINDS = {"weekly": "Weekly", "monthly": "Monthly · provisional", "final": "Monthly · final"}


def _m(v):
    if v is None:
        return "–"
    a = abs(v)
    s = f"{a / 1e6:,.2f} M" if a >= 1e6 else f"{a / 1e3:,.0f} k" if a >= 1e4 else f"{a:,.0f}"
    return ("−" if v < 0 else "") + s


def _pct(a, b):
    return (a - b) / abs(b) * 100 if a is not None and b else None


def _p(v):
    return "–" if v is None else f"{v:+.1f}%"


def _days(a, b):
    out, d = [], a
    while d <= b:
        out.append(d)
        d += timedelta(days=1)
    return out


def last_week(today=None):
    today = today or date.today()
    monday = today - timedelta(days=today.weekday())
    return monday - timedelta(days=7), monday - timedelta(days=1)


def last_month(today=None):
    today = today or date.today()
    b = today.replace(day=1) - timedelta(days=1)
    return b.replace(day=1), b


def first_working_day(y, m):
    d = date(y, m, 1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


# ---------------------------------------------------------------- settings and brands
def settings():
    return store.get_settings().get("brand_report", {})


def brands(s=None):
    """{brand: [registry rows]} in the registry's order."""
    out = {}
    for r in locintel.registry(s):
        b = (r.get("brand") or r.get("name") or r["ref"]).strip().upper()
        out.setdefault(b, []).append(r)
    return out


def recipients_of(brand, s=None, rows=None):
    """The brand's own addresses in To; in Cc the people of its companies in the roles set for this
    report (Settings → Email recipients by company: finance and operations by default), else the Cc here."""
    s = s or settings()
    to = []
    for line in (s.get("recipients") or "").splitlines():
        name, _, emails = line.partition(":")
        if name.strip().upper() == brand:
            to = arap_notify.parse_emails(emails)[0]
    cc = [c for c in arap_notify.parse_emails(s.get("cc"))[0] if c not in to]
    if brand and to:
        companies = sorted({r.get("company") for r in (rows if rows is not None else brands().get(brand, [])) if r.get("company")})
        rec = recipients.resolve("brand_performance", companies, to, cc, promote_cc=False)
        to = list(dict.fromkeys(to + rec["to"]))
        cc = [c for c in rec["cc"] if c not in to]
    return to, cc


# ---------------------------------------------------------------- reading
def _wait(fn, what):
    t0 = time.time()
    while True:
        r = fn()
        if not r.get("pending"):
            return r
        if time.time() - t0 > WAIT:
            raise RuntimeError(f"{what} was still being read after {WAIT // 60} minutes.")
        time.sleep(10)


def _simphony_days(refs, days, latest_fn, parallel_fn):
    """{ref: {iso day: figures}} for the days asked (Simphony keeps them on disk once final)."""
    if not refs or not simphony.info().get("configured"):
        return {}
    latest = {r: (v if not e else None) for r, (v, e) in parallel_fn({r: (lambda x=r: latest_fn(x)) for r in refs}).items()}
    jobs = {}
    for ref in refs:
        for d in days:
            iso = d.isoformat()
            if latest.get(ref) and iso > latest[ref]:
                continue
            jobs[(ref, iso)] = (lambda r=ref, dd=iso, lt=latest.get(ref): simphony.daily_totals(r, dd, lt))
    res = parallel_fn(jobs, workers=4)
    simphony.flush_days()
    out = {}
    for (ref, iso), (v, e) in res.items():
        if v and not e:
            out.setdefault(ref, {})[iso] = simphony.day_figures(v.get("rvcs") or [])
    return out


def _sales(figs, days):
    """Sales of a set of days: net with VAT, ex VAT, checks, guests."""
    t = {"net": 0.0, "ex": 0.0, "checks": 0, "guests": 0, "days": 0}
    for d in days:
        f = figs.get(d.isoformat())
        if not f:
            continue
        t["net"] += f["net"]
        t["ex"] += f["net"] - (f.get("tax") or 0)
        t["checks"] += f["checks"]
        t["guests"] += f["guests"]
        t["days"] += 1
    return t


def _month_keys(a, b):
    out, d = [], a.replace(day=1)
    while d <= b:
        out.append(d.strftime("%Y-%m"))
        d = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return out


def _budget_days(cube_of, dept, a, b):
    """The budget of the days a..b: each month's budget times the share of its days inside.
    cube_of(year) gives the P&L cube of that year (a week can start in December)."""
    if not dept:
        return None
    total, n = None, 0
    for k in _month_keys(a, b):
        y, m = int(k[:4]), int(k[5:7])
        cube = cube_of(y) or {}
        bud, acc = cube.get("budget") or {}, cube.get("accounts") or {}
        if not bud.get("available"):
            continue
        ma, mb = date(y, m, 1), date(y, m, monthrange(y, m)[1])
        share = ((min(b, mb) - max(a, ma)).days + 1) / monthrange(y, m)[1]
        t = finloc._agg(bud["cells"], dept, [k], acc, {k: share})
        if t["n"]:
            n += 1
            total = t if total is None else {x: total[x] + t[x] for x in ("sales", "food", "labor", "opex", "ebitda", "prime")}
    return total


def _cube(client, company, year, currency_of):
    return _wait(lambda: executive.request(client, company, year, currency_of(company)), f"The P&L of {company}")


# ---------------------------------------------------------------- the figures of one brand
def build(kind, brand, client, latest_fn, parallel_fn, currency_of, today=None):
    today = today or date.today()
    rows = brands().get(brand)
    if not rows:
        raise RuntimeError(f"{brand} is not in the restaurant registry (Settings → Location intelligence).")
    weekly = kind == "weekly"
    a, b = last_week(today) if weekly else last_month(today)
    if weekly:
        prev = (a - timedelta(days=7), b - timedelta(days=7))
        ly = (a - timedelta(days=364), b - timedelta(days=364))            # the same weekdays last year
    else:
        prev = None
        try:
            ly = (a.replace(year=a.year - 1), b.replace(year=b.year - 1, day=monthrange(b.year - 1, b.month)[1]))
        except ValueError:
            ly = (a.replace(year=a.year - 1), date(b.year - 1, b.month, 28))
    th = finloc.thresholds()
    errors = []

    # ---- Simphony: the period, the week before, last year (and the month to date for a week)
    m_from = b.replace(day=1)
    try:
        mly = (m_from.replace(year=m_from.year - 1), b.replace(year=b.year - 1))
    except ValueError:
        mly = (m_from.replace(year=m_from.year - 1), date(b.year - 1, b.month, 28))
    need = set(_days(*ly)) | set(_days(a, b))
    if prev:
        need |= set(_days(*prev))
    if weekly:
        need |= set(_days(m_from, b)) | set(_days(*mly))
    refs = [r["ref"] for r in rows]
    try:
        sim = _simphony_days(refs, sorted(need), latest_fn, parallel_fn)
    except Exception as exc:                                        # noqa: BLE001
        traceback.print_exc()
        sim, errors = {}, errors + [f"Simphony: {str(exc)[:200]}"]

    # ---- Business Central: the cube of each company in the brand (and of the year before when a
    # week starts in December)
    cubes, years = {}, {}
    for company in sorted({r.get("company") for r in rows if r.get("company")}):
        try:
            cubes[company] = _cube(client, company, b.year, currency_of)
            years[(company, b.year)] = cubes[company]
            if a.year != b.year:
                years[(company, a.year)] = _cube(client, company, a.year, currency_of)
        except Exception as exc:                                    # noqa: BLE001
            traceback.print_exc()
            errors.append(f"{company}: {str(exc)[:200]}")

    def cell(company, dept, keys, kind_="actual", scale=None):
        cube = cubes.get(company)
        if not cube or not dept:
            return None
        src = cube.get("actual") if kind_ == "actual" else ((cube.get("budget") or {}).get("cells")
                                                            if (cube.get("budget") or {}).get("available") else None)
        if not src:
            return None
        t = finloc._agg(src, dept, keys, cube.get("accounts") or {}, scale)
        return t if t["n"] else None

    mk = b.strftime("%Y-%m")
    cost_to = None
    locs = []
    for r in rows:
        company, dept = r.get("company") or "", r.get("dept") or r.get("bc") or ""
        cur = currency_of(company) if company else (r.get("currency") or "ALL")
        cur = r.get("currency") or cur
        f = sim.get(r["ref"], {})
        L = {"ref": r["ref"], "name": r["name"], "company": company, "dept": dept, "currency": cur,
             "seasonal": r.get("seasonal"), "sales": _sales(f, _days(a, b)), "ly": _sales(f, _days(*ly)),
             "prev": _sales(f, _days(*prev)) if prev else None}
        cube = cubes.get(company)
        cube_of = (lambda y, c=company: years.get((c, y)))
        if weekly:
            L["budget"] = _budget_days(cube_of, dept, a, b) if cube else None
            L["mtd"] = {"sales": _sales(f, _days(m_from, b)), "ly": _sales(f, _days(*mly)),
                        "budget": _budget_days(cube_of, dept, m_from, b) if cube else None}
            # the costs posted so far in the month of the week's last day: the cube's running month runs
            # to yesterday, so its budget and last year are cut at the same day
            if cube:
                run = cube.get("running") or {}
                if run.get("month") == mk:
                    to_ = date.fromisoformat(run["to"])
                    L["mtd"]["bc_ly"] = cell(company, dept, [run.get("ly")])
                else:
                    to_ = date(b.year, b.month, monthrange(b.year, b.month)[1])
                    L["mtd"]["bc_ly"] = cell(company, dept, [f"{b.year - 1}-{b.month:02d}"])
                L["mtd"]["bc"] = cell(company, dept, [mk])
                L["mtd"]["bc_budget"] = _budget_days(cube_of, dept, m_from, to_)
                cost_to = max(cost_to or to_, to_)
        else:
            ytd = [f"{b.year}-{m:02d}" for m in range(1, b.month + 1)]
            ytd_ly = [f"{b.year - 1}-{m:02d}" for m in range(1, b.month + 1)]
            L["budget"] = _budget_days(cube_of, dept, a, b) if cube else None
            L["pl"] = {"cur": cell(company, dept, [mk]), "ly": cell(company, dept, [f"{b.year - 1}-{b.month:02d}"]),
                       "bud": cell(company, dept, [mk], "budget"), "ytd": cell(company, dept, ytd),
                       "ytd_ly": cell(company, dept, ytd_ly), "ytd_bud": cell(company, dept, ytd, "budget")}
            L["ytd_sales"] = None
        active = L["sales"]["ex"] or L["ly"]["ex"] or (L.get("pl") or {}).get("cur")
        if active:
            locs.append(L)

    # ---- totals by currency (a brand can run in two: SALT Tirana in lek, SALT Prishtina in euro)
    totals = {}
    for L in locs:
        t = totals.setdefault(L["currency"], {"sales": 0.0, "ly": 0.0, "prev": 0.0, "budget": 0.0, "has_budget": False,
                                              "b_sales": 0.0, "b_locations": 0,
                                              "checks": 0, "guests": 0, "ly_checks": 0, "ly_guests": 0, "net": 0.0,
                                              "ly_net": 0.0, "locations": 0, "pl": {}, "mtd": {}})
        t["locations"] += 1
        t["sales"] += L["sales"]["ex"]
        t["net"] += L["sales"]["net"]
        t["ly"] += L["ly"]["ex"]
        t["ly_net"] += L["ly"]["net"]
        t["checks"] += L["sales"]["checks"]
        t["guests"] += L["sales"]["guests"]
        t["ly_checks"] += L["ly"]["checks"]
        t["ly_guests"] += L["ly"]["guests"]
        if L.get("prev"):
            t["prev"] += L["prev"]["ex"]
        if L.get("budget") and L["budget"]["sales"]:
            # against the budget only the locations that have one (a seasonal or new place may not)
            t["budget"] += L["budget"]["sales"]
            t["b_sales"] += L["sales"]["ex"]
            t["b_locations"] += 1
            t["has_budget"] = True
        if weekly:
            mt = L["mtd"]
            mb = (mt["budget"] or {}).get("sales") or 0
            for k_, v in (("sales", mt["sales"]["ex"]), ("ly", mt["ly"]["ex"]), ("budget", mb),
                          ("b_sales", mt["sales"]["ex"] if mb else 0)):
                t["mtd"][k_] = t["mtd"].get(k_, 0.0) + v
            for k_ in ("bc", "bc_ly"):
                if mt.get(k_):
                    for x in ("sales", "food", "labor", "opex"):
                        t["mtd"].setdefault(k_, {}).setdefault(x, 0.0)
                        t["mtd"][k_][x] += mt[k_][x]
            if mt.get("bc_budget"):
                for x in ("food", "labor", "opex"):
                    t["mtd"].setdefault("bc_bud", {}).setdefault(x, 0.0)
                    t["mtd"]["bc_bud"][x] += mt["bc_budget"][x]
        else:
            for k_, v in (L.get("pl") or {}).items():
                if not v:
                    continue
                agg = t["pl"].setdefault(k_, dict.fromkeys(("sales", "food", "labor", "opex", "prime", "ebitda"), 0.0))
                for x in agg:
                    agg[x] += v.get(x) or 0
    main = max(totals, key=lambda c: (totals[c]["sales"], totals[c]["locations"])) if totals else None

    # ---- by day of the week, the main currency
    by_day = []
    if weekly:
        for i, d in enumerate(_days(a, b)):
            ld = d - timedelta(days=364)
            cur_ = sum(_sales(sim.get(L["ref"], {}), [d])["ex"] for L in locs if L["currency"] == main)
            ly_ = sum(_sales(sim.get(L["ref"], {}), [ld])["ex"] for L in locs if L["currency"] == main)
            by_day.append([d.isoformat(), cur_, ly_])

    # ---- highlights, in sentences
    hl = []
    T = totals.get(main) or {}
    if T:
        vb, vl = _pct(T["b_sales"], T["budget"]) if T["has_budget"] else None, _pct(T["sales"], T["ly"])
        part = (f" for the {T['b_locations']} locations that have one" if T["has_budget"] and T["b_locations"] < T["locations"] else "")
        hl.append(("good" if (vb or 0) >= 0 else "bad",
                   f"Sales {_m(T['sales'])} {main} excluding VAT: "
                   + (f"{_p(vb)} against budget ({_m(T['budget'])}{part}), " if vb is not None else "no budget, ")
                   + f"{_p(vl)} against last year" + (f", {_p(_pct(T['sales'], T['prev']))} against the week before" if weekly else "") + "."))
        if T["checks"] and T["ly_checks"]:
            ac, acl = T["net"] / T["checks"], T["ly_net"] / T["ly_checks"]
            hl.append(("good" if T["guests"] >= T["ly_guests"] else "bad",
                       f"Guests {T['guests']:,} ({_p(_pct(T['guests'], T['ly_guests']))} vs last year), checks {T['checks']:,}, "
                       f"average check {_m(ac)} ({_p(_pct(ac, acl))})."))
        with_b = [L for L in locs if L.get("budget") and L["budget"]["sales"] and L["currency"] == main]
        if len(with_b) > 1:
            gaps = sorted(with_b, key=lambda L: _pct(L["sales"]["ex"], L["budget"]["sales"]))
            lo, hi = gaps[0], gaps[-1]
            hl.append(("bad" if _pct(lo["sales"]["ex"], lo["budget"]["sales"]) < 0 else "good",
                       f"Furthest under budget: {lo['name']} {_p(_pct(lo['sales']['ex'], lo['budget']['sales']))}; "
                       f"best: {hi['name']} {_p(_pct(hi['sales']['ex'], hi['budget']['sales']))}."))
        if not weekly and T["pl"].get("cur"):
            c, bu = T["pl"]["cur"], T["pl"].get("bud")
            s_ = c["sales"] or 0
            hl.append(("bad" if bu and c["ebitda"] < bu["ebitda"] else "good",
                       (f"EBITDA {_m(c['ebitda'])} ({c['ebitda'] / s_ * 100:.1f}% of sales)" if s_ else f"EBITDA {_m(c['ebitda'])}")
                       + (f", budget {_m(bu['ebitda'])} ({_p(_pct(c['ebitda'], bu['ebitda']))})" if bu else "") + "."))
            if s_:
                fp, lp = c["food"] / s_ * 100, c["labor"] / s_ * 100
                hl.append(("bad" if fp + lp > th["prime"] else "good",
                           f"Food and beverage {fp:.1f}%, labour {lp:.1f}%, prime cost {fp + lp:.1f}% of sales "
                           f"(limit {th['prime']:.0f}%)."))
        if weekly and (T["mtd"].get("bc")):
            hl.append(("info", "Costs posted so far this month are partial: payroll and stock usage are posted at "
                               "month end, so the weekly email shows them only as a running indication."))
    other = [c for c in totals if c != main]
    return {"kind": kind, "brand": brand, "period": {"from": a.isoformat(), "to": b.isoformat()},
            "prev": {"from": prev[0].isoformat(), "to": prev[1].isoformat()} if prev else None,
            "ly": {"from": ly[0].isoformat(), "to": ly[1].isoformat()}, "mtd_from": m_from.isoformat(),
            "cost_to": cost_to.isoformat() if cost_to else None,
            "locations": locs, "totals": totals, "main": main, "other": other, "by_day": by_day,
            "highlights": hl, "errors": errors, "thresholds": th,
            "built_at": datetime.now().isoformat(timespec="seconds")}


# ---------------------------------------------------------------- the email
def subject(r, test=False):
    a, b = date.fromisoformat(r["period"]["from"]), date.fromisoformat(r["period"]["to"])
    when = f"week {a:%d %b} – {b:%d %b %Y}" if r["kind"] == "weekly" else f"{a:%B %Y}"
    T = r["totals"].get(r["main"]) or {}
    vb = _pct(T.get("b_sales"), T.get("budget")) if T.get("has_budget") else None
    tail = f" · sales {_p(vb)} vs budget" if vb is not None else ""
    return (("[Test] " if test else "") + f"[RHG] {r['brand']} · {KINDS[r['kind']]} performance · {when}{tail}")


def render(r):
    e = html.escape
    navy, primary, ink, muted, line, sky = "#08303f", "#0c475e", "#0e2733", "#5c7482", "#d9dcdd", "#e4f3f9"
    red, amber, green = "#c0392b", "#b26a00", "#0e8a57"
    th = f"text-align:left;padding:6px 8px;background:{sky};color:{ink};font-size:11.5px;font-weight:600;white-space:nowrap"
    thn = th.replace("text-align:left", "text-align:right")
    td = f"padding:5px 8px;border-bottom:1px solid #f0f1f2;font-size:12px;color:{ink};vertical-align:top"
    tdn = td + ";text-align:right;white-space:nowrap"
    weekly = r["kind"] == "weekly"
    a, b = date.fromisoformat(r["period"]["from"]), date.fromisoformat(r["period"]["to"])
    main = r["main"]
    T = r["totals"].get(main) or {}

    def var(v, good_up=True):
        if v is None:
            return f"<span style='color:{muted}'>–</span>"
        c = green if (v >= 0) == good_up else red
        return f"<span style='color:{c}'>{_p(v)}</span>"

    def tile(label, value, sub):
        return (f"<td style='padding:10px 12px;background:#fff;border:1px solid {line};border-radius:10px;vertical-align:top;width:25%'>"
                f"<div style='font-size:11.5px;color:{muted}'>{e(label)}</div><div style='font-size:19px;font-weight:700;color:{ink};margin:3px 0'>{value}</div>"
                f"<div style='font-size:11px;color:{muted}'>{sub}</div></td>")

    def table(head, rows):
        hs = "".join(f"<th style='{thn if n else th}'>{e(h)}</th>" for h, n in head)
        return (f"<table role='presentation' cellspacing='0' cellpadding='0' style='width:100%;border-collapse:collapse;"
                f"border:1px solid {line}'><tr>{hs}</tr>{''.join(rows)}</table>")

    def h3(t, sub=""):
        return (f"<h3 style='font-size:14px;color:{ink};margin:22px 0 4px'>{e(t)}</h3>"
                + (f"<div style='font-size:12px;color:{muted};margin-bottom:6px'>{e(sub)}</div>" if sub else ""))
    tiles = ""
    if T:
        vb = _pct(T["b_sales"], T["budget"]) if T["has_budget"] else None
        ac = T["net"] / T["checks"] if T["checks"] else None
        acl = T["ly_net"] / T["ly_checks"] if T["ly_checks"] else None
        cells = [tile("Sales excl. VAT", _m(T["sales"]), f"budget {_m(T['budget']) if T['has_budget'] else '–'} · {var(vb)}"
                      + (f"<br>{T['b_locations']} of {T['locations']} locations budgeted" if T["has_budget"] and T["b_locations"] < T["locations"] else "")),
                 tile("Against last year", var(_pct(T["sales"], T["ly"])), f"last year {_m(T['ly'])}"),
                 tile("Guests · checks", f"{T['guests']:,}", f"{var(_pct(T['guests'], T['ly_guests']))} · {T['checks']:,} checks"),
                 tile("Average check", _m(ac), f"incl. VAT · {var(_pct(ac, acl))} vs last year")]
        if not weekly and T["pl"].get("cur"):
            c, bu = T["pl"]["cur"], T["pl"].get("bud")
            s_ = c["sales"] or 0
            cells[3] = tile("EBITDA", _m(c["ebitda"]), (f"{c['ebitda'] / s_ * 100:.1f}% of sales · " if s_ else "")
                            + (f"budget {_m(bu['ebitda'])} · {var(_pct(c['ebitda'], bu['ebitda']))}" if bu else "no budget"))
        tiles = ("<table role='presentation' cellspacing='6' cellpadding='0' style='width:100%;margin:0 -6px'><tr>"
                 + "".join(cells) + "</tr></table>")
    dot = {"good": green, "bad": red, "info": primary}
    hl = "".join(f"<tr><td style='padding:4px 0;width:14px;vertical-align:top'><span style='display:inline-block;width:8px;height:8px;"
                 f"border-radius:50%;background:{dot.get(k, primary)};margin-top:5px'></span></td><td style='padding:3px 0;font-size:13px;color:{ink}'>{e(t)}</td></tr>"
                 for k, t in r["highlights"])
    hl = f"<table role='presentation' cellspacing='0' cellpadding='0' style='width:100%'>{hl}</table>"

    # ---- by location
    loc_rows = []
    for L in sorted(r["locations"], key=lambda L: (L["currency"] != main, -L["sales"]["ex"])):
        s, bu = L["sales"], (L.get("budget") or {}).get("sales")
        avg = s["net"] / s["checks"] if s["checks"] else None
        cells = (f"<tr><td style='{td}'>{e(L['name'])}<div style='color:{muted};font-size:11px'>{e(L['currency'])}{' · ' + e(L['dept']) if L['dept'] else ''}</div></td>"
                 f"<td style='{tdn}'>{_m(s['ex'])}</td><td style='{tdn};color:{muted}'>{_m(bu) if bu else '–'}</td><td style='{tdn}'>{var(_pct(s['ex'], bu) if bu else None)}</td>"
                 f"<td style='{tdn};color:{muted}'>{_m(L['ly']['ex'])}</td><td style='{tdn}'>{var(_pct(s['ex'], L['ly']['ex']))}</td>")
        if weekly:
            cells += (f"<td style='{tdn}'>{var(_pct(s['ex'], L['prev']['ex']) if L.get('prev') else None)}</td>"
                      f"<td style='{tdn}'>{s['guests']:,}</td><td style='{tdn}'>{_m(avg)}</td></tr>")
        else:
            c, bp = (L.get("pl") or {}).get("cur") or {}, (L.get("pl") or {}).get("bud") or {}
            sb = c.get("sales") or 0
            cells += (f"<td style='{tdn}'>{c.get('food', 0) / sb * 100:.1f}%</td><td style='{tdn}'>{c.get('labor', 0) / sb * 100:.1f}%</td>"
                      if sb else f"<td style='{tdn}'>–</td><td style='{tdn}'>–</td>")
            cells += (f"<td style='{tdn};color:{red if (c.get('ebitda') or 0) < 0 else ink}'>{_m(c.get('ebitda')) if c else '–'}</td>"
                      f"<td style='{tdn}'>{var(_pct(c.get('ebitda'), bp.get('ebitda')) if c and bp else None)}</td></tr>")
        loc_rows.append(cells)
    head = [("Location", 0), ("Sales", 1), ("Budget", 1), ("vs budget", 1), ("Last year", 1), ("vs LY", 1)]
    head += [("vs week before", 1), ("Guests", 1), ("Avg check", 1)] if weekly else [("Food %", 1), ("Labour %", 1), ("EBITDA", 1), ("vs budget", 1)]
    blocks = h3("By location", "Sales excluding VAT from Simphony. " + ("Last year = the same weekdays 52 weeks before." if weekly else "Last year = the same month.")
               + (" Food, labour and EBITDA from the P&L posted in Business Central." if not weekly else "")) + table(head, loc_rows)
    if r["other"]:
        blocks += f"<div style='font-size:11px;color:{muted};margin-top:4px'>Totals above are in {e(main)}; " + \
            ", ".join(f"{e(c)}: sales {_m(r['totals'][c]['sales'])} ({_p(_pct(r['totals'][c]['sales'], r['totals'][c]['ly']))} vs LY)" for c in r["other"]) + ".</div>"

    if weekly:
        mx = max([1] + [max(x[1], x[2]) for x in r["by_day"]])
        day_rows = [f"<tr><td style='{td}'>{date.fromisoformat(d):%a %d %b}</td><td style='{tdn}'>{_m(c)}</td><td style='{tdn};color:{muted}'>{_m(l)}</td>"
                    f"<td style='{tdn}'>{var(_pct(c, l))}</td><td style='{td};width:45%'><div style='background:{primary};height:8px;border-radius:3px;width:{c / mx * 100:.0f}%'></div>"
                    f"<div style='background:#a0d8ea;height:5px;border-radius:3px;margin-top:2px;width:{l / mx * 100:.0f}%'></div></td></tr>" for d, c, l in r["by_day"]]
        blocks += h3("By day", f"This week (dark) against the same weekday last year (light), in {main}.") + \
            table([("Day", 0), ("Sales", 1), ("Last year", 1), ("vs LY", 1), ("", 0)], day_rows)
        mt = T.get("mtd") or {}
        if mt:
            mf = date.fromisoformat(r["mtd_from"])
            blocks += h3(f"Month to date · {mf:%d %b} – {b:%d %b}", f"Sales from Simphony; costs as posted in Business Central so far, against the budget for these days and the same dates last year.")
            rows = [f"<tr><td style='{td}'><b>Sales</b> (Simphony, excl. VAT)</td><td style='{tdn}'>{_m(mt.get('sales'))}</td><td style='{tdn};color:{muted}'>{_m(mt.get('budget'))}</td>"
                    f"<td style='{tdn}'>{var(_pct(mt.get('b_sales'), mt.get('budget')))}</td><td style='{tdn};color:{muted}'>{_m(mt.get('ly'))}</td><td style='{tdn}'>{var(_pct(mt.get('sales'), mt.get('ly')))}</td></tr>"]
            bc, bl, bb = mt.get("bc") or {}, mt.get("bc_ly") or {}, mt.get("bc_bud") or {}
            ct = f" to {date.fromisoformat(r['cost_to']):%d %b}" if r.get("cost_to") else ""
            for k_, lab in (("food", f"Food & beverage (posted{ct})"), ("labor", f"Labour (posted{ct})"), ("opex", f"Other costs (posted{ct})")):
                if k_ in bc and not round(bc[k_]):
                    rows.append(f"<tr><td style='{td}'>{lab}</td><td style='{tdn};color:{muted}'>not posted yet</td><td style='{tdn};color:{muted}'>{_m(bb.get(k_))}</td>"
                                f"<td style='{tdn}'>{var(None)}</td><td style='{tdn};color:{muted}'>{_m(bl.get(k_))}</td><td style='{tdn}'>{var(None)}</td></tr>")
                elif k_ in bc:
                    rows.append(f"<tr><td style='{td}'>{lab}</td><td style='{tdn}'>{_m(bc[k_])}</td><td style='{tdn};color:{muted}'>{_m(bb.get(k_))}</td>"
                                f"<td style='{tdn}'>{var(_pct(bc[k_], bb.get(k_)), good_up=False)}</td><td style='{tdn};color:{muted}'>{_m(bl.get(k_))}</td>"
                                f"<td style='{tdn}'>{var(_pct(bc[k_], bl.get(k_)), good_up=False)}</td></tr>")
            blocks += table([("", 0), ("Actual", 1), ("Budget", 1), ("vs budget", 1), ("Last year", 1), ("vs LY", 1)], rows)
    else:
        P = T.get("pl") or {}
        if P.get("cur"):
            def pl_row(k_, lab, cost):
                c, bu, ly_, y, yb, yl = (P.get(x) or {} for x in ("cur", "bud", "ly", "ytd", "ytd_bud", "ytd_ly"))
                s_ = c.get("sales") or 0
                bold = "font-weight:700;" if k_ in ("ebitda", "prime") else ""
                return (f"<tr><td style='{td};{bold}'>{lab}</td><td style='{tdn};{bold}'>{_m(c.get(k_))}</td>"
                        f"<td style='{tdn};color:{muted}'>{(c.get(k_) or 0) / s_ * 100:.1f}%</td>" if s_ else f"<tr><td style='{td}'>{lab}</td><td style='{tdn}'>{_m(c.get(k_))}</td><td style='{tdn}'>–</td>") + \
                    (f"<td style='{tdn};color:{muted}'>{_m(bu.get(k_)) if bu else '–'}</td><td style='{tdn}'>{var(_pct(c.get(k_), bu.get(k_)) if bu else None, not cost)}</td>"
                     f"<td style='{tdn};color:{muted}'>{_m(ly_.get(k_)) if ly_ else '–'}</td><td style='{tdn}'>{var(_pct(c.get(k_), ly_.get(k_)) if ly_ else None, not cost)}</td>"
                     f"<td style='{tdn}'>{_m(y.get(k_)) if y else '–'}</td><td style='{tdn}'>{var(_pct(y.get(k_), yb.get(k_)) if y and yb else None, not cost)}</td>"
                     f"<td style='{tdn}'>{var(_pct(y.get(k_), yl.get(k_)) if y and yl else None, not cost)}</td></tr>")
            rows = [pl_row("sales", "Sales", False), pl_row("food", "Food & beverage", True), pl_row("labor", "Labour", True),
                    pl_row("prime", "Prime cost", True), pl_row("opex", "Other costs", True), pl_row("ebitda", "EBITDA", False)]
            prov = r["kind"] == "monthly"
            blocks += h3(f"P&L · {a:%B %Y}" + (" · provisional" if prov else ""),
                         ("Costs as posted by today: payroll and stock usage may still come; the final report follows. " if prov else "")
                         + f"Business Central, departments of the brand in {main}.") + \
                table([("", 0), ("Month", 1), ("% sales", 1), ("Budget", 1), ("vs budget", 1), ("Last year", 1), ("vs LY", 1),
                       ("YTD", 1), ("YTD vs budget", 1), ("YTD vs LY", 1)], rows)
    errs = "".join(f"<div style='background:#fdf3e2;color:{amber};padding:7px 11px;border-radius:8px;font-size:12px;margin-bottom:8px'>{e(x)}</div>" for x in r["errors"])
    public = (config.PUBLIC_URL or "").rstrip("/")
    period = f"Week {a:%d %b} – {b:%d %b %Y}" if weekly else f"{a:%B %Y}"
    comp = (f"against the budget, the week before ({r['prev']['from']} → {r['prev']['to']}) and the same weekdays last year "
            f"({r['ly']['from']} → {r['ly']['to']})" if weekly else f"against the budget and {date.fromisoformat(r['ly']['from']):%B %Y}")
    body = f"""<!doctype html><html><head><meta charset="utf-8"></head><body style="margin:0;background:#ecebeb;font-family:Figtree,Segoe UI,Arial,sans-serif;color:{ink}">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#ecebeb"><tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:900px">
<tr><td style="background:{navy};color:#fff;padding:18px 22px;border-radius:12px 12px 0 0">
<div style="font-size:11px;letter-spacing:.12em;color:#a0d8ea">REVO HOSPITALITY GROUP · {e(r['brand'])}</div>
<div style="font-size:20px;font-weight:700;margin-top:4px">{e(KINDS[r['kind']])} performance · {e(r['brand'])}</div>
<div style="font-size:12px;color:#cfe3ea;margin-top:4px">{e(period)} · {len(r['locations'])} locations · {e(comp)}</div></td></tr>
<tr><td style="background:#fff;padding:20px 22px;border-radius:0 0 12px 12px">
{errs}{tiles}
<h3 style="font-size:14px;color:{ink};margin:18px 0 6px">Highlights</h3>{hl}
{blocks}
{f"<p style='margin:20px 0 0'><a href='{e(public)}/executive?tab=locations' style='background:{primary};color:#fff;text-decoration:none;padding:10px 18px;border-radius:8px;font-size:13px;font-weight:600;display:inline-block'>Open Location intelligence</a></p>" if public else ''}
<p style="color:{muted};font-size:11px;margin:22px 0 0;border-top:1px solid {line};padding-top:12px">Sent by RHG Control Towers from Simphony and Business Central; nothing was changed there. Recipients and schedule are in Finance → Settings → Brand performance email.</p>
</td></tr></table></td></tr></table></body></html>"""
    text = f"RHG · {r['brand']} · {KINDS[r['kind']]} performance · {period}\n\n" + "\n".join(f"- {t}" for _, t in r["highlights"])
    return body, text


# ---------------------------------------------------------------- history, sending, schedule
def _read_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_state(s):
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(s, indent=1, default=str), encoding="utf-8")
    tmp.replace(STATE_FILE)


def _log(entry):
    with _state_lock:
        s = _read_state()
        s["runs"] = (s.get("runs", []) + [entry])[-200:]
        _write_state(s)


def recent_runs(limit=20):
    with _state_lock:
        return list(reversed(_read_state().get("runs", [])))[:limit]


class Reporter:
    def __init__(self, client, latest_fn, parallel_fn, currency_of):
        self.client, self.latest_fn, self.parallel_fn, self.currency_of = client, latest_fn, parallel_fn, currency_of
        self._jobs, self._jobs_lock = {}, threading.Lock()

    def build(self, kind, brand):
        return build(kind, brand, self.client, self.latest_fn, self.parallel_fn, self.currency_of)

    def send(self, kind, brand, only_to=None, actor="scheduler", test=False, r=None):
        entry = {"ts": datetime.now().isoformat(timespec="seconds"), "kind": kind, "brand": brand, "actor": actor, "test": test}
        try:
            to, cc = (only_to, []) if only_to else recipients_of(brand)
            if not to:
                entry.update(status="skipped", detail="No address for this brand in Settings.")
            else:
                r = r or self.build(kind, brand)
                if not r["locations"]:
                    entry.update(status="skipped", detail="No sales and nothing posted in the period.")
                else:
                    body, text = render(r)
                    arap_notify.send_mail(to, cc, subject(r, test), body, text)
                    entry.update(status="sent", to=to, cc=cc, period=r["period"], detail=subject(r, test))
        except Exception as exc:                                     # noqa: BLE001
            traceback.print_exc()
            entry.update(status="failed", detail=str(exc)[:300])
        _log(entry)
        store.audit("email", f"brand performance · {kind} · {brand}", actor, {},
                    {k: entry.get(k) for k in ("status", "to", "cc", "test")}, entry.get("detail", ""))
        return entry

    def start_job(self, kind, brand, actor, only_to=None):
        with self._jobs_lock:
            for j in self._jobs.values():
                if j["actor"] == actor and j["status"] == "running":
                    return {k: v for k, v in j.items() if k != "html"}
            self._jobs = {k: v for k, v in list(self._jobs.items())[-20:]}
            job = {"id": uuid.uuid4().hex, "actor": actor, "kind": kind, "brand": brand, "to": only_to or [],
                   "status": "running", "detail": "", "html": None}
            self._jobs[job["id"]] = job

        def go():
            try:
                r = self.build(kind, brand)
                job["html"] = render(r)[0]
                if only_to:
                    res = self.send(kind, brand, only_to, actor, test=True, r=r)
                    if res["status"] != "sent":
                        raise RuntimeError(res.get("detail") or res["status"])
                    job["detail"] = "Sent to " + ", ".join(only_to)
                job["status"] = "done"
            except Exception as exc:                                 # noqa: BLE001
                traceback.print_exc()
                job.update(status="failed", detail=str(exc)[:300])
        threading.Thread(target=go, daemon=True, name="brand-report-job").start()
        return {k: v for k, v in job.items() if k != "html"}

    def job(self, job_id, actor):
        with self._jobs_lock:
            j = self._jobs.get(job_id)
            return j if j and j["actor"] == actor else None

    # ---- the schedule: weekly on the day set, monthly provisional on the first working day,
    # monthly final on the day set (moved to the next working day when it falls on a weekend)
    @staticmethod
    def _time(s):
        hh, _, mm = (s.get("time") or "09:00").partition(":")
        return int(hh or 9), int(mm or 0)

    @staticmethod
    def _final_day(s, y, m):
        try:
            d = max(2, min(28, int(float(s.get("final_day") or 10))))
        except (TypeError, ValueError):
            d = 10
        x = date(y, m, d)
        while x.weekday() >= 5:
            x += timedelta(days=1)
        return x

    def due(self, now):
        """[(kind, key)] due now."""
        s = settings()
        hh, mm = self._time(s)
        start = datetime(now.year, now.month, now.day, hh, mm)
        if not (start <= now <= start + timedelta(hours=10)):
            return []
        out = []
        try:
            wd = max(0, min(6, int(float(s.get("day") or 0))))
        except (TypeError, ValueError):
            wd = 0
        if s.get("weekly") and now.weekday() == wd:
            out.append(("weekly", "W" + last_week(now.date())[0].isoformat()))
        lm = last_month(now.date())[0].strftime("%Y-%m")
        if s.get("monthly") and now.date() == first_working_day(now.year, now.month):
            out.append(("monthly", "P" + lm))
        if s.get("monthly") and now.date() == self._final_day(s, now.year, now.month):
            out.append(("final", "F" + lm))
        with _state_lock:
            st = _read_state()
        done, tries = st.get("done", {}), st.get("tries", {})
        return [(k, key) for k, key in out if not done.get(key)
                and not (tries.get(key, {}).get("count", 0) >= 3 or time.time() - tries.get(key, {}).get("at", 0) < 1800)]

    def run(self, kind, key):
        with _run_lock:
            results = [self.send(kind, b) for b in brands()]
            with _state_lock:
                st = _read_state()
                if all(x["status"] != "failed" for x in results):
                    st.setdefault("done", {})[key] = datetime.now().isoformat(timespec="seconds")
                    st.get("tries", {}).pop(key, None)
                else:
                    t = st.setdefault("tries", {}).setdefault(key, {"count": 0})
                    t.update(count=t["count"] + 1, at=time.time())
                    if t["count"] >= 3:
                        st.setdefault("done", {})[key] = "gave up"
                st["done"] = dict(list(st.get("done", {}).items())[-60:])
                _write_state(st)
            return results

    def next_runs(self, now=None):
        s = settings()
        now = now or datetime.now()
        hh, mm = self._time(s)
        out = {}
        try:
            wd = max(0, min(6, int(float(s.get("day") or 0))))
        except (TypeError, ValueError):
            wd = 0
        if s.get("weekly"):
            d = now.date() + timedelta(days=(wd - now.weekday()) % 7)
            t = datetime(d.year, d.month, d.day, hh, mm)
            if t + timedelta(hours=10) < now:
                t += timedelta(days=7)
            out["weekly"] = t.isoformat(timespec="minutes")
        if s.get("monthly"):
            for k in range(3):
                y, m = now.year + (now.month - 1 + k) // 12, (now.month - 1 + k) % 12 + 1
                p = datetime.combine(first_working_day(y, m), datetime.min.time()).replace(hour=hh, minute=mm)
                f = datetime.combine(self._final_day(s, y, m), datetime.min.time()).replace(hour=hh, minute=mm)
                if "monthly" not in out and p + timedelta(hours=10) >= now:
                    out["monthly"] = p.isoformat(timespec="minutes")
                if "final" not in out and f + timedelta(hours=10) >= now:
                    out["final"] = f.isoformat(timespec="minutes")
        return out

    def loop(self):
        while True:
            try:
                for kind, key in self.due(datetime.now()):
                    self.run(kind, key)
            except Exception:                                        # noqa: BLE001
                traceback.print_exc()
            time.sleep(60)

    def start(self):
        threading.Thread(target=self.loop, daemon=True, name="brand-report").start()
