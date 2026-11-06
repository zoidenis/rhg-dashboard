"""Finance tower → Financial statements → Balance sheet and Liquidity & capital, per legal entity, from the one KPI
library (kpilib): the balance sheet by line at a date against the end of the month before and the end of last
year, the liquidity and debt figures, the equity bridge and the maturity of what is owed to suppliers.

A balance sheet is a position at a date, never cut by location: Business Central's departments carry no
methodology for the balance sheet. Every figure says its status (Pilot until Finance approves the account
mapping; Content required where a fact is missing, with what is needed). Nothing is written to Business Central.
"""
from datetime import date, timedelta

import kpilib

SECTIONS = [
    ("Assets", ["cash", "receivables", "inventory", "fixed_assets", "investments", "related_assets", "other_assets"]),
    ("Liabilities", ["payables", "tax_liabilities", "payroll", "loans", "leases", "other_liabilities", "ic_liabilities"]),
    ("Equity", ["share_capital", "equity", "_retained", "_current"]),
]
MATURITY = [("Overdue", None, -1), ("0–30 days", 0, 30), ("31–60 days", 31, 60), ("61–90 days", 61, 90),
            ("Over 90 days", 91, None)]


def _f(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def dates(d):
    """The comparison dates of a balance sheet at d: the end of the month before and the end of last year."""
    pm = date(d.year, d.month, 1) - timedelta(days=1)
    py = date(d.year - 1, 12, 31)
    return pm, py


def position(rows_bal, rows_ytd, m):
    """The balance sheet at a date, equity split into share capital, other equity accounts, the result of prior
    years not closed to equity and the result of the current year. rows_ytd: the same date's chart with the
    net change from 1 January (the current year's result)."""
    B = kpilib.bs_lines(rows_bal, m)
    cy = kpilib.pl_lines(rows_ytd, m)["net_profit"] if rows_ytd is not None else 0.0
    lines = dict(B["lines"])
    result = B["result_in_equity"]
    lines["equity"] = round(lines["equity"] - result, 2)          # the equity accounts alone
    lines["_retained"] = round(result - cy, 2)
    lines["_current"] = round(cy, 2)
    # cash accounts with a credit balance: overdrafts, for the debt figures
    overdraft = -sum(v for _no, _n, v in B["accounts"]["cash"] if v < 0)
    return {"lines": lines, "accounts": B["accounts"], "totals": B["totals"], "unmapped": B["unmapped"],
            "overdraft": round(overdraft, 2), "cash_positive": round(lines["cash"] + overdraft, 2)}


def statement(now, pm, py, m):
    label = {**{k: kpilib.LINE[k]["label"] for k in kpilib.LINE},
             "_retained": "Result of prior years not closed to equity", "_current": "Profit or loss of the current year"}
    out = []
    for name, keys in SECTIONS:
        rows = []
        for k in keys:
            v, a, b = now["lines"].get(k, 0.0), (pm or {}).get("lines", {}).get(k), (py or {}).get("lines", {}).get(k)
            if not m and k in kpilib.LINE:     # no mapping: the lines are unknown, not nil
                v = a = b = None
            if k in kpilib.LINE and not kpilib.filled(m, k):
                status = "Content required"
            else:
                status = "Approved" if (m or {}).get("status") == "Approved" else ("Pilot" if m else "Content required")
            rows.append({"key": k, "label": label[k], "current": v, "month_end": a, "year_end": b,
                         "vs_month": (v - a) if None not in (a, v) else None,
                         "vs_year": (v - b) if None not in (b, v) else None,
                         "status": status,
                         "accounts": [{"account": no, "name": n, "value": val} for no, n, val in
                                      (now["accounts"].get(k) or []) if abs(val) >= 0.5][:40] if k in kpilib.LINE else []})
        tot = {"current": sum(r["current"] for r in rows) if m else None,
               "month_end": sum(r["month_end"] or 0 for r in rows) if (pm and m) else None,
               "year_end": sum(r["year_end"] or 0 for r in rows) if (py and m) else None}
        out.append({"section": name, "lines": rows, **tot})
    return out


def maturity(vendor_rows, today, companies=()):
    """What is owed to suppliers by due date: overdue, then the days ahead; by supplier and currency, the group
    companies apart (a supplier whose name is a Business Central company)."""
    group = {c.strip().upper() for c in companies if c}
    buckets = {b[0]: 0.0 for b in MATURITY}
    by_vendor, by_ccy, ic = {}, {}, {}
    no_date = 0.0
    for r in vendor_rows or []:
        amt = -_f(r.get("Remaining_Amt_LCY"))
        if not amt:
            continue
        try:
            due = date.fromisoformat(str(r.get("Due_Date") or "")[:10])
            if due.year < 1900:
                raise ValueError
        except ValueError:
            no_date += amt
            continue
        days = (due - today).days
        b = next(lb for lb, lo, hi in MATURITY if (lo is None and days <= hi) or (hi is None and days >= lo)
                 or (lo is not None and hi is not None and lo <= days <= hi))
        buckets[b] += amt
        name = (r.get("Vendor_Name") or r.get("Vendor_No") or "?").strip()
        is_ic = any(g and g in name.upper() for g in group)
        V = (ic if is_ic else by_vendor).setdefault(name, {"vendor": name, "no": r.get("Vendor_No"), "total": 0.0,
                                                           **{lb: 0.0 for lb, *_ in MATURITY}})
        V["total"] += amt
        V[b] += amt
        c = (r.get("Currency_Code") or "").strip() or "LCY"
        C = by_ccy.setdefault(c, {"currency": c, "total": 0.0, **{lb: 0.0 for lb, *_ in MATURITY}})
        C["total"] += amt
        C[b] += amt
    vend = sorted(by_vendor.values(), key=lambda x: -x["total"])
    return {"buckets": [{"label": lb, "value": round(buckets[lb], 2)} for lb, *_ in MATURITY],
            "total": round(sum(buckets.values()), 2), "no_due_date": round(no_date, 2),
            "vendors": vend[:25], "vendors_n": len(vend), "currencies": sorted(by_ccy.values(), key=lambda x: -x["total"]),
            "intercompany": sorted(ic.values(), key=lambda x: -x["total"]),
            "next_30": round(buckets["Overdue"] + buckets["0–30 days"], 2)}


def liquidity(now, pm, py, m, p_ltm, approved):
    """The liquidity and capital figures with their status."""
    L = now["lines"]
    st = "Approved" if approved else ("Pilot" if m else "Content required")

    def k(key, value, status=None, note=""):
        D = kpilib.KPI.get(key, {})
        status = status or kpilib.kpi_status(m, D.get("needs", []))
        if status == "Content required":
            value = None                       # never a figure built on a line with no accounts
        return {"key": key, "label": D.get("label", key), "formula": D.get("formula", ""), "unit": D.get("unit", "money"),
                "value": None if value is None else round(value, 4 if D.get("unit") == "ratio" else 2),
                "status": status, "note": note}

    def debt(P):
        return P["lines"]["loans"] + P["lines"]["leases"] + P["overdraft"]
    eq = now["totals"]["equity"]
    gross = debt(now)
    net = gross - now["cash_positive"]
    owc = L["inventory"] + L["receivables"] - L["payables"]
    out = [k("cash_bank", now["cash_positive"], note=f"{now['overdraft']:,.0f} in cash and bank accounts with a credit "
                                                     f"balance is counted as debt" if now["overdraft"] else ""),
           k("owc", owc),
           k("gross_debt", gross, note="Lease liabilities: no account in the mapping (Content required)"
             if not str(((m or {}).get("rules") or {}).get("leases") or "").strip() else ""),
           k("net_debt", net, note="Lease liabilities: no account in the mapping (Content required)"
             if not str(((m or {}).get("rules") or {}).get("leases") or "").strip() else ""),
           k("debt_equity", gross / eq if eq and eq > 0 else None,
             note="" if eq and eq > 0 else "Equity is nil or negative: the ratio has no meaning"),
           k("total_equity", eq)]
    ebitda_ltm = p_ltm["derived"]["ebitda"] if p_ltm else None
    interest = sum(v for no, _n, v in (p_ltm or {}).get("accounts", {}).get("finance", []) if str(no).startswith("667")) \
        if p_ltm else None
    if approved and ebitda_ltm:
        out.append(k("net_debt_ebitda", net / ebitda_ltm if ebitda_ltm > 0 else None, "Approved",
                     f"EBITDA of the last 12 months {ebitda_ltm:,.0f}"))
        out.append(k("interest_cover", ebitda_ltm / interest if interest and interest > 0 else None, "Approved",
                     f"interest expense (667) of the last 12 months {interest or 0:,.0f}"))
    else:
        out.append(k("net_debt_ebitda", None, "Pending approval",
                     "Shown once Finance approves the account mapping, so EBITDA and debt are validated"))
        out.append(k("interest_cover", None, "Pending approval",
                     "Shown once Finance approves the account mapping and the definition of interest"))
    need = ("which loans (468, 46801), loans given (46802) and deposits (418*) fall due within 12 months; Business "
            "Central does not hold the split")
    out.append(k("current_ratio", None, "Content required", "Needs " + need))
    out.append(k("quick_ratio", None, "Content required", "Needs " + need))
    for x in out:
        x["month_end"] = x["year_end"] = None
    # the same figures at the comparison dates, where they can be computed
    for P, field in ((pm, "month_end"), (py, "year_end")):
        if not P:
            continue
        PL = P["lines"]
        vals = {"cash_bank": P["cash_positive"], "owc": PL["inventory"] + PL["receivables"] - PL["payables"],
                "gross_debt": debt(P), "net_debt": debt(P) - P["cash_positive"], "total_equity": P["totals"]["equity"],
                "debt_equity": debt(P) / P["totals"]["equity"] if P["totals"]["equity"] > 0 else None}
        for x in out:
            if x["key"] in vals and x["value"] is not None and vals[x["key"]] is not None:
                x[field] = round(vals[x["key"]], 4 if x["unit"] == "ratio" else 2)
    return out, st


def equity_bridge(now, py, m):
    """Opening equity (end of last year) + profit or loss of the year + capital contributions − distributions ±
    other movements = closing equity. Distributions have no account in the mapping: Content required."""
    if not py:
        return None
    if not m:
        return {"opening": None, "profit": None, "contributions": None, "distributions": None, "other": None,
                "closing": None, "status": "Content required", "notes": ["Needs the KPI mapping of this company."]}
    opening, closing = py["totals"]["equity"], now["totals"]["equity"]
    profit = now["lines"]["_current"]
    has_cap = bool(str(((m or {}).get("rules") or {}).get("share_capital") or "").strip())
    contrib = (now["lines"]["share_capital"] - py["lines"]["share_capital"]) if has_cap else None
    other = closing - opening - profit - (contrib or 0)
    return {"opening": round(opening, 2), "profit": round(profit, 2),
            "contributions": None if contrib is None else round(contrib, 2),
            "distributions": None, "other": round(other, 2), "closing": round(closing, 2),
            "status": "Approved" if (m or {}).get("status") == "Approved" else ("Pilot" if m else "Content required"),
            "notes": ["Distributions (dividends): no account is mapped for them; Content required from Finance. Any "
                      "distribution sits in Other movements until then.",
                      "Other movements = closing − opening − profit − contributions: anything else posted to equity "
                      "(corrections, the result closed to 109) shows here and needs an explanation."]
            + ([] if has_cap else ["Share capital has no account in the mapping: contributions cannot be told apart."])}
