"""Sales → Menu prices: the active price of every menu item at every Simphony location, by revenue centre and
price level, with its menu item name and groups; on screen with filters and as an Excel file.

Source: Simphony Reporting and Analytics, per location: getMenuItemPrices (the active prices: no "effective to"
date, or one still to come; and once more from three years back, for the items whose prices have all ended, the
inactive ones) and getMenuItemDimensions (names, major and family groups). Each is kept for an hour (simphony.py).
Simphony is only read.

The tax % of each item (QA 21 Nov 2026): as Simphony charged it on the sales of the last 31 days held (its tax class
is not in the reporting API), and the VAT % of the BC item the POS import posts it to (VAT Prod. Posting Group).
"""
import io
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import locintel
import menueng
import recipes
import simphony
import store

_lock = threading.Lock()
_held = {"at": 0, "data": None}
KEEP = 3600


def _read(refresh=False):
    with _lock:
        if _held["data"] and not refresh and time.time() - _held["at"] < KEEP:
            return _held["data"]
    locs = [l for l in simphony.locations() if locintel.in_use(l)]
    reg = {r["ref"]: r for r in locintel.registry()}
    rvc_names, errors = {}, []
    fallback = (store.get_settings().get("group") or {}).get("reporting_currency") or "ALL"

    today = date.today()
    a, b = (today - timedelta(days=32)).isoformat(), (today - timedelta(days=1)).isoformat()

    def one(l):
        ref = l["locRef"]
        prices = simphony.menu_item_prices(ref, 0 if refresh else 3600)
        items = simphony.menu_items(ref, 0 if refresh else 6 * 3600)
        try:
            rvcs = simphony.rvc_dims(ref)
        except Exception:                                          # noqa: BLE001
            rvcs = {}
        try:
            hist = simphony.menu_item_prices(ref, 0 if refresh else 3600, history=True)
        except Exception as exc:                                   # noqa: BLE001
            hist, rvcs["_err"] = None, str(exc)[:120]
        try:
            rates = menueng.tax_rates(ref, a, b)
        except Exception:                                          # noqa: BLE001
            rates = {}
        return ref, prices, items, rvcs, hist, rates
    rows, cards, maps = [], {}, {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = [(l, pool.submit(one, l)) for l in locs]
        for l, f in futs:
            try:
                ref, prices, items, rvcs, hist, rates = f.result()
            except Exception as exc:                               # noqa: BLE001
                errors.append(f"{l['locRef']}: {str(exc)[:160]}")
                continue
            if rvcs.get("_err"):
                errors.append(f"{l['locRef']} (ended prices): {rvcs.pop('_err')}")
            rg = reg.get(ref) or {}
            loc_name = rg.get("name") or l.get("name") or ref
            cur = rg.get("currency") or locintel.norm_currency(l.get("currency")) or fallback
            rvc_names[ref] = rvcs
            co = rg.get("company") or ""
            if co not in cards:
                cards[co] = ((recipes.master_cached(co) or {}).get("items") or {}) if co else {}
                maps[co] = menueng.bc_map_held(co) if co else {}
            active = {p["num"] for p in prices}
            # an item with no active price at the location: its prices have all ended (or start later)
            gone = {}
            for p in hist or []:
                if p["num"] in active:
                    continue
                k = (p["num"], p["rvc"], p["level"])
                if k not in gone or (p["to"] or "9999") > (gone[k]["to"] or "9999"):
                    gone[k] = p
            for p, status in [(p, "active") for p in prices] + [(p, "inactive") for p in gone.values()]:
                it = items.get(p["num"]) or {}
                price, cost = p["price"], p["cost"]
                bc_item = maps[co].get(str(p["num"])) or ""
                card = cards[co].get(bc_item) if bc_item else None
                if status == "inactive" and p["from"] and p["from"] > today.isoformat():
                    status = "future"
                rows.append({
                    "ref": ref, "location": loc_name, "brand": rg.get("brand") or "", "company": rg.get("company") or "",
                    "group": rg.get("price_group") or locintel.price_group(rg) if rg else "",
                    "currency": cur, "num": p["num"], "name": it.get("name") or f"Item {p['num']}",
                    "name2": it.get("name2") or "", "major": it.get("major") or "", "family": it.get("family") or "",
                    "excluded": bool(it.get("excluded")),
                    "rvc": p["rvc"], "rvc_name": (rvcs.get(int(p["rvc"])) if p["rvc"] is not None else "") or "",
                    "level": p["level"], "level_name": p["level_name"] or (f"Level {p['level']}" if p["level"] else ""),
                    "price": price, "cost": cost,
                    # cost as a share of the price as Simphony holds it (menu prices usually include VAT)
                    "cost_pct": round(cost / price * 100, 1) if price and cost else None,
                    "from": p["from"], "to": p["to"], "status": status,
                    # the tax % Simphony charged on the item's sales (last 31 days held), the VAT % of its BC item
                    "tax_sim": rates.get(p["num"]), "bc_item": bc_item,
                    "vat_bc": menueng.vat_pct(card) if card else None})
    rows.sort(key=lambda r: (r["location"], r["major"], r["family"], r["name"], r["rvc"] or 0, r["level"] or 0))
    data = {"rows": rows, "errors": errors, "read_at": time.strftime("%Y-%m-%d %H:%M"),
            "locations": sorted({(r["ref"], r["location"]) for r in rows}, key=lambda x: x[1])}
    with _lock:
        _held.update(at=time.time(), data=data)
    return data


STATUSES = {"active": "Active", "inactive": "Inactive (prices ended)", "future": "Not active yet", "all": "All"}


def request(refs=None, location="", level="", group="", q="", limit=600, refresh=False, status="active", item=None):
    """The price list, filtered. refs: the Simphony locations the person may see (None = all). status: active
    (default), inactive (every price of the item at the location has ended), future, or all. item: one menu item
    number, every price of it (the item's pop-up on the screen)."""
    d = _read(refresh)
    status = status if status in STATUSES else "active"
    every = [r for r in d["rows"] if refs is None or r["ref"].upper() in refs]
    counts = Counter(r.get("status", "active") for r in every)
    rows = [r for r in every if status == "all" or r.get("status", "active") == status]
    opts = {"locations": [{"ref": a, "name": b} for a, b in d["locations"] if refs is None or a.upper() in refs],
            "levels": sorted({r["level_name"] for r in rows if r["level_name"]}),
            "groups": sorted({r["major"] for r in rows if r["major"]})}
    ql = (q or "").strip().lower()
    locs = {x.strip() for x in (location or "").split(",") if x.strip()}      # one location or several
    out = [r for r in rows
           if (not locs or r["ref"] in locs) and (not level or r["level_name"] == level)
           and (not group or r["major"] == group)
           and (not ql or ql in r["name"].lower() or ql in r["name2"].lower() or ql == str(r["num"])
                or ql in r["family"].lower())
           and (item is None or r["num"] == item)]
    # the comparison holds the active prices only, whatever the status filter
    spread = _spread([r for r in out if r.get("status", "active") == "active"])
    items = _items(out)
    tax = [x for x in items if x["tax_diff"]]
    cap = (limit or 0) and max(1, limit // 2)
    return {"rows": out[:limit] if limit else out, "count": len(out), "total": len(rows),
            # one row per menu item for the screen (Geri, 22 Nov 2026: less scrolling), its prices in a pop-up
            "item_rows": items[:cap] if cap else items, "items_n": len(items), "tax_items": tax[:200], "tax_n": len(tax),
            "items": len({r["num"] for r in out}), "locations_n": len({r["ref"] for r in out}),
            "spread": spread[:60], "spread_all": spread, "spread_n": len(spread), "options": opts, "read_at": d["read_at"], "errors": d["errors"],
            "statuses": STATUSES, "status_counts": dict(counts),
            "filters": {"location": location, "level": level, "group": group, "q": q, "status": status}}


# a price of 1 or less is not a price: a placeholder or an open price keyed at the till (SALT's wines at 1.00 against
# 12,000 at LIFT showed a gap of 1,199,900%, QA 14 Nov 2026)
PLACEHOLDER = 1.0


def _real(p):
    return p is not None and p > PLACEHOLDER


def _base_price(rs):
    """The price of an item at a location for the matrix: the lowest price level with a real price, the most common
    price across revenue centres."""
    lv = min((r["level"] for r in rs if r["level"] is not None and _real(r["price"])), default=None)
    ps = [r["price"] for r in rs if r["level"] == lv and _real(r["price"])]
    return Counter(ps).most_common(1)[0][0] if ps else None


def _items(rows):
    """One row per menu item: its locations, the lowest and highest base price (in the main currency), the tax %
    Simphony charged and the VAT % of its BC item (the most common), whether they differ anywhere, its statuses."""
    by = {}
    for r in rows:
        by.setdefault(r["num"], []).append(r)
    main = Counter(r["currency"] for r in rows).most_common(1)[0][0] if rows else ""
    out = []
    for num, rs in by.items():
        locs = {}
        for r in rs:
            locs.setdefault(r["ref"], []).append(r)
        base = [p for p in (_base_price(v) for v in locs.values() if v[0]["currency"] == main) if p]
        ts = Counter(r["tax_sim"] for r in rs if r.get("tax_sim") is not None).most_common(1)
        vb = Counter(r["vat_bc"] for r in rs if r.get("vat_bc") is not None).most_common(1)
        out.append({"num": num, "name": Counter(r["name"] for r in rs).most_common(1)[0][0], "major": rs[0]["major"],
                    "family": rs[0]["family"], "locations": len(locs), "prices": len(rs), "currency": main,
                    "low": min(base) if base else None, "high": max(base) if base else None,
                    "tax_sim": ts[0][0] if ts else None, "vat_bc": vb[0][0] if vb else None,
                    "tax_diff": any(r.get("tax_sim") is not None and r.get("vat_bc") is not None
                                    and abs(r["tax_sim"] - r["vat_bc"]) >= 0.5 for r in rs),
                    "statuses": sorted({r.get("status", "active") for r in rs}),
                    "excluded": all(r.get("excluded") for r in rs)})
    out.sort(key=lambda x: (x["major"], x["family"], x["name"]))
    return out


def _matrix(rows):
    by = {}
    for r in rows:
        by.setdefault(r["num"], {}).setdefault(r["ref"], []).append(r)
    return by


def norm_name(s):
    """A menu item name for matching: lower case, letters and digits only, single spaces."""
    return " ".join(re.sub(r"[^0-9a-z]+", " ", str(s or "").lower()).split())


def _spread(rows):
    """The same product sold at more than one location of a brand at different base prices (same currency), widest
    gap first: the same price group (the brand; an airport unit apart: locintel.price_group), the same menu item
    number (PLU) and the same name, so that two products sharing a number across brands or renamed at one location
    are not taken for one (QA 21 Nov 2026). Prices of 1 or less are left out (placeholders, open prices)."""
    by = {}
    for r in rows:
        g = r.get("group") or r.get("brand") or ""
        by.setdefault((g, r["num"], norm_name(r["name"])), {}).setdefault(r["ref"], []).append(r)
    out = []
    for (brand, num, _n), locs in by.items():
        by_cur = {}
        for ref, rs in locs.items():
            p = _base_price(rs)
            if p:
                by_cur.setdefault(rs[0]["currency"], []).append((p, rs[0]["location"]))
        for cur, ps in by_cur.items():
            if len(ps) < 2:
                continue
            lo, hi = min(ps), max(ps)
            if hi[0] > lo[0]:
                any_r = next(iter(locs.values()))[0]
                out.append({"brand": brand, "num": num, "name": any_r["name"], "currency": cur, "low": lo[0],
                            "low_at": lo[1], "high": hi[0], "high_at": hi[1],
                            "gap_pct": round((hi[0] - lo[0]) / lo[0] * 100, 1), "locations": len(ps),
                            "prices": sorted(ps)})
    out.sort(key=lambda x: -x["gap_pct"])
    return out


def render_xlsx(r, pdf=False):
    """The price list as Excel. pdf=True (Export → PDF): a workbook a page can hold — the items priced differently
    across locations, then the prices with the columns that matter; the matrix by location (a column per location)
    stays in the Excel file, too wide for a page (Geri, 14 Nov 2026: the PDF did not work)."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    fill, font = PatternFill("solid", fgColor="E4F3F9"), Font(bold=True, color="0E2733")

    def head(ws, cols, widths):
        ws.append(cols)
        for c in ws[ws.max_row]:
            c.fill, c.font = fill, font
            c.alignment = Alignment(wrap_text=True, vertical="top")
        for i, w in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(i)].width = w

    if pdf:
        ws = wb.active
        ws.title = "Same item, different prices"
        head(ws, ["Brand", "Item no.", "Item", "Lowest", "at", "Highest", "at", "Gap %", "Locations"],
             [12, 10, 32, 11, 20, 11, 20, 9, 9])
        for x in r.get("spread") or []:
            ws.append([x.get("brand") or "", x["num"], x["name"], x["low"], x["low_at"], x["high"], x["high_at"],
                       x["gap_pct"] / 100, x["locations"]])
        for row in ws.iter_rows(min_row=2):
            row[1].number_format = "0"
            row[3].number_format = row[5].number_format = "#,##0.00"
            row[7].number_format = "0.0%"
        ws = wb.create_sheet("Price list")
        head(ws, ["Location", "Item no.", "Item", "Major group", "Revenue centre", "Price level", "Price", "Cost % of price",
                  "Tax % Simphony", "VAT % BC item", "Status"],
             [20, 9, 30, 16, 14, 12, 10, 8, 8, 8, 9])
        for x in sorted(r["rows"], key=lambda x: (x["location"], x["name"], x["rvc_name"] or "", x["level"] or 0)):
            ws.append([x["location"], x["num"], x["name"], x["major"], x["rvc_name"], x["level_name"], x["price"],
                       x["cost_pct"] / 100 if x.get("cost_pct") is not None else None,
                       x.get("tax_sim"), x.get("vat_bc"), (r.get("statuses") or {}).get(x.get("status"), x.get("status"))])
        for row in ws.iter_rows(min_row=2):
            row[1].number_format = "0"
            row[6].number_format = "#,##0.00"
            row[7].number_format = "0.0%"
        ws = wb.create_sheet("Read me")
        f = r["filters"]
        for line in ["Menu prices from Simphony, read " + r["read_at"] + ".",
                     "Filters: " + (", ".join(f"{k} {v}" for k, v in f.items() if v) or "none") + ".",
                     "The PDF holds the items priced differently across locations and the prices (the first pages of a "
                     "long list); Export → Excel has every price and the matrix by location.",
                     "A price of 1 or less is a placeholder or an open price and is left out of the comparison."]:
            ws.append([line])
        ws.column_dimensions["A"].width = 140
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue()

    rows = r["rows"]
    ws = wb.active
    ws.title = "Price list"
    head(ws, ["Location", "Simphony ref", "Brand", "Currency", "Item no.", "Item", "Second name", "Major group",
              "Family group", "Revenue centre", "RVC no.", "Price level", "Level no.", "Price", "Cost", "Cost % of price",
              "Effective from", "Effective to", "Not in sales", "Status", "Tax % Simphony", "BC item", "VAT % BC item",
              "Tax differs"],
         [22, 12, 12, 8, 10, 32, 20, 18, 18, 16, 7, 14, 7, 11, 10, 9, 12, 12, 9, 12, 9, 11, 9, 8])
    st = r.get("statuses") or {}
    for x in rows:
        ts, vb = x.get("tax_sim"), x.get("vat_bc")
        ws.append([x["location"], x["ref"], x["brand"], x["currency"], x["num"], x["name"], x["name2"], x["major"],
                   x["family"], x["rvc_name"], x["rvc"], x["level_name"], x["level"], x["price"], x["cost"],
                   x["cost_pct"], x["from"], x["to"], "yes" if x["excluded"] else "", st.get(x.get("status"), x.get("status")),
                   ts, x.get("bc_item") or "", vb, "yes" if ts is not None and vb is not None and abs(ts - vb) >= 0.5 else ""])
    for row in ws.iter_rows(min_row=2, min_col=14, max_col=15):
        for c in row:
            c.number_format = "#,##0.00"
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions

    ws = wb.create_sheet("Same item, different prices")
    head(ws, ["Brand", "Item no.", "Item", "Currency", "Lowest", "at", "Highest", "at", "Gap %", "Locations", "Prices"],
         [12, 10, 32, 8, 11, 20, 11, 20, 8, 9, 60])
    for x in r.get("spread_all") or r.get("spread") or []:
        ws.append([x.get("brand") or "", x["num"], x["name"], x["currency"], x["low"], x["low_at"], x["high"],
                   x["high_at"], x["gap_pct"] / 100, x["locations"],
                   "; ".join(f"{loc} {p:,.2f}" for p, loc in x.get("prices") or [])])
    for row in ws.iter_rows(min_row=2):
        row[4].number_format = row[6].number_format = "#,##0.00"
        row[8].number_format = "0.0%"
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions

    # one row per item, a column per location: the base price (lowest price level, most common across RVCs)
    rows = [x for x in rows if x.get("status", "active") == "active"]
    ws = wb.create_sheet("By location")
    locs = sorted({(x["ref"], x["location"], x["currency"]) for x in rows}, key=lambda t: t[1])
    main_cur = Counter(c for _, _, c in locs).most_common(1)[0][0] if locs else ""
    head(ws, ["Item no.", "Item", "Major group", "Family group"] + [f"{n} ({c})" if c else n for _, n, c in locs]
         + [f"Lowest ({main_cur})", f"Highest ({main_cur})", "Gap %"], [10, 32, 18, 18] + [13] * len(locs) + [11, 11, 8])
    mx = _matrix(rows)
    items = sorted(mx, key=lambda n: (next(iter(mx[n].values()))[0]["major"], next(iter(mx[n].values()))[0]["name"]))
    for num in items:
        first = next(iter(mx[num].values()))[0]
        vals = [_base_price(mx[num][ref]) if ref in mx[num] else None for ref, _, _ in locs]
        # lowest and highest among the locations in the main currency (prices in other currencies do not compare)
        got = [v for (ref, _, c), v in zip(locs, vals) if v and c == main_cur]
        lo, hi = (min(got), max(got)) if got else (None, None)
        ws.append([num, first["name"], first["major"], first["family"]] + vals
                  + [lo, hi, round((hi - lo) / lo * 100, 1) if lo and hi and len(got) > 1 else None])
    for row in ws.iter_rows(min_row=2, min_col=5, max_col=4 + len(locs) + 2):
        for c in row:
            c.number_format = "#,##0.00"
    ws.freeze_panes = "E2"
    ws.auto_filter.ref = ws.dimensions

    ws = wb.create_sheet("Read me")
    f = r["filters"]
    for line in [
        "Menu prices from Simphony, read " + r["read_at"] + ".",
        "Filters: " + (", ".join(f"{k} {v}" for k, v in f.items() if v) or "none") + ".",
        "Price list: every active price (no end date, or one still to come) of every menu item, by location, "
        "revenue centre and price level, with the cost Simphony holds and that cost as a share of the price. Menu "
        "prices are as set in Simphony, which usually includes VAT.",
        "By location: one row per item, the base price at each location (the lowest price level; where revenue "
        "centres differ, the most common price), and the gap between the lowest and the highest location. Prices "
        "are in each location's currency. A price of 1 or less is a placeholder or an open price and is left out.",
        "Not in sales: items Simphony marks as not included in sales.",
        "Status: Active = the item has a price in force at the location; Inactive = every price it had there in the "
        "last three years has ended (no longer sold); Not active yet = its only price starts later. The filter on the "
        "screen chooses which are listed; the comparisons hold the active prices only.",
        "Tax % Simphony: the tax Simphony charged on the item's sales of the last 31 days (its VAT over its sales "
        "without VAT, before discounts); empty when the item did not sell. VAT % BC item: the VAT Prod. Posting Group "
        "of the BC item the POS import posts the menu item to. Tax differs: the two are not the same.",
        "Same item, different prices: the same product at more than one location of a brand at a different base "
        "price: same brand, same item number (PLU) and same name. An item number shared by two brands, or renamed "
        "at one location, is not taken for the same product.",
    ]:
        ws.append([line])
    ws.column_dimensions["A"].width = 140
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
