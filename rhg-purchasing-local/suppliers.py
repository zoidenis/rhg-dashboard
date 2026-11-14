"""Supplier performance: what the company bought from each supplier in the period, against the comparison period.

Source: the posted purchase invoices (page 138 list, PostedPurchaseInvoices) and the posted purchase credit memos
(page 147, PostedPurchaseCrMemos) of the period, every line of them, items and services, assets and G/L lines alike.
  * Amount: the document's amount excluding VAT, after line and invoice discounts.
  * In a foreign currency the amount is taken to local currency at the rate of the posting, read from the vendor
    ledger entry of the same document (its Amount (LCY) against the document's amount including VAT). A foreign
    document with no ledger entry stays out of the totals and is counted apart.
  * Net purchases = invoices − credit memos. A cancelled invoice and its corrective credit memo cancel out.
  * Items at BC cost: the invoice and credit-memo value entries of item lines (actual cost), for the share of the
    purchases that went into stock.
  * Location: the document's Location Code, else its department (global dimension 1).
Before this screen counted only the item lines of invoices (value entries), credit memos as a number and never as
money, and the 20 largest suppliers: the totals were short of what the suppliers invoiced. Nothing is written to BC.
"""
import threading
import time
from collections import defaultdict
from datetime import date, timedelta
from calendar import monthrange

INV_FIELDS = ("No,Buy_from_Vendor_No,Buy_from_Vendor_Name,Posting_Date,Amount,Amount_Including_VAT,Currency_Code,"
              "Location_Code,Shortcut_Dimension_1_Code,Purchaser_Code,Vendor_Invoice_No,Cancelled,Corrective")
CR_FIELDS = ("No,Buy_from_Vendor_No,Buy_from_Vendor_Name,Posting_Date,Amount,Amount_Including_VAT,Currency_Code,"
             "Location_Code,Shortcut_Dimension_1_Code,Purchaser_Code,Cancelled,Corrective")
_heads, _heads_lock = {}, threading.Lock()


def _f(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def months(a, b):
    out, cur = [], a
    while cur <= b:
        end = min(b, cur.replace(day=monthrange(cur.year, cur.month)[1]))
        out.append((cur, end))
        cur = end + timedelta(days=1)
    return out


def headers(client, company, entity, fields, a, b, today=None):
    """The document headers of a range, read month by month: a month that has ended is kept six hours, the
    running one five minutes."""
    today = today or date.today()
    rows = []
    for x, y in months(a, b):
        key = (entity, company, x.isoformat(), y.isoformat())
        age = 300 if y >= today - timedelta(days=1) else 6 * 3600
        with _heads_lock:
            hit = _heads.get(key)
        if hit and time.time() - hit[0] < age:
            rows.extend(hit[1])
            continue
        got = client._get_all(client._url(company, entity),
                              {"$filter": f"Posting_Date ge {x.isoformat()} and Posting_Date le {y.isoformat()}",
                               "$select": fields})
        with _heads_lock:
            _heads[key] = (time.time(), got)
            if len(_heads) > 120:
                for k, _ in sorted(_heads.items(), key=lambda kv: kv[1][0])[:30]:
                    _heads.pop(k, None)
        rows.extend(got)
    return rows


def _loc(h):
    return str(h.get("Location_Code") or h.get("Shortcut_Dimension_1_Code") or "").strip()


def lcy_factors(vle_rows):
    """{document no: |Amount (LCY)|} of the invoices and credit memos in the vendor ledger."""
    out = {}
    for r in vle_rows or []:
        if r.get("Document_Type") in ("Invoice", "Credit Memo"):
            out[str(r.get("Document_No"))] = abs(_f(r.get("Amount_LCY")))
    return out


def _docs(inv, cr, vle, local, location=None, only=None):
    """The documents of a period in local currency excluding VAT: [(kind, header, amount)], and the foreign ones
    left out for want of a rate."""
    lcy = lcy_factors(vle)
    out, unconverted = [], []
    for kind, rows in (("invoice", inv), ("credit", cr)):
        for h in rows or []:
            loc = _loc(h).upper()
            if location and loc != location.upper():
                continue
            if only is not None and loc not in only:
                continue
            amt, cur = _f(h.get("Amount")), str(h.get("Currency_Code") or "").strip().upper()
            if cur and cur != local:
                incl, l = _f(h.get("Amount_Including_VAT")), lcy.get(str(h.get("No")))
                if not l or not incl:
                    unconverted.append({"no": h.get("No"), "vendor": h.get("Buy_from_Vendor_Name"), "currency": cur,
                                        "amount": amt, "kind": kind})
                    continue
                amt = amt * l / incl
            out.append((kind, h, amt))
    return out, unconverted


def build(cur_inv, cur_cr, cur_vle, prev_inv, prev_cr, prev_vle, value_rows, local, settings,
          location=None, only=None, docs_per_supplier=150):
    """The Supplier performance screen."""
    local = (local or "ALL").upper()
    docs, unconverted = _docs(cur_inv, cur_cr, cur_vle, local, location, only)
    pdocs, _ = _docs(prev_inv, prev_cr, prev_vle, local, location, only)
    agg = defaultdict(lambda: {"name": "", "invoiced": 0.0, "credit": 0.0, "invoices": 0, "credit_memos": 0,
                               "locations": defaultdict(float), "purchasers": set(), "docs": [], "first": None,
                               "last": None, "cancelled": 0})
    for kind, h, amt in docs:
        no = str(h.get("Buy_from_Vendor_No") or "").strip() or str(h.get("Buy_from_Vendor_Name") or "?")
        a = agg[no]
        a["name"] = h.get("Buy_from_Vendor_Name") or a["name"] or no
        day = str(h.get("Posting_Date") or "")[:10]
        if kind == "invoice":
            a["invoiced"] += amt
            a["invoices"] += 1
            a["locations"][_loc(h) or "–"] += amt
        else:
            a["credit"] += amt
            a["credit_memos"] += 1
            a["locations"][_loc(h) or "–"] -= amt
        a["cancelled"] += 1 if h.get("Cancelled") or h.get("Corrective") else 0
        if h.get("Purchaser_Code"):
            a["purchasers"].add(str(h["Purchaser_Code"]))
        a["first"] = min(a["first"] or day, day)
        a["last"] = max(a["last"] or day, day)
        a["docs"].append([kind, str(h.get("No") or ""), str(h.get("Vendor_Invoice_No") or ""), day, _loc(h),
                          round(amt if kind == "invoice" else -amt, 2)])
    prev = defaultdict(float)
    for kind, h, amt in pdocs:
        no = str(h.get("Buy_from_Vendor_No") or "").strip() or str(h.get("Buy_from_Vendor_Name") or "?")
        prev[no] += amt if kind == "invoice" else -amt
    names_prev = {}
    for kind, h, amt in pdocs:
        no = str(h.get("Buy_from_Vendor_No") or "").strip() or str(h.get("Buy_from_Vendor_Name") or "?")
        names_prev[no] = h.get("Buy_from_Vendor_Name") or no
    # items at BC cost: invoice and credit-memo value entries, by the vendor of their document
    by_doc_vendor = {str(h.get("No")): str(h.get("Buy_from_Vendor_No") or "").strip() for _, h, _ in docs}
    items_cost, items_set = defaultdict(float), defaultdict(set)
    for r in value_rows or []:
        if r.get("Document_Type") not in ("Purchase Invoice", "Purchase Credit Memo"):
            continue
        v = by_doc_vendor.get(str(r.get("Document_No")))
        if v is None:
            continue
        items_cost[v] += _f(r.get("Cost_Amount_Actual"))
        if r.get("Item_No"):
            items_set[v].add(str(r["Item_No"]))
    th = (settings or {}).get("thresholds") or {}
    conc_pct = float(th.get("supplier_concentration_pct") or 35)
    total = sum(max(0.0, a["invoiced"] - a["credit"]) for a in agg.values())
    rows = []
    for no, a in agg.items():
        net = a["invoiced"] - a["credit"]
        p = prev.get(no)
        share = net / total * 100 if total and net > 0 else 0.0
        notes = []
        if share >= conc_pct:
            notes.append("concentration")
        if a["credit"] > 0.5 and a["invoiced"] > 0 and a["credit"] / a["invoiced"] >= 0.05:
            notes.append("credit")
        if not p:
            notes.append("new")
        rows.append({"no": no, "name": a["name"], "net": round(net, 2), "invoiced": round(a["invoiced"], 2),
                     "credit": round(a["credit"], 2), "invoices": a["invoices"], "credit_memos": a["credit_memos"],
                     "avg_invoice": round(a["invoiced"] / a["invoices"], 2) if a["invoices"] else None,
                     "share": round(share, 2), "prev": round(p, 2) if p is not None else None,
                     "change_pct": round((net - p) / abs(p) * 100, 1) if p else None,
                     "items_cost": round(items_cost.get(no, 0.0), 2), "items": len(items_set.get(no, ())),
                     "locations": sorted(([k, round(v, 2)] for k, v in a["locations"].items()), key=lambda kv: -kv[1]),
                     "purchasers": sorted(a["purchasers"]), "first": a["first"], "last": a["last"],
                     "cancelled": a["cancelled"], "notes": notes,
                     "docs": sorted(a["docs"], key=lambda d: d[3], reverse=True)[:docs_per_supplier],
                     "docs_count": len(a["docs"])})
    rows.sort(key=lambda r: -r["net"])
    lost = sorted(([no, names_prev.get(no, no), round(v, 2)] for no, v in prev.items() if v > 0.5 and no not in agg),
                  key=lambda x: -x[2])
    net_total = sum(r["net"] for r in rows)
    prev_total = sum(prev.values())
    top5 = sum(r["net"] for r in rows[:5] if r["net"] > 0)
    return {
        "rows": rows, "lost": lost[:200], "unconverted": unconverted[:100],
        "kpis": {"net": round(net_total, 2), "prev": round(prev_total, 2),
                 "change_pct": round((net_total - prev_total) / abs(prev_total) * 100, 1) if prev_total else None,
                 "invoiced": round(sum(r["invoiced"] for r in rows), 2), "credit": round(sum(r["credit"] for r in rows), 2),
                 "suppliers": sum(1 for r in rows if r["net"] > 0.5), "invoices": sum(r["invoices"] for r in rows),
                 "credit_memos": sum(r["credit_memos"] for r in rows),
                 "top5_share": round(top5 / total * 100, 1) if total else None,
                 "concentrated": sum(1 for r in rows if "concentration" in r["notes"]),
                 "new": sum(1 for r in rows if "new" in r["notes"] and r["net"] > 0.5), "lost": len(lost),
                 "items_cost": round(sum(r["items_cost"] for r in rows), 2), "unconverted": len(unconverted)},
        "concentration_pct": conc_pct,
        "definitions": {
            "net": "Net purchases: the amount excluding VAT of the posted purchase invoices of the period, less the posted "
                   "purchase credit memos, every line (items, services, assets, G/L), in local currency.",
            "items_cost": "Items at BC cost: the actual cost of the item lines of the same invoices and credit memos "
                          "(value entries), the part that went into stock.",
            "share": "Share: the supplier's net purchases against the net purchases of every supplier with purchases.",
            "change": "Change: against the same supplier's net purchases in the comparison period chosen above.",
            "currency": "A document in a foreign currency is taken to local currency at the rate of its posting (its "
                        "vendor ledger entry). One with no ledger entry is left out and counted apart.",
            "concentration": f"Concentration risk: a supplier with {conc_pct:g}% or more of the net purchases "
                             "(Settings → Thresholds).",
            "credit": "Credit notes: credit memos worth 5% or more of what the supplier invoiced; check quality or pricing.",
            "new": "New: no purchases from the supplier in the comparison period.",
        },
    }
