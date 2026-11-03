"""Purchase price changes: every item bought last week at a price that moved, against the last purchase and the
contract, with the impact on the volume bought; one email to the purchaser who holds each supplier.

Source: the posted purchase invoice lines held for Price savings (savings.py: unit cost in local currency excluding
VAT, after discounts, quantities in the item's base unit), the vendor cards (Purchaser Code) and the agreed prices
(price list lines, read as on the Contract prices screen, converted to the base unit).

For every supplier and item bought in the period:
  * price now: the period's average unit cost (cost / quantity);
  * last price: the unit cost of the last day the same item was bought from the same supplier before the period,
    up to 180 days back; when the supplier never sold it before, the last price of another supplier, said so;
  * contract: the agreed purchase price of the item (the supplier's own when the list has one), in the base unit;
  * impact: (price now - last price) x quantity of the period; above the contract: (price now - contract) x
    quantity; a year at this volume: (price now - last price) x the quantity of the last 90 days x 365 / 90.
A line is in the report when the price moved by the limit (Settings → Thresholds, default 2%) either way, or is
above the contract beyond its tolerance. A price over four times or under a quarter of the last one is a unit or
data error: shown to check, left out of the totals.
Purchaser: the Purchaser Code on the supplier's card; who it is and the address in Settings → Purchasers. Each
purchaser gets their suppliers' lines, the Purchasing manager (Email recipients → Purchase price changes) all of them.
Nothing is written to Business Central.
"""
import html
import io
import json
import threading
import time
import traceback
from datetime import date, datetime, timedelta

import notify
import purchasers
import recipients as recipients_dir
import savings
import savings_report
import smtp_config
import store

LOOKBACK = 180
JUMP = 4.0
STATE_FILE = store.DATA_DIR / "price_changes.json"
_state_lock = threading.Lock()


def _f(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def last_week(today=None):
    today = today or date.today()
    monday = today - timedelta(days=today.weekday())
    return monday - timedelta(days=7), monday - timedelta(days=1)


def parse_range(date_from, date_to, today=None):
    today = today or date.today()
    if not date_from or not date_to:
        return last_week(today)
    a, b = date.fromisoformat(date_from[:10]), min(date.fromisoformat(date_to[:10]), today)
    if a > b:
        raise ValueError("The start date is after the end date.")
    if (b - a).days > 92:
        raise ValueError("Choose three months or less.")
    return a, b


def months(a, b):
    return savings.months_between(a - timedelta(days=LOOKBACK), b)


# ---------------------------------------------------------------- the figures
def contracts_base(prices, uom):
    """{item: (agreed price per base unit, supplier of the list or "")}, local currency lines only."""
    out = {}
    for item, p in (prices or {}).items():
        if (p.get("currency") or "").strip() or not _f(p.get("price")):
            continue
        per = uom.get((str(item), str(p.get("uom") or "").upper()), 1.0) if p.get("uom") else 1.0
        out[str(item)] = (_f(p["price"]) / (per or 1.0), str(p.get("vendor") or ""))
    return out


def analyse(company, a, b, rows, cards, contracts, settings, item_names=None, vendor_names=None, today=None):
    """rows: savings.daily from a - LOOKBACK to b; cards: vendor cards; contracts: contracts_base."""
    th = settings.get("thresholds") or {}
    lim = _f(th.get("price_change_pct") or 2)
    ctol = _f(th.get("price_above_contract_pct") or 2)
    pc = settings.get("purchasers") or {}
    pnames, pemails = purchasers.parse_map(pc.get("names")), purchasers.parse_map(pc.get("emails"))
    item_names, vendor_names = item_names or {}, vendor_names or {}
    vcode = {str(c.get("No")): (c.get("Purchaser_Code") or "").upper() for c in cards}
    vname = {str(c.get("No")): c.get("Name") for c in cards if c.get("Name")}
    A, B, v90 = a.isoformat(), b.isoformat(), (b - timedelta(days=89)).isoformat()
    cur, hist, vol90 = {}, {}, {}
    for day, loc, ven, item, cost, qty in rows:
        cost, qty = _f(cost), _f(qty)
        key = (ven, item)
        if day >= v90:
            vol90[key] = vol90.get(key, 0.0) + qty
        if A <= day <= B:
            x = cur.setdefault(key, {"cost": 0.0, "qty": 0.0, "locs": {}, "days": set()})
            x["cost"] += cost
            x["qty"] += qty
            x["locs"][loc] = x["locs"].get(loc, 0.0) + qty
            x["days"].add(day)
        elif day < A and qty > 0 and cost > 0:
            h = hist.setdefault(item, {}).setdefault(ven, {})
            d = h.setdefault(day, [0.0, 0.0])
            d[0] += cost
            d[1] += qty
    out = []
    for (ven, item), x in cur.items():
        if x["qty"] <= 0 or x["cost"] <= 0:
            continue
        now = x["cost"] / x["qty"]
        prev = prev_day = None
        basis = ""
        own = (hist.get(item) or {}).get(ven)
        if own:
            prev_day = max(own)
            prev, basis = own[prev_day][0] / own[prev_day][1], "same"
        else:
            other = [(d, v, c) for v, days in (hist.get(item) or {}).items() for d, c in days.items()]
            if other:
                prev_day, other_ven, c = max(other)
                prev, basis = c[0] / c[1], "other:" + other_ven
        ct = contracts.get(item)
        check = bool(prev and (now > prev * JUMP or now < prev / JUMP)) or \
            bool(ct and (now > ct[0] * JUMP or now < ct[0] / JUMP))
        d_prev = (now - prev) / prev * 100 if prev else None
        d_ct = (now - ct[0]) / ct[0] * 100 if ct else None
        moved = d_prev is not None and abs(d_prev) >= lim
        above = d_ct is not None and d_ct > ctol
        if not (moved or above):
            continue
        code = vcode.get(ven, "")
        imp = (now - prev) * x["qty"] if prev else None
        annual = (now - prev) * vol90.get((ven, item), 0.0) * 365 / 90 if prev else None
        flag = "check" if check else ("up" if (d_prev or 0) > 0 else "down" if d_prev is not None else "contract")
        out.append({
            "vendor": ven, "vendor_name": vname.get(ven) or vendor_names.get(ven) or ven,
            "purchaser": code, "purchaser_name": purchasers.label(code, pnames), "purchaser_email": pemails.get(code, ""),
            "item": item, "desc": item_names.get(item, ""), "qty": round(x["qty"], 3), "cost": round(x["cost"], 2),
            "price": round(now, 4), "prev": round(prev, 4) if prev else None, "prev_day": prev_day,
            "basis": basis[6:] and (vname.get(basis[6:]) or vendor_names.get(basis[6:]) or basis[6:]) if basis.startswith("other:") else "",
            "d_prev": round(d_prev, 1) if d_prev is not None else None, "impact": round(imp, 0) if imp is not None else None,
            "contract": round(ct[0], 4) if ct else None,
            "contract_other": bool(ct and ct[1] and ct[1] != ven),
            "d_contract": round(d_ct, 1) if d_ct is not None else None,
            "over_contract": round((now - ct[0]) * x["qty"], 0) if ct and above else None,
            "vol90": round(vol90.get((ven, item), 0.0), 3), "annual": round(annual, 0) if annual is not None else None,
            "locations": sorted(x["locs"], key=lambda L: -x["locs"][L]), "days": len(x["days"]),
            "flag": flag, "moved": moved, "above": above})
    out.sort(key=lambda r: (r["flag"] == "check", -abs(r["impact"] or r["over_contract"] or 0)))
    ok = [r for r in out if r["flag"] != "check"]

    def tot(rs):
        up = sum(r["impact"] for r in rs if r["impact"] and r["impact"] > 0)
        dn = -sum(r["impact"] for r in rs if r["impact"] and r["impact"] < 0)
        return {"lines": len(rs), "dearer": sum(1 for r in rs if (r["d_prev"] or 0) >= lim),
                "cheaper": sum(1 for r in rs if (r["d_prev"] or 0) <= -lim), "up": round(up), "down": round(dn),
                "net": round(up - dn), "annual": round(sum(r["annual"] or 0 for r in rs)),
                "above": sum(1 for r in rs if r["above"]), "over_contract": round(sum(r["over_contract"] or 0 for r in rs)),
                "spend": round(sum(r["cost"] for r in rs))}
    by = {}
    for r in out:
        P = by.setdefault(r["purchaser"], {"purchaser": r["purchaser"], "name": r["purchaser_name"],
                                           "email": r["purchaser_email"], "rows": []})
        P["rows"].append(r)
    plist = []
    for P in by.values():
        good = [r for r in P["rows"] if r["flag"] != "check"]
        P.update(tot(good), check=len(P["rows"]) - len(good), suppliers=len({r["vendor"] for r in P["rows"]}))
        P.pop("rows")
        plist.append(P)
    plist.sort(key=lambda P: (not P["purchaser"], -P["net"]))
    return {"company": company, "from": A, "to": B, "rows": out, "kpis": dict(tot(ok), check=len(out) - len(ok)),
            "purchasers": plist, "limit": lim, "contract_tol": ctol, "lookback": LOOKBACK,
            "built_at": datetime.now().isoformat(timespec="seconds")}


def build(client, company, a, b, settings=None):
    settings = settings or store.get_settings()
    errors = []
    rows = savings.daily(company, a - timedelta(days=LOOKBACK), b)
    try:
        cards = client.vendor_cards(company)
    except Exception as exc:                                       # noqa: BLE001
        cards, _ = [], errors.append(f"Vendor cards: {str(exc)[:200]}")
    try:
        prices, status = client.price_list(company)
        if not (status or {}).get("available"):
            errors.append("Agreed prices: " + str((status or {}).get("reason") or "not available")[:200])
        contracts = contracts_base(prices, savings._uoms(client, company)) if prices else {}
    except Exception as exc:                                       # noqa: BLE001
        contracts, _ = {}, errors.append(f"Agreed prices: {str(exc)[:200]}")
    r = analyse(company, a, b, rows, cards, contracts, settings, savings.names(company, "item"),
                savings.names(company, "vendor"))
    r["errors"] = errors
    return r


def request(client, company, date_from, date_to, refresh=False):
    """The report, or {"pending"} while the months it needs are read (the same months Price savings holds)."""
    a, b = parse_range(date_from, date_to)
    need = months(a, b)
    missing = savings.missing_months(company, need)
    if refresh:
        recent = [m for m in need if m >= (date.today() - timedelta(days=savings.OPEN_DAYS)).strftime("%Y-%m")]
        missing = sorted(set(missing) | set(recent))
    with savings_report._jobs_lock:
        job = dict(savings_report._jobs.get(company) or {})
    if job.get("state") == "running" or missing:
        if job.get("state") != "running":
            job = dict(savings_report._start_load(client, company, sorted(missing, reverse=True), refresh))
        return {"pending": True, "progress": {"done": job.get("done", 0), "total": job.get("total", 0)},
                "period": {"from": a.isoformat(), "to": b.isoformat()},
                "note": "Reading the purchase invoice lines of these months from Business Central (the period and "
                        f"{LOOKBACK} days before it, for the last prices). A month is read once."}
    r = build(client, company, a, b)
    r["errors"] = (job.get("errors") or []) + r["errors"]
    return r


def cut(r, purchaser):
    """The report of one purchaser's suppliers."""
    rows = [x for x in r["rows"] if x["purchaser"] == purchaser]
    P = next((p for p in r["purchasers"] if p["purchaser"] == purchaser), None)
    k = {k: P[k] for k in ("lines", "dearer", "cheaper", "up", "down", "net", "annual", "above", "over_contract",
                           "spend", "check")} if P else {}
    return dict(r, rows=rows, kpis=k, purchasers=[P] if P else [], purchaser=purchaser,
                purchaser_name=P["name"] if P else purchaser)


# ---------------------------------------------------------------- email and Excel
def _m(v):
    return "–" if v is None else f"{v:,.0f}"


def _p(v):
    return "–" if v is None else (f"{v:,.2f}" if abs(v) < 1000 else f"{v:,.0f}")


def subject(r, test=False):
    k = r["kpis"]
    who = f" · {r['purchaser_name']}" if r.get("purchaser") is not None and r.get("purchaser_name") else ""
    return (("[Test] " if test else "") + f"[RHG Purchasing] {r['company']}{who} · Purchase price changes "
            f"{r['from']} to {r['to']} · {len(r['rows'])} lines, impact {k.get('net', 0):+,.0f}")


def render_email(r, currency):
    e = html.escape
    navy, primary, ink, muted, line, sky = "#08303f", "#0c475e", "#0e2733", "#5c7482", "#d9dcdd", "#e4f3f9"
    green, red, amber = "#0e8a57", "#c0392b", "#b26a00"
    public = (smtp_config.current().get("public_url") or "").rstrip("/")
    th = f"text-align:left;padding:7px 9px;background:{sky};color:{ink};font-size:11.5px;font-weight:600"
    thn = th.replace("text-align:left", "text-align:right")
    td = f"padding:6px 9px;border-bottom:1px solid #f0f1f2;font-size:12px;color:{ink};vertical-align:top"
    tdn = td + ";text-align:right;white-space:nowrap"
    k = r["kpis"]

    def kpi(label, value, sub, color=ink):
        return (f"<td style='padding:12px 14px;background:#fff;border:1px solid {line};border-radius:10px;"
                f"vertical-align:top;width:25%'><div style='font-size:12px;color:{muted}'>{e(label)}</div>"
                f"<div style='font-size:20px;font-weight:700;color:{color};margin:4px 0'>{e(value)}</div>"
                f"<div style='font-size:11px;color:{muted}'>{e(sub)}</div></td>")
    tiles = ("<table role='presentation' cellspacing='8' cellpadding='0' style='width:100%;margin:0 -8px'><tr>"
             + kpi("Impact on the volume bought", f"{k.get('net', 0):+,.0f}",
                   f"+{_m(k.get('up'))} dearer · −{_m(k.get('down'))} cheaper · {currency}",
                   red if k.get("net", 0) > 0 else green)
             + kpi("A year at this volume", f"{k.get('annual', 0):+,.0f}", "at the quantity of the last 90 days",
                   red if k.get("annual", 0) > 0 else green)
             + kpi("Lines", str(k.get("lines", 0)), f"{k.get('dearer', 0)} dearer · {k.get('cheaper', 0)} cheaper")
             + kpi("Above the contract", str(k.get("above", 0)), f"{_m(k.get('over_contract'))} {currency} paid above",
                   amber if k.get("above") else ink)
             + "</tr></table>")

    def row(x):
        col = red if (x["d_prev"] or 0) > 0 else green
        prev = (f"{_p(x['prev'])} <span style='color:{muted}'>({e(x['prev_day'] or '')}"
                + (f", {e(x['basis'])}" if x["basis"] else "") + ")</span>") if x["prev"] else "–"
        ct = "–" if x["contract"] is None else (
            f"{_p(x['contract'])}" + (f" <span style='color:{red if (x['d_contract'] or 0) > 0 else muted}'>"
                                       f"({x['d_contract']:+.1f}%)</span>"))
        chk = f"<div style='color:{amber}'>Check the unit or the price: over 4× apart</div>" if x["flag"] == "check" else ""
        dp = "–" if x["d_prev"] is None else f"{x['d_prev']:+.1f}%"
        im = "–" if x["impact"] is None else f"{x['impact']:+,.0f}"
        an = "–" if x["annual"] is None else f"{x['annual']:+,.0f}"
        return (f"<tr><td style='{td}'><b>{e(x['desc'] or x['item'])}</b> <span style='color:{muted}'>{e(x['item'])}</span>"
                f"<div style='color:{muted}'>{e(x['vendor_name'])} · {e(', '.join(x['locations'][:4]))}</div>{chk}</td>"
                f"<td style='{tdn}'>{prev}</td><td style='{tdn}'><b>{_p(x['price'])}</b></td>"
                f"<td style='{tdn};color:{col}'>{dp}</td>"
                f"<td style='{tdn}'>{ct}</td><td style='{tdn}'>{x['qty']:,.2f}</td>"
                f"<td style='{tdn};color:{col}'><b>{im}</b></td><td style='{tdn}'>{an}</td></tr>")
    head = "".join(f"<th style='{thn if n else th}'>{e(h)}</th>" for h, n in (
        ("Item · supplier · locations", 0), ("Last price", 1), ("Now", 1), ("Change", 1), ("Contract", 1),
        ("Quantity", 1), ("Impact", 1), ("A year", 1)))
    rows = r["rows"][:120]
    table = (f"<table role='presentation' cellspacing='0' cellpadding='0' style='width:100%;border-collapse:collapse;"
             f"border:1px solid {line}'><tr>{head}</tr>{''.join(row(x) for x in rows)}</table>") if rows else \
        f"<p style='font-size:13px;color:{muted}'>No price moved by {r['limit']:g}% or more, and nothing was paid above the contract.</p>"
    by = ""
    if r.get("purchaser") is None and r["purchasers"]:
        by = (f"<h3 style='font-size:15px;color:{ink};margin:22px 0 6px'>By purchaser</h3>"
              f"<table role='presentation' cellspacing='0' cellpadding='0' style='width:100%;border-collapse:collapse;border:1px solid {line}'>"
              f"<tr><th style='{th}'>Purchaser</th><th style='{thn}'>Lines</th><th style='{thn}'>Dearer</th>"
              f"<th style='{thn}'>Above contract</th><th style='{thn}'>Impact</th><th style='{thn}'>A year</th></tr>"
              + "".join(f"<tr><td style='{td}'>{e(P['name'])}<div style='color:{muted}'>{e(P['email'] or ('no address in Settings → Purchasers' if P['purchaser'] else 'set the Purchaser Code on the supplier cards'))}</div></td>"
                        f"<td style='{tdn}'>{P['lines']}</td><td style='{tdn}'>{P['dearer']}</td><td style='{tdn}'>{P['above']}</td>"
                        f"<td style='{tdn};color:{red if P['net'] > 0 else green}'><b>{P['net']:+,.0f}</b></td>"
                        f"<td style='{tdn}'>{P['annual']:+,.0f}</td></tr>" for P in r["purchasers"]) + "</table>")
    more = f"<p style='font-size:12px;color:{muted}'>{len(r['rows']) - len(rows)} more lines in the Excel file.</p>" \
        if len(r["rows"]) > len(rows) else ""
    link = (f"<p style='margin:18px 0 0'><a href='{e(public)}/purchasing/#prices' style='background:{primary};color:#fff;"
            f"text-decoration:none;padding:10px 18px;border-radius:8px;font-size:13px;font-weight:600'>Open Price changes</a></p>"
            if public else "")
    who = r.get("purchaser_name") if r.get("purchaser") is not None else "Purchasing"
    body = f"""<!doctype html><html><head><meta charset="utf-8"></head><body style="margin:0;background:#ecebeb;font-family:Figtree,Segoe UI,Arial,sans-serif">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:980px">
<tr><td style="background:{navy};color:#fff;padding:18px 22px;border-radius:12px 12px 0 0">
<div style="font-size:11px;letter-spacing:.12em;color:#a0d8ea">REVO HOSPITALITY GROUP · PULSE · PURCHASING</div>
<div style="font-size:20px;font-weight:700;margin-top:4px">Purchase price changes · {e(who or '')}</div>
<div style="font-size:12px;color:#cfe3ea;margin-top:4px">{e(r['company'])} · invoices posted {e(r['from'])} to {e(r['to'])}</div></td></tr>
<tr><td style="background:#fff;padding:20px 22px;border-radius:0 0 12px 12px">
<p style="font-size:13px;color:{ink};margin:0 0 12px">Every item bought from {'your suppliers' if r.get('purchaser') is not None else 'every supplier'} at a price that moved by {r['limit']:g}% or more against the last purchase, or paid above the contract by more than {r['contract_tol']:g}%. Prices in {e(currency)} per base unit, excluding VAT, after discounts. Impact = the change × the quantity bought in the period.</p>
{tiles}{by}
<h3 style="font-size:15px;color:{ink};margin:22px 0 6px">Price changes</h3>{table}{more}{link}
<p style="color:{muted};font-size:11px;margin:22px 0 0;border-top:1px solid {line};padding-top:12px">Sent by Pulse RHG from Business Central (posted purchase invoices, price lists, vendor cards); nothing was changed there. The purchaser of a supplier is the Purchaser Code on its card; names and addresses in Settings → Purchasers. Every line is in the Excel file.</p>
</td></tr></table></td></tr></table></body></html>"""
    text = (f"Purchase price changes {r['from']} to {r['to']}: {k.get('lines', 0)} lines, impact {k.get('net', 0):+,.0f} {currency}.\n"
            + "\n".join(f"- {x['desc'] or x['item']} ({x['vendor_name']}): {_p(x['prev'])} -> {_p(x['price'])}"
                        f" ({'–' if x['d_prev'] is None else format(x['d_prev'], '+.1f') + '%'}), impact {_m(x['impact'])}"
                        for x in rows))
    return body, text


def render_xlsx(r, currency):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    fill, font = PatternFill("solid", fgColor="E4F3F9"), Font(bold=True, color="0E2733")
    money, unit, pct = '+#,##0;-#,##0;0', '#,##0.00', '+0.0;-0.0;0.0'
    red, green = Font(color="C0392B"), Font(color="0E8A57")

    def sheet(title, header, rows, formats, widths, first=False, signs=()):
        ws = wb.active if first else wb.create_sheet()
        ws.title = title[:31]
        ws.append(header)
        for c in ws[1]:
            c.fill, c.font, c.alignment = fill, font, Alignment(wrap_text=True, vertical="top")
        for row in rows:
            ws.append(row)
        for i, f in enumerate(formats, start=1):
            if f:
                for (cell,) in ws.iter_rows(min_row=2, min_col=i, max_col=i):
                    cell.number_format = f
        for col in signs:
            for (cell,) in ws.iter_rows(min_row=2, min_col=col, max_col=col):
                if isinstance(cell.value, (int, float)) and cell.value:
                    cell.font = red if cell.value > 0 else green
        for i, w in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.freeze_panes = "A2"
        if rows:
            ws.auto_filter.ref = ws.dimensions
    flag = {"up": "Dearer", "down": "Cheaper", "contract": "Above contract", "check": "Check unit/price"}
    sheet("Price changes", ["Purchaser", "Supplier", "Supplier name", "Item", "Description", "Locations", "Quantity",
                            "Spend", "Last price", "Last bought", "Last price from", "Price now", "Change %", "Impact",
                            "Contract", "Contract of another supplier", "Vs contract %", "Paid above contract",
                            "Quantity 90 days", "A year at this volume", "Result"],
          [[x["purchaser_name"], x["vendor"], x["vendor_name"], x["item"], x["desc"], ", ".join(x["locations"]), x["qty"],
            x["cost"], x["prev"], x["prev_day"], x["basis"] or ("same supplier" if x["prev"] else ""), x["price"],
            x["d_prev"], x["impact"], x["contract"], "yes" if x["contract_other"] else "", x["d_contract"],
            x["over_contract"], x["vol90"], x["annual"], flag[x["flag"]]] for x in r["rows"]],
          [None] * 6 + [unit, '#,##0', unit, None, None, unit, pct, money, unit, None, pct, money, unit, money, None],
          [16, 10, 26, 12, 32, 20, 10, 12, 11, 11, 16, 11, 9, 12, 11, 10, 10, 12, 11, 13, 14], first=True,
          signs=(13, 14, 17, 18, 20))
    sheet("By purchaser", ["Purchaser", "Code", "Email", "Suppliers", "Lines", "Dearer", "Cheaper", "Above contract",
                           "Dearer impact", "Cheaper impact", "Net impact", "A year", "Paid above contract", "To check"],
          [[P["name"], P["purchaser"], P["email"], P["suppliers"], P["lines"], P["dearer"], P["cheaper"], P["above"],
            P["up"], -P["down"], P["net"], P["annual"], P["over_contract"], P["check"]] for P in r["purchasers"]],
          [None] * 8 + [money] * 5 + [None], [22, 14, 26, 9, 7, 7, 8, 9, 12, 12, 12, 12, 12, 8], signs=(11, 12))
    ws = wb.create_sheet("Read me")
    for line in (f"Purchase price changes · {r['company']} · invoices posted {r['from']} to {r['to']}",
                 f"Prices in {currency} per base unit, excluding VAT, after discounts (posted purchase invoice lines).",
                 f"Last price: the last day the item was bought from the same supplier, up to {r['lookback']} days "
                 "before; else from another supplier (named).",
                 "Contract: the agreed purchase price active today, converted to the base unit (local currency lines).",
                 f"In the report: a change of {r['limit']:g}% or more, or above the contract by more than {r['contract_tol']:g}%.",
                 "Impact = (price now - last price) x quantity; a year = the same change on the quantity of the last 90 "
                 "days x 365 / 90. Over 4x apart: a unit or data error to check, left out of the totals."):
        ws.append([line])
    ws.column_dimensions["A"].width = 120
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def file_stem(r):
    who = f"_{r['purchaser_name']}" if r.get("purchaser") is not None and r.get("purchaser_name") else ""
    return f"RHG_{r['company'].replace(' ', '_')}{who.replace(' ', '_')}_price_changes_{r['from']}"


# ---------------------------------------------------------------- sending and the weekly run
def summary_recipients(company):
    n = store.get_settings().get("notifications") or {}
    to = notify.parse_emails(n.get("manager", ""))[0]
    cc = [c for c in notify.parse_emails(n.get("cost_controller", ""))[0] if c not in to]
    res = recipients_dir.resolve("price_changes", [company], to, cc)
    return res["to"], res["cc"]


def send_one(r, currency, to, cc=(), actor="scheduler", test=False):
    body, text = render_email(r, currency)
    notify.send_mail(list(to), list(cc), subject(r, test), body, text,
                     [(f"{file_stem(r)}.xlsx", "application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       render_xlsx(r, currency))])
    entry = {"ts": datetime.now().isoformat(timespec="seconds"), "actor": actor, "company": r["company"],
             "purchaser": r.get("purchaser_name") if r.get("purchaser") is not None else "All (summary)",
             "to": list(to), "cc": list(cc), "lines": r["kpis"].get("lines", 0), "net": r["kpis"].get("net", 0),
             "period": {"from": r["from"], "to": r["to"]}, "status": "sent", "test": test}
    _log(entry)
    store.audit("email", "purchase price changes", actor, {}, entry, subject(r, test))
    return entry


def send_all(r, currency, actor="scheduler", test_to=None, only=None):
    """Each purchaser with an address gets their suppliers' lines (none when nothing moved); the Purchasing
    manager the whole report. test_to: everything to these addresses only. only: one purchaser code."""
    out = []
    for P in r["purchasers"]:
        if only is not None and P["purchaser"] != only:
            continue
        if not P["purchaser"] or not (P["email"] or test_to):
            continue
        to = test_to or notify.parse_emails(P["email"])[0]
        if not to:
            continue
        try:
            out.append(send_one(cut(r, P["purchaser"]), currency, to, actor=actor, test=bool(test_to)))
        except Exception as exc:                                   # noqa: BLE001
            out.append({"purchaser": P["name"], "status": "failed", "detail": str(exc)[:200]})
            _log(dict(out[-1], ts=datetime.now().isoformat(timespec="seconds"), company=r["company"]))
    if only is None:
        to, cc = (test_to, []) if test_to else summary_recipients(r["company"])
        if to:
            try:
                out.append(send_one(r, currency, to, cc, actor=actor, test=bool(test_to)))
            except Exception as exc:                               # noqa: BLE001
                out.append({"purchaser": "All (summary)", "status": "failed", "detail": str(exc)[:200]})
                _log(dict(out[-1], ts=datetime.now().isoformat(timespec="seconds"), company=r["company"]))
        else:
            out.append({"purchaser": "All (summary)", "status": "skipped",
                        "detail": "No Purchasing manager address (Email recipients → Purchase price changes)."})
    return out


def _state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(s):
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(s, indent=1, default=str), encoding="utf-8")
    tmp.replace(STATE_FILE)


def _log(entry):
    with _state_lock:
        s = _state()
        s["runs"] = (s.get("runs", []) + [entry])[-150:]
        _save_state(s)


def recent_runs(limit=15):
    with _state_lock:
        return list(reversed(_state().get("runs", [])))[:limit]


def settings():
    n = store.get_settings().get("notifications") or {}
    return {"enabled": bool(n.get("price_changes")), "time": n.get("price_changes_time") or "08:30"}


def next_run(now=None):
    s = settings()
    if not s["enabled"]:
        return None
    now = now or datetime.now()
    hh, _, mm = s["time"].partition(":")
    for k in range(8):
        d = now.date() + timedelta(days=k)
        t = datetime(d.year, d.month, d.day, int(hh), int(mm or 0))
        if d.weekday() == 0 and t + timedelta(hours=10) >= now:
            with _state_lock:
                if _state().get("done") == d.isoformat():
                    continue
            return t.isoformat(timespec="minutes")
    return None


class Weekly:
    """Every Monday at the time set: last week's report, to each purchaser and to the Purchasing manager."""

    def __init__(self, client, companies_of, currency_of):
        self.client, self.companies_of, self.currency_of = client, companies_of, currency_of

    def due(self, now):
        s = settings()
        if not s["enabled"] or now.weekday() != 0:
            return None
        hh, _, mm = s["time"].partition(":")
        start = now.replace(hour=int(hh), minute=int(mm or 0), second=0, microsecond=0)
        if not (start <= now <= start + timedelta(hours=10)):
            return None
        day = now.date().isoformat()
        with _state_lock:
            st = _state()
        if st.get("done") == day:
            return None
        t = st.get("tries", {})
        if t.get("day") == day and (t.get("count", 0) >= 3 or time.time() - t.get("at", 0) < 1200):
            return None
        return day

    def run(self, day):
        a, b = last_week(date.fromisoformat(day))
        ok = True
        for company in self.companies_of():
            try:
                errs = savings.ensure(self.client, company, months(a, b))
                if errs:
                    raise RuntimeError("Invoice lines not read: " + "; ".join(errs)[:200])
                r = build(self.client, company, a, b)
                res = send_all(r, self.currency_of(company))
                ok = ok and all(x.get("status") in ("sent", "skipped") for x in res)
            except Exception as exc:                               # noqa: BLE001
                traceback.print_exc()
                ok = False
                _log({"ts": datetime.now().isoformat(timespec="seconds"), "actor": "scheduler", "company": company,
                      "purchaser": "", "status": "failed", "detail": str(exc)[:300],
                      "period": {"from": a.isoformat(), "to": b.isoformat()}})
        with _state_lock:
            st = _state()
            t = st.get("tries", {})
            count = t.get("count", 0) + 1 if t.get("day") == day else 1
            if ok or count >= 3:
                st["done"] = day
                st.pop("tries", None)
            else:
                st["tries"] = {"day": day, "count": count, "at": time.time()}
            _save_state(st)

    def loop(self):
        while True:
            try:
                day = self.due(datetime.now())
                if day:
                    self.run(day)
            except Exception:                                      # noqa: BLE001
                traceback.print_exc()
            time.sleep(60)

    def start(self):
        threading.Thread(target=self.loop, daemon=True, name="price-changes").start()
