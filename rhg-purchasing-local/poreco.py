"""Live operations → Orders vs recommendation: what was bought against what the planning parameters called for.

For every purchase received in the period (Item Ledger Entries, Entry Type Purchase) and every purchase order line
still to be received (page 50), at its location:
  * the stock on hand just before it: the item's stock at the location the day before the period (item card,
    InventorybyDate) plus every movement of the item there in the period up to it (Item Ledger Entries);
  * the quantity the system recommended: up to the Maximum Inventory of the item's SKU card in Business Central
    (Reordering Policy Maximum Qty.), recommended = maximum − stock on hand (− what is already on order, for an open
    order); an item without a maximum takes Pulse's estimate from the location's delivery rhythm (Settings → stock
    cadence: use per day × (lead time + cycle + safety days)), marked as such;
  * ordered over the recommendation: the quantity above it, beyond the tolerance (Settings, 10%), at the line's
    unit cost: the money tied up in stock nobody asked for; what that stock costs to carry (the yearly carrying
    rate, Settings, 25%, for the days of use it covers) and, for fresh families, the value beyond the fresh limit
    of days of use, at risk of waste;
  * ordered early: the stock before it was still above the reorder point of the SKU card.
Who: the purchaser or assigned user of an open order; for a received purchase the location (its stock manager) and
the vendor (Source No.). Business Central is only read.
"""
import threading
import time
import traceback
from collections import defaultdict
from datetime import date, timedelta

import config
import stock

DEFAULTS = {"tolerance_pct": 10.0, "carry_pct": 25.0, "fresh_days": 4.0}
FRESH = stock.FRESH_HINTS
_jobs, _results, _lock = {}, {}, threading.Lock()
KEEP = 3600


def params(settings):
    s = (settings or {}).get("poreco") or {}

    def num(k):
        try:
            return float(s.get(k, DEFAULTS[k]))
        except (TypeError, ValueError):
            return DEFAULTS[k]
    return {k: num(k) for k in DEFAULTS} | {"cadence": stock.parse_cadence(((settings or {}).get("stock") or {}).get("cadence"))}


def _f(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _fresh(card):
    text = " ".join(str(card.get(k) or "") for k in ("code", "g1", "g2", "g3", "g4")).upper()
    return any(h in text for h in FRESH)


def purchases(client, company, a, b):
    """The purchase receipts of the period: Entry Type Purchase with a positive quantity."""
    rows = client._get_all(client._url(company, config.BC_ILE_ENTITY), {
        "$filter": f"Entry_Type eq 'Purchase' and Posting_Date ge {a} and Posting_Date le {b}",
        "$select": "Entry_No,Item_No,Item_Description,Posting_Date,Document_No,Location_Code,Quantity,Source_No,"
                   "Cost_Amount_Actual,Cost_Amount_Expected,Unit_of_Measure_Code"})
    return [r for r in rows if _f(r.get("Quantity")) > 0 and r.get("Item_No")]


def movements(client, company, location, items, a, b):
    """Every posted movement of these items at the location in the period, oldest first."""
    out, items = [], sorted(items)
    for i in range(0, len(items), 40):
        part = " or ".join(f"Item_No eq '{x}'" for x in items[i:i + 40])
        out += client._get_all(client._url(company, config.BC_ILE_ENTITY), {
            "$filter": f"Location_Code eq '{location}' and Posting_Date ge {a} and Posting_Date le {b} and ({part})",
            "$select": "Entry_No,Entry_Type,Item_No,Posting_Date,Quantity"})
    out.sort(key=lambda r: (str(r.get("Posting_Date") or "")[:10], int(r.get("Entry_No") or 0)))
    return out


def analyse(receipts, open_lines, opening, moves, skus, catalog, P, a, b, today=None):
    """The lines with their recommendation (pure: the tests feed it). receipts: ILE purchase rows; open_lines:
    [{order, line, item, location, qty_left, unit_cost, order_date, purchaser, vendor}]; opening: {location: {item:
    qty}}; moves: {location: [ILE rows]}; skus: {(item, location): {reorder_point, maximum, inventory}}."""
    today = today or date.today()
    days = max(1, (date.fromisoformat(b) - date.fromisoformat(a)).days + 1)
    # use per day of each item at each location: what left it in the period (sales, consumption, transfers out)
    use = defaultdict(float)
    for loc, rows in moves.items():
        for r in rows:
            q = _f(r.get("Quantity"))
            if q < 0 and r.get("Entry_Type") != "Purchase":
                use[(str(r.get("Item_No")), loc)] += -q
    daily = {k: v / days for k, v in use.items()}
    # the stock just before each entry, walking the movements in order
    before = {}
    for loc, rows in moves.items():
        bal = defaultdict(float, {k: v for k, v in (opening.get(loc) or {}).items()})
        for r in rows:
            it = str(r.get("Item_No"))
            before[int(r.get("Entry_No") or 0)] = bal[it]
            bal[it] += _f(r.get("Quantity"))
        for it, q in bal.items():
            before[("now", loc, it)] = q
    out = []

    def rec_of(it, loc, on_hand, on_order=0.0):
        sku = skus.get((it, loc)) or {}
        mx, rop = _f(sku.get("maximum")), _f(sku.get("reorder_point"))
        d = daily.get((it, loc), 0.0)
        if mx > 0:
            basis = "SKU card in BC"
        else:
            pol = stock.policy_for(loc, P["cadence"])
            mx, rop = d * pol["max_days"], d * pol["reorder_days"]
            basis = "Pulse estimate" if d > 0 else "no basis"
        return max(0.0, mx - on_hand - on_order), mx, rop, basis, d

    def line(kind, it, loc, qty, cost, when, who, vendor, doc, on_hand, on_order=0.0):
        card = catalog.get(it) or {}
        rec, mx, rop, basis, d = rec_of(it, loc, on_hand, on_order)
        unit = cost / qty if qty else 0.0
        over = qty - rec if basis != "no basis" else 0.0
        tol = rec * P["tolerance_pct"] / 100
        over = over if over > max(tol, 1e-9) else 0.0
        excess_value = over * unit
        cover = over / d if d > 0 else None
        carry = excess_value * P["carry_pct"] / 100 * min(cover if cover is not None else 365, 365) / 365
        fresh = _fresh(card)
        waste = max(0.0, over - d * P["fresh_days"]) * unit if fresh and d > 0 else (excess_value if fresh and over else 0.0)
        early = rop > 0 and on_hand > rop
        out.append({"kind": kind, "item": it, "desc": card.get("desc") or "", "uom": card.get("uom") or "",
                    "category": card.get("code") or "", "fresh": fresh, "location": loc, "date": when, "doc": doc,
                    "who": who, "vendor": vendor, "qty": round(qty, 3), "rec": round(rec, 3), "over": round(over, 3),
                    "over_pct": round(over / rec * 100, 1) if rec else None, "unit_cost": round(unit, 4),
                    "value": round(cost, 2), "excess_value": round(excess_value, 2), "carry": round(carry, 2),
                    "waste_risk": round(waste, 2), "cover_days": round(cover, 1) if cover is not None else None,
                    "on_hand": round(on_hand, 3), "on_order": round(on_order, 3), "max": round(mx, 3),
                    "rop": round(rop, 3), "daily": round(d, 3), "basis": basis, "early": early})
    for r in receipts:
        it, loc = str(r.get("Item_No")), str(r.get("Location_Code") or "")
        qty = _f(r.get("Quantity"))
        cost = _f(r.get("Cost_Amount_Actual")) or _f(r.get("Cost_Amount_Expected"))
        oh = before.get(int(r.get("Entry_No") or 0))
        if oh is None:
            continue
        line("received", it, loc, qty, cost, str(r.get("Posting_Date") or "")[:10], loc, str(r.get("Source_No") or ""),
             str(r.get("Document_No") or ""), oh)
    # open orders: against today's stock and the orders placed before them
    seen = defaultdict(float)
    for o in sorted(open_lines, key=lambda x: (x["order_date"] or "", x["order"], x.get("line", 0))):
        it, loc = o["item"], o["location"]
        sku = skus.get((it, loc)) or {}
        oh = before.get(("now", loc, it), _f(sku.get("inventory")))
        line("open", it, loc, o["qty_left"], o["qty_left"] * o["unit_cost"], o["order_date"] or "", o["purchaser"] or loc,
             o["vendor"], o["order"], oh, seen[(it, loc)])
        seen[(it, loc)] += o["qty_left"]
    return out


def summarise(lines, P):
    """KPIs, by location, by who and by vendor, the decisions."""
    def agg(keyf):
        g = {}
        for x in lines:
            k = keyf(x)
            s = g.setdefault(k, {"key": k, "lines": 0, "value": 0.0, "over_lines": 0, "excess_value": 0.0, "carry": 0.0,
                                 "waste_risk": 0.0, "early": 0, "items": defaultdict(float)})
            s["lines"] += 1
            s["value"] += x["value"]
            s["early"] += x["early"]
            if x["over"] > 0:
                s["over_lines"] += 1
                s["excess_value"] += x["excess_value"]
                s["carry"] += x["carry"]
                s["waste_risk"] += x["waste_risk"]
                s["items"][f'{x["item"]} {x["desc"]}'.strip()] += x["excess_value"]
        rows = []
        for s in g.values():
            top = sorted(s.pop("items").items(), key=lambda kv: -kv[1])[:3]
            s.update(top=[k for k, _v in top], over_share=round(s["excess_value"] / s["value"] * 100, 1) if s["value"] else None,
                     value=round(s["value"], 2), excess_value=round(s["excess_value"], 2), carry=round(s["carry"], 2),
                     waste_risk=round(s["waste_risk"], 2))
            rows.append(s)
        return sorted(rows, key=lambda s: -s["excess_value"])
    over = [x for x in lines if x["over"] > 0]
    value = sum(x["value"] for x in lines)
    ex = sum(x["excess_value"] for x in over)
    by_basis = defaultdict(int)
    for x in lines:
        by_basis[x["basis"]] += 1
    kpis = {"lines": len(lines), "value": round(value, 2), "over_lines": len(over), "excess_value": round(ex, 2),
            "excess_share": round(ex / value * 100, 1) if value else None,
            "carry": round(sum(x["carry"] for x in over), 2), "waste_risk": round(sum(x["waste_risk"] for x in over), 2),
            "early": sum(1 for x in lines if x["early"]), "with_card": by_basis.get("SKU card in BC", 0),
            "estimated": by_basis.get("Pulse estimate", 0), "no_basis": by_basis.get("no basis", 0),
            "open_excess": round(sum(x["excess_value"] for x in over if x["kind"] == "open"), 2)}
    locs, who, vendors = agg(lambda x: x["location"]), agg(lambda x: x["who"]), agg(lambda x: x["vendor"] or "–")
    acts = []
    for L in locs[:6]:
        if L["excess_value"] > 0:
            acts.append({"owner": "COO", "value": L["excess_value"],
                         "text": f"{L['key']}: {L['over_lines']} order line(s) over the recommendation, "
                                 f"{L['excess_value']:,.0f} tied up in extra stock (most: {', '.join(L['top'][:2])}). "
                                 f"Review the orders with the stock manager; order up to the maximum, not by habit."})
    if kpis["open_excess"] > 0:
        acts.append({"owner": "CFO", "value": kpis["open_excess"],
                     "text": f"Open orders still to receive carry {kpis['open_excess']:,.0f} above the recommendation: "
                             f"reduce them with the vendors before they are delivered."})
    if kpis["estimated"] + kpis["no_basis"] > max(5, 0.2 * max(1, kpis["lines"])):
        acts.append({"owner": "COO", "value": 0.0,
                     "text": f"{kpis['estimated'] + kpis['no_basis']} line(s) on items without a maximum on their SKU "
                             f"card: set the cards (Finance → Inventory → SKU planning gives the parameters), so the "
                             f"system's recommendation is BC's and the requisition worksheet can propose the orders."})
    if kpis["early"] > max(5, 0.2 * max(1, kpis["lines"])):
        acts.append({"owner": "COO", "value": 0.0,
                     "text": f"{kpis['early']} line(s) ordered while the stock was still above the reorder point: "
                             f"order from the requisition worksheet (Calculate Plan → Carry Out Action Messages) "
                             f"instead of by hand."})
    if kpis["waste_risk"] > 0:
        acts.append({"owner": "COO", "value": kpis["waste_risk"],
                     "text": f"Fresh goods bought beyond {P['fresh_days']:g} days of use: {kpis['waste_risk']:,.0f} "
                             f"at risk of waste; buy fresh families in smaller, more frequent deliveries."})
    acts.append({"owner": "CFO", "value": 0.0,
                 "text": f"Set an approval for purchase orders more than {P['tolerance_pct']:g}% over the recommended "
                         f"quantity (BC purchase approval workflow), and review this report weekly with each stock manager."})
    return {"kpis": kpis, "locations": locs, "who": who[:40], "vendors": vendors[:30],
            "lines": sorted(over, key=lambda x: -x["excess_value"])[:400], "actions": acts}


def build(client, company, a, b, settings, today=None):
    P = params(settings)
    receipts = purchases(client, company, a, b)
    orders = client.purchase_order_cards(company)
    open_lines = []
    for o in orders:
        for i, l in enumerate(o.get("lines") or []):
            left = _f(l.get("qty")) - _f(l.get("received"))
            if str(l.get("type") or "").lower() != "item" or left <= 0 or not l.get("no"):
                continue
            per = _f(l.get("qty_per_uom")) or 1.0             # the order's unit of measure → the item's base unit
            open_lines.append({"order": o.get("no") or "", "line": i, "item": str(l["no"]),
                               "location": l.get("location") or o.get("location") or "", "qty_left": left * per,
                               "unit_cost": _f(l.get("amount")) / (_f(l.get("qty")) * per) if _f(l.get("qty")) else 0.0,
                               "order_date": o.get("order_date") or "", "purchaser": o.get("purchaser") or o.get("assigned_user") or "",
                               "vendor": o.get("vendor_no") or o.get("vendor") or ""})
    items_by_loc = defaultdict(set)
    for r in receipts:
        items_by_loc[str(r.get("Location_Code") or "")].add(str(r.get("Item_No")))
    for l in open_lines:
        items_by_loc[l["location"]].add(l["item"])
    day_before = (date.fromisoformat(a) - timedelta(days=1)).isoformat()
    to = (today or date.today()).isoformat()
    opening, moves = {}, {}
    for loc, items in items_by_loc.items():
        if not loc:
            continue
        bal, _cards = client.inventory_at(company, loc, day_before)
        opening[loc] = {k: v for k, v in bal.items() if k in items}
        moves[loc] = movements(client, company, loc, items, a, max(b, to))
    skus, _st = client.stockkeeping_units(company)
    catalog = client.item_catalog(company)
    lines = analyse(receipts, open_lines, opening, moves, skus, catalog, P, a, b, today)
    lines = [x for x in lines if x["kind"] == "open" or a <= x["date"] <= b]
    out = summarise(lines, P)
    out.update(period={"from": a, "to": b}, params={k: P[k] for k in DEFAULTS}, read_at=time.strftime("%Y-%m-%d %H:%M"))
    return out


def request(client, company, a, b, settings, refresh=False):
    """The report of the period, read in the background: {"pending": true} until it is ready."""
    key = (company, a, b)
    with _lock:
        hit = _results.get(key)
        if hit and not refresh and time.time() - hit[0] < KEEP:
            return hit[1]
        job = _jobs.get(key)
        if job and job.is_alive():
            return {"pending": True, "stage": "Reading the purchases, the stock and the SKU cards from Business Central…"}

        def run():
            try:
                r = build(client, company, a, b, settings)
            except Exception as exc:                                   # noqa: BLE001
                traceback.print_exc()
                r = {"error": f"Could not build the report: {str(exc)[:300]}"}
            with _lock:
                _results[key] = (time.time(), r)
        t = threading.Thread(target=run, daemon=True)
        _jobs[key] = t
        t.start()
    return hit[1] if hit else {"pending": True, "stage": "Reading the purchases, the stock and the SKU cards from Business Central…"}
