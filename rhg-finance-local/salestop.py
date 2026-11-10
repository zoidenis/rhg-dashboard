"""Sales → Blocked by stock: every item whose quantity stops a sale from being posted, now and later today.

Due diligence (10 Nov 2026, BC SALT): the Simphony sales are imported into Business Central as Assembly Orders, one
per menu item, location and day; an order is posted when its components can be taken out of the location's stock.
A component BC flags with "Avail. Warning" (not enough stock at the location) keeps its order open: the sale is not
posted (3,186 open orders of 7–9 Nov, 1,471 of them with a component short). Decision of Geri the same day: the
orders blocked in BC, and what today's Simphony sales will block before they are imported; screen only.

  * Blocked now (BC, read-only): by component and location, need = the remaining quantity of every open Assembly
    Line (page 901) of that component at the location (in the base unit); on hand = the item card's stock at the
    location now (Inventory filtered by location); short = need − on hand above zero blocks the orders that use
    it. BC's own Avail. Warning is set when a line is checked, so it can stay on after the stock came (bamboo
    leaves 200226 at SALT: 71 lines flagged, 1,114.5 needed, 1,114.5 on hand): such a component is listed as
    covered now, its orders can be posted. The orders and menu items, the oldest day, the sales they carry
    (menu item price in Simphony).
  * At risk today (Simphony): the menu items sold today (getMenuItemDailyTotals of the current business day) not
    yet imported, exploded through their BC recipe (Assembly BOM, first level, units converted): a component whose
    on hand, less what the open orders already need, does not cover today's need will block today's sales.
The Simphony menu item number is the BC item number (the POS import posts it as ItemNo); the restaurant's Simphony
location and BC location come from the location registry (Finance → Settings → Location intelligence).
"""
import io
from datetime import date, datetime

import heldjob
import locintel
import recipes
import simphony

KEEP = 300                                   # seconds: the screen asks again every few minutes
_held = heldjob.Held("salestop", KEEP)
TOL = 1e-6


def _f(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _uom(m):
    u = {}
    for it, code, per in m.get("uom") or []:
        u[(it, code)] = per
    return u


def _per_base(items, uom, item, unit):
    base = str((items.get(item) or {}).get("Base_Unit_of_Measure") or "").upper()
    unit = (unit or "").upper()
    if not unit or unit == base:
        return 1.0
    return uom.get((item, unit))


def _price_map(ref):
    """{menu item: price} of a Simphony location: the active price of the lowest price level."""
    try:
        rows = simphony.menu_item_prices(ref)
    except Exception:                                               # noqa: BLE001
        return {}
    out = {}
    for r in sorted(rows, key=lambda r: (r.get("level") or 99)):
        if r.get("price") is not None:
            out.setdefault(str(r["num"]), _f(r["price"]))
    return out


def _today_sold(ref):
    """(business date, {menu item: (count, sales VAT included)}) of the current business day of a location."""
    day = simphony.latest_business_date(ref) or date.today().isoformat()
    j = simphony.call("getMenuItemDailyTotals", {"locRef": ref, "busDt": day}) or {}
    out = {}
    for rvc in j.get("revenueCenters") or []:
        for m in rvc.get("menuItems") or []:
            if m.get("miNum") is None:
                continue
            x = out.setdefault(str(int(m["miNum"])), [0.0, 0.0])
            x[0] += _f(m.get("slsCnt")) - _f(m.get("rtnCnt"))
            x[1] += _f(m.get("slsTtl"))
    return str(day)[:10], out


def build(client, company, progress=lambda s: None, today=None):
    today = today or date.today()
    progress("reading the open assembly orders")
    heads = client._get_all(client._url(company, "AssemblyOrders"), {
        "$select": "No,Item_No,Description,Due_Date,Location_Code,Quantity,Remaining_Quantity"})
    progress(f"reading the lines of {len(heads):,} open assembly orders")
    lines = client._get_all(client._url(company, "AssemblyLinesOpen"), page_size=5000, params={
        "$select": "Document_No,Avail_Warning,Type,No,Description,Location_Code,Unit_of_Measure_Code,"
                   "Qty_per_Unit_of_Measure,Remaining_Quantity,Cost_Amount,Due_Date"})
    progress("reading the item cards and recipes")
    m = recipes.master(client, company)
    items, uom = m["items"], _uom(m)
    head = {str(h.get("No")): h for h in heads}
    reg = [r for r in locintel.registry() if (r.get("company") or "").upper() == company.upper() and r.get("bc")]
    loc_ref = {r["bc"].upper(): r for r in reg}

    # every open line: the need per component and location (base unit), the orders and menu items using it, and the
    # lines BC flags with Avail. Warning (a flag set when the line was last checked: the stock may have come since)
    need, use = {}, {}
    for ln in lines:
        if (ln.get("Type") or "Item") != "Item" or not ln.get("No"):
            continue
        h = head.get(str(ln.get("Document_No"))) or {}
        loc = str(ln.get("Location_Code") or h.get("Location_Code") or "").strip()
        comp = str(ln["No"])
        q = _f(ln.get("Remaining_Quantity")) * (_f(ln.get("Qty_per_Unit_of_Measure")) or 1.0)
        need[(comp, loc)] = need.get((comp, loc), 0.0) + q
        w = use.setdefault((comp, loc), {"desc": ln.get("Description") or "", "lines": 0, "flagged": 0, "cost": 0.0,
                                         "orders": set(), "menu": {}, "oldest": None})
        w["lines"] += 1
        w["cost"] += _f(ln.get("Cost_Amount"))
        if ln.get("Avail_Warning") in (True, "true", "True", 1):
            w["flagged"] += 1
        doc = str(ln.get("Document_No"))
        w["orders"].add(doc)
        mi = str(h.get("Item_No") or "")
        if mi:
            x = w["menu"].setdefault(mi, [h.get("Description") or "", 0.0])
            x[1] += _f(h.get("Remaining_Quantity") or h.get("Quantity"))
        d = str(h.get("Due_Date") or ln.get("Due_Date") or "")[:10]
        if d and (w["oldest"] is None or d < w["oldest"]):
            w["oldest"] = d
    # on hand now at each location that matters
    locs = sorted({loc for _, loc in need if loc} | set(loc_ref))
    progress(f"reading the stock of {len(locs)} locations")
    stock = {}
    for loc in locs:
        try:
            stock[loc] = client.stock_by_location(company, loc, today.isoformat(), 300)
        except Exception as exc:                                    # noqa: BLE001
            stock[loc] = {}
            progress(f"{loc}: {str(exc)[:80]}")
    prices = {}

    def price(loc, mi):
        r = loc_ref.get(loc.upper())
        if not r:
            return None
        if r["ref"] not in prices:
            prices[r["ref"]] = _price_map(r["ref"])
        return prices[r["ref"]].get(mi)

    def card(no):
        c = items.get(no) or {}
        return (c.get("Description") or "").strip(), str(c.get("Base_Unit_of_Measure") or "").upper()

    # blocking now: the open orders need more of a component than the location holds; flagged by BC but covered
    # now: the stock has come since BC set the flag, the orders can be posted
    now_rows = []
    blocked_orders = {}
    for (comp, loc), w in use.items():
        on = _f(stock.get(loc, {}).get(comp))
        tot = need.get((comp, loc), 0.0)
        short = tot - on
        if short <= TOL and not w["flagged"]:
            continue
        desc, unit = card(comp)
        status = "short" if short > TOL else "covered"
        sales = sum((price(loc, mi) or 0) * q for mi, (_d, q) in w["menu"].items())
        if status == "short":
            for doc in w["orders"]:
                blocked_orders.setdefault((loc, doc), set()).add(comp)
        now_rows.append({"item": comp, "desc": desc or w["desc"], "unit": unit, "location": loc, "status": status,
                         "need": round(tot, 4), "on_hand": round(on, 4), "short": round(max(0.0, short), 4),
                         "orders": len(w["orders"]), "lines": w["lines"], "flagged": w["flagged"], "oldest": w["oldest"],
                         "cost": round(w["cost"], 2), "sales": round(sales, 2),
                         "menu": sorted(([mi, d, round(q, 2)] for mi, (d, q) in w["menu"].items()),
                                        key=lambda x: -x[2])[:12]})
    now_rows.sort(key=lambda r: (r["status"] != "short", -r["sales"], -r["orders"]))
    # orders blocked and the sales they carry, by location
    by_loc = {}
    for (loc, doc), comps in blocked_orders.items():
        h = head.get(doc) or {}
        mi = str(h.get("Item_No") or "")
        q = _f(h.get("Remaining_Quantity") or h.get("Quantity"))
        x = by_loc.setdefault(loc, {"location": loc, "orders": 0, "portions": 0.0, "sales": 0.0, "components": set(),
                                    "risk": 0, "days": set()})
        x["orders"] += 1
        x["portions"] += q
        x["sales"] += (price(loc, mi) or 0) * q
        x["components"] |= comps
        x["days"].add(str(h.get("Due_Date") or "")[:10])

    # at risk today: what Simphony sold today and is not imported yet, through the recipes
    progress("reading today's sales in Simphony")
    bom = {}
    for parent, _ln, typ, no, _desc, _nested, qty, unit in m.get("bom") or []:
        if typ == "Item" and no:
            bom.setdefault(parent, []).append((no, qty, unit))
    imported = {}                                   # (location, day, menu item) → portions already imported
    for h in heads:
        k = (str(h.get("Location_Code") or ""), str(h.get("Due_Date") or "")[:10], str(h.get("Item_No") or ""))
        imported[k] = imported.get(k, 0.0) + _f(h.get("Quantity"))
    risk_rows, sim_days, sim_errors = [], {}, []
    sim_on = bool(simphony.info().get("configured"))
    if not sim_on:
        sim_errors.append("The Simphony connection is not set: only the orders blocked in Business Central are shown.")
    for r in reg if sim_on else []:
        loc = r["bc"]
        try:
            day, sold = _today_sold(r["ref"])
            names = simphony.menu_items(r["ref"])
        except Exception as exc:                                    # noqa: BLE001
            sim_errors.append(f"{r['name']}: {str(exc)[:120]}")
            continue
        sim_days[loc] = day
        today_need, drivers, nobom = {}, {}, 0
        for mi, (cnt, sls) in sold.items():
            cnt -= imported.get((loc, day, mi), 0.0)
            if cnt <= TOL:
                continue
            comps = bom.get(mi)
            if not comps:
                nobom += 1
                continue
            for comp, qty, unit in comps:
                per = _per_base(items, uom, comp, unit)
                if per is None:
                    continue
                q = cnt * qty * per
                today_need[comp] = today_need.get(comp, 0.0) + q
                d = drivers.setdefault(comp, {})
                d[mi] = d.get(mi, 0.0) + cnt
        for comp, q in today_need.items():
            on = _f(stock.get(loc, {}).get(comp))
            open_need = need.get((comp, loc), 0.0)
            after = on - open_need - q
            if after >= -TOL:
                continue
            desc, unit = card(comp)
            dr = sorted(drivers[comp].items(), key=lambda kv: -kv[1])[:8]
            risk_rows.append({"item": comp, "desc": desc, "unit": unit, "location": loc, "day": day,
                              "on_hand": round(on, 4), "open_need": round(open_need, 4), "today_need": round(q, 4),
                              "after": round(after, 4),
                              "blocked_now": need.get((comp, loc), 0.0) - on > TOL,
                              "menu": [[mi, ((names.get(int(mi)) or {}).get("name") if mi.isdigit() else "")
                                        or card(mi)[0], round(c, 2)] for mi, c in dr]})
            by_loc.setdefault(loc, {"location": loc, "orders": 0, "portions": 0.0, "sales": 0.0,
                                    "components": set(), "risk": 0, "days": set()})["risk"] += 1
        if nobom:
            by_loc.setdefault(loc, {"location": loc, "orders": 0, "portions": 0.0, "sales": 0.0,
                                    "components": set(), "risk": 0, "days": set()})["nobom"] = nobom
    risk_rows.sort(key=lambda r: r["after"])
    names = {r["bc"].upper(): r["name"] for r in reg}
    loc_rows = []
    for loc, x in by_loc.items():
        loc_rows.append({"location": loc, "name": names.get(loc.upper(), ""), "orders": x["orders"],
                         "portions": round(x["portions"], 2), "sales": round(x["sales"], 2),
                         "components": len(x["components"]), "risk": x["risk"], "days": sorted(d for d in x["days"] if d),
                         "today": sim_days.get(loc), "sold_no_recipe": x.get("nobom", 0)})
    loc_rows.sort(key=lambda x: (-x["sales"], -x["orders"], -x["risk"]))
    return {"available": True, "company": company, "read_at": datetime.now().isoformat(timespec="seconds"),
            "open_orders": len(heads), "open_lines": len(lines), "now": now_rows, "risk": risk_rows,
            "locations": loc_rows, "simphony_errors": sim_errors,
            "totals": {"components": sum(1 for x in now_rows if x["status"] == "short"),
                       "covered": sum(1 for x in now_rows if x["status"] == "covered"), "orders": len(blocked_orders),
                       "portions": round(sum(x["portions"] for x in loc_rows), 2),
                       "sales": round(sum(x["sales"] for x in loc_rows), 2), "risk": len(risk_rows),
                       "days": sorted({d for x in loc_rows for d in x["days"]})}}


def request(client, company, refresh=False):
    return _held.request(company, lambda p: build(client, company, p), refresh,
                         note="Reading the open assembly orders in Business Central and today's sales in Simphony…")


def cut(r, allowed):
    """A person limited to some locations: their locations only (BC code, department or Simphony reference)."""
    if allowed is None or not isinstance(r, dict) or not r.get("available"):
        return r
    keep = lambda loc: (loc or "").upper() in allowed                 # noqa: E731
    out = dict(r, now=[x for x in r["now"] if keep(x["location"])], risk=[x for x in r["risk"] if keep(x["location"])],
               locations=[x for x in r["locations"] if keep(x["location"])])
    L = out["locations"]
    out["totals"] = {"components": sum(1 for x in out["now"] if x["status"] == "short"),
                     "covered": sum(1 for x in out["now"] if x["status"] == "covered"), "orders": sum(x["orders"] for x in L),
                     "portions": round(sum(x["portions"] for x in L), 2), "sales": round(sum(x["sales"] for x in L), 2),
                     "risk": len(out["risk"]), "days": sorted({d for x in L for d in x["days"]})}
    return out


def render_xlsx(r):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    fill, bold = PatternFill("solid", fgColor="E4F3F9"), Font(bold=True)

    def sheet(ws, header, rows, widths):
        ws.append(header)
        for c in ws[1]:
            c.fill, c.font = fill, bold
        for row in rows:
            ws.append(row)
        for i, w in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.freeze_panes = "A2"
        if rows:
            ws.auto_filter.ref = ws.dimensions
    ws = wb.active
    ws.title = "Blocked now"
    sheet(ws, ["Location", "Item", "Description", "Unit", "Status", "Open orders need", "On hand", "Short",
               "Orders using it", "Lines flagged by BC", "Oldest day", "Sales held", "Cost", "Menu items held"],
          [[x["location"], x["item"], x["desc"], x["unit"],
            "short" if x["status"] == "short" else "flagged by BC, stock covers it now", x["need"], x["on_hand"],
            x["short"], x["orders"], x["flagged"], x["oldest"], x["sales"], x["cost"],
            "; ".join(f"{m[0]} {m[1]} ×{m[2]:g}" for m in x["menu"])]
           for x in r["now"]], [12, 12, 34, 8, 22, 14, 11, 11, 13, 13, 11, 13, 12, 70])
    w2 = wb.create_sheet("At risk today")
    sheet(w2, ["Location", "Business day", "Item", "Description", "Unit", "On hand", "Open orders need", "Today's need",
               "After today", "Blocked now too", "Menu items sold today"],
          [[x["location"], x["day"], x["item"], x["desc"], x["unit"], x["on_hand"], x["open_need"], x["today_need"],
            x["after"], "yes" if x["blocked_now"] else "", "; ".join(f"{m[0]} {m[1]} ×{m[2]:g}" for m in x["menu"])]
           for x in r["risk"]], [12, 12, 12, 34, 8, 11, 14, 13, 12, 12, 70])
    w3 = wb.create_sheet("By location")
    sheet(w3, ["Location", "Name", "Orders blocked", "Portions", "Sales held", "Components short", "At risk today",
               "Days", "Simphony business day"],
          [[x["location"], x["name"], x["orders"], x["portions"], x["sales"], x["components"], x["risk"],
            ", ".join(x["days"]), x["today"] or ""] for x in r["locations"]], [12, 22, 13, 11, 13, 15, 13, 26, 18])
    rd = wb.create_sheet("Read me")
    for t in (f"Sales blocked by stock · {r['company']} · read {r['read_at']} (Business Central and Simphony, read only).",
              "Blocked now: the open Assembly Orders (the Simphony sales imported into BC) need more of a component "
              "than the location holds now, so they, and their sales, cannot be posted. A component BC flags with "
              "Avail. Warning whose stock now covers the need is listed as covered: its orders can be posted.",
              "Open orders need = the remaining quantity of every open order of the component at the location (base "
              "unit); short = need − on hand now. Sales held = portions of the blocked orders × the menu item's price.",
              "At risk today: the menu items sold today in Simphony and not imported yet, through their BC recipe: the "
              "components whose stock, less what the open orders need, does not cover today's need."):
        rd.append([t])
    rd.column_dimensions["A"].width = 130
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
