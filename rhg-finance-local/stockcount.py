"""Inventory → Stock count analysis: the stock Business Central holds at a location at the end of a month (the
Inventory Valuation of the month: opening, increases, decreases, closing, in quantity and value) against the
physical count of that location, uploaded as the Excel count form, at the average cost of the month end.

Valuation, per item of the location, read from the item ledger:
  * closing quantity = BC's own Inventory by date at the month end (item card flow field, Location filter);
  * closing value = the remaining cost of the open item ledger entries today (actual + expected cost, in
    proportion to the remaining quantity) less the cost of every entry posted after the month end; the same for
    the quantity, which must give BC's closing quantity (checked);
  * the month's entries (from the 1st to the last day): increases (positive quantity) and decreases (negative);
  * opening = closing − increases − decreases.
Average cost at the month end = closing value / closing quantity. When the closing quantity is not above zero (or
its value is not), the cost of the month's increases is used, then the item card's Unit Cost (named per line).
This is exact when the cost adjustment (Adjust Cost – Item Entries) has run up to the month end: in this BC the
Automatic Cost Adjustment is Never, so it is part of the month-end close.

Count form (Excel): the sheet whose header row has "Kartela" and "GjendjeFizike" (the RHG form; English names
Item No / Counted are taken too); the description, unit, the form's own system stock ("Gjendje Finance"), the
category (the column next to Kartela) and a comment are read when present. A blank count is "not counted", not
zero. Each upload is kept per company, location and month (data/stock_counts/), replacing the one before.

Business Central is only read.
"""
import base64
import io
import json
import re
import threading
import time
import traceback
from datetime import date, datetime, timedelta

import store

DIR = store.DATA_DIR / "stock_counts"
KEEP = 900                                   # seconds a valuation read is held
TOL = 0.0005                                 # quantities closer than this are equal
_lock = threading.Lock()
_results, _jobs = {}, {}

MONTHS_SQ = {"janar": 1, "shkurt": 2, "mars": 3, "prill": 4, "maj": 5, "qershor": 6, "korrik": 7, "gusht": 8,
             "shtator": 9, "tetor": 10, "nentor": 11, "nëntor": 11, "dhjetor": 12}


def _f(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def month_range(month):
    """"2026-10" → (date(2026,10,1), date(2026,10,31))."""
    y, m = (int(x) for x in str(month)[:7].split("-"))
    first = date(y, m, 1)
    last = (first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    return first, last


def default_month(today=None):
    today = today or date.today()
    last = today.replace(day=1) - timedelta(days=1)
    return last.strftime("%Y-%m")


def _norm(s):
    return re.sub(r"[^a-z0-9ëç]+", "", str(s or "").strip().lower())


# ---------------------------------------------------------------- the count form
HEAD = {"item": ("kartela", "itemno", "item", "kodi", "nr", "no"),
        "description": ("pershkrimi", "përshkrimi", "description", "emertimi"),
        "unit": ("njesia", "njësia", "unit", "uom", "baseunitofmeasure"),
        "system": ("gjendjefinance", "gjendjeefinances", "systemqty", "qtycalculated", "gjendjesistem"),
        "counted": ("gjendjefizike", "gjendjafizike", "counted", "qtyphysinventory", "physicalqty", "sasianumeruar"),
        "comment": ("column2", "koment", "komenti", "shenim", "shënim", "comment", "note", "verejtje")}


def _header(row):
    """{field: column index} when the row is a header row (it names the item and the counted quantity)."""
    names = [_norm(c) for c in row]
    out = {}
    for field, keys in HEAD.items():
        for i, n in enumerate(names):
            if n and n in keys and field not in out and i not in out.values():
                out[field] = i
    if "item" in out and "counted" in out:
        nxt = out["item"] + 1
        if nxt < len(names) and nxt not in out.values() and (names[nxt].startswith("column") or names[nxt] in
                                                            ("kategoria", "category", "grupi", "group")):
            out["category"] = nxt
        return out
    return None


def _meta(rows):
    """What the form says above its table: location, date, who counted, month."""
    meta = {}
    for r in rows:
        cells = [c for c in r if c not in (None, "")]
        if not cells:
            continue
        label = _norm(cells[0])
        val = cells[1] if len(cells) > 1 else None
        if label.startswith("dataora") or label == "data":
            meta["date"] = val.isoformat(timespec="minutes") if isinstance(val, datetime) else str(val or "")
        elif label.startswith("inventarizuesi"):
            meta["counted_by"] = str(val or "")
        elif label.startswith("muaji"):
            meta["month_label"] = str(cells[0]).split(":", 1)[-1].strip() or str(val or "")
        elif label.startswith("kategoria"):
            meta["category"] = str(val or "")
        elif len(cells) == 1 and isinstance(cells[0], str) and "formular" not in label and not meta.get("place"):
            meta["place"] = cells[0].strip()
    return meta


def _month_of(meta):
    """"Tetor 2026" → "2026-10" (the month the count closes)."""
    t = str(meta.get("month_label") or "").lower()
    m = re.search(r"([a-zëç]+)\s+(\d{4})", t)
    if m and m.group(1) in MONTHS_SQ:
        return f"{m.group(2)}-{MONTHS_SQ[m.group(1)]:02d}"
    return None


def parse(data, sheet=None):
    """{sheet, sheets, meta, month, rows: [{item, description, category, unit, system, counted, comment, row}],
    warnings}. data: the bytes of an .xlsx file."""
    from openpyxl import load_workbook
    try:
        wb = load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    except Exception as exc:                                            # noqa: BLE001
        raise ValueError(f"The file could not be read as Excel (.xlsx): {str(exc)[:160]}") from None
    found = []
    for ws in wb.worksheets:
        top = []
        for i, row in enumerate(ws.iter_rows(min_row=1, max_row=40, values_only=True), start=1):
            h = _header(row)
            if h:
                found.append((ws.title, i, h, top))
                break
            top.append(row)
    if not found:
        raise ValueError("No sheet has the count table: a header row with Kartela (item number) and "
                         "GjendjeFizike (counted quantity).")
    pick = next((x for x in found if x[0] == sheet), found[0])
    title, hrow, h, top = pick
    ws = wb[title]
    rows, warnings, seen = [], [], {}
    for n, r in enumerate(ws.iter_rows(min_row=hrow + 1, values_only=True), start=hrow + 1):
        def cell(field):
            i = h.get(field)
            return r[i] if i is not None and i < len(r) else None
        item = cell("item")
        if item is None or str(item).strip() == "":
            continue
        if isinstance(item, float) and item.is_integer():
            item = int(item)
        item = str(item).strip().upper()
        raw = cell("counted")
        counted = None
        if raw not in (None, ""):
            try:
                counted = float(str(raw).replace(",", ".")) if isinstance(raw, str) else float(raw)
            except ValueError:
                warnings.append(f"Row {n}: the count of {item} is not a number ({str(raw)[:20]}); left as not counted.")
        system = cell("system")
        line = {"item": item, "description": str(cell("description") or "").strip(),
                "category": str(cell("category") or "").strip(), "unit": str(cell("unit") or "").strip().upper(),
                "system": _f(system) if system not in (None, "") else None, "counted": counted,
                "comment": str(cell("comment") or "").strip()[:300], "row": n}
        if item in seen:                         # the same item twice: the counts add up
            prev = rows[seen[item]]
            if counted is not None:
                prev["counted"] = (prev["counted"] or 0) + counted
            prev["comment"] = "; ".join(x for x in (prev["comment"], line["comment"]) if x)
            warnings.append(f"Row {n}: {item} is on the form more than once; the counts are added up.")
            continue
        seen[item] = len(rows)
        rows.append(line)
    meta = _meta(top)
    return {"sheet": title, "sheets": [x[0] for x in found], "meta": meta, "month": _month_of(meta),
            "rows": rows, "warnings": warnings[:50],
            "counted_lines": sum(1 for x in rows if x["counted"] is not None)}


# ---------------------------------------------------------------- uploaded counts
def _key(company, location, month):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{company}__{location}__{month}")


def save(company, location, month, parsed, filename, actor):
    DIR.mkdir(parents=True, exist_ok=True)
    c = {"company": company, "location": location, "month": month, "file": str(filename or "")[:160],
         "uploaded_by": actor, "uploaded_at": datetime.now().isoformat(timespec="seconds"), **parsed}
    with _lock:
        tmp = DIR / (_key(company, location, month) + ".json.tmp")
        tmp.write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
        tmp.replace(DIR / (_key(company, location, month) + ".json"))
        _results.pop((company, location, month), None)
    store.audit("stock_count", f"{company}|{location}|{month}", actor, None,
                {"file": c["file"], "lines": len(parsed["rows"]), "counted": parsed["counted_lines"]}, "count uploaded")
    return c


def get(company, location, month):
    try:
        return json.loads((DIR / (_key(company, location, month) + ".json")).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def delete(company, location, month, actor):
    p = DIR / (_key(company, location, month) + ".json")
    with _lock:
        ok = p.exists()
        if ok:
            p.replace(p.with_suffix(".removed"))         # kept on disk, out of the list
        _results.pop((company, location, month), None)
    if ok:
        store.audit("stock_count", f"{company}|{location}|{month}", actor, None, None, "count removed")
    return ok


def counts(company):
    """The counts uploaded for a company, newest month first (without their lines)."""
    out = []
    if not DIR.exists():
        return out
    for p in DIR.glob(_key(company, "", "").rstrip("_") + "__*.json"):
        try:
            c = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if c.get("company") != company:
            continue
        out.append({k: c.get(k) for k in ("location", "month", "file", "uploaded_by", "uploaded_at", "sheet",
                                          "counted_lines", "meta", "summary")} | {"lines": len(c.get("rows") or [])})
    out.sort(key=lambda x: (x["month"] or "", x["location"] or ""), reverse=True)
    return out


def _remember_summary(company, location, month, totals):
    p = DIR / (_key(company, location, month) + ".json")
    with _lock:
        try:
            c = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        c["summary"] = {k: totals.get(k) for k in ("net_value", "shortage_value", "surplus_value", "closing_value")}
        p.write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------- the valuation (Business Central)
ILE_FIELDS = ("Entry_No,Entry_Type,Item_No,Posting_Date,Document_No,Quantity,Remaining_Quantity,Cost_Amount_Actual,"
              "Cost_Amount_Expected")


def valuation(client, company, location, month, progress=None):
    """{item: {opening_qty, opening_value, in_qty, in_value, out_qty, out_value, closing_qty, closing_value,
    bc_qty}} of the location for the month, and the checks."""
    first, last = month_range(month)
    loc = location.replace("'", "''")
    say = progress or (lambda s: None)
    say("open entries")
    open_rows = client._get_all(client._url(company, client_entity(client)), page_size=2000, params={
        "$filter": f"Open eq true and Location_Code eq '{loc}'", "$select": ILE_FIELDS})
    say("entries of the month and after")
    moves = client._get_all(client._url(company, client_entity(client)), page_size=2000,
                            progress=lambda n: say(f"entries of the month and after: {n:,}"), params={
        "$filter": f"Posting_Date ge {first.isoformat()} and Location_Code eq '{loc}'", "$select": ILE_FIELDS})
    say("stock at the month end")
    bc_qty = client.stock_by_location(company, location, last.isoformat())
    items, month_moves = {}, {}

    def it(no):
        return items.setdefault(no, {"now_qty": 0.0, "now_value": 0.0, "after_qty": 0.0, "after_value": 0.0,
                                     "in_qty": 0.0, "in_value": 0.0, "out_qty": 0.0, "out_value": 0.0, "types": {}})
    for r in open_rows:
        q, rem = _f(r.get("Quantity")), _f(r.get("Remaining_Quantity"))
        if not q or not rem:
            continue
        cost = _f(r.get("Cost_Amount_Actual")) + _f(r.get("Cost_Amount_Expected"))
        x = it(str(r.get("Item_No")))
        x["now_qty"] += rem
        x["now_value"] += cost * rem / q
    end = last.isoformat()
    for r in moves:
        q = _f(r.get("Quantity"))
        v = _f(r.get("Cost_Amount_Actual")) + _f(r.get("Cost_Amount_Expected"))
        x = it(str(r.get("Item_No")))
        if str(r.get("Posting_Date") or "")[:10] > end:
            x["after_qty"] += q
            x["after_value"] += v
            continue
        # the month's entries of the item (to name what BC posted after the count form was printed)
        month_moves.setdefault(str(r.get("Item_No")), []).append(
            (int(r.get("Entry_No") or 0), str(r.get("Posting_Date") or "")[:10], etype(r.get("Entry_Type")),
             str(r.get("Document_No") or ""), round(q, 4)))
        way = "in" if q > 0 or (q == 0 and v > 0) else "out"
        x[way + "_qty"] += q
        x[way + "_value"] += v
        t = x["types"].setdefault((way, etype(r.get("Entry_Type"))), [0.0, 0.0])
        t[0] += q
        t[1] += v
    for no in bc_qty:
        it(no)
    out, off = {}, []
    for no, x in items.items():
        cq, cv = x["now_qty"] - x["after_qty"], x["now_value"] - x["after_value"]
        bq = bc_qty.get(no, 0.0)
        if abs(cq - bq) > 0.001:
            off.append((no, round(cq, 3), round(bq, 3)))
        row = {"closing_qty": round(bq, 4), "closing_value": round(cv, 2), "rebuilt_qty": round(cq, 4),
               "in_qty": round(x["in_qty"], 4), "in_value": round(x["in_value"], 2),
               "out_qty": round(x["out_qty"], 4), "out_value": round(x["out_value"], 2),
               # the month's entries by direction and BC entry type: [direction, type, quantity, value]
               "types": [[w, t, round(q, 4), round(v, 2)] for (w, t), (q, v) in sorted(x["types"].items())]}
        row["opening_qty"] = round(bq - x["in_qty"] - x["out_qty"], 4)
        row["opening_value"] = round(cv - x["in_value"] - x["out_value"], 2)
        if any(abs(row[k]) > TOL for k in ("closing_qty", "closing_value", "in_qty", "out_qty", "opening_qty")):
            out[no] = row
    return {"items": out, "moves": month_moves, "first": first.isoformat(), "last": last.isoformat(),
            "entries": {"open": len(open_rows), "moves": len(moves)},
            "qty_check": {"items": len(items), "off": len(off), "examples": off[:10]}}


def etype(t):
    """BC's entry type as it reads in BC ("Positive_x0020_Adjmt_x002E_" or "Positive_Adjmt" → "Positive Adjmt.")."""
    t = re.sub(r"_x([0-9A-Fa-f]{4})_", lambda m: chr(int(m.group(1), 16)), str(t or "")).replace("_", " ").strip()
    return {"Positive Adjmt": "Positive Adjmt.", "Negative Adjmt": "Negative Adjmt."}.get(t, t) or "(none)"


def client_entity(client):
    import config
    return config.E_ILE


# ---------------------------------------------------------------- the analysis
STATUSES = {"match": "Match", "short": "Shortage", "surplus": "Surplus", "not_counted": "Not counted",
            "not_on_form": "Not on the form", "unknown": "Not in Business Central",
            "cutoff": "Posted after the form", "late_match": "Match after later postings",
            "no_stock": "No stock"}
FLAGS = {"unit": "Unit differs", "form_system": "Form's system stock differs from BC",
         "cost": "Cost not from the month end"}


def _late(moves, delta):
    """The month's entries that explain BC's change since the form was printed (delta = BC closing − the form's
    system stock): the latest entries whose quantities add up to delta. (entries, exact)."""
    rows = sorted(moves or [], reverse=True)[:40]
    acc, took = 0.0, []
    for e in rows[:12]:
        acc += e[4]
        took.append(e)
        if abs(acc - delta) <= 0.001:
            return took, True
    return rows[:3], False


def analyse(val, count, master):
    """Lines and totals: BC's closing stock against the count, at the average cost of the month end.
    master: {item: item card row (Description, Item_Category_Code, Base_Unit_of_Measure, Unit_Cost)}."""
    V = val["items"]
    lines = []
    # Excel drops the leading zeros of a number typed in a cell (BC 007020 comes back as 7020): such a code is
    # matched to the one BC item that has it with zeros in front
    known = set(V) | set(master)
    zeros = {}
    for no in known:
        if no.isdigit() and no.startswith("0"):
            zeros.setdefault(no.lstrip("0"), []).append(no)
    form = {}
    for r in (count or {}).get("rows") or []:
        no = r["item"]
        if no not in known and no.isdigit() and len(zeros.get(no.lstrip("0"), [])) == 1:
            r = dict(r, item=zeros[no.lstrip("0")][0], form_item=no)
        form[r["item"]] = r

    def cost_of(no, v, card):
        if v and v["closing_qty"] > TOL and v["closing_value"] > 0:
            return v["closing_value"] / v["closing_qty"], "month end"
        if v and v["in_qty"] > TOL and v["in_value"] > 0:
            return v["in_value"] / v["in_qty"], "month's receipts"
        uc = _f((card or {}).get("Unit_Cost"))
        if uc > 0:
            return uc, "item card"
        return 0.0, "none"
    for no in sorted(set(V) | set(form)):
        v, f, card = V.get(no), form.get(no), master.get(no)
        bq = v["closing_qty"] if v else 0.0
        cost, src = cost_of(no, v, card)
        line = {"item": no, "description": (card or {}).get("Description") or (f or {}).get("description") or "",
                "category": (card or {}).get("Item_Category_Code") or "", "form_category": (f or {}).get("category") or "",
                "unit": (card or {}).get("Base_Unit_of_Measure") or "", "form_unit": (f or {}).get("unit") or "",
                "opening_qty": v["opening_qty"] if v else 0.0, "opening_value": v["opening_value"] if v else 0.0,
                "in_qty": v["in_qty"] if v else 0.0, "in_value": v["in_value"] if v else 0.0,
                "out_qty": v["out_qty"] if v else 0.0, "out_value": v["out_value"] if v else 0.0,
                "types": v["types"] if v else [],
                "bc_qty": bq, "bc_value": v["closing_value"] if v else 0.0,
                "cost": round(cost, 4), "cost_source": src, "form_system": (f or {}).get("system"),
                "counted": (f or {}).get("counted"), "comment": (f or {}).get("comment") or "",
                "form_item": (f or {}).get("form_item") or "", "flags": []}
        if not count:
            line["status"] = None
        elif not f:
            line["status"] = "not_on_form" if abs(bq) > TOL else "no_stock"
        elif not card and not v:
            line["status"] = "unknown"
        elif f["counted"] is None:
            # nothing counted and nothing in BC at the month end: no stock to count
            line["status"] = "not_counted" if abs(bq) > TOL else "no_stock"
        else:
            d = f["counted"] - bq
            line["status"] = "match" if abs(d) <= TOL else "short" if d < 0 else "surplus"
        if f and f.get("counted") is not None:
            line["diff_qty"] = round(f["counted"] - bq, 4)
            line["diff_value"] = round(line["diff_qty"] * cost, 2)
            line["counted_value"] = round(f["counted"] * cost, 2)
        else:
            line["diff_qty"] = line["diff_value"] = line["counted_value"] = None
        if f and line["unit"] and line["form_unit"] and _norm(line["unit"]) != _norm(line["form_unit"]):
            line["flags"].append("unit")
        if f and f.get("system") is not None and abs(f["system"] - bq) > 0.001:
            line["flags"].append("form_system")
            late, exact = _late((val.get("moves") or {}).get(no), bq - f["system"])
            line["late"] = [{"entry": e[0], "date": e[1], "type": e[2], "document": e[3], "qty": e[4]} for e in late]
            line["late_exact"] = exact
            # counted as the form says, and BC moved after the form was printed: a cut-off (a document dated in the
            # month, posted later, for goods that were still there, or not yet there, when they were counted)
            if f.get("counted") is not None and abs(f["counted"] - f["system"]) <= TOL and line["status"] != "match":
                line["status"] = "cutoff"
            # counted as BC holds it at the month end, not as the form said: documents dated in the month and
            # posted after the form was printed (a return to the supplier, a transfer) brought BC to the count
            elif line["status"] == "match" and line["late"]:
                line["status"] = "late_match"
        if src not in ("month end",) and (line["diff_qty"] or 0):
            line["flags"].append("cost")
        lines.append(line)
    tot = {"closing_value": round(sum(x["bc_value"] for x in lines), 2),
           "opening_value": round(sum(x["opening_value"] for x in lines), 2),
           "in_value": round(sum(x["in_value"] for x in lines), 2),
           "out_value": round(sum(x["out_value"] for x in lines), 2), "items": len(lines)}
    by_type = {}
    for x in lines:
        for w, t, q, v in x["types"]:
            by_type[(w, t)] = by_type.get((w, t), 0.0) + v
    tot["by_type"] = [[w, t, round(v, 2)] for (w, t), v in sorted(by_type.items(), key=lambda kv: -abs(kv[1]))]
    if count:
        cnt = [x for x in lines if x["diff_value"] is not None and x["status"] != "cutoff"]
        cut = [x for x in lines if x["status"] == "cutoff"]
        tot.update(
            cutoff_value=round(sum(x["diff_value"] for x in cut), 2),
            counted_value=round(sum(x["counted_value"] for x in cnt), 2),
            counted_bc_value=round(sum(x["bc_qty"] * x["cost"] for x in cnt), 2),
            net_value=round(sum(x["diff_value"] for x in cnt), 2),
            shortage_value=round(sum(x["diff_value"] for x in cnt if x["diff_value"] < 0), 2),
            surplus_value=round(sum(x["diff_value"] for x in cnt if x["diff_value"] > 0), 2),
            not_counted_value=round(sum(x["bc_value"] for x in lines if x["status"] in ("not_counted", "not_on_form")), 2),
            **{f"n_{k}": sum(1 for x in lines if x["status"] == k) for k in STATUSES},
            n_unit=sum(1 for x in lines if "unit" in x["flags"]),
            n_form_system=sum(1 for x in lines if "form_system" in x["flags"]),
            n_cost=sum(1 for x in lines if "cost" in x["flags"]))
        tot["net_pct"] = round(tot["net_value"] / tot["closing_value"] * 100, 2) if tot["closing_value"] else None
    cats = {}
    for x in lines:
        k = x["category"] or x["form_category"] or "(none)"
        c = cats.setdefault(k, {"category": k, "opening_value": 0.0, "in_value": 0.0, "out_value": 0.0,
                                "bc_value": 0.0, "counted_value": 0.0, "shortage": 0.0, "surplus": 0.0, "net": 0.0,
                                "lines": 0})
        c["bc_value"] += x["bc_value"]
        for f in ("opening_value", "in_value", "out_value"):
            c[f] += x[f]
        c["lines"] += 1
        if x["diff_value"] is not None:
            c["counted_value"] += x["counted_value"]
            if x["status"] == "cutoff":
                continue
            c["net"] += x["diff_value"]
            c["shortage" if x["diff_value"] < 0 else "surplus"] += x["diff_value"]
    lines.sort(key=lambda x: (x["status"] == "cutoff", -(abs(x["diff_value"]) if x["diff_value"] is not None else -1),
                              -abs(x["bc_value"])))
    return {"lines": lines, "totals": tot,
            "categories": sorted(({k: round(v, 2) if isinstance(v, float) else v for k, v in c.items()}
                                  for c in cats.values()), key=lambda c: c["net"])}


def build(client, company, location, month, progress=None):
    val = valuation(client, company, location, month, progress)
    first, last = month_range(month)
    (progress or (lambda s: None))("item cards")
    master = {str(r.get("No")): r for r in client.item_snapshot(company, last.isoformat())}
    count = get(company, location, month)
    a = analyse(val, count, master)
    val.pop("moves", None)
    if count:
        _remember_summary(company, location, month, a["totals"])
    return {"available": True, "company": company, "location": location, "month": month,
            "from": val["first"], "to": val["last"], "read_at": datetime.now().isoformat(timespec="seconds"),
            "entries": val["entries"], "qty_check": val["qty_check"],
            "count": {k: count.get(k) for k in ("file", "uploaded_by", "uploaded_at", "sheet", "sheets", "meta",
                                                "month", "warnings", "counted_lines")} | {"lines": len(count["rows"])}
            if count else None, **a}


def request(client, company, location, month, refresh=False):
    key = (company, location, month)
    with _lock:
        hit, job = _results.get(key), _jobs.get(key)
        if hit and not refresh and time.time() - hit[0] < KEEP and not (job and job["state"] == "running"):
            return hit[1]
        if job and job["state"] == "running":
            return {"pending": True, "stage": job["stage"]}
        if job and job["state"] == "failed" and not refresh and time.time() - job["at"] < 60:
            _jobs.pop(key, None)
            return {"available": False, "error": job["error"]}
        job = _jobs[key] = {"state": "running", "stage": "starting", "at": time.time(), "error": ""}

    def go():
        try:
            r = build(client, company, location, month, lambda s: job.update(stage=s))
            with _lock:
                _results[key] = (time.time(), r)
            job["state"] = "done"
        except Exception as exc:                                   # noqa: BLE001
            traceback.print_exc()
            job.update(state="failed", error=str(exc)[:300], at=time.time())
    threading.Thread(target=go, daemon=True, name=f"stockcount-{location}").start()
    return {"pending": True, "stage": "starting"}


def forget(company, location, month):
    with _lock:
        _results.pop((company, location, month), None)


# ---------------------------------------------------------------- Excel
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
    ws.title = "Count vs BC"
    sheet(ws, ["Item", "Description", "Category", "Unit (BC)", "Unit (form)", "Opening qty", "Increases qty",
               "Decreases qty", "BC closing qty", "Form system qty", "Counted", "Difference qty", "Average cost",
               "Cost from", "Opening value", "Increases value", "Decreases value", "BC closing value",
               "Difference value", "Status", "Checks", "BC entries after the form", "Comment"],
          [[x["item"], x["description"], x["category"] or x["form_category"], x["unit"], x["form_unit"],
            x["opening_qty"], x["in_qty"], x["out_qty"], x["bc_qty"], x["form_system"], x["counted"], x["diff_qty"],
            x["cost"], x["cost_source"], x["opening_value"], x["in_value"], x["out_value"], x["bc_value"],
            x["diff_value"], STATUSES.get(x["status"], ""), ", ".join(FLAGS.get(f, f) for f in x["flags"]),
            "; ".join(f"{e['type']} {e['document']} {e['date']} {e['qty']:g}" for e in x.get("late") or []),
            x["comment"]] for x in r["lines"]],
          [12, 40, 16, 9, 9, 12, 12, 12, 12, 12, 11, 12, 12, 14, 14, 14, 14, 15, 14, 18, 14, 40, 40])
    v = wb.create_sheet("Inventory valuation")
    sheet(v, ["Item", "Description", "Category", "Unit", "Opening qty", "Opening value", "Increases qty",
              "Increases value", "Decreases qty", "Decreases value", "Closing qty", "Closing value", "Average cost"],
          [[x["item"], x["description"], x["category"], x["unit"], x["opening_qty"], x["opening_value"], x["in_qty"],
            x["in_value"], x["out_qty"], x["out_value"], x["bc_qty"], x["bc_value"], x["cost"]]
           for x in sorted(r["lines"], key=lambda y: y["item"]) if x["status"] != "unknown"],
          [12, 40, 16, 9, 12, 14, 12, 14, 12, 14, 12, 14, 12])
    m = wb.create_sheet("Movements by type")
    sheet(m, ["Item", "Description", "Unit", "Direction", "BC entry type", "Quantity", "Value"],
          [[x["item"], x["description"], x["unit"], "Increase" if w == "in" else "Decrease", t, q, v]
           for x in sorted(r["lines"], key=lambda y: y["item"]) for w, t, q, v in x.get("types") or []],
          [12, 40, 9, 11, 20, 12, 14])
    c = wb.create_sheet("By category")
    sheet(c, ["Category", "Lines", "Opening value", "Increases value", "Decreases value", "BC closing value",
              "Counted value", "Shortages", "Surpluses", "Net difference"],
          [[x["category"], x["lines"], x.get("opening_value"), x.get("in_value"), x.get("out_value"), x["bc_value"],
            x["counted_value"], x["shortage"], x["surplus"], x["net"]] for x in r["categories"]],
          [24, 8, 16, 16, 16, 16, 16, 14, 14, 16])
    rd = wb.create_sheet("Read me")
    for t in (f"Stock count analysis · {r['company']} · {r['location']} · {r['from']} to {r['to']}",
              f"Read from Business Central at {r['read_at']} (read only).",
              "BC closing qty = Inventory by date at the month end (item card, location filter).",
              "Opening + increases + decreases = BC closing (decreases are negative): the month's Inventory Valuation of "
              "the location. Increases and decreases are the month's item ledger entries with a positive and a negative "
              "quantity, at cost; Movements by type splits them by BC entry type (purchase, transfer, sale, consumption, "
              "adjustments).",
              "Average cost = closing value / closing qty; closing value = remaining cost of the open item ledger "
              "entries (actual + expected) less the entries posted after the month end.",
              "Where the closing quantity is not above zero: the cost of the month's receipts, then the item card's "
              "Unit Cost (column Cost from).",
              "Difference = counted − BC closing qty; value = difference × average cost. A blank count is not counted, "
              "not zero.",
              "Posted after the form: counted = the form's system stock (Gjendje Finance), but BC changed after the form "
              "was printed with documents dated in the month (named in BC entries after the form). These lines are "
              "kept out of the shortages, surpluses and net difference: check the cut-off of those documents.",
              "Match after later postings: counted = BC closing, but not the form's system stock: BC was brought to the "
              "count by documents dated in the month and posted after the form was printed (a return to the supplier, "
              "a transfer; named in BC entries after the form). Nothing is missing; the form was printed before them.",
              "No stock: on the form or in the month's entries, zero in BC at the month end and nothing counted."):
        rd.append([t])
    rd.column_dimensions["A"].width = 120
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def decode(b64):
    try:
        data = base64.b64decode(b64 or "", validate=True)
    except (ValueError, TypeError):
        raise ValueError("The file could not be read.") from None
    if not data.startswith(b"PK"):
        raise ValueError("Upload the count form as an Excel file (.xlsx).")
    if len(data) > 12_000_000:
        raise ValueError("The file is larger than 12 MB.")
    return data
