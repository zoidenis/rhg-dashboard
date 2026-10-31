"""Monthly P&L memo: on the 20th (Finance → Settings → P&L memo), the P&L of the month before for every
company, against the budget and the same month last year, with a memo in sentences.

For each company of the restaurant registry (or the companies set in the card):
  * the memo: sales, food and beverage, labour, other costs and EBITDA of the month against budget and
    last year, the year to date and the forecast of the year, the restaurants ahead of and behind budget,
    the largest account variances against budget, and a comment from Finance written in the card;
  * the P&L of the company (sales, other income, food & beverage, labour, prime cost, other costs, EBITDA)
    for the month and the year to date, against budget and last year, with the shares of sales;
  * every department (restaurants, then head office and central kitchen; what is on no department apart):
    sales, food %, labour %, EBITDA and its margin, against budget and last year;
  * the accounts that moved EBITDA most against the budget.
The figures are the Executive dashboard's P&L cube (G/L entries by department, the G/L budget), the month
being the latest closed one (payroll and food cost posted) up to the month before. One email per set of
readers: whoever is set for several companies (a group CFO in All companies) gets them in one email; a
restaurant with people of its own in Email recipients gets its own P&L. Read-only.
"""
import html
import json
import threading
import time
import traceback
import uuid
from datetime import date, datetime, timedelta

import arap_notify
import brand_report
import config
import finloc
import locintel
import recipients
import store

STATE_FILE = store.DATA_DIR / "pl_memo.json"
_state_lock, _run_lock = threading.Lock(), threading.Lock()
_m, _pct, _p = brand_report._m, brand_report._pct, brand_report._p
LINES = [("sales", "Sales", False), ("other_income", "Other income", False), ("food", "Food & beverage", True),
         ("labor", "Labour", True), ("prime", "Prime cost (food + labour)", True), ("opex", "Other operating costs", True),
         ("ebitda", "EBITDA", False)]
COST = {"food", "labor", "prime", "opex", "da", "labor_kitchen", "labor_service", "labor_management", "labor_other"}


def settings():
    return store.get_settings().get("pl_memo", {})


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


def companies(s=None):
    """The companies of the memo: those set in the card, else those of the restaurant registry."""
    s = s or settings()
    chosen = [c.strip() for c in str(s.get("companies") or "").split(",") if c.strip()]
    if chosen:
        return chosen
    seen, out = set(), []
    for r in locintel.registry():
        c = (r.get("company") or "").strip()
        if c and c.upper() not in seen:
            seen.add(c.upper())
            out.append(c)
    return out or [config.BC_COMPANY]


def report_month(today=None):
    """The month the memo is about: the month before."""
    a, _ = brand_report.last_month(today or date.today())
    return a.strftime("%Y-%m")


def send_day(y, m, s=None):
    """The day of month y-m the memo goes out: the day set (20 by default), moved to Monday when it is a
    Saturday or a Sunday and "working days only" is on."""
    s = s or settings()
    try:
        d = max(1, min(28, int(float(s.get("day") or 20))))
    except (TypeError, ValueError):
        d = 20
    when = date(y, m, d)
    if s.get("working_day", True):
        while when.weekday() >= 5:
            when += timedelta(days=1)
    return when


# ---------------------------------------------------------------- the figures of one company
def _variances(cube, dept, month, n_bad=6, n_good=4):
    """[(account, name, bucket, actual, budget or LY, effect on EBITDA)] of the month: the accounts that moved
    EBITDA most against the budget (against last year when there is no budget)."""
    acc = cube.get("accounts") or {}
    act = ((cube.get("actual") or {}).get(dept) or {}).get(month) or {}
    bud = cube.get("budget") or {}
    ref = (((bud.get("cells") or {}).get(dept) or {}).get(month) or {}) if bud.get("available") else {}
    against = "budget"
    if not ref:
        ref = ((cube.get("actual") or {}).get(dept) or {}).get(finloc._shift(month, -12)) or {}
        against = "last year"
    rows = []
    for no in set(act) | set(ref):
        name, b = (acc.get(no) or ["", "sales" if str(no).startswith("70") else "opex"])[:2]
        if b in ("da", "below"):
            continue
        a, r = act.get(no) or 0.0, ref.get(no) or 0.0
        eff = (a - r) * (-1 if b in COST else 1)
        if abs(eff) >= 1:
            rows.append([str(no), name or str(no), b, round(a), round(r), round(eff)])
    bad = sorted([x for x in rows if x[5] < 0], key=lambda x: x[5])[:n_bad]
    good = sorted([x for x in rows if x[5] > 0], key=lambda x: -x[5])[:n_good]
    return {"against": against, "bad": bad, "good": good}


def build_company(client, company, month, currency_of):
    cur = currency_of(company)
    cube = brand_report._cube(client, company, int(month[:4]), currency_of)
    pl, meta = finloc._pl(cube, month)
    if not pl or "*" not in pl:
        raise RuntimeError(f"{company}: no closed month up to {month} in the P&L (payroll or food cost not posted).")
    used = meta["month"]
    reg = [r for r in locintel.registry() if (r.get("company") or "").upper() == company.upper()]
    by_dept = {}
    for r in reg:
        for k in (r.get("dept"), r.get("bc")):
            if k:
                by_dept.setdefault(k.upper(), r)
    names = {d["code"]: d.get("name") or d["code"] for d in cube.get("departments") or []}
    depts = []
    for code, P in pl.items():
        if code == "*":
            continue
        r = by_dept.get(code.upper())
        depts.append({"code": code, "name": (r or {}).get("name") or names.get(code) or code,
                      "brand": (r or {}).get("brand") or "", "group": "restaurant" if r else "support",
                      "cur": P["cur"], "bud": P["bud"], "ly": P["ly"], "ytd": P["ytd"], "ytd_bud": P["ytd_bud"],
                      "ytd_ly": P["ytd_ly"]})
    # what is on no department: the company less its departments
    T = pl["*"]
    rest = {}
    for part in ("cur", "ytd"):
        tot = T[part]
        left = {k: tot[k] - sum((d[part] or {}).get(k, 0) for d in depts) for k in ("sales", "other_income", "food", "labor", "opex", "ebitda")}
        rest[part] = left
    if any(abs(v) >= 1 for v in rest["cur"].values()):
        c, s = rest["cur"], rest["cur"]["sales"]
        c.update({"food_pct": None, "labor_pct": None, "ebitda_pct": None})
        depts.append({"code": "", "name": "Not on a department", "brand": "", "group": "unassigned",
                      "cur": c, "bud": None, "ly": None, "ytd": rest["ytd"], "ytd_bud": None, "ytd_ly": None})
    depts.sort(key=lambda d: ({"restaurant": 0, "support": 1, "unassigned": 2}[d["group"]], d["brand"], -(d["cur"].get("sales") or 0)))
    var = _variances(cube, "*", used)
    return {"company": company, "currency": cur, "month": used, "asked": month, "meta": meta,
            "pl": {k: T[k] for k in ("cur", "bud", "ly", "ytd", "ytd_bud", "ytd_ly", "fy_bud", "forecast")},
            "depts": depts, "variances": var, "memo": memo(company, cur, used, month, meta, T, depts, var)}


def build_dept(client, company, code, month, currency_of):
    """The P&L memo of one department (a restaurant with people of its own)."""
    cur = currency_of(company)
    cube = brand_report._cube(client, company, int(month[:4]), currency_of)
    pl, meta = finloc._pl(cube, month)
    P = (pl or {}).get(code)
    if not P:
        raise RuntimeError(f"{company} · {code}: nothing posted on this department up to {month}.")
    r = next((x for x in locintel.registry() if (x.get("company") or "").upper() == company.upper()
              and code.upper() in {(x.get("dept") or "").upper(), (x.get("bc") or "").upper()}), None)
    name = (r or {}).get("name") or code
    var = _variances(cube, code, meta["month"])
    T = {k: P[k] for k in ("cur", "bud", "ly", "ytd", "ytd_bud", "ytd_ly", "fy_bud", "forecast")}
    return {"company": f"{name}", "legal": company, "currency": cur, "month": meta["month"], "asked": month,
            "meta": meta, "pl": T, "depts": [], "variances": var,
            "memo": memo(name, cur, meta["month"], month, meta, T, [], var)}


# ---------------------------------------------------------------- the memo, in sentences
def _mon(k):
    return date(int(k[:4]), int(k[5:7]), 1).strftime("%B %Y")


def memo(who, cur, used, asked, meta, T, depts, var):
    c, b, ly = T["cur"], T.get("bud"), T.get("ly")
    y, yb, yl = T["ytd"], T.get("ytd_bud"), T.get("ytd_ly")
    out = []
    if used != asked:
        out.append(("warn", f"{_mon(asked)} is not closed yet in Business Central (payroll or food cost missing): "
                            f"the figures are those of {_mon(used)}, the latest closed month."))
    s = c["sales"]

    def vs(a, r, cost=False):
        p = _pct(a, r)
        return None if p is None else p

    def bit(a, r, label, cost=False):
        p = vs(a, r)
        if p is None:
            return None
        word = ("over" if p > 0 else "under") if cost else ("ahead of" if p > 0 else "behind")
        return f"{abs(p):.1f}% {word} {label}"
    parts = [x for x in (bit(s, (b or {}).get("sales"), "budget"), bit(s, (ly or {}).get("sales"), "last year")) if x]
    out.append(("good" if (vs(s, (b or {}).get("sales")) or vs(s, (ly or {}).get("sales")) or 0) >= 0 else "bad",
                f"Sales of {_mon(used)} {_m(s)} {cur}" + (": " + " and ".join(parts) if parts else "") + "."))
    for k, label in (("food", "Food and beverage"), ("labor", "Labour"), ("opex", "Other operating costs")):
        pc = c.get(k + "_pct")
        if c.get(k) is None:
            continue
        refs = []
        if b and b.get(k + "_pct") is not None:
            refs.append(f"budget {b[k + '_pct']:.1f}%")
        if ly and ly.get(k + "_pct") is not None:
            refs.append(f"last year {ly[k + '_pct']:.1f}%")
        amt = bit(c[k], (b or {}).get(k), "budget", True)
        worse = b and b.get(k + "_pct") is not None and pc is not None and pc > b[k + "_pct"] + 0.5
        out.append(("bad" if worse else "info",
                    f"{label} {_m(c[k])}" + (f", {pc:.1f}% of sales" if pc is not None else "")
                    + (f" ({', '.join(refs)})" if refs else "") + (f"; {amt}" if amt else "") + "."))
    e = c["ebitda"]
    eb, el = (b or {}).get("ebitda"), (ly or {}).get("ebitda")
    out.append(("good" if (eb is None or e >= eb) else "bad",
                f"EBITDA {_m(e)}" + (f" ({c['ebitda_pct']:.1f}% of sales)" if c.get("ebitda_pct") is not None else "")
                + (f", {'+' if e - eb >= 0 else '−'}{_m(abs(e - eb))} against the budget of {_m(eb)}" if eb is not None else "")
                + (f", {'+' if e - el >= 0 else '−'}{_m(abs(e - el))} against last year ({_m(el)})" if el is not None else "") + "."))
    yp = [x for x in (bit(y["sales"], (yb or {}).get("sales"), "budget"), bit(y["sales"], (yl or {}).get("sales"), "last year")) if x]
    fc, fy = T.get("forecast") or {}, T.get("fy_bud") or {}
    out.append(("info", f"Year to date ({_mon(meta.get('ytd_from') or used)[:-5]} – {_mon(used)}): sales {_m(y['sales'])}"
                        + (f" ({', '.join(yp)})" if yp else "") + f", EBITDA {_m(y['ebitda'])}"
                        + (f" against {_m(yb['ebitda'])} budgeted" if yb else "") + "."
                        + (f" Forecast of the year: sales {_m(fc.get('sales'))}, EBITDA {_m(fc.get('ebitda'))}"
                           + (f" against the full-year budget of {_m(fy.get('sales'))} and {_m(fy.get('ebitda'))}" if fy else "")
                           + "." if fc.get("sales") is not None else "")))
    rest = [d for d in depts if d["group"] == "restaurant" and d.get("bud") and d["bud"].get("sales")]
    if len(rest) > 1:
        g = sorted(rest, key=lambda d: _pct(d["cur"]["ebitda"] - d["bud"]["ebitda"], abs(d["bud"]["sales"])) or 0)
        lo, hi = g[0], g[-1]
        out.append(("info", f"Restaurants: {hi['name']} did best against budget (EBITDA {_m(hi['cur']['ebitda'])} "
                            f"against {_m(hi['bud']['ebitda'])}); {lo['name']} is furthest behind "
                            f"(EBITDA {_m(lo['cur']['ebitda'])} against {_m(lo['bud']['ebitda'])})."))
    neg = [d["name"] for d in depts if d["group"] == "restaurant" and d["cur"].get("sales") and d["cur"]["ebitda"] < 0]
    if neg:
        out.append(("bad", f"EBITDA negative in the month: {', '.join(neg[:6])}{' …' if len(neg) > 6 else ''}."))
    if var["bad"]:
        out.append(("bad", f"Largest costs and shortfalls against {var['against']}: "
                           + "; ".join(f"{x[1]} {'−' if x[5] < 0 else '+'}{_m(abs(x[5]))}" for x in var["bad"][:3]) + "."))
    if var["good"]:
        out.append(("good", f"Largest savings and gains against {var['against']}: "
                            + "; ".join(f"{x[1]} +{_m(x[5])}" for x in var["good"][:2]) + "."))
    return out


# ---------------------------------------------------------------- the email
INK, MUTED, LINE, SKY, NAVY, PRIMARY = "#0e2733", "#5c7482", "#d9dcdd", "#e4f3f9", "#08303f", "#0c475e"
RED, GREEN, AMBER = "#c0392b", "#0e8a57", "#b26a00"


def _var(v, cost=False):
    if v is None:
        return f"<span style='color:{MUTED}'>–</span>"
    good = (v <= 0) if cost else (v >= 0)
    return f"<span style='color:{GREEN if good else RED}'>{_p(v)}</span>"


def render_comment(note):
    if not (note or "").strip():
        return ""
    return (f"<div style='background:#f6f8f9;border-left:3px solid {PRIMARY};padding:10px 14px;margin:0 0 16px;font-size:13px;"
            f"line-height:1.5;color:{INK}'><div style='font-size:11px;color:{MUTED};letter-spacing:.06em;margin-bottom:4px'>COMMENT FROM FINANCE</div>"
            + html.escape(note.strip()).replace("\n", "<br>") + "</div>")


def render_section(R):
    e = html.escape
    th = f"text-align:left;padding:6px 8px;background:{SKY};color:{INK};font-size:11.5px;font-weight:600;white-space:nowrap"
    thn = th.replace("text-align:left", "text-align:right")
    td = f"padding:5px 8px;border-bottom:1px solid #f0f1f2;font-size:12px;color:{INK};vertical-align:top"
    tdn = td + ";text-align:right;white-space:nowrap"
    T, cur = R["pl"], R["currency"]

    def table(head, rows):
        hs = "".join(f"<th style='{thn if n else th}'>{e(h)}</th>" for h, n in head)
        return (f"<div style='overflow-x:auto'><table role='presentation' cellspacing='0' cellpadding='0' style='width:100%;"
                f"border-collapse:collapse;border:1px solid {LINE}'><tr>{hs}</tr>{''.join(rows)}</table></div>")

    def h3(t, sub=""):
        return (f"<h3 style='font-size:14px;color:{INK};margin:20px 0 4px'>{e(t)}</h3>"
                + (f"<div style='font-size:12px;color:{MUTED};margin-bottom:6px'>{sub}</div>" if sub else ""))
    dot = {"good": GREEN, "bad": RED, "warn": AMBER, "info": PRIMARY}
    memo_html = "".join(f"<tr><td style='padding:4px 0;width:14px;vertical-align:top'><span style='display:inline-block;width:8px;height:8px;"
                        f"border-radius:50%;background:{dot.get(k, PRIMARY)};margin-top:5px'></span></td>"
                        f"<td style='padding:3px 0;font-size:13px;line-height:1.45;color:{INK}'>{e(t)}</td></tr>" for k, t in R["memo"])
    c, b, ly, y, yb, yl = (T.get(k) or {} for k in ("cur", "bud", "ly", "ytd", "ytd_bud", "ytd_ly"))

    def pct_of(x, k):
        v = x.get(k + "_pct") if k != "sales" else None
        return f"<div style='color:{MUTED};font-size:10.5px'>{v:.1f}%</div>" if v is not None else ""
    rows = []
    for k, label, cost in LINES:
        if k == "other_income" and not (c.get(k) or y.get(k)):
            continue
        strong = k in ("sales", "ebitda")
        st = "font-weight:700;" if strong else ""
        bg = "background:#f6f8f9;" if strong else ""
        cells = []
        for A, B_, L_ in ((c, b, ly), (y, yb, yl)):
            a = A.get(k)
            cells.append(f"<td style='{tdn};{bg}{st}'>{_m(a)}{pct_of(A, k)}</td>"
                         f"<td style='{tdn};{bg}color:{MUTED}'>{_m(B_.get(k)) if B_ else '–'}{pct_of(B_, k) if B_ else ''}</td>"
                         f"<td style='{tdn};{bg}'>{_var(_pct(a, B_.get(k)) if B_ else None, cost)}</td>"
                         f"<td style='{tdn};{bg}color:{MUTED}'>{_m(L_.get(k)) if L_ else '–'}</td>"
                         f"<td style='{tdn};{bg}'>{_var(_pct(a, L_.get(k)) if L_ else None, cost)}</td>")
        rows.append(f"<tr><td style='{td};{bg}{st}'>{e(label)}</td>{cells[0]}<td style='width:8px;border-bottom:1px solid #f0f1f2;{bg}'></td>{cells[1]}</tr>")
    mon = _mon(R["month"])
    head = [("", 0), (mon[:-5], 1), ("Budget", 1), ("vs budget", 1), ("Last year", 1), ("vs LY", 1), ("", 0),
            ("Year to date", 1), ("Budget", 1), ("vs budget", 1), ("Last year", 1), ("vs LY", 1)]
    pl_html = h3("Profit and loss", f"{e(cur)}; costs with their share of sales below; red = worse than budget or last year.") + table(head, rows)

    dep_html = ""
    if R["depts"]:
        drows, last = [], None
        for d in R["depts"]:
            grp = {"restaurant": "Restaurants", "support": "Head office, central kitchen and warehouses", "unassigned": "Not on a department"}[d["group"]]
            if grp != last:
                drows.append(f"<tr><td colspan='10' style='{td};background:#f6f8f9;font-weight:700'>{e(grp)}</td></tr>")
                last = grp
            C, B_, L_ = d["cur"], d.get("bud") or {}, d.get("ly") or {}
            drows.append(f"<tr><td style='{td};padding-left:16px'>{e(d['name'])}<div style='color:{MUTED};font-size:11px'>{e(d['code'] or '')}{' · ' + e(d['brand']) if d['brand'] else ''}</div></td>"
                         f"<td style='{tdn}'>{_m(C.get('sales'))}</td><td style='{tdn}'>{_var(_pct(C.get('sales'), B_.get('sales')) if B_ else None)}</td>"
                         f"<td style='{tdn}'>{_var(_pct(C.get('sales'), L_.get('sales')) if L_ else None)}</td>"
                         f"<td style='{tdn}'>{'%.1f%%' % C['food_pct'] if C.get('food_pct') is not None else '–'}</td>"
                         f"<td style='{tdn}'>{'%.1f%%' % C['labor_pct'] if C.get('labor_pct') is not None else '–'}</td>"
                         f"<td style='{tdn};font-weight:600;color:{RED if (C.get('ebitda') or 0) < 0 else INK}'>{_m(C.get('ebitda'))}</td>"
                         f"<td style='{tdn}'>{'%.1f%%' % C['ebitda_pct'] if C.get('ebitda_pct') is not None else '–'}</td>"
                         f"<td style='{tdn};color:{MUTED}'>{_m(B_.get('ebitda')) if B_ else '–'}</td>"
                         f"<td style='{tdn};color:{MUTED}'>{_m(L_.get('ebitda')) if L_ else '–'}</td></tr>")
        dep_html = h3("By restaurant and department", f"{e(mon)}: sales against budget and last year, food and labour as a share of sales, EBITDA and its margin.") + \
            table([("Department", 0), ("Sales", 1), ("vs budget", 1), ("vs LY", 1), ("Food %", 1), ("Labour %", 1), ("EBITDA", 1),
                   ("Margin", 1), ("EBITDA budget", 1), ("EBITDA LY", 1)], drows)
    V = R["variances"]
    var_html = ""
    if V["bad"] or V["good"]:
        vrows = [f"<tr><td style='{td}'>{e(x[1])}<div style='color:{MUTED};font-size:11px'>{e(x[0])}</div></td><td style='{tdn}'>{_m(x[3])}</td>"
                 f"<td style='{tdn};color:{MUTED}'>{_m(x[4])}</td><td style='{tdn};font-weight:600;color:{GREEN if x[5] > 0 else RED}'>{'+' if x[5] > 0 else '−'}{_m(abs(x[5]))}</td></tr>"
                 for x in V["bad"] + V["good"]]
        var_html = h3(f"The accounts that moved EBITDA most against {V['against']}",
                      "Effect on EBITDA: a cost over the reference, or sales under it, takes from EBITDA (red); the opposite adds (green).") + \
            table([("Account", 0), ("Actual", 1), ("Budget" if V["against"] == "budget" else "Last year", 1), ("Effect on EBITDA", 1)], vrows)
    title = e(R["company"]) + (f" <span style='color:{MUTED};font-weight:400;font-size:13px'>· {e(R['legal'])}</span>" if R.get("legal") else "")
    return (f"<h2 style='font-size:18px;color:{INK};margin:4px 0 10px'>{title}</h2>"
            f"<table role='presentation' cellspacing='0' cellpadding='0' style='width:100%'>{memo_html}</table>{pl_html}{dep_html}{var_html}")


def subject(sections, test=False):
    mon = _mon(sections[0]["month"]) if sections else ""
    tail = ""
    if len(sections) == 1:
        R = sections[0]
        b = (R["pl"].get("bud") or {}).get("ebitda")
        tail = f" · {R['company']} · EBITDA {_m(R['pl']['cur']['ebitda'])}" + (f" vs budget {_m(b)}" if b is not None else "")
    else:
        tail = f" · {len(sections)} companies"
    return ("[Test] " if test else "") + f"[RHG] P&L memo · {mon}{tail}"


def render(sections, note=""):
    mon = _mon(sections[0]["month"]) if sections else ""
    body_parts = "".join(f"<div style='{'border-top:2px solid ' + LINE + ';margin-top:26px;padding-top:16px' if i else ''}'>{render_section(R)}</div>"
                         for i, R in enumerate(sections))
    public = (config.PUBLIC_URL or "").rstrip("/")
    body = f"""<!doctype html><html><head><meta charset="utf-8"></head><body style="margin:0;background:#ecebeb;font-family:Figtree,Segoe UI,Arial,sans-serif;color:{INK}">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#ecebeb"><tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:980px">
<tr><td style="background:{NAVY};color:#fff;padding:18px 22px;border-radius:12px 12px 0 0">
<div style="font-size:11px;letter-spacing:.12em;color:#a0d8ea">REVO HOSPITALITY GROUP · FINANCE</div>
<div style="font-size:20px;font-weight:700;margin-top:4px">P&amp;L memo · {html.escape(mon)}</div>
<div style="font-size:12px;color:#cfe3ea;margin-top:4px">Sales and costs of the month and the year to date against the budget and last year · {", ".join(html.escape(R["company"]) for R in sections)}</div></td></tr>
<tr><td style="background:#fff;padding:20px 22px;border-radius:0 0 12px 12px">
{render_comment(note)}{body_parts}
{f"<p style='margin:22px 0 0'><a href='{html.escape(public)}/finance/#overview' style='background:{PRIMARY};color:#fff;text-decoration:none;padding:10px 18px;border-radius:8px;font-size:13px;font-weight:600;display:inline-block'>Open the Finance tower</a></p>" if public else ''}
<p style="color:{MUTED};font-size:11px;margin:22px 0 0;border-top:1px solid {LINE};padding-top:12px">Sent every month by RHG Control Towers from the P&amp;L in Business Central (G/L entries by department, the G/L budget); nothing was changed there. Recipients are in Finance → Settings → Email recipients ("P&amp;L memo · monthly"); the day, the time and the comment in P&amp;L memo.</p>
</td></tr></table></td></tr></table></body></html>"""
    text = f"RHG · P&L memo · {mon}\n\n" + "\n\n".join(R["company"] + "\n" + "\n".join(f"- {t}" for _, t in R["memo"]) for R in sections)
    if note.strip():
        text = text.replace("\n\n", f"\n\nComment from Finance: {note.strip()}\n\n", 1)
    return body, text


# ---------------------------------------------------------------- sending, schedule, jobs
class Reporter:
    def __init__(self, client, currency_of):
        self.client, self.currency_of = client, currency_of
        self._jobs, self._jobs_lock = {}, threading.Lock()

    def general(self, s=None):
        s = s or settings()
        to = arap_notify.parse_emails(s.get("to"))[0]
        return to, [c for c in arap_notify.parse_emails(s.get("cc"))[0] if c not in to]

    def plan(self):
        """[(companies, to, cc)]: one email per set of readers. Everyone set for a company gets that company;
        someone set for several (All companies) gets them in one email, To when in To for any of them."""
        gto, gcc = self.general()
        who = {}
        for c in companies():
            rec = recipients.resolve("pl_monthly", [c], gto, gcc)
            for a in rec["to"]:
                w = who.setdefault(a.lower(), {"addr": a, "cos": [], "to": False})
                w["cos"].append(c)
                w["to"] = True
            for a in rec["cc"]:
                w = who.setdefault(a.lower(), {"addr": a, "cos": [], "to": False})
                if c not in w["cos"]:
                    w["cos"].append(c)
        groups = {}
        for w in who.values():
            g = groups.setdefault(tuple(w["cos"]), {"to": [], "cc": []})
            g["to" if w["to"] else "cc"].append(w["addr"])
        out = []
        for cos, g in groups.items():
            to, cc = g["to"], g["cc"]
            if not to:
                to, cc = cc[:1], cc[1:]
            out.append((list(cos), to, cc))
        return out

    def location_plan(self):
        """[(company, department, name, to, cc)]: restaurants with people of their own for the P&L memo."""
        out = []
        reg = locintel.registry()
        for c in companies():
            for loc, to, cc in recipients.location_targets("pl_monthly", c):
                k = loc.strip().upper()
                row = next((r for r in reg if (r.get("company") or "").upper() == c.upper()
                            and k in {(r.get(x) or "").strip().upper() for x in ("bc", "dept", "ref")}), None)
                dept = (row or {}).get("dept") or (row or {}).get("bc") or loc
                out.append((c, dept, (row or {}).get("name") or loc, to, cc))
        return out

    def _send(self, label, to, cc, sections, note, actor, test, month):
        body, text = render(sections, note)
        subj = subject(sections, test)
        entry = {"ts": datetime.now().isoformat(timespec="seconds"), "target": label, "actor": actor, "test": test,
                 "to": to, "cc": cc, "month": month}
        try:
            arap_notify.send_mail(to, cc, subj, body, text)
            entry.update(status="sent", detail=subj)
        except Exception as exc:                                    # noqa: BLE001
            traceback.print_exc()
            entry.update(status="failed", detail=str(exc)[:300])
        _log(entry)
        store.audit("email", f"P&L memo · {label}", actor, {}, {k: entry.get(k) for k in ("status", "to", "cc", "test")},
                    entry.get("detail", ""))
        return entry

    def run(self, key, month):
        with _run_lock:
            s = settings()
            note = s.get("note") or ""
            built, results = {}, []

            def section(c):
                if c not in built:
                    try:
                        built[c] = build_company(self.client, c, month, self.currency_of)
                    except Exception as exc:                        # noqa: BLE001
                        traceback.print_exc()
                        built[c] = exc
                return built[c]
            plan = self.plan()
            if not plan:
                _log({"ts": datetime.now().isoformat(timespec="seconds"), "target": "All companies", "actor": "scheduler",
                      "status": "skipped", "detail": "Nobody to send the P&L memo to.", "month": month})
            for cos, to, cc in plan:
                secs = [section(c) for c in cos]
                ok = [x for x in secs if not isinstance(x, Exception)]
                bad = [f"{c}: {x}" for c, x in zip(cos, secs) if isinstance(x, Exception)]
                if not ok:
                    results.append({"status": "failed"})
                    _log({"ts": datetime.now().isoformat(timespec="seconds"), "target": ", ".join(cos), "actor": "scheduler",
                          "status": "failed", "detail": "; ".join(bad)[:300], "month": month})
                    continue
                results.append(self._send(", ".join(cos), to, cc, ok, note, "scheduler", False, month))
                if bad:
                    results.append({"status": "failed"})
            for c, dept, name, to, cc in self.location_plan():
                try:
                    R = build_dept(self.client, c, dept, month, self.currency_of)
                    results.append(self._send(name, to, cc, [R], "", "scheduler", False, month))
                except Exception as exc:                            # noqa: BLE001
                    traceback.print_exc()
                    results.append({"status": "failed"})
                    _log({"ts": datetime.now().isoformat(timespec="seconds"), "target": name, "actor": "scheduler",
                          "status": "failed", "detail": str(exc)[:300], "month": month})
            ok = all(x.get("status") != "failed" for x in results)
            with _state_lock:
                st = _read_state()
                if ok:
                    st.setdefault("done", {})[key] = datetime.now().isoformat(timespec="seconds")
                    st.get("tries", {}).pop(key, None)
                else:
                    t = st.setdefault("tries", {}).setdefault(key, {"count": 0})
                    t.update(count=t["count"] + 1, at=time.time())
                    if t["count"] >= 3:
                        st.setdefault("done", {})[key] = "gave up"
                st["done"] = dict(list(st.get("done", {}).items())[-36:])
                _write_state(st)
            if ok and note.strip():                                 # the comment goes with one memo only
                store.save_settings({"pl_memo": {"note": ""}}, "scheduler")
            return results

    # ---- preview and test: target "all", a company, or "company|department"
    def start_job(self, target, actor, only_to=None):
        with self._jobs_lock:
            for j in self._jobs.values():
                if j["actor"] == actor and j["status"] == "running":
                    return {k: v for k, v in j.items() if k != "html"}
            self._jobs = {k: v for k, v in list(self._jobs.items())[-20:]}
            job = {"id": uuid.uuid4().hex, "actor": actor, "target": target, "to": only_to or [], "status": "running",
                   "detail": "", "html": None}
            self._jobs[job["id"]] = job

        def go():
            try:
                month = report_month()
                note = settings().get("note") or ""
                if "|" in target:
                    c, d = target.split("|", 1)
                    secs, label, note = [build_dept(self.client, c, d, month, self.currency_of)], target, ""
                else:
                    cos = companies() if target == "all" else [target]
                    secs, errs = [], []
                    for c in cos:
                        try:
                            secs.append(build_company(self.client, c, month, self.currency_of))
                        except Exception as exc:                    # noqa: BLE001
                            traceback.print_exc()
                            errs.append(f"{c}: {str(exc)[:200]}")
                    if not secs:
                        raise RuntimeError("; ".join(errs) or "Nothing to show.")
                    label = "All companies" if target == "all" else target
                    if errs:
                        job["detail"] = "Left out: " + "; ".join(errs)
                body, _ = render(secs, note)
                job["html"] = body
                if only_to:
                    res = self._send(label, only_to, [], secs, note, actor, True, month)
                    if res["status"] != "sent":
                        raise RuntimeError(res.get("detail") or res["status"])
                    job["detail"] = ("Sent to " + ", ".join(only_to) + ". " + job["detail"]).strip()
                job["status"] = "done"
            except Exception as exc:                                # noqa: BLE001
                traceback.print_exc()
                job.update(status="failed", detail=str(exc)[:300])
        threading.Thread(target=go, daemon=True, name="pl-memo-job").start()
        return {k: v for k, v in job.items() if k != "html"}

    def job(self, job_id, actor):
        with self._jobs_lock:
            j = self._jobs.get(job_id)
            return j if j and j["actor"] == actor else None

    def due(self, now):
        s = settings()
        if not s.get("enabled"):
            return None
        hh, _, mm = (s.get("time") or "09:00").partition(":")
        when = send_day(now.year, now.month, s)
        start = datetime(when.year, when.month, when.day, int(hh or 9), int(mm or 0))
        if not (start <= now <= start + timedelta(hours=10)):
            return None
        month = report_month(now.date())
        key = "M" + month
        with _state_lock:
            st = _read_state()
        t = st.get("tries", {}).get(key, {})
        if st.get("done", {}).get(key) or t.get("count", 0) >= 3 or time.time() - t.get("at", 0) < 1800:
            return None
        return key, month

    def next_run(self, now=None):
        s = settings()
        if not s.get("enabled"):
            return None
        now = now or datetime.now()
        hh, _, mm = (s.get("time") or "09:00").partition(":")
        y, m = now.year, now.month
        for _ in range(3):
            d = send_day(y, m, s)
            t = datetime(d.year, d.month, d.day, int(hh or 9), int(mm or 0))
            done = _read_state().get("done", {}).get("M" + report_month(d))
            if t + timedelta(hours=10) >= now and not done:
                return t.isoformat(timespec="minutes")
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        return None

    def loop(self):
        while True:
            try:
                due = self.due(datetime.now())
                if due:
                    self.run(*due)
            except Exception:                                       # noqa: BLE001
                traceback.print_exc()
            time.sleep(60)

    def start(self):
        threading.Thread(target=self.loop, daemon=True, name="pl-memo").start()
