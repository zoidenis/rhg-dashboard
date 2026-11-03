"""The daily negative stock report: every item below zero at every location, at the end of yesterday, each
line with its likely cause and the person who fixes it.

A quantity below zero means something was taken out of stock that, for Business Central, never came in.
Each line is put in one of three causes, each with its owner:

  * Receipt not posted (Stock manager): the goods are in, the posting is not. An open purchase order line
    for the item and location with quantity still to receive, or a transfer shipped to the location and
    not received; an item made here (assembly output) used or shipped beyond what was posted as made; or
    a negative adjustment larger than the stock.
  * Recipe wrong (Cost control): the stock went below zero through recipe consumption (Assembly
    Consumption), so the recipes use more than the location receives, or use an item the location never
    receives (the wrong component). The recipes that use the item are listed with their quantity per,
    and a unit without a conversion is flagged.
  * Sold without purchase (Purchasing): sold as it is (Sale), shipped on to other locations, or returned
    to the vendor beyond what was bought: bought outside Business Central, without a PO, or the purchase
    is not posted yet.

A document (purchase order, transfer) is evidence; the other causes are read from the item ledger of the
last 90 days (the movements since the line went below zero, else the whole window) and say so.
"Since" is the day the balance last went below zero, walked back from the quantity on hand with the daily
movements; before the window it shows as more than 90 days.

It reads what Movement & dead stock reads (stock by location and the item ledger), the open purchase orders
(page 50 with its lines) and the open transfers. Business Central is never written. One email per company
to the Stock manager, the Cost controller and Purchasing (Email recipients → Negative stock · daily), one
per location to the location's own people, every day at the time set in Finance → Settings.
"""
import html
import io
import json
import threading
import time
import traceback
import uuid
from datetime import date, datetime, timedelta

import arap_notify
import config
import movement
import recipes
import recipients
import store

STATE_FILE = store.DATA_DIR / "negative_report.json"
HISTORY = 90
TOL = 0.0005
WAIT = 45 * 60
_state_lock, _run_lock = threading.Lock(), threading.Lock()

OWNERS = [("stock", "Stock manager", "Receipt not posted",
           "Post the receipt or the transfer that is waiting, or the production; recount where nothing is waiting."),
          ("cost", "Cost control", "Recipe wrong",
           "Check the recipes that use the item: quantity per, unit of measure, or the wrong component."),
          ("purchasing", "Purchasing", "Sold without purchase",
           "Find the purchase: bought without a PO, invoice not posted, or bought under another item or location.")]
OWNER = {k: (label, cause, todo) for k, label, cause, todo in OWNERS}
OUT_KINDS = ("consumption", "sale", "transfer_out", "adj_neg", "purchase_return")
IN_KINDS = ("receipt", "transfer_in", "output", "adj_pos", "sale_return")
DAYS = {"daily": (0, 1, 2, 3, 4, 5, 6), "mon_sat": (0, 1, 2, 3, 4, 5), "weekdays": (0, 1, 2, 3, 4)}


def _m(v):
    if v is None:
        return "–"
    a = abs(v)
    s = f"{a / 1e6:,.1f} M" if a >= 1e6 else f"{a / 1e3:,.0f} k" if a >= 1e4 else f"{a:,.0f}"
    return ("−" if v < 0 else "") + s


def _q(v):
    a = abs(v)
    s = f"{a:,.0f}" if a >= 100 else (f"{a:,.1f}" if a >= 10 else f"{a:,.2f}").rstrip("0").rstrip(".")
    return ("−" if v < 0 else "") + s


def _day(v):
    s = str(v or "")[:10]
    return "" if not s or s.startswith("0001") else s


# ---------------------------------------------------------------- open documents
def open_documents(client, company):
    """{(location, item): [evidence]} of what is on its way into a location: purchase order lines with
    quantity still to receive, and transfers shipped and not received. Errors are returned, not raised."""
    docs, errors = {}, []
    try:
        rows = client._get_all(client._url(company, config.E_PO),
                               {"$filter": "Document_Type eq 'Order'", "$expand": config.E_PO_LINES})
        for r in rows:
            head_loc = r.get("Location_Code") or ""
            for line in r.get(config.E_PO_LINES) or []:
                if (line.get("Type") or "") != "Item" or not line.get("No"):
                    continue
                left = float(line.get("Quantity") or 0) - float(line.get("Quantity_Received") or 0)
                if left <= TOL:
                    continue
                loc = line.get("Location_Code") or head_loc
                docs.setdefault((loc, str(line["No"])), []).append({
                    "kind": "PO", "no": r.get("No") or "", "party": r.get("Buy_from_Vendor_Name") or r.get("Buy_from_Vendor_No") or "",
                    "qty": round(left, 3), "uom": line.get("Unit_of_Measure_Code") or "", "status": r.get("Status") or "",
                    "date": _day(r.get("Order_Date")) or _day(r.get("Document_Date")),
                    "expected": _day(line.get("Expected_Receipt_Date")) or _day(r.get("Expected_Receipt_Date"))})
    except Exception as exc:                                       # noqa: BLE001
        errors.append(f"Open purchase orders could not be read: {str(exc)[:200]}")
    try:
        heads = {str(h.get("No")): h for h in client._get_all(client._url(company, config.E_TRANSFER_HEADERS))}
        for line in client._get_all(client._url(company, config.E_TRANSFER_LINES)):
            shipped = float(line.get("Quantity_Shipped") or 0)
            h = heads.get(str(line.get("Document_No")))
            if shipped <= TOL or not h or not line.get("Item_No"):
                continue
            docs.setdefault((h.get("Transfer_to_Code") or "", str(line["Item_No"])), []).append({
                "kind": "Transfer", "no": str(line.get("Document_No")), "party": h.get("Transfer_from_Code") or "",
                "qty": round(shipped, 3), "uom": "", "status": "shipped", "date": _day(h.get("Posting_Date")),
                "expected": ""})
    except Exception as exc:                                       # noqa: BLE001
        errors.append(f"Open transfers could not be read: {str(exc)[:200]}")
    return docs, errors


def _doc_text(d):
    if d["kind"] == "PO":
        when = f", expected {d['expected']}" if d["expected"] else (f", ordered {d['date']}" if d["date"] else "")
        return f"PO {d['no']} {d['party']}: {_q(d['qty'])} {d['uom']} not received{when}".replace("  ", " ")
    return f"Transfer {d['no']} from {d['party']}: {_q(d['qty'])} shipped {d['date']}, not received"


# ---------------------------------------------------------------- the analysis
def analyse(company, mv, m, docs, as_of, previous=None, settings=None):
    """mv: a Movement & dead stock result (its _rows); m: recipes.master; docs: open_documents."""
    items = (m or {}).get("items") or {}
    uom = {(it, code): per for it, code, per in (m or {}).get("uom") or []}
    used_in = {}
    for parent, _line, typ, no, _desc, _nested, qty, unit in (m or {}).get("bom") or []:
        if typ == "Item" and no:
            used_in.setdefault(no, []).append((parent, qty, (unit or "").upper()))
    rows = mv.get("_rows") or []
    neg = [r for r in rows if r[2] < -TOL]
    held = {}
    for r in rows:
        if r[2] > TOL:
            held.setdefault(r[1], []).append((r[0], r[2]))
    start = as_of - timedelta(days=HISTORY - 1)
    keys = {(r[0], r[1]) for r in neg}
    parents = {p for r in neg for p, _, _ in used_in.get(r[1], [])}
    daily, sold_parent = {}, {}
    with movement._lock:
        db = movement._db()
        q = db.execute("SELECT location, item, kind, day, qty FROM mv WHERE company=? AND day>=? AND day<=?",
                       (company, start.isoformat(), as_of.isoformat()))
        for loc, it, kind, day, qty in q:
            if (loc, it) in keys:
                daily.setdefault((loc, it), []).append((day, kind, qty))
            if it in parents and kind in ("sale", "sale_return"):
                sold_parent.setdefault((loc, it), []).append((day, -qty))
    prev_lines = {x["key"]: x for x in (previous or {}).get("lines") or []}
    out = []
    for r in neg:
        loc, it, qty, value = r[0], r[1], r[2], r[3]
        card = items.get(it) or {}
        mov = sorted(daily.get((loc, it), []))
        by_day = {}
        for d, _k, q_ in mov:
            by_day[d] = by_day.get(d, 0.0) + q_
        # walk back from the quantity on hand: the last day the balance was not below zero
        bal, since = qty, None
        d = as_of
        while d >= start:
            bal -= by_day.get(d.isoformat(), 0.0)          # balance at the end of the day before d
            if bal >= -TOL:
                since = d
                break
            d -= timedelta(days=1)
        episode_from = since.isoformat() if since else start.isoformat()
        tot = {k: 0.0 for k in OUT_KINDS + IN_KINDS + ("other",)}
        ep = {k: 0.0 for k in OUT_KINDS + IN_KINDS + ("other",)}
        last_in = ""
        for d_, k, q_ in mov:
            tot[k] = tot.get(k, 0.0) + q_
            if d_ >= episode_from:
                ep[k] = ep.get(k, 0.0) + q_
            if k in ("receipt", "transfer_in", "output") and q_ > 0:
                last_in = max(last_in, d_)
        outs = {k: -ep[k] for k in OUT_KINDS if ep[k] < 0} or {k: -tot[k] for k in OUT_KINDS if tot[k] < 0}
        received = tot["receipt"] + tot["transfer_in"]
        made = tot["output"]
        dominant = max(outs, key=outs.get) if outs else None
        base = str(card.get("Base_Unit_of_Measure") or "").upper()
        unit = base.lower() if base else ""
        # a purchase order counts once its goods were due (expected receipt, else order date); a transfer
        # shipped to the location counts whatever its date: receiving it is all that is left to do
        mine = sorted(docs.get((loc, it), []), key=lambda x: x["date"] or "")
        due = [x for x in mine if x["kind"] != "PO" or (x["expected"] or x["date"] or "9999") <= as_of.isoformat()]
        coming = [x for x in mine if x not in due]
        evidence = [_doc_text(x) for x in due]
        recipe_list = []
        for parent, per, u in used_in.get(it, []):
            conv = 1.0 if not u or u == base else uom.get((it, u))
            sold = sum(q_ for d_, q_ in sold_parent.get((loc, parent), []) if d_ >= episode_from)
            recipe_list.append({"parent": parent, "desc": (items.get(parent) or {}).get("Description") or "",
                                "per": per, "unit": u or base, "no_conversion": conv is None, "sold": round(sold, 2),
                                "use": round(sold * per * (conv or 0), 3)})
        recipe_list.sort(key=lambda x: (-x["use"], -x["sold"]))
        since_txt = f"since {episode_from}" if since else f"for more than {HISTORY} days"
        strength = "ledger"
        if evidence:
            owner, strength = "stock", "document"
            why = evidence[0] + (f" (and {len(evidence) - 1} more)" if len(evidence) > 1 else "")
        elif made > TOL and dominant in ("sale", "transfer_out", "consumption"):
            owner = "stock"
            why = (f"Made here: {_q(made)} {unit} posted as made in {HISTORY} days, {_q(outs[dominant])} "
                   f"{unit} {'used' if dominant == 'consumption' else 'sold' if dominant == 'sale' else 'shipped'} {since_txt}. "
                   f"Post the production (assembly).")
        elif dominant == "adj_neg":
            owner = "stock"
            why = f"Negative adjustments of {_q(outs['adj_neg'])} {unit} {since_txt} took it below zero. Recount."
        elif dominant == "consumption":
            owner = "cost"
            bad_unit = [x for x in recipe_list if x["no_conversion"]]
            if received <= TOL:
                why = (f"Used {_q(outs['consumption'])} {unit} by recipes {since_txt}; this location received none in "
                       f"{HISTORY} days. Check the recipes use the item this location stocks.")
            else:
                why = (f"Used {_q(outs['consumption'])} {unit} by recipes {since_txt}; received {_q(received)} {unit} in "
                       f"{HISTORY} days. Check the quantity per.")
            if bad_unit:
                why += f" Unit {bad_unit[0]['unit']} in recipe {bad_unit[0]['parent']} has no conversion."
        elif dominant == "sale":
            owner = "purchasing"
            why = (f"Sold {_q(outs['sale'])} {unit} {since_txt}; " +
                   (f"received {_q(received)} {unit} in {HISTORY} days" if received > TOL else
                    f"none bought or received here in {HISTORY} days") + ".")
        elif dominant == "transfer_out":
            owner = "purchasing"
            why = (f"Shipped {_q(outs['transfer_out'])} {unit} to other locations {since_txt}; bought or received "
                   f"{_q(received)} {unit} in {HISTORY} days.")
        elif dominant == "purchase_return":
            owner = "purchasing"
            why = f"Returned {_q(outs['purchase_return'])} {unit} to the vendor {since_txt}, beyond what was received."
        else:
            owner = "stock"
            why = f"Below zero for more than {HISTORY} days with no movement since. Count it and adjust."
        if coming and owner != "stock":
            why += f" On order: {_doc_text(coming[0])}."
        elsewhere = sorted((x for x in held.get(it, []) if x[0] != loc), key=lambda x: -x[1])[:2]
        if elsewhere and owner != "stock":
            why += " Held at " + ", ".join(f"{L} {_q(qq)}" for L, qq in elsewhere) + "."
        key = f"{loc}|{it}"
        days = (as_of - since).days + 1 if since else None
        out.append({"key": key, "location": loc, "item": it, "desc": card.get("Description") or "",
                    "category": str(card.get("Item_Category_Code") or ""), "unit": unit, "qty": round(qty, 3),
                    "value": round(value, 2), "unit_cost": float(card.get("Unit_Cost") or 0),
                    "since": episode_from if since else "", "days": days,
                    "new": key not in prev_lines if previous else bool(since and since >= as_of),
                    "owner": owner, "cause": OWNER[owner][1], "why": why, "strength": strength,
                    "evidence": evidence, "recipes": recipe_list[:5], "received": round(received, 3),
                    "made": round(made, 3), "outs": {k: round(v, 3) for k, v in outs.items()}, "last_in": last_in,
                    "elsewhere": elsewhere})
    out.sort(key=lambda x: (not x["new"], x["value"]))
    locs = {}
    for x in out:
        L = locs.setdefault(x["location"], {"location": x["location"], "lines": 0, "value": 0.0, "new": 0,
                                            **{k: 0 for k, *_ in OWNERS}})
        L["lines"] += 1
        L["value"] += x["value"]
        L["new"] += x["new"]
        L[x["owner"]] += 1
    by_owner = {k: [len([x for x in out if x["owner"] == k]), round(sum(x["value"] for x in out if x["owner"] == k), 0)]
                for k, *_ in OWNERS}
    head = {"lines": len(out), "value": round(sum(x["value"] for x in out), 0), "new": sum(1 for x in out if x["new"]),
            "locations": len(locs), "documents": sum(1 for x in out if x["strength"] == "document"),
            **{f"{k}_lines": v[0] for k, v in by_owner.items()}}
    gone = [v for k, v in prev_lines.items() if k not in {x["key"] for x in out}] if previous else []
    return {"company": company, "as_of": as_of.isoformat(), "history_from": start.isoformat(), "lines": out,
            "locations": sorted(locs.values(), key=lambda L: L["value"]), "by_owner": by_owner, "headline": head,
            "resolved": [{"location": g["location"], "item": g["item"], "desc": g.get("desc", ""), "owner": g["owner"]}
                         for g in gone]}


def cut(r, location):
    """The report of one location: its lines only, totals of its own."""
    lines = [x for x in r["lines"] if x["location"] == location]
    by_owner = {k: [len([x for x in lines if x["owner"] == k]), round(sum(x["value"] for x in lines if x["owner"] == k), 0)]
                for k, *_ in OWNERS}
    head = {"lines": len(lines), "value": round(sum(x["value"] for x in lines), 0), "new": sum(1 for x in lines if x["new"]),
            "locations": 1 if lines else 0, "documents": sum(1 for x in lines if x["strength"] == "document"),
            **{f"{k}_lines": v[0] for k, v in by_owner.items()}}
    return dict(r, location=location, label=f"{r['company']} · {location}", key=f"{r['company']}|{location}",
                lines=lines, by_owner=by_owner, headline=head,
                locations=[L for L in r["locations"] if L["location"] == location],
                resolved=[x for x in r.get("resolved") or [] if x["location"] == location])


# ---------------------------------------------------------------- the email
def subject(r, test=False):
    h = r["headline"]
    d = date.fromisoformat(r["as_of"])
    return (("[Test] " if test else "") + f"[RHG Finance] {r.get('label') or r['company']} · Negative stock {d:%d %b %Y} · "
            f"{h['lines']} {'line' if h['lines'] == 1 else 'lines'}, {h['new']} new")


def render(r, public_url="", max_lines=50):
    e = html.escape
    C = r.get("currency") or ""
    H, P = r["headline"], ((r.get("previous") or {}).get("headline") or {})
    navy, primary, ink, muted, line, sky = "#08303f", "#0c475e", "#0e2733", "#5c7482", "#d9dcdd", "#e4f3f9"
    red, amber, green = "#c0392b", "#b26a00", "#0e8a57"
    th = f"text-align:left;padding:6px 8px;background:{sky};color:{ink};font-size:11.5px;font-weight:600"
    thn = th.replace("text-align:left", "text-align:right")
    td = f"padding:5px 8px;border-bottom:1px solid #f0f1f2;font-size:12px;color:{ink};vertical-align:top"
    tdn = td + ";text-align:right;white-space:nowrap"
    d = date.fromisoformat(r["as_of"])

    def delta(key, money=False):
        if P.get(key) is None or H.get(key) is None:
            return ""
        dv = H[key] - P[key]
        if (abs(dv) < 1000) if money else dv == 0:
            return f"<span style='color:{muted}'>= yesterday</span>"
        worse = dv < 0 if money else dv > 0           # more value below zero is a more negative number
        txt = _m(dv) if money else f"{dv:+,.0f}"
        if money and dv > 0:
            txt = "+" + txt
        return f"<span style='color:{red if worse else green}'>{'▲' if dv > 0 else '▼'} {txt} vs yesterday</span>"

    def tile(label, value, sub, color=ink, dl=""):
        return (f"<td style='padding:10px 12px;background:#fff;border:1px solid {line};border-radius:10px;vertical-align:top;"
                f"width:25%'><div style='font-size:11.5px;color:{muted}'>{e(label)}</div>"
                f"<div style='font-size:19px;font-weight:700;color:{color};margin:3px 0'>{value}</div>"
                f"<div style='font-size:11px;color:{muted}'>{e(sub)}</div><div style='font-size:11px;margin-top:2px'>{dl}</div></td>")
    owners_sub = " · ".join(f"{OWNER[k][0]} {H.get(k + '_lines', 0)}" for k, *_ in OWNERS)
    tiles = ("<table role='presentation' cellspacing='6' cellpadding='0' style='width:100%;margin:0 -6px'><tr>"
             + tile("Lines below zero", f"{H['lines']:,}", f"{H['locations']} locations", red if H["lines"] else green, delta("lines"))
             + tile("Value below zero", _m(H["value"]), f"at item unit cost, {C}", red if H["value"] < 0 else ink, delta("value", True))
             + tile("New since " + (f"{date.fromisoformat(r['previous']['as_of']):%d %b}" if r.get("previous") else "yesterday"),
                    f"{H['new']:,}", f"{len(r.get('resolved') or [])} back to zero or above",
                    amber if H["new"] else ink)
             + tile("With a document waiting", f"{H['documents']:,}", "open PO or transfer", ink)
             + "</tr></table>"
             + f"<div style='font-size:11.5px;color:{muted};margin:2px 0 0'>By owner: {e(owners_sub)}</div>")
    loc_rows = "".join(
        f"<tr><td style='{td}'>{e(L['location'])}</td><td style='{tdn}'>{L['lines']:,}</td><td style='{tdn};color:{red}'>{_m(L['value'])}</td>"
        + "".join(f"<td style='{tdn}'>{L[k] or ''}</td>" for k, *_ in OWNERS)
        + f"<td style='{tdn};color:{amber if L['new'] else muted}'>{L['new'] or ''}</td></tr>" for L in r["locations"])
    loc_table = (f"<h3 style='font-size:14px;color:{ink};margin:22px 0 6px'>By location</h3>"
                 f"<table role='presentation' cellspacing='0' cellpadding='0' style='width:100%;border-collapse:collapse;border:1px solid {line}'>"
                 f"<tr><th style='{th}'>Location</th><th style='{thn}'>Lines</th><th style='{thn}'>{e(C)}</th>"
                 + "".join(f"<th style='{thn}'>{e(OWNER[k][1])}</th>" for k, *_ in OWNERS)
                 + f"<th style='{thn}'>New</th></tr>{loc_rows}</table>") if len(r["locations"]) > 1 else ""
    sections = []
    for k, label, cause, todo in OWNERS:
        lines = [x for x in r["lines"] if x["owner"] == k]
        n, v = len(lines), sum(x["value"] for x in lines)
        head = (f"<h3 style='font-size:14px;color:{ink};margin:24px 0 2px'>{e(label)} · {e(cause)} "
                f"<span style='color:{muted};font-weight:400'>({n:,} lines, {_m(v)})</span></h3>"
                f"<div style='font-size:12px;color:{muted}'>{e(todo)}</div>")
        if not lines:
            sections.append(head + f"<p style='font-size:12px;color:{green};margin:6px 0 0'>Nothing for {e(label.lower())} today.</p>")
            continue
        shown = lines if not max_lines else lines[:max_lines]
        body = "".join(
            f"<tr><td style='{td}'>{e(x['location'])}</td><td style='{td}'>"
            + (f"<span style='background:#fdf3e2;color:{amber};font-size:10.5px;font-weight:700;padding:1px 5px;border-radius:4px;margin-right:4px'>NEW</span>" if x["new"] else "")
            + f"{e(x['desc'] or x['item'])} <span style='color:{muted}'>{e(x['item'])}</span></td>"
            f"<td style='{tdn};color:{red}'>{_q(x['qty'])} {e(x['unit'])}</td><td style='{tdn}'>{_m(x['value'])}</td>"
            f"<td style='{tdn};color:{muted}'>{(str(x['days']) + ' d') if x['days'] else '90+ d'}</td>"
            f"<td style='{td}'>{e(x['why'])}</td></tr>" for x in shown)
        more = (f"<div style='font-size:11.5px;color:{muted};margin-top:4px'>{n - len(shown):,} more lines in the Excel file.</div>"
                if len(shown) < n else "")
        sections.append(head + f"<table role='presentation' cellspacing='0' cellpadding='0' style='width:100%;border-collapse:collapse;"
                        f"border:1px solid {line};margin-top:6px'><tr><th style='{th}'>Location</th><th style='{th}'>Item</th>"
                        f"<th style='{thn}'>Quantity</th><th style='{thn}'>{e(C)}</th><th style='{thn}'>Below zero</th>"
                        f"<th style='{th}'>Why</th></tr>{body}</table>{more}")
    errs = "".join(f"<div style='background:#fdf3e2;color:{amber};padding:7px 11px;border-radius:8px;font-size:12px;margin-bottom:8px'>"
                   f"{e(x)}</div>" for x in r.get("errors") or [])
    empty = (f"<p style='font-size:14px;color:{green};font-weight:600'>No item is below zero at {e(r.get('location') or 'any location')} "
             f"at the end of {d:%d %b %Y}.</p>") if not r["lines"] else ""
    body = f"""<!doctype html><html><head><meta charset="utf-8"></head><body style="margin:0;background:#ecebeb;font-family:Figtree,Segoe UI,Arial,sans-serif;color:{ink}">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#ecebeb"><tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:960px">
<tr><td style="background:{navy};color:#fff;padding:18px 22px;border-radius:12px 12px 0 0">
<div style="font-size:11px;letter-spacing:.12em;color:#a0d8ea">REVO HOSPITALITY GROUP · RHG FINANCE</div>
<div style="font-size:20px;font-weight:700;margin-top:4px">Negative stock by location and item</div>
<div style="font-size:12px;color:#cfe3ea;margin-top:4px">{e(r.get('label') or r['company'])} · stock at the end of {d:%A %d %b %Y}{' · against the report of ' + e(r['previous']['as_of']) if r.get('previous') else ''}</div></td></tr>
<tr><td style="background:#fff;padding:20px 22px;border-radius:0 0 12px 12px">
{errs}{empty}
<h3 style="font-size:14px;color:{ink};margin:0 0 6px">Today</h3>{tiles}
{loc_table}
{''.join(sections)}
<p style="color:{muted};font-size:12px;margin:20px 0 0"><b>How the cause is chosen.</b> An open purchase order line or a transfer shipped and not received for the item and location: receipt not posted (a document, shown). Otherwise the item ledger of the last {HISTORY} days, since the line went below zero: made here beyond what was posted as made, or a negative adjustment: stock manager; used by recipes beyond what came in: recipe wrong; sold as it is, shipped on or returned beyond what was bought: sold without purchase. Read from the ledger, a cause is likely, not certain: check before you correct.</p>
<p style="color:{muted};font-size:12px;margin:8px 0 0"><b>The attached Excel file holds every line</b>, with the open documents, the recipes that use each item and the movements. Values at item unit cost (estimated); quantities from Business Central.</p>
{f"<p style='margin:20px 0 0'><a href='{e(public_url)}/finance/#movement' style='background:{primary};color:#fff;text-decoration:none;padding:10px 18px;border-radius:8px;font-size:13px;font-weight:600;display:inline-block'>Open Movement &amp; dead stock</a></p>" if public_url else ''}
<p style="color:{muted};font-size:11px;margin:22px 0 0;border-top:1px solid {line};padding-top:12px">Sent every day by RHG Control Towers from Business Central; nothing was changed there. Recipients are in Settings → Email recipients by company (Negative stock · daily); the time in Finance → Settings → Negative stock daily email.</p>
</td></tr></table></td></tr></table></body></html>"""
    text = (f"RHG Finance · Negative stock · {r.get('label') or r['company']} · end of {d:%d %b %Y}\n"
            f"{H['lines']} lines below zero ({_m(H['value'])} {C}), {H['new']} new.\n\n"
            + "\n".join(f"- [{OWNER[x['owner']][0]}] {x['location']} {x['item']} {x['desc']}: {_q(x['qty'])} {x['unit']} · {x['why']}"
                        for x in r["lines"][:200])
            + "\n\nThe attached Excel file holds every line.")
    return body, text


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
    sheet("Below zero", ["Owner", "Cause", "Location", "Item", "Description", "Category", "Quantity", "Unit", "Unit cost",
                         "Value", "Below zero since", "Days", "New", "Basis", "Why", "Received 90 d", "Made 90 d",
                         "Used by recipes", "Sold", "Shipped out", "Negative adj.", "Last in", "Held elsewhere"],
          [[OWNER[x["owner"]][0], x["cause"], x["location"], x["item"], x["desc"], x["category"], x["qty"], x["unit"],
            round(x["unit_cost"], 2), round(x["value"]), x["since"] or f"> {HISTORY} days", x["days"], "yes" if x["new"] else "",
            "Document" if x["strength"] == "document" else "Item ledger", x["why"], x["received"], x["made"],
            x["outs"].get("consumption"), x["outs"].get("sale"), x["outs"].get("transfer_out"), x["outs"].get("adj_neg"),
            x["last_in"], ", ".join(f"{L} {q:g}" for L, q in x["elsewhere"])] for x in r["lines"]],
          [None] * 6 + [qty, None, money, money, None, None, None, None, None, qty, qty, qty, qty, qty, qty, None, None],
          [14, 20, 13, 11, 32, 16, 11, 6, 10, 12, 12, 6, 5, 11, 70, 11, 10, 11, 10, 10, 10, 11, 22], first=True)
    sheet("By location", ["Location", "Lines", "Value", *[OWNER[k][1] for k, *_ in OWNERS], "New"],
          [[L["location"], L["lines"], round(L["value"]), *[L[k] for k, *_ in OWNERS], L["new"]] for L in r["locations"]],
          [None, None, money, None, None, None, None], [16, 8, 13, 18, 14, 20, 7])
    sheet("Open documents", ["Location", "Item", "Description", "Document"],
          [[x["location"], x["item"], x["desc"], ev] for x in r["lines"] for ev in x["evidence"]],
          [None] * 4, [14, 11, 32, 90])
    sheet("Recipes using the item", ["Location", "Item", "Description", "Recipe", "Recipe description", "Quantity per",
                                     "Unit", "No conversion", "Sold since below zero", "Expected use"],
          [[x["location"], x["item"], x["desc"], rc["parent"], rc["desc"], rc["per"], rc["unit"],
            "yes" if rc["no_conversion"] else "", rc["sold"], rc["use"]] for x in r["lines"] for rc in x["recipes"]],
          [None] * 5 + [qty, None, None, qty, qty], [14, 11, 30, 11, 30, 11, 7, 10, 12, 12])
    if r.get("resolved"):
        sheet("Back to zero or above", ["Location", "Item", "Description", "Owner yesterday"],
              [[x["location"], x["item"], x["desc"], OWNER[x["owner"]][0]] for x in r["resolved"]], [None] * 4, [14, 11, 32, 16])
    ws = sheet("Read me", ["Negative stock by location and item"], [
        [f"{r.get('label') or r['company']}: stock at the end of {r['as_of']}, item ledger from {r['history_from']}."],
        ["Receipt not posted (Stock manager): an open PO line or a transfer shipped and not received for the item and "
         "location; an item made here used or shipped beyond the output posted; a negative adjustment beyond the stock."],
        ["Recipe wrong (Cost control): below zero through Assembly Consumption: the recipes use more than comes in, or "
         "use an item this location never receives. See the sheet Recipes using the item."],
        ["Sold without purchase (Purchasing): sold as it is, shipped on to other locations or returned beyond what was "
         "bought: bought outside BC or without a PO, or the purchase is not posted."],
        ["Basis: Document = an open purchase order or transfer is the evidence; Item ledger = read from the movements "
         "since the line went below zero, a likely cause to check, not a certain one."],
        ["Values at item unit cost (estimated). Business Central was only read."]], [None], [120])
    for row in ws.iter_rows(min_row=2):
        row[0].alignment = Alignment(wrap_text=True, vertical="top")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------- state, sending, schedule
def _read_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_state(s):
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(s, indent=1, default=str), encoding="utf-8")
    tmp.replace(STATE_FILE)


def recent_runs(limit=15):
    with _state_lock:
        return list(reversed(_read_state().get("runs", [])))[:limit]


def _log(entry):
    with _state_lock:
        s = _read_state()
        s["runs"] = (s.get("runs", []) + [entry])[-100:]
        _write_state(s)


def _wait(fn, what):
    t0 = time.time()
    while True:
        r = fn()
        if not r.get("pending"):
            if r.get("available") is False:
                raise RuntimeError(r.get("error") or f"{what} could not be read.")
            return r
        if time.time() - t0 > WAIT:
            raise RuntimeError(f"{what} was still being read after {WAIT // 60} minutes.")
        time.sleep(10)


class Reporter:
    def __init__(self, client, currency_of, default_company):
        self.client, self.currency_of, self.default_company = client, currency_of, default_company
        self._jobs, self._jobs_lock = {}, threading.Lock()

    @staticmethod
    def settings():
        return store.get_settings().get("negative_report", {})

    def companies(self, s=None):
        s = s or self.settings()
        return [c.strip() for c in (s.get("companies") or "").split(",") if c.strip()] or [self.default_company]

    def recipients(self, s=None):
        s = s or self.settings()
        to, bad = arap_notify.parse_emails(s.get("to"))
        cc, bad2 = arap_notify.parse_emails(s.get("cc"))
        return {"to": to, "cc": [c for c in cc if c not in to], "invalid": bad + bad2}

    @staticmethod
    def as_of(today=None):
        return (today or date.today()) - timedelta(days=1)

    def build(self, company, today=None):
        a = self.as_of(today)
        errors = []
        mv = _wait(lambda: movement.request(self.client, company, a.isoformat(), HISTORY), "Movement & dead stock")
        errors += [f"Movement: {x}" for x in (mv.get("errors") or [])[:5]]
        m = recipes.master_cached(company) or recipes.master(self.client, company)
        docs, derr = open_documents(self.client, company)
        errors += derr
        with _state_lock:
            prev = (_read_state().get("last") or {}).get(company)
        r = analyse(company, mv, m, docs, a, prev)
        r.update(currency=self.currency_of(company), errors=errors, label=company, key=company, location="",
                 previous=prev, built_at=datetime.now().isoformat(timespec="seconds"))
        return r

    @staticmethod
    def _remember(r):
        with _state_lock:
            s = _read_state()
            s.setdefault("last", {})[r["key"]] = {
                "as_of": r["as_of"], "headline": r["headline"],
                "lines": [{k: x[k] for k in ("key", "location", "item", "desc", "owner")} for x in r["lines"]]}
            _write_state(s)

    def _for_location(self, r, loc):
        x = cut(r, loc)
        prev = (r.get("previous") or {})
        if prev:
            pl = [y for y in prev.get("lines") or [] if y["location"] == loc]
            x["previous"] = {"as_of": prev.get("as_of"), "lines": pl,
                             "headline": {"lines": len(pl)}}
        return x

    def send(self, company, r=None, only_to=None, actor="scheduler", test=False, location=None, to=None, cc=None):
        entry = {"ts": datetime.now().isoformat(timespec="seconds"), "company": f"{company} · {location}" if location else company,
                 "actor": actor, "test": test}
        try:
            rec = {"to": only_to, "cc": []} if only_to else self.recipients()
            if location and not only_to:
                rec = {"to": to or [], "cc": cc or []}
            elif not only_to:
                rec = recipients.resolve("negative_daily", [company], rec["to"], rec["cc"])
            if not rec["to"]:
                raise RuntimeError("No recipient is set: Settings → Email recipients by company (Negative stock · daily) "
                                   "or Finance → Settings → Negative stock daily email.")
            r = r or self.build(company)
            if location:
                r = self._for_location(r, location)
            public = (config.PUBLIC_URL or "").rstrip("/")
            try:
                max_lines = int(float(self.settings().get("email_lines") or 0))
            except (TypeError, ValueError):
                max_lines = 50
            body, text = render(r, public, max_lines)
            name = f"RHG_{(r.get('label') or company).replace(' · ', '_').replace(' ', '_')}_negative_stock_{r['as_of']}.xlsx"
            arap_notify.send_mail(rec["to"], rec["cc"], subject(r, test), body, text,
                                  [(name, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", render_xlsx(r))])
            entry.update(status="sent", to=rec["to"], cc=rec["cc"], as_of=r["as_of"],
                         detail=f"{r['headline']['lines']} below zero · {r['headline']['new']} new")
            if not test and not location:
                self._remember(r)
        except Exception as exc:                                   # noqa: BLE001
            traceback.print_exc()
            entry.update(status="failed", detail=str(exc)[:300])
        _log(entry)
        store.audit("email", f"negative stock daily · {entry['company']}", actor, {},
                    {k: entry.get(k) for k in ("status", "to", "cc", "test")}, entry.get("detail", ""))
        return entry

    # ---- preview and test in the background
    def start_job(self, company, actor, only_to=None):
        with self._jobs_lock:
            for j in self._jobs.values():
                if j["actor"] == actor and j["status"] == "running":
                    return {k: v for k, v in j.items() if k != "html"}
            self._jobs = {k: v for k, v in list(self._jobs.items())[-20:]}
            job = {"id": uuid.uuid4().hex, "actor": actor, "company": company, "to": only_to or [], "status": "running",
                   "detail": "", "html": None, "started": datetime.now().isoformat(timespec="seconds")}
            self._jobs[job["id"]] = job

        def go():
            try:
                r = self.build(company)
                try:
                    max_lines = int(float(self.settings().get("email_lines") or 0))
                except (TypeError, ValueError):
                    max_lines = 50
                job["html"] = render(r, (config.PUBLIC_URL or "").rstrip("/"), max_lines)[0]
                if only_to:
                    res = self.send(company, r, only_to, actor, test=True)
                    if res["status"] != "sent":
                        raise RuntimeError(res.get("detail") or "not sent")
                    job["detail"] = "Sent to " + ", ".join(only_to)
                else:
                    job["detail"] = f"{r['headline']['lines']} lines below zero · {r['headline']['new']} new"
                job["status"] = "done"
            except Exception as exc:                               # noqa: BLE001
                traceback.print_exc()
                job.update(status="failed", detail=str(exc)[:300])
        threading.Thread(target=go, daemon=True, name="negative-report-job").start()
        return {k: v for k, v in job.items() if k != "html"}

    def job(self, job_id, actor):
        with self._jobs_lock:
            j = self._jobs.get(job_id)
            return j if j and j["actor"] == actor else None

    # ---- the daily schedule
    @staticmethod
    def _when(s):
        hh, _, mm = (s.get("time") or "07:30").partition(":")
        return DAYS.get(s.get("days") or "daily", DAYS["daily"]), int(hh or 7), int(mm or 0)

    def next_run(self, now=None):
        s = self.settings()
        if not s.get("enabled"):
            return None
        now = now or datetime.now()
        days, hh, mm = self._when(s)
        with _state_lock:
            done = _read_state().get("done")
        for k in range(8):
            d = now.date() + timedelta(days=k)
            if d.weekday() not in days:
                continue
            t = datetime(d.year, d.month, d.day, hh, mm)
            if t + timedelta(hours=10) >= now and done != d.isoformat():
                return t.isoformat(timespec="minutes")
        return None

    def due(self, now):
        s = self.settings()
        if not s.get("enabled"):
            return None
        days, hh, mm = self._when(s)
        if now.weekday() not in days:
            return None
        start = datetime(now.year, now.month, now.day, hh, mm)
        if not (start <= now <= start + timedelta(hours=10)):
            return None
        day = now.date().isoformat()
        with _state_lock:
            st = _read_state()
        if st.get("done") == day:
            return None
        tries = st.get("tries", {})
        if tries.get("day") == day and (tries.get("count", 0) >= 3 or time.time() - tries.get("at", 0) < 1800):
            return None
        return day

    def run(self, day):
        with _run_lock:
            results = []
            for c in self.companies():
                r = None
                try:
                    r = self.build(c)
                except Exception:                                  # noqa: BLE001
                    traceback.print_exc()
                results.append(self.send(c, r))
                for loc, to, cc in recipients.location_targets("negative_daily", c):
                    if r is None:
                        break
                    results.append(self.send(c, r, location=loc, to=to, cc=cc))
            with _state_lock:
                st = _read_state()
                if all(x["status"] == "sent" for x in results):
                    st["done"] = day
                    st.pop("tries", None)
                else:
                    t = st.get("tries", {})
                    count = t.get("count", 0) + 1 if t.get("day") == day else 1
                    st["tries"] = {"day": day, "count": count, "at": time.time()}
                    if count >= 3:
                        st["done"] = day
                _write_state(st)
            return results

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
        threading.Thread(target=self.loop, daemon=True, name="negative-report").start()
