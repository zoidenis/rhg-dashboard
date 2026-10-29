"""Executive dashboard figures for the Control center.

One read builds a cube that the page then filters on its own: every month of the
chosen year and of the year before, for the whole company and for each department
(DEPARTMENT, global dimension 1). A filter on the page re-cuts what is already
loaded; it never asks Business Central for another report.

Where the figures come from
- Actuals: Chart of Accounts flow fields (Net_Change) for the income-statement
  posting accounts, one call per month and department. BC does the summing.
- Budget: G/L budget entries of the year, placed on a department through their
  dimension set.
- Sales activity: Simphony daily totals (net sales, checks, guests), rolled up to
  months. Days already read are kept, so only new days are ever asked for again.

Closed months are kept on disk. A month stays open to re-reading for 75 days after
it ends, because food cost is only right once the monthly inventory is posted.
"""
import json
import re
import threading
import time
import traceback
from calendar import monthrange
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

import finance
import simphony
import store
from bc_client import BCError

CACHE_FILE = store.DATA_DIR / "executive_cache.json"
WORKERS = 4
OPEN_DAYS = 75                       # a month can still change this long after it ends

# The defaults follow the SALT chart. They are settings, so another chart is a matter of
# typing its accounts in Finance → Settings. "641*" means every account starting 641.
DEFAULT_RULES = {
    "sales": "70*, 76",
    "food": "6011, 60111, 6012, 60121, 6013, 60122, 60123, 601231, 602, 6027, 60271, 6034, 605, 6051, 6581",
    "labor": "641*, 644*, 648*",
    "below": "667, 668, 669*, 69, 727, 769*, 77",
}
INCOME_BUCKETS = ("sales", "other_income")

_cache_lock = threading.Lock()
_mem = {"data": None, "dirty": 0, "saved": 0.0}
_jobs, _jobs_lock = {}, threading.Lock()
_bc_gate = threading.Semaphore(1)    # one cube read against BC at a time


# ------------------------------------------------------------------ rules
def _matcher(text):
    exact, prefixes = set(), []
    for part in re.split(r"[,;\s]+", text or ""):
        part = part.strip()
        if not part:
            continue
        if part.endswith("*"):
            prefixes.append(part[:-1])
        else:
            exact.add(part)
    prefixes = tuple(prefixes)
    return lambda no: no in exact or (bool(prefixes) and no.startswith(prefixes))


def rules_from(settings):
    raw = {k: ((settings.get("executive") or {}).get(k) or v) for k, v in DEFAULT_RULES.items()}
    return raw, {k: _matcher(v) for k, v in raw.items()}


def classify(no, name, match):
    """The dashboard bucket of an income-statement account."""
    if match["food"](no):
        return "food"
    if match["labor"](no):
        n = (name or "").lower()
        for word, bucket in (("kitchen", "labor_kitchen"), ("service", "labor_service"),
                             ("management", "labor_management")):
            if word in n:
                return bucket
        return "labor_other"
    if match["sales"](no):
        return "sales"
    if match["below"](no):
        return "below"
    if finance._is_da({"Name": name}):
        return "da"
    if no.startswith("7"):
        return "other_income"
    return "opex"


def mgmt(bucket, net):
    """Management sign: income and costs both positive."""
    return -net if bucket in INCOME_BUCKETS else net


# ------------------------------------------------------------------ disk cache
def _data():
    if _mem["data"] is None:
        try:
            _mem["data"] = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _mem["data"] = {}
        _mem["data"].setdefault("cells", {})
        _mem["data"].setdefault("names", {})
        _mem["data"].setdefault("misc", {})
    return _mem["data"]


def _save(force=False):
    if not _mem["dirty"] or (not force and time.time() - _mem["saved"] < 10):
        return
    tmp = CACHE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(_mem["data"], separators=(",", ":")), encoding="utf-8")
    tmp.replace(CACHE_FILE)
    _mem.update(dirty=0, saved=time.time())


def _ttl(period_to, today):
    age = (today - period_to).days
    if age > OPEN_DAYS:
        return 30 * 86400
    if age > 0:
        return 6 * 3600
    return 3600


def _key(company, dept, pkey, a, b):
    return f"{company}|{dept or '*'}|{pkey}|{a.isoformat()}|{b.isoformat()}"


def _held(key):
    with _cache_lock:
        return _data()["cells"].get(key)


def _read_cell(client, company, dept, pkey, a, b, today, force):
    key = _key(company, dept, pkey, a, b)
    hit = _held(key)
    if hit:
        age = time.time() - hit.get("at", 0)
        if age < _ttl(b, today) and not (force and (today - b).days <= OPEN_DAYS and age > 120):
            return hit
    rows = client.net_change(company, a.isoformat(), b.isoformat(), None if dept == "*" else dept)
    nets, names = {}, {}
    for r in rows:
        no = str(r.get("No") or "")
        if not no:
            continue
        names[no] = [r.get("Name") or "", r.get("Account_Category") or ""]
        v = float(r.get("Net_Change") or 0)
        if abs(v) >= 0.005:
            nets[no] = round(v, 2)
    cell = {"at": time.time(), "n": nets}
    with _cache_lock:
        d = _data()
        prefix = f"{company}|{dept or '*'}|{pkey}|"
        for stale in [k for k in d["cells"] if k.startswith(prefix) and k != key]:
            d["cells"].pop(stale, None)     # the month in progress gets a new end every day
        d["cells"][key] = cell
        d["names"].setdefault(company, {}).update(names)
        _mem["dirty"] += 1
        _save()
    return cell


# ------------------------------------------------------------------ periods
def plan(year, today=None):
    """[(key, from, to)] for every month of the year before and of the year, the running
    month cut at yesterday, plus the like-for-like slices the running month needs."""
    today = today or date.today()
    out, running = [], None
    for y in (year - 1, year):
        for m in range(1, 13):
            a = date(y, m, 1)
            if a > today:
                break
            b = date(y, m, monthrange(y, m)[1])
            if a <= today <= b:
                b = max(a, today - timedelta(days=1))
                running = (f"{y}-{m:02d}", a, b)
            out.append((f"{y}-{m:02d}", a, b))
    extra = []
    if running and running[0].startswith(str(year)):
        key, a, b = running
        try:
            ly_a, ly_b = a.replace(year=a.year - 1), b.replace(year=b.year - 1)
        except ValueError:
            ly_a, ly_b = a.replace(year=a.year - 1), date(b.year - 1, 2, 28)
        pa = (a - timedelta(days=1)).replace(day=1)
        pb = min(pa + (b - a), pa.replace(day=monthrange(pa.year, pa.month)[1]))
        extra = [(key + "~ly", ly_a, ly_b), (key + "~prior", pa, pb)]
    return out, extra, running


# ------------------------------------------------------------------ the read
def _set_progress(job_key, **kw):
    with _jobs_lock:
        job = _jobs.get(job_key)
        if job:
            job.setdefault("progress", {}).update(kw)


def _run_parallel(jobs, on_done=None):
    out = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {k: pool.submit(fn) for k, fn in jobs}
        for k, f in futures.items():
            try:
                out[k] = (f.result(), None)
            except Exception as exc:                               # noqa: BLE001
                out[k] = (None, exc)
            if on_done:
                on_done()
    return out


def build(client, company, year, job_key, refresh=False, today=None):
    today = today or date.today()
    errors = []
    months, extra, running = plan(year, today)
    span_from, span_to = months[0][1], months[-1][2]

    _set_progress(job_key, stage="Departments", done=0, total=0)
    try:
        values = client.dimension_values(company)
    except BCError as exc:
        values = []
        errors.append(f"Departments could not be read: {str(exc)[:200]}")
    names = dict(values)

    # Departments with no posting in either year are left out, so a closed shop does
    # not cost twenty-four empty calls.
    found = _run_parallel([(code, (lambda c=code: _read_cell(client, company, c, "span", span_from, span_to, today, refresh)))
                           for code in names])
    active = []
    for code in names:
        cell, err = found.get(code, (None, None))
        if err:
            errors.append(f"{code}: {str(err)[:160]}")
        elif cell and cell["n"]:
            active.append(code)
    with _jobs_lock:
        _jobs[job_key]["meta"] = {"departments": [{"code": c, "name": names[c]} for c in active]}

    # Most recent first, so the page has the months people look at before the rest.
    ordered = [p for p in reversed(months) if p[0].startswith(str(year))] + extra + \
              [p for p in reversed(months) if not p[0].startswith(str(year))]
    tasks = []
    for key, a, b in ordered:
        for dept in ["*"] + active:
            tasks.append(((key, dept), (lambda d=dept, k=key, aa=a, bb=b:
                                         _read_cell(client, company, d, k, aa, bb, today, refresh))))
    counter = {"n": 0}

    def tick():
        counter["n"] += 1
        _set_progress(job_key, stage="Business Central", done=counter["n"], total=len(tasks))
    _set_progress(job_key, stage="Business Central", done=0, total=len(tasks))
    for (key, dept), (_, err) in _run_parallel(tasks, tick).items():
        if err:
            errors.append(f"{key} {dept}: {str(err)[:160]}")
    with _cache_lock:
        _save(force=True)

    # Budget of the year, by department through the dimension set
    settings = store.get_settings()
    budget_name = (settings.get("budget") or {}).get("name") or f"BUDGET{year}"
    _set_progress(job_key, stage="Budget")
    try:
        entries = client.budget_entries(company, f"{year}-01-01", f"{year}-12-31", budget_name)
        sets = client.dimension_sets(company) if entries else {}
        cells = {}
        for e in entries:
            month = str(e.get("Date") or "")[:7]
            no = str(e.get("G_L_Account_No") or "")
            amt = float(e.get("Amount") or 0)
            if not month or not no or not amt:
                continue
            dept = sets.get(int(e.get("Dimension_Set_ID") or 0), "")
            for d in ("*", dept or ""):
                if d == "" and dept:
                    continue
                c = cells.setdefault(d, {}).setdefault(month, {})
                c[no] = round(c.get(no, 0) + amt, 2)
        with _cache_lock:
            _data()["misc"][f"budget|{company}|{year}"] = {"at": time.time(), "name": budget_name,
                                                          "cells": cells, "rows": len(entries)}
            _mem["dirty"] += 1
            _save(force=True)
    except BCError as exc:
        errors.append(f"Budget {budget_name}: {str(exc)[:200]}")

    # Simphony: read the days of the year not held yet
    if simphony.info().get("configured"):
        try:
            _simphony_fill(company, year, active, job_key, today, errors)
        except simphony.SimphonyError as exc:
            errors.append(f"Simphony: {str(exc)[:200]}")
    return errors


def _simphony_locations(active):
    locs = [l for l in simphony.locations() if l.get("active", True)]
    mapping = simphony.loc_map()
    upper = {c.upper(): c for c in active}
    for l in locs:
        mapped = mapping.get(l["locRef"], "")
        l["dept"] = upper.get(mapped.upper(), "") if mapped else upper.get((l["name"] or "").upper(), "")
    tied = [l for l in locs if l["dept"]]
    return (tied, True) if tied else (locs, False)


def _days(a, b):
    out, d = [], a
    while d <= b:
        out.append(d)
        d += timedelta(days=1)
    return out


def _simphony_fill(company, year, active, job_key, today, errors):
    locs, _ = _simphony_locations(active)
    jobs = []
    for l in locs:
        try:
            latest = simphony.latest_business_date(l["locRef"])
        except simphony.SimphonyError as exc:
            errors.append(f"Simphony {l['name']}: {str(exc)[:160]}")
            continue
        if not latest:
            continue
        start = date(year, 1, 1)
        if l.get("opened"):
            try:
                start = max(start, date.fromisoformat(str(l["opened"])[:10]))
            except ValueError:
                pass
        end = min(date.fromisoformat(latest), date(year, 12, 31), today)
        for d in _days(start, end):
            held = simphony.held_day(l["locRef"], d.isoformat())
            if held and held.get("final"):
                continue
            jobs.append(((l["locRef"], d.isoformat()),
                         (lambda r=l["locRef"], dd=d.isoformat(), lt=latest: simphony.daily_totals(r, dd, lt))))
    counter = {"n": 0}

    def tick():
        counter["n"] += 1
        _set_progress(job_key, stage="Simphony", done=counter["n"], total=len(jobs))
    _set_progress(job_key, stage="Simphony", done=0, total=len(jobs))
    failed = 0
    for _, (_, err) in _run_parallel(jobs, tick).items():
        failed += 1 if err else 0
    simphony.flush_days()
    if failed:
        errors.append(f"Simphony: {failed} location-days could not be read; they are retried on the next refresh.")


# ------------------------------------------------------------------ the payload
def assemble(company, year, currency, today=None, departments=None):
    today = today or date.today()
    settings = store.get_settings()
    raw, match = rules_from(settings)
    months, extra, running = plan(year, today)
    with _cache_lock:
        d = _data()
        names = dict(d["names"].get(company, {}))
        budget = d["misc"].get(f"budget|{company}|{year}")
        depts = departments
        if depts is None:
            depts = (d["misc"].get(f"depts|{company}|{year}") or {}).get("list") or []
        cells_src = d["cells"]
        actual, have = {}, 0
        for key, a, b in months + extra:
            for dept in ["*"] + [x["code"] for x in depts]:
                cell = cells_src.get(_key(company, dept, key, a, b))
                if cell is None:
                    continue
                have += 1
                actual.setdefault(dept, {})[key] = cell["n"]
    buckets = {}
    for no, (name, _cat) in names.items():
        buckets[no] = classify(no, name, match)

    def signed(cellmap):
        out = {}
        for dept, by_month in cellmap.items():
            for m, nets in by_month.items():
                row = {}
                for no, v in nets.items():
                    b = buckets.get(no) or classify(no, "", match)
                    row[no] = round(mgmt(b, v), 2)
                out.setdefault(dept, {})[m] = row
        return out

    accounts = {no: [names[no][0], buckets[no]] for no in names}
    budget_out = {"available": False, "name": (settings.get("budget") or {}).get("name") or f"BUDGET{year}"}
    if budget:
        budget_out = {"available": bool(budget.get("rows")), "name": budget.get("name"),
                      "rows": budget.get("rows"), "cells": signed(budget.get("cells") or {})}
    # the monthly inventory is usually posted within a few weeks of the month end
    provisional = [k for k, a, b in months if b > today - timedelta(days=45)]
    return {
        "company": company, "year": year, "currency": currency,
        "today": today.isoformat(),
        "months": [k for k, _, _ in months],
        "periods": {k: [a.isoformat(), b.isoformat()] for k, a, b in months + extra},
        "running": ({"month": running[0], "from": running[1].isoformat(), "to": running[2].isoformat(),
                     "ly": running[0] + "~ly", "prior": running[0] + "~prior"} if running else None),
        "departments": depts,
        "accounts": accounts,
        "actual": signed(actual),
        "cells_held": have,
        "budget": budget_out,
        "simphony": simphony_months(company, year, [x["code"] for x in depts]),
        "provisional": provisional,
        "rules": raw,
        "definitions": DEFINITIONS,
    }


def simphony_months(company, year, active):
    """Monthly Simphony figures from the days already held; never calls Simphony."""
    if not simphony.info().get("configured"):
        return {"configured": False}
    try:
        locs, tied = _simphony_locations(active)
    except simphony.SimphonyError as exc:
        return {"configured": True, "error": str(exc)[:200]}
    fallback = (store.get_settings().get("group") or {}).get("reporting_currency") or "ALL"
    out = {}
    for l in locs:
        months = {}
        for d in _days(date(year - 1, 1, 1), min(date(year, 12, 31), date.today())):
            held = simphony.held_day(l["locRef"], d.isoformat())
            if not held:
                continue
            fig = simphony.day_figures(held.get("rvcs") or [])
            m = months.setdefault(d.strftime("%Y-%m"), {"net": 0.0, "gross": 0.0, "discounts": 0.0,
                                                         "checks": 0, "guests": 0, "days": 0,
                                                         "last": ""})
            for k in ("net", "gross", "discounts", "checks", "guests"):
                m[k] += fig[k]
            m["days"] += 1
            m["last"] = max(m["last"], d.isoformat())
        if months:
            out[l["locRef"]] = {"name": l["name"], "dept": l["dept"],
                                "currency": l.get("currency") or fallback,
                                "months": {k: {kk: (round(vv, 2) if isinstance(vv, float) else vv)
                                               for kk, vv in v.items()} for k, v in months.items()}}
    return {"configured": True, "tied": tied, "locations": out}


DEFINITIONS = {
    "sales": "Revenue from the sales accounts (Finance → Settings → Executive dashboard), as posted in "
             "Business Central. Income is shown positive.",
    "food": "Food cost is the listed cost-of-goods accounts. It is only final once the monthly inventory "
            "is posted, so recent months are marked provisional.",
    "labor": "Labour is the payroll accounts (salaries, overtime, bonus, social and health insurance, "
             "other staff costs). Kitchen, service and management follow the account names.",
    "prime": "Prime cost is food cost plus labour.",
    "ebitda": "EBITDA is revenue and other operating income less food, labour and other operating costs. "
              "Depreciation, interest, exchange differences and profit tax are left out.",
    "budget": "Budget is the G/L budget of the year, placed on a restaurant through its dimension set.",
    "simphony": "Checks and guests are Simphony's daily totals. Guests are only as good as the covers "
                "entered at the POS.",
    "running": "The month in progress runs to yesterday and is compared with the same days of last year "
               "or of the month before.",
}


# ------------------------------------------------------------------ the request
def request(client, company, year, currency, refresh=False):
    key = (company, int(year))
    now = time.time()
    start = False
    with _jobs_lock:
        job = _jobs.get(key)
        if not job or (job["state"] != "running" and (refresh or now - job.get("finished", 0) > 900)):
            job = _jobs[key] = {"state": "running", "started": now, "progress": {"stage": "Queued"}}
            start = True
        state, progress = job["state"], dict(job.get("progress") or {})
        meta, errors = job.get("meta"), list(job.get("errors") or [])
    if start:
        threading.Thread(target=_run, args=(client, company, int(year), key, refresh),
                         daemon=True, name="executive").start()
    depts = meta["departments"] if meta else None
    payload = assemble(company, int(year), currency, departments=depts)
    payload.update(pending=state == "running", progress=progress, errors=errors,
                   started=datetime.fromtimestamp(job["started"]).isoformat(timespec="seconds"))
    return payload


def _run(client, company, year, key, refresh):
    errors = []
    try:
        with _bc_gate:
            errors = build(client, company, year, key, refresh)
        with _jobs_lock:
            meta = _jobs[key].get("meta") or {}
        with _cache_lock:
            _data()["misc"][f"depts|{company}|{year}"] = {"at": time.time(),
                                                         "list": meta.get("departments") or []}
            _mem["dirty"] += 1
            _save(force=True)
        state = "done"
    except Exception as exc:                                       # noqa: BLE001
        traceback.print_exc()
        errors.append(f"The dashboard could not be read: {str(exc)[:300]}")
        state = "failed"
    with _jobs_lock:
        _jobs[key].update(state=state, finished=time.time(), errors=errors,
                          progress={"stage": "Done" if state == "done" else "Failed"})
