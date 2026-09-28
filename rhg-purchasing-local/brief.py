"""Executive Purchasing Brief.

Every statement is derived from records returned by the BC connector in this
session. Statements are labelled: fact (read from BC), inference (a conclusion
drawn from those records) or recommendation. Nothing is estimated when the
underlying field is missing - the gap is reported instead.
"""
from datetime import date, timedelta
import currency


def _money(v):
    return f"{v:,.0f} {currency.code()}"


def _methodology(meta):
    mode = meta.get("mode", "week")
    if mode == "month":
        return ("Month to date against the same number of days of the previous month."
                if meta["like_for_like"] else "Full month against the preceding full month.")
    if mode in ("ytd", "year"):
        return ("Year to date against the same days of the previous year."
                if meta["like_for_like"] else "Full year against the preceding full year.")
    return ("Current week to date against the same weekdays of the previous week."
            if meta["like_for_like"] else "Full week against the preceding full week.")


def build(data, inv, ops, settings, meta, gaps_count):
    currency.set_current(meta.get("currency", "ALL"))
    k, ik = data["kpis"], (inv or {}).get("kpis", {})
    th = settings["thresholds"]
    exceptions = ops["sla"]
    critical = [e for e in exceptions if e["severity"] == "critical"]
    warnings = [e for e in exceptions if e["severity"] == "warning"]
    po_summary = ops.get("po_summary") or {}
    pending_invoice = po_summary.get("awaiting_invoice",
                                     sum(p["received_not_invoiced"] for p in ops["purchase_orders"]))
    pending_orders = po_summary.get("awaiting_invoice_orders",
                                    sum(1 for p in ops["purchase_orders"] if p["received_not_invoiced"] > 0))
    period_uninvoiced = data["kpis"].get("uninvoiced_in_period") or 0
    open_receipts = data["kpis"].get("uninvoiced_receipts") or 0
    live_period = meta.get("like_for_like")
    top_supplier = ops["suppliers"][0] if ops["suppliers"] else None
    conc = top_supplier["share"] if top_supplier else 0

    # ---- overall status ----
    if critical or conc >= th["supplier_concentration_pct"] * 1.5:
        status, status_note = "Critical intervention required", f"{len(critical)} critical exception(s) open."
    elif warnings or k["net_price_impact"] > th["price_critical_impact"]:
        status, status_note = "Attention required", f"{len(warnings)} exception(s) above the warning threshold."
    else:
        status, status_note = "Under control", "No exception passed the configured thresholds."

    period = f"{meta['current'][0]} to {meta['current'][1]}"
    comp = f"{meta['prior'][0]} to {meta['prior'][1]}"

    # ---- main developments ----
    dev = []
    if k["total_prev"]:
        pct = (k["total_cur"] - k["total_prev"]) / k["total_prev"] * 100
        dev.append({"label": "Spend", "kind": "fact",
                    "text": f"Purchases {_money(k['total_cur'])} against {_money(k['total_prev'])} in the comparison "
                            f"period ({pct:+.1f}%), across {k['lines']:,} posted lines and {k['items']:,} items.",
                    "drill": {"kind": "purchases"}})
    if k["increases"] or k["decreases"]:
        dev.append({"label": "Prices", "kind": "fact",
                    "text": f"{k['increases']} items rose in unit cost (+{_money(k['increase_impact'])}) and "
                            f"{k['decreases']} fell ({_money(k['saving'])} saved). Net price effect "
                            f"{k['net_price_impact']:+,.0f} {currency.code()} on comparable volumes.",
                    "drill": {"kind": "movers"}})
    if ik.get("invoices"):
        dev.append({"label": "Processing", "kind": "fact",
                    "text": f"{ik['invoices']:,} vendor invoices and {ik['credit_memos']} credit notes posted, "
                            f"{ik['item_lines']:,} item lines on {ik['item_invoices']:,} invoices"
                            + (f", average posting delay {ik['avg_lag']:.1f} days." if ik.get("avg_lag") is not None else "."),
                    "drill": {"kind": "invoices"}})
    if abs(period_uninvoiced) >= 1:
        dev.append({"label": "Exposure", "kind": "fact",
                    "text": f"{_money(period_uninvoiced)} of goods received in this period is still waiting for its "
                            f"invoice, across {open_receipts} receipts. Business Central books a receipt at expected "
                            f"cost and reverses it when the invoice arrives, so this is what this period's receipts "
                            f"still owe.",
                    "drill": {"kind": "purchases"}})
    if pending_invoice > 0:
        dev.append({"label": "Open orders today", "kind": "fact",
                    "text": f"Separately, {_money(pending_invoice)} sits on "
                            f"{pending_orders} open purchase orders received but not invoiced. That is the position as at today, not "
                            f"{'this period' if live_period else 'the period selected above'}: Business Central keeps "
                            f"no history of order status, so it does not change when you look at an earlier month.",
                    "drill": {"kind": "po_pending"}})
    reqs = ops.get("requisitions") or []
    if reqs:
        waiting = [r for r in reqs if r["severity"]]
        dev.append({"label": "Requisitions", "kind": "fact",
                    "text": f"As at today, {len(reqs)} requisition lines are open, {len(waiting)} of them past the service-level "
                            f"threshold; the oldest has waited {max((r['hours_open'] or 0) for r in reqs):.0f} hours."
                            + (f" Average time to carry a requisition into an order so far: "
                               f"{ops['history']['avg_requisition_hours']:.0f} hours."
                               if (ops.get("history") or {}).get("avg_requisition_hours") else ""),
                    "drill": {"kind": "requisitions"}})
    if top_supplier:
        dev.append({"label": "Suppliers", "kind": "fact" if conc < th["supplier_concentration_pct"] else "inference",
                    "text": f"{top_supplier['supplier']} accounts for {top_supplier['share']:.0f}% of posted purchase "
                            f"value ({_money(top_supplier['spend'])})"
                            + (". Concentration is above the configured threshold, which limits negotiating room."
                               if conc >= th["supplier_concentration_pct"] else "."),
                    "drill": {"kind": "suppliers"}})

    # ---- financial impact ----
    avoidable = sum(abs(e["impact"]) for e in exceptions if e["rule"].startswith("Price increase"))
    spread_saving = sum(s["overpay"] for s in data.get("spreads", []))
    finance = {
        "extra_cost": k["increase_impact"],
        "savings": k["saving"],
        "net": k["net_price_impact"],
        "avoidable": avoidable,
        "location_spread": spread_saving,
        "unconfirmed_exposure": pending_invoice,
    }

    # ---- operational risks ----
    risks = []
    if ik.get("suspicious"):
        risks.append({"kind": "fact", "text": f"{ik['suspicious']} posted invoices carry negative or zero-cost lines.",
                      "drill": {"kind": "suspicious"}})
    if ik.get("late"):
        risks.append({"kind": "fact", "text": f"{ik['late']} invoices were posted more than "
                                              f"{th['invoice_posting_late_days']:.0f} days after the vendor's invoice date.",
                      "drill": {"kind": "late"}})
    if pending_invoice > 0:
        risks.append({"kind": "fact",
                      "text": f"{_money(pending_invoice)} received but not invoiced on open orders, as at today.",
                      "drill": {"kind": "po_pending"}})
    if data.get("spreads"):
        s = data["spreads"][0]
        risks.append({"kind": "inference",
                      "text": f"The same item is bought at different prices by location, e.g. {s['desc'] or s['item_no']} "
                              f"{s['low']:,.2f} at {s['low_loc']} against {s['high']:,.2f} at {s['high_loc']}.",
                      "drill": {"kind": "spreads"}})
    contract = [r for r in (ops.get("contract_prices") or []) if r["pct"] > 0]
    if contract:
        over = sum(r["overpaid"] for r in contract)
        risks.append({"kind": "fact", "text": f"{len(contract)} items were paid above the contracted price, "
                                              f"{_money(over)} above the agreed rates.",
                      "drill": {"kind": "contract"}})
    elif not (ops.get("price_status") or {}).get("available"):
        risks.append({"kind": "fact", "text": "There is no contracted price list in BC, so paid prices cannot be "
                                              "checked against an agreement.",
                      "drill": {"kind": "gaps"}})
    if not (ops.get("requisition_status") or {}).get("available"):
        risks.append({"kind": "fact", "text": "Requisitions are not visible: the requisition worksheet is not published "
                                              "as a web service, so requisition-to-order time cannot be measured.",
                      "drill": {"kind": "gaps"}})
    if not (inv or {}).get("user_status", {}).get("available"):
        risks.append({"kind": "fact", "text": "Posting user is not available from BC, so workload cannot be "
                                              "attributed per colleague until Item Registers is published.",
                      "drill": {"kind": "gaps"}})
    if gaps_count:
        risks.append({"kind": "fact", "text": f"{gaps_count} KPIs requested by management cannot be produced from the "
                                              f"published web services and are listed under Data availability.",
                      "drill": {"kind": "gaps"}})

    # ---- recommended actions (max 5, each traceable) ----
    due_fast = (date.today() + timedelta(days=1)).isoformat()
    due_week = (date.today() + timedelta(days=5)).isoformat()
    actions = []
    by_rule = {}
    for e in critical + warnings:
        by_rule.setdefault(e["rule"], []).append(e)
    ranked = sorted(by_rule.items(), key=lambda kv: (-max(abs(x.get("impact") or 0) for x in kv[1]), kv[0]))
    for rule, group in ranked:
        if len(actions) >= 5:
            break
        e = group[0]
        if len(group) > 2:
            total = sum(abs(x.get("impact") or 0) for x in group)
            actions.append({
                "title": f"{rule}: {len(group)} cases, {_money(total)} involved",
                "priority": "Critical" if e["severity"] == "critical" else "High",
                "owner": e.get("owner", "") or settings["owners"].get("default", ""),
                "due_date": due_fast if e["severity"] == "critical" else due_week,
                "impact": total, "expected_impact": _money(total) + " at stake",
                "recommendation": e.get("next_action", ""),
                "source_exception": f"group-{rule[:24]}-{meta['current'][0]}",
                "bc_document": "", "document_type": e.get("document_type", ""), "supplier": "", "location": "",
                "confidence": "high",
                "evidence": [{"entity": "Business Central", "key": x.get("document", ""), "period": period} for x in group[:8]],
                "detail": "Largest case: " + e["title"] + ". " + e.get("detail", ""),
            })
            continue
        actions.append({
            "title": e["title"], "priority": "Critical" if e["severity"] == "critical" else "High",
            "owner": e.get("owner", "") or settings["owners"].get("default", ""),
            "due_date": due_fast if e["severity"] == "critical" else due_week,
            "impact": e.get("impact", 0), "expected_impact": _money(abs(e.get("impact", 0))) + " at stake",
            "recommendation": e.get("next_action", ""), "source_exception": e["code"],
            "bc_document": e.get("document", ""), "document_type": e.get("document_type", ""),
            "supplier": e.get("supplier", ""), "location": e.get("location", ""),
            "confidence": "high" if e["severity"] != "info" else "medium",
            "evidence": [{"entity": "ValueEntries / VendorLedgerEntries / PurchaseOrderList",
                          "key": e.get("document", ""), "period": period}],
            "detail": e.get("detail", ""),
        })
    if len(actions) < 5 and conc >= th["supplier_concentration_pct"] and top_supplier:
        actions.append({
            "title": f"Review dependency on {top_supplier['supplier']}",
            "priority": "Medium", "owner": settings["owners"].get("default", ""), "due_date": due_week,
            "impact": top_supplier["spend"], "expected_impact": f"{top_supplier['share']:.0f}% of spend concentrated",
            "recommendation": "Test a second source for the highest-value items from this supplier.",
            "source_exception": f"conc-{top_supplier['supplier'][:20]}-{meta['current'][0]}",
            "bc_document": "", "document_type": "Vendor", "supplier": top_supplier["supplier"], "location": "",
            "confidence": "medium",
            "evidence": [{"entity": "VendorLedgerEntries", "key": top_supplier["supplier"], "period": period}],
            "detail": f"{top_supplier['invoices']} invoices, {_money(top_supplier['spend'])} in the period.",
        })

    rank = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}
    actions.sort(key=lambda a: (rank.get(a["priority"], 9), -abs(a.get("impact") or 0)))

    return {
        "status": status, "status_note": status_note,
        "period": period, "comparison": comp,
        "like_for_like": meta["like_for_like"],
        "methodology": _methodology(meta),
        "developments": dev[:5], "finance": finance, "risks": risks[:6], "actions": actions[:5],
        "counts": {"critical": len(critical), "warning": len(warnings), "total": len(exceptions)},
        "generated_at": meta["checked_at"], "filters": {"company": meta["company"], "location": meta["location"] or "All"},
    }
