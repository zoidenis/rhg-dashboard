"""Monthly stock bridge per item and location.

Opening stock, every posted movement of the month, and the closing stock Business
Central itself reports, side by side. The bridge is arithmetic on posted entries:

    opening + receipts - returns + transfers in - transfers out
    + assembly output - assembly consumption - sales/other consumption
    + positive adjustments - negative adjustments = calculated closing

Nothing is inferred. Where BC does not publish a field (variant codes, reorder point,
maximum inventory, physical count entries) the column says so instead of guessing.
"""
from collections import defaultdict

import currency

# how Business Central's entry types map onto the bridge columns
RECEIPT, RETURN = "receipts", "returns"
TRANSFER_IN, TRANSFER_OUT = "transfer_in", "transfer_out"
ASM_OUT, ASM_USE = "assembly_output", "assembly_consumption"
SALES, POS_ADJ, NEG_ADJ, OTHER = "sales", "positive_adjustment", "negative_adjustment", "other"

COLUMNS = [RECEIPT, RETURN, TRANSFER_IN, TRANSFER_OUT, ASM_OUT, ASM_USE, SALES, POS_ADJ, NEG_ADJ, OTHER]


# Replenishment reality at RHG: the restaurants are delivered two or three times a week,
# so an order placed today arrives within a day or two and the cycle between orders is
# about three days. MAGAZINE is different: it holds imported goods with a lead time of
# thirty to forty-five days, so its stock has to carry the whole lead time and nothing
# about it should be judged against a restaurant's threshold.
DEFAULT_CADENCE = "*=2/3/2, MAGAZINE=45/30/10"      # location = lead / cycle / safety, in days


def parse_cadence(text):
    """'*=2/3/2, MAGAZINE=45/30/10' -> {location: {lead, cycle, safety}}."""
    out = {}
    for part in (text or DEFAULT_CADENCE).split(","):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        bits = [b.strip() for b in value.split("/")]
        if len(bits) != 3:
            continue
        try:
            lead, cycle, safety = (float(b) for b in bits)
        except ValueError:
            continue
        out[key.strip().upper()] = {"lead": lead, "cycle": cycle, "safety": safety}
    return out or parse_cadence(DEFAULT_CADENCE)


def policy_for(location, cadence):
    """How much cover this location should hold, from its own delivery rhythm."""
    p = cadence.get((location or "").upper()) or cadence.get("*") or {"lead": 2, "cycle": 3, "safety": 2}
    reorder_days = p["lead"] + p["safety"]
    max_days = p["lead"] + p["cycle"] + p["safety"]
    return {**p, "reorder_days": reorder_days, "max_days": max_days,
            "alert_days": max_days * 2,
            "label": (f"delivered every {p['cycle']:.0f} days, lead time {p['lead']:.0f} days, "
                      f"safety {p['safety']:.0f} days")}


def _f(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def classify(entry_type, qty):
    """Which bridge column a posted entry belongs in. Sign decides direction, because
    BC records a purchase return and a transfer out as negative quantities of the same
    entry type as a receipt and a transfer in."""
    if entry_type == "Purchase":
        return RECEIPT if qty >= 0 else RETURN
    if entry_type == "Transfer":
        return TRANSFER_IN if qty >= 0 else TRANSFER_OUT
    if entry_type == "Assembly Output":
        return ASM_OUT
    if entry_type == "Assembly Consumption":
        return ASM_USE
    if entry_type == "Sale":
        return SALES
    if entry_type == "Positive Adjmt.":
        return POS_ADJ
    if entry_type == "Negative Adjmt.":
        return NEG_ADJ
    return OTHER


def build(ile_rows, opening, closing, cards, skus=None, location="", tolerance=0.001):
    """One row per item for a single location and month."""
    moves = defaultdict(lambda: {c: 0.0 for c in COLUMNS})
    cost = defaultdict(float)
    cost_by_column = defaultdict(lambda: defaultdict(float))
    lots = defaultdict(set)
    expiry = {}
    docs = defaultdict(set)
    for r in ile_rows:
        item = str(r.get("Item_No") or "")
        qty = _f(r.get("Quantity"))
        column = classify(r.get("Entry_Type"), qty)
        moves[item][column] += qty
        posted = _f(r.get("Cost_Amount_Actual")) + _f(r.get("Cost_Amount_Expected"))
        cost[item] += posted
        cost_by_column[item][column] += posted
        if r.get("Lot_No"):
            lots[item].add(r.get("Lot_No"))
        exp = str(r.get("Expiration_Date") or "")[:10]
        if exp and not exp.startswith("0001") and _f(r.get("Remaining_Quantity")) > 0:
            expiry[item] = min(expiry.get(item, exp), exp)
        docs[item].add(r.get("Document_No"))

    rows = []
    for item in sorted(set(moves) | set(opening) | set(closing)):
        m = moves.get(item, {c: 0.0 for c in COLUMNS})
        card = cards.get(item, {})
        open_qty = _f(opening.get(item))
        bc_close = _f(closing.get(item))
        calculated = (open_qty + m[RECEIPT] + m[RETURN] + m[TRANSFER_IN] + m[TRANSFER_OUT]
                      + m[ASM_OUT] + m[ASM_USE] + m[SALES] + m[POS_ADJ] + m[NEG_ADJ] + m[OTHER])
        difference = calculated - bc_close
        unit_cost = _f(card.get("unit_cost"))
        consumption = -(m[ASM_USE] + m[SALES])          # quantity that left the shelf
        posted = cost_by_column.get(item, {})
        # Cost of what actually left this location. Assembly consumption and assembly
        # output are the two halves of one transformation and cancel out, so counting
        # both the ingredient and the finished good doubles the same event. What leaves
        # is the sale, plus the net of the inventory adjustments.
        consumed_cost = -(posted.get(SALES, 0.0) + posted.get(ASM_USE, 0.0) + posted.get(ASM_OUT, 0.0))
        adjustment_cost = posted.get(POS_ADJ, 0.0) + posted.get(NEG_ADJ, 0.0)
        sku = (skus or {}).get((item, location)) or {}
        maximum = sku.get("maximum") or 0
        rows.append({
            "reorder_point": sku.get("reorder_point"), "maximum": maximum or None,
            "reorder_qty": sku.get("reorder_qty"),
            "above_maximum": max(0.0, bc_close - maximum) if maximum > 0 else None,
            "above_maximum_value": (max(0.0, bc_close - maximum) * _f(card.get("unit_cost"))
                                    if maximum > 0 else None),
            "item": item, "description": card.get("desc") or "", "uom": card.get("uom") or "",
            "category": card.get("g3") or card.get("category") or "",
            "sub_category": card.get("g4") or "",
            "vendor": card.get("vendor") or "",
            "opening": open_qty, **{c: m[c] for c in COLUMNS},
            "calculated_closing": calculated, "bc_closing": bc_close, "difference": difference,
            "reconciled": abs(difference) <= tolerance,
            "unit_cost": unit_cost, "closing_value": bc_close * unit_cost,
            "opening_value": open_qty * unit_cost,
            "movement_cost": cost.get(item, 0.0),
            "consumption": consumption,
            "days_cover": (bc_close / (consumption / 30.0)) if consumption > 0 else None,
            "turns": (consumption / ((open_qty + bc_close) / 2)) if (open_qty + bc_close) > 0 else None,
            "consumed_cost": consumed_cost, "adjustment_cost": adjustment_cost,
            "purchase_cost": posted.get(RECEIPT, 0.0) + posted.get(RETURN, 0.0),
            "transfer_cost": posted.get(TRANSFER_IN, 0.0) + posted.get(TRANSFER_OUT, 0.0),
            "is_assembled": abs(posted.get(ASM_OUT, 0.0)) > 0,
            "documents": len(docs.get(item, ())),
            "lots": len(lots.get(item, ())),
            "earliest_expiry": expiry.get(item),
        })
    rows.sort(key=lambda r: -abs(r["closing_value"]))
    return rows


def summary(rows, sku_status=None, location="", month=""):
    """Totals, each with the coverage behind it.

    Quantities, posted costs and the card valuation are kept apart on purpose: a bridge
    that reconciles in quantity says nothing about whether the value is right.
    """
    unreconciled = [r for r in rows if not r["reconciled"]]
    with_cost = [r for r in rows if r["unit_cost"] > 0]
    with_max = [r for r in rows if r.get("maximum")]
    priced_value = sum(r["closing_value"] for r in with_cost)
    sku = sku_status or {}
    return {
        "items": len(rows),
        "closing_value": priced_value,
        "opening_value": sum(r["opening_value"] for r in rows),
        "value_coverage": {"priced": len(with_cost), "items": len(rows),
                           "unpriced": len(rows) - len(with_cost),
                           "method": "Quantity on hand at the month end times the unit cost on the item "
                                     "card. This is a management valuation, not the posted inventory value "
                                     "in the general ledger.",
                           "confidence": "medium" if len(with_cost) < len(rows) else "high"},
        "purchases_cost": sum(r["purchase_cost"] for r in rows),
        "consumed_cost": sum(r["consumed_cost"] for r in rows),
        "consumption_method": "Cost of the sales posted at this location, plus the net of the inventory "
                              "adjustments. Assembly output and assembly consumption are the two halves of "
                              "one transformation and cancel out, so the ingredient and the finished good "
                              "are not both counted.",
        "adjustment_cost": sum(r["adjustment_cost"] for r in rows),
        "adjustment_items": sum(1 for r in rows if abs(r["adjustment_cost"]) > 0),
        "transfer_cost": sum(r["transfer_cost"] for r in rows),
        "negative_stock": sum(1 for r in rows if r["bc_closing"] < 0),
        "negative_value": sum(abs(r["closing_value"]) for r in rows if r["bc_closing"] < 0),
        "no_movement_value": sum(r["closing_value"] for r in rows
                                 if r["consumption"] <= 0 and r["bc_closing"] > 0),
        "no_movement_items": sum(1 for r in rows if r["consumption"] <= 0 and r["bc_closing"] > 0),
        "with_maximum": len(with_max),
        "above_maximum_value": sum(r["above_maximum_value"] or 0 for r in rows),
        "above_maximum_items": sum(1 for r in rows if (r.get("above_maximum") or 0) > 0),
        "maximum_coverage": {"with_maximum": sku.get("with_maximum", 0), "cards": sku.get("cards", 0),
                             "measurable": bool(sku.get("with_maximum"))},
        "unreconciled": len(unreconciled),
        "unreconciled_value": sum(abs(r["difference"]) * r["unit_cost"] for r in unreconciled),
        "reconciled": not unreconciled,
        "status": {
            "quantity": {"state": "reconciled" if not unreconciled else "differences",
                         "detail": (f"{location or 'this location'}, {month}: every item's opening balance "
                                    f"plus the month's posted movements equals the closing balance BC "
                                    f"reports, across {len(rows)} items."
                                    if not unreconciled else
                                    f"{len(unreconciled)} of {len(rows)} items differ from the balance BC "
                                    f"reports.")},
            "valuation": {"state": "management estimate",
                          "detail": (f"{len(with_cost)} of {len(rows)} items carry a unit cost on the card. "
                                     f"The movements are valued at the cost BC posted; the closing balance "
                                     f"at the card cost, which is not the accounting inventory value.")},
            "classification": {"state": "checked" if not unreconciled else "check the differences",
                               "detail": "Every entry type BC publishes is mapped to a column of the bridge. "
                                         "Assembly output and consumption are shown separately and net out "
                                         "in the value figures."},
        },
    }


def exceptions(rows, settings, location, policy=None):
    """Only what the posted records support. Each line says what it is based on."""
    th = settings["thresholds"]
    policy = policy or policy_for(location, parse_cadence((settings.get("stock") or {}).get("cadence")))
    cover_days = policy["alert_days"]
    dead_days = float(th.get("stock_dead_days") or 90)
    floor = float(th.get("stock_value_floor") or 10000)
    adj_pct = float(th.get("adjustment_pct") or 5)
    out = []
    for r in rows:
        value = r["closing_value"]
        if not r["reconciled"]:
            out.append({"code": f"stock-recon-{location}-{r['item']}", "severity": "critical",
                        "rule": "Stock bridge does not reconcile",
                        "title": f"{r['description'] or r['item']}: bridge differs from BC by {r['difference']:,.2f} {r['uom']}",
                        "detail": f"Opening {r['opening']:,.2f} plus the month's movements gives "
                                  f"{r['calculated_closing']:,.2f}, Business Central reports {r['bc_closing']:,.2f}. "
                                  f"Check backdated postings, transfers in transit and unit conversions before "
                                  f"using this item's figures.",
                        "impact": abs(r["difference"]) * r["unit_cost"], "item": r["item"],
                        "location": location, "confidence": "high",
                        "next_action": "Investigate the item's entries in BC for the month.",
                        "owner_role": "Cost Controller"})
        if r["bc_closing"] < 0:
            out.append({"code": f"stock-negative-{location}-{r['item']}", "severity": "critical",
                        "rule": "Negative stock",
                        "title": f"{r['description'] or r['item']}: closing stock {r['bc_closing']:,.2f} {r['uom']}",
                        "detail": "Consumption has been posted for stock that was never received. Usually a missing "
                                  "receipt, a transfer posted one way, or an assembly posted before its components.",
                        "impact": abs(r["bc_closing"]) * r["unit_cost"], "item": r["item"],
                        "location": location, "confidence": "high",
                        "next_action": "Post the missing receipt or correct the entry in BC.",
                        "owner_role": "Stock Manager"})
        if r["days_cover"] is not None and r["days_cover"] > cover_days and value >= floor:
            out.append({"code": f"stock-cover-{location}-{r['item']}", "severity": "warning",
                        "rule": "Stock cover above the agreed threshold",
                        "title": f"{r['description'] or r['item']}: {r['days_cover']:,.0f} days of cover",
                        "detail": f"Closing {r['bc_closing']:,.2f} {r['uom']} against {r['consumption']:,.2f} used in "
                                  f"the month. At that rate the stock lasts {r['days_cover']:,.0f} days, against "
                                  f"{policy['max_days']:,.0f} days of cover this location needs "
                                  f"({policy['label']}); the alert sits at {cover_days:,.0f} days. Check "
                                  f"seasonality and any event before cutting the order.",
                        "impact": max(0.0, value - (r["consumption"] / 30.0 * policy["max_days"] * r["unit_cost"])),
                        "item": r["item"], "location": location, "confidence": "medium",
                        "next_action": "Pause or reduce the next order, or move stock where it is used.",
                        "owner_role": "Purchasing Coordinator"})
        if r["consumption"] <= 0 and r["bc_closing"] > 0 and value >= floor:
            out.append({"code": f"stock-dead-{location}-{r['item']}", "severity": "warning",
                        "rule": "No consumption in the month",
                        "title": f"{r['description'] or r['item']}: {value:,.0f} {currency.code()} sitting unused",
                        "detail": f"Closing {r['bc_closing']:,.2f} {r['uom']} with nothing consumed this month"
                                  + (f", and {r[RECEIPT]:,.2f} still received." if r[RECEIPT] > 0 else "."),
                        "impact": value, "item": r["item"], "location": location, "confidence": "high",
                        "next_action": "Check condition and expiry, then move it or stop buying it.",
                        "owner_role": "Unit Manager"})
        if r[RECEIPT] > 0 and r["opening"] > 0 and r["consumption"] > 0:
            cover_at_start = r["opening"] / (r["consumption"] / 30.0)
            if cover_at_start > cover_days and value >= floor:
                out.append({"code": f"stock-rebuy-{location}-{r['item']}", "severity": "warning",
                            "rule": "Bought while stock was already long",
                            "title": f"{r['description'] or r['item']}: bought {r[RECEIPT]:,.2f} {r['uom']} on "
                                     f"{cover_at_start:,.0f} days of cover",
                            "detail": f"The month opened with {r['opening']:,.2f} {r['uom']}, enough for "
                                      f"{cover_at_start:,.0f} days at this month's usage, and "
                                      f"{r[RECEIPT]:,.2f} more was received.",
                            "impact": r[RECEIPT] * r["unit_cost"], "item": r["item"], "location": location,
                            "confidence": "medium",
                            "next_action": "Review the order cycle and pack size with the supplier.",
                            "owner_role": "Purchasing Coordinator"})
        if r.get("maximum") and (r.get("above_maximum") or 0) > 0 and (r["above_maximum_value"] or 0) >= floor:
            out.append({"code": f"stock-max-{location}-{r['item']}", "severity": "warning",
                        "rule": "Closing stock above the maximum set on the SKU card",
                        "title": f"{r['description'] or r['item']}: {r['above_maximum']:,.2f} {r['uom']} above the "
                                 f"maximum of {r['maximum']:,.2f}",
                        "detail": f"Closing {r['bc_closing']:,.2f} {r['uom']} against a maximum of "
                                  f"{r['maximum']:,.2f} set in Business Central for this location. The limit is "
                                  f"the business's own, not a threshold of this application.",
                        "impact": r["above_maximum_value"], "item": r["item"], "location": location,
                        "confidence": "high",
                        "next_action": "Stop ordering until the balance is back inside the limit, or revise "
                                       "the maximum if it no longer matches demand.",
                        "owner_role": "Purchasing Coordinator"})
        if r.get("reorder_point") and r["consumption"] > 0:
            days_at_point = r["reorder_point"] / (r["consumption"] / 30.0)
            if days_at_point < policy["reorder_days"] and r["closing_value"] >= floor:
                out.append({"code": f"stock-rop-low-{location}-{r['item']}", "severity": "warning",
                            "rule": "Reorder point below the lead time",
                            "title": f"{r['description'] or r['item']}: reorder point {r['reorder_point']:,.2f} "
                                     f"{r['uom']} is {days_at_point:,.1f} days of use",
                            "detail": f"At this month's usage of {r['consumption']:,.2f} {r['uom']}, the reorder "
                                      f"point triggers with {days_at_point:,.1f} days of stock left, while this "
                                      f"location needs {policy['reorder_days']:,.0f} days to be resupplied "
                                      f"({policy['label']}). It will run out before the delivery arrives.",
                            "impact": 0, "item": r["item"], "location": location, "confidence": "medium",
                            "next_action": "Review the reorder point against the supplier's delivery time.",
                            "owner_role": "Purchasing Coordinator"})
        moved = abs(r[RECEIPT]) + abs(r[ASM_USE]) + abs(r[SALES]) + abs(r[TRANSFER_IN]) + abs(r[TRANSFER_OUT])
        adjustment = abs(r[POS_ADJ] + r[NEG_ADJ])
        if moved > 0 and adjustment / moved * 100 >= adj_pct and adjustment * r["unit_cost"] >= floor:
            out.append({"code": f"stock-adj-{location}-{r['item']}", "severity": "warning",
                        "rule": "Large count adjustment",
                        "title": f"{r['description'] or r['item']}: adjustments are "
                                 f"{adjustment / moved * 100:,.1f}% of the month's movement",
                        "detail": f"Positive {r[POS_ADJ]:,.2f}, negative {r[NEG_ADJ]:,.2f} {r['uom']} against "
                                  f"{moved:,.2f} moved. This is a count or posting difference, not consumption.",
                        "impact": adjustment * r["unit_cost"], "item": r["item"], "location": location,
                        "confidence": "high",
                        "next_action": "Review the count sheets and the posting behind the adjustment.",
                        "owner_role": "Cost Controller"})
    out.sort(key=lambda e: (0 if e["severity"] == "critical" else 1, -abs(e["impact"])))
    return out


def proposed_parameters(rows, policy):
    """A starting point for items with no maximum set, built from the location's own
    delivery rhythm and the month's usage.

    Reorder point covers the lead time plus the safety days, so the order is placed
    before the shelf empties. Maximum covers lead time, the gap between orders and the
    safety days, so a full delivery still fits inside the limit. It is a proposal for a
    human to review: it assumes the month is representative, which a seasonal item or a
    month with an event is not, and it takes no account of shelf life or pack size.
    """
    out = []
    for r in rows:
        if r.get("maximum") or r["consumption"] <= 0:
            continue
        daily = r["consumption"] / 30.0
        out.append({"item": r["item"], "description": r["description"], "uom": r["uom"],
                    "usage_month": r["consumption"], "daily": round(daily, 3),
                    "closing": r["bc_closing"], "closing_value": r["closing_value"],
                    "proposed_reorder_point": round(daily * policy["reorder_days"], 2),
                    "proposed_maximum": round(daily * policy["max_days"], 2),
                    "value_at_proposed": round(daily * policy["max_days"] * r["unit_cost"], 2),
                    "excess_against_proposal": round(max(0.0, r["bc_closing"] - daily * policy["max_days"])
                                                     * r["unit_cost"], 2)})
    out.sort(key=lambda x: -x["closing_value"])
    return out[:60]


# ---- the management view: categories, causes and this week's decisions ----

FRESH_HINTS = ("USHQIME", "MISH", "PERIME", "FRUTA", "BULMET", "PESHK", "FRESH")
DRINK_HINTS = ("PIJE", "BAR", "ALCOHOL", "WINE", "BIRRA")


def category_view(rows, policy):
    """Cover by category, because one number for a whole restaurant averages olive oil
    with lettuce and means nothing."""
    groups = defaultdict(lambda: {"value": 0.0, "consumption_cost": 0.0, "items": 0,
                                  "no_movement": 0.0, "negative": 0})
    for r in rows:
        key = r.get("category") or r.get("sub_category") or "(no category on the card)"
        g = groups[key]
        g["value"] += r["closing_value"]
        g["consumption_cost"] += r["consumed_cost"]
        g["items"] += 1
        if r["consumption"] <= 0 and r["bc_closing"] > 0:
            g["no_movement"] += r["closing_value"]
        if r["bc_closing"] < 0:
            g["negative"] += 1
    out = []
    for name, g in groups.items():
        daily = g["consumption_cost"] / 30.0
        cover = (g["value"] / daily) if daily > 0 else None
        fresh = any(h in name.upper() for h in FRESH_HINTS)
        target = policy["max_days"] * (0.5 if fresh else 1.0)
        out.append({"category": name, "value": g["value"], "items": g["items"],
                    "consumption_cost": g["consumption_cost"], "no_movement_value": g["no_movement"],
                    "negative": g["negative"], "days_cover": cover, "target_days": target,
                    "excess_value": (max(0.0, g["value"] - daily * target) if daily > 0 else None),
                    "kind": "fresh" if fresh else "drinks" if any(h in name.upper() for h in DRINK_HINTS)
                            else "other",
                    "basis": ("Cover is this category's closing value divided by its own daily cost of "
                              "sales; fresh categories are held to half the location's normal cover."
                              if daily > 0 else "No consumption posted, so cover cannot be calculated.")})
    out.sort(key=lambda c: -(c["excess_value"] or 0))
    return out


def causes(rows, totals, categories, policy, sku_status):
    """Why the capital is sitting there, stated as confirmed, possible, or not knowable."""
    out = []
    purchases, consumed = totals["purchases_cost"], totals["consumed_cost"]
    if purchases > consumed * 1.15 and consumed > 0:
        out.append({"cause": "Bought more than was used", "confidence": "confirmed",
                    "value": purchases - consumed,
                    "detail": f"Purchases cost {purchases:,.0f} against {consumed:,.0f} consumed, so "
                              f"{purchases - consumed:,.0f} stayed on the shelf this month."})
    if totals["no_movement_value"] > 0:
        out.append({"cause": "Stock that did not move at all", "confidence": "confirmed",
                    "value": totals["no_movement_value"],
                    "detail": f"{totals['no_movement_items']} items with a balance and no consumption "
                              f"posted in the month."})
    fresh_excess = sum(c["excess_value"] or 0 for c in categories if c["kind"] == "fresh")
    if fresh_excess > 0:
        out.append({"cause": "Fresh categories above the cover they should hold", "confidence": "possible",
                    "value": fresh_excess,
                    "detail": "Fresh goods are judged against half the location's normal cover. Shelf life "
                              "is not published in BC, so this is a signal, not a confirmed loss."})
    if abs(totals["adjustment_cost"]) > 0:
        out.append({"cause": "Inventory count differences", "confidence": "confirmed",
                    "value": abs(totals["adjustment_cost"]),
                    "detail": f"{totals['adjustment_items']} items carry a count adjustment, net "
                              f"{totals['adjustment_cost']:,.0f}. A count difference is neither purchase "
                              f"nor consumption; it is a correction."})
    if totals["negative_stock"]:
        out.append({"cause": "Negative balances", "confidence": "confirmed",
                    "value": totals["negative_value"],
                    "detail": f"{totals['negative_stock']} items close below zero, which means consumption "
                              f"was posted against stock that was never received."})
    if not sku_status.get("with_maximum"):
        out.append({"cause": "No stock limits set to measure against", "confidence": "not knowable",
                    "value": 0,
                    "detail": f"{sku_status.get('with_maximum', 0)} of {sku_status.get('cards', 0)} "
                              f"stockkeeping unit cards at this location carry a maximum inventory, so "
                              f"excess cannot be measured against the business's own limit. Everything "
                              f"above is measured against this location's delivery rhythm instead "
                              f"({policy['label']})."})
    unpriced = totals["value_coverage"]["unpriced"]
    if unpriced:
        out.append({"cause": "Items with no cost on the card", "confidence": "not knowable", "value": 0,
                    "detail": f"{unpriced} items have a balance but no unit cost, so their value is not in "
                              f"any figure on this screen."})
    out.sort(key=lambda c: -(c["value"] or 0))
    return out


def decisions(rows, categories, policy, location, settings, limit=10):
    """At most ten calls to make this week, ranked by the money they move."""
    floor = float(settings["thresholds"].get("stock_value_floor") or 10000)
    out = []
    for r in rows:
        if r["bc_closing"] < 0 and abs(r["closing_value"]) >= floor:
            out.append({"unit": location, "category": r["category"], "item": r["item"],
                        "problem": f"{r['description'] or r['item']} closes at {r['bc_closing']:,.2f} "
                                   f"{r['uom']}, below zero.",
                        "evidence": f"Opening {r['opening']:,.2f}, received {r['receipts']:,.2f}, used "
                                    f"{-(r['assembly_consumption'] + r['sales']):,.2f} in the month.",
                        "action": "Find the missing receipt or transfer and post it.",
                        "value": abs(r["closing_value"]), "owner": "Stock Manager",
                        "urgency": "This week", "confidence": "confirmed", "reference": r["item"]})
        elif r["consumption"] <= 0 and r["closing_value"] >= floor:
            out.append({"unit": location, "category": r["category"], "item": r["item"],
                        "problem": f"{r['description'] or r['item']} did not move at all this month.",
                        "evidence": f"{r['bc_closing']:,.2f} {r['uom']} on hand"
                                    + (f", and {r['receipts']:,.2f} still received." if r["receipts"] > 0
                                       else "."),
                        "action": "Check condition and expiry, then move it to a unit that uses it or stop "
                                  "ordering it.",
                        "value": r["closing_value"], "owner": "Unit Manager",
                        "urgency": "This week", "confidence": "confirmed", "reference": r["item"]})
        elif (r["days_cover"] is not None and r["days_cover"] > policy["alert_days"]
              and r["closing_value"] >= floor):
            out.append({"unit": location, "category": r["category"], "item": r["item"],
                        "problem": f"{r['description'] or r['item']} holds {r['days_cover']:,.0f} days of "
                                   f"cover against {policy['max_days']:,.0f} needed.",
                        "evidence": f"Closing {r['bc_closing']:,.2f} {r['uom']} against "
                                    f"{r['consumption']:,.2f} used in the month.",
                        "action": "Pause the next order for this item.",
                        "value": max(0.0, r["closing_value"] - r["consumption"] / 30.0
                                     * policy["max_days"] * r["unit_cost"]),
                        "owner": "Purchasing Coordinator", "urgency": "Before the next order",
                        "confidence": "possible", "reference": r["item"]})
        elif abs(r["adjustment_cost"]) >= floor:
            out.append({"unit": location, "category": r["category"], "item": r["item"],
                        "problem": f"{r['description'] or r['item']} carries a count adjustment of "
                                   f"{r['adjustment_cost']:,.0f}.",
                        "evidence": f"Positive {r['positive_adjustment']:,.2f}, negative "
                                    f"{r['negative_adjustment']:,.2f} {r['uom']}.",
                        "action": "Review the count sheet and the posting behind it.",
                        "value": abs(r["adjustment_cost"]), "owner": "Cost Controller",
                        "urgency": "This month", "confidence": "confirmed", "reference": r["item"]})
    rank = {"confirmed": 0, "possible": 1, "not knowable": 2}
    out.sort(key=lambda d: (rank.get(d["confidence"], 3), -d["value"]))
    return out[:limit]


def transfer_opportunities(by_location, settings):
    """Same item long at one restaurant and short at another. A proposal, not an order:
    condition, expiry and transfer cost are not in the published data."""
    floor = float(settings["thresholds"].get("stock_value_floor") or 10000)
    cover_days = float(settings["thresholds"].get("stock_cover_days") or 45)
    by_item = defaultdict(list)
    for loc, rows in by_location.items():
        for r in rows:
            by_item[r["item"]].append((loc, r))
    out = []
    for item, entries in by_item.items():
        longs = [(l, r) for l, r in entries
                 if r["days_cover"] is not None and r["days_cover"] > cover_days and r["closing_value"] >= floor]
        shorts = [(l, r) for l, r in entries
                  if r["consumption"] > 0 and (r["days_cover"] is None or r["days_cover"] < 7)]
        for (lloc, lr) in longs:
            for (sloc, sr) in shorts:
                out.append({
                    "item": item, "description": lr["description"], "uom": lr["uom"],
                    "from": lloc, "from_cover": lr["days_cover"], "from_stock": lr["bc_closing"],
                    "to": sloc, "to_cover": sr["days_cover"], "to_usage": sr["consumption"],
                    "suggested_qty": min(lr["bc_closing"] / 2, max(0.0, sr["consumption"] / 30.0 * 14)),
                    "value": min(lr["bc_closing"] / 2, max(0.0, sr["consumption"] / 30.0 * 14)) * lr["unit_cost"],
                })
    out.sort(key=lambda t: -t["value"])
    return out[:40]
