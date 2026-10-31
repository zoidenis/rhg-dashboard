"""Finance → Location intelligence: every location of a company with its finances on one screen.

Per department (the restaurant's BC department, from the registry in Settings):
  * P&L of the latest closed month (payroll and food cost posted) up to the month chosen: sales,
    food, labour, prime cost, other costs and EBITDA against last year and the budget, the year to
    date, the full-year budget and a forecast, and the months of the year (Executive dashboard cube);
  * sales: Simphony net sales of the month to date against the sales posted in BC for the
    restaurant's POS customer (the read Location intelligence of the Executive dashboard holds);
  * payables and purchases: open supplier entries placed on the department through their dimension
    set (overdue, beyond 60 days, largest suppliers) and the supplier invoices of the period;
  * receivables: open customer entries on the department, and the balance of its POS customer.
Each part loads on its own; the screen asks again while one is still being read. BC stays read-only.
"""
import threading
import time
import traceback
from datetime import date, timedelta

import executive
import locintel
import store

_jobs, _lock = {}, threading.Lock()
KEEP = 600
_sets = {}

BUCKETS = ("sales", "other_income", "food", "labor_kitchen", "labor_service", "labor_management", "labor_other",
           "opex", "da", "below")
LINES = [("sales", "Sales"), ("food", "Food & beverage cost"), ("labor", "Labour"), ("opex", "Other costs"),
         ("ebitda", "EBITDA")]


def thresholds(settings=None):
    s = (settings or store.get_settings()).get("locations") or {}

    def num(k, d):
        try:
            return float(s.get(k, d))
        except (TypeError, ValueError):
            return d
    return {"food_cost": num("food_cost_target", 30), "prime": num("prime_cost_pct", 65),
            "budget_gap": num("budget_gap_pct", 10), "posting_gap": num("posting_gap_pct", 3), "overdue_60": 20.0}


# ---------------------------------------------------------------- the P&L cube
def _agg(src, dept, keys, accounts, scale=None):
    t = dict.fromkeys(BUCKETS, 0.0)
    n = 0
    for k in keys:
        cell = ((src or {}).get(dept) or {}).get(k)
        if not cell:
            continue
        n += 1
        f = (scale or {}).get(k, 1)
        for no, v in cell.items():
            a = accounts.get(no)
            b = a[1] if a else ("other_income" if str(no).startswith("7") else "opex")
            if b in t:
                t[b] += v * f
    return _derive(t, n)


def _derive(t, n):
    t["labor"] = t["labor_kitchen"] + t["labor_service"] + t["labor_management"] + t["labor_other"]
    t["prime"] = t["food"] + t["labor"]
    t["ebitda"] = t["sales"] + t["other_income"] - t["food"] - t["labor"] - t["opex"]
    s = t["sales"]
    out = {k: round(t[k]) for k in ("sales", "other_income", "food", "labor", "opex", "da", "prime", "ebitda")}
    for k in ("food", "labor", "prime", "opex", "ebitda"):
        out[k + "_pct"] = round(t[k] / s * 100, 1) if s else None
    out["n"] = n
    return out


def _shift(key, months):
    y, m = int(key[:4]), int(key[5:7])
    i = y * 12 + (m - 1) + months
    return f"{i // 12}-{i % 12 + 1:02d}"


def _closed_month(cube, accounts, wanted):
    """The latest month up to the one wanted with sales, food cost and payroll posted (payroll at least
    half of the three months before), never the running one: the running month has neither."""
    run = (cube.get("running") or {}).get("month")
    ks = [k for k in cube.get("months") or [] if k <= wanted and k[:4] == wanted[:4]]
    for i in range(len(ks) - 1, -1, -1):
        k = ks[i]
        if k == run:
            continue
        t = _agg(cube.get("actual"), "*", [k], accounts)
        if not (t["n"] and t["sales"] > 0 and t["food"] > 0 and t["labor"] > 0):
            continue
        prev = [_agg(cube.get("actual"), "*", [x], accounts)["labor"] for x in ks[max(0, i - 3):i]]
        prev = [v for v in prev if v > 0]
        if prev and t["labor"] < 0.5 * sum(prev) / len(prev):
            continue
        return k
    return None


def _pl(cube, wanted):
    accounts = cube.get("accounts") or {}
    month = _closed_month(cube, accounts, wanted)
    if not month:
        return None, {}
    year = month[:4]
    ytd = [k for k in cube.get("months") or [] if k[:4] == year and k <= month]
    ytd_ly = [_shift(k, -12) for k in ytd]
    rest = [f"{year}-{m:02d}" for m in range(int(month[5:7]) + 1, 13)]
    full = [f"{year}-{m:02d}" for m in range(1, 13)]
    bud = cube.get("budget") or {}
    bcells = bud.get("cells") if bud.get("available") else None
    held = [k for k in cube.get("months") or [] if k[:4] == year]
    out = {}
    for d in ["*"] + [x["code"] for x in cube.get("departments") or []]:
        cur = _agg(cube["actual"], d, [month], accounts)
        if not cur["n"] and d != "*":
            continue
        y = _agg(cube["actual"], d, ytd, accounts)
        b_m = _agg(bcells, d, [month], accounts) if bcells else None
        b_ytd = _agg(bcells, d, ytd, accounts) if bcells else None
        b_fy = _agg(bcells, d, full, accounts) if bcells else None
        b_rest = _agg(bcells, d, rest, accounts) if bcells else None
        # forecast of the year: actual to date + the budget of the months left, or, without a budget
        # on this department, the average of its last three closed months for each month left
        if b_rest and b_rest["n"]:
            fc = {k: y[k] + b_rest[k] for k in ("sales", "ebitda")}
            fc["method"] = "actual to date + budget of the months left"
        else:
            last3 = [_agg(cube["actual"], d, [k], accounts) for k in ytd[-3:]]
            last3 = [x for x in last3 if x["n"]]
            avg = {k: sum(x[k] for x in last3) / len(last3) if last3 else 0 for k in ("sales", "ebitda")}
            fc = {k: round(y[k] + avg[k] * len(rest)) for k in ("sales", "ebitda")}
            fc["method"] = "actual to date + the average of the last three months for each month left"
        trend = []
        for k in held:
            a = _agg(cube["actual"], d, [k], accounts)
            ly = _agg(cube["actual"], d, [_shift(k, -12)], accounts)
            bb = _agg(bcells, d, [k], accounts) if bcells else None
            trend.append([k, a["sales"] if a["n"] else None, a["ebitda"] if a["n"] else None,
                          ly["sales"] if ly["n"] else None, bb["sales"] if bb and bb["n"] else None,
                          k == (cube.get("running") or {}).get("month")])
        out[d] = {"cur": cur, "ly": _agg(cube["actual"], d, [_shift(month, -12)], accounts),
                  "bud": b_m if b_m and b_m["n"] else None, "ytd": y,
                  "ytd_ly": _agg(cube["actual"], d, ytd_ly, accounts),
                  "ytd_bud": b_ytd if b_ytd and b_ytd["n"] else None,
                  "fy_bud": b_fy if b_fy and b_fy["n"] else None, "forecast": fc, "trend": trend}
    meta = {"month": month, "ytd_from": ytd[0], "provisional": month in (cube.get("provisional") or []),
            "budget": bool(bcells), "budget_name": bud.get("name")}
    return out, meta


# ---------------------------------------------------------------- payables and receivables
def _dim_sets(client, company):
    hit = _sets.get(company)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    value = client.dimension_sets(company)
    _sets[company] = (time.time(), value)
    return value


def _ledgers(client, company, c_from, c_to, today):
    sets = _dim_sets(client, company)

    def dept(e):
        return sets.get(int(e.get("Dimension_Set_ID") or 0), "")

    def due(e):
        return str(e.get("Due_Date") or "")[:10]
    late, late60 = today.isoformat(), (today - timedelta(days=60)).isoformat()
    ap, ar = {}, {}
    for e in client.open_vendor_entries(company):
        owed = -float(e.get("Remaining_Amt_LCY") or 0)              # what is owed to the supplier
        o = ap.setdefault(dept(e), {"open": 0.0, "overdue": 0.0, "overdue_60": 0.0, "n": 0, "by": {}})
        o["open"] += owed
        o["n"] += 1 if owed > 0 else 0
        if owed > 0 and due(e) and due(e) < late:
            o["overdue"] += owed
            if due(e) < late60:
                o["overdue_60"] += owed
        name = (e.get("Vendor_Name") or e.get("Vendor_No") or "?").strip()
        o["by"][name] = o["by"].get(name, 0.0) + owed
    for e in client.vendor_entries(company, c_from.isoformat(), c_to.isoformat()):
        if e.get("Document_Type") not in ("Invoice", "Credit Memo"):
            continue
        amt = -float(e.get("Amount_LCY") or 0)
        o = ap.setdefault(dept(e), {"open": 0.0, "overdue": 0.0, "overdue_60": 0.0, "n": 0, "by": {}})
        o["purchases"] = o.get("purchases", 0.0) + amt
        o["invoices"] = o.get("invoices", 0) + (1 if e.get("Document_Type") == "Invoice" else 0)
        name = (e.get("Vendor_Name") or e.get("Vendor_No") or "?").strip()
        o.setdefault("pby", {})[name] = o.setdefault("pby", {}).get(name, 0.0) + amt
    for e in client.open_customer_entries(company):
        rem = float(e.get("Remaining_Amt_LCY") or 0)
        o = ar.setdefault(dept(e), {"open": 0.0, "overdue": 0.0, "overdue_60": 0.0, "n": 0, "by": {},
                                    "customers": {}})
        o["open"] += rem
        o["n"] += 1 if rem > 0 else 0
        if rem > 0 and due(e) and due(e) < late:
            o["overdue"] += rem
            if due(e) < late60:
                o["overdue_60"] += rem
        name = (e.get("Customer_Name") or e.get("Customer_No") or "?").strip()
        o["by"][name] = o["by"].get(name, 0.0) + rem
        cu = e.get("Customer_No") or ""
        o["customers"][cu] = o["customers"].get(cu, 0.0) + rem
    pos_bal = {}
    for o in ar.values():
        for cu, v in o.pop("customers").items():
            pos_bal[cu] = pos_bal.get(cu, 0.0) + v

    def top(by, n=5):
        return [[k, round(v)] for k, v in sorted(by.items(), key=lambda kv: -kv[1])[:n] if round(v)]
    for o in ap.values():
        o["top"] = top(o.pop("by"))
        o["purch_top"] = top(o.pop("pby", {}))
        for k in ("open", "overdue", "overdue_60", "purchases"):
            o[k] = round(o.get(k, 0.0))
        o.setdefault("invoices", 0)
    for o in ar.values():
        o["top"] = top(o.pop("by"))
        for k in ("open", "overdue", "overdue_60"):
            o[k] = round(o[k])
    return {"ap": ap, "ar": ar, "pos_balance": {k: round(v) for k, v in pos_bal.items()}}


def _ledger_job(client, company, c_from, c_to, refresh):
    key = (company, c_from.isoformat(), c_to.isoformat())
    now = time.time()
    with _lock:
        job = _jobs.get(key)
        if job and job["state"] == "running":
            return "pending", None, ""
        if job and job["state"] == "done" and not refresh and now - job["finished"] < KEEP:
            return job["status"], job.get("result"), job.get("error", "")
        job = _jobs[key] = {"state": "running", "started": now}

    def run():
        try:
            r, status, err = _ledgers(client, company, c_from, c_to, date.today()), "ready", ""
        except Exception as exc:                                      # noqa: BLE001
            traceback.print_exc()
            r, status, err = None, "failed", f"The supplier and customer ledgers could not be read: {str(exc)[:300]}"
        with _lock:
            job.update(state="done", finished=time.time(), status=status, result=r, error=err)
    threading.Thread(target=run, daemon=True, name="finloc-ledgers").start()
    return "pending", None, ""


# ---------------------------------------------------------------- the screen
def build(client, company, period, currency, latest_fn, parallel_fn, rights, refresh=False):
    """period: (c_from, c_to, p_from, p_to, ly_from, ly_to, running, label) of the month chosen."""
    settings = store.get_settings()
    th = thresholds(settings)
    c_from, c_to = period[0], period[1]
    reg = [r for r in locintel.registry(settings) if (r.get("company") or "").upper() == (company or "").upper()]
    by_dept = {}
    for r in reg:
        d = r.get("dept") or r.get("bc")
        if d:
            by_dept[d] = r
    out, parts = {}, {}

    def loc(code, name=None):
        L = out.get(code)
        if L is None:
            r = by_dept.get(code)
            L = out[code] = {"name": (r or {}).get("name") or name or code or "Not on a location",
                             "group": "restaurant" if r else ("unassigned" if not code else "support"),
                             "brand": (r or {}).get("brand") or "", "ref": (r or {}).get("ref") or "",
                             "customer": (r or {}).get("customer") or "", "bc": (r or {}).get("bc") or ""}
        elif name and L["name"] == code:
            L["name"] = name
        return L

    # ---- P&L
    if rights.get("pl"):
        cube = executive.request(client, company, c_from.year, currency, refresh=False)
        names = {x["code"]: x["name"] for x in cube.get("departments") or []}
        pl, meta = _pl(cube, c_from.strftime("%Y-%m")) if cube.get("actual") else (None, {})
        if pl:
            for d, v in pl.items():
                if d == "*":
                    continue
                loc(d, names.get(d))["pl"] = v
            parts["pl"] = {"state": "pending" if cube.get("pending") else "ready", **meta,
                           "note": "Business Central is still being read; the figures fill in." if cube.get("pending") else "",
                           "company": pl.get("*")}
        elif cube.get("pending"):
            g = cube.get("progress") or {}
            parts["pl"] = {"state": "pending", "note": f"P&L: {g.get('stage') or 'starting'}"
                                                       + (f" {g.get('done')} of {g.get('total')}" if g.get("total") else "")}
        else:
            parts["pl"] = {"state": "failed", "note": "No closed month yet in this year (payroll and food cost "
                                                      "posted)." if cube.get("actual") else
                           "; ".join(cube.get("errors") or [])[:300] or "The P&L could not be read."}
    else:
        parts["pl"] = {"state": "hidden", "note": "Your role does not include Profit and loss."}

    # ---- sales: Simphony against BC posted, from the read of the Executive dashboard
    li = locintel.request(client, company, period, latest_fn, parallel_fn,
                          {"purchases": False, "inventory": False}, refresh=False)
    if li.get("pending"):
        parts["sales"] = {"state": "pending", "note": f"Sales: {li.get('stage') or 'starting'}"}
    elif li.get("available") is False:
        parts["sales"] = {"state": "failed", "note": li.get("error") or "Sales could not be read."}
    else:
        parts["sales"] = {"state": "ready", "from": li["period"]["from"], "to": li["period"]["to"],
                          "simphony": li.get("simphony"), "errors": (li.get("errors") or [])[:3]}
        for r in li.get("locations") or []:
            if (r.get("company") or "").upper() != (company or "").upper():
                continue
            d = r.get("dept") or r.get("bc")
            if not d:
                continue
            s, bc = r.get("sim") or {}, r.get("bc_posted") or {}
            cur = s.get("cur") or {}
            net = cur.get("net")
            if not net and not bc.get("total"):
                continue                                            # closed, or out of season
            loc(d)["sales"] = {
                "status": r.get("status"), "currency": r.get("currency"),
                "net": net, "prev": (s.get("prev") or {}).get("net"), "ly": (s.get("ly") or {}).get("net"),
                "checks": cur.get("checks"), "guests": cur.get("guests"),
                "avg_check": round(net / cur["checks"]) if net and cur.get("checks") else None,
                "posted": bc.get("total"), "days_posted": bc.get("days_posted"),
                "unposted": (bc.get("unposted") or [])[:10], "unposted_n": len(bc.get("unposted") or []),
                "gap": round(net - bc["total"]) if net is not None and bc.get("total") is not None else None,
                "gap_pct": round((net - bc["total"]) / net * 100, 1) if net and bc.get("total") is not None else None,
                "daily": (r.get("daily") or {}).get("net"), "posted_daily": bc.get("daily"),
                "days": li["period"]["days"]}

    # ---- payables, purchases and receivables
    if rights.get("ap") or rights.get("ar"):
        state, led, err = _ledger_job(client, company, c_from, c_to, refresh)
        parts["ledgers"] = {"state": state, "note": err or ("Supplier and customer ledgers are being read"
                                                             if state == "pending" else "")}
        if led:
            if rights.get("ap"):
                for d, v in led["ap"].items():
                    loc(d)["ap"] = v
            if rights.get("ar"):
                for d, v in led["ar"].items():
                    loc(d)["ar"] = v
                for d, r in by_dept.items():
                    if r.get("customer") and r["customer"] in led["pos_balance"]:
                        loc(d).setdefault("ar", {"open": 0, "overdue": 0, "overdue_60": 0, "n": 0, "top": []})[
                            "pos_balance"] = led["pos_balance"][r["customer"]]
            if not rights.get("ap"):
                parts["ledgers"]["note"] = "Your role does not include Payables."
            if not rights.get("ar"):
                parts["ledgers"]["note"] = "Your role does not include Receivables."
    else:
        parts["ledgers"] = {"state": "hidden", "note": "Your role includes neither Payables nor Receivables."}

    # ---- alerts
    for code, L in out.items():
        al = []
        p = (L.get("pl") or {})
        cur, bud = p.get("cur") or {}, p.get("bud") or {}
        if cur.get("ebitda") is not None and cur.get("sales") and cur["ebitda"] < 0:
            al.append(["crit", "EBITDA negative in the month"])
        if bud and bud.get("ebitda") and cur.get("ebitda") is not None and \
                cur["ebitda"] < bud["ebitda"] - abs(bud["ebitda"]) * th["budget_gap"] / 100:
            al.append(["warn", f"EBITDA {round((cur['ebitda'] - bud['ebitda']) / abs(bud['ebitda']) * 100)}% against budget"])
        if cur.get("prime_pct") and cur["prime_pct"] > th["prime"]:
            al.append(["warn", f"Prime cost {cur['prime_pct']:.0f}% of sales"])
        elif cur.get("food_pct") and cur["food_pct"] > th["food_cost"]:
            al.append(["warn", f"Food cost {cur['food_pct']:.0f}% of sales"])
        s = L.get("sales") or {}
        if s.get("unposted_n"):
            al.append(["warn", f"{s['unposted_n']} day{'s' if s['unposted_n'] != 1 else ''} of sales not posted in BC"])
        elif s.get("gap_pct") is not None and abs(s["gap_pct"]) > th["posting_gap"]:
            al.append(["warn", f"BC posted differs from Simphony by {s['gap_pct']:+.0f}%"])
        ap = L.get("ap") or {}
        if ap.get("open") and ap.get("overdue_60", 0) > ap["open"] * th["overdue_60"] / 100:
            al.append(["warn", f"{round(ap['overdue_60'] / ap['open'] * 100)}% of payables overdue beyond 60 days"])
        ar = L.get("ar") or {}
        if ar.get("overdue_60", 0) > 0:
            al.append(["info", "Receivables overdue beyond 60 days"])
        L["alerts"] = al
    out = {k: v for k, v in out.items() if any(v.get(x) for x in ("pl", "sales", "ap", "ar"))}
    return {"company": company, "currency": currency, "parts": parts, "thresholds": th, "locations": out,
            "period": {"from": c_from.isoformat(), "to": c_to.isoformat(), "label": period[7]},
            "pending": any(p["state"] == "pending" for p in parts.values()),
            "lines": LINES, "definitions": DEFINITIONS}


DEFINITIONS = {
    "pl": "P&L of the latest closed month up to the month chosen: the latest with sales, food cost and payroll "
          "posted (the running month has neither). Sales, food & beverage cost, labour, other costs and EBITDA "
          "by department, as on the Executive dashboard; prime cost = food + labour.",
    "budget": "Budget = the G/L budget of the year placed on the department through its dimension set. Forecast "
              "of the year = actual to date + the budget of the months left, or, without a budget on the "
              "department, the average of its last three closed months for each month left.",
    "sales": "Simphony net sales of the month chosen, whole days (a running month stops yesterday), against the "
             "sales posted in BC for the restaurant's POS customer (VAT included in both).",
    "payables": "Open supplier entries placed on the department through their dimension set; overdue = past the "
                "due date. Purchases = supplier invoices less credit memos of the period, VAT included.",
    "receivables": "Open customer entries on the department, and the open balance of the restaurant's POS "
                   "customer (Settings → Location intelligence).",
}
