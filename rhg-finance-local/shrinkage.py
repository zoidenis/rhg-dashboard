"""Inventory → Shrinkage KPIs: the physical count differences of a location, month by month, by category, against
the category's waste allowance, in value; the automation of the Excel "Analize Inventari … KPIs" (SIN, 2026).

Everything is read from Business Central, read-only (due diligence: Claude Doc "Shrinkage KPIs — due diligence dhe
plani i automatizimit", 10 Nov 2026; decisions of Geri the same day):
  * the count of the 1st of a month is posted as an item adjustment dated the last day of the month before, its
    document number starting with "IF" ("IF 01.10.2026", "IFAMB 01/10/2026"…): its quantity is the count
    difference of each item, its cost amount the difference in value (the cost BC posted);
  * in (Hyrje) = the month's other entries with a positive quantity, out (Dalje) = with a negative one; the other
    adjustments (staff consumption, declared waste "FIRO", corrections that cancel each other) are netted per item
    and month and go in or out by their sign: this gives the Excel's figures item by item;
  * opening = BC's stock at the end of the month before, counted = BC's stock at the month end (the count is
    posted); theoretical = opening + in + out (out is negative); difference = counted − theoretical = the count;
  * KMSH / Dalje = the cost of the month's outs, as posted; Diferenca / Dalje = −difference value ÷ KMSH / Dalje;
  * a category is "Investigate" when Diferenca / Dalje is above its % Waste; the unjustified value is shown both
    ways: the Excel's (the whole loss of a category above its threshold) and the recommended one (loss − % Waste ×
    KMSH / Dalje, never below zero);
  * the category of an item comes from its card (Item Category Group 4 and 5, Group 2 for PN) through a map kept
    here, with exceptions per item; an item the map does not reach takes its card's own group in Business Central
    (Group 4, else Group 3, 2, the item category code), with no % Waste until one is set (Geri, 10 Nov 2026).
Months are kept on disk once read (data/shrinkage/); the recent ones are read again.
"""
import io
import json
import re
import threading
import time
import traceback
from datetime import date, datetime, timedelta

import config
import heldjob
import stockcount
import store
from stockcount import etype, month_range

DIR = store.DATA_DIR / "shrinkage"
CFG_FILE = store.DATA_DIR / "shrinkage.json"
_held = heldjob.Held("shrinkage", 900)
_lock = threading.Lock()
ADJ = {"Positive Adjmt.", "Negative Adjmt."}
UNITS = ("KG", "LITER", "COPE", "PAKO")
NO_BC = "(no category in Business Central)"
STATUSES = ("Pilot", "Reconciled", "Approved")
RECENT_DAYS = 62                       # a month that ended within this is read again; older ones are kept

# The waste table of the Excel (sheet "Firot % Category"), as it was on 10 Nov 2026; Peshk 55% kept by decision.
DEFAULT_CATEGORIES = [
    ("Peshk", "Kuzhine", 55, True, "Peshk i fresket/deti: humbje nga prerja, prishja, afati i shkurter"),
    ("Mish", "Kuzhine", 5, True, "Mish i ngrire/fresket: shkrirje, trim, skadim"),
    ("Proshute", "Kuzhine", 5, True, "Proshute: prerje, kore/skaje, thatesire"),
    ("Bulmet", "Kuzhine", 5, True, "Qumesht, veze, gjalpe, akullore: skadim, fuqi e shkurter"),
    ("Djathe", "Kuzhine", 5, False, "Djathe: prerje/cirkulim, njomje; humbje e ulet nese ruhet mire"),
    ("Perime", "Kuzhine", 10, True, "Perime/sallata gjethore: prishja e shpejte; objektivi 5-10%"),
    ("Fruta", "Fruta", 10, False, "Fruta te fresketa: pjekja/prishja, trim; objektivi 5-10%"),
    ("Kuzhine", "Kuzhine", 5, True, "Kuzhine e pergjithshme (vaj, thate, ndihmese): humbje trim/skadim"),
    ("Pasta", "Kuzhine", 5, False, "Produkt i thate, afat i gjate; humbje vetem nga gatimi/porcionimi"),
    ("Ereza", "Kuzhine", 5, False, "Erezat: fuqi e gjate, humbje shume e vogel"),
    ("Buke", "Kuzhine", 10, False, "Buke e fresket ditore: mbetjet e dites; objektivi 5-8%"),
    ("Sushi", "Kuzhine", 5, True, "Peshk i paperpunuar/sushi: afat shume i shkurter, standard HACCP"),
    ("PN", "PN", 5, False, "Prodhim i brendshem (p.sh. granola): batch i tepert / skadim"),
    ("Bar", "Bar", 100, False, "Artikuj te perzier bar (akull, uje, personeli): humbje minimale"),
    ("White Wine", "Bar", 5, True, "Shishe: thyerje, tapa, vere e hapur per gote (oksidim)"),
    ("Red Wine", "Bar", 5, True, "Me e qendrueshme se e bardha; thyerje/tapa"),
    ("Rose Wine", "Bar", 5, True, "Shishe: thyerje, vere e hapur per gote"),
    ("Sparkling", "Bar", 5, True, "Shishe e hapur humb gazin shpejt; thyerje"),
    ("Soft Drinks", "Bar", 5, True, "Produkt i paketuar, jetegjatesi e gjate; thyerje/skadim"),
    ("Birre", "Bar", 5, True, "Shishe/qelq 1%; fuci draft (Estrella 30L) 3-5%, mesatare 2%"),
    ("Vodka", "Bar", 5, True, "Spirits: derdhje/porcionim; objektivi bar 1-2%"),
    ("Gin", "Bar", 5, True, "Spirits: derdhje/porcionim; objektivi bar 1-2%"),
    ("Rum", "Bar", 5, True, "Spirits: derdhje/porcionim; objektivi bar 1-2%"),
    ("Tequila", "Bar", 5, True, "Spirits: derdhje/porcionim, perdorim ne kokteje"),
    ("Whiskey", "Bar", 5, True, "Spirits premium: kontroll i rrept, humbje e ulet"),
    ("Coganc", "Bar", 5, True, "Spirits premium: kontroll i rrept, humbje e ulet"),
    ("Liker", "Bar", 5, True, "Likere: derdhje/porcionim; kristalizim shishe e hapur"),
    ("Amaro", "Bar", 5, True, "Amaro/digestiv: konsum i ngadalshem, humbje e ulet"),
    ("Vermouth", "Bar", 5, True, "I fortifikuar por oksidohet pas hapjes; rrotullim i ngadalte"),
    ("Monin", "Bar", 5, True, "Shurupe: derdhje; afat i gjate"),
    ("Kafeteria", "Bar", 5, False, "Kafe, qumesht barista: derdhje, skadim"),
    ("Cigare", "Bar", 5, True, "Cigare: pa prishje, vetem rreziku i lageshtise/humbjes"),
]
# Item card groups → category (due diligence, section "Kategorizimi nga BC"); the most specific row wins.
DEFAULT_MAP = [
    ("", "PESHK&PROD.DETI", "", "Peshk"), ("", "MISH&SHPENDE", "", "Mish"),
    ("", "BULMET&DJATHE", "DJATHE", "Djathe"), ("", "BULMET&DJATHE", "", "Bulmet"),
    ("", "PERIME&FRUTE", "FRESH.FRUIT", "Fruta"), ("", "PERIME&FRUTE", "", "Perime"),
    ("", "PROD TE THATA", "DRITHERA", "Pasta"), ("", "PROD TE THATA", "", "Kuzhine"),
    ("", "PROD BUKE&FURRE", "", "Buke"),
    ("", "VERE", "WHITE", "White Wine"), ("", "VERE", "RED", "Red Wine"), ("", "VERE", "ROSE", "Rose Wine"),
    ("", "VERE", "SPARKLING", "Sparkling"), ("", "ALKOOL I FORTE", "SPARKLING", "Sparkling"),
    ("", "ALKOOL I FORTE", "VODKA", "Vodka"), ("", "ALKOOL I FORTE", "GIN", "Gin"), ("", "ALKOOL I FORTE", "RUM", "Rum"),
    ("", "ALKOOL I FORTE", "TEQUILA", "Tequila"), ("", "ALKOOL I FORTE", "WHISKY", "Whiskey"),
    ("", "ALKOOL I FORTE", "COGNAC", "Coganc"), ("", "ALKOOL I FORTE", "LIQUEURS", "Liker"),
    ("", "ALKOOL I FORTE", "APERITIFS", "Amaro"), ("", "BIRRE", "", "Birre"),
    ("", "PIJE JO ALKOOLIKE", "SHURUPE&PURE", "Monin"), ("", "PIJE JO ALKOOLIKE", "KAFE&CAJ", "Kafeteria"),
    ("", "PIJE JO ALKOOLIKE", "", "Soft Drinks"), ("", "MAT.KONS.BARI", "", "Bar"), ("", "TABAKO", "", "Cigare"),
    ("PN", "", "", "PN"),
]


def _f(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _norm(s):
    return re.sub(r"\s+", " ", str(s or "").strip()).upper()


# ---------------------------------------------------------------- the settings (categories, map, exceptions)
def _default_config():
    return {"categories": [{"kat2": k2, "kat1": k1, "waste_pct": w, "check": c, "basis": b}
                           for k2, k1, w, c, b in DEFAULT_CATEGORIES],
            "map": [{"g2": a, "g4": b, "g5": c, "kat2": k} for a, b, c, k in DEFAULT_MAP],
            "overrides": {}, "prefixes": ["IF"], "status": "Pilot", "history": [],
            "updated_at": None, "updated_by": None}


def get_config():
    try:
        c = json.loads(CFG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        c = {}
    d = _default_config()
    d.update({k: v for k, v in c.items() if k in d})
    return d


def _write(c):
    CFG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CFG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(c, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(CFG_FILE)


def _clean_categories(rows):
    out, seen = [], set()
    for r in rows or []:
        k2 = str(r.get("kat2") or "").strip()[:60]
        if not k2 or k2.upper() in seen:
            continue
        seen.add(k2.upper())
        try:
            w = float(r.get("waste_pct"))
        except (TypeError, ValueError):
            raise ValueError(f"% Waste of {k2} is not a number.") from None
        if not 0 <= w <= 100:
            raise ValueError(f"% Waste of {k2} must be between 0 and 100.")
        out.append({"kat2": k2, "kat1": str(r.get("kat1") or "").strip()[:60], "waste_pct": round(w, 4),
                    "check": bool(r.get("check")), "basis": str(r.get("basis") or "").strip()[:300]})
    return out


def save_config(company, patch, actor, note=""):
    """patch: categories, map, prefixes, overrides (of the company: {item: category or ""}), status."""
    with _lock:
        c = get_config()
        before = {k: c.get(k) for k in ("status",)}
        changed = []
        if "categories" in patch:
            c["categories"] = _clean_categories(patch["categories"])
            changed.append("categories")
        names = {x["kat2"].upper(): x["kat2"] for x in c["categories"]}
        if "map" in patch:
            rows = []
            for r in patch["map"] or []:
                k = names.get(str(r.get("kat2") or "").strip().upper())
                g = [_norm(r.get(f)) for f in ("g2", "g4", "g5")]
                if k and any(g):
                    rows.append({"g2": g[0], "g4": g[1], "g5": g[2], "kat2": k})
            c["map"] = rows
            changed.append("map")
        if "prefixes" in patch:
            p = [_norm(x) for x in (patch["prefixes"] if isinstance(patch["prefixes"], list)
                                    else str(patch["prefixes"]).split(",")) if _norm(x)]
            if not p:
                raise ValueError("Give at least one document prefix of the count adjustments (e.g. IF).")
            c["prefixes"] = p[:20]
            changed.append("prefixes")
        if "overrides" in patch:
            ov = c["overrides"].setdefault(company, {})
            for item, k in (patch["overrides"] or {}).items():
                item = str(item).strip()[:40]
                if not item:
                    continue
                if not k:
                    ov.pop(item, None)
                    continue
                k = names.get(str(k).strip().upper())
                if not k:
                    raise ValueError(f"{item}: choose one of the categories.")
                ov[item] = {"kat2": k, "by": actor, "at": datetime.now().isoformat(timespec="seconds"),
                            "source": "screen"}
            changed.append("exceptions")
        if "status" in patch:
            st = str(patch["status"])
            if st not in STATUSES:
                raise ValueError("Status: Pilot, Reconciled or Approved.")
            if st != c["status"] and st != "Pilot" and not (note or "").strip():
                raise ValueError(f"Write a note to mark the KPIs {st}.")
            c["status"] = st
            changed.append("status")
        if not changed:
            return c
        c["updated_at"], c["updated_by"] = datetime.now().isoformat(timespec="seconds"), actor
        c["history"] = ([{"at": c["updated_at"], "by": actor, "company": company, "changed": changed,
                          "status": c["status"], "note": str(note or "")[:300]}] + c["history"])[:200]
        _write(c)
    store.audit("shrinkage_config", company, actor, before, {"status": c["status"], "changed": changed}, note or "")
    _held.forget()
    return c


def _header(row):
    return [re.sub(r"[^a-z0-9%]+", "", str(v or "").lower()) for v in row]


def import_excel(company, data, actor, items):
    """The Excel analysis: the categories of its items (a sheet with Kartela + Kat. 2) become exceptions where they
    differ from the map, and its waste table (Kat. 2 + % Waste + CHECK) the categories. items: the item cards."""
    from openpyxl import load_workbook
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:                                        # noqa: BLE001
        raise ValueError("The file could not be read as an Excel workbook.") from exc
    cats, item_kat, kat1_of = None, {}, {}
    for ws in wb.worksheets:
        rows = ws.iter_rows(values_only=True)
        for n, row in enumerate(rows):
            if n > 30:
                break
            h = _header(row)
            iw = next((i for i, x in enumerate(h) if "waste" in x), None)
            if "kat2" in h and iw is not None and "check" in h and cats is None:
                ik, ic = h.index("kat2"), h.index("check")
                i1 = h.index("kat1") if "kat1" in h else None
                ib = h.index("bazaevleresimit") if "bazaevleresimit" in h else None
                cats = []
                for r in rows:
                    k2 = str(r[ik] or "").strip() if ik < len(r) else ""
                    if not k2 or k2 in ("0", "#N/A"):
                        continue
                    w = _f(r[iw]) if iw < len(r) else 0
                    cats.append({"kat2": k2, "kat1": str(r[i1] or "").strip() if i1 is not None else "",
                                 "waste_pct": round(w * 100 if w <= 1 else w, 4),
                                 "check": str(r[ic] or "").strip().upper() == "YES",
                                 "basis": str(r[ib] or "").strip() if ib is not None and ib < len(r) else ""})
                break
            if "kartela" in h and "kat2" in h and not item_kat:
                ii, ik = h.index("kartela"), h.index("kat2")
                i1 = h.index("kat1") if "kat1" in h else None
                for r in rows:
                    no = str(r[ii] or "").strip() if ii < len(r) else ""
                    k2 = str(r[ik] or "").strip() if ik < len(r) else ""
                    if no and k2 and k2 not in ("0", "#N/A"):
                        item_kat[no] = k2
                        if i1 is not None and r[i1] and str(r[i1]).strip() not in ("#N/A", "0"):
                            kat1_of.setdefault(k2.upper(), str(r[i1]).strip())
                break
    if cats is None and not item_kat:
        raise ValueError("No sheet with Kartela + Kat. 2, or with Kat. 2 + % Waste + CHECK, was found.")
    c = get_config()
    if cats:
        for x in cats:                                              # "#N/A" Kat. 1 in the waste table
            if not x["kat1"] or x["kat1"] in ("#N/A", "0"):
                x["kat1"] = kat1_of.get(x["kat2"].upper(), "")
        known = {x["kat2"].upper() for x in cats}
        for k2 in {v for v in item_kat.values()}:                  # categories used by items, missing in the table
            if k2.upper() not in known and k2.upper() not in {x["kat2"].upper() for x in c["categories"]}:
                cats.append({"kat2": k2, "kat1": kat1_of.get(k2.upper(), ""), "waste_pct": 5, "check": False,
                             "basis": "added by the import: not in the waste table"})
                known.add(k2.upper())
        save_config(company, {"categories": cats}, actor, "Excel import: waste table")
        c = get_config()
    names = {x["kat2"].upper(): x["kat2"] for x in c["categories"]}
    ov, same, missing = {}, 0, []
    for no, k2 in item_kat.items():
        k = names.get(k2.upper())
        if not k:
            missing.append(no)
            continue
        card = items.get(no)
        if card is None:
            missing.append(no)
            continue
        if category(card, c, company, overrides=False) == k:
            same += 1
            continue
        ov[no] = k
    with _lock:
        c = get_config()
        cur = c["overrides"].setdefault(company, {})
        now = datetime.now().isoformat(timespec="seconds")
        for no, k in ov.items():
            cur[no] = {"kat2": k, "by": actor, "at": now, "source": "Excel import"}
        c["updated_at"], c["updated_by"] = now, actor
        c["history"] = ([{"at": now, "by": actor, "company": company, "changed": ["exceptions"],
                          "status": c["status"], "note": f"Excel import: {len(ov)} exceptions"}] + c["history"])[:200]
        _write(c)
    store.audit("shrinkage_config", company, actor, None, {"import": len(ov)}, "Excel import")
    _held.forget()
    return {"items": len(item_kat), "same_as_map": same, "exceptions": len(ov), "not_found": missing[:50],
            "n_not_found": len(missing), "categories": len(cats or [])}


def category(card, cfg, company, overrides=True, item=None):
    """The category of an item card: its exception, else the most specific map row; None = out of scope."""
    if overrides and item:
        o = (cfg["overrides"].get(company) or {}).get(item)
        if o:
            return o["kat2"]
    g = {"g2": _norm(card.get("Item_Category_Group_2")), "g4": _norm(card.get("Item_Category_Group_4")),
         "g5": _norm(card.get("Item_Category_Group_5"))}
    best, score = None, -1
    for r in cfg["map"]:
        keys = [k for k in ("g2", "g4", "g5") if r.get(k)]
        if keys and all(g[k] == r[k] for k in keys) and len(keys) > score:
            best, score = r["kat2"], len(keys)
    return best


def bc_group(card):
    """An item the map does not reach: its card's own category in BC (Group 4, else 3, 2, the category code)."""
    for f in ("Item_Category_Group_4", "Item_Category_Group_3", "Item_Category_Group_2", "Item_Category_Code"):
        v = str(card.get(f) or "").strip()
        if v:
            return v
    return NO_BC


def category_of(card, cfg, company, item):
    """(category, from): from = "exception", "map" or "bc" (the card's own group, the map not reaching it)."""
    if (cfg["overrides"].get(company) or {}).get(item):
        return cfg["overrides"][company][item]["kat2"], "exception"
    k = category(card, cfg, company, overrides=False)
    return (k, "map") if k else (bc_group(card), "bc")


# ---------------------------------------------------------------- reading Business Central
_items_cache = {}
ITEM_SELECT = ("No,Description,Base_Unit_of_Measure,Item_Category_Code,Item_Category_Group_2,Item_Category_Group_3,"
               "Item_Category_Group_4,Item_Category_Group_5,Unit_Cost")


def item_cards(client, company, max_age=6 * 3600):
    hit = _items_cache.get(company)
    if hit and time.time() - hit[0] < max_age:
        return hit[1]
    rows = client._get_all(client._url(company, config.E_ITEMS), {"$select": ITEM_SELECT}, page_size=5000)
    cards = {str(r.get("No")): r for r in rows if r.get("No")}
    _items_cache[company] = (time.time(), cards)
    return cards


def _month_file(company, location, ym):
    return DIR / (re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{company}__{location}__{ym}") + ".json")


def _is_count(et, doc, prefixes):
    d = _norm(doc)
    return et in ADJ and any(d.startswith(p) for p in prefixes)


def _cls(et, doc, q):
    d = _norm(doc)
    if et in ADJ:
        return "staff" if "KONSUM STAF" in d else "waste" if d.startswith("FIRO") else "adjustment"
    return {"Purchase": "purchase", "Sale": "sale", "Consumption": "consumption", "Assembly Consumption": "consumption",
            "Output": "output", "Assembly Output": "output"}.get(et, "transfer_in" if et == "Transfer" and q > 0
                                                                  else "transfer_out" if et == "Transfer" else "other")


def read_month(client, company, location, ym, prefixes, refresh=False, say=lambda s: None):
    """One month of one location, per item: [in_q, in_v, out_q, out_v, count_q, count_v, {class: [q, v]}], the
    stock at the month end before and at its end, and the count documents. Kept on disk."""
    first, last = month_range(ym)
    p = _month_file(company, location, ym)
    try:
        old = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        old = None
    recent = (date.today() - last).days < RECENT_DAYS
    if old and old.get("prefixes") == prefixes and not refresh and (not recent or time.time() - old.get("at", 0) < 6 * 3600):
        return old
    loc = location.replace("'", "''")
    say(f"{location} {ym}: entries")
    rows = client._get_all(client._url(company, config.E_ILE), page_size=2000,
                           progress=lambda n: say(f"{location} {ym}: {n:,} entries"), params={
        "$filter": f"Location_Code eq '{loc}' and Posting_Date ge {first.isoformat()} and "
                   f"Posting_Date le {last.isoformat()}",
        "$select": "Entry_Type,Item_No,Document_No,Quantity,Cost_Amount_Actual,Cost_Amount_Expected"})
    items, docs = {}, {}
    for r in rows:
        no = str(r.get("Item_No") or "")
        if not no:
            continue
        et, doc, q = etype(r.get("Entry_Type")), str(r.get("Document_No") or ""), _f(r.get("Quantity"))
        v = _f(r.get("Cost_Amount_Actual")) + _f(r.get("Cost_Amount_Expected"))
        x = items.setdefault(no, {"pos": [0.0, 0.0], "neg": [0.0, 0.0], "adj": [0.0, 0.0], "cnt": [0.0, 0.0], "cls": {}})
        if _is_count(et, doc, prefixes):
            x["cnt"][0] += q
            x["cnt"][1] += v
            docs[doc] = docs.get(doc, 0) + 1
            continue
        c = _cls(et, doc, q)
        t = x["cls"].setdefault(c, [0.0, 0.0])
        t[0] += q
        t[1] += v
        if et in ADJ:
            x["adj"][0] += q
            x["adj"][1] += v
        elif q > 0 or (q == 0 and v > 0):
            x["pos"][0] += q
            x["pos"][1] += v
        else:
            x["neg"][0] += q
            x["neg"][1] += v
    out = {}
    for no, x in items.items():
        iq, iv, oq, ov = x["pos"][0], x["pos"][1], x["neg"][0], x["neg"][1]
        aq, av = x["adj"]
        if aq > 1e-9 or (abs(aq) <= 1e-9 and av > 0):              # other adjustments, netted: in or out
            iq, iv = iq + aq, iv + av
        else:
            oq, ov = oq + aq, ov + av
        out[no] = [round(iq, 4), round(iv, 2), round(oq, 4), round(ov, 2), round(x["cnt"][0], 4),
                   round(x["cnt"][1], 2), {k: [round(a, 4), round(b, 2)] for k, (a, b) in x["cls"].items()}]
    say(f"{location} {ym}: stock at the month ends")
    opening = client.stock_by_location(company, location, (first - timedelta(days=1)).isoformat(), 86400)
    closing = client.stock_by_location(company, location, last.isoformat(), 900 if recent else 86400)
    m = {"company": company, "location": location, "month": ym, "prefixes": prefixes, "at": time.time(),
         "read_at": datetime.now().isoformat(timespec="seconds"), "entries": len(rows), "items": out,
         "opening": {k: round(v, 4) for k, v in opening.items()}, "closing": {k: round(v, 4) for k, v in closing.items()},
         "docs": dict(sorted(docs.items(), key=lambda kv: -kv[1]))}
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)
    return m


def months_of(year, today=None):
    today = today or date.today()
    return [f"{year}-{m:02d}" for m in range(1, 13) if date(year, m, 1) <= today]


# ---------------------------------------------------------------- the KPIs
def _cat_rows(lines, cfg):
    """lines: [{kat2, unit, out_q, out_v, cnt_q, cnt_v}] in scope → one row per category + the totals."""
    cats = {x["kat2"]: x for x in cfg["categories"]}
    acc = {}
    for ln in lines:
        a = acc.setdefault(ln["kat2"], {"u": {}, "kmsh": 0.0, "net": 0.0, "loss_items": 0.0, "gain_items": 0.0,
                                          "items": 0, "kat1": ln.get("kat1_bc") or ""})
        u = ln["unit"] if ln["unit"] in UNITS else "Other"
        b = a["u"].setdefault(u, [0.0, 0.0, 0.0])              # count qty, out qty (positive), value
        b[0] += ln["cnt_q"]
        b[1] += -ln["out_q"]
        b[2] += ln["cnt_v"]
        a["kmsh"] += -ln["out_v"]
        a["net"] += ln["cnt_v"]
        a["loss_items" if ln["cnt_v"] < 0 else "gain_items"] += ln["cnt_v"]
        a["items"] += 1
    rows = []
    for k2, a in acc.items():
        c = cats.get(k2) or {"kat1": a["kat1"], "waste_pct": 0, "check": False, "from_bc": True}
        dq, oq = sum(b[0] for b in a["u"].values()), sum(b[1] for b in a["u"].values())
        loss = max(0.0, -a["net"])
        ratio_signed = -a["net"] / a["kmsh"] if a["kmsh"] > 0 else None
        waste = c["waste_pct"] / 100
        investigate = ratio_signed is not None and ratio_signed > waste or (a["kmsh"] <= 0 and loss > 0)
        allowed = waste * a["kmsh"]
        rows.append({"kat2": k2, "kat1": c["kat1"], "check": c["check"], "waste_pct": c["waste_pct"], "items": a["items"],
                     "from_bc": bool(c.get("from_bc")),
                     "delta": {u: (round(b[0] / b[1], 4) if b[1] else None) for u, b in a["u"].items()},
                     "delta_all": round(dq / oq, 4) if oq else None,
                     "value": {u: round(b[2], 2) for u, b in a["u"].items()}, "net": round(a["net"], 2),
                     "losses": round(a["loss_items"], 2), "surpluses": round(a["gain_items"], 2),
                     "kmsh": round(a["kmsh"], 2),
                     "ratio": None if ratio_signed is None else round(ratio_signed, 4),
                     "target": "Investigate" if investigate else "OK", "allowed": round(allowed, 2),
                     "unjust": round(max(0.0, loss - allowed), 2),
                     "unjust_excel": round(loss if investigate else 0.0, 2)})
    rows.sort(key=lambda r: r["net"])
    return rows


def _totals(rows):
    t = {k: round(sum(r[k] for r in rows), 2) for k in ("net", "losses", "surpluses", "kmsh", "allowed", "unjust",
                                                         "unjust_excel")}
    yes = [r for r in rows if r["check"]]
    t["unjust_yes"] = round(sum(r["unjust"] for r in yes), 2)
    t["unjust_excel_yes"] = round(sum(r["unjust_excel"] for r in yes), 2)
    t["ratio"] = round(-t["net"] / t["kmsh"], 4) if t["kmsh"] else None
    t["investigate"] = sum(1 for r in rows if r["target"] == "Investigate")
    return t


def form_counts(form, known):
    """The uploaded count form of a month (Inventory → Stock count analysis, the same file): {item: counted or None
    (left blank)}; a code Excel stripped of its leading zeros is matched to the one BC item that has them."""
    zeros = {}
    for no in known:
        if no.isdigit() and no.startswith("0"):
            zeros.setdefault(no.lstrip("0"), []).append(no)
    out = {}
    for r in form.get("rows") or []:
        no = r["item"]
        if no not in known and no.isdigit() and len(zeros.get(no.lstrip("0"), [])) == 1:
            no = zeros[no.lstrip("0")][0]
        out[no] = r["counted"]
    return out


def _unit_cost(iq, iv, oq, ov, cq, cv, card):
    """KMSH of an item in a month, from what BC posted: the cost of its outs, else of its ins, else of the count
    adjustment, else the item card's Unit Cost."""
    if oq < -1e-9 and ov < 0:
        return ov / oq
    if iq > 1e-9 and iv > 0:
        return iv / iq
    if abs(cq) > 1e-9 and cv:
        return cv / cq
    return _f(card.get("Unit_Cost"))


def build(client, company, location, year, refresh=False, progress=lambda s: None):
    """A location's year. The count of a month is the count form uploaded for it when there is one (theoretical =
    the count before, or BC's stock, + in − out; difference = counted − theoretical, at KMSH), else the count
    adjustments posted in BC (documents starting with IF)."""
    cfg = get_config()
    progress("reading the item cards")
    cards = item_cards(client, company)
    yms = months_of(year)
    months, items, prev = [], {}, None
    for ym in yms:
        m = read_month(client, company, location, ym, cfg["prefixes"], refresh, progress)
        form = stockcount.get(company, location, ym)
        fc = form_counts(form, set(cards)) if form else None
        src = "form" if fc is not None else ("bc" if m["docs"] else None)
        info = {"month": ym, "counted": bool(src), "source": src, "docs": m["docs"], "read_at": m["read_at"],
                "entries": m["entries"]}
        if form:
            info["form"] = {k: form.get(k) for k in ("file", "uploaded_by", "uploaded_at", "sheet", "sheets",
                                                     "counted_lines")} | {"lines": len(form.get("rows") or [])}
        seen = set(m["items"]) | {k for k, v in m["opening"].items() if v} | {k for k, v in m["closing"].items() if v}
        if fc:
            seen |= set(fc)
        off = nc = mismatch = 0
        nc_value = 0.0
        for no in seen:
            iq, iv, oq, ov, cq, cv, cls = m["items"].get(no) or [0, 0, 0, 0, 0, 0, {}]
            op = prev[no] if prev is not None and no in prev else m["opening"].get(no, 0.0)
            flag = ""
            if src == "form":
                theo = op + iq + oq
                c = fc.get(no)
                if c is None:                       # not on the form, or left blank: no difference, said apart
                    dq, dv, cl = 0.0, 0.0, theo
                    if abs(theo) > 1e-6:
                        nc += 1
                        nc_value += theo * _unit_cost(iq, iv, oq, ov, cq, cv, cards.get(no) or {})
                        flag = "not counted"
                else:
                    uc = _unit_cost(iq, iv, oq, ov, cq, cv, cards.get(no) or {})
                    dq, cl = c - theo, c
                    dv = dq * uc
                    if m["docs"] and abs(dq - cq) > 0.01:
                        mismatch += 1
            else:
                dq, dv, cl = cq, cv, m["closing"].get(no, 0.0)
                if src == "bc" and abs(op + iq + oq + cq - cl) > 0.01:
                    off += 1
            it = items.setdefault(no, {})
            it[ym] = [round(op, 4), iq, iv, oq, ov, round(dq, 4), round(dv, 2), round(cl, 4), cls, flag]
        info.update(qty_off=off, not_counted=nc, not_counted_value=round(nc_value, 2), posted_mismatch=mismatch)
        months.append(info)
        prev = {k: v for k, v in fc.items() if v is not None} if fc else None
    # category per item; months with a count only feed the KPIs
    lines_by_month, item_rows = {}, []
    for no, per in items.items():
        card = cards.get(no) or {}
        k2, src = category_of(card, cfg, company, no)
        unit = _norm(card.get("Base_Unit_of_Measure"))
        item_rows.append({"no": no, "desc": (card.get("Description") or "").strip(), "unit": unit,
                          "kat2": k2, "from": src, "g4": card.get("Item_Category_Group_4") or "",
                          "g5": card.get("Item_Category_Group_5") or "", "exception": src == "exception",
                          "m": {ym: v[:8] for ym, v in per.items()}, "cls": {ym: v[8] for ym, v in per.items() if v[8]},
                          "not_counted": [ym for ym, v in per.items() if v[9]]})
        kat1 = str(card.get("Item_Category_Group_3") or "").strip() if src == "bc" else ""
        for ym, v in per.items():
            lines_by_month.setdefault(ym, []).append({"kat2": k2, "kat1_bc": kat1, "unit": unit, "out_q": v[3],
                                                      "out_v": v[4], "cnt_q": v[5], "cnt_v": v[6]})
    per_month = {}
    for mm in months:
        ym = mm["month"]
        rows = _cat_rows(lines_by_month.get(ym, []), cfg) if mm["counted"] else []
        per_month[ym] = {"rows": rows, "totals": _totals(rows) if rows else None}
    counted = [m["month"] for m in months if m["counted"]]
    ytd_lines = [ln for ym in counted for ln in lines_by_month.get(ym, [])]
    ytd_rows = _cat_rows(ytd_lines, cfg)
    for r in ytd_rows:                                  # the thresholds are monthly: sum the months' unjustified
        r["unjust"] = round(sum(x["unjust"] for ym in counted for x in per_month[ym]["rows"] if x["kat2"] == r["kat2"]), 2)
        r["unjust_excel"] = round(sum(x["unjust_excel"] for ym in counted for x in per_month[ym]["rows"]
                                      if x["kat2"] == r["kat2"]), 2)
        r["months_investigate"] = sum(1 for ym in counted for x in per_month[ym]["rows"]
                                      if x["kat2"] == r["kat2"] and x["target"] == "Investigate")
    progress("putting it together")
    return {"available": True, "company": company, "location": location, "year": year,
            "read_at": datetime.now().isoformat(timespec="seconds"), "months": months, "per_month": per_month,
            "ytd": {"rows": ytd_rows, "totals": _totals(ytd_rows) if ytd_rows else None, "months": counted},
            "items": sorted(item_rows, key=lambda r: r["no"]),
            "config": {k: cfg[k] for k in ("categories", "status", "prefixes", "updated_at", "updated_by")},
            "notes": {
                "difference": "Difference = counted − theoretical, where the month's count form was uploaded "
                              "(theoretical = the count before, or BC's stock, + in − out; value at the item's KMSH: "
                              "the posted cost of its outs, else of its ins, else the card's Unit Cost); else the count "
                              "adjustment posted in Business Central (document starting with "
                              + ", ".join(cfg["prefixes"]) + "), quantity and cost as posted.",
                "in_out": "In and out = the month's other entries of the location by sign; other adjustments (staff "
                          "consumption, FIRO, corrections) netted per item.",
                "kmsh": "KMSH / Dalje = the cost of the month's outs, as posted.",
                "unjust": "Unjustified (recommended) = loss − % Waste × KMSH / Dalje, never below zero. Unjustified "
                          "(Excel) = the whole loss of a category whose Diferenca / Dalje is above its % Waste."}}


def request(client, company, location, year, refresh=False):
    return _held.request(f"{company}|{location}|{year}", lambda p: build(client, company, location, year, refresh, p),
                         refresh, note="Reading the month's entries of the location from Business Central…")


# ---------------------------------------------------------------- every location: the count adjustments only
def overview(client, company, ym):
    """Every location's count adjustments of a month (one read of the count entries of the company), and the
    analysis already kept for it."""
    cfg = get_config()
    first, last = month_range(ym)
    pre = " or ".join(f"startswith(Document_No,'{p}')" for p in cfg["prefixes"])
    rows = client._get_all(client._url(company, config.E_ILE), page_size=5000, params={
        "$filter": f"Posting_Date ge {first.isoformat()} and Posting_Date le {last.isoformat()} and ({pre}) and "
                   f"(Entry_Type eq 'Positive Adjmt.' or Entry_Type eq 'Negative Adjmt.')",
        "$select": "Location_Code,Item_No,Document_No,Quantity,Cost_Amount_Actual,Cost_Amount_Expected"})
    locs = {}
    for r in rows:
        no, loc = str(r.get("Item_No") or ""), str(r.get("Location_Code") or "")
        if not _is_count("Positive Adjmt.", r.get("Document_No"), cfg["prefixes"]):
            continue
        v = _f(r.get("Cost_Amount_Actual")) + _f(r.get("Cost_Amount_Expected"))
        x = locs.setdefault(loc, {"location": loc, "net": 0.0, "losses": 0.0, "surpluses": 0.0, "lines": 0,
                                  "items": set(), "docs": {}})
        x["docs"][r.get("Document_No")] = x["docs"].get(r.get("Document_No"), 0) + 1
        x["items"].add(no)
        x["lines"] += 1
        x["net"] += v
        x["losses" if v < 0 else "surpluses"] += v
    out = []
    for loc, x in locs.items():
        kept = None
        try:
            m = json.loads(_month_file(company, loc, ym).read_text(encoding="utf-8"))
            kept = m.get("read_at")
        except (OSError, ValueError):
            pass
        x["items"] = len(x["items"])
        out.append({**{k: round(v, 2) if isinstance(v, float) else v for k, v in x.items()},
                    "docs": sorted(x["docs"], key=lambda d: -x["docs"][d])[:6], "analysis_read_at": kept})
    forms = {c["location"]: c for c in stockcount.counts(company) if c.get("month") == ym}
    for x in out:
        x["form"] = bool(forms.get(x["location"]))
    for loc, c in forms.items():
        if loc not in locs:
            out.append({"location": loc, "net": None, "losses": None, "surpluses": None, "lines": None, "items": None,
                        "docs": [], "analysis_read_at": None, "form": True})
    out.sort(key=lambda x: (x["net"] is None, x["net"] or 0))
    return {"available": True, "company": company, "month": ym, "locations": out,
            "read_at": datetime.now().isoformat(timespec="seconds"), "prefixes": cfg["prefixes"]}


_ov_held = heldjob.Held("shrinkage_overview", 1800)


def request_overview(client, company, ym, refresh=False):
    return _ov_held.request(f"{company}|{ym}", lambda p: overview(client, company, ym), refresh,
                            note="Reading the count adjustments of every location…")


# ---------------------------------------------------------------- Excel
MONTHS_SQ = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def render_xlsx(r):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    fill, bold = PatternFill("solid", fgColor="E4F3F9"), Font(bold=True)

    def sheet(ws, header, rows, widths, top=None):
        if top:
            ws.append([top])
            ws["A1"].font = Font(bold=True, size=13)
            ws.append([])
        ws.append(header)
        hr = ws.max_row
        for c in ws[hr]:
            c.fill, c.font = fill, bold
        for row in rows:
            ws.append(row)
        for i, w in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.freeze_panes = ws.cell(hr + 1, 1)
        if rows:
            ws.auto_filter.ref = f"A{hr}:{get_column_letter(len(header))}{ws.max_row}"

    def label(ym):
        return f"{MONTHS_SQ[int(ym[5:7]) - 1]} {ym[:4]}"

    head = ["CHECK", "% Waste", "Kat. 1", "Kat. 2", "Items"] + [f"% Delta {u}" for u in UNITS] + ["% Delta All"] + \
           [f"Value {u}" for u in UNITS] + ["Value Other", "Net value", "KMSH / Dalje", "Diferenca / Dalje",
                                            "TARGET", "Allowed waste", "Unjustified (loss − allowed)",
                                            "Unjustified (Excel method)"]

    def catrow(x):
        return (["YES" if x["check"] else "NO", x["waste_pct"] / 100, x["kat1"], x["kat2"], x["items"]] +
                [x["delta"].get(u) for u in UNITS] + [x["delta_all"]] + [x["value"].get(u) for u in UNITS] +
                [x["value"].get("Other"), x["net"], x["kmsh"], x["ratio"], x["target"], x["allowed"], x["unjust"],
                 x["unjust_excel"]])
    ws = wb.active
    ws.title = f"KPI Summary {r['year']}"
    counted = [m["month"] for m in r["months"] if m["counted"]]
    cats = sorted({x["kat2"] for ym in counted for x in r["per_month"][ym]["rows"]})
    sh = ["Kat. 2"] + [f"{label(ym)} net" for ym in counted] + ["YTD net"] + \
         [f"{label(ym)} unjustified" for ym in counted] + ["YTD unjustified"]
    srows = []
    for k in cats:
        nets = [next((x["net"] for x in r["per_month"][ym]["rows"] if x["kat2"] == k), 0) for ym in counted]
        uns = [next((x["unjust"] for x in r["per_month"][ym]["rows"] if x["kat2"] == k), 0) for ym in counted]
        srows.append([k] + nets + [round(sum(nets), 2)] + uns + [round(sum(uns), 2)])
    sheet(ws, sh, srows, [18] + [14] * (len(sh) - 1),
          f"Shrinkage KPIs · {r['company']} · {r['location']} · {r['year']} · read {r['read_at']} · "
          f"status {r['config']['status']}")
    src = {m["month"]: (f"count form {m['form']['file']}" if m.get("source") == "form"
                        else "count adjustments posted in BC") for m in r["months"] if m["counted"]}
    for ym in reversed(counted):
        pm = r["per_month"][ym]
        w = wb.create_sheet(f"KPI {label(ym).replace(' ', '')}")
        sheet(w, head, [catrow(x) for x in pm["rows"]], [7, 8, 10, 14, 6] + [9] * 5 + [12] * 5 + [12, 13, 11, 12, 12, 14, 14],
              f"KPI {label(ym)} · {r['location']} · {src[ym]} · net {pm['totals']['net']:,.0f} · unjustified "
              f"{pm['totals']['unjust']:,.0f} (Excel method {pm['totals']['unjust_excel']:,.0f})")
        for row in w.iter_rows(min_row=4):
            for c in row[5:10]:
                c.number_format = "0.0%"
            row[1].number_format = "0%"
            row[20].number_format = "0.0%"
    a = wb.create_sheet("Analize")
    ah = ["Kartela", "Pershkrimi", "Njesia", "Kat. 2", "Group 4", "Group 5", "Category from"]
    for ym in r["months"]:
        L = label(ym["month"])
        ah += [f"{L} Fillim", f"{L} Hyrje", f"{L} Dalje", f"{L} Teorike", f"{L} Fizike", f"{L} Diference",
               f"{L} KMSH", f"{L} Vlere"]
    arows = []
    for it in r["items"]:
        row = [it["no"], it["desc"], it["unit"], it["kat2"], it["g4"], it["g5"],
               {"exception": "exception", "map": "map", "bc": "BC group (not in the map)"}.get(it.get("from"), "")]
        for ym in r["months"]:
            v = it["m"].get(ym["month"])
            if not v:
                row += [None] * 8
                continue
            op, iq, iv, oq, ov, cq, cv, cl = v
            unit_cost = (cv / cq) if cq else ((ov / oq) if oq else None)
            row += [op, iq, -oq, round(op + iq + oq, 4), cl, cq, round(unit_cost, 4) if unit_cost else None, cv]
        arows.append(row)
    sheet(a, ah, arows, [11, 34, 8, 14, 16, 14, 9] + [10] * (len(ah) - 7))
    f = wb.create_sheet("Firot % Category")
    sheet(f, ["CHECK", "Kat. 1", "Kat. 2", "% Waste", "Baza e vleresimit"],
          [["YES" if c["check"] else "NO", c["kat1"], c["kat2"], c["waste_pct"] / 100, c["basis"]]
           for c in r["config"]["categories"]], [8, 12, 16, 9, 80])
    rd = wb.create_sheet("Read me")
    for t in [f"Shrinkage KPIs · {r['company']} · {r['location']} · {r['year']}, read from Business Central "
              f"{r['read_at']} (read only). Status of the categories and waste table: {r['config']['status']}."] + \
             list(r["notes"].values()) + \
             ["A month is in the KPIs only once its count is posted (a count document in the month).",
              "Teorike = Fillim + Hyrje − Dalje; Diference = Fizike − Teorike = the count adjustment.",
              "% Delta = count difference ÷ Dalje in quantity, per base unit; Diferenca / Dalje = −net value ÷ KMSH.",
              "An item the category map does not reach takes its card's own group in Business Central (Group 4, "
              "else Group 3, 2, the item category code): such a category has no % Waste until one is set in "
              "Categories & waste, so all of its loss counts as unjustified, and it is not in CHECK = YES."]:
        rd.append([t])
    rd.column_dimensions["A"].width = 130
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
