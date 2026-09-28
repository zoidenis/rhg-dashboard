"""Turns Business Central balances and ledger entries into finance management figures.

Sign convention: BC stores income and liabilities as credits (negative). Here
revenue, liabilities and equity are presented as positive management figures and
the conversion is stated wherever it happens.
"""
from collections import defaultdict
from datetime import date, timedelta

DEFAULT_ACCOUNT_RULES = {
    # Used only where Account Category is blank in Business Central. The Albanian and
    # Kosovo charts both number class 7 as income, 60x as cost of goods and the rest of
    # class 6 as expense, so the account number answers the question the missing field
    # was meant to answer.
    "income_prefixes": "7",
    "cogs_prefixes": "60",
    "expense_prefixes": "6",
    "equity_prefixes": "1",
}

CATEGORY_INCOME = "Income"
CATEGORY_COGS = "Cost of Goods Sold"
CATEGORY_EXPENSE = "Expense"
BS_CATEGORIES = ("Assets", "Liabilities", "Equity")
DA_HINTS = ("depreciation", "amortization", "amortisation", "amortizim", "zhvler")


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


def _prefixes(text, fallback):
    return tuple(p.strip() for p in (text or fallback).split(",") if p.strip())


def account_class(acc, rules=None):
    """The account's category, and how it was decided.

    Business Central's Account Category is the first source. In companies where it was
    never filled in it comes back blank, and a blank field must not silently turn revenue
    into a negative expense, so the account number decides instead and the method is
    reported. Balance-sheet accounts with no category and no matching number fall back to
    the side their balance sits on, which is an inference, not a fact.
    """
    rules = rules or DEFAULT_ACCOUNT_RULES
    category = (acc.get("Account_Category") or "").strip()
    if category:
        return category, "Account Category"
    no = str(acc.get("No") or "").strip()
    statement = acc.get("Income_Balance") == "Income Statement"
    if statement:
        if no.startswith(_prefixes(rules.get("income_prefixes"), "7")):
            return CATEGORY_INCOME, "Account number"
        if no.startswith(_prefixes(rules.get("cogs_prefixes"), "60")):
            return CATEGORY_COGS, "Account number"
        if no.startswith(_prefixes(rules.get("expense_prefixes"), "6")):
            return CATEGORY_EXPENSE, "Account number"
        return CATEGORY_EXPENSE, "Assumed expense"
    if no.startswith(_prefixes(rules.get("equity_prefixes"), "1")):
        return "Equity", "Account number"
    balance = _f(acc.get("Balance_at_Date"))
    return ("Liabilities" if balance < 0 else "Assets"), "Balance sign"


def _is_da(acc):
    text = f"{acc.get('Name','')} {acc.get('Account_Subcategory_Descript','')}".lower()
    return any(h in text for h in DA_HINTS)


# --------------------------------------------------------------------------- P&L
def profit_and_loss(cur, prev=None, last_year=None, budget_rows=None, rules=None):
    """cur/prev/last_year: chart-of-accounts rows with Net_Change for the period."""
    rules = rules or DEFAULT_ACCOUNT_RULES
    def index(rows):
        return {r["No"]: r for r in (rows or []) if r.get("Account_Type") == "Posting"}

    c, p, l = index(cur), index(prev), index(last_year)
    budget = defaultdict(float)
    for b in (budget_rows or []):
        budget[str(b.get("G_L_Account_No"))] += _f(b.get("Amount"))

    def value(cat, row):
        """Management sign: revenue positive, cost positive."""
        net = _f(row.get("Net_Change"))
        return -net if cat == CATEGORY_INCOME else net

    lines, totals = [], defaultdict(float)
    methods = defaultdict(int)
    for no, acc in c.items():
        if acc.get("Income_Balance") != "Income Statement":
            continue
        cat, method = account_class(acc, rules)
        methods[method] += 1
        cur_v = value(cat, acc)
        prev_v = value(cat, p.get(no, {})) if p else None
        ly_v = value(cat, l.get(no, {})) if l else None
        bud = abs(budget.get(no, 0.0)) if budget else None
        bucket = ("revenue" if cat == CATEGORY_INCOME else
                  "cogs" if cat == CATEGORY_COGS else
                  "da" if _is_da(acc) else "opex")
        totals[bucket] += cur_v
        if prev_v is not None:
            totals[bucket + "_prev"] += prev_v
        if ly_v is not None:
            totals[bucket + "_ly"] += ly_v
        if bud is not None:
            totals[bucket + "_budget"] += bud
        if cur_v or prev_v or bud:
            lines.append({"account": no, "name": acc.get("Name"), "category": cat, "bucket": bucket,
                          "classified_by": method,
                          "subcategory": acc.get("Account_Subcategory_Descript") or "",
                          "current": cur_v, "previous": prev_v, "last_year": ly_v, "budget": bud})
    lines.sort(key=lambda r: (r["bucket"], -abs(r["current"])))

    revenue = totals["revenue"]
    gross = revenue - totals["cogs"]
    ebitda = gross - totals["opex"]
    ebit = ebitda - totals["da"]

    def block(name, cur_v, prev_v, ly_v, bud):
        return {"label": name, "current": cur_v, "previous": prev_v, "last_year": ly_v, "budget": bud,
                "vs_previous": (cur_v - prev_v) if prev_v is not None else None,
                "vs_previous_pct": ((cur_v - prev_v) / abs(prev_v) * 100) if prev_v else None,
                "vs_last_year_pct": ((cur_v - ly_v) / abs(ly_v) * 100) if ly_v else None,
                "vs_budget": (cur_v - bud) if bud else None,
                "vs_budget_pct": ((cur_v - bud) / abs(bud) * 100) if bud else None,
                "of_revenue": (cur_v / revenue * 100) if revenue else None}

    prev_rev, prev_cogs, prev_opex, prev_da = (totals["revenue_prev"], totals["cogs_prev"],
                                               totals["opex_prev"], totals["da_prev"])
    ly_rev, ly_cogs, ly_opex, ly_da = (totals["revenue_ly"], totals["cogs_ly"],
                                       totals["opex_ly"], totals["da_ly"])
    statement = [
        block("Revenue", revenue, prev_rev, ly_rev, totals.get("revenue_budget")),
        block("Cost of goods sold", totals["cogs"], prev_cogs, ly_cogs, totals.get("cogs_budget")),
        block("Gross profit", gross, prev_rev - prev_cogs, ly_rev - ly_cogs, None),
        block("Operating expenses", totals["opex"], prev_opex, ly_opex, totals.get("opex_budget")),
        block("EBITDA", ebitda, prev_rev - prev_cogs - prev_opex, ly_rev - ly_cogs - ly_opex, None),
        block("Depreciation and amortization", totals["da"], prev_da, ly_da, totals.get("da_budget")),
        block("EBIT", ebit, prev_rev - prev_cogs - prev_opex - prev_da,
              ly_rev - ly_cogs - ly_opex - ly_da, None),
    ]
    # A negative cost of sales is not a profit. RHG posts purchases to the materials
    # accounts and moves them to inventory at period end, so inside an unfinished month
    # the cost accounts can carry a net credit. When that happens the gross figure is
    # arithmetically larger than revenue, which is not a result anyone should read.
    validation = []
    cogs_credit = totals["cogs"] < 0
    margin_ok = revenue > 0 and not cogs_credit and gross <= revenue
    if cogs_credit:
        validation.append({
            "figure": "Cost of goods sold", "state": "Under validation",
            "reason": f"The cost accounts net to a credit of {abs(totals['cogs']):,.0f} in this period. "
                      f"Purchases are posted to the materials accounts and moved to inventory at period "
                      f"end, so in an unfinished month the cost can be negative and the gross profit "
                      f"meaningless. Compare the inventory movement on the balance sheet before reading "
                      f"a margin.",
            "owner": "Chief accountant", "suppressed": ["Gross profit", "Gross margin", "EBITDA margin"]})
    elif gross > revenue:
        validation.append({
            "figure": "Gross profit", "state": "Under validation",
            "reason": "Gross profit is larger than revenue, which cannot be true. Check the account "
                      "classification and the sign of the cost accounts for this period.",
            "owner": "Chief accountant", "suppressed": ["Gross margin"]})
    return {
        "statement": statement, "lines": lines[:250],
        "validation": validation, "margin_reliable": margin_ok,
        "kpis": {"revenue": revenue, "cogs": totals["cogs"],
                 "gross": gross if margin_ok else None,
                 "gross_raw": gross,
                 "gross_margin": (gross / revenue * 100) if margin_ok else None,
                 "opex": totals["opex"], "ebitda": ebitda if margin_ok else None,
                 "ebitda_raw": ebitda,
                 "ebitda_margin": (ebitda / revenue * 100) if margin_ok else None,
                 "da": totals["da"], "ebit": ebit,
                 "revenue_prev": prev_rev, "gross_prev": prev_rev - prev_cogs,
                 "revenue_ly": ly_rev,
                 "revenue_budget": totals.get("revenue_budget") or None},
        "classified_by": dict(methods),
        "note": "Revenue, cost and expense follow the Account Category in Business Central where it is "
                "filled in, and the account number where it is blank. Depreciation is separated by "
                "account name, so review the split before publishing.",
    }


# ------------------------------------------------------------------- balance sheet
def balance_sheet(cur, prev=None, last_year=None, rules=None, result_rows=None):
    """result_rows: the same chart of accounts, used to bring in the result of the period.

    A balance sheet has to carry the profit earned so far. RHG has never closed its
    income statement accounts to equity, so reading only the balance-sheet accounts
    leaves the whole accumulated result out and the statement cannot balance.
    """
    rules = rules or DEFAULT_ACCOUNT_RULES
    def index(rows):
        return {r["No"]: r for r in (rows or []) if r.get("Account_Type") == "Posting"}

    c, p, l = index(cur), index(prev), index(last_year)
    groups = defaultdict(lambda: {"current": 0.0, "previous": 0.0, "last_year": 0.0, "lines": []})
    flags = []
    methods = defaultdict(int)
    for no, acc in c.items():
        if acc.get("Income_Balance") != "Balance Sheet":
            continue
        cat, method = account_class(acc, rules)
        methods[method] += 1
        raw = _f(acc.get("Balance_at_Date"))
        value = raw if cat == "Assets" else -raw        # liabilities and equity shown positive
        prev_v = (_f(p[no].get("Balance_at_Date")) if no in p else 0.0)
        prev_v = prev_v if cat == "Assets" else -prev_v
        ly_v = (_f(l[no].get("Balance_at_Date")) if no in l else 0.0)
        ly_v = ly_v if cat == "Assets" else -ly_v
        g = groups[cat]
        g["current"] += value
        g["previous"] += prev_v
        g["last_year"] += ly_v
        line = {"account": no, "name": acc.get("Name"), "category": cat,
                "subcategory": acc.get("Account_Subcategory_Descript") or "",
                "current": value, "previous": prev_v, "last_year": ly_v,
                "movement": value - prev_v,
                "movement_pct": ((value - prev_v) / abs(prev_v) * 100) if prev_v else None}
        g["lines"].append(line)
        line["classified_by"] = method
        if method != "Balance sign" and value < 0 and abs(value) > 1:
            flags.append({"account": no, "name": acc.get("Name"), "category": cat, "value": value,
                          "issue": "Unexpected sign", "detail":
                          f"{cat} account carrying a {'credit' if cat == 'Assets' else 'debit'} balance."})
    for g in groups.values():
        g["lines"].sort(key=lambda r: -abs(r["current"]))
    # the accumulated result sitting in the income statement accounts
    result = result_prev = result_ly = 0.0
    for no, acc in index(result_rows if result_rows is not None else cur).items():
        if acc.get("Income_Balance") != "Income Statement":
            continue
        result -= _f(acc.get("Balance_at_Date"))
        if no in p:
            result_prev -= _f(p[no].get("Balance_at_Date"))
        if no in l:
            result_ly -= _f(l[no].get("Balance_at_Date"))
    if abs(result) > 1:
        groups["Equity"]["current"] += result
        groups["Equity"]["previous"] += result_prev
        groups["Equity"]["last_year"] += result_ly
        groups["Equity"]["lines"].append({
            "account": "—", "name": "Result for the period, not yet closed to equity",
            "category": "Equity", "subcategory": "Retained result",
            "current": result, "previous": result_prev, "last_year": result_ly,
            "movement": result - result_prev,
            "movement_pct": ((result - result_prev) / abs(result_prev) * 100) if result_prev else None,
            "classified_by": "Income statement accounts, accumulated"})
        groups["Equity"]["lines"].sort(key=lambda r: -abs(r["current"]))
    assets = groups["Assets"]["current"]
    liabilities = groups["Liabilities"]["current"]
    equity = groups["Equity"]["current"]
    return {
        "groups": {k: {"current": v["current"], "previous": v["previous"], "last_year": v["last_year"],
                       "lines": v["lines"][:120]} for k, v in groups.items()},
        "totals": {"assets": assets, "liabilities": liabilities, "equity": equity,
                   "result_in_equity": result,
                   "liabilities_and_equity": liabilities + equity,
                   "difference": assets - (liabilities + equity),
                   "balances": abs(assets - (liabilities + equity)) < max(1.0, abs(assets) * 0.0001)},
        "flags": sorted(flags, key=lambda f: -abs(f["value"]))[:40],
        "classified_by": dict(methods),
        "note": "Balances are taken at the end of the selected period. Liabilities and equity are shown "
                "as positive management figures; in BC they are credit balances. The result accumulated "
                "in the income statement accounts is carried into equity as its own line, because those "
                "accounts have never been closed; without it the statement cannot balance. The difference "
                "line is what remains after that, and should be nil.",
    }


# ------------------------------------------------------------------------- aging
BUCKETS = [("Not due", -10 ** 6, 0), ("1-30 days", 1, 30), ("31-60 days", 31, 60),
           ("61-90 days", 61, 90), ("Over 90 days", 91, 10 ** 6)]


def _aging(rows, party_no, party_name, today, settings, kind):
    th = settings["thresholds"]
    buckets = {b[0]: 0.0 for b in BUCKETS}
    parties = defaultdict(lambda: {"name": "", "total": 0.0, "overdue": 0.0, "oldest": None, "entries": 0})
    total = overdue = 0.0
    detail, exceptions = [], []
    for r in rows:
        remaining = _f(r.get("Remaining_Amt_LCY"))
        if kind == "ap":
            remaining = -remaining                     # payables are credits in BC
        if abs(remaining) < 0.01:
            continue
        due = _d(r.get("Due_Date"))
        days = (today - due).days if due else 0
        for name, lo, hi in BUCKETS:
            if lo <= days <= hi:
                buckets[name] += remaining
                break
        total += remaining
        if days > 0:
            overdue += remaining
        p = parties[r.get(party_no)]
        p["name"] = r.get(party_name) or r.get(party_no)
        p["total"] += remaining
        p["entries"] += 1
        if days > 0:
            p["overdue"] += remaining
            p["oldest"] = max(p["oldest"] or 0, days)
        detail.append({"party": p["name"], "document": r.get("Document_No"), "type": r.get("Document_Type"),
                       "posting": r.get("Posting_Date"), "due": r.get("Due_Date"), "days": days,
                       "currency": r.get("Currency_Code") or "", "remaining": remaining})
        limit = th["ar_critical_days"] if kind == "ar" else th["ap_critical_days"]
        if days > limit and abs(remaining) >= th["materiality"]:
            exceptions.append({
                "code": f"{kind}-old-{r.get('Entry_No')}", "severity": "critical",
                "rule": "Overdue balance beyond the critical limit",
                "title": f"{p['name']}: {remaining:,.0f} {'receivable' if kind == 'ar' else 'payable'} "
                         f"overdue {days} days",
                "detail": f"Document {r.get('Document_No')} dated {r.get('Posting_Date')}, due "
                          f"{r.get('Due_Date')}, remaining {remaining:,.0f}.",
                "impact": remaining, "document": r.get("Document_No"), "area": kind.upper(),
                "next_action": "Chase the collection." if kind == "ar" else "Confirm the payment plan.",
                "drill": {"kind": kind, "key": ""}})
        if kind == "ap" and remaining < -th["materiality"]:
            exceptions.append({
                "code": f"ap-debit-{r.get('Entry_No')}", "severity": "warning",
                "rule": "Debit balance on a supplier account",
                "title": f"{p['name']}: supplier account in debit {abs(remaining):,.0f}",
                "detail": f"Document {r.get('Document_No')} leaves the supplier owing us money; "
                          f"often an unapplied credit note or a double payment.",
                "impact": abs(remaining), "document": r.get("Document_No"), "area": "AP",
                "next_action": "Apply the credit note or request a refund.",
                "drill": {"kind": "ap", "key": ""}})
    party_rows = sorted(({"party": v["name"], "total": v["total"], "overdue": v["overdue"],
                          "oldest_days": v["oldest"], "entries": v["entries"]}
                         for v in parties.values()), key=lambda x: -abs(x["total"]))
    detail.sort(key=lambda x: -abs(x["remaining"]))
    return {"total": total, "overdue": overdue, "current": total - overdue,
            "buckets": [{"label": b[0], "value": buckets[b[0]]} for b in BUCKETS],
            "parties": party_rows[:40], "detail": detail[:400], "exceptions": exceptions,
            "concentration": (abs(party_rows[0]["total"]) / abs(total) * 100) if party_rows and total else None}


def payables(rows, today, settings):
    return _aging(rows, "Vendor_No", "Vendor_Name", today, settings, "ap")


def receivables(rows, today, settings):
    return _aging(rows, "Customer_No", "Customer_Name", today, settings, "ar")


# --------------------------------------------------------------------------- cash
CASH_HINTS = ("cash", "bank", "arka", "banka")


def cash_position(accounts, bank_rows, settings):
    lines = []
    for acc in accounts or []:
        if acc.get("Income_Balance") != "Balance Sheet" or acc.get("Account_Type") != "Posting":
            continue
        text = f"{acc.get('Name','')} {acc.get('Account_Subcategory_Descript','')}".lower()
        if acc.get("Account_Category") == "Assets" and any(h in text for h in CASH_HINTS):
            lines.append({"account": acc["No"], "name": acc.get("Name"),
                          "balance": _f(acc.get("Balance_at_Date")),
                          "movement": _f(acc.get("Net_Change"))})
    lines.sort(key=lambda r: -abs(r["balance"]))
    by_bank = defaultdict(lambda: {"name": "", "in": 0.0, "out": 0.0, "currency": ""})
    for r in bank_rows or []:
        b = by_bank[r.get("Bank_Account_No")]
        b["name"] = r.get("Bank_Account_Name") or r.get("Bank_Account_No")
        b["currency"] = r.get("Currency_Code") or ""
        amt = _f(r.get("Amount_LCY"))
        b["in" if amt >= 0 else "out"] += amt
    movements = sorted(({"bank": v["name"], "currency": v["currency"], "in": v["in"], "out": v["out"],
                         "net": v["in"] + v["out"]} for v in by_bank.values()),
                       key=lambda x: -abs(x["net"]))
    return {"accounts": lines[:30], "total": sum(l["balance"] for l in lines),
            "movement": sum(l["movement"] for l in lines), "bank_movements": movements[:30],
            "note": "Cash is read from the balance-sheet accounts whose name or subcategory identifies them "
                    "as cash or bank. Bank movements come from the bank ledger of the selected period."}


# --------------------------------------------------------- general ledger control
def gl_control(entries, settings, period_from, period_to, closed_before=None):
    th = settings["thresholds"]
    rows, exceptions = [], []
    round_hits = 0
    for e in entries or []:
        amount = _f(e.get("Amount"))
        posting, doc = _d(e.get("Posting_Date")), _d(e.get("Document_Date"))
        manual = (e.get("Source_Code") or "").upper() in ("GENJNL", "GENERAL", "")
        row = {"entry": e.get("Entry_No"), "account": e.get("G_L_Account_No"),
               "account_name": e.get("G_L_Account_Name"), "posting": e.get("Posting_Date"),
               "document_date": e.get("Document_Date"), "document": e.get("Document_No"),
               "source": e.get("Source_Code"), "reason": e.get("Reason_Code"), "amount": amount,
               "manual": manual, "dimension_set": e.get("Dimension_Set_ID")}
        rows.append(row)
        if manual and abs(amount) >= th["manual_journal"]:
            exceptions.append({"code": f"gl-manual-{e.get('Entry_No')}", "severity": "warning",
                               "rule": "Large manual journal", "area": "G/L",
                               "title": f"Manual entry {amount:,.0f} on {e.get('G_L_Account_No')} "
                                        f"{e.get('G_L_Account_Name') or ''}".strip(),
                               "detail": f"Document {e.get('Document_No')} posted {e.get('Posting_Date')} "
                                         f"with source code {e.get('Source_Code') or 'none'}.",
                               "impact": abs(amount), "document": e.get("Document_No"),
                               "next_action": "Review the supporting documentation.",
                               "drill": {"kind": "gl", "key": ""}})
        if posting and doc and (posting - doc).days > th["prior_period_days"]:
            exceptions.append({"code": f"gl-late-{e.get('Entry_No')}", "severity": "warning",
                               "rule": "Entry posted long after the document date", "area": "G/L",
                               "title": f"Entry {e.get('Document_No')} posted {(posting - doc).days} days "
                                        f"after the document date",
                               "detail": f"Document dated {e.get('Document_Date')}, posted {e.get('Posting_Date')}, "
                                         f"amount {amount:,.0f}.",
                               "impact": abs(amount), "document": e.get("Document_No"),
                               "next_action": "Check which period the cost belongs to.",
                               "drill": {"kind": "gl", "key": ""}})
        if closed_before and posting and posting < closed_before:
            exceptions.append({"code": f"gl-closed-{e.get('Entry_No')}", "severity": "critical",
                               "rule": "Posting inside a closed period", "area": "G/L",
                               "title": f"Entry {e.get('Document_No')} posted into a closed period",
                               "detail": f"Posting date {e.get('Posting_Date')} is before the close date "
                                         f"{closed_before.isoformat()}.",
                               "impact": abs(amount), "document": e.get("Document_No"),
                               "next_action": "Confirm the period status with the chief accountant.",
                               "drill": {"kind": "gl", "key": ""}})
        if amount and abs(amount) % 100000 == 0 and abs(amount) >= th["materiality"]:
            round_hits += 1
    if round_hits:
        exceptions.append({"code": f"gl-round-{period_from}", "severity": "info",
                           "rule": "Round-value entries", "area": "G/L",
                           "title": f"{round_hits} material entries with perfectly round amounts",
                           "detail": "Round amounts are often estimates or accruals; worth confirming that "
                                     "each has support.",
                           "impact": 0, "document": "", "next_action": "Spot-check the largest ones.",
                           "drill": {"kind": "gl", "key": ""}})
    rows.sort(key=lambda r: -abs(r["amount"]))
    return {"entries": rows[:300], "count": len(rows), "exceptions": exceptions,
            "threshold": th["materiality"],
            "note": "Only entries at or above the materiality threshold are read: the ledger holds tens of "
                    "thousands of entries a day, so a full scan is not possible. BC does not publish the "
                    "posting user or timestamps, so entries cannot be attributed to a person."}
