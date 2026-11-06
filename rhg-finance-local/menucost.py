"""Sales → Menu & food cost: the weekly food cost % the menu should have had (theoretical: what was sold at the cost
of its recipe), against the month's food cost % the P&L shows (actual), for a brand, a location or all of RHG.

Theoretical, by week
  * the items sold and the net sales of every menu item and business day, from the same Simphony store as Menu
    engineering (menueng: getMenuItemDailyTotals, kept in data/menu_items.db; the days not held are read first);
  * the portion cost of each item as Menu engineering costs it (BC recipe, or the item's unit cost for an item
    sold as bought, else Simphony's preparation cost), at today's unit costs: a week's figure moves with the mix and
    the prices sold, not with what the ingredients cost that week;
  * food cost % = Σ items sold × portion cost / net sales of the items that carry a cost; the share of sales with
    a cost is given beside it. POS technical items stay out, as in Menu engineering.
Actual, by month
  * the cost-of-goods and revenue lines of the KPI mapping, as posted in Business Central, read by the Executive
    dashboard (executive.py) for the company of the selection: the sum of the departments of its locations, or
    the company line for all of a company's locations. Only final once the month's inventory is posted.
The gap between the two is what the menu does not explain: waste, staff meals, portions, stock differences,
prices bought above the unit cost of the item card, or a recipe that is wrong. Nothing is written anywhere.
"""
from datetime import date, timedelta

import executive
import locintel
import menueng
import store


def trend_period(weeks, today=None):
    """(from, to, …, label): whole weeks back from the week of yesterday, the shape menueng.build reads."""
    today = today or date.today()
    end = today - timedelta(days=1)
    start = end - timedelta(days=end.weekday()) - timedelta(days=7 * (weeks - 1))
    label = f"{weeks} weeks to {end:%d %b %Y}"
    return (start, end, None, None, None, None, True, label)


def _select(locs, level, key):
    if level == "brand":
        return [L for L in locs if (L["brand"] or "").upper() == (key or "").upper()]
    if level == "location":
        return [L for L in locs if L["ref"] == key]
    return list(locs)


def weekly(ds, level="all", key="", settings=None):
    settings = settings or store.get_settings()
    S = menueng.settings_of(settings)
    reporting, rates = menueng._rates(settings)
    sel = _select(ds["locations"], level, key)
    curs = {L["currency"] for L in sel}
    convert = len(curs) > 1
    currency = reporting if convert else (next(iter(curs)) if curs else reporting)
    a, b = ds["period"]["from"], ds["period"]["to"]
    # the portion cost of every item of every location, as menueng.build costed it for this period
    cost = {(ref, r["mi"]): (r["unit_cost"], r["technical"]) for ref, lst in ds["rows"].items() for r in lst}
    refs = [L["ref"] for L in sel]
    fx = {L["ref"]: (rates.get(L["currency"]) if convert else 1.0) for L in sel}
    weeks, months, by_loc, skipped = {}, {}, {}, sorted(L["ref"] for L in sel if fx[L["ref"]] is None)
    if refs:
        with menueng._db_lock:
            q = menueng._db().execute(
                "SELECT ref, day, mi, SUM(net), SUM(qty) FROM mio WHERE day>=? AND day<=? AND ref IN (%s) "
                "GROUP BY ref, day, mi" % ",".join("?" * len(refs)), (a, b, *refs)).fetchall()
    else:
        q = []
    for ref, day, mi, net, qty in q:
        f = fx.get(ref)
        c = cost.get((ref, mi))
        if f is None or c is None or c[1] or not qty or qty <= 0:
            continue                                       # another currency without a rate, excluded or technical
        d = date.fromisoformat(day)
        wk = (d - timedelta(days=d.weekday())).isoformat()
        w = weeks.setdefault(wk, {"net": 0.0, "net_costed": 0.0, "cost": 0.0, "days": set()})
        mo = months.setdefault(day[:7], {"net": 0.0, "net_costed": 0.0, "cost": 0.0, "days": set()})
        mo["days"].add(day)
        mo["net"] += (net or 0) * f
        l_ = by_loc.setdefault(ref, {"net": 0.0, "net_costed": 0.0, "cost": 0.0})
        w["days"].add(day)
        w["net"] += (net or 0) * f
        l_["net"] += (net or 0) * f
        if c[0] is not None:
            w["net_costed"] += (net or 0) * f
            w["cost"] += c[0] * qty * f
            mo["net_costed"] += (net or 0) * f
            mo["cost"] += c[0] * qty * f
            l_["net_costed"] += (net or 0) * f
            l_["cost"] += c[0] * qty * f
    pct = lambda x: round(x["cost"] / x["net_costed"] * 100, 1) if x["net_costed"] > 0 else None
    share = lambda x: round(x["net_costed"] / x["net"] * 100, 1) if x["net"] > 0 else None
    out_weeks = [{"week": k, "to": min((date.fromisoformat(k) + timedelta(days=6)).isoformat(), b),
                  "days": len(v["days"]), "net": round(v["net"], 0), "cost": round(v["cost"], 0),
                  "cost_pct": pct(v), "costed_share": share(v)} for k, v in sorted(weeks.items())]
    # the months the weeks touch, with the days of each held: a month cut by the start of the weeks is partial
    out_months = [{"month": k, "days": len(v["days"]), "net": round(v["net"], 0), "cost_pct": pct(v),
                   "costed_share": share(v)} for k, v in sorted(months.items())]
    names = {L["ref"]: L["name"] for L in sel}
    locations = sorted(({"ref": r, "name": names.get(r, r), "net": round(v["net"], 0), "cost_pct": pct(v),
                         "costed_share": share(v)} for r, v in by_loc.items()), key=lambda x: -x["net"])
    tot = {k: sum(v[k] for v in weeks.values()) for k in ("net", "net_costed", "cost")}
    return {"period": ds["period"], "currency": currency, "converted": convert, "skipped": skipped,
            "target": S["target"], "weeks": out_weeks, "months": out_months, "locations": locations,
            "total": {"net": round(tot["net"], 0), "cost_pct": pct(tot), "costed_share": share(tot)}, "selection": sel}


def actual(client, sel, level, from_day, currency_of, today=None, limited=False):
    """{available, months: [{month, sales, food, cost_pct, provisional}], …} from the Executive dashboard's months
    of the one company of the selection; its read starts if it is not held (the same as opening that screen)."""
    today = today or date.today()
    reg = {r["ref"]: r for r in locintel.registry()}
    companies = {(reg.get(L["ref"]) or {}).get("company") or L.get("company") or "" for L in sel}
    companies.discard("")
    if len(companies) != 1:
        return {"available": False, "reason": "The P&L food cost is shown for the locations of one company: choose a "
                                              "brand or a location."}
    company = next(iter(companies))
    whole = level == "all" or {L["ref"] for L in sel} == {r["ref"] for r in reg.values() if r.get("company") == company}
    if whole and not limited:                    # a person limited to some locations never reads the company line
        depts, line = None, "company"
    else:
        depts = {((reg.get(L["ref"]) or {}).get("dept") or "").upper() for L in sel}
        if "" in depts:
            return {"available": False, "company": company,
                    "reason": "A location of the selection has no department in Settings → Locations, so its P&L "
                              "cannot be cut."}
        line = "departments " + ", ".join(sorted(depts))
    months, pending, provisional, cur, seen = {}, False, set(), currency_of(company), set()
    for year in [today.year] if from_day.year >= today.year - 1 else sorted({from_day.year, today.year}):
        r = executive.request(client, company, year, cur)       # it holds the year before as well
        pending = pending or bool(r.get("pending"))
        provisional |= set(r.get("provisional") or [])
        acc = r.get("accounts") or {}
        for dept, by_month in (r.get("actual") or {}).items():
            if (depts is None and dept != "*") or (depts is not None and dept.upper() not in depts):
                continue
            for m, row in by_month.items():
                if "~" in m or m < from_day.strftime("%Y-%m") or (dept, m) in seen:
                    continue
                seen.add((dept, m))                      # a month two reads both hold counts once
                x = months.setdefault(m, {"sales": 0.0, "food": 0.0})
                for no, v in row.items():
                    bk = (acc.get(no) or [None, None])[1]
                    if bk in ("sales", "food"):
                        x[bk] += v
    out = [{"month": m, "sales": round(v["sales"], 0), "food": round(v["food"], 0),
            "cost_pct": round(v["food"] / v["sales"] * 100, 1) if v["sales"] > 0 else None,
            "provisional": m in provisional} for m, v in sorted(months.items())]
    return {"available": True, "company": company, "currency": cur, "line": line, "pending": pending, "months": out}
