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
from datetime import date


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
            "posting_group": (r.get("Inventory_Posting_Group") or "").strip(),
        }
    return out


# The same rule the purchasing tower uses, so both towers answer the question the same
# way: stock has moved if anything left the location or arrived at it, whatever door it
# used. Transfers are how this group moves stock between MAGAZINE and the restaurants,
# and a sales return receipt is posted as a sale with a positive quantity, so it is read
# by its sign and counted as stock coming back in.
OUT_TYPES = ("Assembly Consumption", "Negative Adjmt.")
IN_TYPES = ("Purchase", "Positive Adjmt.", "Assembly Output")


def movement_summary(entries):
    """{item: last outflow date, quantities and cost by direction} from posted entries."""
    out = {}
    for e in entries or []:
        item = str(e.get("Item_No") or "")
        if not item:
            continue
        qty = _f(e.get("Quantity"))
        kind = (e.get("Entry_Type") or "").strip()
        cost = _f(e.get("Cost_Amount_Actual"))
        date_text = str(e.get("Posting_Date") or "")[:10]
        rec = out.setdefault(item, {"out_qty": 0.0, "in_qty": 0.0, "out_cost": 0.0, "in_cost": 0.0,
                                    "last_out": "", "last_in": "", "entries": 0})
        rec["entries"] += 1
        leaving = (kind in OUT_TYPES or (kind == "Sale" and qty < 0)
                   or (kind == "Transfer" and qty < 0) or (kind == "Purchase" and qty < 0))
        if kind == "Assembly Output" or (kind == "Sale" and qty > 0):
            leaving = False
        if leaving:
            rec["out_qty"] += abs(qty)
            rec["out_cost"] += abs(cost)
            rec["last_out"] = max(rec["last_out"], date_text)
        else:
            rec["in_qty"] += abs(qty)
            rec["in_cost"] += abs(cost)
            rec["last_in"] = max(rec["last_in"], date_text)
    return out


def overview(cur, history, settings, gl_inventory=None, location="", movements=None, as_of=None):
    """cur: snapshot now. history: {days: snapshot} for the trailing windows.

    movements: the posted ledger movements, when they could be read. They decide whether
    an item has moved; the snapshots can only show a net change, so an item that was
    shipped out and replaced looks untouched and an item nobody used looks the same as
    one used every day.
    """
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

    today = as_of or date.today()
    if movements is not None:
        for row in aged:
            m = movements.get(row["item"])
            row["last_out"] = (m or {}).get("last_out") or ""
            row["out_qty"] = (m or {}).get("out_qty") or 0.0
            row["in_qty"] = (m or {}).get("in_qty") or 0.0
            if row["last_out"]:
                try:
                    row["still_days"] = (today - date.fromisoformat(row["last_out"])).days
                except ValueError:
                    pass
            else:
                row["still_days"] = th["stock_dead_days"]      # nothing left it in the window
            row["movement_basis"] = "posted ledger movements"

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

    provision = provision_proposal(class_totals, settings)

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
        "provision": provision,
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


# ------------------------------------------------------------------ finance summary
def _codes(text):
    return [c.strip() for c in (text or "").split(",") if c.strip()]


def parse_rates(text):
    """'Slow-moving=25, Non-moving=50' -> {'Slow-moving': 25.0, 'Non-moving': 50.0}."""
    out = {}
    for part in (text or "").split(","):
        key, _, value = part.partition("=")
        try:
            out[key.strip()] = max(0.0, min(100.0, float(value)))
        except ValueError:
            continue
    return out


def provision_proposal(class_totals, settings):
    """A write-down proposal per stock class, at the rates set in Settings.

    It is a proposal for the accountant, not a figure BC holds: nothing is posted, and the
    rates are the group's own policy, entered in Settings."""
    rates = parse_rates((settings.get("inventory") or {}).get("provision_rates"))
    lines = []
    for cls, rate in rates.items():
        t = class_totals.get(cls) or {"value": 0.0, "items": 0}
        lines.append({"class": cls, "rate": rate, "value": t["value"], "items": t["items"],
                      "provision": t["value"] * rate / 100})
    return {"lines": lines, "total": sum(l["provision"] for l in lines),
            "basis": "Value at item cost of each stock class, times the rate set in Settings. "
                     "A proposal for review: nothing is posted to Business Central."}


def finance_summary(cards, loc_close, loc_open, accounts, cogs_accounts, settings,
                    as_of, opening_date, cogs_days):
    """The stock position in accounting terms.

    cards:         snapshot() of every item at as_of, all locations: quantity, unit cost
                   and inventory posting group.
    loc_close/open:{location: {item: quantity}} at as_of and at opening_date.
    accounts:      chart of accounts for the period (Balance_at_Date = closing balance,
                   Net_Change = movement of the period).
    cogs_accounts: chart of accounts for the trailing cogs_days, for days of inventory.
    """
    inv = settings.get("inventory") or {}
    th = settings["thresholds"]
    wanted = _codes(inv.get("accounts"))
    excluded = set(_codes(inv.get("dio_exclude")))
    cost = {k: v["unit_cost"] for k, v in cards.items()}

    # --- by location: opening and closing at the same unit cost, so the change is quantity
    locations = []
    for loc in sorted(set(loc_close) | set(loc_open)):
        close_q, open_q = loc_close.get(loc) or {}, loc_open.get(loc) or {}
        close_v = sum(q * cost.get(i, 0.0) for i, q in close_q.items())
        open_v = sum(q * cost.get(i, 0.0) for i, q in open_q.items())
        neg = [(i, q) for i, q in close_q.items() if q < 0]
        if not close_q and not open_q:
            continue
        locations.append({"location": loc, "opening": open_v, "closing": close_v,
                          "change": close_v - open_v,
                          "change_pct": ((close_v - open_v) / open_v * 100) if open_v else None,
                          "items": sum(1 for q in close_q.values() if q > 0),
                          "negative_items": len(neg),
                          "negative_value": sum(q * cost.get(i, 0.0) for i, q in neg),
                          "no_cost_items": sum(1 for i, q in close_q.items() if q > 0 and not cost.get(i))})
    locations.sort(key=lambda r: -r["closing"])
    loc_total_close = sum(r["closing"] for r in locations)
    for r in locations:
        r["share_pct"] = (r["closing"] / loc_total_close * 100) if loc_total_close else 0.0

    # --- G/L by inventory account, against the item value posted to the same account
    by_group = defaultdict(lambda: {"value": 0.0, "items": 0})
    for c in cards.values():
        g = by_group[c.get("posting_group") or ""]
        g["value"] += c["value"]
        g["items"] += 1
    acc = {str(r.get("No")): r for r in accounts or []}
    rows = []
    for no in wanted:
        r = acc.get(no)
        closing = _f((r or {}).get("Balance_at_Date"))
        change = _f((r or {}).get("Net_Change"))
        item_value = by_group.get(no, {}).get("value", 0.0)
        rows.append({"account": no, "name": ((r or {}).get("Name") or "").strip(),
                     "found": r is not None, "opening": closing - change, "closing": closing,
                     "change": change, "debit": _f((r or {}).get("Debit_Amount")),
                     "credit": _f((r or {}).get("Credit_Amount")),
                     "item_value": item_value, "items": by_group.get(no, {}).get("items", 0),
                     "difference": item_value - closing,
                     "flag": abs(item_value - closing) >= th["inventory_difference"],
                     "in_dio": no not in excluded})
    unmapped = sorted(({"posting_group": g or "(none)", **v} for g, v in by_group.items()
                       if g not in wanted and abs(v["value"]) > 0.5), key=lambda x: -abs(x["value"]))
    gl_close = sum(r["closing"] for r in rows)
    gl_open = sum(r["opening"] for r in rows)
    item_total = sum(c["value"] for c in cards.values())

    # --- days of inventory, from BC's own cost-of-goods-sold category
    cogs_rows = [r for r in cogs_accounts or []
                 if (r.get("Account_Category") or "") == "Cost of Goods Sold"
                 and (r.get("Account_Type") or "Posting") == "Posting"]
    cogs = sum(_f(r.get("Net_Change")) for r in cogs_rows)
    consumable = sum(r["closing"] for r in rows if r["in_dio"])
    dio, dio_state = None, "ok"
    if not wanted:
        dio_state = "not_configured"
    elif cogs <= 0:
        dio_state = "under_validation"
    else:
        dio = consumable / (cogs / cogs_days)

    top_cogs = sorted(({"account": str(r.get("No")), "name": (r.get("Name") or "").strip(),
                        "amount": _f(r.get("Net_Change"))} for r in cogs_rows if _f(r.get("Net_Change"))),
                      key=lambda x: -abs(x["amount"]))

    return {
        "as_of": as_of, "opening_date": opening_date,
        "totals": {"gl_closing": gl_close, "gl_opening": gl_open, "gl_change": gl_close - gl_open,
                   "item_value": item_total, "difference": item_total - gl_close,
                   "difference_pct": ((item_total - gl_close) / gl_close * 100) if gl_close else None,
                   "location_value": loc_total_close,
                   "location_opening": sum(r["opening"] for r in locations),
                   "negative_locations": sum(1 for r in locations if r["negative_items"]),
                   "negative_value": sum(r["negative_value"] for r in locations)},
        "locations": locations,
        "accounts": rows,
        "unmapped": unmapped,
        "dio": {"days": dio, "state": dio_state, "consumable_stock": consumable,
                "cogs": cogs, "cogs_days": cogs_days, "daily_cogs": (cogs / cogs_days) if cogs_days else 0,
                "excluded": sorted(excluded), "accounts": top_cogs[:12],
                "method": f"Closing balance of the inventory accounts, less {', '.join(sorted(excluded)) or 'none'}, "
                          f"divided by the average daily cost of goods sold over the last {cogs_days} days. "
                          f"Cost of goods sold is every posting account Business Central itself files under "
                          f"the category 'Cost of Goods Sold'."},
        "notes": [
            "Location values are quantity on hand times today's unit cost on the item card, at both "
            "dates. The change therefore shows quantity, not revaluation.",
            "The G/L column is the posted balance of each inventory account. The item value beside it "
            "is every item whose inventory posting group carries the same code.",
            "Items in transit sit on the TRANSIT location and are counted there.",
        ],
    }
