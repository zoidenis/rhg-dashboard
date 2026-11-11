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


NO_SUPPLIER = "(supplier not known)"


def build(rows, prices, price_status, catalog, settings, period_label="", suppliers=None, vendor_names=None):
    """rows: purchase value entries of the period. prices: {item: contracted price}. suppliers: Document_No →
    {"vendor"} from the vendor ledger (who invoiced it); vendor_names: vendor no → name, for the price list's
    vendor and the item card's when no invoice names one (goods received, not yet invoiced)."""
    suppliers, vendor_names = suppliers or {}, vendor_names or {}
    tolerance = float(settings["thresholds"].get("price_above_contract_pct") or 2)
    if not price_status or not price_status.get("available"):
        return {"available": False,
                "reason": (price_status or {}).get("reason", "No price list could be read."),
                "period": period_label}

    spend = defaultdict(lambda: {"cost": 0.0, "qty": 0.0, "desc": "", "vendor": "", "category": "",
                                 "by_vendor": defaultdict(float), "locations": set()})
    for r in rows:
        item = str(r.get("Item_No") or "")
        if not item:
            continue
        s = spend[item]
        c = _f(r.get("Cost_Amount_Actual")) + _f(r.get("Cost_Amount_Expected"))
        s["cost"] += c
        s["qty"] += _f(r.get("Item_Ledger_Entry_Quantity"))
        s["desc"] = (r.get("Item_Description") or "").strip() or s["desc"]
        if r.get("Location_Code"):
            s["locations"].add(r["Location_Code"])
        inv = (suppliers.get(str(r.get("Document_No") or "")) or {}).get("vendor")
        if inv:
            s["by_vendor"][inv] += c
        card = (catalog or {}).get(item) or {}
        s["card_vendor"] = card.get("vendor") or s.get("card_vendor") or ""
        s["category"] = card.get("g3") or card.get("code") or s["category"]
    name = lambda no: vendor_names.get(str(no or ""), "") or str(no or "")
    for item, s in spend.items():
        # who it was bought from: the supplier that invoiced most of it, else the agreed price's, else the item card's
        if s["by_vendor"]:
            s["vendor"] = max(s["by_vendor"].items(), key=lambda kv: kv[1])[0]
        else:
            s["vendor"] = name((prices.get(item) or {}).get("vendor")) or name(s.get("card_vendor"))

    covered_cost = uncovered_cost = 0.0
    above_value = below_value = 0.0
    covered_items = []
    uncovered = []
    by_supplier = defaultdict(lambda: {"covered": 0.0, "uncovered": 0.0, "above": 0.0, "below": 0.0, "items": 0})
    by_category = defaultdict(lambda: {"covered": 0.0, "uncovered": 0.0, "above": 0.0, "below": 0.0, "items": 0})
    for item, s in spend.items():
        if s["cost"] <= 0 or s["qty"] <= 0:
            continue
        paid_unit = s["cost"] / s["qty"]
        contract = prices.get(item)
        sup = by_supplier[s["vendor"] or NO_SUPPLIER]
        cat = by_category[s["category"] or "(no category)"]
        sup["items"] += 1
        cat["items"] += 1
        if not contract:
            uncovered_cost += s["cost"]
            sup["uncovered"] += s["cost"]
            cat["uncovered"] += s["cost"]
            uncovered.append({"item": item, "description": s["desc"], "cost": s["cost"], "qty": s["qty"],
                              "paid_unit": paid_unit, "vendor": s["vendor"], "category": s["category"],
                              "locations": sorted(s["locations"]), "status": "none"})
            continue
        agreed = _f(contract.get("price"))
        covered_cost += s["cost"]
        sup["covered"] += s["cost"]
        cat["covered"] += s["cost"]
        gap_unit = paid_unit - agreed
        gap_value = gap_unit * s["qty"]
        pct = (gap_unit / agreed * 100) if agreed else 0
        status = "within"
        if agreed and pct > tolerance:
            above_value += gap_value
            sup["above"] += gap_value
            cat["above"] += gap_value
            status = "above"
        elif agreed and pct < -tolerance:
            below_value += gap_value
            sup["below"] += gap_value
            cat["below"] += gap_value
            status = "below"
        covered_items.append({"item": item, "description": s["desc"], "cost": s["cost"], "qty": s["qty"],
                              "paid_unit": paid_unit, "agreed": agreed, "pct": pct,
                              "gap_value": gap_value, "vendor": s["vendor"],
                              "category": s["category"], "status": status, "locations": sorted(s["locations"]),
                              "contract_vendor": name(contract.get("vendor")), "uom": contract.get("uom") or "",
                              "list": contract.get("list") or "", "alternatives": contract.get("alternatives") or 0})

    total_cost = covered_cost + uncovered_cost
    uncovered.sort(key=lambda u: -u["cost"])
    suppliers = sorted(({"supplier": k, **v, "share": (v["covered"] / (v["covered"] + v["uncovered"]) * 100)
                         if (v["covered"] + v["uncovered"]) else 0}
                        for k, v in by_supplier.items()),
                       key=lambda r: -(r["covered"] + r["uncovered"]))[:25]
    categories = sorted(({"category": k, **v, "share": (v["covered"] / (v["covered"] + v["uncovered"]) * 100)
                          if (v["covered"] + v["uncovered"]) else 0}
                         for k, v in by_category.items()),
                        key=lambda r: -(r["covered"] + r["uncovered"]))

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
                      "The supplier of an item is the one that invoiced most of it in the period; for goods "
                      "received and not yet invoiced, the agreed price's supplier, else the item card's.",
            "vat": (price_status.get("vat_note") or "")
                   + (f" {price_status['skipped']['vat_unknown']} prices including VAT were left out: no VAT rate "
                      f"for their vendor and item in the VAT Posting Setup."
                      if (price_status.get("skipped") or {}).get("vat_unknown") else ""),
        },
        "suppliers": suppliers, "categories": categories,
        "biggest_uncovered": uncovered[:8],
        "biggest_gaps": sorted([c for c in covered_items if c["pct"] > tolerance],
                               key=lambda c: -c["gap_value"])[:8],
        # every item bought, for the pop-ups (by status, supplier and category), largest spend first
        "items": sorted(covered_items + uncovered, key=lambda c: -c["cost"])[:600],
        "items_total": len(covered_items) + len(uncovered),
        "price_list": {"lines": lines, "entity": price_status.get("entity", ""),
                       "multi_price_items": price_status.get("multi_price_items", 0),
                       "skipped": price_status.get("skipped", {})},
    }
