"""People & Productivity: Odoo's HR records set against Simphony's sales and Business Central's personnel cost.

Sources (each only read):
  * Odoo (odoo.py): employees, contracts, attendances (actual hours) and planning (planned hours), summed by Odoo
    restaurant (the top department, rhg_unit_id on attendances and shifts). Time Off is not installed in RHG's Odoo:
    leave is not shown; the absences written in Planning are left out of the planned hours.
  * Simphony: net sales, checks and guests by restaurant, business date and hour (getOrderTypeQuarterHourTotals,
    the read Sales → Location intelligence keeps).
  * Business Central: the personnel cost posted on the payroll accounts (641*, 644*, 648*: salaries, top-ups, bonus,
    overtime, social and health insurance, other personnel costs) by department and month, and the net revenue of
    the same department and month (the Executive dashboard's read). Odoo Payroll is not used for cost, so no salary is
    counted twice.
Odoo restaurant → restaurant of Finance → Settings → Location intelligence: odoo.mapping (defaults + the lines of the
Connection card). A unit that maps to nothing (the warehouse, the central kitchen, the head office) is shown apart.
Nothing is matched by a person's name; nothing about a person reaches these screens: only sums by restaurant.
"""
import threading
import time
import traceback
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

import locintel
import odoo
import simphony
import store

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_jobs, _lock = {}, threading.Lock()


# ---------------------------------------------------------------- places
def places(settings=None):
    """(registry rows by reference, {Odoo unit (upper): reference or ""})."""
    settings = settings or store.get_settings()
    reg = {r["ref"]: r for r in locintel.registry(settings) if r.get("ref")}
    m = odoo.mapping(odoo.info().get("map"))
    return reg, m


def unit_ref(unit, reg, m):
    """(reference, how): "mapped" by the mapping when the reference is a restaurant of the registry; "needs review"
    when the line names a reference the registry does not hold; "" when the unit has no restaurant (support)."""
    ref = m.get((unit or "").strip().upper())
    if ref is None:
        return "", "no restaurant"
    if not ref:
        return "", "left out"
    return (ref, "mapped") if ref in reg else (ref, "needs review")


def _cut_keep(r, allowed):
    if allowed is None:
        return None
    A = {str(x).strip().upper() for x in allowed}
    return {L["ref"] for L in r.get("locations") or [] if {str(L.get(k) or "").upper() for k in ("ref", "bc", "dept")} & A}


def cut(r, allowed):
    """A person limited to some restaurants: their restaurants only, the support units and totals of the company
    dropped."""
    keep = _cut_keep(r, allowed)
    if keep is None or not isinstance(r, dict):
        return r
    out = dict(r, locations=[L for L in r.get("locations") or [] if L.get("ref") in keep], support=[], cut=True)
    for k in ("totals", "weekly", "unmapped", "transfers_list"):
        if k in out:
            out[k] = None if k in ("totals",) else []
    return out


# ---------------------------------------------------------------- Simphony by hour
def _sales_hours(refs, days_, latest_fn, progress=None):
    """{(ref, day): {hour: [net, checks, guests]}} from the quarter-hour totals, read four at a time."""
    jobs, out = {}, {}
    for ref in refs:
        try:
            last = latest_fn(ref)
        except Exception:                                          # noqa: BLE001
            last = None
        for d in days_:
            if last and d > last:
                continue
            jobs[(ref, d)] = (lambda r=ref, dd=d, lt=last: simphony.order_type_hours(r, dd, lt))
    errors = set()
    done = 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = {pool.submit(fn): k for k, fn in jobs.items()}
        for f in as_completed(futs):
            k = futs[f]
            done += 1
            if progress and done % 20 == 0:
                progress(f"Simphony: {done} of {len(jobs)} restaurant-days")
            try:
                q = f.result()
            except Exception as exc:                               # noqa: BLE001
                errors.add(str(exc)[:160])
                continue
            hrs = defaultdict(lambda: [0.0, 0, 0])
            for key, v in (q or {}).items():
                h = key.rsplit("|", 1)[-1]
                if h == "":
                    continue
                x = hrs[int(h) % 24]
                x[0] += float(v[0] or 0)
                x[2] += int(v[3] or 0)
                x[1] += int(v[2] or 0)
            out[k] = dict(hrs)
    simphony.flush_days()
    return out, sorted(errors)


def _dates(a, b):
    out, d = [], a
    while d <= b:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _per(v, h):
    return round(v / h, 2) if h else None


# ---------------------------------------------------------------- B, C, E: productivity and scheduling
def productivity(c_from, c_to, p_from, p_to, latest_fn, progress=None, refresh=False):
    """Sales, checks and guests per actual labour hour by restaurant and week; the day × hour grid (Simphony against
    Odoo's planned and actual staff); planned against actual hours; the attendances to review."""
    settings = store.get_settings()
    reg, m = places(settings)
    if progress:
        progress("Odoo: attendances and planned shifts")
    od = odoo.days(c_from, c_to, force=refresh, progress=progress)
    pod = odoo.days(p_from, p_to, progress=progress) if p_from and p_to else {}
    units_seen = sorted({u for v in list(od.values()) + list(pod.values()) for u in v})
    by_ref = defaultdict(list)
    support = []
    review = []
    for u in units_seen:
        ref, how = unit_ref(u, reg, m)
        if how == "mapped":
            by_ref[ref].append(u)
        elif how == "needs review":
            review.append({"unit": u, "ref": ref})
        else:
            support.append(u)
    refs = sorted(by_ref)
    cur_days, prev_days = _dates(c_from, c_to), (_dates(p_from, p_to) if p_from and p_to else [])
    sales, errors = _sales_hours(refs, cur_days + prev_days, latest_fn, progress)
    try:
        th = odoo.thresholds(odoo.info().get("thresholds"))
    except ValueError:
        th = {}
    locs, weekly_tot = [], defaultdict(lambda: [0.0, 0, 0, 0.0])
    for ref in refs:
        r = reg[ref]
        U = by_ref[ref]
        grid = [[{"net": 0.0, "checks": 0, "guests": 0, "hours": 0.0, "heads": 0, "planned": 0.0, "n": 0} for _ in range(24)]
                for _ in range(7)]
        tot = {"net": 0.0, "checks": 0, "guests": 0, "hours": 0.0, "planned": 0.0, "overtime": 0.0,
               "sections": {s: 0.0 for s in odoo.SECTIONS}, "open": 0, "forgotten": 0, "long": 0, "rejected_hours": 0.0,
               "states": defaultdict(int), "days_sales": 0, "days_hours": 0, "planned_days": 0}
        weeks, days_out = defaultdict(lambda: [0.0, 0, 0, 0.0, 0.0]), []
        for d in cur_days:
            wd = date.fromisoformat(d).weekday()
            s = sales.get((ref, d))
            day_net = sum(v[0] for v in (s or {}).values())
            day_chk = sum(v[1] for v in (s or {}).values())
            day_gst = sum(v[2] for v in (s or {}).values())
            hrs, pl = 0.0, 0.0
            for u in U:
                x = (od.get(d) or {}).get(u)
                if not x:
                    continue
                h = sum(x["hours"].values())
                hrs += h
                pl += x["planned"]
                tot["overtime"] += x["overtime"]
                for k, v in x["hours"].items():
                    tot["sections"][k] += v
                tot["open"] += x["open"]
                tot["forgotten"] += x["forgotten"]
                tot["long"] += x.get("long", 0)
                tot["rejected_hours"] += x["rejected_hours"]
                for k, v in x["states"].items():
                    tot["states"][k] += v
                for hh in range(24):
                    g = grid[wd][hh]
                    g["hours"] += sum(x["by_hour"][hh])
                    g["heads"] += x["heads"][hh]
                    g["planned"] += x["planned_by_hour"][hh]
            for hh, v in (s or {}).items():
                g = grid[wd][hh]
                g["net"] += v[0]
                g["checks"] += v[1]
                g["guests"] += v[2]
            for hh in range(24):
                grid[wd][hh]["n"] += 1 if (s is not None or hrs) else 0
            tot["net"] += day_net
            tot["checks"] += day_chk
            tot["guests"] += day_gst
            tot["hours"] += hrs
            tot["planned"] += pl
            tot["days_sales"] += 1 if s is not None else 0
            tot["days_hours"] += 1 if hrs else 0
            tot["planned_days"] += 1 if pl else 0
            wk = (date.fromisoformat(d) - timedelta(days=wd)).isoformat()
            w = weeks[wk]
            w[0] += day_net
            w[1] += day_chk
            w[2] += day_gst
            w[3] += hrs
            w[4] += pl
            # both sources are needed for the day to count in sales per labour hour
            if s is not None and hrs:
                wt = weekly_tot[wk]
                wt[0] += day_net
                wt[1] += day_chk
                wt[2] += day_gst
                wt[3] += hrs
            days_out.append([d, round(day_net), day_chk, day_gst, round(hrs, 1), round(pl, 1), s is not None])
        # the period before: sales per labour hour only
        pn = ph = 0.0
        for d in prev_days:
            s = sales.get((ref, d))
            h = sum(sum(((pod.get(d) or {}).get(u) or {"hours": {}})["hours"].values()) for u in U)
            if s is not None and h:
                pn += sum(v[0] for v in s.values())
                ph += h
        # both: only the days where Simphony and Odoo both answered
        both_net = sum(x[1] for x in days_out if x[6] and x[4])
        both_chk = sum(x[2] for x in days_out if x[6] and x[4])
        both_gst = sum(x[3] for x in days_out if x[6] and x[4])
        both_h = sum(x[4] for x in days_out if x[6] and x[4])
        cells = []
        for wd in range(7):
            row = []
            for hh in range(24):
                g = grid[wd][hh]
                n = max(1, sum(1 for d in cur_days if date.fromisoformat(d).weekday() == wd))
                row.append([round(g["net"] / n), round(g["checks"] / n, 1), round(g["heads"] / n, 1),
                            round(g["planned"] / n, 1), _per(g["net"], g["hours"]), round(g["hours"] / n, 1)])
            cells.append(row)
        sig = []
        lim = th.get(ref) or th.get("*") or {}
        hi = float(lim.get("splh_high") or 0)
        lo = float(lim.get("splh_low") or 0)
        if hi or lo:
            for wd in range(7):
                for hh in range(24):
                    v = cells[wd][hh][4]
                    if v is None or cells[wd][hh][0] <= 0:
                        continue
                    if hi and v > hi:
                        sig.append([WEEKDAYS[wd], hh, "high demand, low cover", v])
                    elif lo and v < lo:
                        sig.append([WEEKDAYS[wd], hh, "low demand, high cover", v])
        locs.append({
            "ref": ref, "name": r.get("name") or ref, "brand": r.get("brand") or "", "company": r.get("company") or "",
            "bc": r.get("bc") or "", "dept": r.get("dept") or "", "units": U, "currency": r.get("currency") or "ALL",
            "net": round(tot["net"]), "checks": tot["checks"], "guests": tot["guests"], "hours": round(tot["hours"], 1),
            "planned": round(tot["planned"], 1), "overtime": round(tot["overtime"], 1),
            "sections": {k: round(v, 1) for k, v in tot["sections"].items()},
            "splh": _per(both_net, both_h), "cplh": _per(both_chk, both_h), "gplh": _per(both_gst, both_h),
            "both_days": sum(1 for x in days_out if x[6] and x[4]), "days": len(cur_days),
            "days_sales": tot["days_sales"], "days_hours": tot["days_hours"], "planned_days": tot["planned_days"],
            "prev_splh": _per(pn, ph), "change_splh": round((both_net / both_h) / (pn / ph) * 100 - 100, 1) if both_h and ph and pn else None,
            "open": tot["open"], "forgotten": tot["forgotten"], "long": tot["long"], "rejected_hours": round(tot["rejected_hours"], 1),
            "states": dict(tot["states"]),
            "weeks": [[k, round(v[0]), v[1], v[2], round(v[3], 1), round(v[4], 1), _per(v[0], v[3])] for k, v in sorted(weeks.items())],
            "daily": days_out, "grid": cells, "signals": sig, "thresholds": {"splh_high": hi or None, "splh_low": lo or None}})
    locs.sort(key=lambda L: -L["net"])
    sup = []
    for u in support:
        h = sum(sum(((od.get(d) or {}).get(u) or {"hours": {}})["hours"].values()) for d in cur_days)
        p = sum(((od.get(d) or {}).get(u) or {}).get("planned", 0) for d in cur_days)
        sup.append({"unit": u, "hours": round(h, 1), "planned": round(p, 1)})
    sup.sort(key=lambda x: -x["hours"])
    return {
        "available": True, "period": {"from": c_from.isoformat(), "to": c_to.isoformat()},
        "prior": {"from": p_from.isoformat(), "to": p_to.isoformat()} if p_from else None,
        "locations": locs, "support": sup, "unmapped": review,
        "weekly": [[k, round(v[0]), v[1], v[2], round(v[3], 1), _per(v[0], v[3]), _per(v[1], v[3])] for k, v in sorted(weekly_tot.items())],
        "errors": errors, "read_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "definitions": DEFINITIONS, "day_start": odoo.DAY_START}


DEFINITIONS = {
    "splh": "Sales per labour hour = Simphony net sales / Odoo actual hours, both of the same restaurant and business "
            "days; a day counts only when both systems answered for it.",
    "cplh": "Checks per labour hour = Simphony checks / Odoo actual hours, the same days.",
    "gplh": "Guests per labour hour = Simphony guests / Odoo actual hours. Guests are the covers the staff key into "
            "Simphony: where covers are not keyed (take-away, delivery) the figure is low; read it with that in mind.",
    "hours": "Actual hours: Odoo attendances from check-in to check-out, split by the hour (an attendance across "
             "midnight goes to its hours); the hours after midnight up to 05:00 count for the business day before, as "
             "Simphony counts its sales. An attendance rejected in the review is left out; one still open counts to "
             "now; one open for more than 16 hours is a forgotten check-out, counted apart, without hours; one longer than "
             "14 hours is counted and flagged to check (a check-out closed late). Breaks are not recorded apart in "
             "Odoo: they are inside the attendance.",
    "people": "Someone who works in two restaurants counts in each for the hours checked in there (the restaurant of "
              "the attendance, rhg_unit_id), never twice.",
    "planned": "Planned hours: Odoo Planning shifts (allocated share of each), absences left out. Planning is used "
               "fully only by some restaurants: where it is empty the plan shows as missing, not as zero.",
    "grid": "The day × hour grid: the average of each weekday and hour of the period: Simphony net sales and checks, "
            "the people on shift (actual) and planned, and sales per labour hour of the cell.",
    "signals": "Signals appear only where thresholds of sales per labour hour are set for the restaurant "
               "(People & Productivity → Connection → Thresholds): above the high one, demand was high for the staff on shift; under the low one, the "
               "opposite. They point to hours to look at, not to a cause.",
    "team": "These are team figures of a restaurant, not the productivity of a person.",
}


# ---------------------------------------------------------------- A, F, quality: the workforce
def workforce():
    settings = store.get_settings()
    reg, m = places(settings)
    w = odoo.workforce()
    rows, support, review = [], [], []
    for u, v in sorted(w["now"].items(), key=lambda kv: -kv[1]["employees"]):
        ref, how = unit_ref(u, reg, m)
        r = reg.get(ref) or {}
        item = {"unit": u, "ref": ref if how == "mapped" else "", "name": r.get("name") or u, "how": how,
                "company": r.get("company") or v.get("company") or "", "bc": r.get("bc") or "", "dept": r.get("dept") or "",
                **{k: v[k] for k in ("employees", "sections", "jobs", "fte", "fte_unknown", "no_manager", "no_location",
                                     "no_job", "no_contract")},
                "trend": [[k, (w["headcount"].get(k) or {}).get(u, 0)] for k in w["months"]],
                "hires": [[k, (w["hires"].get(k) or {}).get(u, 0)] for k in w["months"]],
                "leavers": [[k, (w["leavers"].get(k) or {}).get(u, 0)] for k in w["months"]]}
        (rows if how == "mapped" else review if how == "needs review" else support).append(item)
    for x in rows + support + review:
        hs = [c for _, c in x["trend"][-13:-1]]        # the twelve closed months
        avg = sum(hs) / len(hs) if hs else 0
        lv = sum(c for _, c in x["leavers"][-13:-1])
        x["turnover_12m"] = round(lv / avg * 100, 1) if avg else None
    transfers = sorted(([k, t, n] for k, d in w["transfers"].items() for t, n in d.items()), key=lambda x: (x[0], -x[2]))
    return {"available": True, "read_at": w["read_at"], "months": w["months"], "locations": rows, "support": support,
            "unmapped": review, "totals": w["totals"], "history_from": w["history_from"],
            "transfers_list": transfers[-200:],
            "definitions": {
                "headcount": "Headcount at a month end: the employees with a contract (running or closed) covering that "
                             "day, in the restaurant of that contract (its department). Archived employees count for "
                             "the months they were under contract; the current status is never used for past months.",
                "hires": "Hires: the first contract of an employee starting in the month. Leavers: the last contract of "
                         "an employee closed in the month, with none after it.",
                "turnover": "Turnover (12 months) = leavers of the last twelve closed months / average headcount at "
                            "those month ends × 100.",
                "transfers": "Transfers: an employee whose next contract is in another restaurant, counted in the month "
                             "the new contract starts.",
                "fte": "FTE = weekly hours of the running contract's working schedule / 40. A schedule that does not "
                       "say its hours a week (a 24-hour schedule, \"Company Schedule\") is not counted: those people "
                       "are shown as 'FTE unknown'.",
                "sections": "Front of house, back of house and management follow the employee's department (FOH, BOH, "
                            "Management under each restaurant)."}}


# ---------------------------------------------------------------- D: personnel cost (BC)
LABOUR = ("labor_kitchen", "labor_service", "labor_management", "labor_other")


def personnel(cube, year, allowed_depts=None):
    """From the Executive dashboard cube of BC (by department, month and account): personnel cost by department and
    month, its accounts, net revenue and the budget, and labour cost %."""
    accounts = cube.get("accounts") or {}
    actual = cube.get("actual") or {}
    bud = cube.get("budget") or {}
    bcells = bud.get("cells") if bud.get("available") else None
    reg = {}
    for r in locintel.registry():
        for k in ("dept", "bc"):
            if r.get(k):
                reg.setdefault(r[k].upper(), r)
    months = sorted({k for d in actual.values() for k in d if len(k) == 7 and k[:4] == str(year)})

    def sums(src, dept, ks):
        lab, sales, by_acc = defaultdict(float), 0.0, defaultdict(float)
        for k in ks:
            for no, v in ((src or {}).get(dept) or {}).get(k, {}).items():
                a = accounts.get(no)
                b = a[1] if a else ""
                if b in LABOUR:
                    lab[b] += v
                    by_acc[no] += v
                elif b == "sales":
                    sales += v
        return lab, sales, by_acc
    rows = []
    for d in sorted(actual):
        if d == "*" or (allowed_depts is not None and d.upper() not in allowed_depts):
            continue
        per_m = []
        tot_l, tot_s, tot_b, accs = 0.0, 0.0, 0.0, defaultdict(float)
        for k in months:
            lab, sales, by_acc = sums(actual, d, [k])
            blab, bsales, _ = sums(bcells, d, [k]) if bcells else ({}, 0.0, {})
            L = sum(lab.values())
            B = sum(blab.values()) if blab else None
            per_m.append([k, round(L), round(sales), round(L / sales * 100, 1) if sales > 0 else None,
                          round(B) if B else None, {x: round(v) for x, v in lab.items()}])
            tot_l += L
            tot_s += sales
            tot_b += B or 0
            for no, v in by_acc.items():
                accs[no] += v
        if not tot_l and not tot_s:
            continue
        r = reg.get(d.upper()) or {}
        rows.append({"dept": d, "ref": r.get("ref") or "", "bc": r.get("bc") or "", "name": r.get("name") or d,
                     "group": "restaurant" if r else "support",
                     "cost": round(tot_l), "sales": round(tot_s), "pct": round(tot_l / tot_s * 100, 1) if tot_s > 0 else None,
                     "budget": round(tot_b) if tot_b else None, "months": per_m,
                     "accounts": sorted(([no, (accounts.get(no) or [no])[0], round(v)] for no, v in accs.items()), key=lambda x: -abs(x[2]))})
    rows.sort(key=lambda x: -x["cost"])
    return {"available": True, "year": year, "months": months, "locations": rows,
            "pending": bool(cube.get("pending")), "provisional": cube.get("provisional") or [],
            "budget": bool(bcells), "budget_name": bud.get("name"),
            "definitions": {
                "cost": "Personnel cost: what Business Central holds on the payroll accounts of the department (641*: "
                        "salaries, top-ups, bonus and overtime of kitchen, service and management; 644*: social and "
                        "health insurance; 648: other personnel costs), by posting month. Payroll is posted once a "
                        "month, so the figures are monthly; a month whose payroll is not posted yet is low or empty.",
                "pct": "Labour cost % = personnel cost / net revenue of the same department and month (Business Central, "
                       "the accounting period; Simphony's business days are not mixed in).",
                "central": "Head office, the central kitchen and the warehouse carry their own personnel cost on their "
                           "departments; it is not spread over the restaurants here.",
                "budget": "Budget: the personnel accounts of the G/L budget of Settings, when it has them."}}


# ---------------------------------------------------------------- held reads
def held_request(key, build, refresh=False):
    """A read that takes long: the last result at once, read again behind it after 10 minutes."""
    with _lock:
        j = _jobs.get(key)
        if j and j["state"] == "running":
            return dict(j.get("value") or {}, pending=not j.get("value"), reading=True, stage=j.get("stage", ""))
        if j and j["state"] == "done" and not refresh and time.time() - j["at"] < 600:
            return j["value"]
        if j and j["state"] == "failed" and not refresh and time.time() - j["at"] < 60:
            return {"available": False, "error": j["error"]}
        last = (j or {}).get("value")
        j = _jobs[key] = {"state": "running", "at": time.time(), "stage": "starting", "value": last}

    def go():
        try:
            v = build(lambda st: j.update(stage=st))
            j.update(state="done", at=time.time(), value=v)
        except Exception as exc:                                   # noqa: BLE001
            traceback.print_exc()
            j.update(state="failed", at=time.time(), error=str(exc)[:300])
    threading.Thread(target=go, daemon=True, name="people").start()
    return dict(last, reading=True, stage="starting") if last else {"pending": True, "stage": "starting"}
