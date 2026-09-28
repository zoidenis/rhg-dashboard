"""Posting follow-up: the two things the posting records reveal that nothing else does.

Business Central books a receipt at the price on the order and the invoice at the price
the supplier actually charged, and keeps both on the same entry. The difference between
them is a real control and it is not visible anywhere else in this application. Returns
and credit notes are the second: they are corrections, and a supplier who keeps needing
them is a supplier worth a conversation.

Everything else this screen could have shown is already covered: goods received without
an invoice by Live operations, invoices without an order by its own section, posting
mistakes by Data quality. Those are linked, never recalculated here.
"""
from collections import defaultdict

import currency

RECEIPT, INVOICE = "Purchase Receipt", "Purchase Invoice"
CREDIT, RETURN = "Purchase Credit Memo", "Purchase Return Shipment"


def _f(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def build(rows, settings, period_label=""):
    """rows: purchase value entries of the period."""
    th = settings["thresholds"]
    tolerance_pct = float(th.get("price_review_pct") or 5)
    floor = float(th.get("exception_spend") or 100000) / 10

    # the two halves of one purchase: what was received, and what was invoiced
    by_entry = defaultdict(lambda: {"expected": 0.0, "actual": 0.0, "item": "", "description": "",
                                    "location": "", "receipt_doc": "", "invoice_doc": "",
                                    "receipt_date": "", "invoice_date": "", "qty": 0.0})
    kinds = defaultdict(lambda: {"rows": 0, "value": 0.0, "documents": set()})
    credits = defaultdict(lambda: {"rows": 0, "value": 0.0, "documents": set(), "items": set(),
                                   "locations": set()})
    for r in rows:
        doc_type = (r.get("Document_Type") or "").strip()
        kinds[doc_type or "(no document type)"]["rows"] += 1
        kinds[doc_type or "(no document type)"]["value"] += _f(r.get("Cost_Amount_Actual"))
        kinds[doc_type or "(no document type)"]["documents"].add(r.get("Document_No"))
        link = r.get("Item_Ledger_Entry_No")
        e = by_entry[link]
        e["item"] = r.get("Item_No") or e["item"]
        e["description"] = (r.get("Item_Description") or "").strip() or e["description"]
        e["location"] = r.get("Location_Code") or e["location"]
        if doc_type == RECEIPT:
            e["expected"] += _f(r.get("Cost_Amount_Expected"))
            e["receipt_doc"] = r.get("Document_No") or e["receipt_doc"]
            e["receipt_date"] = str(r.get("Posting_Date") or "")[:10] or e["receipt_date"]
            e["qty"] += _f(r.get("Item_Ledger_Entry_Quantity"))
        elif doc_type == INVOICE:
            e["actual"] += _f(r.get("Cost_Amount_Actual"))
            e["invoice_doc"] = r.get("Document_No") or e["invoice_doc"]
            e["invoice_date"] = str(r.get("Posting_Date") or "")[:10] or e["invoice_date"]
        if doc_type in (CREDIT, RETURN):
            c = credits[r.get("Document_No")]
            c["rows"] += 1
            c["value"] += _f(r.get("Cost_Amount_Actual"))
            c["documents"].add(r.get("Document_No"))
            c["items"].add(r.get("Item_No"))
            c["locations"].add(r.get("Location_Code"))
            c["date"] = str(r.get("Posting_Date") or "")[:10]

    # invoiced at a different price than received
    differences = []
    for entry, e in by_entry.items():
        if not (e["expected"] and e["actual"]):
            continue                      # only entries received and invoiced in this period
        gap = e["actual"] - e["expected"]
        pct = gap / abs(e["expected"]) * 100 if e["expected"] else 0
        if abs(pct) >= tolerance_pct and abs(gap) >= floor:
            differences.append({
                "item": e["item"], "description": e["description"], "location": e["location"],
                "received_cost": e["expected"], "invoiced_cost": e["actual"], "difference": gap,
                "pct": pct, "quantity": e["qty"],
                "receipt": e["receipt_doc"], "invoice": e["invoice_doc"],
                "receipt_date": e["receipt_date"], "invoice_date": e["invoice_date"],
                "reference": f"Receipt {e['receipt_doc']} · invoice {e['invoice_doc']}",
            })
    differences.sort(key=lambda d: -abs(d["difference"]))

    returns = sorted(({"document": doc, "value": c["value"], "items": len(c["items"]),
                       "locations": sorted(x for x in c["locations"] if x), "date": c.get("date", "")}
                      for doc, c in credits.items()), key=lambda r: r["value"])

    total_expected = sum(e["expected"] for e in by_entry.values())
    total_actual = sum(e["actual"] for e in by_entry.values())
    return {
        "period": period_label,
        "kinds": sorted(({"type": k, "rows": v["rows"], "value": v["value"],
                          "documents": len(v["documents"])} for k, v in kinds.items()),
                        key=lambda k: -k["rows"]),
        "price_gap": {
            "received_cost": total_expected, "invoiced_cost": total_actual,
            "difference": total_actual - total_expected,
            "pct": ((total_actual - total_expected) / total_expected * 100) if total_expected else 0,
            "cases": len(differences),
            "case_value": sum(abs(d["difference"]) for d in differences),
            "tolerance": tolerance_pct,
            "method": "Business Central books the receipt at the price on the order and the invoice at the "
                      "price the supplier charged, both on the same item entry. The difference is what the "
                      "invoice added or took off after the goods had arrived. Only entries received and "
                      "invoiced inside this period are compared, so part of the month's receipts will be "
                      "settled in the next one.",
            "top": differences[:5], "all": differences[:100],
            "owner": "Purchasing Coordinator",
            "action": "Check the invoice against the order price and raise a credit note if the supplier "
                      "charged more than agreed.",
        },
        "returns": {
            "documents": len(returns), "value": sum(r["value"] for r in returns),
            "top": returns[:5], "all": returns[:60],
            "owner": "Purchasing Coordinator",
            "action": "Look at whether these are quality returns from one supplier or corrections of "
                      "posting mistakes; the two need different conversations.",
            "method": "Credit notes and return shipments posted in the period, at the cost BC recorded.",
        },
        "covered_elsewhere": [
            {"control": "Goods received with no invoice yet", "where": "Live operations",
             "why": "Already measured there against the agreed time limit, with the same records."},
            {"control": "Invoices posted without a purchase order", "where": "Invoices without PO",
             "why": "That section proves the link from the order number BC records; repeating it here "
                    "would risk two different answers to the same question."},
            {"control": "Negative lines, zero cost, backdated postings", "where": "Data quality",
             "why": "Posting mistakes are collected there with the records that show them."},
        ],
    }
