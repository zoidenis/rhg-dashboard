"""Inventory → Item cards → Stock by location: every item card and how much of it each warehouse holds.

Read from Business Central, read-only, the way Movement & dead stock and Stock count analysis read stock:
  * the quantity of each item at each location on a date is the item card's InventorybyDate flow field,
    evaluated by BC for that location (bc_client.stock_by_location, only the items not at zero);
  * the company total is the same flow field without a location (bc_client.item_snapshot); what it holds beyond
    the sum of the locations is stock posted without a location code, shown as "(no location)";
  * the item cards (description, base unit, category groups, unit cost, blocked) come from the master read of
    Recipe integrity (recipes.master, every six hours);
  * value = quantity × the item card's unit cost: an estimate (the posted value is in the value entries and the
    G/L, which Inventory → Overview reconciles).
A person limited to some locations sees every warehouse here: what the other warehouses hold is what they need to
ask for a transfer.
"""
import io
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

import heldjob
import locintel
import recipes

KEEP = 900
_held = heldjob.Held("itemstock", KEEP)
NO_LOC = "(no location)"


def _f(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def build(client, company, as_of, progress=lambda s: None):
    progress("reading the item cards")
    master = recipes.master(client, company)
    cards = master.get("items") or {}
    progress("reading the locations")
    locations = client.locations(company)
    names = {}
    try:
        for r in locintel.registry():
            if (r.get("company") or "") == company and r.get("bc"):
                names.setdefault(str(r["bc"]).upper(), r.get("name") or "")
    except Exception:                                                  # noqa: BLE001
        pass
    progress(f"reading the stock of {len(locations)} locations")
    by_loc, failed = {}, []
    age = 900 if as_of >= date.today() else 86400
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {loc: pool.submit(client.stock_by_location, company, loc, as_of.isoformat(), age) for loc in locations}
        futures["__total__"] = pool.submit(client.item_snapshot, company, as_of.isoformat(), None, age)
        for loc, fut in futures.items():
            try:
                by_loc[loc] = fut.result()
            except Exception as exc:                                   # noqa: BLE001
                failed.append(f"{loc}: {str(exc)[:160]}")
    total_rows = by_loc.pop("__total__", None) or []
    totals = {str(r.get("No")): _f(r.get("InventorybyDate")) for r in total_rows if r.get("No")}
    progress("putting it together")
    qty = {}
    for loc, m in by_loc.items():
        for item, q in m.items():
            if q:
                qty.setdefault(item, {})[loc] = round(q, 4)
    for item, t in totals.items():
        rest = t - sum((qty.get(item) or {}).values())
        if abs(rest) > 1e-6 and not failed:
            qty.setdefault(item, {})[NO_LOC] = round(rest, 4)
    items, loc_sum = [], {}
    for no, c in cards.items():
        locs = qty.get(no) or {}
        cost = _f(c.get("Unit_Cost"))
        total = sum(locs.values())
        items.append([no, (c.get("Description") or "").strip(), c.get("Base_Unit_of_Measure") or "",
                      (c.get("Item_Category_Group_3") or "").strip(), (c.get("Item_Category_Group_4") or "").strip(),
                      (c.get("Item_Category_Code") or "").strip(), bool(c.get("Blocked")), round(cost, 4),
                      round(total, 4), round(total * cost, 2), locs])
        for loc, q in locs.items():
            x = loc_sum.setdefault(loc, {"items": 0, "value": 0.0, "negative": 0})
            x["items"] += 1
            x["value"] += q * cost
            if q < 0:
                x["negative"] += 1
    for no in set(qty) - set(cards):                                   # stock on an item the master does not hold
        locs = qty[no]
        items.append([no, "", "", "", "", "", False, 0.0, round(sum(locs.values()), 4), 0.0, locs])
    loc_list = sorted(({"code": loc, "name": names.get(loc.upper(), ""), "items": v["items"],
                        "value": round(v["value"], 2), "negative": v["negative"]} for loc, v in loc_sum.items()),
                      key=lambda x: (x["code"] == NO_LOC, -x["value"]))
    for loc in locations:
        if loc not in loc_sum:
            loc_list.append({"code": loc, "name": names.get(loc.upper(), ""), "items": 0, "value": 0.0, "negative": 0})
    return {"available": True, "company": company, "as_of": as_of.isoformat(),
            "read_at": datetime.now().isoformat(timespec="seconds"), "cards_read_at": master.get("read_at"),
            "columns": ["no", "desc", "unit", "group3", "group4", "category", "blocked", "unit_cost", "qty", "value",
                        "locations"],
            "items": items, "locations": loc_list, "failed": failed,
            "notes": {"qty": "Quantity on hand on the date, per location, calculated by Business Central "
                             "(the item card's Inventory by date, filtered by location).",
                      "value": "Value = quantity × the item card's unit cost: an estimate; the posted inventory "
                               "value is reconciled in Inventory → Overview.",
                      "no_location": "“(no location)” is stock posted without a location code: the company total "
                                     "minus the sum of the locations.",
                      "access": "Every warehouse is shown to everyone who opens the screen, so that stock can be "
                                "asked for from the warehouse that holds it."}}


def request(client, company, as_of, refresh=False):
    return _held.request(f"{company}|{as_of.isoformat()}", lambda p: build(client, company, as_of, p), refresh,
                         note="Reading the item cards and the stock of every location from Business Central…")


def render_xlsx(r, f=None):
    """Every item with stock (or every card): one column per location, quantity; value and unit cost."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    f = f or {}
    wb = Workbook()
    ws = wb.active
    ws.title = "Stock by location"
    locs = [l["code"] for l in r["locations"] if l["items"]]
    loc = f.get("location") or ""
    if loc:
        locs = [x for x in locs if x == loc]
    ws.append([f"Item cards · stock by location · {r['company']} · on {r['as_of']} · read {r['read_at']}"])
    ws["A1"].font = Font(bold=True, size=13)
    ws.append([])
    head = ["Item No.", "Description", "Base unit", "Category group 3", "Category group 4", "Blocked", "Unit cost",
            "Total quantity", "Value (estimated)"] + locs
    ws.append(head)
    fill = PatternFill("solid", fgColor="EFEDE6")
    for c in ws[3]:
        c.font, c.fill = Font(bold=True), fill
    show = f.get("show") or "stock"
    for it in r["items"]:
        no, desc, unit, g3, g4, _cat, blocked, cost, qty, value, q = it
        if loc and not q.get(loc):
            continue
        if show == "stock" and not q:
            continue
        if show == "negative" and not any(v < 0 for v in q.values()):
            continue
        ws.append([no, desc, unit, g3, g4, "yes" if blocked else "", cost, qty, value] + [q.get(x) for x in locs])
    ws.freeze_panes = "C4"
    ws.auto_filter.ref = f"A3:{ws.cell(3, len(head)).column_letter}{ws.max_row}"
    for i, w in enumerate([12, 36, 8, 16, 18, 8, 11, 13, 15] + [11] * len(locs), 1):
        ws.column_dimensions[ws.cell(3, i).column_letter].width = w
    for row in ws.iter_rows(min_row=4, min_col=7):
        for c in row:
            c.number_format = "#,##0.00"
    ws2 = wb.create_sheet("Locations")
    ws2.append(["Location", "Name", "Items with stock", "Items below zero", "Value (estimated)"])
    for c in ws2[1]:
        c.font, c.fill = Font(bold=True), fill
    for l in r["locations"]:
        ws2.append([l["code"], l["name"], l["items"], l["negative"], l["value"]])
    ws3 = wb.create_sheet("Read me")
    for k, v in r["notes"].items():
        ws3.append([v])
    ws3.column_dimensions["A"].width = 120
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
