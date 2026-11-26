"""Budget & forecast → Build the budget (Geri, 26 Nov 2026, approved): an interactive budget of a company for a year,
for Business Central.

The base, month by month: each calendar month from the latest one closed and posted (the year before; a month not
closed yet or still provisional, from the year before that), the same month for the sales and for the costs, so the
ratios hold.
  * Sales: Simphony by restaurant and channel (order types): net sales and checks of the base month; the mix by major
    group and family group (category) of the last three closed months (menu items by order type). The budget is
    checks × average check, each with a % per restaurant and channel; the major groups go to the sales accounts of BC
    (a mapping, by default from the names: food 705, beverages 707).
  * Costs and the other income: every income-statement account of the chart of accounts in BC, by department and
    month (the Executive dashboard's read: actual by department × month × account). Each account takes a driver:
    last year + %, % of sales, fixed a month, per check; by department where it differs.
The page works the figures out; a version is saved as a draft, sent for review and approved (finance.budget.approve);
the approved version keeps the entries it was approved with, and only those are exported for BC: a list of G/L
Budget Entries (budget name, G/L account, date, DEPARTMENT, amount signed as BC holds it). Nothing is written to BC.
"""
import json
import threading
import time
import uuid
from datetime import date, timedelta

import executive
import locintel
import menueng
import plan
import simphony
import store

FILE = store.DATA_DIR / "budget_builds.json"
_lock = threading.Lock()
_job = {"running": False, "done": 0, "total": 0}
STATUSES = ("Draft", "In review", "Approved")


def _months_of(year):
    return [f"{year}-{m:02d}" for m in range(1, 13)]


def base_months(budget_year, cube):
    """{"01": "2026-01", …}: each calendar month from the latest one closed and posted."""
    held = set(cube.get("months") or [])
    prov = set(cube.get("provisional") or [])
    run = (cube.get("running") or {}).get("month")
    out = {}
    for m in range(1, 13):
        mm = f"{m:02d}"
        for y in (budget_year - 1, budget_year - 2):
            k = f"{y}-{mm}"
            if k in held and k not in prov and k != run:
                out[mm] = k
                break
        out.setdefault(mm, f"{budget_year - 2}-{mm}")
    return out


def _span(key):
    y, m = int(key[:4]), int(key[5:7])
    a = date(y, m, 1)
    b = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
    return a, b


def _read_items(jobs):
    """The menu-item days not held (the mix), read in the background (read only)."""
    with _lock:
        if _job["running"] or not jobs:
            return
        _job.update(running=True, done=0, total=len(jobs))

    def go():
        try:
            latest = {}
            for ref in {r for r, _d in jobs}:
                try:
                    latest[ref] = simphony.latest_business_date(ref)
                except Exception:                                    # noqa: BLE001
                    latest[ref] = None
            for ref, d in jobs:
                try:
                    menueng.read_day(ref, d, latest.get(ref))
                except Exception:                                    # noqa: BLE001
                    pass
                with _lock:
                    _job["done"] += 1
        finally:
            with _lock:
                _job["running"] = False
    threading.Thread(target=go, daemon=True, name="budget-mix").start()


def mix(ref, a, b, missing):
    """{channel: {"major": {group: net}, "family": {group: net}}} of one restaurant between two dates."""
    with menueng._db_lock:
        held = {r[0] for r in menueng._db().execute("SELECT day FROM dayso WHERE ref=? AND day>=? AND day<=?",
                                                    (ref, a.isoformat(), b.isoformat())).fetchall()}
    for d in plan.days(a, b):
        if d not in held:
            missing.append((ref, d))
    try:
        items, ots = simphony.menu_items(ref), simphony.order_type_dims(ref)
    except Exception:                                                # noqa: BLE001
        return {}
    import chanprof
    out = {}
    for _d, ot, mi, net, _qty, _prep in menueng.items_by_order_type(ref, a.isoformat(), b.isoformat()):
        if not net:
            continue
        n = ots.get(int(ot)) if str(ot).lstrip("-").isdigit() and int(ot) else None
        ch = chanprof.channel_key(n or ("No order type" if not ot or str(ot) == "0" else f"Order type {ot}"))
        it = items.get(mi) or {}
        x = out.setdefault(ch, {"major": {}, "family": {}, "fam_major": {}})
        mj, fm = (it.get("major") or "Other").strip() or "Other", (it.get("family") or "Other").strip() or "Other"
        x["major"][mj] = x["major"].get(mj, 0.0) + net
        x["family"][fm] = x["family"].get(fm, 0.0) + net
        x["fam_major"][fm] = mj
    return out


def default_account(major, sales_accounts):
    """The sales account of a major group by its name: beverages and drinks to 707, the rest to 705, when the chart
    has them; else the first sales account."""
    u = (major or "").upper()
    bev = any(w in u for w in ("BEV", "DRINK", "PIJE", "BAR", "WINE", "VER", "BIRR", "COFFEE", "KAFE"))
    for want in (("707",) if bev else ("705",)):
        if want in sales_accounts:
            return want
    return sales_accounts[0] if sales_accounts else ""


def base(client, company, year, currency, refresh=False, today=None):
    """Everything the page builds the budget from, or {"pending": …} while the Executive dashboard's read runs."""
    today = today or date.today()
    cur_year = min(today.year, year - 1)
    cube = executive.request(client, company, cur_year, currency, refresh)
    if cube.get("pending") and not cube.get("cells_held"):
        return {"pending": True, "progress": cube.get("progress"), "stage": "Business Central: the actual by department"}
    bm = base_months(year, cube)
    accounts = {no: v for no, v in (cube.get("accounts") or {}).items()}
    depts = [{"code": d["code"], "name": d.get("name") or d["code"]} for d in cube.get("departments") or []]
    codes = {d["code"] for d in depts}
    # the actual of each department in the base months; "" = what is posted on no department
    act = cube.get("actual") or {}
    ly = {}
    for d in [x["code"] for x in depts]:
        ly[d] = {mm: dict((act.get(d) or {}).get(k) or {}) for mm, k in bm.items()}
    ly[""] = {}
    for mm, k in bm.items():
        tot = dict((act.get("*") or {}).get(k) or {})
        for d in codes:
            for no, v in ((act.get(d) or {}).get(k) or {}).items():
                tot[no] = tot.get(no, 0.0) - v
        ly[""][mm] = {no: round(v, 2) for no, v in tot.items() if abs(v) >= 0.5}
    # the restaurants of the company with their Simphony reference and department
    reg = [r for r in locintel.registry() if (r.get("company") or "").upper() == company.upper()
           and not locintel.is_franchise(r) and r.get("ref")]
    locs, missing_q, missing_i = [], [], []
    try:
        in_use = {l["locRef"] for l in simphony.locations() if locintel.in_use(l)}
    except Exception:                                                # noqa: BLE001
        in_use = None
    last_closed = None
    for r in reg:
        if in_use is not None and r["ref"] not in in_use:
            continue
        dept = r.get("dept") or r.get("bc") or ""
        ch_m = {}
        for mm, k in bm.items():
            a, b = _span(k)
            for ch, x in plan.by_channel(r["ref"], plan.days(a, b), missing_q).items():
                ch_m.setdefault(ch, {})[mm] = {"net": round(x["net"], 2), "checks": x["checks"]}
        # the mix of the last three months closed
        if last_closed is None:
            try:
                lb = simphony.latest_business_date(r["ref"])
                last_closed = date.fromisoformat(lb) if lb else today - timedelta(days=1)
            except Exception:                                        # noqa: BLE001
                last_closed = today - timedelta(days=1)
        m_end = last_closed.replace(day=1) - timedelta(days=1)
        m_from = (m_end.replace(day=1) - timedelta(days=62)).replace(day=1)
        mx = mix(r["ref"], m_from, m_end, missing_i)
        locs.append({"ref": r["ref"], "name": r["name"], "brand": r.get("brand") or "", "dept": dept,
                     "dept_known": dept in codes, "channels": ch_m, "mix": mx,
                     "mix_from": m_from.isoformat(), "mix_to": m_end.isoformat()})
    missing_q = sorted(set(missing_q))
    if missing_q:
        plan.read_behind(missing_q)
    missing_i = sorted(set(missing_i))
    if missing_i:
        _read_items(missing_i)
    sales_accounts = sorted(no for no, v in accounts.items() if v[1] == "sales")
    majors = sorted({mj for L in locs for x in L["mix"].values() for mj in x["major"]})
    with _lock:
        job = dict(_job)
    with plan._lock:
        pj = dict(plan._job)
    prev_budget = cube.get("budget") or {}
    return {"company": company, "year": year, "currency": currency, "base_months": bm,
            "departments": depts, "accounts": accounts, "ly": ly, "locations": locs,
            "sales_accounts": sales_accounts, "majors": majors,
            "major_accounts": {mj: default_account(mj, sales_accounts) for mj in majors},
            "income_buckets": list(executive.INCOME_BUCKETS),
            "prev_budget": {"name": prev_budget.get("name"), "available": bool(prev_budget.get("available"))},
            "reading": {"checks": {"running": pj["running"], "done": pj["done"], "total": pj["total"],
                                   "missing": len(missing_q)},
                        "mix": {"running": job["running"], "done": job["done"], "total": job["total"],
                                "missing": len(missing_i)},
                        "bc_pending": bool(cube.get("pending"))},
            "kpi_status": cube.get("kpi_status")}


# ---------------------------------------------------------------- the versions
def _read():
    try:
        return json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write(d):
    FILE.parent.mkdir(exist_ok=True)
    tmp = FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    tmp.replace(FILE)


def _key(company, year):
    return f"{company.strip().upper()}|{int(year)}"


def load(company, year):
    """{"current": the version being worked on, "versions": [the last ones, without their state]}."""
    with _lock:
        b = _read().get(_key(company, year)) or {}
    vs = b.get("versions") or []
    cur = vs[-1] if vs else None
    return {"current": {k: v for k, v in cur.items() if k != "entries"} if cur else None,
            "approved": next(({k: v for k, v in x.items() if k not in ("state", "entries")}
                              for x in reversed(vs) if x.get("status") == "Approved"), None),
            "versions": [{k: v for k, v in x.items() if k not in ("state", "entries")} for x in vs[-30:]]}


def _clean_entries(entries):
    out = []
    for e in (entries or [])[:60000]:
        try:
            m, amt = int(e.get("m")), round(float(e.get("v")), 2)
        except (TypeError, ValueError):
            continue
        if not 1 <= m <= 12 or not amt:
            continue
        out.append({"a": str(e.get("a") or "")[:20], "d": str(e.get("d") or "")[:20], "m": m, "v": amt})
    return out


def save(company, year, body, actor, can_approve):
    """action: save (Draft), submit (In review), approve (Approved: with its entries), reopen (Draft again)."""
    action = body.get("action") or "save"
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    with _lock:
        d = _read()
        b = d.setdefault(_key(company, year), {"versions": []})
        vs = b["versions"]
        cur = vs[-1] if vs else None
        if cur and cur.get("status") == "Approved" and action not in ("reopen",):
            raise ValueError("The approved version is locked: reopen it to change it.")
        if action in ("approve", "reopen") and not can_approve:
            raise PermissionError("Approving or reopening the budget needs the right to approve it (Finance).")
        if action == "approve" and (not cur or cur.get("status") != "In review"):
            raise ValueError("Send the budget for review first; the reviewer approves it.")
        state = body.get("state") if isinstance(body.get("state"), dict) else (cur or {}).get("state") or {}
        if len(json.dumps(state)) > 3_000_000:
            raise ValueError("The budget is too large to save.")
        status = {"save": "Draft", "submit": "In review", "approve": "Approved", "reopen": "Draft"}.get(action)
        if not status:
            raise ValueError("Unknown action.")
        v = {"id": uuid.uuid4().hex[:10], "status": status, "by": actor, "at": now,
             "note": str(body.get("note") or "")[:300], "name": str(body.get("name") or f"BUDGET{year}")[:20],
             "summary": body.get("summary") if isinstance(body.get("summary"), dict) else {}, "state": state}
        if action == "approve":
            v["entries"] = _clean_entries(body.get("entries"))
            if not v["entries"]:
                raise ValueError("No budget entry to approve.")
            v["approved_by"], v["approved_at"] = actor, now
        vs.append(v)
        b["versions"] = vs[-40:]
        _write(d)
    store.audit("budget", f"{company} {year}", actor, {"status": (cur or {}).get("status")}, {"status": status},
                v["note"])
    return load(company, year)


def approved_entries(company, year):
    with _lock:
        vs = (_read().get(_key(company, year)) or {}).get("versions") or []
    return next((x for x in reversed(vs) if x.get("status") == "Approved" and x.get("entries")), None)


def render_xlsx(company, year, v, accounts, depts):
    """The approved budget for BC: G/L Budget Entries, and the check by account and department."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    wb = Workbook()
    ws = wb.active
    ws.title = "G-L Budget Entries"
    head = ["Budget Name", "Date", "G/L Account No.", "G/L Account Name", "DEPARTMENT", "Amount", "Description"]
    ws.append(head)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="0C475E")
    name = v.get("name") or f"BUDGET{year}"
    rows = sorted(v["entries"], key=lambda e: (e["a"], e["d"], e["m"]))
    for e in rows:
        ws.append([name, date(int(year), e["m"], 1), e["a"], (accounts.get(e["a"]) or [""])[0], e["d"], e["v"],
                   f"Pulse {name} approved by {v.get('approved_by')} {str(v.get('approved_at'))[:10]}"])
    for r in ws.iter_rows(min_row=2, min_col=2, max_col=2):
        r[0].number_format = "yyyy-mm-dd"
    for r in ws.iter_rows(min_row=2, min_col=6, max_col=6):
        r[0].number_format = "#,##0.00"
    for col, w in zip("ABCDEFG", (14, 12, 14, 40, 14, 16, 46)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A2"
    # the check: by account and month, and by department
    c = wb.create_sheet("Check by account")
    c.append(["G/L Account No.", "Name"] + [f"{year}-{m:02d}" for m in range(1, 13)] + ["Year"])
    by = {}
    for e in rows:
        by.setdefault(e["a"], [0.0] * 12)[e["m"] - 1] += e["v"]
    for a in sorted(by):
        c.append([a, (accounts.get(a) or [""])[0]] + [round(x, 2) for x in by[a]] + [round(sum(by[a]), 2)])
    dd = wb.create_sheet("Check by department")
    dd.append(["DEPARTMENT", "Name", "Income (BC sign)", "Costs", "Result (income − costs)"])
    names = {x["code"]: x["name"] for x in depts}
    agg = {}
    for e in rows:
        x = agg.setdefault(e["d"], [0.0, 0.0])
        x[0 if e["v"] < 0 else 1] += e["v"]
    for k in sorted(agg):
        dd.append([k or "(none)", names.get(k, "no department" if not k else ""), round(agg[k][0], 2),
                   round(agg[k][1], 2), round(-agg[k][0] - agg[k][1], 2)])
    n = wb.create_sheet("Notes")
    for line in [f"{company} · {name} · approved by {v.get('approved_by')} on {str(v.get('approved_at'))[:10]}",
                 "Amounts signed as Business Central holds them: income negative, costs positive.",
                 "Date: the first day of each month. DEPARTMENT: the value of the DEPARTMENT dimension; an empty one is "
                 "a budget line on no department.",
                 "Import: G/L Budgets → the budget name → G/L Budget Entries (Edit in Excel), or a configuration "
                 "package of table 96 G/L Budget Entry with the DEPARTMENT dimension. Pulse writes nothing to "
                 "Business Central."]:
        n.append([line])
    n.column_dimensions["A"].width = 120
    from io import BytesIO
    bio = BytesIO()
    wb.save(bio)
    return bio.getvalue()
