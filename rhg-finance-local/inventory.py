"""Inventory figures for the Control Tower.

The item ledger grows by roughly 290,000 entries a month, so quantities are not
summed here. They are read from the item card's Inventory flow field, which BC
calculates for the location and date passed as filters. Older snapshots of the
same field are used to see what has and has not moved.

Value is quantity multiplied by the item's unit cost. That is an indication, not
the posted inventory value: the posted value lives in the value entries and in
the G/L, and the difference between the two is shown rather than hidden.
"""
from collections import defaultdict


def _f(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def snapshot(rows):
    """Item card rows -> {item_no: {...}} for the items that carry stock.

    InventorybyDate answers the Date_Filter that was asked for; InventoryField ignores
    it and always returns today's stock. Reading the wrong one made every historical
    snapshot identical to the present, which made the slow-moving and dead-stock split
    meaningless. The date-aware field is used, and the other only as a fallback for a
    snapshot taken as at today.
    """
    out = {}
    for r in rows or []:
        qty = _f(r.get("InventorybyDate") if r.get("InventorybyDate") is not None
                 else r.get("InventoryField"))
        if qty == 0:
            continue
        cost = _f(r.get("Unit_Cost"))
        out[str(r.get("No"))] = {
            "item": str(r.get("No")), "description": (r.get("Description") or "").strip(),
            "quantity": qty, "unit_cost": cost, "value": qty * cost,
            "category": r.get("Item_Category_Code") or "", "uom": r.get("Base_Unit_of_Measure") or "",
            "costing": r.get("Costing_Method") or "", "blocked": bool(r.get("Blocked")),
            "last_cost": _f(r.get("Last_Direct_Cost")), "vendor": r.get("Vendor_No") or "",
        }
    return out


def overview(cur, history, settings, gl_inventory=None, location=""):
    """cur: snapshot now. history: {days: snapshot} for the trailing windows."""
    th = settings["thresholds"]
    items = list(cur.values())
    total_value = sum(i["value"] for i in items)
    total_qty = sum(i["quantity"] for i in items)

    by_category = defaultdict(lambda: {"value": 0.0, "quantity": 0.0, "items": 0})
    for i in items:
        c = by_category[i["category"] or "(no category)"]
        c["value"] += i["value"]
        c["quantity"] += i["quantity"]
        c["items"] += 1
    categories = sorted(({"category": k, **v} for k, v in by_category.items()),
                        key=lambda x: -x["value"])

    negative = sorted((i for i in items if i["quantity"] < 0), key=lambda i: i["quantity"])
    zero_cost = [i for i in items if i["quantity"] > 0 and i["unit_cost"] == 0]

    # --- movement and stillness, measured from the snapshots ---
    aged, moved_value = [], 0.0
    for i in items:
        entry = {"still_days": None, "change_30": None}
        for days in sorted(history):
            old = history[days].get(i["item"])
            old_qty = old["quantity"] if old else 0.0
            changed = abs(i["quantity"] - old_qty) > 0.0001
            if days == th["stock_monitor_days"]:
                entry["change_30"] = i["quantity"] - old_qty
                moved_value += abs(i["quantity"] - old_qty) * i["unit_cost"]
            if not changed:
                entry["still_days"] = days          # unchanged at least this long
        aged.append({**i, **entry})

    def classify(row):
        d = row["still_days"]
        if d is None:
            return "Active"
        if d >= th["stock_dead_days"]:
            return "Non-moving"
        if d >= th["stock_slow_days"]:
            return "Slow-moving"
        if d >= th["stock_monitor_days"]:
            return "Monitor"
        return "Active"

    for row in aged:
        row["class"] = classify(row)
    risk = sorted((r for r in aged if r["class"] != "Active" and r["value"] >= th["stock_value_floor"]),
                  key=lambda r: -r["value"])
    class_totals = defaultdict(lambda: {"value": 0.0, "items": 0})
    for r in aged:
        t = class_totals[r["class"]]
        t["value"] += r["value"]
        t["items"] += 1

    reconciliation = None
    if gl_inventory is not None:
        diff = total_value - gl_inventory
        reconciliation = {
            "inventory_value": total_value, "gl_balance": gl_inventory, "difference": diff,
            "difference_pct": (diff / gl_inventory * 100) if gl_inventory else None,
            "method": "Item quantity multiplied by the item card's unit cost, against the balance of the "
                      "inventory accounts named in Settings. A difference is normal when cost adjustment "
                      "has not run; a large or growing difference is not.",
        }

    return {
        "location": location or "All locations",
        "totals": {"value": total_value, "quantity": total_qty, "items": len(items),
                   "negative_items": len(negative),
                   "negative_value": sum(i["value"] for i in negative),
                   "moved_value_30": moved_value,
                   "at_risk_value": sum(r["value"] for r in risk)},
        "categories": categories[:25],
        "top_items": sorted(items, key=lambda i: -i["value"])[:40],
        "negative": negative[:60],
        "zero_cost": zero_cost[:60],
        "risk": risk[:120],
        "classes": [{"class": k, **v} for k, v in sorted(class_totals.items(), key=lambda kv: -kv[1]["value"])],
        "reconciliation": reconciliation,
        "note": "Quantities come from the item card's Inventory flow field, calculated by BC for the "
                "selected location and date. Movement is the change between snapshots, so an item that "
                "moved out and back in on equal quantities reads as unchanged.",
    }


def exceptions(inv, settings):
    th = settings["thresholds"]
    out = []
    for i in inv["negative"][:20]:
        out.append({"code": f"inv-neg-{i['item']}", "severity": "critical", "area": "Inventory",
                    "rule": "Negative inventory",
                    "title": f"{i['description'] or i['item']}: {i['quantity']:,.2f} {i['uom']} on hand",
                    "detail": f"Negative stock at {inv['location']} distorts cost and makes valuation "
                              f"unreliable until it is corrected.",
                    "impact": abs(i["value"]), "document": i["item"],
                    "next_action": "Post the missing receipt or correct the consumption.",
                    "drill": {"kind": "inventory_negative", "key": ""}})
    dead = [r for r in inv["risk"] if r["class"] == "Non-moving"]
    if dead:
        value = sum(r["value"] for r in dead)
        out.append({"code": f"inv-dead-{inv['location']}", "severity": "warning", "area": "Inventory",
                    "rule": "Non-moving stock",
                    "title": f"{len(dead)} items have not moved for {th['stock_dead_days']:.0f} days, "
                             f"{value:,.0f} at cost",
                    "detail": "Measured as no change in the on-hand quantity between snapshots.",
                    "impact": value, "document": "",
                    "next_action": "Decide per item: consume, transfer, return or write down.",
                    "drill": {"kind": "inventory_risk", "key": ""}})
    if inv["zero_cost"]:
        out.append({"code": f"inv-zero-{inv['location']}", "severity": "warning", "area": "Inventory",
                    "rule": "Stock held at zero cost",
                    "title": f"{len(inv['zero_cost'])} items hold stock with a unit cost of zero",
                    "detail": "Zero-cost stock understates inventory value and cost of sales.",
                    "impact": 0, "document": "",
                    "next_action": "Run the cost adjustment, or correct the item cost.",
                    "drill": {"kind": "inventory_zero", "key": ""}})
    rec = inv.get("reconciliation")
    if rec and abs(rec["difference"]) >= th["inventory_difference"]:
        out.append({"code": "inv-gl-diff", "severity": "warning", "area": "Inventory",
                    "rule": "Inventory does not reconcile with the G/L",
                    "title": f"Inventory value differs from the G/L by {rec['difference']:,.0f}",
                    "detail": f"Item value {rec['inventory_value']:,.0f} against the inventory accounts "
                              f"{rec['gl_balance']:,.0f}. {rec['method']}",
                    "impact": abs(rec["difference"]), "document": "",
                    "next_action": "Run the cost adjustment, then investigate what remains.",
                    "drill": {"kind": "inventory_top", "key": ""}})
    return out
