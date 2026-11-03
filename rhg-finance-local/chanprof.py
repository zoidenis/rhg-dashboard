"""Sales → Channel profitability: a profit and loss of every sales channel (Simphony order type: dine in, take
away, Wolt, Glovo…) for all of RHG, a brand or a location, in the period of the filters, by channel or month by
month for one channel. The lines follow RHG's Wolt profitability template (OPA_Wolt_Perfitueshmeria.xlsx).

Where each line comes from
  * Simphony, by order type, day by day (getOrderTypeQuarterHourTotals, kept with the daily totals): orders
    (checks), net sales without VAT, discounts. Gross before discounts = net sales + discounts.
  * Cost of goods: the menu items sold under each order type (getMenuItemDailyTotals with its order type, kept
    in data/menu_items.db) × the portion cost from Business Central (recipe or item cost, as Menu engineering),
    raised by the waste % of the parameters. Items with no cost take the cost ratio of the items with one.
  * Business Central, when accounts are given in the parameters: the net change of those G/L accounts on the
    location's department in each month (commission, ads, other fees, packaging per channel; the location's
    shared costs, allocated to its channels by net sales).
  * The parameters (data/channel_costs.json, set in the tab): commission % (on sales with VAT or without),
    fee per order, refunds %, other income, ads, campaigns and other fees per month, non-recoverable VAT on the
    fees, packaging, variable labour and transport per order, other variable %, dedicated fixed costs per
    month. Amounts per month are per location and count a part of the month for a part of a month.
Nothing is assumed: a delivery channel with no commission is flagged, not given a rate. Simphony and Business
Central are only read.
"""
import calendar
import io
import json
import math
import re
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

import config
import locintel
import menueng
import recipes
import simphony
import store

CONFIG_FILE = store.DATA_DIR / "channel_costs.json"
KEEP_SECONDS = 900
# key, label, unit
CHANNEL_FIELDS = [
    ("commission_pct", "Commission", "%"), ("commission_base", "Commission charged on", "base"),
    ("fee_per_order", "Fee per order", "per order"), ("refunds_pct", "Refunds charged to the restaurant", "% of gross"),
    ("other_income_month", "Other income from the channel", "per month"),
    ("ads_month", "Ads", "per month"), ("campaign_month", "Extra campaigns", "per month"),
    ("other_fees_month", "Other channel fees", "per month"),
    ("fees_vat_nonrec_pct", "Non-recoverable VAT on the channel's fees", "%"),
    ("packaging_per_order", "Packaging", "per order"), ("labour_per_order", "Variable labour", "per order"),
    ("transport_per_order", "Transport / delivery paid by the restaurant", "per order"),
    ("other_var_pct", "Other variable costs", "% of net sales"),
    ("fixed_month", "Dedicated fixed costs", "per month"),
    ("gl_commission", "BC accounts: commission", "accounts"), ("gl_ads", "BC accounts: ads", "accounts"),
    ("gl_other_fees", "BC accounts: other fees", "accounts"), ("gl_packaging", "BC accounts: packaging", "accounts"),
]
GL_FIELDS = ("gl_commission", "gl_ads", "gl_other_fees", "gl_packaging")
DEFAULT_CHANNEL = {k: ("" if u == "accounts" else "vat" if u == "base" else 0.0) for k, _l, u in CHANNEL_FIELDS}
DEFAULT_CONFIG = {"vat_pct": 20.0, "waste_pct": 0.0, "allocate_shared": False, "shared_accounts": "", "channels": {}}
_lock, _cfg_lock = threading.Lock(), threading.Lock()
_jobs = {}


# ---------------------------------------------------------------- parameters
def _num(v, lo=0.0, hi=1e12):
    try:
        x = float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return 0.0
    return max(lo, min(hi, x)) if math.isfinite(x) else 0.0


def _accounts(text):
    """G/L account numbers or prefixes: "6271, 6272" (letters, digits, - and . only)."""
    return ", ".join(dict.fromkeys(a for a in (re.sub(r"[^A-Za-z0-9.\-]", "", p) for p in re.split(r"[,;\s]+", str(text or ""))) if a))


def channel_key(name):
    return re.sub(r"\s+", " ", str(name or "")).strip().upper()


def load_config():
    with _cfg_lock:
        try:
            raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
    cfg = dict(DEFAULT_CONFIG, **{k: raw[k] for k in DEFAULT_CONFIG if k in raw})
    cfg["channels"] = {k: dict(DEFAULT_CHANNEL, **v) for k, v in (raw.get("channels") or {}).items()}
    cfg["updated_at"], cfg["updated_by"] = raw.get("updated_at"), raw.get("updated_by")
    return cfg


def save_config(patch, actor):
    cfg = load_config()
    for k in ("vat_pct", "waste_pct"):
        if k in patch:
            cfg[k] = _num(patch[k], 0, 100)
    if "allocate_shared" in patch:
        cfg["allocate_shared"] = bool(patch["allocate_shared"])
    if "shared_accounts" in patch:
        cfg["shared_accounts"] = _accounts(patch["shared_accounts"])[:400]
    for name, vals in (patch.get("channels") or {}).items():
        key = channel_key(name)[:60]
        if not key or not isinstance(vals, dict):
            continue
        ch = dict(DEFAULT_CHANNEL, **cfg["channels"].get(key, {}))
        for f, _l, unit in CHANNEL_FIELDS:
            if f not in vals:
                continue
            if unit == "accounts":
                ch[f] = _accounts(vals[f])[:200]
            elif unit == "base":
                ch[f] = "net" if vals[f] == "net" else "vat"
            else:
                ch[f] = _num(vals[f], 0, 100 if "%" in unit else 1e12)
        cfg["channels"][key] = ch
    cfg["updated_at"], cfg["updated_by"] = time.strftime("%Y-%m-%dT%H:%M:%S"), actor
    with _cfg_lock:
        tmp = CONFIG_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(CONFIG_FILE)
    store.audit("settings", "channel profitability parameters", actor, {}, {}, ", ".join(sorted((patch.get("channels") or {}).keys()))[:200])
    return load_config()


def bc_prefixes(cfg):
    out = set()
    for ch in cfg["channels"].values():
        for f in GL_FIELDS:
            out |= {a for a in (ch.get(f) or "").replace(" ", "").split(",") if a}
    if cfg.get("allocate_shared"):
        out |= {a for a in (cfg.get("shared_accounts") or "").replace(" ", "").split(",") if a}
    return tuple(sorted(out))


# ---------------------------------------------------------------- the read
def _months(a, b):
    """[(month, from, to, share of the month)] of a period."""
    out, d = [], a
    while d <= b:
        last = date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])
        e = min(last, b)
        out.append((d.strftime("%Y-%m"), d, e, ((e - d).days + 1) / last.day))
        d = last + timedelta(days=1)
    return out


def build(periods, latest_fn, client, refs, prefixes, progress=None):
    c_from, c_to = periods[0], periods[1]
    label = periods[7] if len(periods) > 7 else ""
    settings = store.get_settings()
    reg = {r["ref"]: r for r in locintel.registry(settings)}
    fallback = (settings.get("group") or {}).get("reporting_currency") or "ALL"
    locs = [l for l in simphony.locations() if l.get("active", True) and (refs is None or l["locRef"].upper() in refs)]
    latest = {}
    for l in locs:
        try:
            latest[l["locRef"]] = latest_fn(l["locRef"])
        except Exception:                                          # noqa: BLE001
            latest[l["locRef"]] = None
    closed = [v for v in latest.values() if v]
    if closed and date.fromisoformat(max(closed)) < c_to:
        c_to = max(c_from, date.fromisoformat(max(closed)))
    months = _months(c_from, c_to)
    days = [(c_from + timedelta(days=i)).isoformat() for i in range((c_to - c_from).days + 1)]
    jobs = {}
    for l in locs:
        ref, last = l["locRef"], latest.get(l["locRef"])
        for d in days:
            if last and d > last:
                continue
            jobs[("Q", ref, d)] = (lambda r=ref, dd=d, lt=last: simphony.order_type_hours(r, dd, lt))
            jobs[("M", ref, d)] = (lambda r=ref, dd=d, lt=last: menueng.read_day(r, dd, lt))
    out_locs = []
    for l in locs:
        rg = reg.get(l["locRef"]) or {}
        out_locs.append({"ref": l["locRef"], "name": rg.get("name") or l.get("name") or l["locRef"],
                         "brand": rg.get("brand") or "", "company": rg.get("company") or config.BC_COMPANY,
                         "dept": rg.get("dept") or rg.get("bc") or "",
                         "currency": rg.get("currency") or locintel.norm_currency(l.get("currency")) or fallback})
    bc_jobs = {}
    if prefixes:
        for L in out_locs:
            if not L["dept"]:
                continue
            for m, a, b, _f in months:
                bc_jobs[("B", L["ref"], m)] = (lambda L=L, a=a, b=b: client.accounts(L["company"], a.isoformat(), b.isoformat(),
                                                                                    L["dept"], 3600))
    total, done, results, errors = len(jobs) + len(bc_jobs), 0, {}, []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = {pool.submit(fn): k for k, fn in {**jobs, **bc_jobs}.items()}
        for f in as_completed(futs):
            k = futs[f]
            try:
                results[k] = f.result()
            except Exception as exc:                               # noqa: BLE001
                if len(errors) < 8:
                    errors.append(f"{k[1]} {k[2]}: {str(exc)[:140]}")
            done += 1
            if progress and (done % 10 == 0 or done == total):
                progress(done, total)
    simphony.flush_days()
    costers, sim, bc = {}, {}, {}
    for L in out_locs:
        ref = L["ref"]
        try:
            ots = simphony.order_type_dims(ref)
        except Exception as exc:                                   # noqa: BLE001
            ots = {}
            errors.append(f"{ref}: order types: {str(exc)[:120]}")

        def chan(ot):
            n = ots.get(int(ot)) if str(ot).lstrip("-").isdigit() and int(ot) else None
            return channel_key(n or ("No order type" if not ot or str(ot) == "0" else f"Order type {ot}"))
        month_of = {d: m for m, a, b, _f in months for d in days if a.isoformat() <= d <= b.isoformat()}
        for d in days:
            q = results.get(("Q", ref, d))
            for k, v in (q or {}).items():
                ot = k.split("|")[1]
                x = sim.setdefault((ref, chan(ot), month_of[d]), [0.0, 0.0, 0, 0.0, 0.0, 0.0])
                x[0] += v[0]
                x[1] += v[1]
                x[2] += v[2]
        company = L["company"]
        if company not in costers:
            try:
                costers[company] = menueng._coster(recipes.master(client, company))
            except Exception as exc:                               # noqa: BLE001
                errors.append(f"BC recipes of {company} could not be read: {str(exc)[:140]}")
                costers[company] = lambda it: (None, "BC not read")
        for d, ot, mi, net, qty, prep in menueng.items_by_order_type(ref, c_from.isoformat(), c_to.isoformat()):
            uc, _basis = costers[company](str(mi))
            if uc is None and prep and qty:
                uc = prep / qty
            x = sim.setdefault((ref, chan(ot), month_of.get(d, d[:7])), [0.0, 0.0, 0, 0.0, 0.0, 0.0])
            x[5] += net or 0
            if uc is not None:
                x[3] += uc * (qty or 0)
                x[4] += net or 0
        for m, a, b, _f in months:
            rows = results.get(("B", ref, m))
            if rows is None:
                continue
            amounts = {}
            for p in prefixes:
                amounts[p] = round(sum(float(r.get("Net_Change") or 0) for r in rows
                                       if str(r.get("No") or "").startswith(p) and (r.get("Account_Type") or "Posting") == "Posting"), 2)
            bc[(ref, m)] = amounts
    return {"period": {"from": c_from.isoformat(), "to": c_to.isoformat(), "label": label, "days": len(days)},
            "months": [{"month": m, "from": a.isoformat(), "to": b.isoformat(), "share": round(f, 4)} for m, a, b, f in months],
            "locations": out_locs, "sim": [[*k, *v] for k, v in sim.items()],
            "bc": [[k[0], k[1], v] for k, v in bc.items()], "prefixes": list(prefixes), "errors": errors,
            "read_at": time.strftime("%Y-%m-%dT%H:%M:%S")}


def request(periods, latest_fn, client, key, refs=None, prefixes=(), refresh=False):
    now = time.time()
    with _lock:
        job = _jobs.get(key)
        if job and job["state"] == "running":
            return {"pending": True, "done": job.get("done", 0), "total": job.get("total", 0)}
        if job and job["state"] == "done" and not refresh and now - job["finished"] < KEEP_SECONDS:
            return job["result"]
        if job and job["state"] == "failed" and not refresh:
            _jobs.pop(key, None)
            return {"available": False, "error": job["error"]}
        job = _jobs[key] = {"state": "running", "started": now, "done": 0, "total": 0}

    def progress(done, total):
        with _lock:
            job.update(done=done, total=total)

    def run():
        try:
            state = {"state": "done", "result": build(periods, latest_fn, client, refs, prefixes, progress)}
        except simphony.SimphonyError as exc:
            state = {"state": "failed", "error": str(exc)}
        except Exception as exc:                                   # noqa: BLE001
            traceback.print_exc()
            state = {"state": "failed", "error": f"The channels could not be read: {str(exc)[:300]}"}
        with _lock:
            _jobs[key] = dict(state, started=now, finished=time.time())
            for k in [k for k, v in _jobs.items() if v.get("state") == "done" and time.time() - v.get("finished", 0) > 3 * KEEP_SECONDS]:
                _jobs.pop(k, None)
    threading.Thread(target=run, daemon=True, name="channel-profit").start()
    return {"pending": True, "done": 0, "total": 0}


# ---------------------------------------------------------------- the profit and loss
LINES = [
    ("orders", "Completed orders (checks)", "n", "Revenue"),
    ("gross", "Gross sales before discounts, without VAT", "m", "Revenue"),
    ("disc", "Discounts funded by the restaurant", "m", "Revenue"),
    ("refunds", "Refunds charged to the restaurant", "m", "Revenue"),
    ("other_income", "Other income from the channel", "m", "Revenue"),
    ("net", "NET SALES", "M", "Revenue"),
    ("commission", "Commission", "m", "Channel costs"),
    ("ads", "Ads", "m", "Channel costs"),
    ("campaigns", "Extra campaigns", "m", "Channel costs"),
    ("other_fees", "Other channel fees", "m", "Channel costs"),
    ("fees_vat", "Non-recoverable VAT on the fees", "m", "Channel costs"),
    ("channel_costs", "TOTAL CHANNEL COSTS", "M", "Channel costs"),
    ("cogs", "Cost of goods, waste included", "m", "Product and service"),
    ("packaging", "Packaging", "m", "Product and service"),
    ("labour", "Variable labour", "m", "Product and service"),
    ("transport", "Transport / delivery paid by the restaurant", "m", "Product and service"),
    ("other_var", "Other variable costs", "m", "Product and service"),
    ("variable", "TOTAL VARIABLE COSTS, channel included", "M", "Product and service"),
    ("contribution", "CONTRIBUTION AFTER VARIABLE COSTS", "M", "Result"),
    ("contribution_pct", "Contribution margin", "%", "Result"),
    ("fixed", "Dedicated fixed costs", "m", "Result"),
    ("shared", "Shared costs allocated", "m", "Result"),
    ("result", "RESULT AFTER FULL COSTS", "M", "Result"),
    ("result_pct", "Margin after full costs", "%", "Result"),
    ("net_per_order", "Net sales per order", "m", "Indicators"),
    ("commission_share", "Commission / net sales", "%", "Indicators"),
    ("cogs_share", "Cost of goods / net sales", "%", "Indicators"),
    ("packaging_per_order", "Packaging per order", "m", "Indicators"),
    ("contribution_per_order", "Contribution per order", "m", "Indicators"),
    ("be_sales_dedicated", "Sales to cover the dedicated costs", "m", "Indicators"),
    ("be_orders_dedicated", "Orders to cover the dedicated costs", "n", "Indicators"),
    ("be_sales_full", "Sales to cover the full costs", "m", "Indicators"),
    ("cogs_coverage", "Item sales with a BC cost", "%", "Indicators"),
]
SUMS = ("orders", "gross", "disc", "refunds", "other_income", "commission", "ads", "campaigns", "other_fees", "fees_vat",
        "cogs", "packaging", "labour", "transport", "other_var", "fixed", "shared", "_items_all", "_items_costed")


def _derive(v):
    """The totals, margins and indicators of one column from its summed lines."""
    v["net"] = v["gross"] - v["disc"] - v["refunds"] + v["other_income"]
    v["channel_costs"] = v["commission"] + v["ads"] + v["campaigns"] + v["other_fees"] + v["fees_vat"]
    v["variable"] = v["channel_costs"] + v["cogs"] + v["packaging"] + v["labour"] + v["transport"] + v["other_var"]
    v["contribution"] = v["net"] - v["variable"]
    v["result"] = v["contribution"] - v["fixed"] - v["shared"]
    n, o = v["net"], v["orders"]
    v["contribution_pct"] = v["contribution"] / n * 100 if n > 0 else None
    v["result_pct"] = v["result"] / n * 100 if n > 0 else None
    v["net_per_order"] = n / o if o else None
    v["commission_share"] = v["commission"] / n * 100 if n > 0 else None
    v["cogs_share"] = v["cogs"] / n * 100 if n > 0 else None
    v["packaging_per_order"] = v["packaging"] / o if o else None
    v["contribution_per_order"] = v["contribution"] / o if o else None
    cm = v["contribution_pct"]
    v["be_sales_dedicated"] = v["fixed"] / (cm / 100) if cm and cm > 0 and v["fixed"] else None
    cpo = v["contribution_per_order"]
    v["be_orders_dedicated"] = math.ceil(v["fixed"] / cpo) if cpo and cpo > 0 and v["fixed"] else None
    v["be_sales_full"] = (v["fixed"] + v["shared"]) / (cm / 100) if cm and cm > 0 and (v["fixed"] + v["shared"]) else None
    v["cogs_coverage"] = v["_items_costed"] / v["_items_all"] * 100 if v["_items_all"] else None
    return v


def analyse(ds, cfg, level="all", key="", view="channel", channel="", settings=None):
    settings = settings or store.get_settings()
    reporting, rates = menueng._rates(settings)
    locs = ds["locations"]
    if level == "brand":
        sel = [L for L in locs if (L["brand"] or "").upper() == (key or "").upper()]
        title = f"{key} (brand)"
    elif level == "location":
        sel = [L for L in locs if L["ref"] == key]
        title = sel[0]["name"] if sel else key
    else:
        sel, title, level = list(locs), "All RHG", "all"
    curs = {L["currency"] for L in sel}
    convert = len(curs) > 1
    currency = reporting if convert else (next(iter(curs)) if curs else reporting)
    refs = {L["ref"]: L for L in sel if not convert or L["currency"] in rates}
    words = [w.strip().lower() for w in ((settings.get("locations") or {}).get("delivery_order_types") or "deliver, wolt, glovo").replace(";", ",").split(",") if w.strip()]
    share = {m["month"]: m["share"] for m in ds["months"]}
    vat = float(cfg.get("vat_pct") or 0) / 100
    waste = float(cfg.get("waste_pct") or 0) / 100
    bc = {(r, m): v for r, m, v in ds["bc"]}
    shared_pf = [a for a in (cfg.get("shared_accounts") or "").replace(" ", "").split(",") if a] if cfg.get("allocate_shared") else []
    loc_month_net = {}
    for ref, ch, m, net, disc, orders, cogs, ic, ia in ds["sim"]:
        if ref in refs:
            loc_month_net[(ref, m)] = loc_month_net.get((ref, m), 0.0) + net

    def accts(text):
        return [a for a in (text or "").replace(" ", "").split(",") if a]

    cells, used_bc, channels = {}, {}, {}
    for ref, ch, m, net, disc, orders, cogs, items_costed, items_all in ds["sim"]:
        L = refs.get(ref)
        if L is None or (not net and not orders and not items_all):
            continue
        f = rates.get(L["currency"], 1.0) if convert else 1.0
        c = dict(DEFAULT_CHANNEL, **cfg["channels"].get(ch, {}))
        channels[ch] = channels.get(ch, 0.0) + net * f
        fr = share.get(m, 1.0)
        gross = net + disc
        v = {k: 0.0 for k in SUMS}
        v["orders"] = orders
        v["gross"], v["disc"] = gross * f, disc * f
        refunds = c["refunds_pct"] / 100 * gross
        v["refunds"] = refunds * f
        v["other_income"] = c["other_income_month"] * fr
        base = net * (1 + vat) if c["commission_base"] == "vat" else net
        src = {}

        def from_bc(field, line, fallback):
            a = accts(c.get(field))
            if a and (ref, m) in bc:
                amt = sum((bc[(ref, m)] or {}).get(p, 0.0) for p in a)
                used_bc.setdefault(line, set()).update(a)
                src[line] = "BC"
                return amt * f
            return fallback
        v["commission"] = from_bc("gl_commission", "commission", (c["commission_pct"] / 100 * base + c["fee_per_order"] * orders) * f)
        v["ads"] = from_bc("gl_ads", "ads", c["ads_month"] * fr)
        v["campaigns"] = c["campaign_month"] * fr
        v["other_fees"] = from_bc("gl_other_fees", "other_fees", c["other_fees_month"] * fr)
        v["fees_vat"] = c["fees_vat_nonrec_pct"] / 100 * (v["commission"] + v["ads"] + v["campaigns"] + v["other_fees"])
        cogs_full = cogs / items_costed * items_all if items_costed > 0 else 0.0
        v["cogs"] = cogs_full * (1 + waste) * f
        v["_items_all"], v["_items_costed"] = items_all * f, items_costed * f
        v["packaging"] = from_bc("gl_packaging", "packaging", c["packaging_per_order"] * orders * f)
        v["labour"] = c["labour_per_order"] * orders * f
        v["transport"] = c["transport_per_order"] * orders * f
        v["other_var"] = c["other_var_pct"] / 100 * (net - refunds) * f
        v["fixed"] = c["fixed_month"] * fr
        if shared_pf and (ref, m) in bc and loc_month_net.get((ref, m)):
            v["shared"] = sum((bc[(ref, m)] or {}).get(p, 0.0) for p in shared_pf) * net / loc_month_net[(ref, m)] * f
            used_bc.setdefault("shared", set()).update(shared_pf)
        col = ch if view == "channel" else m
        if view == "month" and ch != channel:
            continue
        acc = cells.setdefault(col, {k: 0.0 for k in SUMS})
        for k in SUMS:
            acc[k] += v[k]
    cols = sorted(cells, key=lambda k: -cells[k]["gross"]) if view == "channel" else sorted(cells)
    total = {k: sum(cells[c][k] for c in cols) for k in SUMS}
    out_cols = [{"key": c, "label": c.title() if view == "channel" else c, "values": _derive(dict(cells[c]))} for c in cols]
    tot = _derive(total)
    warnings = []
    names = sorted(channels, key=lambda k: -channels[k])
    for ch in names:
        c = cfg["channels"].get(ch) or {}
        delivery = any(w in ch.lower() for w in words)
        if delivery and not c.get("commission_pct") and not c.get("fee_per_order") and not c.get("gl_commission"):
            warnings.append(f"{ch.title()}: no commission set (no rate is assumed). Set it in Parameters, or the BC accounts it posts to.")
        if delivery and not c.get("packaging_per_order") and not c.get("gl_packaging"):
            warnings.append(f"{ch.title()}: no packaging cost per order.")
    if tot["cogs_coverage"] is not None and tot["cogs_coverage"] < 90:
        warnings.append(f"Only {tot['cogs_coverage']:.0f}% of the item sales have a BC cost; the rest take the same cost ratio.")
    sources = {
        "orders": "Simphony", "gross": "Simphony", "disc": "Simphony", "refunds": "Parameter", "other_income": "Parameter",
        "commission": "BC" if "commission" in used_bc else "Parameter", "ads": "BC" if "ads" in used_bc else "Parameter",
        "campaigns": "Parameter", "other_fees": "BC" if "other_fees" in used_bc else "Parameter", "fees_vat": "Parameter",
        "cogs": "Simphony × BC", "packaging": "BC" if "packaging" in used_bc else "Parameter", "labour": "Parameter",
        "transport": "Parameter", "other_var": "Parameter", "fixed": "Parameter", "shared": "BC" if "shared" in used_bc else "–"}
    return {"title": title, "level": level, "key": key, "view": view, "channel": channel, "currency": currency,
            "converted": convert, "period": ds["period"], "months": ds["months"], "read_at": ds["read_at"],
            "errors": ds["errors"], "selection": [{"ref": L["ref"], "name": L["name"]} for L in sel],
            "lines": [{"key": k, "label": lb, "kind": kd, "section": sc, "source": sources.get(k, "")} for k, lb, kd, sc in LINES],
            "columns": out_cols, "total": tot, "channels": [{"key": c, "label": c.title(), "net": round(channels[c], 0)} for c in names],
            "warnings": warnings, "bc_accounts": {k: sorted(v) for k, v in used_bc.items()},
            "config": cfg, "fields": [{"key": k, "label": lb, "unit": u} for k, lb, u in CHANNEL_FIELDS],
            "brands": sorted({L["brand"] for L in locs if L["brand"]}),
            "locations": [{"ref": L["ref"], "name": L["name"], "brand": L["brand"]} for L in locs],
            "definitions": DEFINITIONS}


DEFINITIONS = {
    "sales": "Orders, net sales without VAT and discounts by Simphony order type (the channel), day by day, to the last "
             "business day Simphony has closed. Gross before discounts = net sales + discounts.",
    "cogs": "Cost of goods = the menu items sold under the channel × their portion cost in Business Central (recipe or item "
            "cost, as in Menu engineering), raised by the waste %; items with no cost take the cost ratio of the items "
            "with one (the coverage is shown).",
    "costs": "Commission, ads, fees, packaging, labour, transport, other variable and fixed costs come from the parameters, "
             "or, where G/L accounts are given, from the net change of those accounts on the location's department in "
             "Business Central. Amounts per month are per location and count a part of the month for a part of a month.",
    "result": "Contribution = net sales − every variable cost, the channel's included. Result after full costs also takes "
              "the dedicated fixed costs and the location's shared costs (BC accounts in the parameters), allocated to the "
              "channels by net sales. Break-even: the dedicated (or full) costs ÷ the contribution margin, assuming it holds.",
    "template": "The lines follow RHG's Wolt profitability template; cancelled orders and the Wolt payout check need the "
                "partner's own report and are not in Simphony by order type.",
}


# ---------------------------------------------------------------- Excel
def render_xlsx(r):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    hdr, bold = PatternFill("solid", fgColor="E4F3F9"), Font(bold=True, color="0E2733")
    sec_fill = PatternFill("solid", fgColor="F3F4F4")
    fmt = {"n": '#,##0', "m": '#,##0;[Red]-#,##0', "M": '#,##0;[Red]-#,##0', "%": '0.0"%";[Red]-0.0"%"'}

    def pl(ws, title, cols):
        ws.append([title])
        ws["A1"].font = Font(bold=True, size=13, color="0E2733")
        ws.append([f"{r['title']} · {r['period']['from']} to {r['period']['to']} · {r['currency']}"
                   + (" (converted at the group rates)" if r["converted"] else "") + " · without VAT"])
        ws.append([])
        ws.append(["Line", "Source", *[c["label"] for c in cols], "TOTAL"])
        for c in ws[4]:
            c.fill, c.font = hdr, bold
        sec = None
        for ln in r["lines"]:
            if ln["section"] != sec:
                sec = ln["section"]
                ws.append([sec.upper()])
                ws.cell(ws.max_row, 1).font = bold
                for i in range(1, len(cols) + 4):
                    ws.cell(ws.max_row, i).fill = sec_fill
            vals = [c["values"].get(ln["key"]) for c in cols]
            tv = r["total"].get(ln["key"])
            ws.append([ln["label"], ln["source"], *[None if v is None else round(v, 2) for v in vals],
                       None if tv is None else round(tv, 2)])
            row = ws.max_row
            if ln["kind"] == "M":
                for i in range(1, len(cols) + 4):
                    ws.cell(row, i).font = bold
            for i in range(3, len(cols) + 4):
                ws.cell(row, i).number_format = fmt[ln["kind"]]
        ws.column_dimensions["A"].width = 46
        ws.column_dimensions["B"].width = 14
        for i in range(3, len(cols) + 4):
            ws.column_dimensions[get_column_letter(i)].width = 15
        ws.freeze_panes = "C5"
    pl(wb.active, "Channel profitability" + (f" · {r['channel'].title()} by month" if r["view"] == "month" else " by channel"),
       r["columns"])
    wb.active.title = "Profit and loss"
    if r["warnings"]:
        ws = wb.create_sheet("To check")
        ws.append(["To check"])
        for w in r["warnings"]:
            ws.append([w])
        ws.column_dimensions["A"].width = 120
    ws = wb.create_sheet("Parameters")
    ws.append(["Channel", *[f["label"] + (f" ({f['unit']})" if f["unit"] not in ("base", "accounts") else "") for f in r["fields"]]])
    for c in ws[1]:
        c.fill, c.font, c.alignment = hdr, bold, Alignment(wrap_text=True, vertical="top")
    cfg = r["config"]
    for ch in r["channels"]:
        v = cfg["channels"].get(ch["key"]) or DEFAULT_CHANNEL
        ws.append([ch["label"], *[v.get(f["key"]) for f in r["fields"]]])
    ws.append([])
    ws.append([f"VAT {cfg['vat_pct']}% · waste {cfg['waste_pct']}% · shared costs allocated: "
               + ("yes, accounts " + (cfg["shared_accounts"] or "none") if cfg["allocate_shared"] else "no")])
    ws.column_dimensions["A"].width = 22
    ws = wb.create_sheet("Read me")
    ws.append(["Channel profitability"])
    for t in DEFINITIONS.values():
        ws.append([t])
    ws.append(["Simphony and Business Central were only read."])
    ws.column_dimensions["A"].width = 140
    for row in ws.iter_rows(min_row=2):
        row[0].alignment = Alignment(wrap_text=True, vertical="top")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
