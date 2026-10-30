"""Location intelligence: every restaurant of a company on one screen.

One read brings together, per restaurant:
  * Simphony (point of sale): net sales, checks, guests, discounts, voids and error corrects for
    the month to date, the same days of the month before and the same dates last year, plus the
    day-by-day series of the month;
  * Business Central: the sales posted for the restaurant's POS customer (customer ledger), the
    purchase invoices posted to its location, the use at cost of the month (item ledger, held by
    Movement & dead stock) and its stock position (Typology & turnover).

Which Simphony location is which BC company, location and POS customer is kept in a registry in
Settings, one line per restaurant. Business Central stays read-only.
"""
import threading
import time
import traceback
from datetime import date, datetime, timedelta

import config
import movement
import recipes
import simphony
import store
import typology

# One line per restaurant: Simphony location | name | key=value ...
#   brand     icon on the map (SALT, LIFT, OPA, VESPER, LE DISTRICT, SIN, ZOI)
#   company   BC company;  bc = BC location code;  customer = BC customer the POS sales post to
#   area      shown under the name;  region = anything but blank puts it outside the city map
#   currency  overrides what Simphony says (blank in Simphony means the group currency)
#   map=x,y   position on the city map in % of its width and height;  seasonal = closes off season
DEFAULT_REGISTRY = """\
SALT     | SALT Tirana          | brand=SALT        | company=SALT | bc=SALT       | customer=SALT     | area=Blloku           | map=41,53
LIFT     | LIFT Steak & Rooftop | brand=LIFT        | company=SALT | bc=LIFT       | customer=LIFT     | area=Qendër           | map=52,40
OPA_TI   | OPA · TIA Airport    | brand=OPA         | company=SALT | bc=OPA TIA    | customer=OPA TIA  | area=Rinas airport    | map=10,12
OPA_B    | OPA Bllok            | brand=OPA         | company=SALT | bc=OPA1       | customer=OPA1     | area=Blloku           | map=31,67
VES      | Vesper               | brand=VESPER      | company=SALT | bc=VESPER     | customer=VESPER   | area=Blloku           | map=47,72
OPA_9KAT | OPA 9 Katëshet       | brand=OPA         | company=SALT | bc=OPA 9KAT   | customer=OPA 9KAT | area=Rr. e Kavajës    | map=27,49
OPA_A    | OPA Arena            | brand=OPA         | company=SALT | bc=OPA3       | customer=OPA3     | area=Arena Center     | map=63,52
OPA_TE   | OPA TEG              | brand=OPA         | company=SALT | bc=OPA TEG    | customer=OPA TEG  | area=Tirana East Gate | map=84,80
OPA_Q    | OPA QTU              | brand=OPA         | company=SALT | bc=OPA QTU    | customer=OPA QTU  | area=QTU, Kashar      | map=14,40
OPA_SK   | OPA Shkodër          | brand=OPA         | company=SALT | bc=OPASHKODER | customer=         | area=Shkodër          | region=Shkodër
OPA_RK   | OPA Rruga e Kavajës  | brand=OPA         | company=SALT | bc=           | customer=         | area=Rr. e Kavajës
SALT_GC  | SALT Green Coast     | brand=SALT        | company=SALT | bc=SALT GC    | customer=SALT GC  | area=Palasë           | seasonal
SIN      | SIN GC               | brand=SIN         | company=SALT | bc=SIN        | customer=SIN      | area=Palasë           | seasonal
District | Le District          | brand=LE DISTRICT | company=TBM  | bc=           | customer=LEDISTRICT | area=Tirana
SALTPRIS | SALT Prishtina       | brand=SALT        | company=RKS Revo Hospitality Group | currency=EUR | area=Prishtina | region=Kosovo
Zoi      | Zoi Paros            | brand=ZOI         | company=Zoi Greece | currency=EUR | area=Paros | region=Greece | seasonal
"""

CURRENCY_ALIASES = {"L": "ALL", "LEK": "ALL", "LEKE": "ALL", "€": "EUR"}
FIELDS = ("net", "discounts", "gross", "checks", "guests", "voids", "tax", "service", "error_corrects")
USE_KINDS = ("sale", "sale_return", "consumption", "output")


def norm_currency(code):
    c = (code or "").strip().upper()
    return CURRENCY_ALIASES.get(c, c)


def registry(settings=None):
    text = ((settings or store.get_settings()).get("locations") or {}).get("registry") or DEFAULT_REGISTRY
    out, seen = [], set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        ref = parts[0]
        if not ref or ref in seen:
            continue
        seen.add(ref)
        d = {"ref": ref, "name": ref, "brand": "", "company": "", "bc": "", "customer": "", "area": "",
             "region": "", "currency": "", "x": None, "y": None, "seasonal": False}
        for i, p in enumerate(parts[1:]):
            if "=" in p:
                k, _, v = p.partition("=")
                k, v = k.strip().lower(), v.strip()
                if k == "map":
                    try:
                        x, y = (float(t) for t in v.split(","))
                        if 0 <= x <= 100 and 0 <= y <= 100:
                            d["x"], d["y"] = x, y
                    except ValueError:
                        pass
                elif k == "currency":
                    d["currency"] = norm_currency(v)
                elif k in d and k not in ("ref", "x", "y", "seasonal"):
                    d[k] = v[:80]
            elif p.lower() in ("seasonal", "season"):
                d["seasonal"] = True
            elif i == 0 and p:
                d["name"] = p[:80]
        d["brand"] = (d["brand"] or d["name"].split()[0]).upper()
        out.append(d)
    return out


def currency_for(loc_ref, given="", fallback="ALL"):
    """The currency a Simphony location reports in: the registry first, then Simphony, then the
    group currency. 'L' and 'LEK' are lek."""
    for r in registry():
        if r["ref"] == loc_ref and r["currency"]:
            return r["currency"]
    return norm_currency(given) or fallback


def thresholds(settings=None):
    s = (settings or store.get_settings()).get("locations") or {}

    def num(k, d):
        try:
            return float(s.get(k, d))
        except (TypeError, ValueError):
            return d
    return {"food_cost": num("food_cost_target", 30), "cover_days": num("cover_days", 30),
            "error_corrects": num("error_corrects_pct", 5), "discounts": num("discounts_pct", 8),
            "voids": num("voids_pct", 2), "sales_drop": num("sales_drop_pct", 15)}


# ---------------------------------------------------------------- reads
def _days(a, b):
    out, d = [], a
    while d <= b:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _empty():
    return dict.fromkeys(FIELDS, 0.0)


def _simphony(reg, c, p, ly, latest_fn, parallel_fn, progress):
    """{ref: {"cur", "prev", "ly", "daily"}} plus Simphony locations missing from the registry."""
    locs = [l for l in simphony.locations() if l.get("active", True)]
    by_ref = {l["locRef"]: l for l in locs}
    latest = {r: (v if not e else None) for r, (v, e) in parallel_fn(
        {l["locRef"]: (lambda x=l["locRef"]: latest_fn(x)) for l in locs}).items()}
    days = {"cur": _days(*c), "prev": _days(*p), "ly": _days(*ly)}
    jobs = {}
    for ref in by_ref:
        last = latest.get(ref)
        for period, ds in days.items():
            for d in ds:
                if last and d > last:
                    continue
                jobs[(ref, d)] = (lambda r=ref, dd=d, lt=last: simphony.daily_totals(r, dd, lt))
    progress(f"Simphony · {len(by_ref)} locations, {len(jobs)} days")
    res = parallel_fn(jobs, workers=4)
    simphony.flush_days()
    errors = sorted({str(e)[:200] for _, (_, e) in res.items() if e})
    out = {}
    for ref, l in by_ref.items():
        row = {"cur": _empty(), "prev": _empty(), "ly": _empty(),
               "daily": {"net": [0.0] * len(days["cur"]), "checks": [0] * len(days["cur"]),
                         "guests": [0] * len(days["cur"])},
               "name": l.get("name") or ref, "currency_given": norm_currency(l.get("currency")),
               "latest": latest.get(ref)}
        for period, ds in days.items():
            for i, d in enumerate(ds):
                value, _ = res.get((ref, d), (None, None))
                if not value:
                    continue
                fig = simphony.day_figures(value.get("rvcs") or [])
                t = row[period]
                for k in FIELDS:
                    t[k] += fig[k]
                if period == "cur":
                    row["daily"]["net"][i] = round(fig["net"], 2)
                    row["daily"]["checks"][i] = fig["checks"]
                    row["daily"]["guests"][i] = fig["guests"]
        for period in ("cur", "prev", "ly"):
            row[period] = {k: round(v, 2) for k, v in row[period].items()}
        out[ref] = row
    return out, errors, latest


def _posted_sales(client, company, customers, c):
    """{customer: [amount per day of the period]}: invoices less credit memos in the customer
    ledger, lek, VAT included (POS sales post one invoice per restaurant and day)."""
    days = _days(*c)
    idx = {d: i for i, d in enumerate(days)}
    out = {cu: [0.0] * len(days) for cu in customers}
    if not customers:
        return out
    rows = client._get_all(client._url(company, config.E_CUSTOMER), {
        "$filter": f"Posting_Date ge {c[0].isoformat()} and Posting_Date le {c[1].isoformat()} and "
                   f"(Document_Type eq 'Invoice' or Document_Type eq 'Credit Memo')",
        "$select": "Customer_No,Posting_Date,Document_Type,Amount_LCY"})
    for r in rows:
        cu, d = r.get("Customer_No") or "", str(r.get("Posting_Date") or "")[:10]
        if cu in out and d in idx:
            out[cu][idx[d]] += float(r.get("Amount_LCY") or 0)
    return {k: [round(v, 2) for v in vs] for k, vs in out.items()}


def _purchases(client, company, c):
    """{location: {"amount", "n", "suppliers"}} from the posted purchase invoices (lek, VAT
    included; invoices in another currency are counted apart)."""
    rows = client._get_all(client._url(company, config.E_PINV), {
        "$filter": f"Posting_Date ge {c[0].isoformat()} and Posting_Date le {c[1].isoformat()}",
        "$select": "Location_Code,Buy_from_Vendor_Name,Amount_Including_VAT,Currency_Code,Cancelled"})
    out = {}
    for r in rows:
        if r.get("Cancelled"):
            continue
        loc = (r.get("Location_Code") or "").strip()
        o = out.setdefault(loc, {"amount": 0.0, "n": 0, "fx": 0, "by": {}})
        if (r.get("Currency_Code") or "").strip():
            o["fx"] += 1
            continue
        amt = float(r.get("Amount_Including_VAT") or 0)
        o["amount"] += amt
        o["n"] += 1
        name = (r.get("Buy_from_Vendor_Name") or "").strip() or "?"
        o["by"][name] = o["by"].get(name, 0.0) + amt
    for o in out.values():
        o["suppliers"] = [[k, round(v, 0)] for k, v in sorted(o.pop("by").items(), key=lambda kv: -kv[1])[:3]]
        o["amount"] = round(o["amount"], 0)
    return out


def _use_at_cost(client, company, c):
    """{location: use at cost in the period}: sales and assembly consumption net of assembly output
    (a dish assembled to order counts once), from the item ledger that Movement holds."""
    months = sorted({d[:7] for d in _days(*c)})
    for mo in months:
        movement.load_month(client, company, mo)
    with movement._lock:
        rows = movement._db().execute(
            f"SELECT location, SUM(cost) FROM mv WHERE company=? AND day>=? AND day<=? AND kind IN "
            f"({','.join('?' * len(USE_KINDS))}) GROUP BY location",
            (company, c[0].isoformat(), c[1].isoformat(), *USE_KINDS)).fetchall()
    return {loc: round(-cost, 0) for loc, cost in rows}


def inventory(client, company, settings):
    """Stock position per location from Movement & dead stock and Typology & turnover. Starts the
    Movement read when it is not held; returns ("pending", note) until it is ready."""
    r = movement.request(client, company, None, 90)
    if r.get("pending"):
        g = r.get("progress") or {}
        months = f" · {g.get('done')} of {g.get('total')} months" if g.get("total") else ""
        return "pending", f"Stock is being read: {g.get('stage') or 'starting'}{months}", None
    if r.get("available") is False:
        return "failed", r.get("error") or "Movement could not be read.", None
    m = recipes.master_cached(company) or {}
    t = typology.analyse(r, m.get("items") or {}, settings)
    days = t["history_days"]
    out = {}
    for loc, typ, fam, nat, lines, value, neg, use, nouse, excess, tw, cw, dw, count in t["rows"]:
        o = out.setdefault(loc, {"stock": 0.0, "fb_stock": 0.0, "fb_use": 0.0, "count": 0, "slow": 0.0,
                                 "neg_lines": 0, "neg_value": 0.0, "count_top": []})
        o["stock"] += value
        if nat in ("ingredients", "resale"):
            o["fb_stock"] += value
            o["fb_use"] += use
        o["count"] += count
    for L in r.get("locations") or []:
        o = out.get(L["location"])
        if not o:
            continue
        o["slow"] = sum(L.get(k, [0, 0])[1] for k in ("SLOW MOVING", "NON-MOVING", "DEAD STOCK"))
        o["neg_lines"], o["neg_value"] = L.get("NEGATIVE", [0, 0])[0], L.get("NEGATIVE", [0, 0])[1]
    for x in t["items"]:
        if x[15] and x[0] in out and len(out[x[0]]["count_top"]) < 3:
            out[x[0]]["count_top"].append([x[2] or x[1], x[4], x[8], x[10]])
    for o in out.values():
        daily = o["fb_use"] / days if days else 0
        o["doh"] = round(o["fb_stock"] / daily) if daily > 0 else None
        for k in ("stock", "fb_stock", "fb_use", "slow", "neg_value"):
            o[k] = round(o[k], 0)
    return "ready", f"Stock as of {t['as_of']} · {days} days of history", {"as_of": t["as_of"], "by_location": out}


# ---------------------------------------------------------------- the job
_jobs, _lock = {}, threading.Lock()
KEEP = 900


def request(client, company, period, latest_fn, parallel_fn, rights, refresh=False):
    """period: (c_from, c_to, p_from, p_to, ly_from, ly_to, running, label). Runs in the background;
    the screen asks again until the result is ready."""
    key = (company or "", period[0].isoformat())
    now = time.time()
    with _lock:
        job = _jobs.get(key)
        if job and job["state"] == "running":
            return {"pending": True, "stage": job.get("stage", ""), "elapsed": int(now - job["started"])}
        res = job["result"] if job and job["state"] == "done" and not refresh and now - job["finished"] < KEEP else None
    if res is not None:
        if res.get("inventory", {}).get("state") == "pending" and rights.get("inventory"):
            _top_up_inventory(client, res)
        return _for(res, rights)
    with _lock:
        job = _jobs.get(key)
        if job and job["state"] == "running":
            return {"pending": True, "stage": job.get("stage", ""), "elapsed": int(now - job["started"])}
        if job and job["state"] == "failed" and not refresh:
            _jobs.pop(key, None)
            return {"available": False, "error": job["error"]}
        job = _jobs[key] = {"state": "running", "started": now, "stage": "Starting"}

    def progress(text):
        with _lock:
            job["stage"] = text

    def run():
        try:
            result = build(client, company, period, latest_fn, parallel_fn, progress)
            state = {"state": "done", "result": result}
        except simphony.SimphonyError as exc:
            state = {"state": "failed", "error": str(exc)}
        except Exception as exc:                                     # noqa: BLE001
            traceback.print_exc()
            state = {"state": "failed", "error": f"Location intelligence could not be read: {str(exc)[:300]}"}
        with _lock:
            _jobs[key] = dict(state, started=now, finished=time.time())
    threading.Thread(target=run, daemon=True, name="locintel").start()
    return {"pending": True, "stage": "Starting", "elapsed": 0}


def _top_up_inventory(client, res):
    try:
        state, note, inv = inventory(client, res["company_bc"], store.get_settings())
    except Exception as exc:                                         # noqa: BLE001
        state, note, inv = "failed", str(exc)[:200], None
    res["inventory"] = {"state": state, "note": note, "as_of": inv and inv["as_of"]}
    if inv:
        for l in res["locations"]:
            if l["company"] == res["company_bc"] and l["bc"]:
                l["inv"] = inv["by_location"].get(l["bc"])


def _for(res, rights):
    """What this user may see: purchases need Payables, stock needs Inventory."""
    if rights.get("purchases") and rights.get("inventory"):
        return res
    out = dict(res, locations=[dict(l) for l in res["locations"]])
    for l in out["locations"]:
        if not rights.get("purchases"):
            l["purch"] = None
        if not rights.get("inventory"):
            l["inv"] = None
            l["use"] = None
    if not rights.get("inventory"):
        out["inventory"] = {"state": "hidden", "note": "Your role does not include Inventory."}
    return out


def build(client, company, period, latest_fn, parallel_fn, progress):
    settings = store.get_settings()
    c_from, c_to, p_from, p_to, ly_from, ly_to, running, label = period[:8]
    t0 = time.time()
    reg = registry(settings)
    group_ccy = norm_currency((settings.get("group") or {}).get("reporting_currency")) or "ALL"
    # whole days only: a running month stops yesterday, and the comparisons are cut to the same
    # number of days, so every period is like for like
    if running:
        c_to = min(c_to, date.today() - timedelta(days=1))
    if c_to < c_from:
        c_to = c_from
    n = (c_to - c_from).days
    p_to = min(p_to, p_from + timedelta(days=n))
    ly_to = min(ly_to, ly_from + timedelta(days=n))
    errors = []
    sim, sim_latest = {}, {}
    configured = simphony.info()["configured"]
    if configured:
        sim, errs, sim_latest = _simphony(reg, (c_from, c_to), (p_from, p_to), (ly_from, ly_to),
                                          latest_fn, parallel_fn, progress)
        errors += errs
    # which companies to read from Business Central
    companies = sorted({r["company"] for r in reg if r["company"]})
    try:
        known = set(client.companies())
    except Exception:                                                # noqa: BLE001
        known = set(companies)
    scope = [company] if company and company in known else [c for c in companies if c in known][:1]
    company_bc = scope[0] if scope else ""
    posted, purch, use = {}, {}, {}
    if company_bc:
        customers = sorted({r["customer"] for r in reg if r["company"] == company_bc and r["customer"]})
        progress(f"{company_bc} · sales posted in BC")
        try:
            posted = _posted_sales(client, company_bc, customers, (c_from, c_to))
        except Exception as exc:                                     # noqa: BLE001
            errors.append(f"Posted sales: {str(exc)[:200]}")
    has_bc = bool(company_bc) and any(r["bc"] for r in reg if r["company"] == company_bc)
    if has_bc:
        progress(f"{company_bc} · purchase invoices")
        try:
            purch = _purchases(client, company_bc, (c_from, c_to))
        except Exception as exc:                                     # noqa: BLE001
            errors.append(f"Purchase invoices: {str(exc)[:200]}")
        progress(f"{company_bc} · use at cost (item ledger)")
        try:
            use = _use_at_cost(client, company_bc, (c_from, c_to))
        except Exception as exc:                                     # noqa: BLE001
            errors.append(f"Use at cost: {str(exc)[:200]}")
    days = _days(c_from, c_to)
    rows = []
    for r in reg:
        s = sim.get(r["ref"])
        mine = r["company"] == company_bc
        cust = posted.get(r["customer"]) if mine and r["customer"] else None
        row = dict(r, registered=True,
                   currency=r["currency"] or (s and s["currency_given"]) or group_ccy,
                   sim={k: s[k] for k in ("cur", "prev", "ly")} if s else None,
                   daily=s["daily"] if s else None,
                   latest=s and s["latest"],
                   bc_posted=None, purch=None, use=None, inv=None)
        if cust is not None:
            unposted = [days[i] for i, v in enumerate(cust) if not v and s and s["daily"]["net"][i] > 0]
            row["bc_posted"] = {"total": round(sum(cust), 0), "daily": cust,
                                "days_posted": sum(1 for v in cust if v), "unposted": unposted,
                                "last": max((days[i] for i, v in enumerate(cust) if v), default=None)}
        if mine and r["bc"]:
            row["purch"] = purch.get(r["bc"]) or {"amount": 0, "n": 0, "fx": 0, "suppliers": []}
            row["use"] = use.get(r["bc"])
        rows.append(row)
    known_refs = {r["ref"] for r in reg}
    for ref, s in sim.items():
        if ref in known_refs or not any(s[p]["net"] for p in ("cur", "prev", "ly")):
            continue
        rows.append({"ref": ref, "name": s["name"], "brand": s["name"].split()[0].upper() if s["name"] else ref,
                     "company": "", "bc": "", "customer": "", "area": "", "region": "", "x": None, "y": None,
                     "seasonal": False, "registered": False, "currency": s["currency_given"] or group_ccy,
                     "sim": {k: s[k] for k in ("cur", "prev", "ly")}, "daily": s["daily"], "latest": s["latest"],
                     "bc_posted": None, "purch": None, "use": None, "inv": None})
    for row in rows:
        s = row["sim"]
        active = bool(s and s["cur"]["net"]) or bool(row["bc_posted"] and row["bc_posted"]["total"])
        before = bool(s and (s["prev"]["net"] or s["ly"]["net"]))
        row["status"] = "open" if active else "closed" if before or row["seasonal"] else "no sales"
    inv_state = {"state": "n/a", "note": "No BC location in the registry for this company, so no stock is read."
                 if company_bc else "No Business Central company in scope."}
    result = {
        "available": True, "company": company_bc or company or "", "company_bc": company_bc,
        "companies": companies, "simphony": configured, "group_currency": group_ccy,
        "period": {"from": c_from.isoformat(), "to": c_to.isoformat(), "label": label, "running": running,
                   "prev_from": p_from.isoformat(), "prev_to": p_to.isoformat(),
                   "ly_from": ly_from.isoformat(), "ly_to": ly_to.isoformat(), "days": days},
        "locations": rows, "inventory": inv_state, "thresholds": thresholds(settings),
        "errors": errors, "checked_at": datetime.now().isoformat(timespec="seconds"),
        "ms": int((time.time() - t0) * 1000),
        "definitions": DEFINITIONS,
    }
    if has_bc:
        progress(f"{company_bc} · stock position")
        _top_up_inventory(client, result)
    return result


DEFINITIONS = {
    "net": "Simphony net sales (netSlsTtl): what guests paid after item and check discounts, before "
           "service charge. VAT is inside this figure; ex VAT = net − tax collected.",
    "posted": "Sales posted in Business Central for the restaurant's POS customer: invoices less credit "
              "memos in the customer ledger, VAT included, so they compare with Simphony net sales.",
    "food_cost": "Use at cost of the month (sales and assembly consumption net of assembly output, item "
                 "ledger at item cost) ÷ Simphony sales ex VAT. An estimate: waste and staff meals posted "
                 "as adjustments are not in it.",
    "cover": "Days of cover = food, beverage and resale stock at item cost ÷ its average daily use over the "
             "Movement history.",
    "purchases": "Posted purchase invoices with this location, VAT included. Most goods reach the "
                 "restaurants from the warehouse by transfer, so this is the direct buying only.",
    "period": "Whole days only: a running month stops yesterday. The month before and last year are cut "
              "to the same number of days.",
    "guests": "Covers keyed at the POS. Spend per guest is shown only where covers are keyed on every check "
              "(guests ≥ checks).",
}
