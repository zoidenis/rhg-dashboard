"""Cash flow, treasury and the obligations that fall due next.

The indirect cash flow is built from the movement in the balance-sheet accounts
plus the result of the period, because that is what the published data supports.
The bank ledger of the period gives the direct movements. Anything that would
need payment plans or a cash-flow forecast module is not estimated here.
"""
from collections import defaultdict
from datetime import date, timedelta

RECEIVABLE_HINTS = ("receivable", "kliente", "debitor")
PAYABLE_HINTS = ("payable", "furnitor", "kreditor")
INVENTORY_HINTS = ("inventar", "magazin", "stok", "inventory")
CASH_HINTS = ("cash", "bank", "arka", "banka")
DA_HINTS = ("depreciation", "amortization", "amortizim", "zhvler")


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


def _hit(acc, hints):
    text = f"{acc.get('Name','')} {acc.get('Account_Subcategory_Descript','')}".lower()
    return any(h in text for h in hints)


def cash_flow(cur_accounts, settings):
    """Indirect cash flow for the period, from the accounts' net change."""
    result = wc_receivables = wc_payables = wc_inventory = da = cash_move = other = 0.0
    lines = []
    for a in cur_accounts or []:
        if a.get("Account_Type") != "Posting":
            continue
        net = _f(a.get("Net_Change"))
        if not net:
            continue
        cat = a.get("Account_Category")
        if a.get("Income_Balance") == "Income Statement":
            result += -net if cat == "Income" else -net      # income credit, cost debit: both flip to result
            if _hit(a, DA_HINTS):
                da += net
            continue
        if _hit(a, CASH_HINTS) and cat == "Assets":
            cash_move += net
        elif _hit(a, RECEIVABLE_HINTS):
            wc_receivables += -net                            # a rise in receivables consumes cash
        elif _hit(a, PAYABLE_HINTS):
            wc_payables += -net                               # BC credit: a rise releases cash
        elif _hit(a, INVENTORY_HINTS):
            wc_inventory += -net
        else:
            other += -net
    operating = result + da + wc_receivables + wc_payables + wc_inventory
    lines = [
        {"label": "Result for the period", "value": result,
         "note": "Income less cost and expense from the income-statement accounts."},
        {"label": "Depreciation and amortization added back", "value": da,
         "note": "Accounts identified by name; confirm the list before publishing."},
        {"label": "Change in receivables", "value": wc_receivables, "note": "A rise consumes cash."},
        {"label": "Change in payables", "value": wc_payables, "note": "A rise releases cash."},
        {"label": "Change in inventory", "value": wc_inventory, "note": "A rise consumes cash."},
        {"label": "Operating cash flow (indirect)", "value": operating, "note": "The four lines above."},
        {"label": "Other balance-sheet movements", "value": other,
         "note": "Fixed assets, loans, equity and anything not classified above."},
        {"label": "Movement on cash and bank accounts", "value": cash_move,
         "note": "The actual change in the cash accounts; it is the check figure for the lines above."},
        {"label": "Unexplained difference", "value": cash_move - (operating + other),
         "note": "What the classification above does not account for. A large figure means the account "
                 "names did not classify cleanly, not that cash is missing."},
    ]
    return {"lines": lines, "operating": operating, "cash_move": cash_move,
            "note": "Built from the net change of the accounts for the period. Investing and financing are "
                    "not split out: that needs an account-to-cash-flow mapping, which BC does not publish."}


def bank_activity(rows):
    by_account = defaultdict(lambda: {"in": 0.0, "out": 0.0, "currency": "", "name": "", "entries": 0})
    by_source = defaultdict(lambda: {"in": 0.0, "out": 0.0, "entries": 0})
    for r in rows or []:
        amt = _f(r.get("Amount_LCY"))
        a = by_account[r.get("Bank_Account_No")]
        a["name"] = r.get("Bank_Account_Name") or r.get("Bank_Account_No")
        a["currency"] = r.get("Currency_Code") or ""
        a["entries"] += 1
        a["in" if amt >= 0 else "out"] += amt
        s = by_source[r.get("Source_Code") or "(none)"]
        s["entries"] += 1
        s["in" if amt >= 0 else "out"] += amt
    accounts = sorted(({"account": k, **v, "net": v["in"] + v["out"]} for k, v in by_account.items()),
                      key=lambda x: -abs(x["net"]))
    sources = sorted(({"source": k, **v, "net": v["in"] + v["out"]} for k, v in by_source.items()),
                     key=lambda x: -abs(x["net"]))
    return {"accounts": accounts[:30], "sources": sources[:20],
            "in": sum(a["in"] for a in accounts), "out": sum(a["out"] for a in accounts)}


def obligations(ap_detail, ar_detail, cash_total, today=None):
    """What falls due in the next seven and thirty days, from the open ledger entries.

    Open entries are netted per supplier and per customer first. A supplier can carry an
    open invoice and an open payment of the same amount when the payment was never
    applied to the invoice in BC: the balance is nil, but both entries stay open. Without
    netting the invoice would be counted as due and the payment counted again in absolute
    terms, so one settled supplier would appear twice.
    """
    today = today or date.today()

    def net_by_party(rows):
        by_party = defaultdict(lambda: {"net": 0.0, "positive": [], "negative": 0.0})
        for r in rows or []:
            amount = r.get("remaining") or 0
            p = by_party[r.get("party")]
            p["net"] += amount
            if amount > 0:
                p["positive"].append(r)
            else:
                p["negative"] += -amount
        return by_party

    def window(rows, days):
        """Amounts genuinely outstanding that fall due within the window, oldest first."""
        limit = today + timedelta(days=days)
        total, items, unapplied = 0.0, [], 0.0
        for party, p in net_by_party(rows).items():
            if p["net"] <= 0.01:
                unapplied += p["negative"] if p["net"] < -0.01 else 0.0
                continue                       # nothing is owed by, or to, this party
            unapplied += p["negative"]
            left = p["net"]                    # only the net can actually fall due
            for r in sorted(p["positive"], key=lambda x: (x.get("due") or "")):
                if left <= 0.01:
                    break
                due = _d(r.get("due"))
                amount = min(r.get("remaining") or 0, left)
                left -= amount
                if not due or due > limit:
                    continue
                total += amount
                items.append({"party": party, "document": r.get("document"), "due": r.get("due"),
                              "days": r.get("days"), "amount": amount})
        items.sort(key=lambda x: (x["due"] or "", -abs(x["amount"])))
        return total, items[:60], unapplied

    pay7, pay7_items, unapplied_ap = window(ap_detail, 7)
    pay30, _, _ = window(ap_detail, 30)
    rec7, rec7_items, unapplied_ar = window(ar_detail, 7)
    rec30, _, _ = window(ar_detail, 30)
    return {
        "cash": cash_total,
        "due_7": pay7, "due_30": pay30, "expected_7": rec7, "expected_30": rec30,
        "net_7": cash_total + rec7 - pay7, "net_30": cash_total + rec30 - pay30,
        "payments": pay7_items, "receipts": rec7_items,
        "unapplied_ap": unapplied_ap, "unapplied_ar": unapplied_ar,
        "note": "Due dates come from the open supplier and customer entries, overdue amounts included, "
                "netted per party. Payments and credit notes left unapplied in BC reduce what is shown as "
                "due, because the money is no longer owed even though the entries are still open. "
                "Expected receipts assume customers pay on the due date, which is an assumption, not a "
                "forecast: BC publishes no payment plans or promised dates.",
    }


def exceptions(flow, obl, settings):
    th = settings["thresholds"]
    out = []
    if obl.get("unapplied_ap", 0) >= th["materiality"]:
        out.append({"code": "ap-unapplied", "severity": "warning", "area": "AP",
                    "rule": "Payments and credit notes not applied to invoices",
                    "title": f"{obl['unapplied_ap']:,.0f} of supplier payments or credits sit unapplied",
                    "detail": "The entries are still open in BC although the balance is settled, so aging, "
                              "statements and the payment proposal all read wrong until they are applied.",
                    "impact": obl["unapplied_ap"], "document": "",
                    "next_action": "Run Apply Entries on these supplier accounts in BC.",
                    "drill": {"kind": "ap", "key": ""}})
    if obl["net_7"] < 0:
        out.append({"code": "cash-7", "severity": "critical", "area": "Cash",
                    "rule": "Cash does not cover the next seven days",
                    "title": f"Seven-day position is {obl['net_7']:,.0f}",
                    "detail": f"Cash {obl['cash']:,.0f} plus receipts due {obl['expected_7']:,.0f} against "
                              f"payments due {obl['due_7']:,.0f}.",
                    "impact": abs(obl["net_7"]), "document": "",
                    "next_action": "Agree with the CFO which payments move and which collections are chased.",
                    "drill": {"kind": "payments", "key": ""}})
    elif obl["net_30"] < 0:
        out.append({"code": "cash-30", "severity": "warning", "area": "Cash",
                    "rule": "Cash does not cover the next thirty days",
                    "title": f"Thirty-day position is {obl['net_30']:,.0f}",
                    "detail": f"Cash {obl['cash']:,.0f} plus receipts due {obl['expected_30']:,.0f} against "
                              f"payments due {obl['due_30']:,.0f}.",
                    "impact": abs(obl["net_30"]), "document": "",
                    "next_action": "Plan the month's payment run against expected collections.",
                    "drill": {"kind": "payments", "key": ""}})
    gap = flow["lines"][-1]["value"]
    if abs(gap) >= th["materiality"] * 5:
        out.append({"code": "cf-gap", "severity": "info", "area": "Cash",
                    "rule": "Cash flow does not reconcile to the cash movement",
                    "title": f"{gap:,.0f} of the cash movement is not explained by the classification",
                    "detail": flow["lines"][-1]["note"],
                    "impact": abs(gap), "document": "",
                    "next_action": "Name the receivable, payable and inventory accounts so the classification "
                                   "catches them.",
                    "drill": {"kind": "cashflow", "key": ""}})
    return out
