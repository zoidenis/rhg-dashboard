"""Daily and weekly reports built from the same records as the live screens."""
from datetime import date, timedelta


def _money(v):
    return f"{v:,.0f} ALL"


def _d(s):
    try:
        v = date.fromisoformat((s or "")[:10])
        return None if v.year < 1900 else v
    except ValueError:
        return None


def daily(data, ve_rows, day=None):
    """Yesterday's activity and today's priorities."""
    today = date.today()
    day = day or (today - timedelta(days=1))
    ops = data.get("operations") or {}
    inv = data.get("invoices") if isinstance(data.get("invoices"), dict) else {}
    sla = ops.get("sla") or []
    actions = data.get("actions") or []

    rows = [r for r in ve_rows if (r.get("Posting_Date") or "")[:10] == day.isoformat()]
    spend = sum(float(r.get("Cost_Amount_Actual") or 0) + float(r.get("Cost_Amount_Expected") or 0) for r in rows)
    docs = {(r.get("Document_Type"), r.get("Document_No")) for r in rows}
    invoices_posted = sorted({d[1] for d in docs if d[0] == "Purchase Invoice"})

    by_owner = {}
    for a in actions:
        if a["status"] in ("Resolved", "Dismissed"):
            continue
        by_owner.setdefault(a.get("owner") or "unassigned", []).append(
            {"title": a["title"], "priority": a["priority"], "due": a["due_date"], "status": a["status"]})

    pending = [p for p in (ops.get("purchase_orders") or []) if p["received_not_invoiced"] > 0]
    reqs_late = [r for r in (ops.get("requisitions") or []) if r.get("severity")]

    return {
        "type": "daily", "title": f"Daily purchasing brief, {day.isoformat()}",
        "company": data["meta"]["company"], "location": data["meta"]["location"] or "All locations",
        "generated_at": data["meta"]["checked_at"],
        "activity": {"day": day.isoformat(), "spend": spend, "lines": len(rows),
                     "documents": len(docs), "invoices": len(invoices_posted),
                     "items": len({r.get("Item_No") for r in rows})},
        "priorities": [{"severity": e["severity"], "title": e["title"], "detail": e["detail"],
                        "impact": e.get("impact", 0), "action": e.get("next_action", ""),
                        "owner": e.get("owner", "")} for e in sla if e["severity"] in ("critical", "warning")][:10],
        "overdue_documents": [{"order": p["no"], "supplier": p["supplier"], "location": p["location"],
                               "value": p["received_not_invoiced"], "age_days": p["age_days"]}
                              for p in pending][:15],
        "price_moves": [{"item": m["desc"] or m["item_no"], "pct": m["pct"], "impact": m["impact"]}
                        for m in (data.get("movers") or []) if abs(m["pct"]) >= 5][:10],
        "supplier_issues": [{"supplier": s["supplier"], "credit_notes": s["credit_notes"], "issues": s["issues"],
                             "spend": s["spend"]}
                            for s in (ops.get("suppliers") or []) if s["credit_notes"] or s["issues"]][:10],
        "requisitions_waiting": [{"item": r["description"] or r["item"], "hours": r["hours_open"],
                                  "location": r["location"], "user": r["user"]} for r in reqs_late][:10],
        "actions_by_owner": by_owner,
        "quality": {"findings": (data.get("quality") or {}).get("totals", {}).get("findings", 0),
                    "top": [{"title": c["title"], "count": c["count"], "severity": c["severity"]}
                            for c in (data.get("quality") or {}).get("checks", [])[:5]]},
        "notes": ["Figures are ex-VAT in ALL, read from Business Central value entries and the vendor ledger.",
                  "Approvals and requisition history are limited by what BC publishes; see Data availability."],
    }


def weekly(data, comparisons_data=None):
    """Weekly report for the purchasing director."""
    k = data["kpis"]
    ops = data.get("operations") or {}
    inv = data.get("invoices") if isinstance(data.get("invoices"), dict) else {}
    ik = inv.get("kpis", {}) if inv else {}
    sla = ops.get("sla") or []
    actions = data.get("actions") or []
    b = data.get("brief") or {}
    meta = data["meta"]

    resolved = [a for a in actions if a["status"] == "Resolved"]
    open_actions = [a for a in actions if a["status"] not in ("Resolved", "Dismissed")]

    return {
        "type": "weekly", "title": f"Purchasing report, {meta.get('period_label') or meta['current'][0]} "
                                   f"({meta['current'][0]} to {meta['current'][1]})",
        "company": meta["company"], "location": meta["location"] or "All locations",
        "generated_at": meta["checked_at"],
        "status": b.get("status", ""), "status_note": b.get("status_note", ""),
        "summary": b.get("developments", []),
        "spend": {"current": k["total_cur"], "previous": k["total_prev"], "pct": k["total_pct"],
                  "lines": k["lines"], "items": k["items"], "documents": k["documents"]},
        "comparisons": (comparisons_data or {}).get("rows", []),
        "budget": (comparisons_data or {}).get("budget", {}),
        "price": {"increases": k["increases"], "decreases": k["decreases"],
                  "extra_cost": k["increase_impact"], "savings": k["saving"], "net": k["net_price_impact"],
                  "top_movers": [{"item": m["desc"] or m["item_no"], "pct": m["pct"], "impact": m["impact"],
                                  "prev": m["prev_avg"], "cur": m["cur_avg"]} for m in (data.get("movers") or [])[:10]]},
        "avoidable": {"location_spread": sum(s["overpay"] for s in (data.get("spreads") or [])),
                      "above_contract": sum(r["overpaid"] for r in (ops.get("contract_prices") or []) if r["pct"] > 0),
                      "spreads": [{"item": s["desc"] or s["item_no"], "low_loc": s["low_loc"], "high_loc": s["high_loc"],
                                   "spread": s["spread"], "overpay": s["overpay"]} for s in (data.get("spreads") or [])[:10]]},
        "suppliers": [{"supplier": s["supplier"], "spend": s["spend"], "share": s["share"], "invoices": s["invoices"],
                       "credit_notes": s["credit_notes"], "issues": s["issues"]} for s in (ops.get("suppliers") or [])[:10]],
        "locations": [{"location": l["code"], "current": l["cur"], "previous": l["prev"],
                       "pct": ((l["cur"] - l["prev"]) / l["prev"] * 100) if l["prev"] else None}
                      for l in (data.get("locations") or [])],
        "team": {"available": bool(inv.get("user_status", {}).get("available")),
                 "users": [{"user": u["user"], "invoices": u["invoices"], "lines": u["lines"],
                            "avg_lines": u["avg_lines"], "late": u["late"], "neg_lines": u["neg_lines"],
                            "avg_lag": u["avg_lag"]} for u in (inv.get("users") or [])]},
        "sla": {"critical": sum(1 for e in sla if e["severity"] == "critical"),
                "warning": sum(1 for e in sla if e["severity"] == "warning"),
                "by_rule": _count_by(sla, "rule")},
        "backlog": {"open_orders": ops.get("open_orders", 0),
                    "pending_invoice_value": ops.get("pending_invoice_value", 0),
                    "open_requisitions": len(ops.get("requisitions") or []),
                    "invoice_posting_delay": ik.get("avg_lag")},
        "actions": {"open": len(open_actions), "resolved": len(resolved),
                    "overdue": sum(1 for a in actions if a["status"] == "Overdue"),
                    "next_week": [{"title": a["title"], "priority": a["priority"], "owner": a.get("owner", ""),
                                   "due": a["due_date"], "status": a["status"]} for a in open_actions[:10]]},
        "quality": (data.get("quality") or {}).get("totals", {}),
        "recommended": b.get("actions", []),
        "notes": ["Comparisons use weekday-aligned periods. Budget is a cost-of-goods figure pro-rated by days.",
                  "Costs are ex-VAT in ALL and include goods received but not yet invoiced at expected cost."],
    }


def _count_by(rows, key):
    out = {}
    for r in rows:
        out[r.get(key, "")] = out.get(r.get(key, ""), 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))
