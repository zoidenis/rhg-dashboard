"""Sales → Bonus management: the monthly bonus scorecard of every unit, scored automatically from the platforms RHG
already reads, against a plan of weights, scoring bands and a bonus range that management keeps on the screen; the
automation of the "Bonus Management Plan 2026" workbooks of OPA (one per unit), made the same for every brand.

Access: only the CEO and the Administrator (screen finance.s.sales.bonus, given by no other default role; see
permissions.RESTRICTED). Nothing is written to any source; the plan, the values entered by hand, the locked months and
the super bonuses stay in data/bonus.json, each change in the audit trail.

How a unit is scored, every month (the workbook's own method):
  * each KPI gets a score from 1 to 6 by five cut points (the workbook's "Scoring Range"), compared on one of three
    scales: "ratio" = actual ÷ target in % (sales), "delta" = actual − target in percentage points (food and labour
    cost against the plan), "value" = the actual itself (turnover %, Zenput %, review rating, food safety %);
  * the unit's score = Σ weight × score ÷ Σ weight of the KPIs that have a score (a KPI with no figure yet is left
    out and its weight shared by the others, said on the screen as the scorecard's coverage);
  * the bonus of the month = the amount of the range the score falls in (from 3.00: 3–3.49, 3.5–3.99, 4–4.49, ≥4.5);
  * payout: 50% of each quarter's months at the quarter end, the other 50% at the year end (changeable per plan);
  * super bonus: an amount management grants a unit for a quarter, with its reason; the screen marks the units whose
    quarter average reaches the plan's super-bonus score.

Where each figure comes from (all read only):
  * Sales vs budget: Simphony net sales of the month ÷ the BC G/L budget's net revenue of the unit's department;
  * Sales vs last year: Simphony net sales ÷ the same month of the year before (the same days for a running month);
  * Food cost / Labour cost vs plan: BC cost of goods / payroll accounts ÷ net revenue of the department (the KPI
    mapping, as the Executive dashboard), against the same ratio of the budget; else the plan's target;
  * % TM turnover: Odoo, the leavers of the month ÷ the average headcount at the two month ends;
  * Zenput reporting: Crunchtime Ops Execution (Zenput), checklist tasks done ÷ due;
  * Food safety & hygiene: Crunchtime food safety logs, checks within limits ÷ checks (temperatures, frying oil,
    products received fit to use);
  * Reviews: the month's reviews on Google, Wolt and SevenRooms (reviews.py), their average weighted by their number,
    with the plan's minimum number of reviews; the plan chooses the channels;
  * % of team trained, and anything a source does not give: entered by hand on the scorecard (with who and when).
A figure entered by hand wins over the automatic one; both are shown.
"""
import copy
import json
import threading
import time
import uuid
from calendar import monthrange
from datetime import date, datetime, timedelta

import store

FILE = store.DATA_DIR / "bonus.json"
_lock = threading.Lock()

CATEGORIES = ["Sales", "Profit", "People", "Software", "Customer satisfaction & hygiene"]

# key: (label, category, method, better, unit, source)
CATALOG = {
    "sales_budget": ("Sales vs budget", "Sales", "ratio", "higher", "money",
                     "Simphony net sales ÷ the BC G/L budget's net revenue of the unit"),
    "sales_ly": ("Sales vs last year", "Sales", "ratio", "higher", "money",
                 "Simphony net sales ÷ the same month of last year (the same days for a running month)"),
    "food_cost": ("Food cost vs plan", "Profit", "delta", "lower", "pct",
                  "BC cost of goods ÷ net revenue of the department, against the budget's (or the plan's target)"),
    "labor_cost": ("Labour cost vs plan", "Profit", "delta", "lower", "pct",
                   "BC payroll accounts ÷ net revenue of the department, against the budget's (or the plan's target)"),
    "tm_turnover": ("% TM turnover", "People", "value", "lower", "pct",
                    "Odoo: leavers of the month ÷ the average headcount of the two month ends"),
    "team_trained": ("% of team trained", "People", "value", "higher", "pct",
                     "Entered by hand: new team members fully trained within their first two weeks"),
    "ops_reporting": ("Zenput reporting", "Software", "value", "higher", "pct",
                      "Crunchtime Ops Execution (Zenput): checklist tasks done ÷ due"),
    "reviews": ("Reviews", "Customer satisfaction & hygiene", "value", "higher", "rating",
                "Google (Business Profile, or Places), Wolt and SevenRooms reviews of the month, averaged by their number"),
    "food_safety": ("Food safety & hygiene", "Customer satisfaction & hygiene", "value", "higher", "pct",
                    "Crunchtime food safety logs: checks within limits ÷ checks (temperatures, oil, products received)"),
}
METHODS = ("ratio", "delta", "value")
REVIEW_CHANNELS = ("google", "wolt", "sevenrooms")


def _kpi(key, weight, cuts, target=None, min_count=0, label=None):
    lb, cat, method, better, unit, _src = CATALOG[key]
    return {"key": key, "label": label or lb, "category": cat, "weight": weight, "method": method, "better": better,
            "cuts": cuts, "target": target, "min_count": min_count}


# The OPA plan of 2026, as the workbooks have it (Geri, 22 Nov 2026). The food cost bands follow the workbook's
# 40 / 39 / 38 / 37.5 / 37 / 36 % around a 38% plan, i.e. plan +1, plan, −0.5, −1, −2 points. The workbook's labour
# range is in transactions per labour hour (TPLH), not in labour %: the labour bands here are set from the scores the
# workbooks gave to labour % in 2026 (plan +5, +2.5, −0.5, −3.5, −5 points).
OPA_PLAN = {
    "name": "OPA", "currency": "", "min_score": 3.0, "positions": "Shift Manager",
    "kpis": [
        _kpi("sales_budget", 16, [95, 100, 104, 107, 110], label="Food sales vs budget"),
        _kpi("sales_ly", 17, [95, 100, 104, 107, 110], label="Food sales vs last year"),
        _kpi("food_cost", 20, [1.0, 0.0, -0.5, -1.0, -2.0], target=32.0),
        _kpi("labor_cost", 20, [5.0, 2.5, -0.5, -3.5, -5.0], target=20.0),
        _kpi("tm_turnover", 4, [12, 10, 9, 8, 7], target=10.0),
        _kpi("team_trained", 0, [87, 90, 93, 96, 99], target=90.0),
        _kpi("ops_reporting", 3, [75, 80, 85, 90, 95], target=80.0),
        _kpi("reviews", 10, [3.7, 4.0, 4.3, 4.6, 4.9], target=4.0, min_count=10),
        _kpi("food_safety", 10, [75, 80, 85, 90, 95], target=80.0),
    ],
    "ranges": [{"min": 3.0, "amount": 60000}, {"min": 3.5, "amount": 70000}, {"min": 4.0, "amount": 85000},
               {"min": 4.5, "amount": 120000}],
    "payout": {"quarter_pct": 50, "year_pct": 50},
    "super": {"min_score": 5.0},
    "review_sources": ["google", "wolt", "sevenrooms"],
}
DEFAULT_PLAN = dict(copy.deepcopy(OPA_PLAN), name="Default")
for _k in DEFAULT_PLAN["kpis"]:
    _k["label"] = CATALOG[_k["key"]][0]

# what the workbooks of 2026 set apart for a unit: TEG's weights, QTU's lower range
SEED_OVERRIDES = {
    "SALT|OPA_TE": {"weights": {"sales_budget": 15, "sales_ly": 15, "team_trained": 3}},
    "SALT|OPA_Q": {"ranges": [{"min": 3.0, "amount": 20000}, {"min": 3.5, "amount": 30000},
                              {"min": 4.0, "amount": 40000}, {"min": 4.5, "amount": 60000}]},
}


# ---------------------------------------------------------------- the store
def _empty():
    return {"templates": {"OPA": copy.deepcopy(OPA_PLAN), "*": copy.deepcopy(DEFAULT_PLAN)},
            "overrides": copy.deepcopy(SEED_OVERRIDES), "manual": {}, "locks": {}, "super": [], "history": {}}


def _data():
    d = store._read(FILE, None)
    if not isinstance(d, dict):
        d = _empty()
        store._write(FILE, d)
    for k, v in _empty().items():
        d.setdefault(k, v)
    return d


def _save(d):
    store._write(FILE, d)


def ukey(company, ref):
    return f"{str(company or '').upper()}|{ref}"


# ---------------------------------------------------------------- plans
def plan_for(unit, d=None):
    """The effective plan of a unit: its template (its own choice, else its brand's, else the default), with the
    unit's weights and range on top. {plan, template, overridden: [what the unit sets apart]}"""
    d = d or _data()
    T = d["templates"]
    ov = d["overrides"].get(ukey(unit.get("company"), unit["ref"])) or {}
    name = ov.get("template") if ov.get("template") in T else (unit.get("brand") if unit.get("brand") in T else "*")
    p = copy.deepcopy(T[name])
    changed = []
    for k in p["kpis"]:
        w = (ov.get("weights") or {}).get(k["key"])
        if w is not None and float(w) != float(k["weight"]):
            k["weight"] = float(w)
            changed.append("weights")
    if ov.get("ranges"):
        p["ranges"] = copy.deepcopy(ov["ranges"])
        changed.append("range")
    if ov.get("targets"):
        changed.append("targets")
    return {"plan": p, "template": name, "overridden": sorted(set(changed))}


def eligible(unit, d=None):
    d = d or _data()
    ov = d["overrides"].get(ukey(unit.get("company"), unit["ref"])) or {}
    if ov.get("eligible") is not None:
        return bool(ov["eligible"])
    return (unit.get("ownership") or "RHG").lower() != "franchise"


def target_override(unit, kpi, ym, d=None):
    d = d or _data()
    t = ((d["overrides"].get(ukey(unit.get("company"), unit["ref"])) or {}).get("targets") or {}).get(kpi) or {}
    v = t.get(ym, t.get("*"))
    return float(v) if v not in (None, "") else None


def validate_plan(p):
    """A plan as typed on the screen → a clean plan, or ValueError saying what is wrong."""
    if not isinstance(p, dict):
        raise ValueError("A plan must be an object.")
    out = {"name": str(p.get("name") or "")[:60], "currency": str(p.get("currency") or "")[:6],
           "positions": str(p.get("positions") or "")[:200], "kpis": [], "ranges": [],
           "min_score": 0.0, "payout": {}, "super": {}}
    seen = set()
    for k in p.get("kpis") or []:
        key = k.get("key")
        if key not in CATALOG or key in seen:
            raise ValueError(f"Unknown or repeated KPI: {key}.")
        seen.add(key)
        lb, cat, method, better, _u, _s = CATALOG[key]
        method = k.get("method") if k.get("method") in METHODS else method
        better = k.get("better") if k.get("better") in ("higher", "lower") else better
        try:
            w = float(k.get("weight") or 0)
            cuts = [float(c) for c in k.get("cuts") or []]
            target = None if k.get("target") in (None, "") else float(k["target"])
            mc = int(k.get("min_count") or 0)
        except (TypeError, ValueError):
            raise ValueError(f"{lb}: weight, cut points and target must be numbers.")
        if not 0 <= w <= 100:
            raise ValueError(f"{lb}: the weight is a percentage from 0 to 100.")
        if len(cuts) != 5:
            raise ValueError(f"{lb}: five cut points are needed (the lower limit of scores 2, 3, 4, 5 and 6).")
        steps = [b - a for a, b in zip(cuts, cuts[1:])]
        if better == "higher" and any(s < 0 for s in steps) or better == "lower" and any(s > 0 for s in steps):
            raise ValueError(f"{lb}: the cut points must go " + ("up" if better == "higher" else "down")
                             + " from score 2 to score 6.")
        out["kpis"].append({"key": key, "label": str(k.get("label") or lb)[:60], "category": cat, "weight": w,
                            "method": method, "better": better, "cuts": cuts, "target": target, "min_count": max(0, mc)})
    if not out["kpis"]:
        raise ValueError("A plan needs at least one KPI.")
    if abs(sum(k["weight"] for k in out["kpis"]) - 100) > 0.01:
        raise ValueError(f"The weights add up to {sum(k['weight'] for k in out['kpis']):g}%, not 100%.")
    out["ranges"] = validate_ranges(p.get("ranges"))
    try:
        out["min_score"] = float(p.get("min_score", out["ranges"][0]["min"] if out["ranges"] else 3))
        qp = float((p.get("payout") or {}).get("quarter_pct", 50))
        sm = (p.get("super") or {}).get("min_score")
        out["super"] = {"min_score": float(sm) if sm not in (None, "") else None}
    except (TypeError, ValueError):
        raise ValueError("The minimum score, the payout split and the super-bonus score must be numbers.")
    if not 0 <= qp <= 100:
        raise ValueError("The share paid at the quarter end is a percentage from 0 to 100.")
    out["payout"] = {"quarter_pct": qp, "year_pct": round(100 - qp, 4)}
    rs = p.get("review_sources")
    out["review_sources"] = [c for c in REVIEW_CHANNELS if c in rs] if isinstance(rs, list) else list(REVIEW_CHANNELS)
    return out


def validate_ranges(ranges):
    out = []
    for r in ranges or []:
        try:
            out.append({"min": round(float(r.get("min")), 2), "amount": round(float(r.get("amount") or 0), 2)})
        except (TypeError, ValueError, AttributeError):
            raise ValueError("Each line of the bonus range needs a score from and an amount.")
    out.sort(key=lambda r: r["min"])
    if not out:
        raise ValueError("The bonus range needs at least one line.")
    if any(r["amount"] < 0 for r in out) or len({r["min"] for r in out}) != len(out):
        raise ValueError("The bonus range: amounts cannot be negative and each score from appears once.")
    if any(not 1 <= r["min"] <= 6 for r in out):
        raise ValueError("The bonus range: the scores are between 1 and 6.")
    return out


# ---------------------------------------------------------------- scoring (the workbook's method)
def score_of(k, value, target):
    """(score 1–6, the compared figure x) of one KPI, or (None, None) when it cannot be scored."""
    if value is None:
        return None, None
    m = k["method"]
    if m == "ratio":
        if not target:
            return None, None
        x = value / target * 100
    elif m == "delta":
        if target is None:
            return None, None
        x = value - target
    else:
        x = value
    hi = k["better"] == "higher"
    s = 1 + sum(1 for c in k["cuts"] if (x >= c - 1e-9 if hi else x <= c + 1e-9))
    return s, round(x, 4)


def band_of(plan, score):
    """(amount, label) of the range the score falls in; (0, None) below the first line or the minimum."""
    if score is None:
        return 0, None
    s = round(score, 2)
    R = sorted(plan["ranges"], key=lambda r: r["min"])
    if s < max(plan.get("min_score") or 0, R[0]["min"] if R else 99):
        return 0, None
    hit = None
    for i, r in enumerate(R):
        if s >= r["min"]:
            nxt = R[i + 1]["min"] if i + 1 < len(R) else None
            hit = (r["amount"], f"{r['min']:.2f} – {nxt - 0.01:.2f}" if nxt else f"≥ {r['min']:.2f}")
    return hit or (0, None)


def evaluate(unit, plan, auto, manual):
    """The scorecard of a unit for a month. auto/manual: {kpi: {value, target, ...}}."""
    rows, sw, sws, tot_w = [], 0.0, 0.0, 0.0
    for k in plan["kpis"]:
        a = (auto or {}).get(k["key"]) or {}
        m = (manual or {}).get(k["key"]) or {}
        has_m = m.get("value") is not None
        value = m["value"] if has_m else a.get("value")
        target = m.get("target") if m.get("target") is not None else a.get("target")
        if target is None:
            target = k.get("target") if k["method"] != "ratio" else None
        status = a.get("status") or ("" if value is not None else "no figure")
        if has_m:
            status = "entered by hand"
        s, x = score_of(k, value, target)
        n = a.get("count")
        if not has_m and s is not None and k.get("min_count") and n is not None and n < k["min_count"]:
            s, status = 1, f"{n} of the {k['min_count']} reviews needed: scored 1"
        w = float(k["weight"] or 0)
        tot_w += w
        if s is not None and w > 0:
            sw += w
            sws += w * s
        rows.append({"key": k["key"], "label": k["label"], "category": k["category"], "weight": w, "method": k["method"],
                     "better": k["better"], "cuts": k["cuts"], "unit": CATALOG[k["key"]][4], "source": CATALOG[k["key"]][5],
                     "value": value, "target": target, "x": x, "score": s, "status": status, "count": n,
                     "provisional": bool(a.get("provisional")), "partial": bool(a.get("partial")),
                     "approximate": bool(a.get("approximate")) and not has_m, "channels": a.get("channels"),
                     "auto": {kk: a.get(kk) for kk in ("value", "target", "count", "status", "detail")} if a else None,
                     "manual": m if has_m or m.get("target") is not None else None})
    score = round(sws / sw, 4) if sw else None
    cats = []
    for c in CATEGORIES:
        R = [r for r in rows if r["category"] == c and r["weight"] > 0]
        if not R:
            continue
        cw = sum(r["weight"] for r in R)
        sc = [r for r in R if r["score"] is not None]
        cats.append({"name": c, "weight": cw, "score": round(sum(r["weight"] * r["score"] for r in sc) / sum(r["weight"] for r in sc), 2)
                     if sc else None})
    amount, band = band_of(plan, score)
    return {"ref": unit["ref"], "name": unit.get("name") or unit["ref"], "brand": unit.get("brand") or "",
            "kpis": rows, "categories": cats, "score": score, "coverage": round(sw / tot_w * 100, 1) if tot_w else 0,
            "amount": amount, "band": band, "currency": plan.get("currency") or unit.get("currency") or ""}


# ---------------------------------------------------------------- the sources
def month_range(ym, today=None):
    today = today or date.today()
    y, m = int(ym[:4]), int(ym[5:7])
    a = date(y, m, 1)
    b = date(y, m, monthrange(y, m)[1])
    running = a <= today <= b
    if running:
        b = max(a, today - timedelta(days=1))
    return a, b, running


def _sum_bucket(cells, accounts, bucket):
    tot, have = 0.0, False
    for no, v in (cells or {}).items():
        b = ((accounts or {}).get(no) or [None, None])[1] or ""
        if b == bucket or b.startswith(bucket + "_"):
            tot += v or 0
            have = True
    return tot if have else None


def from_exec(ex, unit, ym, running=False, a=None, b=None, ly_net_same_days=None, sim=None):
    """Sales, food cost and labour cost of a unit from the Executive dashboard's read (BC actuals and budget, Simphony
    months)."""
    out = {}
    if not isinstance(ex, dict):
        return out
    dept = (unit.get("dept") or unit.get("bc") or "").upper()
    codes = {str(x.get("code") or "").upper(): x.get("code") for x in ex.get("departments") or []}
    dkey = codes.get(dept)
    acc = ex.get("accounts") or {}
    act = ((ex.get("actual") or {}).get(dkey) or {}).get(ym) if dkey else None
    bud_cells = ((ex.get("budget") or {}).get("cells") or {}).get(dkey) or {} if dkey else {}
    bud = bud_cells.get(ym)
    prov = ym in (ex.get("provisional") or [])
    # Simphony
    held = ((ex.get("simphony") or {}).get("locations") or {})
    loc = held.get(unit["ref"]) or next((v for v in held.values() if (v.get("dept") or "").upper() == dept and dept), None)
    net = ((loc or {}).get("months") or {}).get(ym) or {}
    y, mm = int(ym[:4]), ym[5:7]
    ly = ((loc or {}).get("months") or {}).get(f"{y - 1}-{mm}") or {}
    if sim is not None:                  # read by the unit's own Simphony reference (sim_month), not the dashboard's ties
        net, ly, ly_net_same_days = {"net": sim.get("cur")}, {"net": sim.get("ly")}, sim.get("ly")
        if sim.get("reading"):
            for k in ("sales_budget", "sales_ly"):
                out[k] = {"value": None, "status": "reading the month's days from Simphony"}
    days_in = monthrange(y, int(mm))[1]
    if net.get("net"):
        bs = _sum_bucket(bud, acc, "sales") if bud else None
        share = 1.0
        if running and a and b:
            share = ((b - a).days + 1) / days_in
        out["sales_budget"] = {"value": round(net["net"], 2), "target": round(bs * share, 2) if bs else None,
                               "status": "" if bs else "no budget for the unit's department",
                               "partial": running,
                               "detail": "month in progress: budget prorated to the days read" if running else ""}
        lyv = ly_net_same_days if (running and ly_net_same_days) else ly.get("net")
        out["sales_ly"] = {"value": round(net["net"], 2), "target": round(lyv, 2) if lyv else None,
                           "status": "" if lyv else "no Simphony sales last year", "partial": running,
                           "detail": "month in progress: the same days of last year" if running else ""}
    # BC ratios
    s_act = _sum_bucket(act, acc, "sales") if act else None
    s_bud = _sum_bucket(bud, acc, "sales") if bud else None
    for key, bucket in (("food_cost", "food"), ("labor_cost", "labor")):
        v_act = _sum_bucket(act, acc, bucket) if act else None
        v_bud = _sum_bucket(bud, acc, bucket) if bud else None
        val = round(v_act / s_act * 100, 2) if (v_act is not None and s_act) else None
        tgt = round(v_bud / s_bud * 100, 2) if (v_bud is not None and s_bud) else None
        if val is None and not dkey:
            st = "the unit's department is not in BC"
        elif val is None:
            st = "no BC figures for the month yet"
        else:
            st = ""
        out[key] = {"value": val, "target": tgt, "status": st, "provisional": prov and bucket == "food",
                    "detail": ("target: the budget's ratio" if tgt is not None else "target: the plan's")
                              + ("; provisional until the month's inventory is posted" if prov and bucket == "food" else "")}
    return out


def from_workforce(wf, unit, ym):
    if not isinstance(wf, dict):
        return {}
    row = next((r for r in wf.get("locations") or [] if r.get("ref") == unit["ref"]), None)
    if not row:
        return {"tm_turnover": {"value": None, "status": "the unit is not mapped in Odoo"}}
    hc = dict(row.get("trend") or [])
    lv = dict(row.get("leavers") or [])
    y, m = int(ym[:4]), int(ym[5:7])
    prev = f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"
    if ym not in hc:
        return {"tm_turnover": {"value": None, "status": "no Odoo headcount for the month"}}
    avg = ((hc.get(prev) or hc[ym]) + hc[ym]) / 2
    n = lv.get(ym, 0)
    return {"tm_turnover": {"value": round(n / avg * 100, 2) if avg else None, "count": n,
                            "detail": f"{n} leaver(s), average headcount {avg:g}"}}


def from_ops(row):
    if not row:
        return {"ops_reporting": {"value": None, "status": "no Crunchtime location for the unit"},
                "food_safety": {"value": None, "status": "no Crunchtime location for the unit"}}
    t = row.get("tasks") or {}
    out = {"ops_reporting": {"value": row.get("completion"), "count": t.get("due"),
                             "status": "" if row.get("completion") is not None else "no checklist due in the month",
                             "detail": f"{t.get('done', 0)} of {t.get('due', 0)} tasks done, {t.get('late', 0)} late"}}
    n = (row.get("temp_n") or 0) + (row.get("oil_n") or 0) + (row.get("trace_n") or 0)
    bad = (row.get("temp_out") or 0) + (row.get("oil_over") or 0) + (row.get("trace_unfit") or 0)
    out["food_safety"] = {"value": round((n - bad) / n * 100, 2) if n else None, "count": n,
                          "status": "" if n else "no food safety log in the month",
                          "detail": f"{n - bad} of {n} checks within limits"}
    return out


def from_feedback(rows):
    """SevenRooms feedback of the month as a review channel."""
    rs = [r.get("overall") for r in rows or [] if r.get("overall") is not None]
    return {"value": round(sum(rs) / len(rs), 2) if rs else None, "count": len(rs), "approximate": False,
            "detail": f"SevenRooms: {len(rs)} review(s)"}


def combine_reviews(channels, allowed=REVIEW_CHANNELS):
    """The Reviews KPI from its channels: the average of the month's reviews weighted by their number, the total
    count. A channel that gives only an overall rating (Google Places) counts with its month's number of reviews."""
    ch = {k: v for k, v in (channels or {}).items() if k in allowed and v}
    if not ch:
        return None
    num = den = 0.0
    total, approx = 0, False
    for v in ch.values():
        n = v.get("count") or 0
        total += max(n, 0)
        if v.get("value") is not None and n > 0:
            num += v["value"] * n
            den += n
            approx = approx or bool(v.get("approximate"))
    value = round(num / den, 2) if den else None
    if value is None:
        g = ch.get("google") or {}
        if g.get("value") is not None:              # Places only, no new review counted yet: the overall rating
            value, approx = g["value"], True
    detail = "; ".join(v.get("detail") or "" for v in ch.values() if v.get("detail"))
    return {"value": value, "count": total, "approximate": approx,
            "status": "" if value is not None else "no review in the month",
            "detail": detail + ("; approximate: Google's overall rating" if approx else ""),
            "channels": {k: {kk: v.get(kk) for kk in ("value", "count", "approximate")} for k, v in ch.items()}}


_fb_reading = set()
_sim_jobs = {}


def _days_of(a, b):
    return [(a + timedelta(days=i)).isoformat() for i in range((b - a).days + 1)]


def sim_month(refs, a, b, refresh=False):
    """{ref: {cur, ly, missing}}: Simphony net sales of each unit by its own reference (as Location intelligence reads
    them), the month a..b and the same days of last year, from the days already read; the days not read yet are
    read behind (one job per month), so the screen says it is still reading. ({}, reading)"""
    import simphony
    if not simphony.info().get("configured"):
        return {}, False
    try:
        known = {l["locRef"] for l in simphony.locations()}
    except Exception:                                               # noqa: BLE001
        known = set()
    cur_days = _days_of(a, b)
    ly_days = []
    for d in cur_days:
        try:
            ly_days.append(date.fromisoformat(d).replace(year=a.year - 1).isoformat())
        except ValueError:                                          # 29 February
            pass
    today = date.today().isoformat()
    out, need = {}, {}
    for ref in refs:
        if known and ref not in known:
            continue
        tot, miss = {"cur": 0.0, "ly": 0.0}, []
        for part, ds in (("cur", cur_days), ("ly", ly_days)):
            for d in ds:
                if d >= today:
                    continue
                h = simphony.held_day(ref, d)
                if h is None or (refresh and not h.get("final")):
                    miss.append(d)
                if h:
                    tot[part] += simphony.day_figures(h.get("rvcs") or [])["net"]
        out[ref] = {"cur": round(tot["cur"], 2) or None, "ly": round(tot["ly"], 2) or None, "missing": len(miss)}
        if miss:
            need[ref] = miss
    key = (a, b)
    job = _sim_jobs.get(key)
    running = bool(job and job.get("running"))
    if need and not running and (not job or time.time() - job.get("at", 0) > 300 or refresh):
        _sim_jobs[key] = {"running": True, "at": time.time()}

        def go(need=need, key=key):
            from concurrent.futures import ThreadPoolExecutor
            try:
                def one(ref, ds):
                    try:
                        latest = simphony.latest_business_date(ref)
                    except Exception:                               # noqa: BLE001
                        return
                    for d in ds:
                        if latest and d > latest:
                            continue
                        try:
                            simphony.daily_totals(ref, d, latest)
                        except Exception:                           # noqa: BLE001
                            pass
                with ThreadPoolExecutor(4) as ex:
                    list(ex.map(lambda kv: one(*kv), need.items()))
                simphony.flush_days()
            finally:
                _sim_jobs[key] = {"running": False, "at": time.time()}
        threading.Thread(target=go, daemon=True, name="bonus-simphony").start()
        running = True
    if running:                          # a partial month would score low: no figure until the days are read
        for ref in need:
            out[ref].update(cur=None, ly=None, reading=True)
    return out, running


def collect(client, company, currency, ym, units, refresh=False, today=None):
    """{ref: {kpi: auto}}, and the state of each source {name: ok | reading | not connected | error}."""
    import crunchtime
    import executive
    import odoo
    import opsexec
    import people
    import sevenrooms
    import simphony
    a, b, running = month_range(ym, today)
    state, auto = {}, {u["ref"]: {} for u in units}
    # BC + Simphony (one read for the year, the Executive dashboard's)
    try:
        ex = executive.request(client, company, a.year, currency, refresh=refresh)
        state["Business Central"] = "reading" if ex.get("pending") else "ok"
        try:
            sims, sim_reading = sim_month([u["ref"] for u in units], a, b, refresh)
            state["Simphony sales"] = "reading" if sim_reading else ("ok" if sims else "not connected")
        except Exception as exc:                                    # noqa: BLE001
            sims = {}
            state["Simphony sales"] = "error: " + str(exc)[:160]
        for u in units:
            if u["ref"] in sims:
                auto[u["ref"]].update(from_exec(ex, u, ym, running, a, b, None, sims[u["ref"]]))
                continue
            lysd = None
            if running:
                try:
                    lysd = 0.0
                    for i in range((b - a).days + 1):
                        d = a + timedelta(days=i)
                        try:
                            dl = d.replace(year=d.year - 1)
                        except ValueError:
                            continue
                        h = simphony.held_day(u["ref"], dl.isoformat())
                        if h:
                            lysd += simphony.day_figures(h.get("rvcs") or [])["net"]
                    lysd = lysd or None
                except Exception:                                   # noqa: BLE001
                    lysd = None
            auto[u["ref"]].update(from_exec(ex, u, ym, running, a, b, lysd))
    except Exception as exc:                                        # noqa: BLE001
        state["Business Central"] = "error: " + str(exc)[:160]
    # Crunchtime Ops Execution (Zenput)
    try:
        if not crunchtime.info()["configured"]:
            state["Crunchtime (Zenput)"] = "not connected"
        else:
            r = opsexec.held_request(("ox", a, b), lambda st: opsexec.build(a, b, st), refresh)
            if r.get("pending"):
                state["Crunchtime (Zenput)"] = "reading"
            elif r.get("available") is False:
                state["Crunchtime (Zenput)"] = "error: " + str(r.get("error") or "")[:160]
            else:
                state["Crunchtime (Zenput)"] = "reading" if r.get("reading") else "ok"
                by = {L.get("ref"): L for L in r.get("locations") or [] if L.get("ref")}
                for u in units:
                    auto[u["ref"]].update(from_ops(by.get(u["ref"])))
    except Exception as exc:                                        # noqa: BLE001
        state["Crunchtime (Zenput)"] = "error: " + str(exc)[:160]
    # Odoo HR
    try:
        if not odoo.info()["configured"]:
            state["Odoo HR"] = "not connected"
        else:
            r = people.held_request(("wf",), lambda st: people.workforce(), refresh)
            if r.get("pending"):
                state["Odoo HR"] = "reading"
            elif r.get("available") is False:
                state["Odoo HR"] = "error: " + str(r.get("error") or "")[:160]
            else:
                state["Odoo HR"] = "reading" if r.get("reading") else "ok"
                for u in units:
                    auto[u["ref"]].update(from_workforce(r, u, ym))
    except Exception as exc:                                        # noqa: BLE001
        state["Odoo HR"] = "error: " + str(exc)[:160]
    # reviews: Google and Wolt (reviews.py), SevenRooms feedback (what is held at once, a fresh read behind it)
    channels = {u["ref"]: {} for u in units}
    try:
        import reviews
        ri = reviews.info()
        g, w = ri["google"], ri["wolt"]
        state["Google reviews"] = ("ok" if (g["business_profile"] or g["places"]) and not (ri["state"].get("google_error") or ri["state"].get("places_error"))
                                   else "error: " + (ri["state"].get("google_error") or ri["state"].get("places_error"))[:160]
                                   if (g["business_profile"] or g["places"]) else "not connected")
        state["Wolt reviews"] = "ok" if w["secret_set"] else "not connected"
        for u in units:
            channels[u["ref"]].update(reviews.monthly(u["ref"], a, b))
    except Exception as exc:                                        # noqa: BLE001
        state["Google reviews"] = "error: " + str(exc)[:160]
    try:
        conn = sevenrooms.info()
        if not conn["configured"]:
            state["SevenRooms"] = "not connected"
        else:
            import locintel
            import srsim
            regs = [r for r in locintel.registry() if (r.get("company") or "").upper() == company.upper()]
            placed = {r["ref"]: v for v, r, _h in srsim.place(sevenrooms.venues(), regs, conn["map"]) if r}
            state["SevenRooms"] = "ok"
            for u in units:
                v = placed.get(u["ref"])
                if not v:
                    continue
                channels[u["ref"]]["sevenrooms"] = from_feedback(sevenrooms.feedback_held(v["id"], a, b))
                k = (v["id"], a, b)
                if k not in _fb_reading:
                    _fb_reading.add(k)

                    def go(vid=v["id"], kk=k):
                        try:
                            sevenrooms.read_feedback(vid, a, b)
                        except Exception:                           # noqa: BLE001
                            pass
                        finally:
                            _fb_reading.discard(kk)
                    threading.Thread(target=go, daemon=True, name="bonus-fb").start()
    except Exception as exc:                                        # noqa: BLE001
        state["SevenRooms"] = "error: " + str(exc)[:160]
    for u in units:
        if channels[u["ref"]]:
            auto[u["ref"]]["_review_channels"] = channels[u["ref"]]
    return auto, state


# ---------------------------------------------------------------- the screens
def units_of(company, registry):
    return [r for r in registry if (r.get("company") or "").upper() == str(company or "").upper()]


def scorecard(company, ym, registry, auto, state, save_history=True):
    d = _data()
    out, changed = [], False
    for u in units_of(company, registry):
        k = ukey(company, u["ref"])
        lock = ((d["locks"].get(k) or {}).get(ym))
        pl = plan_for(u, d)
        if lock:
            r = dict(lock["result"], locked={"by": lock["by"], "at": lock["at"]}, eligible=lock.get("eligible", True),
                     template=lock.get("template"), overridden=lock.get("overridden") or [])
            out.append(r)
            continue
        man = (d["manual"].get(k) or {}).get(ym) or {}
        au = copy.deepcopy(auto.get(u["ref"]) or {})
        rc = au.pop("_review_channels", None)
        if rc:
            rv = combine_reviews(rc, pl["plan"].get("review_sources") or REVIEW_CHANNELS)
            if rv:
                au["reviews"] = rv
        for kpi in list(au):
            t = target_override(u, kpi, ym, d)
            if t is not None:
                au[kpi]["target"] = t
                au[kpi]["detail"] = ((au[kpi].get("detail") or "") + "; target set for the unit").strip("; ")
        for kp in pl["plan"]["kpis"]:
            t = target_override(u, kp["key"], ym, d)
            if t is not None and kp["key"] not in au:
                au[kp["key"]] = {"value": None, "target": t}
        r = evaluate(u, pl["plan"], au, man)
        r.update(eligible=eligible(u, d), template=pl["template"], overridden=pl["overridden"], locked=None,
                 currency=r["currency"] or u.get("currency") or "")
        if not r["eligible"]:
            r["amount"] = 0
        out.append(r)
        if save_history and r["score"] is not None:
            h = d["history"].setdefault(k, {})
            new = {"score": r["score"], "amount": r["amount"], "coverage": r["coverage"], "band": r["band"]}
            if {kk: (h.get(ym) or {}).get(kk) for kk in new} != new:
                h[ym] = dict(new, at=datetime.now().isoformat(timespec="seconds"))
                changed = True
    if changed:
        with _lock:
            fresh = _data()
            for k, h in d["history"].items():
                fresh["history"].setdefault(k, {}).update(h)
            _save(fresh)
    reading = [n for n, s in state.items() if s == "reading"]
    return {"company": company, "month": ym, "units": out, "sources": state, "reading": reading,
            "running": month_range(ym)[2], "catalog": catalog(), "categories": CATEGORIES}


def year_view(company, year, registry):
    """Every unit's months (locked, else the last computed), quarters, the payout and the super bonuses."""
    d = _data()
    rows = []
    for u in units_of(company, registry):
        k = ukey(company, u["ref"])
        pl = plan_for(u, d)["plan"]
        months = {}
        for m in range(1, 13):
            ym = f"{year}-{m:02d}"
            lock = (d["locks"].get(k) or {}).get(ym)
            if lock:
                r = lock["result"]
                months[ym] = {"score": r.get("score"), "amount": r.get("amount") or 0, "locked": True,
                              "coverage": r.get("coverage")}
            elif (d["history"].get(k) or {}).get(ym):
                h = d["history"][k][ym]
                months[ym] = {"score": h.get("score"), "amount": h.get("amount") or 0, "locked": False,
                              "coverage": h.get("coverage"), "at": h.get("at")}
        el = eligible(u, d)
        qp = (pl.get("payout") or {}).get("quarter_pct", 50) / 100
        smin = (pl.get("super") or {}).get("min_score")
        quarters = []
        for q in range(1, 5):
            ms = [months.get(f"{year}-{m:02d}") for m in range(3 * q - 2, 3 * q + 1)]
            got = [x for x in ms if x and x.get("score") is not None]
            earned = sum(x["amount"] for x in got) if el else 0
            sup = [s for s in d["super"] if s["key"] == k and s["period"] == f"{year}-Q{q}"]
            avg = round(sum(x["score"] for x in got) / len(got), 2) if got else None
            quarters.append({"q": q, "avg": avg, "months": len(got), "locked": sum(1 for x in got if x["locked"]),
                             "earned": earned, "paid": round(earned * qp, 2), "super": sum(s["amount"] for s in sup),
                             "super_items": sup, "super_eligible": bool(el and avg is not None and smin and avg >= smin)})
        tot = sum(q["earned"] for q in quarters)
        rows.append({"ref": u["ref"], "name": u.get("name") or u["ref"], "brand": u.get("brand") or "", "eligible": el,
                     "currency": pl.get("currency") or u.get("currency") or "", "months": months, "quarters": quarters,
                     "earned": tot, "paid_quarters": round(sum(q["paid"] for q in quarters), 2),
                     "year_end": round(tot * (1 - qp), 2), "super": sum(q["super"] for q in quarters),
                     "total": round(tot + sum(q["super"] for q in quarters), 2),
                     "super_min": smin, "quarter_pct": qp * 100})
    return {"company": company, "year": year, "units": rows}


def catalog():
    return [{"key": k, "label": v[0], "category": v[1], "method": v[2], "better": v[3], "unit": v[4], "source": v[5]}
            for k, v in CATALOG.items()]


def config_view(company, registry):
    d = _data()
    units = []
    for u in units_of(company, registry):
        pl = plan_for(u, d)
        ov = d["overrides"].get(ukey(company, u["ref"])) or {}
        units.append({"ref": u["ref"], "name": u.get("name") or u["ref"], "brand": u.get("brand") or "",
                      "ownership": u.get("ownership") or "RHG", "template": pl["template"], "eligible": eligible(u, d),
                      "override": ov, "plan": pl["plan"]})
    brands = sorted({u["brand"] for u in units if u["brand"]})
    return {"templates": d["templates"], "units": units, "brands": brands, "catalog": catalog(),
            "categories": CATEGORIES, "methods": METHODS}


# ---------------------------------------------------------------- changes
def save_template(name, plan, actor):
    name = str(name or "").strip().upper() if name != "*" else "*"
    if not name:
        raise ValueError("Name the plan: the brand it is for.")
    clean = validate_plan(plan)
    clean["name"] = "Default" if name == "*" else name
    with _lock:
        d = _data()
        before = d["templates"].get(name)
        d["templates"][name] = clean
        _save(d)
    store.audit("bonus plan saved", name, actor, before or {}, clean)
    return clean


def delete_template(name, actor):
    if name in ("*",):
        raise ValueError("The default plan stays: it is the plan of every brand without its own.")
    with _lock:
        d = _data()
        before = d["templates"].pop(name, None)
        if before is None:
            raise ValueError("No such plan.")
        _save(d)
    store.audit("bonus plan deleted", name, actor, before, {})


def save_override(company, unit, patch, actor):
    """unit: the registry line of the unit (its ref and brand)."""
    ref = unit["ref"]
    k = ukey(company, ref)
    with _lock:
        d = _data()
        ov = dict(d["overrides"].get(k) or {})
        before = copy.deepcopy(ov)
        if "template" in patch:
            t = patch["template"] or None
            if t and t not in d["templates"]:
                raise ValueError("No such plan.")
            ov["template"] = t
        if "eligible" in patch:
            ov["eligible"] = None if patch["eligible"] is None else bool(patch["eligible"])
        if "weights" in patch:
            W = {}
            for kk, w in (patch["weights"] or {}).items():
                if kk not in CATALOG:
                    raise ValueError(f"Unknown KPI: {kk}.")
                if w in (None, ""):
                    continue
                w = float(w)
                if not 0 <= w <= 100:
                    raise ValueError("A weight is a percentage from 0 to 100.")
                W[kk] = w
            ov["weights"] = W
        if "ranges" in patch:
            ov["ranges"] = validate_ranges(patch["ranges"]) if patch["ranges"] else None
        if "targets" in patch:
            T = {}
            for kk, by in (patch["targets"] or {}).items():
                if kk not in CATALOG:
                    raise ValueError(f"Unknown KPI: {kk}.")
                T[kk] = {str(m): float(v) for m, v in (by or {}).items() if v not in (None, "")}
            ov["targets"] = {kk: v for kk, v in T.items() if v}
        if "note" in patch:
            ov["note"] = str(patch["note"] or "")[:300]
        ov = {kk: v for kk, v in ov.items() if v not in (None, {}, [], "")}
        # the weights must still add up to 100
        d["overrides"][k] = ov
        pl = plan_for(dict(unit, company=company), d)["plan"]
        tw = sum(x["weight"] for x in pl["kpis"])
        if abs(tw - 100) > 0.01:
            raise ValueError(f"With these weights the unit's KPIs add up to {tw:g}%, not 100%.")
        if not ov:
            d["overrides"].pop(k, None)
        _save(d)
    store.audit("bonus unit plan", k, actor, before, ov)
    return ov


def set_manual(company, ref, ym, kpi, value, target, note, actor):
    if kpi not in CATALOG:
        raise ValueError("Unknown KPI.")
    datetime.strptime(ym, "%Y-%m")
    k = ukey(company, ref)
    with _lock:
        d = _data()
        if (d["locks"].get(k) or {}).get(ym):
            raise ValueError("The month is locked: unlock it first.")
        cell = d["manual"].setdefault(k, {}).setdefault(ym, {})
        before = copy.deepcopy(cell.get(kpi))
        if value in (None, "") and target in (None, ""):
            cell.pop(kpi, None)
        else:
            cell[kpi] = {"value": None if value in (None, "") else float(value),
                         "target": None if target in (None, "") else float(target),
                         "note": str(note or "")[:300], "by": actor, "at": datetime.now().isoformat(timespec="seconds")}
        _save(d)
    store.audit("bonus value", f"{k} {ym} {kpi}", actor, before or {}, cell.get(kpi) or {})


def lock(company, ref, ym, result, actor):
    k = ukey(company, ref)
    with _lock:
        d = _data()
        d["locks"].setdefault(k, {})[ym] = {"result": {kk: v for kk, v in result.items()
                                                       if kk not in ("locked", "eligible", "template", "overridden")},
                                            "eligible": result.get("eligible", True), "template": result.get("template"),
                                            "overridden": result.get("overridden"),
                                            "by": actor, "at": datetime.now().isoformat(timespec="seconds")}
        _save(d)
    store.audit("bonus month locked", f"{k} {ym}", actor, {}, {"score": result.get("score"), "amount": result.get("amount")})


def unlock(company, ref, ym, actor, reason=""):
    k = ukey(company, ref)
    with _lock:
        d = _data()
        before = (d["locks"].get(k) or {}).pop(ym, None)
        if before is None:
            raise ValueError("The month is not locked.")
        _save(d)
    store.audit("bonus month unlocked", f"{k} {ym}", actor, {"score": before["result"].get("score")}, {}, reason)


def add_super(company, ref, period, amount, reason, actor):
    if not (len(period) == 7 and period[4:6] == "-Q" and period[6] in "1234"):
        raise ValueError("The period of a super bonus is a quarter, e.g. 2026-Q3.")
    amount = float(amount)
    if amount <= 0:
        raise ValueError("The super bonus must be more than zero.")
    reason = str(reason or "").strip()
    if len(reason) < 5:
        raise ValueError("Say why the super bonus is granted: it goes in the audit trail.")
    item = {"id": uuid.uuid4().hex[:10], "key": ukey(company, ref), "period": period, "amount": amount,
            "reason": reason[:500], "by": actor, "at": datetime.now().isoformat(timespec="seconds")}
    with _lock:
        d = _data()
        d["super"].append(item)
        _save(d)
    store.audit("bonus super granted", item["key"], actor, {}, item)
    return item


def remove_super(sid, actor):
    with _lock:
        d = _data()
        item = next((s for s in d["super"] if s["id"] == sid), None)
        if not item:
            raise ValueError("No such super bonus.")
        d["super"] = [s for s in d["super"] if s["id"] != sid]
        _save(d)
    store.audit("bonus super removed", item["key"], actor, item, {})
