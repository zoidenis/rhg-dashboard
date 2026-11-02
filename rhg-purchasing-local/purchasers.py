"""Purchaser performance: each purchaser (the Purchaser Code of Business Central) and the suppliers, orders,
invoices and prices that are theirs.

Who a document belongs to: the Purchaser Code on the document itself; when the document has none (orders raised
before the supplier had a purchaser, invoices), the Purchaser Code on the supplier's card. A supplier without one
goes to "No purchaser", which is a list to clear in Business Central, not a person.

What is measured, for a period chosen on the screen (invoices, prices) and today (orders, payables):
- Spend: posted purchase invoices of the period, excluding VAT (cancelled and corrective ones left out). Invoices
  in another currency are kept apart, never added to the local currency.
- Orders first: the share of that spend whose invoice carries an order number; the rest is invoiced without a PO.
- Price effect: on the item lines of the period against the same dates last year, (this year's average unit cost
  - last year's) x this year's quantity, on what was bought from the purchaser's suppliers (savings.py).
- Open orders today, each at one step: Open (not released), Released (nothing received), Partly received,
  Received not invoiced, Invoiced but still open. An order is late at its step after the days set in Settings:
  not released after "release" days; not received by the expected receipt date (or after "receive" days from the
  order date when no date is set); not invoiced after "receive" + "invoice" days from the order date (the date of
  the receipt is not on the order).
- Payables: the balance and the balance due on the cards of the purchaser's suppliers.
Business Central is only read.
"""
import io
from datetime import date, timedelta

import savings
import savings_report
import store

NONE_LABEL = "No purchaser"
STAGES = [("open", "Open, not released"), ("released", "Released, nothing received"),
          ("partial", "Partly received"), ("to_invoice", "Received, not invoiced"),
          ("to_close", "Invoiced, still open")]


def _f(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _d(v):
    try:
        return date.fromisoformat(str(v)[:10]) if v and not str(v).startswith("0001") else None
    except ValueError:
        return None


def parse_map(text):
    """"PM-BESNIK=Besnik, PM-ERJON=Erjon" -> {"PM-BESNIK": "Besnik", ...}"""
    out = {}
    for part in str(text or "").replace(";", ",").replace("\n", ",").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip():
                out[k.strip().upper()] = v.strip()
    return out


def label(code, names):
    """The name set in Settings, else the code made readable ("PM-BESNIK" -> "Besnik")."""
    if not code:
        return NONE_LABEL
    if names.get(code.upper()):
        return names[code.upper()]
    bare = code.split("-", 1)[1] if code.upper().startswith("PM-") and len(code) > 3 else code
    return " ".join(w.capitalize() for w in bare.replace("_", " ").replace(".", " ").split()) or code


def stage_of(o):
    if (o.get("status") or "").lower() == "open":
        return "open"
    if o["qty_received"] <= 0 and o["received"] <= 0:
        return "released"
    if o["qty_received"] < o["qty"] - 1e-9:
        return "partial"
    if o["received_not_invoiced"] > 0.5 or o["qty_invoiced"] < o["qty_received"] - 1e-9:
        return "to_invoice"
    return "to_close"


def late_reason(o, stage, age, today, cfg):
    exp = _d(o.get("expected"))
    if stage == "open" and age is not None and age > cfg["release_days"]:
        return f"Not released after {age} days"
    if stage in ("released", "partial"):
        if exp and exp < today:
            return f"Expected {exp.isoformat()}, {(today - exp).days} days ago"
        if not exp and age is not None and age > cfg["receive_days"]:
            return f"Nothing {'more ' if stage == 'partial' else ''}received after {age} days, no expected date"
    if stage == "to_invoice" and age is not None and age > cfg["receive_days"] + cfg["invoice_days"]:
        return f"Received, not invoiced, {age} days after the order"
    if stage == "to_close" and age is not None and age > cfg["receive_days"] + cfg["invoice_days"]:
        return "Invoiced in full but the order is still open"
    return ""


def _blank(code, names):
    return {"code": code, "name": label(code, names), "vendors_assigned": 0, "vendors_active": 0,
            "spend": 0.0, "spend_fcy": {}, "invoices": 0, "with_po": 0.0, "without_po": 0.0, "without_po_n": 0,
            "orders": 0, "orders_value": 0.0, "orders_fcy": {}, "rni": 0.0, "outstanding": 0.0, "late": 0,
            "late_value": 0.0, "no_expected": 0, "oldest": None, "age_sum": 0, "stages": {k: 0 for k, _ in STAGES},
            "owed": 0.0, "due": 0.0, "save": None, "extra": None, "net": None, "price_items": 0, "matched": None}


def build(client, company, a, b, today=None, settings=None):
    today = today or date.today()
    settings = settings or store.get_settings()
    pc = settings.get("purchasers") or {}
    cfg = {k: int(_f(pc.get(k, d))) for k, d in (("release_days", 2), ("receive_days", 7), ("invoice_days", 3))}
    names, emails = parse_map(pc.get("names")), parse_map(pc.get("emails"))
    errors = []

    try:
        cards = client.vendor_cards(company)
    except Exception as exc:                                       # noqa: BLE001
        cards, _ = [], errors.append(f"Vendor cards: {str(exc)[:200]}")
    vcode = {str(c.get("No")): (c.get("Purchaser_Code") or "").upper() for c in cards}
    vname = {str(c.get("No")): c.get("Name") or str(c.get("No")) for c in cards}
    try:
        orders = client.purchase_order_cards(company)
    except Exception as exc:                                       # noqa: BLE001
        orders, _ = [], errors.append(f"Purchase orders: {str(exc)[:200]}")
    try:
        invoices = client.posted_invoices(company, a.isoformat(), b.isoformat())
    except Exception as exc:                                       # noqa: BLE001
        invoices, _ = [], errors.append(f"Posted invoices: {str(exc)[:200]}")

    P, V = {}, {}

    def P_(code):
        return P.setdefault(code, _blank(code, names))

    def V_(no, owner):
        v = V.setdefault(no, {"no": no, "name": vname.get(no, no), "owner": owner, "card_code": vcode.get(no, ""),
                              "spend": 0.0, "spend_fcy": {}, "invoices": 0, "without_po": 0.0, "without_po_n": 0,
                              "orders": 0, "orders_value": 0.0, "late": 0, "owed": 0.0, "due": 0.0, "net": None})
        return v

    for c in cards:
        code = (c.get("Purchaser_Code") or "").upper()
        no = str(c.get("No"))
        if code:
            P_(code)["vendors_assigned"] += 1
        owed, due = _f(c.get("Balance_LCY")), _f(c.get("Balance_Due_LCY"))
        if abs(owed) > 0.5 or abs(due) > 0.5:
            p = P_(code)
            p["owed"] += owed
            p["due"] += due
            v = V_(no, code)
            v["owed"], v["due"] = owed, due

    # invoices of the period
    nopo = []
    for h in invoices:
        if h.get("Cancelled") or h.get("Corrective"):
            continue
        no = str(h.get("Buy_from_Vendor_No") or "")
        own = (h.get("Purchaser_Code") or "").upper() or vcode.get(no, "")
        amt, cur = _f(h.get("Amount")), (h.get("Currency_Code") or "")
        p, v = P_(own), V_(no, own)
        if no not in vname:
            v["name"] = h.get("Buy_from_Vendor_Name") or no
        p["invoices"] += 1
        v["invoices"] += 1
        if cur:
            p["spend_fcy"][cur] = p["spend_fcy"].get(cur, 0.0) + amt
            v["spend_fcy"][cur] = v["spend_fcy"].get(cur, 0.0) + amt
            continue
        p["spend"] += amt
        v["spend"] += amt
        if h.get("Order_No"):
            p["with_po"] += amt
        else:
            p["without_po"] += amt
            p["without_po_n"] += 1
            v["without_po"] += amt
            v["without_po_n"] += 1
            nopo.append({"no": h.get("No"), "vendor_invoice": h.get("Vendor_Invoice_No") or "",
                         "vendor_no": no, "vendor": v["name"], "owner": own, "date": str(h.get("Posting_Date"))[:10],
                         "location": h.get("Location_Code") or h.get("Shortcut_Dimension_1_Code") or "",
                         "amount": round(amt, 2)})
    nopo.sort(key=lambda x: -x["amount"])

    # open orders today
    rows, mismatched, from_vendor, no_expected = [], 0, 0, 0
    for o in orders:
        doc = (o.get("purchaser") or "").upper()
        card = vcode.get(o["vendor_no"], "")
        own = doc or card
        src = "document" if doc else "vendor" if card else "none"
        if doc and card and doc != card:
            mismatched += 1
        if not doc and card:
            from_vendor += 1
        od = _d(o.get("order_date"))
        age = (today - od).days if od else None
        st = stage_of(o)
        why = late_reason(o, st, age, today, cfg)
        p, v = P_(own), V_(o["vendor_no"], own)
        if o["vendor_no"] not in vname:
            v["name"] = o.get("vendor") or o["vendor_no"]
        p["orders"] += 1
        v["orders"] += 1
        p["stages"][st] += 1
        if o.get("currency"):
            p["orders_fcy"][o["currency"]] = p["orders_fcy"].get(o["currency"], 0.0) + o["amount"]
        else:
            p["orders_value"] += o["amount"]
            v["orders_value"] += o["amount"]
            p["rni"] += o["received_not_invoiced"]
            p["outstanding"] += o["outstanding"]
        if age is not None:
            p["age_sum"] += age
            p["oldest"] = max(p["oldest"] or 0, age)
        if not o.get("expected") and st in ("released", "partial"):
            p["no_expected"] += 1
            no_expected += 1
        if why:
            p["late"] += 1
            v["late"] += 1
            if not o.get("currency"):
                p["late_value"] += o["outstanding"] if st in ("released", "partial") else o["amount"]
        rows.append({"no": o["no"], "vendor_no": o["vendor_no"], "vendor": v["name"], "owner": own, "source": src,
                     "doc_code": doc, "card_code": card, "status": o.get("status"), "stage": st, "late": why,
                     "location": o.get("location") or "", "order_date": o.get("order_date") or "", "age": age,
                     "expected": o.get("expected") or "", "currency": o.get("currency") or "",
                     "amount": o["amount"], "received": o["received"], "invoiced": o["invoiced"],
                     "outstanding": o["outstanding"], "rni": o["received_not_invoiced"], "lines": o["line_count"],
                     "assigned_user": o.get("assigned_user") or ""})
    rows.sort(key=lambda r: (not r["late"], -(r["age"] or 0)))

    # price effect against the same dates last year, on what each purchaser's suppliers sold
    months = savings.needed_months(a, b)
    missing = savings.missing_months(company, months)
    prices = {"ready": not missing, "progress": None, "note": ""}
    movers = {}
    if missing:
        job = savings_report._start_load(client, company, sorted(missing, reverse=True), False)
        prices.update(progress={"done": job.get("done", 0), "total": job.get("total", 0)},
                      note="Reading the purchase invoice lines of these months and the same months last year "
                           "(read once, then kept).")
    else:
        la, lb = savings.last_year(a), savings.last_year(b)
        cur_rows, ly_rows = savings.rows(company, a, b), savings.rows(company, la, lb)
        item_names = savings.names(company, "item")
        by_owner = {}
        for tag, src_rows in (("cur", cur_rows), ("ly", ly_rows)):
            for loc, ven, item, c, q, n in src_rows:
                own = vcode.get(str(ven), "")
                d = by_owner.setdefault(own, {"cur": {}, "ly": {}, "vend": {}})[tag]
                x = d.setdefault(item, [0.0, 0.0])
                x[0] += c or 0
                x[1] += q or 0
                if tag == "cur":
                    vv = by_owner[own]["vend"].setdefault(str(ven), ({}, {}))
                    y = vv[0].setdefault(item, [0.0, 0.0])
                    y[0] += c or 0
                    y[1] += q or 0
                else:
                    vv = by_owner[own]["vend"].setdefault(str(ven), ({}, {}))
                    y = vv[1].setdefault(item, [0.0, 0.0])
                    y[0] += c or 0
                    y[1] += q or 0
        for own, d in by_owner.items():
            t, detail = savings.effect(d["cur"], d["ly"])
            p = P_(own)
            p.update(save=round(t["save"]), extra=round(t["extra"]), net=round(t["net"]),
                     price_items=t["items"], matched=round(t["matched"]))
            ms = []
            for it, x in detail.items():
                if x[0] != "both":
                    continue
                q = d["cur"][it][1]
                ms.append({"item": it, "desc": item_names.get(it, ""), "now": round(x[1], 2), "ly": round(x[2], 2),
                           "pct": round((x[1] - x[2]) / x[2] * 100, 1) if x[2] else None, "qty": round(q, 2),
                           "effect": round(x[3])})
            ms.sort(key=lambda m: -abs(m["effect"]))
            movers[own] = ms[:25]
            for ven, (vc, vl) in d["vend"].items():
                if ven in V or vc:
                    tv, _ = savings.effect(vc, vl)
                    V_(ven, own)["net"] = round(tv["net"])

    # finish
    out = []
    total = _blank("*", names)
    total["name"] = "All purchasers"
    for code, p in P.items():
        p["vendors_active"] = sum(1 for v in V.values() if v["owner"] == code and (v["spend"] or v["spend_fcy"]
                                                                                    or v["orders"]))
        p["avg_age"] = round(p["age_sum"] / p["orders"]) if p["orders"] else None
        p["po_share"] = round(p["with_po"] / p["spend"] * 100, 1) if p["spend"] > 0 else None
        p["email"] = emails.get(code, "") if code else ""
        for k in ("spend", "with_po", "without_po", "orders_value", "rni", "outstanding", "late_value", "owed", "due"):
            p[k] = round(p[k])
        for k in ("vendors_assigned", "vendors_active", "invoices", "without_po_n", "orders", "late", "no_expected",
                  "spend", "with_po", "without_po", "orders_value", "rni", "outstanding", "late_value", "owed", "due"):
            total[k] += p[k]
        for st in p["stages"]:
            total["stages"][st] += p["stages"][st]
        for k in ("save", "extra", "net", "matched"):
            if p[k] is not None:
                total[k] = (total[k] or 0) + p[k]
        total["price_items"] += p["price_items"]
        if p["oldest"] is not None:
            total["oldest"] = max(total["oldest"] or 0, p["oldest"])
        total["age_sum"] += p["age_sum"]
        out.append(p)
    total["po_share"] = round(total["with_po"] / total["spend"] * 100, 1) if total["spend"] > 0 else None
    total["avg_age"] = round(total["age_sum"] / total["orders"]) if total["orders"] else None
    total["spend_share"] = 100.0 if total["spend"] else None
    for p in out:
        p["spend_share"] = round(p["spend"] / total["spend"] * 100, 1) if total["spend"] > 0 else None
    out.sort(key=lambda p: (p["code"] == "", -p["spend"], -p["orders_value"]))
    vendors = sorted((v for v in V.values() if v["spend"] or v["spend_fcy"] or v["orders"] or abs(v["owed"]) > 0.5),
                     key=lambda v: (-v["spend"], -v["orders_value"], -v["owed"]))
    for v in vendors:
        for k in ("spend", "without_po", "orders_value", "owed", "due"):
            v[k] = round(v[k])

    unknown = sorted({c for c in vcode.values() if c} - set(names)) if names else []
    active_uncoded = [v for v in vendors if not v["card_code"] and (v["spend"] or v["spend_fcy"] or v["orders"])]
    quality = {"orders_from_vendor": from_vendor, "orders_mismatch": mismatched, "orders_no_expected": no_expected,
               "vendors_uncoded_active": len(active_uncoded),
               "vendors_uncoded_spend": round(sum(v["spend"] for v in active_uncoded)),
               "vendors_with_code": sum(1 for c in vcode.values() if c), "vendors_total": len(vcode),
               "unknown_codes": unknown,
               "mismatches": [r for r in rows if r["doc_code"] and r["card_code"] and r["doc_code"] != r["card_code"]][:50]}
    return {"company": company, "period": {"from": a.isoformat(), "to": b.isoformat()},
            "last_year": {"from": savings.last_year(a).isoformat(), "to": savings.last_year(b).isoformat()},
            "today": today.isoformat(), "settings": cfg, "purchasers": out, "total": total,
            "orders": rows, "vendors": vendors[:600], "nopo": nopo[:300], "movers": movers, "prices": prices,
            "quality": quality, "stages": [{"key": k, "label": l} for k, l in STAGES], "errors": errors,
            "definitions": DEFINITIONS}


DEFINITIONS = {
    "owner": "A document belongs to the Purchaser Code on it; when it has none, to the Purchaser Code on the "
             "supplier's card; without either, to \"No purchaser\".",
    "spend": "Posted purchase invoices of the period, excluding VAT; cancelled and corrective invoices left out. "
             "Invoices in another currency are shown apart and not added.",
    "po_share": "Share of the spend whose invoice carries a purchase order number.",
    "price": "Item lines of the period against the same dates last year: (average unit cost now - last year) x "
             "quantity now, on what the purchaser's suppliers sold. Negative is a saving.",
    "orders": "Open purchase orders today, whatever their date, each at one step. Values exclude VAT.",
    "late": "Not released after the release days; not received by the expected receipt date (or after the receive "
            "days when no date is set); not invoiced after receive + invoice days from the order date.",
    "payables": "Balance and balance due on the cards of the purchaser's suppliers, today.",
}


def request(client, company, date_from, date_to):
    a, b = savings_report.parse_range(date_from, date_to)
    return build(client, company, a, b)


def render_xlsx(r, cur):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    fill, font = PatternFill("solid", fgColor="E4F3F9"), Font(bold=True, color="0E2733")
    money, pct = '#,##0;[Red]-#,##0', '0.0'
    stage_label = dict(STAGES)

    def sheet(ws, header, rows_in, money_cols=(), widths=None):
        ws.append(header)
        for c in ws[1]:
            c.fill, c.font = fill, font
            c.alignment = Alignment(wrap_text=True, vertical="top")
        for row in rows_in:
            ws.append(row)
        for i in money_cols:
            for cell in ws.iter_rows(min_row=2, min_col=i, max_col=i):
                cell[0].number_format = money
        for i, w in enumerate(widths or [], start=1):
            ws.column_dimensions[chr(64 + i) if i <= 26 else "A"].width = w
        ws.freeze_panes = "A2"

    ws = wb.active
    ws.title = "Purchasers"
    p, l = r["period"], r["last_year"]
    rows_ = []
    for x in r["purchasers"] + [r["total"]]:
        rows_.append([x["name"], x["code"] if x["code"] != "*" else "", x["vendors_assigned"], x["vendors_active"],
                      x["spend"], x["spend_share"], x["invoices"], x["po_share"], x["without_po"], x["without_po_n"],
                      x["save"], x["extra"], x["net"], x["orders"], x["orders_value"], x["late"], x["late_value"],
                      x["rni"], x["avg_age"], x["oldest"], x["owed"], x["due"]])
    sheet(ws, ["Purchaser", "Code", "Suppliers (card)", "Suppliers active", f"Spend {cur}", "Share of spend %",
               "Invoices", "With PO %", f"Without PO {cur}", "Invoices without PO", f"Price savings {cur}",
               f"Price increases {cur}", f"Price effect net {cur}", "Open orders", f"Open orders {cur}",
               "Late orders", f"Late value {cur}", f"Received not invoiced {cur}", "Average age (days)",
               "Oldest (days)", f"Owed to suppliers {cur}", f"Due {cur}"], rows_,
          money_cols=(5, 9, 11, 12, 13, 15, 17, 18, 21, 22), widths=[22, 13, 10, 10, 14, 9, 9, 9, 14, 10, 13, 13, 13,
                                                                       9, 14, 9, 13, 14, 9, 9, 15, 13])
    ws.insert_rows(1, 2)
    ws["A1"] = f"Purchaser performance · {r['company']} · {p['from']} to {p['to']} (prices against {l['from']} " \
               f"to {l['to']}) · open orders and payables on {r['today']}"
    ws["A1"].font = Font(bold=True, size=12)
    ws.freeze_panes = "A4"

    names = {x["code"]: x["name"] for x in r["purchasers"]}
    ws = wb.create_sheet("Open orders")
    sheet(ws, ["Order", "Purchaser", "From", "Supplier no.", "Supplier", "Location", "Status", "Step", "Late",
               "Order date", "Age (days)", "Expected", "Currency", f"Amount {cur}", f"Received {cur}",
               f"Invoiced {cur}", f"Still to receive {cur}", f"Received not invoiced {cur}"],
          [[o["no"], names.get(o["owner"], NONE_LABEL), o["source"], o["vendor_no"], o["vendor"], o["location"],
            o["status"], stage_label.get(o["stage"], o["stage"]), o["late"], o["order_date"], o["age"], o["expected"],
            o["currency"] or cur, o["amount"], o["received"], o["invoiced"], o["outstanding"], o["rni"]]
           for o in r["orders"]], money_cols=(14, 15, 16, 17, 18),
          widths=[10, 16, 9, 10, 28, 11, 9, 22, 34, 11, 8, 11, 8, 13, 13, 13, 13, 13])
    ws = wb.create_sheet("Suppliers")
    sheet(ws, ["Supplier no.", "Supplier", "Purchaser", "Code on card", f"Spend {cur}", "Other currencies",
               "Invoices", f"Without PO {cur}", "Open orders", f"Open orders {cur}", "Late orders",
               f"Price effect {cur}", f"Owed {cur}", f"Due {cur}"],
          [[v["no"], v["name"], names.get(v["owner"], NONE_LABEL), v["card_code"], v["spend"],
            ", ".join(f"{k} {x:,.0f}" for k, x in v["spend_fcy"].items()), v["invoices"], v["without_po"],
            v["orders"], v["orders_value"], v["late"], v["net"], v["owed"], v["due"]] for v in r["vendors"]],
          money_cols=(5, 8, 10, 12, 13, 14), widths=[10, 30, 16, 12, 14, 16, 9, 13, 9, 13, 9, 13, 14, 13])
    ws = wb.create_sheet("Invoices without PO")
    sheet(ws, ["Invoice", "Vendor invoice", "Purchaser", "Supplier no.", "Supplier", "Posting date", "Location",
               f"Amount {cur}"],
          [[n["no"], n["vendor_invoice"], names.get(n["owner"], NONE_LABEL), n["vendor_no"], n["vendor"], n["date"],
            n["location"], n["amount"]] for n in r["nopo"]], money_cols=(8,), widths=[11, 14, 16, 10, 30, 11, 11, 14])
    ws = wb.create_sheet("Price changes")
    sheet(ws, ["Purchaser", "Item", "Description", "Avg cost now", "Avg cost last year", "Change %", "Quantity now",
               f"Price effect {cur}"],
          [[names.get(k, NONE_LABEL), m["item"], m["desc"], m["now"], m["ly"], m["pct"], m["qty"], m["effect"]]
           for k, ms in r["movers"].items() for m in ms], money_cols=(8,), widths=[16, 11, 34, 12, 12, 9, 11, 13])
    ws = wb.create_sheet("How it is measured")
    for k, v in r["definitions"].items():
        ws.append([k, v])
    ws.column_dimensions["A"].width, ws.column_dimensions["B"].width = 14, 120
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
