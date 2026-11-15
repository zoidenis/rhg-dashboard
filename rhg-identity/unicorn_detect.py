"""Unicorn's detectors: rules without AI over the morning FACTS (Finance and Purchasing), each judged with the rule in
force in the rules library (unicorn_registers). Every breach becomes a finding with its measured value, the limit,
an estimated impact in the company's currency and the figures read, so a person can check it without Unicorn.

A detector never reads a missing figure as zero: a restaurant or company without the figure is skipped. The impact
is an estimate and is labelled so (its formula is in the finding's evidence).

metric(facts, rule, company, location) gives the same measure again on a later morning: the action tracker uses
it to follow the KPI of an approved decision against its baseline.
"""
import unicorn_registers as reg

# rule → (capability, title template, unit)
RULES = {
    "sales.drop_vs_last_year": ("restaurant", "Shitjet ranë {pct}% kundër vitit të kaluar", "%"),
    "sales.mtd_vs_budget": ("restaurant", "Shitjet e muajit {pct}% nën buxhet", "%"),
    "sales.discounts_pct": ("restaurant", "Zbritjet {v}% e shitjeve bruto", "%"),
    "sales.voids_pct": ("restaurant", "Void-et {v}% e shitjeve bruto", "%"),
    "pl.food_vs_budget": ("financial", "Food cost {v}% kundër {b}% në buxhet", "%"),
    "pl.labor_vs_budget": ("financial", "Labour cost {v}% kundër {b}% në buxhet", "%"),
    "pl.ebitda_vs_budget": ("financial", "EBITDA {pct}% nën buxhet", "%"),
    "apar.payables_beyond_critical": ("financial", "Detyrime përtej afatit kritik", "amount"),
    "apar.receivables_beyond_critical": ("financial", "Të arkëtueshme përtej afatit kritik", "amount"),
    "purchasing.price_mover": ("purchasing", "Çmimi i {item} +{pct}%", "%"),
    "purchasing.invoices_without_po": ("purchasing", "Fatura pa PO: {v}% e faturave", "%"),
}


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _sev(x, crit, high, medium):
    """Severity from how far past the limit (x and the bounds in the same unit, larger = worse)."""
    if x is None:
        return "medium"
    return "critical" if x >= crit else "high" if x >= high else "medium" if x >= medium else "low"


def _limit(key, default):
    value, status, version = reg.rule_value(key, default)
    return value, {"rule": key, "rule_status": status, "rule_version": version, "rule_value": value}


def _restaurants(facts):
    return (((facts.get("finance") or {}).get("sales") or {}).get("restaurants")) or []


def detect(facts):
    """[finding dict] for the facts of one morning."""
    day = facts.get("day") or ""
    out = []
    fin = facts.get("finance") or {}
    sales = fin.get("sales") or {}
    month = (sales.get("month_from") or day)[:7]

    # ---- restaurants (Simphony): the day and the month to date
    v1, ev1 = _limit("sales.drop_vs_last_year", {"pct": -15})
    v2, ev2 = _limit("sales.mtd_vs_budget", {"pct": -10})
    v3, ev3 = _limit("sales.discounts_pct", {"max": 8})
    v4, ev4 = _limit("sales.voids_pct", {"max": 2})
    for L in _restaurants(facts):
        base = {"company": L.get("company") or "", "location": L.get("ref") or L.get("name") or "",
                "location_name": L.get("name") or "", "currency": L.get("currency") or ""}
        y, ly, pct = _num(L.get("yesterday")), _num(L.get("same_weekday_last_year")), _num(L.get("vs_last_year_pct"))
        if pct is not None and y is not None and ly and pct <= v1.get("pct", -15):
            out.append(dict(base, rule="sales.drop_vs_last_year", capability="restaurant", period=day,
                            title=RULES["sales.drop_vs_last_year"][1].format(pct=round(pct)),
                            detail=f"{base['location_name']} {day}: {y:,.0f} kundër {ly:,.0f} të së njëjtës ditë të "
                                   f"javës vitin e kaluar ({sales.get('last_year_day') or ''}).",
                            metric="vs_last_year_pct", value=round(pct, 1), limit=v1.get("pct"), unit="%",
                            impact=round(ly - y), severity=_sev(-pct, 35, 25, 0),
                            evidence=dict(ev1, yesterday=y, same_weekday_last_year=ly, last_year_day=sales.get("last_year_day"),
                                          impact_formula="shitjet e vitit të kaluar − shitjet e djeshme (pa TVSH)")))
        mtd, bud, mpct = _num(L.get("month_to_date")), _num(L.get("month_to_date_budget")), _num(L.get("mtd_vs_budget_pct"))
        if mpct is not None and mtd is not None and bud and mpct <= v2.get("pct", -10):
            out.append(dict(base, rule="sales.mtd_vs_budget", capability="restaurant", period=month,
                            title=RULES["sales.mtd_vs_budget"][1].format(pct=round(-mpct)),
                            detail=f"{base['location_name']}: {mtd:,.0f} nga {bud:,.0f} buxhet për ditët e muajit deri më {day}.",
                            metric="mtd_vs_budget_pct", value=round(mpct, 1), limit=v2.get("pct"), unit="%",
                            impact=round(bud - mtd), severity=_sev(-mpct, 30, 20, 0),
                            evidence=dict(ev2, month_to_date=mtd, month_to_date_budget=bud, day=day,
                                          impact_formula="buxheti i ditëve të muajit − shitjet e muajit deri sot")))
        for key, v, ev, fld, name in (("sales.discounts_pct", v3, ev3, "discounts_pct_of_gross", "discounts"),
                                      ("sales.voids_pct", v4, ev4, "voids_pct_of_gross", "voids")):
            share, mx = _num(L.get(fld)), v.get("max")
            if share is not None and mx is not None and y and share > mx:
                out.append(dict(base, rule=key, capability="restaurant", period=day,
                                title=RULES[key][1].format(v=round(share, 1)),
                                detail=f"{base['location_name']} {day}: {share:.1f}% kundër kufirit {mx}%.",
                                metric=fld, value=round(share, 1), limit=mx, unit="%",
                                impact=round((share - mx) / 100 * y), severity=_sev(share / mx if mx else None, 2.5, 1.6, 1),
                                evidence=dict(ev, share=share, yesterday=y,
                                              impact_formula=f"({name} % − kufiri %) × shitjet e djeshme pa TVSH (vlerësim)")))

    # ---- P&L of the latest closed month, by department
    v5, ev5 = _limit("pl.food_vs_budget", {"pts": 2})
    v6, ev6 = _limit("pl.labor_vs_budget", {"pts": 2})
    v7, ev7 = _limit("pl.ebitda_vs_budget", {"pct": -15})
    for P in fin.get("pl") or []:
        co, cur, m = P.get("company") or "", P.get("currency") or "", P.get("month") or ""
        for d in P.get("departments") or []:
            a, b = d.get("actual") or {}, d.get("budget") or {}
            loc = d.get("code") or d.get("name") or ""
            base = {"company": co, "location": loc, "location_name": d.get("name") or loc, "currency": cur, "period": m}
            s = _num(a.get("sales"))
            for key, v, ev, k in (("pl.food_vs_budget", v5, ev5, "food_pct"), ("pl.labor_vs_budget", v6, ev6, "labor_pct")):
                x, bx = _num(a.get(k)), _num(b.get(k))
                if x is not None and bx is not None and s and x - bx > v.get("pts", 2):
                    out.append(dict(base, rule=key, capability="financial",
                                    title=RULES[key][1].format(v=round(x, 1), b=round(bx, 1)),
                                    detail=f"{base['location_name']} ({co}) {m}: {x:.1f}% kundër {bx:.1f}% në buxhet, "
                                           f"shitjet {s:,.0f} {cur}.",
                                    metric=k, value=round(x, 1), limit=round(bx + v.get("pts", 2), 1), unit="%",
                                    impact=round((x - bx) / 100 * s), severity=_sev(x - bx, 6, 4, 0),
                                    evidence=dict(ev, actual=a, budget=b, month=m,
                                                  impact_formula=f"({k} aktual − {k} buxhet) × shitjet e muajit")))
            e, be = _num(a.get("ebitda")), _num(b.get("ebitda"))
            if e is not None and be and be > 0:
                pct = (e - be) / be * 100
                if pct <= v7.get("pct", -15):
                    out.append(dict(base, rule="pl.ebitda_vs_budget", capability="financial",
                                    title=RULES["pl.ebitda_vs_budget"][1].format(pct=round(-pct)),
                                    detail=f"{base['location_name']} ({co}) {m}: EBITDA {e:,.0f} kundër {be:,.0f} në buxhet.",
                                    metric="ebitda_vs_budget_pct", value=round(pct, 1), limit=v7.get("pct"), unit="%",
                                    impact=round(be - e), severity=_sev(-pct, 50, 30, 0),
                                    evidence=dict(ev7, ebitda=e, ebitda_budget=be, month=m,
                                                  impact_formula="EBITDA buxhet − EBITDA aktuale")))

    # ---- payables and receivables
    v8, ev8 = _limit("apar.payables_beyond_critical", {"min": 0})
    v9, ev9 = _limit("apar.receivables_beyond_critical", {"min": 0})
    for A in fin.get("payables_receivables") or []:
        co, cur = A.get("company") or "", A.get("currency") or ""
        for key, v, ev, side in (("apar.payables_beyond_critical", v8, ev8, "payables"),
                                 ("apar.receivables_beyond_critical", v9, ev9, "receivables")):
            S = A.get(side) or {}
            beyond, opn = _num(S.get("beyond_critical")), _num(S.get("open"))
            if beyond is not None and beyond > (v.get("min") or 0):
                share = beyond / opn * 100 if opn else None
                out.append(dict(rule=key, capability="financial", company=co, location="", location_name=co,
                                currency=cur, period=day[:7], title=RULES[key][1],
                                detail=f"{co}: {beyond:,.0f} {cur} përtej {S.get('critical_days')} ditëve, nga {opn or 0:,.0f} të hapura.",
                                metric=f"{side}_beyond_critical", value=round(beyond), limit=v.get("min") or 0, unit=cur,
                                impact=round(beyond), severity=_sev(share, 25, 10, 0),
                                evidence=dict(ev, side=side, open=opn, beyond_critical=beyond,
                                              critical_days=S.get("critical_days"), largest=S.get("largest"),
                                              impact_formula="shuma përtej afatit kritik (rrezik, jo humbje)")))

    # ---- purchasing
    pur = facts.get("purchasing") or {}
    v10, ev10 = _limit("purchasing.price_mover", {"pct": 10, "min_impact": 50000})
    v11, ev11 = _limit("purchasing.invoices_without_po", {"max": 5})
    for C in pur.get("companies") or []:
        co, cur = C.get("company") or "", C.get("currency") or ""
        for m in C.get("largest_price_movers") or []:
            pct, imp = _num(m.get("change_pct")), _num(m.get("impact"))
            if pct is not None and imp is not None and pct >= v10.get("pct", 10) and imp >= v10.get("min_impact", 0):
                item = m.get("item") or ""
                out.append(dict(rule="purchasing.price_mover", capability="purchasing", company=co, location=item[:60],
                                location_name=item, currency=cur, period=day[:7],
                                title=RULES["purchasing.price_mover"][1].format(item=item[:50], pct=round(pct)),
                                detail=f"{item} ({co}): kostoja mesatare {m.get('previous_average_cost')} → "
                                       f"{m.get('current_average_cost')}, ndikimi {imp:,.0f} {cur}.",
                                metric="price_change_pct", value=round(pct, 1), limit=v10.get("pct"), unit="%",
                                impact=round(imp), severity=_sev(pct, 30, 20, 0),
                                evidence=dict(ev10, mover=m, impact_formula="ndikimi i Purchasing: (kosto e re − kosto e "
                                                                           "mëparshme) × sasia e blerë në periudhë")))
        n = C.get("invoices_without_po_yesterday") or {}
        rate = _num(n.get("rate_pct"))
        if rate is not None and n.get("invoices") and rate > v11.get("max", 5):
            out.append(dict(rule="purchasing.invoices_without_po", capability="purchasing", company=co, location="",
                            location_name=co, currency=cur, period=day,
                            title=RULES["purchasing.invoices_without_po"][1].format(v=round(rate, 1)),
                            detail=f"{co} {day}: {n.get('without_order')} nga {n.get('invoices')} fatura pa PO, "
                                   f"{_num(n.get('value_without_order')) or 0:,.0f} {cur}.",
                            metric="invoices_without_po_pct", value=round(rate, 1), limit=v11.get("max"), unit="%",
                            impact=_num(n.get("value_without_order")), severity=_sev(rate, 25, 12, 0),
                            evidence=dict(ev11, invoices=n, impact_formula="vlera e faturave pa PO (në rrezik, jo humbje)")))
    return out


def run(facts):
    """Detects and records: (all finding ids seen, new ids)."""
    day = facts.get("day") or ""
    seen, new = [], []
    for f in detect(facts):
        fid, is_new = reg.upsert_finding(f, day)
        seen.append(fid)
        if is_new:
            new.append(fid)
    return seen, new


def metric(facts, rule, company, location):
    """The value of a finding's measure in later facts (same rule, company, location); None when not there."""
    fin = facts.get("finance") or {}
    if rule.startswith("sales."):
        fld = {"sales.drop_vs_last_year": "vs_last_year_pct", "sales.mtd_vs_budget": "mtd_vs_budget_pct",
               "sales.discounts_pct": "discounts_pct_of_gross", "sales.voids_pct": "voids_pct_of_gross"}.get(rule)
        for L in _restaurants(facts):
            if (L.get("ref") or L.get("name")) == location and (L.get("company") or "") == (company or ""):
                return _num(L.get(fld))
        return None
    if rule.startswith("pl."):
        for P in fin.get("pl") or []:
            if (P.get("company") or "") != (company or ""):
                continue
            for d in P.get("departments") or []:
                if (d.get("code") or d.get("name")) == location:
                    a, b = d.get("actual") or {}, d.get("budget") or {}
                    if rule == "pl.ebitda_vs_budget":
                        e, be = _num(a.get("ebitda")), _num(b.get("ebitda"))
                        return round((e - be) / be * 100, 1) if e is not None and be else None
                    return _num(a.get("food_pct" if rule == "pl.food_vs_budget" else "labor_pct"))
        return None
    if rule.startswith("apar."):
        side = "payables" if "payables" in rule else "receivables"
        for A in fin.get("payables_receivables") or []:
            if (A.get("company") or "") == (company or ""):
                return _num((A.get(side) or {}).get("beyond_critical"))
        return None
    if rule == "purchasing.invoices_without_po":
        for C in (facts.get("purchasing") or {}).get("companies") or []:
            if (C.get("company") or "") == (company or ""):
                return _num((C.get("invoices_without_po_yesterday") or {}).get("rate_pct"))
    if rule == "purchasing.price_mover":
        for C in (facts.get("purchasing") or {}).get("companies") or []:
            if (C.get("company") or "") == (company or ""):
                for m in C.get("largest_price_movers") or []:
                    if (m.get("item") or "")[:60] == location:
                        return _num(m.get("change_pct"))
    return None


def follow_up(facts):
    """Measures every open action's KPI in today's facts. Returns the actions measured."""
    day = facts.get("day") or ""
    n = 0
    for a in reg.actions("open"):
        if not a["rule"]:
            continue
        v = metric(facts, a["rule"], a["company"], a["location"])
        if v is not None:
            reg.measure_action(a["id"], v, day)
            n += 1
    return n
