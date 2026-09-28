"""How much of what RHG buys is actually covered by an agreed price, and is it honoured.

Executive level on purpose: coverage, adherence and freshness of the price list, rolled
up by supplier and category. The item-by-item comparison already exists on the
Comparisons screen and is not repeated here.

Coverage is the question that comes first. A high adherence figure means little if the
agreed prices only cover a fifth of the spend, so every adherence number on this screen
carries the share of spend it was measured on.
"""
from collections import defaultdict

import currency


def _f(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def build(rows, prices, price_status, catalog, settings, period_label=""):
    """rows: purchase value entries of the period. prices: {item: contracted price}."""
    tolerance = float(settings["thresholds"].get("price_above_contract_pct") or 2)
    if not price_status or not price_status.get("available"):
        return {"available": False,
                "reason": (price_status or {}).get("reason", "No price list could be read."),
                "period": period_label}

    spend = defaultdict(lambda: {"cost": 0.0, "qty": 0.0, "desc": "", "vendor": "", "category": ""})
    for r in rows:
        item = str(r.get("Item_No") or "")
        if not item:
            continue
        s = spend[item]
        s["cost"] += _f(r.get("Cost_Amount_Actual")) + _f(r.get("Cost_Amount_Expected"))
        s["qty"] += _f(r.get("Item_Ledger_Entry_Quantity"))
        s["desc"] = (r.get("Item_Description") or "").strip() or s["desc"]
        card = (catalog or {}).get(item) or {}
        s["vendor"] = card.get("vendor") or s["vendor"]
        s["category"] = card.get("g3") or card.get("code") or s["category"]

    covered_cost = uncovered_cost = 0.0
    above_value = below_value = 0.0
    covered_items = []
    uncovered = []
    by_supplier = defaultdict(lambda: {"covered": 0.0, "uncovered": 0.0, "above": 0.0, "items": 0})
    by_category = defaultdict(lambda: {"covered": 0.0, "uncovered": 0.0, "above": 0.0, "items": 0})
    for item, s in spend.items():
        if s["cost"] <= 0 or s["qty"] <= 0:
            continue
        paid_unit = s["cost"] / s["qty"]
        contract = prices.get(item)
        sup = by_supplier[s["vendor"] or "(no supplier on the item card)"]
        cat = by_category[s["category"] or "(no category)"]
        sup["items"] += 1
        cat["items"] += 1
        if not contract:
            uncovered_cost += s["cost"]
            sup["uncovered"] += s["cost"]
            cat["uncovered"] += s["cost"]
            uncovered.append({"item": item, "description": s["desc"], "cost": s["cost"],
                              "vendor": s["vendor"], "category": s["category"]})
            continue
        agreed = _f(contract.get("price"))
        covered_cost += s["cost"]
        sup["covered"] += s["cost"]
        cat["covered"] += s["cost"]
        gap_unit = paid_unit - agreed
        gap_value = gap_unit * s["qty"]
        pct = (gap_unit / agreed * 100) if agreed else 0
        if agreed and pct > tolerance:
            above_value += gap_value
            sup["above"] += gap_value
            cat["above"] += gap_value
        elif agreed and pct < -tolerance:
            below_value += gap_value
        covered_items.append({"item": item, "description": s["desc"], "cost": s["cost"],
                              "paid_unit": paid_unit, "agreed": agreed, "pct": pct,
                              "gap_value": gap_value, "vendor": s["vendor"],
                              "category": s["category"]})

    total_cost = covered_cost + uncovered_cost
    uncovered.sort(key=lambda u: -u["cost"])
    suppliers = sorted(({"supplier": k, **v, "share": (v["covered"] / (v["covered"] + v["uncovered"]) * 100)
                         if (v["covered"] + v["uncovered"]) else 0}
                        for k, v in by_supplier.items()),
                       key=lambda r: -(r["covered"] + r["uncovered"]))[:12]
    categories = sorted(({"category": k, **v, "share": (v["covered"] / (v["covered"] + v["uncovered"]) * 100)
                          if (v["covered"] + v["uncovered"]) else 0}
                         for k, v in by_category.items()),
                        key=lambda r: -(r["covered"] + r["uncovered"]))[:12]

    lines = price_status.get("count", 0)
    return {
        "available": True, "period": period_label,
        "kpis": {
            "coverage_pct": (covered_cost / total_cost * 100) if total_cost else 0,
            "covered_cost": covered_cost, "uncovered_cost": uncovered_cost, "total_cost": total_cost,
            "items_covered": len(covered_items), "items_bought": len(covered_items) + len(uncovered),
            "above_value": above_value, "below_value": below_value,
            "above_pct": (above_value / covered_cost * 100) if covered_cost else 0,
            "agreed_items": lines,
            "tolerance": tolerance,
        },
        "definitions": {
            "coverage": "The share of this period's purchase cost on items that carry an agreed price "
                        "valid today. Everything else was bought with no agreed price to compare against.",
            "adherence": f"Paid unit cost against the agreed price, measured only on the covered spend and "
                         f"only outside a {tolerance:g}% tolerance. It is a management comparison: the paid "
                         f"cost is the period's average for the item, not a single invoice.",
            "limits": "Unit of measure is not reconciled between the price list and the posting, so an "
                      "agreed price per case compared with a cost per kilo would show a false gap. "
                      "Suppliers come from the item card, not from the invoice.",
        },
        "suppliers": suppliers, "categories": categories,
        "biggest_uncovered": uncovered[:8],
        "biggest_gaps": sorted([c for c in covered_items if c["pct"] > tolerance],
                               key=lambda c: -c["gap_value"])[:8],
        "price_list": {"lines": lines, "entity": price_status.get("entity", ""),
                       "multi_price_items": price_status.get("multi_price_items", 0),
                       "skipped": price_status.get("skipped", {})},
    }
