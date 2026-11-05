"""Received vs discharged: which item codes come into a location and which codes the recipes and the sales take
out of it, and where the two do not meet. A cost controller's list of recipe interventions.

For every location and item, over the period (30, 60 or 90 days to the stock date of Movement & dead stock):
  in       = purchase receipts (net of returns to the vendor) + transfers in + assembly output (made here)
  out      = assembly consumption (what the recipes discharge) + sales as it is (net of returns)
  shipped  = transfers out (at the distribution locations, passing goods on is their job, not a gap)
  opening  = closing − every movement of the period; closing = quantity on hand at the stock date
Positive adjustments left out of the negative stock email (Settings, SHITJE in October 2026 by default) are left
out here too, from the closing stock and the movements.

Signals, each with what to do in the recipes:
  * Received, not discharged: it comes in and nothing takes it out. In no recipe → add it to the recipes that
    use it, or the recipes use another code for it; in recipes not sold at the location → check the menu and
    the mapping. Value = what was received.
  * Discharged, not received: the recipes take it out and it does not come in, and the stock at the start of
    the period did not cover it. The recipe uses a code the location does not receive. Value = what was
    discharged.
  * Discharged far less than received (below the low ratio, with more cover left than the cover limit): the
    recipe quantity is too low, the item is missing from some recipes, or waste is not posted.
  * Discharged far more than received (above the high ratio, the stock run down to a few days): the recipe
    quantity is too high, or the unit is wrong.
  * Discharged from stock: discharged, not received in the period, the opening stock covered it. Not a gap.
A code discharged and not received is paired with a code received and not (or too little) discharged at the
same location when they look like the same product (description words, family, unit): "replace B with A in
these recipes", or receive B. A recipe (Assembly BOM) is one per item for every location, so the swap is
suggested only when no other location receives B.

The company view sums every location (transfers between them cancel out): purchases and output against what the
recipes and the sales discharge, which is the cleanest test of a recipe quantity.

In scope: inventory items of nature food & beverage ingredients or goods for resale (Typology, Settings), and
any item that is a component of a recipe; not Assemble-to-Order items (they are the menu items themselves);
the locations and categories Movement leaves out stay out. Business Central is never written.
"""
import fnmatch
import io
import re
import threading
import time
import unicodedata
from datetime import date, timedelta

import locintel
import movement
import typology

PERIODS = (30, 60, 90)
TOL = 0.0005
STATUS = {"not_used": "Received, not discharged", "not_received": "Discharged, not received",
          "under": "Discharged far less than received", "over": "Discharged far more than received",
          "from_stock": "Discharged from stock", "ok": "In line"}
FLAGGED = ("not_received", "not_used", "under", "over")
STOP = {"per", "me", "pa", "i", "e", "te", "të", "dhe", "nga", "ne", "në", "kg", "gr", "g", "l", "ml", "lt", "cl", "pcs",
        "cope", "copë", "pako", "pk", "x", "the", "of", "and", "with", "opa", "salt", "lift", "rhg", "fresh", "fresket",
        "i/e", "art", "artikull"}
_cache, _cache_lock = {}, threading.Lock()


def settings_of(settings):
    inv = (settings or {}).get("inventory") or {}

    def num(key, default):
        try:
            v = float(inv.get(key) if inv.get(key) not in (None, "") else default)
            return v if v > 0 else float(default)
        except (TypeError, ValueError):
            return float(default)
    return {"low": num("discharge_low_ratio", 0.5), "high": num("discharge_high_ratio", 1.5),
            "cover_days": num("discharge_cover_days", 30), "min_value": num("discharge_min_value", 1000)}


def _words(text):
    """The words of a description, in order: lower case, no accents, no units or filler words."""
    t = unicodedata.normalize("NFKD", (text or "").lower())
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    return [w for w in re.findall(r"[a-z]{2,}", t) if w not in STOP]


def similarity(a, b):
    """0..1: the words two item descriptions share, plus the same first word, family and unit."""
    la, lb = _words(a["desc"]), _words(b["desc"])
    wa, wb = set(la), set(lb)
    if not (wa & wb):
        return 0.0
    score = len(wa & wb) / len(wa | wb) + (0.15 if la[0] == lb[0] else 0) \
        + (0.2 if a["family"] and a["family"] == b["family"] else 0) + (0.1 if a["unit"] and a["unit"] == b["unit"] else 0)
    return round(min(1.0, score), 2)


def analyse(company, mv, m, settings, period=30, excluded=None, location=None):
    """mv: a held Movement & dead stock result; m: recipes.master; excluded: negstock.excluded_adjustments."""
    as_of = date.fromisoformat(mv["as_of"])
    period = period if period in PERIODS else 30
    period = min(period, int(mv.get("history_days") or 90))
    start = as_of - timedelta(days=period - 1)
    S = settings_of(settings)
    R = typology.rules(settings)
    dist = R["distribution"]
    items = (m or {}).get("items") or {}
    uom = {(it, code): per for it, code, per in (m or {}).get("uom") or []}
    used_in, parents = {}, set()
    for parent, _line, typ, no, _desc, _nested, qty, unit in (m or {}).get("bom") or []:
        if typ == "Item" and no:
            used_in.setdefault(no, []).append((parent, qty, (unit or "").upper()))
            parents.add(parent)
    rules = mv.get("rules") or {}
    ex_locs, ex_cats = rules.get("exclude_locations") or [], set(rules.get("exclude_categories") or [])

    def left_out(loc, it):
        card = items.get(it) or {}
        return any(fnmatch.fnmatchcase((loc or "").upper(), p) for p in ex_locs) \
            or str(card.get("Item_Category_Code") or "").upper() in ex_cats

    def in_scope(it):
        card = items.get(it) or {}
        if card.get("Assembly_Policy") == "Assemble-to-Order":
            return False
        if card.get("Type") and card.get("Type") != "Inventory":
            return False
        return it in used_in or typology.classify(card, R["natures"])[2] in ("ingredients", "resale")

    closing = {(r[0], r[1]): r[2] for r in mv.get("_rows") or []}
    excluded = excluded or {}
    for key, ents in excluded.items():
        q_out = sum(q for d, q, _c, _doc in ents if d <= as_of.isoformat())
        if q_out and not left_out(*key):
            closing[key] = closing.get(key, 0.0) - q_out
    flows = {}
    sold_parent = {}
    with movement._lock:
        q = movement._db().execute(
            "SELECT location, item, kind, SUM(qty) FROM mv WHERE company=? AND day>=? AND day<=? GROUP BY location, item, kind",
            (company, start.isoformat(), as_of.isoformat())).fetchall()
    for loc, it, kind, qty in q:
        if it in parents and kind in ("sale", "sale_return"):
            sold_parent[(loc, it)] = sold_parent.get((loc, it), 0.0) - qty
        flows.setdefault((loc, it), {})[kind] = flows.get((loc, it), {}).get(kind, 0.0) + qty
    for key, ents in excluded.items():
        for d, q_, _c, _doc in ents:
            if start.isoformat() <= d <= as_of.isoformat() and key in flows:
                flows[key]["adj_pos"] = flows[key].get("adj_pos", 0.0) - q_

    lines = []
    for key in set(flows) | {k for k, v in closing.items() if v}:
        loc, it = key
        if location and loc != location:
            continue
        if left_out(loc, it) or not in_scope(it):
            continue
        f = flows.get(key, {})
        rcv = f.get("receipt", 0.0) + f.get("transfer_in", 0.0)
        ret = -f.get("purchase_return", 0.0)
        made = f.get("output", 0.0)
        cons = -f.get("consumption", 0.0)
        sold = -(f.get("sale", 0.0) + f.get("sale_return", 0.0))
        ship = -f.get("transfer_out", 0.0)
        net = sum(f.values())
        close = closing.get(key, 0.0)
        opening = close - net
        inflow = rcv - ret + made
        out = cons + sold
        is_dist = loc.upper() in dist
        if inflow <= TOL and out <= TOL and not (is_dist and ship > TOL):
            continue
        card = items.get(it) or {}
        cost = float(card.get("Unit_Cost") or 0)
        daily = out / period if out > 0 else 0.0
        cover = round(close / daily) if daily > 0 and close > 0 else None
        out, inflow = out + 0.0, inflow + 0.0                 # no "−0"
        ratio = round(out / inflow, 2) + 0.0 if inflow > TOL else None
        recipes_all = used_in.get(it, [])
        sold_here = [(p, per, u) for p, per, u in recipes_all if sold_parent.get((loc, p), 0) > TOL]
        if inflow > TOL and out <= TOL and not (is_dist and ship >= inflow * 0.5):
            status, value = "not_used", inflow * cost
        elif out > TOL and inflow <= TOL:
            status, value = ("from_stock", 0.0) if opening >= out - TOL else ("not_received", (out - max(opening, 0)) * cost)
        elif ratio is not None and ratio < S["low"] and (cover is None or cover > S["cover_days"]) and not is_dist:
            status, value = "under", (inflow - out) * cost
        elif ratio is not None and ratio > S["high"] and (close <= TOL or (cover is not None and cover < 3)):
            status, value = "over", (out - inflow) * cost
        else:
            status, value = "ok", 0.0
        if status in ("under", "over") and abs(value) < S["min_value"]:
            status, value = "ok", 0.0
        typ, fam, nat = typology.classify(card, R["natures"])
        lines.append({"location": loc, "item": it, "desc": card.get("Description") or "", "unit":
                      str(card.get("Base_Unit_of_Measure") or "").upper(), "family": fam, "nature": nat,
                      "received": round(rcv - ret, 3), "made": round(made, 3), "consumed": round(cons, 3),
                      "sold": round(sold, 3), "shipped": round(ship, 3), "opening": round(opening, 3),
                      "closing": round(close, 3), "ratio": ratio, "cover": cover, "status": status,
                      "value": round(value, 0), "unit_cost": cost, "distribution": is_dist,
                      "in_recipes": len(recipes_all), "recipes_here": [p for p, _, _ in sold_here][:5],
                      "recipes": [{"parent": p, "desc": (items.get(p) or {}).get("Description") or "", "per": per,
                                   "unit": u or str(card.get("Base_Unit_of_Measure") or "").upper(),
                                   "no_conversion": bool(u) and u != str(card.get("Base_Unit_of_Measure") or "").upper()
                                   and (it, u) not in uom,
                                   "sold": round(sold_parent.get((loc, p), 0.0), 2)}
                                  for p, per, u in sorted(recipes_all, key=lambda x: -sold_parent.get((loc, x[0]), 0))[:6]]})

    # where each code comes in, company-wide (a recipe is one BOM for every location)
    received_at = {}
    for x in lines:
        if x["received"] + x["made"] > TOL:
            received_at.setdefault(x["item"], []).append(x["location"])

    # pairs: discharged-not-received with received-not-(enough-)discharged, same location
    by_loc = {}
    for x in lines:
        by_loc.setdefault(x["location"], []).append(x)
    for loc, xs in by_loc.items():
        donors = [y for y in xs if y["status"] in ("not_used", "under")]
        for x in xs:
            if x["status"] != "not_received":
                continue
            best = None
            for y in donors:
                s = similarity(x, y)
                if s >= 0.45 and (best is None or s > best[0]):
                    best = (s, y)
            if best:
                y = best[1]
                x["pair"] = {"item": y["item"], "desc": y["desc"], "score": best[0], "status": y["status"]}
                y.setdefault("paired_with", []).append(x["item"])

    for x in lines:
        x["note"], x["action"] = _advice(x, received_at)
    rank = {"not_received": 0, "not_used": 1, "over": 2, "under": 3, "from_stock": 4, "ok": 5}
    lines.sort(key=lambda x: (rank[x["status"]], -abs(x["value"])))

    # interventions: one line per thing to change, the most valuable first
    acts = []
    for x in lines:
        if x["status"] not in FLAGGED:
            continue
        if x["status"] == "not_used" and x.get("paired_with"):
            continue                                           # said once, on the discharged code
        acts.append({"location": x["location"], "item": x["item"], "desc": x["desc"], "status": x["status"],
                     "action": x["action"], "value": x["value"], "recipes": x["recipes_here"] or
                     [r["parent"] for r in x["recipes"][:3]], "pair": (x.get("pair") or {}).get("item", "")})
    acts.sort(key=lambda a: (0 if a["pair"] else 1, -abs(a["value"])))

    # company view: every location summed
    comp = {}
    for x in lines:
        c = comp.setdefault(x["item"], {"item": x["item"], "desc": x["desc"], "unit": x["unit"], "family": x["family"],
                                        "purchased": 0.0, "made": 0.0, "consumed": 0.0, "sold": 0.0, "opening": 0.0,
                                        "closing": 0.0, "locations_in": 0, "locations_out": 0, "unit_cost": x["unit_cost"]})
        c["made"] += x["made"]
        c["consumed"] += x["consumed"]
        c["sold"] += x["sold"]
        c["opening"] += x["opening"]
        c["closing"] += x["closing"]
        c["locations_in"] += x["received"] + x["made"] > TOL
        c["locations_out"] += x["consumed"] + x["sold"] > TOL
    for (loc, it), f in flows.items():                       # purchases only: transfers cancel out
        if it in comp and not left_out(loc, it):
            comp[it]["purchased"] += f.get("receipt", 0.0) + f.get("purchase_return", 0.0)
    company_rows = []
    for c in comp.values():
        inflow, out = c["purchased"] + c["made"], c["consumed"] + c["sold"]
        c["ratio"] = round(out / inflow, 2) + 0.0 if inflow > TOL else None
        daily = out / period if out > 0 else 0
        c["cover"] = round(c["closing"] / daily) if daily > 0 and c["closing"] > 0 else None
        if inflow > TOL and out <= TOL:
            st, v = "not_used", inflow * c["unit_cost"]
        elif out > TOL and inflow <= TOL:
            st, v = ("from_stock", 0.0) if c["opening"] >= out - TOL else ("not_received", (out - max(c["opening"], 0)) * c["unit_cost"])
        elif c["ratio"] is not None and c["ratio"] < S["low"] and (c["cover"] is None or c["cover"] > S["cover_days"]):
            st, v = "under", (inflow - out) * c["unit_cost"]
        elif c["ratio"] is not None and c["ratio"] > S["high"] and (c["closing"] <= TOL or (c["cover"] or 99) < 3):
            st, v = "over", (out - inflow) * c["unit_cost"]
        else:
            st, v = "ok", 0.0
        if st in ("under", "over") and abs(v) < S["min_value"]:
            st, v = "ok", 0.0
        c.update(status=st, value=round(v, 0))
        for k in ("purchased", "made", "consumed", "sold", "opening", "closing"):
            c[k] = round(c[k], 3)
        company_rows.append(c)
    company_rows.sort(key=lambda c: (rank[c["status"]], -abs(c["value"])))

    try:
        reg = {(r.get("bc") or "").upper(): r for r in locintel.registry(settings)
               if (r.get("company") or "").upper() == (company or "").upper() and r.get("bc")}
    except Exception:                                              # noqa: BLE001
        reg = {}
    locs = {}
    for x in lines:
        if x["location"] not in locs:
            g = reg.get(x["location"].upper())
            locs[x["location"]] = {"location": x["location"], "codes_in": 0, "codes_out": 0, "value": 0.0, "pairs": 0,
                                   "name": (g or {}).get("name") or x["location"], "brand": (g or {}).get("brand") or "",
                                   "group": "restaurant" if g else ("warehouse" if x["location"].upper() in dist else "other"),
                                   "top": None, **{k: 0 for k in STATUS}}
        L = locs[x["location"]]
        L["codes_in"] += x["received"] + x["made"] > TOL
        L["codes_out"] += x["consumed"] + x["sold"] > TOL
        L[x["status"]] += 1
        L["pairs"] += bool(x.get("pair"))
        if x["status"] in FLAGGED:
            L["value"] += abs(x["value"])
    for a in acts:                                         # the first thing to change at each location
        L = locs.get(a["location"])
        if L is not None and L["top"] is None:
            L["top"] = {"status": a["status"], "item": a["item"], "pair": a["pair"], "desc": a["desc"]}
    kpis = {"codes_in": sum(1 for x in lines if x["received"] + x["made"] > TOL),
            "codes_out": sum(1 for x in lines if x["consumed"] + x["sold"] > TOL),
            **{k: [sum(1 for x in lines if x["status"] == k), round(sum(abs(x["value"]) for x in lines if x["status"] == k), 0)]
               for k in STATUS},
            "pairs": sum(1 for x in lines if x.get("pair")), "interventions": len(acts),
            "company_flagged": sum(1 for c in company_rows if c["status"] in FLAGGED)}
    return {"company": company, "as_of": as_of.isoformat(), "from": start.isoformat(), "period": period,
            "location": location or "", "settings": S, "kpis": kpis, "lines": lines, "interventions": acts,
            "company_view": company_rows, "locations": sorted(locs.values(), key=lambda L: -L["value"]),
            "statuses": STATUS, "excluded_entries": sum(len(v) for v in excluded.values()),
            "definitions": DEFINITIONS, "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S")}


def cut(r, allowed):
    """The analysis for some locations only (a person or a report limited to them): their lines, interventions and
    squares; the company view (every location summed) is the whole company and is left out. allowed: upper-case codes."""
    if not isinstance(r, dict) or allowed is None or not isinstance(r.get("lines"), list):
        return r
    A = {str(x).strip().upper() for x in allowed}
    ok = lambda x: str((x or {}).get("location") or "").strip().upper() in A
    lines = [x for x in r["lines"] if ok(x)]
    acts = [a for a in r.get("interventions") or [] if ok(a)]
    kpis = {"codes_in": sum(1 for x in lines if x["received"] + x["made"] > TOL),
            "codes_out": sum(1 for x in lines if x["consumed"] + x["sold"] > TOL),
            **{k: [sum(1 for x in lines if x["status"] == k), round(sum(abs(x["value"]) for x in lines if x["status"] == k), 0)]
               for k in STATUS},
            "pairs": sum(1 for x in lines if x.get("pair")), "interventions": len(acts), "company_flagged": None}
    return dict(r, lines=lines, interventions=acts, kpis=kpis, company_view=[], cut=True,
                locations=[L for L in r.get("locations") or [] if ok(L)])


def _q(v):
    a = abs(v or 0)
    s = f"{a:,.0f}" if a >= 100 else (f"{a:,.1f}" if a >= 10 else f"{a:,.2f}").rstrip("0").rstrip(".")
    return ("−" if (v or 0) < 0 else "") + s


def _advice(x, received_at):
    """(note, action) of one line, in words for the cost controller."""
    u = x["unit"].lower()
    rec = x["recipes_here"]
    rtxt = (", ".join(rec[:3]) + (f" and {len(rec) - 3} more" if len(rec) > 3 else "")) if rec else ""
    st = x["status"]
    if st == "not_used":
        if x.get("paired_with"):
            return (f"Received {_q(x['received'] + x['made'])} {u}, not discharged; the recipes discharge "
                    f"{', '.join(x['paired_with'][:2])} instead.", "See the paired code.")
        if not x["in_recipes"] and x["nature"] == "resale":
            return (f"Received {_q(x['received'] + x['made'])} {u}, nothing sold or discharged.",
                    "Check the Simphony item is mapped to this code, or that it is sold under another code.")
        if not x["in_recipes"]:
            return (f"Received {_q(x['received'] + x['made'])} {u}; in no recipe.",
                    "Add it to the recipes that use it, or find the code the recipes use for it.")
        return (f"Received {_q(x['received'] + x['made'])} {u}; in {x['in_recipes']} recipes, none sold here in the period.",
                "Check the menu items sold here are mapped to these recipes, or the item is not needed here.")
    if st == "not_received":
        where = [L for L in received_at.get(x["item"], []) if L != x["location"]]
        base = f"Discharged {_q(x['consumed'])} {u} by recipes{' (' + rtxt + ')' if rtxt else ''}; not received here."
        if x.get("pair"):
            p = x["pair"]
            if where:
                return (base + f" This location receives {p['item']} {p['desc']} instead; {x['item']} comes in at "
                        f"{', '.join(where[:3])}.", f"Receive {x['item']} here instead of {p['item']}, or align the code.")
            return (base + f" This location receives {p['item']} {p['desc']} instead.",
                    f"Replace {x['item']} with {p['item']} in the recipes{' ' + rtxt if rtxt else ''}.")
        if where:
            return (base + f" It comes in at {', '.join(where[:3])}.",
                    "Transfer or buy it for this location, or the recipe should use the code this location receives.")
        return (base + " No location receives it in the period.",
                "The recipes use a code nobody receives: replace it with the code that is bought.")
    if st == "under":
        return (f"Discharged {x['ratio'] * 100:.0f}% of what came in ({_q(x['consumed'] + x['sold'])} of "
                f"{_q(x['received'] + x['made'])} {u}); {x['cover'] if x['cover'] is not None else 'no'} days of cover left.",
                "Recipe quantity too low, missing from some recipes, or waste not posted: check the quantity per"
                + (f" in {rtxt}." if rtxt else "."))
    if st == "over":
        bad = [r for r in x["recipes"] if r["no_conversion"]]
        return (f"Discharged {x['ratio'] * 100:.0f}% of what came in ({_q(x['consumed'] + x['sold'])} of "
                f"{_q(x['received'] + x['made'])} {u}); stock run down to {_q(x['closing'])}.",
                (f"Unit {bad[0]['unit']} in {bad[0]['parent']} has no conversion. " if bad else "")
                + "Recipe quantity too high or wrong unit: check the quantity per" + (f" in {rtxt}." if rtxt else "."))
    if st == "from_stock":
        return (f"Discharged {_q(x['consumed'] + x['sold'])} {u} from the stock held at the start ({_q(x['opening'])}).", "")
    return ("", "")


DEFINITIONS = {
    "in": "In = purchase receipts net of returns to the vendor, transfers in and assembly output (made here), in the "
          "period. Item ledger, verified.",
    "out": "Discharged = assembly consumption (what the recipes take out when a menu item is sold) and sales of the item "
           "as it is, net of returns. Transfers out are shown apart: at the distribution locations passing goods on is "
           "their job.",
    "stock": "Closing = quantity on hand at the stock date (Business Central); opening = closing less every movement of the "
             "period. Positive adjustments left out of the negative stock email are left out here too.",
    "signals": "Received, not discharged · Discharged, not received (the opening stock did not cover it) · Discharged "
               "far less than received (ratio below the low limit and more cover left than the cover limit) · "
               "Discharged far more than received (ratio above the high limit, stock run down to under 3 days or below "
               "zero). Limits in Finance → Settings → Inventory. Values at item unit cost, estimated.",
    "pairs": "A code discharged and not received is paired with a code received and not, or too little, discharged at the "
             "same location when the descriptions share words (with family and unit as a bonus): score 0.45 and above. "
             "A recipe is one BOM for every location: when other locations receive the recipe's code, the advice is to "
             "receive it here; when none does, to change the recipe.",
    "company": "Company view: every location summed, so transfers between locations cancel out: purchases and output "
               "against recipe consumption and sales.",
}


def remember(key, r):
    with _cache_lock:
        _cache[key] = (time.time(), r)
        for k, _ in sorted(_cache.items(), key=lambda kv: kv[1][0])[:-6]:
            _cache.pop(k, None)


def held(key):
    with _cache_lock:
        hit = _cache.get(key)
    return hit[1] if hit else None


def render_xlsx(r):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    fill, font = PatternFill("solid", fgColor="E4F3F9"), Font(bold=True, color="0E2733")
    money, qty = '#,##0;[Red]-#,##0', '#,##0.00;[Red]-#,##0.00'

    def sheet(title, header, rows, formats, widths, first=False):
        ws = wb.active if first else wb.create_sheet()
        ws.title = title[:31]
        ws.append(header)
        for c in ws[1]:
            c.fill, c.font, c.alignment = fill, font, Alignment(wrap_text=True, vertical="top")
        for row in rows:
            ws.append(row)
        for i, f in enumerate(formats, start=1):
            if f:
                for cell in ws.iter_rows(min_row=2, min_col=i, max_col=i):
                    cell[0].number_format = f
        for i, w in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.freeze_panes = "A2"
        if rows:
            ws.auto_filter.ref = ws.dimensions
        return ws
    S = STATUS
    sheet("Interventions", ["Location", "Signal", "Item", "Description", "Paired code", "What to do", "Recipes", "Value"],
          [[a["location"], S[a["status"]], a["item"], a["desc"], a["pair"], a["action"], ", ".join(a["recipes"]),
            a["value"]] for a in r["interventions"]],
          [None] * 7 + [money], [14, 26, 11, 32, 11, 80, 34, 12], first=True)
    sheet("Lines", ["Location", "Item", "Description", "Family", "Unit", "Signal", "Received", "Made", "Discharged by recipes",
                    "Sold as it is", "Shipped out", "Opening", "Closing", "Discharged / in", "Days of cover", "Value",
                    "Unit cost", "In recipes", "Recipes sold here", "Paired code", "Score", "Why", "What to do"],
          [[x["location"], x["item"], x["desc"], x["family"], x["unit"], S[x["status"]], x["received"], x["made"],
            x["consumed"], x["sold"], x["shipped"], x["opening"], x["closing"], x["ratio"], x["cover"], x["value"],
            round(x["unit_cost"], 2), x["in_recipes"], ", ".join(x["recipes_here"]), (x.get("pair") or {}).get("item", ""),
            (x.get("pair") or {}).get("score"), x["note"], x["action"]] for x in r["lines"]],
          [None] * 6 + [qty] * 7 + ['0%', None, money, '#,##0.00', None, None, None, '0.00', None, None],
          [14, 11, 32, 18, 6, 24, 10, 9, 11, 10, 10, 10, 10, 9, 8, 11, 9, 8, 28, 11, 6, 70, 60])
    sheet("Recipes using the item", ["Location", "Item", "Description", "Recipe", "Recipe description", "Quantity per",
                                     "Unit", "No conversion", "Recipe sold here"],
          [[x["location"], x["item"], x["desc"], rc["parent"], rc["desc"], rc["per"], rc["unit"],
            "yes" if rc["no_conversion"] else "", rc["sold"]]
           for x in r["lines"] if x["status"] in FLAGGED for rc in x["recipes"]],
          [None] * 5 + [qty, None, None, qty], [14, 11, 30, 11, 30, 11, 7, 10, 12])
    sheet("Company view", ["Item", "Description", "Family", "Unit", "Signal", "Purchased", "Made", "Discharged by recipes",
                           "Sold as it is", "Opening", "Closing", "Discharged / in", "Days of cover", "Locations receiving",
                           "Locations discharging", "Value"],
          [[c["item"], c["desc"], c["family"], c["unit"], S[c["status"]], c["purchased"], c["made"], c["consumed"], c["sold"],
            c["opening"], c["closing"], c["ratio"], c["cover"], c["locations_in"], c["locations_out"], c["value"]]
           for c in r["company_view"]],
          [None] * 5 + [qty] * 6 + ['0%', None, None, None, money], [11, 32, 18, 6, 24, 10, 9, 11, 10, 10, 10, 9, 8, 9, 9, 11])
    sheet("By location", ["Location", "Codes received", "Codes discharged", *S.values(), "Value flagged"],
          [[L["location"], L["codes_in"], L["codes_out"], *[L[k] for k in S], round(L["value"])] for L in r["locations"]],
          [None] * 9 + [money], [14, 10, 10, 14, 14, 14, 14, 12, 9, 13])
    ws = sheet("Read me", ["Received vs discharged"],
               [[f"{r['company']}{' · ' + r['location'] if r.get('location') else ''}: {r['from']} to {r['as_of']} "
                 f"({r['period']} days). Limits: discharged/in below {r['settings']['low']:.0%} with more than "
                 f"{r['settings']['cover_days']:.0f} days of cover, above {r['settings']['high']:.0%} with the stock run down; "
                 f"value at least {r['settings']['min_value']:,.0f}."]]
               + [[v] for v in DEFINITIONS.values()] + [["Business Central was only read."]], [None], [130])
    for row in ws.iter_rows(min_row=2):
        row[0].alignment = Alignment(wrap_text=True, vertical="top")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
