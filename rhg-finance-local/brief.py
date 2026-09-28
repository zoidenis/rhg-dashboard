"""AI Executive Finance Brief.

Every line is derived from records read through the MCP/OData layer in this
session and is labelled fact, inference or recommendation. Nothing is estimated:
where a figure needs a field BC does not publish, the brief says so.
"""
from datetime import date, timedelta

def _money(v, currency="ALL"):
    """A figure the application has withheld prints as such, never as a number.

    Some figures are deliberately None: a margin whose cost base failed validation is
    not shown, because a wrong result read confidently is worse than a gap.
    """
    if v is None:
        return "under validation"
    return f"{v:,.0f} {currency}"


def build(data, settings, meta):
    currency = meta.get("currency", "ALL")

    def _m(v):
        return _money(v, currency)

    pl, bs = data["pl"], data["balance_sheet"]
    ap, ar, cash = data["payables"], data["receivables"], data["cash"]
    th = settings["thresholds"]
    exc = data["exceptions"]
    critical = [e for e in exc if e["severity"] == "critical"]
    warnings = [e for e in exc if e["severity"] == "warning"]
    k = pl["kpis"]

    if critical or cash["total"] < th["cash_warning"]:
        status, note = "Critical management intervention required", f"{len(critical)} critical exception(s) open."
    elif warnings:
        status, note = "Attention required", f"{len(warnings)} exception(s) above the review threshold."
    else:
        status, note = "Financial position under control", "No exception passed the configured thresholds."

    performance = []
    if k["revenue"]:
        rev_txt = f"Revenue {_m(k['revenue'])}"
        if k["revenue_prev"]:
            pct = (k["revenue"] - k["revenue_prev"]) / abs(k["revenue_prev"]) * 100
            rev_txt += f", {pct:+.1f}% against the comparison period"
        if k["revenue_ly"]:
            pct_ly = (k["revenue"] - k["revenue_ly"]) / abs(k["revenue_ly"]) * 100
            rev_txt += f" and {pct_ly:+.1f}% against last year"
        performance.append({"kind": "fact", "text": rev_txt + ".", "drill": {"kind": "pl", "key": ""}})
        performance.append({"kind": "fact",
                            "text": f"Gross profit {_m(k['gross'])}"
                                    + (f" at a margin of {k['gross_margin']:.1f}%" if k["gross_margin"] is not None else "")
                                    + f", operating expenses {_m(k['opex'])}, EBITDA {_m(k['ebitda'])}.",
                            "drill": {"kind": "pl", "key": ""}})
    else:
        performance.append({"kind": "fact",
                            "text": "No income was posted to the income accounts in this period, so the "
                                    "result cannot be read from the ledger yet.",
                            "drill": {"kind": "pl", "key": ""}})

    position = [{"kind": "fact",
                 "text": f"Assets {_m(bs['totals']['assets'])} against liabilities {_m(bs['totals']['liabilities'])} "
                         f"and equity {_m(bs['totals']['equity'])}.",
                 "drill": {"kind": "bs", "key": ""}}]
    if abs(bs["totals"]["difference"]) > th["materiality"]:
        position.append({"kind": "inference",
                         "text": f"Assets exceed liabilities and equity by {_m(bs['totals']['difference'])}. "
                                 f"This is normally the result of the current period that has not yet been "
                                 f"closed to equity; confirm before reporting it as a gap.",
                         "drill": {"kind": "bs", "key": ""}})
    if bs["flags"]:
        position.append({"kind": "fact",
                         "text": f"{len(bs['flags'])} accounts carry a balance on the unexpected side, "
                                 f"the largest being {bs['flags'][0]['name']} at {_m(bs['flags'][0]['value'])}.",
                         "drill": {"kind": "bs", "key": ""}})

    liquidity = [{"kind": "fact", "text": f"Cash and bank balances {_m(cash['total'])}, movement in the period "
                                          f"{_m(cash['movement'])}.", "drill": {"kind": "cash", "key": ""}},
                 {"kind": "fact", "text": f"Payables {_m(ap['total'])} of which {_m(ap['overdue'])} overdue; "
                                          f"receivables {_m(ar['total'])} of which {_m(ar['overdue'])} overdue.",
                  "drill": {"kind": "ap", "key": ""}}]
    net_position = cash["total"] + ar["total"] - ap["total"]
    liquidity.append({"kind": "inference",
                      "text": f"Cash plus receivables less payables is {_m(net_position)}. This is a working "
                              f"capital indication, not a cash-flow forecast: BC does not publish payment "
                              f"plans or expected receipt dates here.",
                      "drill": {"kind": "ar", "key": ""}})

    accounting = []
    gl = data["gl"]
    if gl["count"]:
        accounting.append({"kind": "fact",
                           "text": f"{gl['count']} entries at or above the materiality threshold of "
                                   f"{_m(gl['threshold'])} were reviewed.",
                           "drill": {"kind": "gl", "key": ""}})
    for e in [x for x in exc if x.get("area") == "G/L"][:2]:
        accounting.append({"kind": "fact", "text": e["title"] + ".", "drill": {"kind": "gl", "key": ""}})
    if data["gaps"]:
        accounting.append({"kind": "fact",
                           "text": f"{len(data['gaps'])} requested figures cannot be produced from the "
                                   f"published web services and are listed under Data availability.",
                           "drill": {"kind": "gaps", "key": ""}})

    due = (date.today() + timedelta(days=2)).isoformat()
    due_fast = (date.today() + timedelta(days=1)).isoformat()
    actions, by_rule = [], {}
    for e in critical + warnings:
        by_rule.setdefault(e["rule"], []).append(e)
    for rule, group in sorted(by_rule.items(), key=lambda kv: -max(abs(x.get("impact") or 0) for x in kv[1])):
        if len(actions) >= 5:
            break
        first = group[0]
        total = sum(abs(x.get("impact") or 0) for x in group)
        grouped = len(group) > 2
        actions.append({
            "title": (f"{rule}: {len(group)} cases, {_m(total)} involved" if grouped else first["title"]),
            "priority": "Critical" if first["severity"] == "critical" else "High",
            "owner": settings["owners"].get(_owner_key(first.get("area")), "") or settings["owners"].get("default", ""),
            "due_date": due_fast if first["severity"] == "critical" else due,
            "impact": total if grouped else abs(first.get("impact") or 0),
            "area": first.get("area", ""), "confidence": "high",
            "recommendation": first.get("next_action", ""),
            "source_exception": (f"group-{rule[:28]}-{meta['current'][0]}" if grouped else first["code"]),
            "document": first.get("document", ""),
            "detail": first.get("detail", ""),
            "evidence": [{"entity": "Business Central", "key": x.get("document", ""),
                          "period": f"{meta['current'][0]}..{meta['current'][1]}"} for x in group[:8]],
        })
    rank = {"Critical": 0, "High": 1}
    actions.sort(key=lambda a: (rank.get(a["priority"], 9), -a["impact"]))

    return {
        "status": status, "status_note": note,
        "period": f"{meta['current'][0]} to {meta['current'][1]}",
        "comparison": f"{meta['prior'][0]} to {meta['prior'][1]}",
        "methodology": meta.get("methodology", ""),
        "performance": performance, "position": position, "liquidity": liquidity,
        "accounting": accounting, "actions": actions[:5],
        "counts": {"critical": len(critical), "warning": len(warnings), "total": len(exc)},
        "generated_at": meta["checked_at"],
        "filters": {"company": meta["company"], "dimension": meta.get("dimension") or "All departments"},
    }


def _owner_key(area):
    return {"AP": "payables", "AR": "receivables", "G/L": "accounting",
            "Cash": "treasury", "Inventory": "inventory"}.get(area, "default")
