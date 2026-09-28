"""Normalizes Business Central responses into application objects:
documents, operational events, PO backlog, supplier links and SLA exceptions.

Only fields that the MCP/OData layer actually returns are used. Anything a KPI
needs but BC does not expose is reported in DATA_GAPS instead of being estimated.
"""
from collections import defaultdict
from datetime import date, datetime, timedelta
import currency

INV, CM, RCPT = "Purchase Invoice", "Purchase Credit Memo", "Purchase Receipt"

# Capabilities the memo asks for that the published BC web services do not carry.
REQUISITION_GAP = {
    "kpi": "Purchase requisitions created / requisition-to-PO time",
    "missing": "The requisition worksheet is not published as a web service, so open requisitions cannot be read.",
    "requirement": "In BC: Web Services > New > Object Type Page > pick 'Req. Worksheet' > Service Name RequisitionLines > Published. "
                   "BC deletes a worksheet line once it is carried out into an order, so the app timestamps every line it sees "
                   "and measures how long it stayed open from its own history.",
}

CONTRACT_PRICE_GAP = {
    "kpi": "Expected or contracted supplier price",
    "missing": "No purchase price lines could be read, so paid prices cannot be checked against an agreed price.",
    "requirement": "RHG keeps its negotiated prices in the purchase price lists (P00001 and the rest), and those live "
                   "in the Price List Line table, not in the old Purchase Price table. Publishing the 'Purchase Prices' "
                   "page therefore answers with zero rows. In BC: Web Services > New > Object Type Page > Object ID "
                   "lookup > choose the page named 'Price List Lines' > Service Name PriceListLines > Published. The app "
                   "then reads the purchase lines that are active today and flags anything paid above the agreed price "
                   "by more than the tolerance in Settings.",
}

DATA_GAPS = [
    REQUISITION_GAP,
    CONTRACT_PRICE_GAP,
    {"kpi": "Approval status, approver, time waiting for approval",
     "missing": "Approval Entries (page 658) is not published, and PurchaseOrderList shows no 'Pending Approval' status.",
     "requirement": "Publish 'Approval Entries' with Document No., Approver ID, Status, Date-Time Sent for Approval."},
    {"kpi": "Buyer / responsible user on a purchase order",
     "missing": "Purchaser_Code and Assigned_User_ID are empty on every PurchaseOrderList row.",
     "requirement": "Populate Purchaser Code or Assigned User ID on purchase documents in BC."},
    {"kpi": "Document created / modified timestamps, reopened or corrected documents",
     "missing": "Purchase headers expose Document_Date only; no SystemCreatedAt / SystemModifiedAt in the published service.",
     "requirement": "Add SystemCreatedAt, SystemCreatedBy, SystemModifiedAt to the PurchaseOrderList web service."},
    {"kpi": "Unit of measure and item category on purchase value entries",
     "missing": "ValueEntries carries no UOM or item category field, so unit normalization cannot be verified.",
     "requirement": "Add Unit of Measure Code and Item Category Code to the ValueEntries query (or publish Purch. Inv. Line)."},
    {"kpi": "Contracted / approved price list and approved supplier list",
     "missing": "No purchase price or vendor agreement web service is published.",
     "requirement": "Publish 'Purchase Prices' (page 7012) and the approved vendor list."},
    {"kpi": "Delivery performance (promised vs actual receipt)",
     "missing": "Requested_Receipt_Date is blank (0001-01-01) on the purchase orders returned.",
     "requirement": "Fill Requested/Promised Receipt Date on purchase orders, or publish Purch. Rcpt. Header with expected dates."},
    {"kpi": "Line-level purchase price with supplier on open orders",
     "missing": "PurchaseLineBI exposes no unit price and no vendor; supplier is only linked after posting.",
     "requirement": "Extend the PurchaseLineBI query with Buy-from Vendor No., Direct Unit Cost and Line Amount."},
]


def _d(s):
    try:
        v = date.fromisoformat((s or "")[:10])
        return None if v.year < 1900 else v
    except ValueError:
        return None


def _f(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def working_days_between(start, end, exclude_weekends=True):
    if not start or not end:
        return None
    if not exclude_weekends:
        return (end - start).days
    days, cur = 0, start
    step = 1 if end >= start else -1
    while cur != end:
        cur += timedelta(days=step)
        if cur.weekday() < 5:
            days += step
    return days


def supplier_index(vle_rows):
    """Document_No -> supplier, taken from the vendor ledger (the only place vendor is exposed)."""
    idx = {}
    for r in vle_rows:
        if r.get("Document_Type") in ("Invoice", "Credit Memo"):
            idx[str(r.get("Document_No"))] = {
                "vendor_no": r.get("Vendor_No"), "vendor": r.get("Vendor_Name") or r.get("Vendor_No"),
                "amount": -_f(r.get("Amount_LCY")), "posting_date": r.get("Posting_Date"),
                "document_date": r.get("Document_Date"), "type": r.get("Document_Type"),
            }
    return idx


def documents(ve_rows, suppliers, registers_by_entry=None):
    """Groups value entries into posted documents and attaches supplier + posting user."""
    docs = {}
    for r in ve_rows:
        key = (r.get("Document_Type"), str(r.get("Document_No")))
        d = docs.get(key)
        if d is None:
            sup = suppliers.get(key[1], {})
            d = docs[key] = {"type": key[0], "no": key[1], "lines": 0, "value": 0.0, "qty": 0.0,
                             "neg_lines": 0, "zero_lines": 0, "no_location": 0, "locations": set(),
                             "posting": r.get("Posting_Date"), "document_date": r.get("Document_Date"),
                             "supplier": sup.get("vendor", ""), "vendor_no": sup.get("vendor_no", ""),
                             "entries": [], "items": set(), "user": ""}
        d["lines"] += 1
        actual = _f(r.get("Cost_Amount_Actual"))
        d["value"] += actual + (_f(r.get("Cost_Amount_Expected")) if d["type"] == RCPT else 0)
        d["qty"] += _f(r.get("Item_Ledger_Entry_Quantity"))
        if d["type"] == INV and actual < 0:
            d["neg_lines"] += 1
        if d["type"] == INV and actual == 0:
            d["zero_lines"] += 1
        if r.get("Location_Code"):
            d["locations"].add(r["Location_Code"])
        else:
            d["no_location"] += 1
        if r.get("Item_No"):
            d["items"].add(r["Item_No"])
        d["entries"].append(r.get("Entry_No") or 0)
    if registers_by_entry:
        for d in docs.values():
            d["user"] = registers_by_entry(max(d["entries"])) or ""
    return docs


def requisitions(lines, hist, settings):
    """Open requisition worksheet lines with the time each has been waiting."""
    th = settings["thresholds"]
    out = []
    for l in lines:
        h = hist.get(l["key"], {})
        hours = h.get("hours_open")
        severity = ("critical" if hours is not None and hours >= th["requisition_critical_hours"]
                    else "warning" if hours is not None and hours >= th["requisition_warning_hours"] else "")
        out.append({**l, "first_seen": h.get("first_seen"), "hours_open": hours, "severity": severity,
                    "value": _f(l.get("quantity")) * _f(l.get("unit_cost"))})
    out.sort(key=lambda r: (r["hours_open"] is None, -(r["hours_open"] or 0)))
    return out


def requisition_exceptions(reqs, settings):
    th = settings["thresholds"]
    out = []
    for r in reqs:
        if not r["severity"]:
            continue
        out.append({
            "code": f"req-{r['key']}", "rule": "Requisition not converted into an order",
            "severity": r["severity"],
            "title": f"Requisition {r['description'] or r['item']}: open {r['hours_open']:.0f}h",
            "detail": f"{_f(r['quantity']):,.2f} {r['uom'] or 'units'} for {r['location'] or 'no location'}"
                      f"{', supplier ' + r['vendor'] if r['vendor'] else ''}, waiting since {r['first_seen']} "
                      f"(warning {th['requisition_warning_hours']:.0f}h, critical {th['requisition_critical_hours']:.0f}h).",
            "impact": r["value"], "owner": settings["owners"].get("requisitions", ""),
            "document": r["item"], "document_type": "Requisition", "supplier": r["vendor"],
            "location": r["location"], "drill": {"kind": "requisitions", "key": ""},
            "next_action": "Convert it into a purchase order or clear the line.",
        })
    return out


def item_averages(ve_rows):
    """Average paid unit cost per item in the period, from posted value entries."""
    agg = defaultdict(lambda: {"cost": 0.0, "qty": 0.0, "desc": "", "locations": set()})
    for r in ve_rows:
        a = agg[r.get("Item_No")]
        a["cost"] += _f(r.get("Cost_Amount_Actual")) + _f(r.get("Cost_Amount_Expected"))
        a["qty"] += _f(r.get("Item_Ledger_Entry_Quantity"))
        if r.get("Item_Description"):
            a["desc"] = r["Item_Description"].strip()
        if r.get("Location_Code"):
            a["locations"].add(r["Location_Code"])
    return {k: {"avg": v["cost"] / v["qty"], "qty": v["qty"], "cost": v["cost"], "desc": v["desc"],
                "locations": sorted(v["locations"])}
            for k, v in agg.items() if v["qty"] > 0 and v["cost"] > 0}


def contract_exceptions(items, prices, settings):
    """Flags items paid above the contracted purchase price beyond the tolerance."""
    if not prices:
        return [], []
    tol = settings["thresholds"].get("price_above_contract_pct", 2)
    rows, out = [], []
    for item_no, a in items.items():
        c = prices.get(str(item_no))
        if not c or not c.get("price"):
            continue
        pct = (a["avg"] - c["price"]) / c["price"] * 100
        over = (a["avg"] - c["price"]) * a["qty"]
        rows.append({"item_no": item_no, "desc": a["desc"], "paid": a["avg"], "contract": c["price"],
                     "pct": pct, "overpaid": over, "qty": a["qty"], "vendor": c.get("vendor", ""),
                     "locations": a["locations"]})
        if pct > tol:
            out.append({
                "code": f"contract-{item_no}", "rule": "Paid above the contracted price",
                "severity": "critical" if pct > tol * 3 else "warning",
                "title": f"{a['desc'] or item_no}: paid {pct:+.1f}% above the agreed price",
                "detail": f"Agreed {c['price']:,.2f}, paid {a['avg']:,.2f} {currency.code()} on {a['qty']:,.2f} units "
                          f"({over:+,.0f} {currency.code()}). Tolerance {tol:.0f}%.",
                "impact": over, "owner": settings["owners"].get("price", ""),
                "document": item_no, "document_type": "Item", "supplier": c.get("vendor", ""),
                "location": ", ".join(a["locations"]), "drill": {"kind": "item", "key": item_no},
                "next_action": "Charge back the difference or update the price list if the agreement changed.",
            })
    rows.sort(key=lambda r: r["overpaid"], reverse=True)
    return rows, out


def purchase_orders(po_rows, settings, today=None):
    """Open purchase orders, their age and the value received but not yet invoiced."""
    today = today or date.today()
    th = settings["thresholds"]
    out = []
    for r in po_rows:
        if r.get("Document_Type") != "Order":
            continue
        doc_date = _d(r.get("Document_Date")) or _d(r.get("Posting_Date"))
        age = working_days_between(doc_date, today, settings.get("exclude_weekends", True)) if doc_date else None
        pending = _f(r.get("Amount_Received_Not_Invoiced_excl_VAT_LCY"))
        out.append({
            "no": r.get("No"), "supplier": r.get("Buy_from_Vendor_Name") or r.get("Buy_from_Vendor_No"),
            "vendor_no": r.get("Buy_from_Vendor_No"), "location": r.get("Location_Code") or "",
            "status": r.get("Status"), "document_date": r.get("Document_Date"),
            "posting_date": r.get("Posting_Date"), "amount": _f(r.get("Amount")),
            "received_not_invoiced": pending, "age_days": age,
            "currency": r.get("Currency_Code") or "ALL",
            "assigned_user": r.get("Assigned_User_ID") or "",
            "flag": ("invoice_due" if pending > 0 and (age or 0) * 24 > th["receipt_to_invoice_hours"]
                     else "stale" if (age or 0) > th["po_open_days"] else ""),
        })
    out.sort(key=lambda p: (p["received_not_invoiced"], p["amount"]), reverse=True)
    return out


def events(docs, pos, limit=120, reqs=None, closed_reqs=None, order_hist=None):
    """Live operations feed from the events BC actually records, plus what the app has tracked itself."""
    ev = []
    for r in (reqs or []):
        ev.append({
            "kind": "Requisition open", "date": (r.get("first_seen") or "")[:10], "user": r.get("user", ""),
            "document": r.get("item") or r.get("key"), "document_type": "Requisition", "supplier": r.get("vendor", ""),
            "location": r.get("location", ""), "amount": r.get("value") or 0, "lines": None,
            "status": "Awaiting order", "sort": (r.get("first_seen") or "", 0),
            "age_days": round((r.get("hours_open") or 0) / 24, 1) if r.get("hours_open") is not None else None,
            "next_action": "Convert into a purchase order.",
        })
    for c in (closed_reqs or [])[:20]:
        ev.append({
            "kind": "Requisition carried out", "date": (c.get("closed_at") or "")[:10], "user": c.get("user", ""),
            "document": c.get("item") or c.get("key"), "document_type": "Requisition", "supplier": c.get("vendor", ""),
            "location": c.get("location", ""), "amount": 0, "lines": None,
            "status": f"Closed after {c['hours_open']:.0f}h" if c.get("hours_open") is not None else "Closed",
            "sort": (c.get("closed_at") or "", 0), "next_action": "None",
        })
    label = {INV: "Purchase invoice posted", CM: "Credit note posted", RCPT: "Goods receipt posted"}
    for d in docs.values():
        ev.append({
            "kind": label.get(d["type"], d["type"]), "date": d["posting"], "user": d["user"],
            "document": d["no"], "document_type": d["type"], "supplier": d["supplier"],
            "location": ", ".join(sorted(d["locations"])), "amount": d["value"], "lines": d["lines"],
            "status": "Posted", "sort": (d["posting"] or "", max(d["entries"])),
            "next_action": ("Check negative lines: should this be a credit note?" if d["neg_lines"]
                            else "Add the missing price" if d["zero_lines"] else "None"),
        })
    for p in pos:
        h = (order_hist or {}).get(p["no"], {})
        ev.append({
            "kind": "Purchase order open" if p["status"] == "Open" else "Purchase order released",
            "date": p["document_date"], "user": p["assigned_user"], "document": p["no"],
            "document_type": "Purchase Order", "supplier": p["supplier"], "location": p["location"],
            "amount": p["amount"] or p["received_not_invoiced"], "lines": None, "status": p["status"],
            "sort": (p["document_date"] or "", 0),
            "next_action": ("Post the vendor invoice: goods already received" if p["received_not_invoiced"] > 0
                            else "Release or close the order" if p["status"] == "Open" else "Await receipt"),
            "age_days": p["age_days"],
            "hours_in_status": h.get("hours_in_status"), "released_after_hours": h.get("released_after_hours"),
        })
    ev.sort(key=lambda e: e["sort"], reverse=True)
    for e in ev:
        e.pop("sort", None)
    return ev[:limit]


def sla_exceptions(docs, pos, movers, settings, meta):
    """Applies the configurable service-level rules to the normalized objects."""
    th = settings["thresholds"]
    out = []

    def add(code, rule, severity, title, detail, impact, owner_key="default", **extra):
        out.append({"code": code, "rule": rule, "severity": severity, "title": title, "detail": detail,
                    "impact": impact, "owner": settings["owners"].get(owner_key, ""), **extra})

    for p in pos:
        if p["received_not_invoiced"] > 0 and (p["age_days"] or 0) * 24 > th["receipt_to_invoice_hours"]:
            sev = "critical" if p["received_not_invoiced"] >= th["exception_spend"] else "warning"
            add(f"po-invoice-{p['no']}", "Goods received, invoice not posted", sev,
                f"PO {p['no']} · {p['supplier']}: goods received, no invoice",
                f"{p['received_not_invoiced']:,.0f} {currency.code()} received but not invoiced, order open {p['age_days']} working days "
                f"(threshold {th['receipt_to_invoice_hours']:.0f}h).",
                p["received_not_invoiced"], "invoices", document=p["no"], document_type="Purchase Order",
                supplier=p["supplier"], location=p["location"],
                next_action="Chase the vendor invoice or post it; the cost is accrued but unconfirmed.")
        elif p["status"] == "Open" and (p["age_days"] or 0) > th["po_open_days"]:
            add(f"po-stale-{p['no']}", "Purchase order open too long", "warning",
                f"PO {p['no']} · {p['supplier']}: open {p['age_days']} working days",
                f"Order value {p['amount']:,.0f} {currency.code()}, still Open (threshold {th['po_open_days']:.0f} days).",
                p["amount"], "default", document=p["no"], document_type="Purchase Order",
                supplier=p["supplier"], location=p["location"],
                next_action="Release, amend or close the order.")

    for m in movers:
        if abs(m["pct"]) < th["price_review_pct"] or m["pct"] < 0:
            continue
        critical = m["pct"] >= th["price_critical_pct"] or m["impact"] >= th["price_critical_impact"]
        add(f"price-{m['item_no']}-{meta['current'][0]}", "Price increase above threshold",
            "critical" if critical else "warning",
            f"{m['desc'] or m['item_no']}: price up {m['pct']:+.1f}%",
            f"Average unit cost {m['prev_avg']:,.2f} → {m['cur_avg']:,.2f} {currency.code()} on {m['cur_qty']:,.2f} units, "
            f"cost impact {m['impact']:+,.0f} {currency.code()}.",
            m["impact"], "price", document=m["item_no"], document_type="Item",
            location=", ".join(m["locations"]), drill={"kind": "item", "key": m["item_no"]},
            next_action="Confirm the new price with the supplier or revert to the agreed price.")

    for d in docs.values():
        if d["type"] != INV:
            continue
        if d["neg_lines"]:
            add(f"neg-{d['no']}", "Negative lines on a purchase invoice", "warning",
                f"Invoice {d['no']} · {d['supplier'] or 'unknown supplier'}: {d['neg_lines']} negative line(s)",
                "A return posted as an invoice line instead of a credit note distorts cost and vendor balance.",
                abs(d["value"]), "invoices", document=d["no"], document_type=INV, supplier=d["supplier"],
                location=", ".join(sorted(d["locations"])), drill={"kind": "document", "key": d["no"]},
                next_action="Reverse and repost as a purchase credit memo.")
        if d["zero_lines"]:
            add(f"zero-{d['no']}", "Invoice line with no cost", "warning",
                f"Invoice {d['no']}: {d['zero_lines']} line(s) posted at zero cost",
                "Zero-cost lines understate food cost until the price is corrected.",
                0, "data_quality", document=d["no"], document_type=INV, supplier=d["supplier"],
                location=", ".join(sorted(d["locations"])), drill={"kind": "document", "key": d["no"]},
                next_action="Add the missing unit price and repost.")
        if d["no_location"]:
            add(f"noloc-{d['no']}", "Missing location on posted lines", "warning",
                f"Invoice {d['no']}: {d['no_location']} line(s) without a location",
                "Lines without a location cannot be allocated to a venue's food cost.",
                0, "data_quality", document=d["no"], document_type=INV, supplier=d["supplier"],
                drill={"kind": "document", "key": d["no"]},
                next_action="Correct the location dimension on the document.")
        lag = None
        pd, dd = _d(d["posting"]), _d(d["document_date"])
        if pd and dd:
            lag = (pd - dd).days
        if lag is not None and lag > th["invoice_posting_late_days"]:
            add(f"late-{d['no']}", "Invoice posted late", "warning",
                f"Invoice {d['no']} posted {lag} days after the vendor's invoice date",
                f"Vendor invoice dated {d['document_date']}, posted {d['posting']} "
                f"(threshold {th['invoice_posting_late_days']:.0f} days).",
                d["value"], "invoices", document=d["no"], document_type=INV, supplier=d["supplier"],
                location=", ".join(sorted(d["locations"])), drill={"kind": "document", "key": d["no"]},
                next_action="Check where the document was held before posting.")
        if not d["supplier"]:
            add(f"nosup-{d['no']}", "Posted invoice without a vendor match", "info",
                f"Invoice {d['no']}: no vendor ledger entry matched",
                "The vendor could not be resolved from the vendor ledger for this document number.",
                d["value"], "data_quality", document=d["no"], document_type=INV,
                drill={"kind": "document", "key": d["no"]},
                next_action="Check the document number and vendor posting group.")

    rank = {"critical": 0, "warning": 1, "info": 2}
    out.sort(key=lambda e: (rank.get(e["severity"], 3), -abs(e.get("impact") or 0)))
    return out


def supplier_summary(docs, settings, limit=20):
    """Supplier spend and concentration from posted documents (vendor comes from the ledger join)."""
    agg = defaultdict(lambda: {"spend": 0.0, "invoices": 0, "credit_notes": 0, "lines": 0,
                               "items": set(), "locations": set(), "issues": 0})
    for d in docs.values():
        if d["type"] not in (INV, CM) or not d["supplier"]:
            continue
        a = agg[d["supplier"]]
        if d["type"] == INV:
            a["spend"] += d["value"]
            a["invoices"] += 1
        else:
            a["credit_notes"] += 1
        a["lines"] += d["lines"]
        a["items"].update(d["items"])
        a["locations"].update(d["locations"])
        a["issues"] += (1 if d["neg_lines"] or d["zero_lines"] else 0)
    total = sum(a["spend"] for a in agg.values()) or 1
    rows = [{"supplier": k, "spend": v["spend"], "share": v["spend"] / total * 100, "invoices": v["invoices"],
             "credit_notes": v["credit_notes"], "lines": v["lines"], "items": len(v["items"]),
             "locations": sorted(v["locations"]), "issues": v["issues"],
             "avg_invoice": v["spend"] / v["invoices"] if v["invoices"] else 0}
            for k, v in agg.items()]
    rows.sort(key=lambda r: r["spend"], reverse=True)
    return rows[:limit], total
