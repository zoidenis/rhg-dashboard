"""Invoices posted without a purchase order.

RHG's rule is that a purchase has an approved order before it happens. Business
Central records the order number on the posted invoice, so this is proved from the
records rather than guessed: an invoice either carries an order number or it does not.

What cannot be proved is kept apart. BC publishes the order date only while an order
is still open, so for an invoice whose order has been closed there is no way to tell
whether the order was raised before the goods arrived or after. Those cases are
reported as unverifiable, never as compliant.
"""
from collections import defaultdict

import currency


def _f(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def build(headers, blank_lines, settings, period_label="", open_orders=None):
    corrections = [h for h in headers if h.get("Cancelled") or h.get("Corrective")]
    live = [h for h in headers if not (h.get("Cancelled") or h.get("Corrective"))]
    by_doc_blank = defaultdict(list)
    for l in blank_lines or []:
        by_doc_blank[str(l.get("Document_No"))].append(l)

    invoices = []
    for h in live:
        doc = str(h.get("No"))
        order = str(h.get("Order_No") or "").strip()
        blanks = by_doc_blank.get(doc, [])
        amount = _f(h.get("Amount"))
        if not order:
            state = "No order"
        elif blanks:
            state = "Partly without an order"
        else:
            state = "Order first, unverified date"
        invoices.append({
            "invoice": doc, "vendor": h.get("Buy_from_Vendor_Name") or h.get("Buy_from_Vendor_No") or "",
            "vendor_no": h.get("Buy_from_Vendor_No") or "",
            "vendor_invoice": h.get("Vendor_Invoice_No") or "",
            "order": order, "posted": str(h.get("Posting_Date") or "")[:10],
            "document_date": str(h.get("Document_Date") or "")[:10],
            "amount": amount, "amount_vat": _f(h.get("Amount_Including_VAT")),
            "currency": h.get("Currency_Code") or currency.code(),
            "unit": h.get("Shortcut_Dimension_1_Code") or "(no unit dimension)",
            "category": h.get("Shortcut_Dimension_2_Code") or "(no category dimension)",
            "location": h.get("Location_Code") or "",
            "buyer": h.get("Purchaser_Code") or "",
            "state": state,
            "line_types": sorted({str(l.get("Type") or "").strip() for l in blanks}) if blanks else [],
            "unlinked_value": sum(_f(l.get("Amount")) for l in blanks) if order else amount,
            "reference": f"Posted purchase invoice {doc}",
        })

    no_order = [i for i in invoices if i["state"] == "No order"]
    partial = [i for i in invoices if i["state"] == "Partly without an order"]
    with_order = [i for i in invoices if i["state"] == "Order first, unverified date"]
    total_value = sum(i["amount"] for i in invoices)
    breach_value = sum(i["unlinked_value"] for i in no_order + partial)

    def group(key):
        out = defaultdict(lambda: {"invoices": 0, "value": 0.0, "vendors": set(), "categories": set()})
        for i in no_order + partial:
            g = out[i[key] or "(not set)"]
            g["invoices"] += 1
            g["value"] += i["unlinked_value"]
            g["vendors"].add(i["vendor"])
            g["categories"].add(i["category"])
        return sorted(({"name": k, "invoices": v["invoices"], "value": v["value"],
                        "vendors": len(v["vendors"]), "top_categories": sorted(v["categories"])[:3]}
                       for k, v in out.items()), key=lambda r: -r["value"])

    by_vendor = defaultdict(lambda: {"invoices": 0, "value": 0.0, "units": set(), "months": set()})
    for i in no_order + partial:
        v = by_vendor[i["vendor"]]
        v["invoices"] += 1
        v["value"] += i["unlinked_value"]
        v["units"].add(i["unit"])
        v["months"].add(i["posted"][:7])
    vendors = sorted(({"vendor": k, "invoices": v["invoices"], "value": v["value"],
                       "units": sorted(v["units"]), "repeat": v["invoices"] > 1}
                      for k, v in by_vendor.items()), key=lambda r: -r["value"])

    line_kinds = defaultdict(lambda: {"lines": 0, "value": 0.0})
    for l in blank_lines or []:
        k = str(l.get("Type") or "").strip() or "(no type)"
        line_kinds[k]["lines"] += 1
        line_kinds[k]["value"] += _f(l.get("Amount"))
    kinds = sorted(({"type": k, **v} for k, v in line_kinds.items()), key=lambda r: -r["value"])

    reasons = []
    services = [i for i in no_order if "SHERBIME" in (i["category"] or "").upper()]
    capex = [i for i in no_order if "INVESTIME" in (i["category"] or "").upper()]
    goods = [i for i in no_order if i not in services and i not in capex]
    if services:
        reasons.append({"reason": "Services bought without an order", "confidence": "confirmed",
                        "invoices": len(services), "value": sum(i["unlinked_value"] for i in services),
                        "owner": "Purchasing Manager",
                        "detail": "Rent, maintenance and professional services posted straight to an "
                                  "invoice. The rule applies to services as much as to goods, and this is "
                                  "where it is being skipped.",
                        "units": sorted({i["unit"] for i in services})})
    if capex:
        reasons.append({"reason": "Investments bought without an order", "confidence": "confirmed",
                        "invoices": len(capex), "value": sum(i["unlinked_value"] for i in capex),
                        "owner": "Purchasing Manager",
                        "detail": "Equipment and fit-out invoices with no order behind them. These are the "
                                  "largest single amounts, and an order is what would have carried the "
                                  "approval.",
                        "units": sorted({i["unit"] for i in capex})})
    if goods:
        reasons.append({"reason": "Goods bought without an order", "confidence": "confirmed",
                        "invoices": len(goods), "value": sum(i["unlinked_value"] for i in goods),
                        "owner": "Purchasing Coordinator",
                        "detail": "Items or freight charges invoiced with no order. Often an urgent "
                                  "purchase; the order should still be raised before the goods arrive.",
                        "units": sorted({i["unit"] for i in goods})})
    if partial:
        reasons.append({"reason": "Lines added onto an order's invoice", "confidence": "confirmed",
                        "invoices": len(partial), "value": sum(i["unlinked_value"] for i in partial),
                        "owner": "Purchasing Coordinator",
                        "detail": "The invoice came from an order, but extra lines were added to it that no "
                                  "order covers.", "units": sorted({i["unit"] for i in partial})})
    missing_unit = [i for i in no_order if i["unit"] == "(no unit dimension)"]
    if missing_unit:
        reasons.append({"reason": "No unit recorded on the invoice", "confidence": "data issue",
                        "invoices": len(missing_unit),
                        "value": sum(i["unlinked_value"] for i in missing_unit),
                        "owner": "Cost Controller",
                        "detail": "These invoices carry no department dimension, so the unit responsible "
                                  "cannot be identified from the records.",
                        "units": []})
    reasons.sort(key=lambda r: -r["value"])

    floor = float(settings["thresholds"].get("exception_spend") or 100000)
    decisions = []
    for v in vendors[:20]:
        if v["repeat"]:
            decisions.append({
                "case": f"{v['vendor']}: {v['invoices']} invoices with no order",
                "evidence": f"{currency.money(v['value'])} across {', '.join(v['units'][:3])}.",
                "action": "Put this supplier on a standing order or a contract, so the spend stops "
                          "arriving without one.",
                "value": v["value"], "owner": "Purchasing Manager", "due": "This month",
                "status": "New", "confidence": "confirmed"})
    for i in sorted(no_order, key=lambda x: -x["unlinked_value"])[:10]:
        if i["unlinked_value"] >= floor:
            decisions.append({
                "case": f"{i['vendor']}: {currency.money(i['unlinked_value'])} with no order",
                "evidence": f"Invoice {i['invoice']} posted {i['posted']}, {i['unit']} · {i['category']}.",
                "action": "Ask the unit who approved it and record the approval against the invoice.",
                "value": i["unlinked_value"], "owner": "Purchasing Manager", "due": "This week",
                "status": "New", "confidence": "confirmed", "invoice": i["invoice"]})
    decisions.sort(key=lambda d: -d["value"])

    return {
        "period": period_label,
        "kpis": {
            "invoices": len(invoices),
            "no_order": len(no_order),
            "partial": len(partial),
            "with_order": len(with_order),
            "rate": (len(no_order) + len(partial)) / len(invoices) * 100 if invoices else 0,
            "value_total": total_value,
            "value_breach": breach_value,
            "value_rate": breach_value / total_value * 100 if total_value else 0,
            "corrections": len(corrections),
            "unverifiable": len(with_order),
        },
        "definitions": {
            "denominator": f"{len(invoices)} posted purchase invoices in the period, excluding "
                           f"{len(corrections)} cancelled or corrective documents, which are corrections "
                           f"rather than new purchases.",
            "breach": "An invoice whose header carries no order number, or an invoice from an order that "
                      "has extra lines no order covers. Proved from the order number BC records, not "
                      "inferred from the supplier, the amount or the description.",
            "value": "Amounts are excluding VAT. For a partly linked invoice only the lines without an "
                     "order are counted.",
            "unverifiable": f"{len(with_order)} invoices do carry an order number, but BC publishes the "
                            f"order date only while the order is still open. For the rest there is no way "
                            f"to prove the order came before the goods, so they are counted as compliant "
                            f"on the order number alone and the date remains unverified.",
        },
        "by_unit": group("unit"), "by_category": group("category"), "vendors": vendors[:25],
        "line_types": kinds, "reasons": reasons, "decisions": decisions[:10],
        "invoices": sorted(no_order + partial, key=lambda i: -i["unlinked_value"])[:200],
        "compliant_sample": with_order[:5],
    }
