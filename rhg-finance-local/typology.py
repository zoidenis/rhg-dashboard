"""Inventory typology and turnover (Inventory Control).

What the stock is made of and how many days of use it covers, built on the data the
Movement & dead stock report already holds (no extra reads from Business Central):

- Typology: the item's type (BC Item Category Group 2) and family (Group 4), grouped into a
  nature that decides how turnover is read (Settings, default below):
    ingredients  food and beverage raw materials and semi-finished (LP, PN)
    resale       goods sold as purchased (LP E SHITSHME)
    supplies     operating supplies: packaging, cleaning, bar and kitchen consumables, fuel,
                 stationery (often used up through negative adjustments)
    equipment    uniforms and small equipment: held, not turned over (value only)
- Use in the history: qualifying consumption as defined by Movement (assembly consumption and
  sales net of returns; transfers out at distribution locations). For supplies, negative
  adjustments also count when Settings says so (default yes). Value = quantity x item unit cost.
- Days of cover = stock value / (use at cost / days of history). Turnover = use at cost over a
  year / stock value. For the group, transfers out of distribution locations are not added to
  the use, so nothing is counted twice; their stock is.
- Target days of cover per location-item, from the deliveries in the history: days of history /
  days with a delivery (purchase receipt, transfer in, or production run for made items) = the
  delivery cycle, plus the safety days in Settings. A family target in Settings replaces it.
  Families and groups show the target weighted by use. Stock above target = value - target x daily use (lines with use only); stock
  with no use in the history is shown on its own.
- Fresh families (Settings) covering more days than the count threshold are flagged for a
  physical count: that stock cannot be on the shelf.

Nothing is written to Business Central.
"""
import io
import math

NATURES = ("ingredients", "resale", "supplies", "equipment", "other")
NATURE_LABEL = {"ingredients": "Food & beverage ingredients", "resale": "Goods for resale",
                "supplies": "Operating supplies", "equipment": "Equipment & uniforms", "other": "No category"}
DEFAULT_NATURES = ("LP=ingredients, PN=ingredients, LP E SHITSHME=resale, I SHITSHEM=resale, "
                   "AMBALAZH=supplies, PRODUKT PASTRIMI=supplies, BAR=supplies, RESTORANT=supplies, "
                   "LENDE DJEGESE=supplies, MJETE KANCELARIE=supplies, MATERIALE NDIHMESE=supplies, "
                   "MIREMBAJTJE=supplies, UNIFORMA=equipment, II=equipment")
DEFAULT_FRESH = "PERIME&FRUTE, MISH&SHPENDE, PESHK&PROD.DETI, BULMET&DJATHE, PROD BUKE&FURRE"


def _pairs(text):
    out = {}
    for part in (text or "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip():
                out[k.strip().upper()] = v.strip()
    return out


def _list(text):
    return {x.strip().upper() for x in (text or "").split(",") if x.strip()}


def rules(settings):
    inv = settings.get("inventory") or {}
    natures = {k: (v.lower() if v.lower() in NATURES else "supplies")
               for k, v in _pairs(inv.get("turnover_natures") or DEFAULT_NATURES).items()}
    targets = {}
    for k, v in _pairs(inv.get("turnover_targets")).items():
        try:
            if float(v) > 0:
                targets[k] = float(v)
        except ValueError:
            pass

    def num(key, default):
        try:
            return float(inv.get(key) if inv.get(key) not in (None, "") else default)
        except ValueError:
            return float(default)
    return {"natures": natures, "targets": targets, "safety_days": num("turnover_safety_days", 2),
            "supplies_adjustments": (inv.get("turnover_supplies_adjustments") or "yes").strip().lower()
            in ("yes", "true", "1"),
            "fresh": _list(inv.get("turnover_fresh_families") if inv.get("turnover_fresh_families") is not None
                           else DEFAULT_FRESH),
            "fresh_days": num("turnover_fresh_check_days", 30),
            "distribution": _list(inv.get("distribution_locations") or "MAGAZINE, CENTRAL")}


def classify(card, natures):
    """(type, family, nature) of an item card."""
    typ = str(card.get("Item_Category_Group_2") or "").strip().upper()
    fam = str(card.get("Item_Category_Group_4") or "").strip().upper()
    if typ == "PN" or not fam:
        fam = typ or str(card.get("Item_Category_Group_3") or card.get("Item_Category_Code") or "").strip().upper()
    if not typ:
        return "(NO TYPE)", fam or "(NO FAMILY)", "other"
    return typ, fam or typ, natures.get(typ, "supplies")


def analyse(mv, items, settings):
    """mv: the held Movement result (with _typo); items: item cards from the recipes master."""
    R = rules(settings)
    days = mv["history_days"]
    dist = R["distribution"]
    groups = {}          # (location, type, family) -> additive figures
    lines = []
    for loc, it, qty, cost, use, adj_neg, rdays in mv.get("_typo") or []:
        card = items.get(it) or {}
        typ, fam, nat = classify(card, R["natures"])
        used = use + (adj_neg if nat == "supplies" and R["supplies_adjustments"] else 0.0)
        value = qty * cost if qty > 0 else 0.0
        daily = max(used, 0.0) * cost / days
        cycle = days / rdays if rdays else None
        if fam in R["targets"]:
            target, source = R["targets"][fam], "settings"
        elif cycle is not None:
            target, source = math.ceil(cycle + R["safety_days"]), "deliveries"
        else:
            target, source = None, ""
        doh = value / daily if daily > 0 else None
        turned = nat not in ("equipment",)
        excess = max(0.0, value - target * daily) if turned and target and daily > 0 else 0.0
        fresh_check = bool(fam in R["fresh"] and value > 0 and (doh is None or doh > R["fresh_days"]))
        g = groups.setdefault((loc, typ, fam), {"nature": nat, "lines": 0, "value": 0.0, "neg": 0.0, "use": 0.0,
                                                 "nouse": 0.0, "excess": 0.0, "tw": 0.0, "cw": 0.0, "dw": 0.0,
                                                 "count": 0})
        g["lines"] += 1 if qty > 0 else 0
        g["value"] += value
        g["neg"] += qty * cost if qty < 0 else 0.0
        g["use"] += daily * days
        g["nouse"] += value if value > 0 and daily <= 0 else 0.0
        g["excess"] += excess
        if target and daily > 0:
            g["tw"] += target * daily
            g["dw"] += daily
        if cycle is not None and daily > 0:
            g["cw"] += cycle * daily
        g["count"] += 1 if fresh_check else 0
        if turned and value > 0 and (fresh_check or (target and doh is not None and doh > 3 * target)
                                     or (daily <= 0 and value >= 100000)):
            lines.append([loc, it, card.get("Description") or "", typ, fam, nat, round(qty, 3),
                          card.get("Base_Unit_of_Measure") or "", round(value, 0), round(daily * days / cost, 3)
                          if cost else 0.0, round(doh, 0) if doh is not None else None, target, source,
                          round(cycle, 1) if cycle else None, round(excess, 0), fresh_check])
    rows = [[loc, typ, fam, g["nature"], g["lines"], round(g["value"], 0), round(g["neg"], 0), round(g["use"], 0),
             round(g["nouse"], 0), round(g["excess"], 0), round(g["tw"], 2), round(g["cw"], 2), round(g["dw"], 2),
             g["count"]] for (loc, typ, fam), g in groups.items()]
    lines.sort(key=lambda x: (-(x[14] or 0), -(x[8] if x[15] else 0)))
    fresh_lines = [x for x in lines if x[15]]
    return {
        "company": mv["company"], "as_of": mv["as_of"], "history_days": days, "history_from": mv.get("history_from"),
        "checked_at": mv.get("checked_at"), "excluded": mv.get("excluded") or [],
        "cols": ["location", "type", "family", "nature", "lines", "value", "negative", "use", "no_use", "excess",
                 "target_w", "cycle_w", "daily_w", "count_flags"],
        "rows": rows,
        "item_cols": ["location", "item", "description", "type", "family", "nature", "qty", "unit", "value",
                      "used", "days_of_cover", "target", "target_source", "delivery_cycle", "above_target",
                      "count_it"],
        "items": lines[:3000], "items_total": len(lines), "fresh_flags": len(fresh_lines),
        "natures": NATURE_LABEL,
        "rules": {"distribution": sorted(dist), "safety_days": R["safety_days"],
                  "supplies_adjustments": R["supplies_adjustments"], "fresh": sorted(R["fresh"]),
                  "fresh_days": R["fresh_days"], "targets": R["targets"],
                  "natures": R["natures"]},
        "definitions": DEFINITIONS,
    }


DEFINITIONS = {
    "typology": "Type = BC Item Category Group 2, family = Group 4 (PN items are one family). Each type has a "
                "nature (Settings): food and beverage ingredients, goods for resale, operating supplies, "
                "equipment and uniforms.",
    "use": "Use in the history at item unit cost: assembly consumption and sales net of returns; transfers out "
           "at distribution locations; for operating supplies also negative adjustments when Settings says so. "
           "For the whole group, transfers out of distribution locations are not added, so nothing is counted "
           "twice.",
    "cover": "Days of cover = stock value / average daily use at cost. Turnover = yearly use at cost / stock "
             "value. Equipment and uniforms are not turned over and show value only.",
    "target": "Target days of cover per location and item = the delivery cycle seen in the history (days of "
              "history / days with a purchase receipt, transfer in or production run) plus the safety days "
              "in Settings. A family "
              "target in Settings replaces it. Families, types and locations show the target weighted by use.",
    "above_target": "Stock above target = stock value - target x daily use, on lines with use. Stock with no use "
                    "in the history is shown separately.",
    "count": "Fresh families (Settings) covering more days than the count threshold, or with stock and no use, "
             "are flagged for a physical count: that quantity cannot be on the shelf.",
    "value": "Quantities on hand are verified (BC); values at item unit cost are estimated.",
}


def render_xlsx(r):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    fill, bold = PatternFill("solid", fgColor="E4F3F9"), Font(bold=True)
    days = r["history_days"]
    dist = set(r["rules"]["distribution"])

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

    def summarise(keyf, group=True):
        acc = {}
        for loc, typ, fam, nat, n, v, neg, use, nouse, ex, tw, cw, dw, cnt in r["rows"]:
            k = keyf(loc, typ, fam, nat)
            a = acc.setdefault(k, [nat, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
            a[1] += n
            a[2] += v
            a[5] += ex
            a[4] += nouse
            if group and loc.upper() in dist:
                continue
            a[3] += use
            a[6] += tw
            a[7] += cw
            a[8] += dw
        out = []
        for k, (nat, n, v, use, nouse, ex, tw, cw, dw) in sorted(acc.items(), key=lambda kv: -kv[1][2]):
            turned = nat != "equipment"
            doh = round(v / (use / days)) if use > 0 and turned else None
            out.append(list(k) + [r["natures"].get(nat, nat), n, round(v), round(use), doh,
                                  round(use * 365 / days / v, 1) if v and use and turned else None,
                                  round(cw / dw, 1) if dw else None, round(tw / dw) if dw else None,
                                  round(ex), round(nouse)])
        return out
    tail = ["Nature", "Lines with stock", "Stock value", f"Use at cost, {days} days", "Days of cover",
            "Turnover / year", "Delivery cycle (days)", "Target days", "Above target", "No use in history"]
    ws = wb.active
    ws.title = "By family"
    sheet(ws, ["Type", "Family"] + tail, summarise(lambda l, t, f, n: (t, f)), [18, 22, 26, 10, 14, 16, 10, 10, 12, 10, 14, 14])
    sheet(wb.create_sheet("By type"), ["Type"] + tail, summarise(lambda l, t, f, n: (t,)), [18, 26, 10, 14, 16, 10, 10, 12, 10, 14, 14])
    sheet(wb.create_sheet("By location and family"), ["Location", "Type", "Family"] + tail,
          summarise(lambda l, t, f, n: (l, t, f), group=False), [14, 18, 22, 26, 10, 14, 16, 10, 10, 12, 10, 14, 14])
    sheet(wb.create_sheet("Lines to review"),
          ["Location", "Item", "Description", "Type", "Family", "Nature", "Qty on hand", "Unit", "Value (est.)",
           f"Used, {days} days", "Days of cover", "Target days", "Target from", "Delivery cycle (days)",
           "Above target", "Count it"],
          [x[:5] + [r["natures"].get(x[5], x[5])] + x[6:15] + ["yes" if x[15] else ""] for x in r["items"]],
          [12, 12, 40, 16, 20, 24, 12, 8, 14, 12, 12, 10, 12, 12, 14, 9])
    ws = wb.create_sheet("Definitions")
    for k, v in DEFINITIONS.items():
        ws.append([k, v])
    ws.column_dimensions["A"].width = 16
    ws.column_dimensions["B"].width = 140
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
