"""Posted invoice and user performance KPIs.

Sources:
- VendorLedgerEntries: every posted purchase invoice / credit memo (items and services).
- ValueEntries: the item lines of each invoice, receipt and credit memo.
- ItemRegisters (optional): which BC user posted each document, and when.
"""
from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import date

import config

INV, CM, RCPT = "Purchase Invoice", "Purchase Credit Memo", "Purchase Receipt"


def _d(s):
    try:
        d = date.fromisoformat((s or "")[:10])
        return None if d.year < 1900 else d
    except ValueError:
        return None


def _lag(posting, document):
    p, d = _d(posting), _d(document)
    return (p - d).days if p and d else None


def _cost(r):
    c = float(r.get("Cost_Amount_Actual") or 0)
    if config.INCLUDE_EXPECTED_COST and r.get("Document_Type") == RCPT:
        c += float(r.get("Cost_Amount_Expected") or 0)
    return c


def _pct(a, b):
    return (a - b) / b * 100 if b else None


def vendor_summary(rows):
    inv = [r for r in rows if r.get("Document_Type") == "Invoice"]
    cm = [r for r in rows if r.get("Document_Type") == "Credit Memo"]
    lags = [x for x in (_lag(r.get("Posting_Date"), r.get("Document_Date")) for r in inv) if x is not None]
    value = sum(-float(r.get("Amount_LCY") or 0) for r in inv)
    return {
        "invoices": len(inv), "credit_memos": len(cm),
        "credit_ratio": len(cm) / len(inv) * 100 if inv else 0,
        "value": value, "cm_value": sum(float(r.get("Amount_LCY") or 0) for r in cm),
        "avg_value": value / len(inv) if inv else 0,
        "vendors": len({r.get("Vendor_No") for r in inv}),
        "avg_lag": sum(lags) / len(lags) if lags else None,
        "same_day_pct": sum(1 for x in lags if x <= 1) / len(lags) * 100 if lags else None,
        "late": sum(1 for x in lags if x > config.LATE_POSTING_DAYS),
    }


def _docs(ve_rows):
    """Groups value entries into documents."""
    docs = {}
    for r in ve_rows:
        key = (r.get("Document_Type"), r.get("Document_No"))
        d = docs.get(key)
        if d is None:
            d = docs[key] = {"type": key[0], "no": key[1], "lines": 0, "value": 0.0, "neg_lines": 0,
                             "zero_lines": 0, "locations": set(), "posting": r.get("Posting_Date"),
                             "document": r.get("Document_Date"), "entries": []}
        d["lines"] += 1
        c = _cost(r)
        d["value"] += c
        if d["type"] == INV and (float(r.get("Cost_Amount_Actual") or 0) < 0):
            d["neg_lines"] += 1
        if d["type"] == INV and float(r.get("Cost_Amount_Actual") or 0) == 0:
            d["zero_lines"] += 1
        if r.get("Location_Code"):
            d["locations"].add(r["Location_Code"])
        d["entries"].append(r.get("Entry_No") or 0)
    return docs


def _bucket(n):
    if n == 1: return "1 line"
    if n <= 5: return "2–5 lines"
    if n <= 10: return "6–10 lines"
    if n <= 20: return "11–20 lines"
    return "21+ lines"


def _hour(t):
    try:
        return int(str(t)[:2])
    except ValueError:
        return None


def build(vle_cur, vle_prev, ve_cur, ve_prev, registers=None, register_status=None, location=None):
    if location:
        ve_cur = [r for r in ve_cur if r.get("Location_Code") == location]
        ve_prev = [r for r in ve_prev if r.get("Location_Code") == location]

    vc, vp = vendor_summary(vle_cur), vendor_summary(vle_prev)
    docs = _docs(ve_cur)
    docs_prev = _docs(ve_prev)
    inv_docs = [d for d in docs.values() if d["type"] == INV]
    inv_prev = [d for d in docs_prev.values() if d["type"] == INV]
    lines = sum(d["lines"] for d in inv_docs)
    lines_prev = sum(d["lines"] for d in inv_prev)

    buckets = Counter(_bucket(d["lines"]) for d in inv_docs)
    order = ["1 line", "2–5 lines", "6–10 lines", "11–20 lines", "21+ lines"]

    # Daily invoice counts (vendor ledger: all invoices)
    daily = defaultdict(lambda: {"invoices": 0, "credit_memos": 0})
    for r in vle_cur:
        if r.get("Document_Type") == "Invoice":
            daily[r.get("Posting_Date")]["invoices"] += 1
        elif r.get("Document_Type") == "Credit Memo":
            daily[r.get("Posting_Date")]["credit_memos"] += 1

    # Vendors by invoice count
    vend = defaultdict(lambda: {"name": "", "invoices": 0, "value": 0.0, "lags": []})
    for r in vle_cur:
        if r.get("Document_Type") != "Invoice":
            continue
        v = vend[r.get("Vendor_No")]
        v["name"] = r.get("Vendor_Name") or r.get("Vendor_No")
        v["invoices"] += 1
        v["value"] += -float(r.get("Amount_LCY") or 0)
        lag = _lag(r.get("Posting_Date"), r.get("Document_Date"))
        if lag is not None:
            v["lags"].append(lag)
    vendors = sorted([{"no": k, "name": v["name"], "invoices": v["invoices"], "value": v["value"],
                       "avg_lag": sum(v["lags"]) / len(v["lags"]) if v["lags"] else None} for k, v in vend.items()],
                     key=lambda x: x["invoices"], reverse=True)[:15]

    late = sorted([{"doc": r.get("Document_No"), "vendor": r.get("Vendor_Name"), "document_date": r.get("Document_Date"),
                    "posting_date": r.get("Posting_Date"), "lag": _lag(r.get("Posting_Date"), r.get("Document_Date")),
                    "value": -float(r.get("Amount_LCY") or 0)}
                   for r in vle_cur if r.get("Document_Type") == "Invoice"
                   and (_lag(r.get("Posting_Date"), r.get("Document_Date")) or 0) > config.LATE_POSTING_DAYS],
                  key=lambda x: x["lag"], reverse=True)[:20]

    suspicious = sorted([{"doc": d["no"], "lines": d["lines"], "neg_lines": d["neg_lines"], "zero_lines": d["zero_lines"],
                          "value": d["value"], "locations": sorted(d["locations"]), "user": None}
                         for d in inv_docs if d["neg_lines"] or d["zero_lines"]],
                        key=lambda x: (x["neg_lines"], x["zero_lines"]), reverse=True)

    biggest = sorted([{"doc": d["no"], "lines": d["lines"], "value": d["value"], "locations": sorted(d["locations"]), "user": None}
                      for d in inv_docs], key=lambda x: x["lines"], reverse=True)[:10]

    # ---- Users (needs Item Registers) ----
    users, hours, user_status = [], [0] * 24, {"available": False}
    if registers is not None:
        user_status = {"available": True, "registers": len(registers)}
        starts = [r["from"] for r in registers]

        def reg_for(entry):
            i = bisect_right(starts, entry) - 1
            if i >= 0 and registers[i]["from"] <= entry <= registers[i]["to"]:
                return registers[i]
            return None

        u = defaultdict(lambda: {"invoices": 0, "lines": 0, "value": 0.0, "credit_memos": 0, "receipts": 0,
                                 "receipt_lines": 0, "neg_lines": 0, "zero_lines": 0, "days": set(),
                                 "hours": Counter(), "lags": [], "backdated": 0, "first": None, "last": None})
        doc_user = {}
        for d in docs.values():
            reg = reg_for(max(d["entries"]))
            name = reg["user"] if reg else "(unknown)"
            doc_user[(d["type"], d["no"])] = name
            x = u[name]
            if d["type"] == INV:
                x["invoices"] += 1; x["lines"] += d["lines"]; x["value"] += d["value"]
                x["neg_lines"] += d["neg_lines"]; x["zero_lines"] += d["zero_lines"]
                lag = _lag(d["posting"], d["document"])
                if lag is not None:
                    x["lags"].append(lag)
            elif d["type"] == CM:
                x["credit_memos"] += 1
            elif d["type"] == RCPT:
                x["receipts"] += 1; x["receipt_lines"] += d["lines"]
            if reg:
                if reg["date"]:
                    x["days"].add(reg["date"])
                    created, posted = _d(reg["date"]), _d(d["posting"])
                    if created and posted and (created - posted).days > config.LATE_POSTING_DAYS:
                        x["backdated"] += 1
                h = _hour(reg["time"])
                if h is not None and d["type"] in (INV, CM):
                    x["hours"][h] += 1
                    hours[h] += 1
                stamp = f"{reg['date']} {reg['time'][:5]}"
                x["first"] = min(x["first"] or stamp, stamp)
                x["last"] = max(x["last"] or stamp, stamp)
        total_inv = sum(x["invoices"] for x in u.values()) or 1
        for name, x in u.items():
            peak = x["hours"].most_common(1)
            users.append({
                "user": name, "invoices": x["invoices"], "lines": x["lines"], "value": x["value"],
                "avg_lines": x["lines"] / x["invoices"] if x["invoices"] else 0,
                "share": x["invoices"] / total_inv * 100, "credit_memos": x["credit_memos"],
                "credit_ratio": x["credit_memos"] / x["invoices"] * 100 if x["invoices"] else None,
                "receipts": x["receipts"], "receipt_lines": x["receipt_lines"],
                "neg_lines": x["neg_lines"], "zero_lines": x["zero_lines"],
                "active_days": len(x["days"]),
                "per_day": x["invoices"] / len(x["days"]) if x["days"] else None,
                "avg_lag": sum(x["lags"]) / len(x["lags"]) if x["lags"] else None,
                "late": sum(1 for l in x["lags"] if l > config.LATE_POSTING_DAYS),
                "backdated": x["backdated"], "peak_hour": peak[0][0] if peak else None,
                "first": x["first"], "last": x["last"],
            })
        users.sort(key=lambda z: z["invoices"], reverse=True)
        for row in suspicious + biggest:
            row["user"] = doc_user.get((INV, row["doc"]))
    else:
        user_status = {"available": False, "reason": (register_status or {}).get("error") if isinstance(register_status, dict) else None}

    return {
        "kpis": {
            "invoices": vc["invoices"], "invoices_prev": vp["invoices"], "invoices_pct": _pct(vc["invoices"], vp["invoices"]),
            "credit_memos": vc["credit_memos"], "credit_memos_prev": vp["credit_memos"], "credit_ratio": vc["credit_ratio"],
            "value": vc["value"], "avg_value": vc["avg_value"], "vendors": vc["vendors"],
            "avg_lag": vc["avg_lag"], "avg_lag_prev": vp["avg_lag"], "same_day_pct": vc["same_day_pct"], "late": vc["late"],
            "item_invoices": len(inv_docs), "item_lines": lines, "item_lines_prev": lines_prev,
            "avg_lines": lines / len(inv_docs) if inv_docs else 0,
            "avg_lines_prev": lines_prev / len(inv_prev) if inv_prev else 0,
            "max_lines": max((d["lines"] for d in inv_docs), default=0),
            "receipts": sum(1 for d in docs.values() if d["type"] == RCPT),
            "suspicious": len(suspicious),
        },
        "buckets": [{"label": b, "count": buckets.get(b, 0)} for b in order],
        "daily": dict(sorted(daily.items())), "vendors": vendors, "late": late,
        "suspicious": suspicious[:25], "biggest": biggest,
        "users": users, "hours": hours, "user_status": user_status,
    }
