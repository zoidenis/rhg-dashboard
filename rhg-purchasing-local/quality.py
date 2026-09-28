"""Data quality checks over the records already read from Business Central.

Each finding names the document and the user where BC provides one. Where the
source of an error cannot be established, the finding says so instead of
assigning blame.
"""
from collections import defaultdict
from datetime import date

INV, RCPT = "Purchase Invoice", "Purchase Receipt"


def _f(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _d(s):
    try:
        v = date.fromisoformat((s or "")[:10])
        return None if v.year < 1900 else v
    except ValueError:
        return None


def _norm_desc(s):
    return " ".join((s or "").lower().split())


def build(ve_rows, vle_rows, docs, prev_items, cur_items, free_text_lines=None, today=None):
    today = today or date.today()
    checks = []

    def add(code, title, severity, why, rows, fix):
        if rows:
            checks.append({"code": code, "title": title, "severity": severity, "why": why,
                           "count": len(rows), "rows": rows[:100], "fix": fix})

    # --- missing location ---
    add("missing_location", "Lines without a location", "warning",
        "Lines with no location cannot be allocated to a venue, so food cost per venue is understated.",
        [{"Document": r.get("Document_No"), "Type": r.get("Document_Type"), "Item": r.get("Item_No"),
          "Description": (r.get("Item_Description") or "").strip(), "Posted": r.get("Posting_Date"),
          "Cost (ALL)": round(_f(r.get("Cost_Amount_Actual")), 2)}
         for r in ve_rows if not r.get("Location_Code")],
        "Correct the location on the document and repost.")

    # --- zero cost / zero quantity ---
    add("zero_cost", "Invoice lines posted at zero cost", "warning",
        "A zero cost line understates food cost until the price is corrected.",
        [{"Document": r.get("Document_No"), "Item": r.get("Item_No"),
          "Description": (r.get("Item_Description") or "").strip(), "Quantity": _f(r.get("Item_Ledger_Entry_Quantity")),
          "Posted": r.get("Posting_Date")}
         for r in ve_rows if r.get("Document_Type") == INV and _f(r.get("Cost_Amount_Actual")) == 0],
        "Add the unit price on the purchase line and repost.")

    add("zero_qty", "Cost posted without quantity", "warning",
        "Cost with no quantity cannot produce a unit price, so the item's average cost is distorted.",
        [{"Document": r.get("Document_No"), "Type": r.get("Document_Type"), "Item": r.get("Item_No"),
          "Description": (r.get("Item_Description") or "").strip(),
          "Cost (ALL)": round(_f(r.get("Cost_Amount_Actual")), 2), "Posted": r.get("Posting_Date")}
         for r in ve_rows if r.get("Document_Type") == RCPT
         and _f(r.get("Item_Ledger_Entry_Quantity")) == 0 and _f(r.get("Cost_Amount_Expected")) != 0],
        "Check whether the receipt was posted with the right quantity.")

    # --- future posting dates ---
    add("future_date", "Posting date in the future", "critical",
        "A future posting date moves cost into a period that has not happened yet.",
        [{"Document": r.get("Document_No"), "Type": r.get("Document_Type"), "Posted": r.get("Posting_Date"),
          "Item": r.get("Item_No"), "Cost (ALL)": round(_f(r.get("Cost_Amount_Actual")), 2)}
         for r in ve_rows if (_d(r.get("Posting_Date")) or today) > today],
        "Correct the posting date on the document.")

    # --- inconsistent item descriptions ---
    desc = defaultdict(set)
    for r in ve_rows:
        if r.get("Item_No") and r.get("Item_Description"):
            desc[r["Item_No"]].add(r["Item_Description"].strip())
    add("item_desc", "Same item number, different descriptions", "info",
        "Different descriptions on one item make reports and price checks harder to read.",
        [{"Item": k, "Descriptions": " | ".join(sorted(v))} for k, v in desc.items() if len(v) > 1],
        "Align the description on the item card and on the purchase lines.")

    # --- duplicate item names across item numbers ---
    by_desc = defaultdict(set)
    for item, names in desc.items():
        for n in names:
            by_desc[_norm_desc(n)].add(item)
    add("duplicate_items", "Same description on different item numbers", "info",
        "Two item numbers for the same product split the purchase history and hide price differences.",
        [{"Description": k, "Item numbers": ", ".join(sorted(v))} for k, v in by_desc.items() if len(v) > 1],
        "Decide which item number is the master and block the duplicate.")

    # --- possible duplicate vendor invoices ---
    seen = defaultdict(list)
    for r in vle_rows:
        if r.get("Document_Type") != "Invoice":
            continue
        key = (r.get("Vendor_No"), round(_f(r.get("Amount_LCY")), 2), (r.get("Document_Date") or "")[:10])
        seen[key].append(r)
    dupes = []
    for (vendor, amount, doc_date), rows in seen.items():
        if len(rows) > 1:
            dupes.append({"Supplier": rows[0].get("Vendor_Name") or vendor, "Invoice date": doc_date,
                          "Amount (ALL)": round(-amount, 2), "Documents": ", ".join(str(r.get("Document_No")) for r in rows),
                          "Times": len(rows)})
    add("duplicate_invoice", "Possible duplicate invoices", "critical",
        "Same supplier, same date, same amount, posted more than once. It may be legitimate, but it is worth checking.",
        dupes, "Compare the documents in BC and issue a credit note if one is a duplicate.")

    # --- documents with no vendor ---
    add("no_vendor", "Posted documents with no vendor match", "warning",
        "The vendor ledger has no entry with this document number, so the supplier cannot be identified.",
        [{"Document": d["no"], "Type": d["type"], "Posted": d["posting"], "Lines": d["lines"],
          "Value (ALL)": round(d["value"])} for d in docs.values() if d["type"] == INV and not d["supplier"]],
        "Check the document number and the vendor posting group.")

    # --- possible unit of measure error ---
    uom = []
    for item, cur in cur_items.items():
        prev = prev_items.get(item)
        if not prev or not prev.get("avg") or not cur.get("avg"):
            continue
        ratio = cur["avg"] / prev["avg"]
        if ratio >= 5 or ratio <= 0.2:
            uom.append({"Item": item, "Description": cur["desc"], "Previous unit cost": round(prev["avg"], 2),
                        "Current unit cost": round(cur["avg"], 2), "Ratio": round(ratio, 1),
                        "Quantity": round(cur["qty"], 2), "Locations": ", ".join(cur["locations"])})
    add("uom_error", "Unit cost changed by more than five times", "critical",
        "A jump of this size is usually a unit of measure or quantity mistake rather than a real price change. "
        "BC does not expose the unit of measure on value entries, so this is a signal, not proof.",
        sorted(uom, key=lambda r: -abs(r["Ratio"])),
        "Open the invoice and check the quantity and unit of measure against the supplier document.")

    # --- free text purchases ---
    if free_text_lines:
        add("free_text", "Purchase lines without an item number", "warning",
            "Free text lines never reach item statistics, so their cost is invisible in price and food cost analysis.",
            free_text_lines[:100], "Use an item number, or create the item if it is missing.")

    order = {"critical": 0, "warning": 1, "info": 2}
    checks.sort(key=lambda c: (order.get(c["severity"], 3), -c["count"]))
    return {
        "checks": checks,
        "totals": {"critical": sum(c["count"] for c in checks if c["severity"] == "critical"),
                   "warning": sum(c["count"] for c in checks if c["severity"] == "warning"),
                   "info": sum(c["count"] for c in checks if c["severity"] == "info"),
                   "findings": sum(c["count"] for c in checks)},
    }
