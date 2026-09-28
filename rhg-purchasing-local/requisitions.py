"""Requisitions as a management view.

The audit of the published worksheet found that only lines carrying a worksheet
template are requisitions. Everything else in that table is what an order-planning
run left behind: fractions of a unit, no vendor, due dates from 2022, sometimes no
item number at all. Those rows are excluded from every figure here and reported once,
as a data-quality finding, because counting them as open requests turned a worksheet
that needed tidying into a false emergency.
"""
from collections import defaultdict
from datetime import date

import currency

INVALID_DATE = "0001"


def _d(value):
    text = str(value or "")[:10]
    if not text or text.startswith(INVALID_DATE):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _f(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def split(rows):
    """Real worksheet lines, and the planning leftovers that are not requisitions."""
    real, leftovers = [], []
    for r in rows or []:
        (real if str(r.get("template") or "").strip() else leftovers).append(r)
    return real, leftovers


def build(rows, settings, today=None):
    today = today or date.today()
    th = settings["thresholds"]
    warn_days = float(th.get("requisition_warning_hours") or 24) / 24
    real, leftovers = split(rows or [])

    lines = []
    for r in real:
        qty = _f(r.get("quantity"))
        due = _d(r.get("due_date"))
        ordered = _d(r.get("order_date"))
        converted = bool(r.get("ref_order"))
        age = (today - ordered).days if ordered else None
        days_to_due = (due - today).days if due else None
        issues = []
        if qty <= 0:
            issues.append("Quantity is zero")
        if not r.get("vendor_no") and not r.get("vendor"):
            issues.append("No supplier on the line")
        if due is None:
            issues.append("No usable due date")
        if 0 < _f(r.get("unit_cost")) < 0.01:
            issues.append("Unit cost looks unset")
        if converted:
            state = "Converted to order"
        elif issues:
            state = "Data issue"
        elif days_to_due is not None and days_to_due < 0:
            state = "Past its required date"
        elif days_to_due is not None and days_to_due <= 2:
            state = "Due within two days"
        else:
            state = "Awaiting conversion"
        lines.append({
            "batch": r.get("batch") or "", "template": r.get("template") or "",
            "line_no": r.get("line_no") or 0,
            "item": r.get("item") or "", "description": r.get("description") or "",
            "location": r.get("location") or "", "quantity": qty, "uom": r.get("uom") or "",
            "unit_cost": _f(r.get("unit_cost")), "value": qty * _f(r.get("unit_cost")),
            "vendor": r.get("vendor") or r.get("vendor_no") or "",
            "due": due.isoformat() if due else "", "ordered": ordered.isoformat() if ordered else "",
            "age_days": age, "days_to_due": days_to_due,
            "converted": converted, "order_no": r.get("ref_order") or "",
            "action": (r.get("action") or "").strip(), "accepted": bool(r.get("accepted")),
            "requester": r.get("requester") or "", "purchaser": r.get("purchaser") or "",
            "state": state, "issues": issues,
            "reference": f"{r.get('template')} / {r.get('batch')} / line {r.get('line_no')}",
        })

    open_lines = [l for l in lines if not l["converted"]]
    actionable = [l for l in open_lines if l["state"] != "Data issue"]
    at_risk = [l for l in actionable if l["state"] in ("Past its required date", "Due within two days")]
    with_cost = [l for l in open_lines if l["unit_cost"] >= 0.01]
    oldest = max((l["age_days"] for l in actionable if l["age_days"] is not None), default=None)

    batches = defaultdict(lambda: {"lines": 0, "open": 0, "risk": 0, "value": 0.0, "locations": set(),
                                   "oldest": None, "converted": 0})
    for l in lines:
        b = batches[(l["template"], l["batch"])]
        b["lines"] += 1
        b["converted"] += l["converted"]
        if not l["converted"]:
            b["open"] += 1
            b["value"] += l["value"]
        if l in at_risk:
            b["risk"] += 1
        if l["location"]:
            b["locations"].add(l["location"])
        if l["age_days"] is not None:
            b["oldest"] = max(b["oldest"] or 0, l["age_days"])

    by_location = defaultdict(lambda: {"lines": 0, "open": 0, "risk": 0, "issues": 0, "value": 0.0,
                                       "batches": set(), "oldest": None, "converted": 0,
                                       "reasons": defaultdict(int)})
    for l in lines:
        loc = l["location"] or "(no location on the line)"
        g = by_location[loc]
        g["lines"] += 1
        g["batches"].add(l["batch"])
        g["converted"] += l["converted"]
        if not l["converted"]:
            g["open"] += 1
            g["value"] += l["value"]
            g["reasons"][l["state"]] += 1
        if l["state"] == "Data issue":
            g["issues"] += 1
        if l in at_risk:
            g["risk"] += 1
        if l["age_days"] is not None and not l["converted"]:
            g["oldest"] = max(g["oldest"] or 0, l["age_days"])

    locations = sorted(
        ({"location": loc, "lines": g["lines"], "open": g["open"], "risk": g["risk"],
          "issues": g["issues"], "value": g["value"], "converted": g["converted"],
          "batches": sorted(x for x in g["batches"] if x),
          "oldest_days": g["oldest"],
          "main_reason": max(g["reasons"].items(), key=lambda kv: kv[1])[0] if g["reasons"] else "",
          "next_action": ("Order or cancel the overdue lines" if g["risk"] else
                          "Convert the open lines into orders" if g["open"] else
                          "Nothing waiting")}
         for loc, g in by_location.items()),
        key=lambda r: (-r["risk"], -r["open"], -r["value"]))

    groups = defaultdict(lambda: {"lines": [], "locations": set()})
    for l in open_lines:
        if l["issues"]:
            for issue in l["issues"]:
                groups[issue]["lines"].append(l)
                groups[issue]["locations"].add(l["location"] or "(none)")
        else:
            groups[l["state"]]["lines"].append(l)
            groups[l["state"]]["locations"].add(l["location"] or "(none)")
    owners = {
        "Past its required date": "Purchasing Coordinator",
        "Due within two days": "Purchasing Coordinator",
        "Awaiting conversion": "Purchasing Coordinator",
        "No supplier on the line": "Purchasing Coordinator",
        "Quantity is zero": "Stock Manager",
        "No usable due date": "Stock Manager",
        "Unit cost looks unset": "Cost Controller",
    }
    actions = {
        "Past its required date": "Raise the order today or cancel the line.",
        "Due within two days": "Raise the order now; the required date is within the delivery time.",
        "Awaiting conversion": "Review and carry into a purchase order.",
        "No supplier on the line": "Set the supplier so the line can become an order.",
        "Quantity is zero": "Enter the quantity or remove the line.",
        "No usable due date": "Set the required date; without it the line cannot be prioritised.",
        "Unit cost looks unset": "Check the item's cost; the value shown cannot be trusted.",
    }
    exceptions = sorted(
        ({"cause": cause, "count": len(g["lines"]),
          "locations": sorted(g["locations"]),
          "oldest_days": max((l["age_days"] for l in g["lines"] if l["age_days"] is not None), default=None),
          "value": sum(l["value"] for l in g["lines"]),
          "owner": owners.get(cause, "Purchasing Coordinator"),
          "action": actions.get(cause, "Review the lines."),
          "severity": "critical" if cause == "Past its required date" else "warning",
          "lines": sorted(g["lines"], key=lambda l: (l["days_to_due"] if l["days_to_due"] is not None else 999))[:20]}
         for cause, g in groups.items()),
        key=lambda e: (0 if e["severity"] == "critical" else 1, -e["count"]))

    cost_coverage = (len(with_cost) / len(open_lines) * 100) if open_lines else 0
    value_reliable = cost_coverage >= 90
    leftover_dates = sorted({str(r.get("due_date") or "")[:10] for r in leftovers if r.get("due_date")})

    return {
        "kpis": {
            "locations": len({l["location"] for l in actionable if l["location"]}),
            "batches": len({(l["template"], l["batch"]) for l in open_lines}),
            "open_lines": len(open_lines),
            "awaiting_conversion": len([l for l in actionable if l["state"] == "Awaiting conversion"]),
            "at_risk": len(at_risk),
            "converted": sum(1 for l in lines if l["converted"]),
            "oldest_days": oldest,
            "value": sum(l["value"] for l in with_cost) if value_reliable else None,
            "value_coverage": cost_coverage,
            "value_note": (f"From {len(with_cost)} of {len(open_lines)} open lines that carry a unit cost."
                           if value_reliable else
                           f"Not shown: only {cost_coverage:.0f}% of the open lines carry a usable unit cost."),
        },
        "locations": locations,
        "exceptions": exceptions,
        "funnel": [
            {"stage": "Lines in the worksheets", "count": len(lines),
             "note": "Every line carrying a worksheet template."},
            {"stage": "Still open", "count": len(open_lines),
             "note": "No reference order number on the line."},
            {"stage": "Ready to order", "count": len(actionable),
             "note": "Open, with a quantity, a supplier and a usable date."},
            {"stage": "Carried into an order", "count": sum(1 for l in lines if l["converted"]),
             "note": "Business Central holds the order number on the line, so this is verified, not inferred."},
        ],
        "batches": sorted(({"template": t, "batch": b, **{k: v for k, v in g.items() if k != "locations"},
                            "locations": sorted(g["locations"])}
                           for (t, b), g in batches.items()), key=lambda x: -x["open"]),
        "lines": lines,
        "data_quality": {
            "planning_leftovers": len(leftovers),
            "leftover_dates": leftover_dates[:8],
            "zero_quantity": sum(1 for l in lines if l["quantity"] <= 0),
            "no_supplier": sum(1 for l in lines if not l["vendor"]),
            "no_due_date": sum(1 for l in lines if not l["due"]),
            "suspect_cost": sum(1 for l in lines if 0 < l["unit_cost"] < 0.01),
            "note": (f"{len(leftovers)} rows in the same Business Central table carry no worksheet "
                     f"template. They are what order-planning runs left behind — fractions of a unit, no "
                     f"supplier, due dates as old as {leftover_dates[0] if leftover_dates else 'several years'} "
                     f"— and they are excluded from every figure above. They are not requisitions, but they "
                     f"should be cleared from the worksheet in BC."),
        },
        "age_basis": "Age is measured from the line's Order Date in Business Central. The service publishes "
                     "no creation or submission timestamp, so nothing here is measured from when this "
                     "application first saw the line.",
    }
