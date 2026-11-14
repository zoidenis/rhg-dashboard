"""Sales → Menu prices: the active price of every menu item at every Simphony location, by revenue centre and
price level, with its menu item name and groups; on screen with filters and as an Excel file.

Source: Simphony Reporting and Analytics, two calls per location: getMenuItemPrices (the active prices: no
"effective to" date, or one still to come) and getMenuItemDimensions (names, major and family groups). Each is kept
for an hour (simphony.py). Simphony is only read.
"""
import io
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import locintel
import simphony
import store

_lock = threading.Lock()
_held = {"at": 0, "data": None}
KEEP = 3600


def _read(refresh=False):
    with _lock:
        if _held["data"] and not refresh and time.time() - _held["at"] < KEEP:
            return _held["data"]
    locs = [l for l in simphony.locations() if l.get("active", True)]
    reg = {r["ref"]: r for r in locintel.registry()}
    rvc_names, errors = {}, []
    fallback = (store.get_settings().get("group") or {}).get("reporting_currency") or "ALL"

    def one(l):
        ref = l["locRef"]
        prices = simphony.menu_item_prices(ref, 0 if refresh else 3600)
        items = simphony.menu_items(ref, 0 if refresh else 6 * 3600)
        try:
            rvcs = simphony.rvc_dims(ref)
        except Exception:                                          # noqa: BLE001
            rvcs = {}
        return ref, prices, items, rvcs
    rows = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = [(l, pool.submit(one, l)) for l in locs]
        for l, f in futs:
            try:
                ref, prices, items, rvcs = f.result()
            except Exception as exc:                               # noqa: BLE001
                errors.append(f"{l['locRef']}: {str(exc)[:160]}")
                continue
            rg = reg.get(ref) or {}
            loc_name = rg.get("name") or l.get("name") or ref
            cur = rg.get("currency") or locintel.norm_currency(l.get("currency")) or fallback
            rvc_names[ref] = rvcs
            for p in prices:
                it = items.get(p["num"]) or {}
                price, cost = p["price"], p["cost"]
                rows.append({
                    "ref": ref, "location": loc_name, "brand": rg.get("brand") or "", "company": rg.get("company") or "",
                    "currency": cur, "num": p["num"], "name": it.get("name") or f"Item {p['num']}",
                    "name2": it.get("name2") or "", "major": it.get("major") or "", "family": it.get("family") or "",
                    "excluded": bool(it.get("excluded")),
                    "rvc": p["rvc"], "rvc_name": (rvcs.get(int(p["rvc"])) if p["rvc"] is not None else "") or "",
                    "level": p["level"], "level_name": p["level_name"] or (f"Level {p['level']}" if p["level"] else ""),
                    "price": price, "cost": cost,
                    # cost as a share of the price as Simphony holds it (menu prices usually include VAT)
                    "cost_pct": round(cost / price * 100, 1) if price and cost else None,
                    "from": p["from"], "to": p["to"]})
    rows.sort(key=lambda r: (r["location"], r["major"], r["family"], r["name"], r["rvc"] or 0, r["level"] or 0))
    data = {"rows": rows, "errors": errors, "read_at": time.strftime("%Y-%m-%d %H:%M"),
            "locations": sorted({(r["ref"], r["location"]) for r in rows}, key=lambda x: x[1])}
    with _lock:
        _held.update(at=time.time(), data=data)
    return data


def request(refs=None, location="", level="", group="", q="", limit=600, refresh=False):
    """The price list, filtered. refs: the Simphony locations the person may see (None = all)."""
    d = _read(refresh)
    rows = [r for r in d["rows"] if refs is None or r["ref"].upper() in refs]
    opts = {"locations": [{"ref": a, "name": b} for a, b in d["locations"] if refs is None or a.upper() in refs],
            "levels": sorted({r["level_name"] for r in rows if r["level_name"]}),
            "groups": sorted({r["major"] for r in rows if r["major"]})}
    ql = (q or "").strip().lower()
    out = [r for r in rows
           if (not location or r["ref"] == location) and (not level or r["level_name"] == level)
           and (not group or r["major"] == group)
           and (not ql or ql in r["name"].lower() or ql in r["name2"].lower() or ql == str(r["num"])
                or ql in r["family"].lower())]
    spread = _spread(out)
    return {"rows": out[:limit] if limit else out, "count": len(out), "total": len(rows),
            "items": len({r["num"] for r in out}), "locations_n": len({r["ref"] for r in out}),
            "spread": spread[:60], "options": opts, "read_at": d["read_at"], "errors": d["errors"],
            "filters": {"location": location, "level": level, "group": group, "q": q}}


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


def _matrix(rows):
    by = {}
    for r in rows:
        by.setdefault(r["num"], {}).setdefault(r["ref"], []).append(r)
    return by


def _spread(rows):
    """Items sold at more than one location at different base prices (same currency), widest gap first. Prices of 1
    or less are left out (placeholders, open prices)."""
    out = []
    for num, locs in _matrix(rows).items():
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
                out.append({"num": num, "name": any_r["name"], "currency": cur, "low": lo[0], "low_at": lo[1],
                            "high": hi[0], "high_at": hi[1], "gap_pct": round((hi[0] - lo[0]) / lo[0] * 100, 1),
                            "locations": len(ps)})
    out.sort(key=lambda x: -x["gap_pct"])
    return out


def render_xlsx(r):
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

    rows = r["rows"]
    ws = wb.active
    ws.title = "Price list"
    head(ws, ["Location", "Simphony ref", "Brand", "Currency", "Item no.", "Item", "Second name", "Major group",
              "Family group", "Revenue centre", "RVC no.", "Price level", "Level no.", "Price", "Cost", "Cost % of price",
              "Effective from", "Effective to", "Not in sales"],
         [22, 12, 12, 8, 10, 32, 20, 18, 18, 16, 7, 14, 7, 11, 10, 9, 12, 12, 9])
    for x in rows:
        ws.append([x["location"], x["ref"], x["brand"], x["currency"], x["num"], x["name"], x["name2"], x["major"],
                   x["family"], x["rvc_name"], x["rvc"], x["level_name"], x["level"], x["price"], x["cost"],
                   x["cost_pct"], x["from"], x["to"], "yes" if x["excluded"] else ""])
    for row in ws.iter_rows(min_row=2, min_col=14, max_col=15):
        for c in row:
            c.number_format = "#,##0.00"
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions

    # one row per item, a column per location: the base price (lowest price level, most common across RVCs)
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
    ]:
        ws.append([line])
    ws.column_dimensions["A"].width = 140
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
