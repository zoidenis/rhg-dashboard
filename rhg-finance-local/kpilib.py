"""The one KPI library of Pulse RHG: the reporting lines, the account mapping that fills them, and the KPI
definitions every screen uses (Executive dashboard, Finance tower P&L and Group view, P&L memo, brand report,
Unicorn), so the same indicator has one formula everywhere.

Mapping: account number → reporting line, per company, kept in data/kpi_mapping.json and edited in Finance →
Settings → KPI mapping. A rule is a list of account numbers, "641*" meaning every account starting 641; the
most specific rule wins (an exact number before a prefix, a longer prefix before a shorter one). Business
Central's Account Category is not used: it is wrong or blank in several companies (TBM's sales are "Expense",
most of RKS has none, SALT's bank accounts are "Liabilities").

Status of a company's mapping: "Proposed" (the default of the chart model, nobody has reviewed it), "Reviewed"
(saved by Finance), "Approved" (approved by name and date). A KPI built on it is "Pilot" until the mapping is
approved; "Approved" after; "Content required" when a line it needs has no accounts. Nothing is written to
Business Central.

Sign convention: management figures, income and costs both positive; balance sheet: assets positive,
liabilities and equity positive (BC holds them as credits).
"""
import json
import re
import threading
from datetime import datetime

import store

FILE = store.DATA_DIR / "kpi_mapping.json"
_lock = threading.Lock()

# (key, label, statement, sign) · sign: "income" (credit shown positive), "cost" (debit positive),
# "asset" (debit positive), "credit" (liability or equity, credit positive), "net" (income and costs mixed:
# costs positive, income negative)
LINES = [
    ("revenue", "Net revenue", "pl", "income"),
    ("other_income", "Other operating income", "pl", "income"),
    ("cogs", "Cost of goods sold", "pl", "cost"),
    ("labour", "Labour", "pl", "cost"),
    ("occupancy", "Occupancy", "pl", "cost"),
    ("opex", "Other operating expenses", "pl", "cost"),
    ("da", "Depreciation and amortisation", "pl", "cost"),
    ("finance", "Interest and finance costs (net)", "pl", "net"),
    ("nonop", "Non-operating items", "pl", "net"),
    ("tax", "Income tax", "pl", "cost"),
    ("cash", "Cash and bank", "bs", "asset"),
    ("receivables", "Trade receivables", "bs", "asset"),
    ("inventory", "Inventory", "bs", "asset"),
    ("fixed_assets", "Fixed assets (net)", "bs", "asset"),
    ("investments", "Investments in group companies", "bs", "asset"),
    ("related_assets", "Related parties and shareholders (receivable)", "bs", "asset"),
    ("other_assets", "Other assets", "bs", "asset"),
    ("payables", "Trade payables", "bs", "credit"),
    ("tax_liabilities", "Tax and VAT", "bs", "credit"),
    ("payroll", "Payroll liabilities", "bs", "credit"),
    ("loans", "Loans and borrowings", "bs", "credit"),
    ("leases", "Lease liabilities", "bs", "credit"),
    ("ic_liabilities", "Intercompany (payable)", "bs", "credit"),
    ("other_liabilities", "Other liabilities", "bs", "credit"),
    ("share_capital", "Share capital", "bs", "credit"),
    ("equity", "Other equity", "bs", "credit"),
]
LINE = {k: {"key": k, "label": lb, "statement": st, "sign": sg} for k, lb, st, sg in LINES}

# The P&L of the management pack ("Chart of Account MNG Pack vs. Tower Dashboard", sheet P&L GL Category, column
# P&L Cluster 2; Geri, 6 November 2026): the lines the Profit & loss shows, each in one of the KPI lines above (its
# group, which the KPIs, the Executive dashboard and the Group view use; Finance → Settings can change it).
# (key, label, section, group) · section: revenue, cogs, opex, below (D&A, interest, tax)
CLUSTERS = [
    ("service_revenue", "Service Revenue", "revenue", "revenue"),
    ("other_income_pack", "Other Income", "revenue", "other_income"),
    ("fx_gain", "Other Income - FX Gain", "revenue", "other_income"),
    ("cogs_pack", "Cost of Goods Sold", "cogs", "cogs"),
    ("payroll_exp", "Payroll Expense", "opex", "labour"),
    ("employees", "Employees Expenses", "opex", "opex"),
    ("recruiting", "Employees Expenses - Recruiting", "opex", "opex"),
    ("health", "Employees Expenses - Health Insurance", "opex", "opex"),
    ("uniforms", "Employees Expenses - Uniforms", "opex", "opex"),
    ("travel", "Employees Expenses - Travel & Accommodation", "opex", "opex"),
    ("marketing", "Marketing, Events, PR, Printing", "opex", "opex"),
    ("energy", "Logistics - Energy, Water, Fuel", "opex", "occupancy"),
    ("maintenance", "Logistics - Maintenance", "opex", "occupancy"),
    ("transport", "Logistics - Transportation", "opex", "opex"),
    ("telecom", "Post, Internet, Telecommunication", "opex", "opex"),
    ("rent", "Rent Expenses", "opex", "occupancy"),
    ("other_exp", "Other Expenses", "opex", "opex"),
    ("call_centre", "Other Expenses - Call Centre", "opex", "opex"),
    ("insurance", "Other Expenses - Insurance", "opex", "opex"),
    ("consulting", "Consulting (Legal, Audit, Accounting, IT)", "opex", "opex"),
    ("customs", "Other Expenses - Customs", "opex", "opex"),
    ("local_taxes", "Other Expenses - Local Taxes", "opex", "opex"),
    ("bank", "Other Expenses - Bank Expenses", "opex", "opex"),
    ("penalties", "Other Expenses - Penalty Fees", "opex", "opex"),
    ("fx_loss", "Other Expenses - FX Loss", "opex", "opex"),
    ("da_pack", "Depreciation & Amortization", "below", "da"),
    ("interest", "Net Interest", "below", "finance"),
    ("income_tax", "Income Tax - Corporate Tax (15%)", "below", "tax"),
]
CLUSTER = {k: {"key": k, "label": lb, "section": sec, "group": g} for k, lb, sec, g in CLUSTERS}
assert not set(CLUSTER) & set(LINE), "a line of the pack has the key of a KPI line"
PL_LINES = [k for k, _, st, _ in LINES if st == "pl"]
EQUITY_LINES = ("share_capital", "equity")
BS_LINES = [k for k, _, st, _ in LINES if st == "bs"]

# The chart model of SALT (TBM and Zoi Greece were opened from it). A proposal from the chart as read on
# 3 November 2026, to be reviewed by Finance; RKS has its own chart and no proposal.
# The P&L by the lines of the management pack (P&L GL Category, every account of the chart named); the KPI lines
# of the P&L stay empty: they are the sums of their lines (CLUSTERS).
PACK_PL = {
    "service_revenue": "704, 705, 707, 70711, 707126, 76, 761",
    "other_income_pack": "75, 751, 77, 707123, 7072, 708, 7903",
    "fx_gain": "769, 76901",
    "cogs_pack": "6011, 60111, 6012, 60121, 6013, 60122, 60123, 601231, 602, 6027, 60271, 6034, 605, 6051, 6581",
    "payroll_exp": "641, 6411, 6412, 6413, 6414, 64101, 64102, 64103, 64111, 64121, 64122, 64131, 644, 64401, 64402, 648",
    "employees": "622",
    "recruiting": "6276",
    "health": "68188",
    "uniforms": "61501",
    "travel": "625",
    "marketing": "649, 650, 654, 62401, 65802, 65803",
    "energy": "604, 60402, 60403, 60124, 60125, 60401",
    "maintenance": "615, 65801, 658, 6151",
    "transport": "627, 6271",
    "telecom": "626",
    "rent": "636",
    "other_exp": "6362, 666, 668, 687, 618, 624",
    "call_centre": "6361",
    "insurance": "616",
    "consulting": "67205, 65804, 6153, 6152",
    "customs": "632, 6321, 6322",
    "local_taxes": "601241, 634, 638",
    "bank": "628",
    "penalties": "657",
    "fx_loss": "669, 66901",
    "da_pack": "6813",
    "interest": "727, 667",
    "income_tax": "69",
}
SALT_MODEL = {
    **{k: "" for k in ("revenue", "other_income", "cogs", "labour", "occupancy", "opex", "da", "finance", "nonop",
                       "tax")},
    **PACK_PL,
    "cash": "51*, 53*, 58*",
    "receivables": "411*",
    "inventory": "3*",
    "fixed_assets": "20*, 21*, 28*",
    "investments": "23*, 26*",
    "related_assets": "467012, 467013, 467014, 467015, 467016, 467017, 467018, 46803, 46804",
    "other_assets": "418*, 432, 461*, 46802, 54*",
    "payables": "401",
    "tax_liabilities": "44*",
    "payroll": "421*, 431*",
    "loans": "468, 46801",
    "leases": "",
    "ic_liabilities": "40102, 467, 46701, 467011, 46702",
    "other_liabilities": "4181",
    "share_capital": "101*",
    "equity": "1*, 8*",
}
PROPOSALS = {"SALT": SALT_MODEL, "TBM": SALT_MODEL, "ZOI GREECE": SALT_MODEL}
STATUSES = ("Proposed", "Reviewed", "Approved")

# KPI definitions: one formula per indicator for every screen. kind "period" = measured over a period (P&L),
# "point" = a position at a date (balance sheet).
KPIS = [
    {"key": "net_revenue", "label": "Net revenue", "kind": "period", "unit": "money", "needs": ["revenue"],
     "formula": "Net revenue = the mapped revenue accounts (sales, returns, royalties), credit shown positive"},
    {"key": "gross_profit", "label": "Gross profit", "kind": "period", "unit": "money", "needs": ["revenue", "cogs"],
     "formula": "Gross profit = Revenue total (net revenue + other operating income) − Cost of goods sold, as the "
                "management pack"},
    {"key": "gross_margin", "label": "Gross margin %", "kind": "period", "unit": "pct", "needs": ["revenue", "cogs"],
     "formula": "Gross margin % = Gross profit / Net revenue (service revenue) × 100"},
    {"key": "ebitda", "label": "EBITDA", "kind": "period", "unit": "money",
     "needs": ["revenue", "cogs", "labour", "opex"],
     "formula": "EBITDA = Net revenue + Other operating income − COGS − Labour − Occupancy − Other operating expenses "
                "(before interest and finance costs, income tax, depreciation and amortisation, non-operating items)"},
    {"key": "ebitda_margin", "label": "EBITDA margin %", "kind": "period", "unit": "pct",
     "needs": ["revenue", "cogs", "labour", "opex"], "formula": "EBITDA margin % = EBITDA / Net revenue × 100"},
    {"key": "ebit", "label": "EBIT", "kind": "period", "unit": "money", "needs": ["revenue", "da"],
     "formula": "EBIT = EBITDA − Depreciation and amortisation"},
    {"key": "net_profit", "label": "Net profit", "kind": "period", "unit": "money", "needs": [],
     "formula": "Net profit = − the sum of every income-statement account (it does not depend on the mapping)"},
    {"key": "net_margin", "label": "Net margin %", "kind": "period", "unit": "pct", "needs": ["revenue"],
     "formula": "Net margin % = Net profit / Net revenue × 100"},
    {"key": "cogs_pct", "label": "COGS %", "kind": "period", "unit": "pct", "needs": ["revenue", "cogs"],
     "formula": "COGS % = Cost of goods sold / Net revenue × 100"},
    {"key": "labour_pct", "label": "Labour %", "kind": "period", "unit": "pct", "needs": ["revenue", "labour"],
     "formula": "Labour % = Labour / Net revenue × 100"},
    {"key": "prime_cost_pct", "label": "Prime cost %", "kind": "period", "unit": "pct",
     "needs": ["revenue", "cogs", "labour"], "formula": "Prime cost % = (COGS + Labour) / Net revenue × 100"},
    {"key": "occupancy_pct", "label": "Occupancy cost %", "kind": "period", "unit": "pct",
     "needs": ["revenue", "occupancy"], "formula": "Occupancy cost % = Occupancy / Net revenue × 100"},
    {"key": "revenue_growth", "label": "Revenue growth %", "kind": "period", "unit": "pct", "needs": ["revenue"],
     "formula": "Revenue growth % = (Net revenue − the same period last year) / the same period last year × 100"},
    {"key": "cash_bank", "label": "Cash and bank", "kind": "point", "unit": "money", "needs": ["cash"],
     "formula": "Cash and bank = the mapped cash, bank and in-transit accounts at the date"},
    {"key": "owc", "label": "Operating working capital", "kind": "point", "unit": "money",
     "needs": ["inventory", "receivables", "payables"],
     "formula": "Operating working capital = Inventory + Trade receivables − Trade payables"},
    {"key": "net_debt", "label": "Net debt", "kind": "point", "unit": "money", "needs": ["loans", "cash"],
     "formula": "Net debt = Gross financial debt − Cash and bank"},
    {"key": "total_equity", "label": "Total equity", "kind": "point", "unit": "money", "needs": ["equity"],
     "formula": "Total equity = Share capital + the other equity accounts + the result not yet closed to equity "
                "(prior years) + the profit or loss of the current year"},
    {"key": "gross_debt", "label": "Gross financial debt", "kind": "point", "unit": "money", "needs": ["loans"],
     "formula": "Gross financial debt = Loans and borrowings + Lease liabilities + cash and bank accounts with a "
                "credit balance (overdrafts)"},
    {"key": "debt_equity", "label": "Debt / equity", "kind": "point", "unit": "ratio", "needs": ["loans", "equity"],
     "formula": "Debt / equity = Gross financial debt / Total equity"},
    {"key": "net_debt_ebitda", "label": "Net debt / EBITDA", "kind": "point", "unit": "ratio",
     "needs": ["loans", "cash", "revenue", "cogs", "labour", "opex"],
     "formula": "Net debt / EBITDA of the last 12 months; shown only once the mapping is approved"},
    {"key": "interest_cover", "label": "Interest coverage", "kind": "period", "unit": "ratio",
     "needs": ["revenue", "cogs", "labour", "opex", "finance"],
     "formula": "Interest coverage = EBITDA of the last 12 months / interest expense of the last 12 months; shown "
                "only once the mapping is approved"},
    {"key": "current_ratio", "label": "Current ratio", "kind": "point", "unit": "ratio", "needs": ["current_split"],
     "formula": "Current ratio = Current assets / Current liabilities; needs the split of loans, deposits and loans "
                "given by maturity (within or after 12 months)"},
    {"key": "quick_ratio", "label": "Quick ratio", "kind": "point", "unit": "ratio", "needs": ["current_split"],
     "formula": "Quick ratio = (Current assets − Inventory) / Current liabilities; same condition as the current ratio"},
]
KPI = {k["key"]: k for k in KPIS}


# ---------------------------------------------------------------- the mapping store
def _norm(company):
    return str(company or "").strip().upper()


def _read():
    try:
        return json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write(d):
    FILE.parent.mkdir(exist_ok=True)
    tmp = FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(FILE)


def mapping(company):
    """{"company", "status", "rules": {line: "accounts"}, "proposal": bool, "approved_by", "approved_at",
    "updated_by", "updated_at", "history"}; None when the company has neither a saved mapping nor a proposal."""
    key = _norm(company)
    with _lock:
        d = _read()
        saved = d.get(key)
        if saved and key in PROPOSALS and not any(str((saved.get("rules") or {}).get(c) or "").strip() for c in CLUSTER):
            # saved before the P&L had the lines of the management pack: its P&L rules give way to the pack's
            # (P&L GL Category), the balance sheet stays as saved; reviewed again, approved again
            saved = _to_pack(saved)
            d[key] = saved
            _write(d)
    if saved:
        rules = {k: str(saved.get("rules", {}).get(k, "")) for k in RULE_KEYS}
        return dict(saved, company=company, rules=rules, groups=groups_of(saved), proposal=False)
    model = PROPOSALS.get(key)
    if not model:
        return None
    return {"company": company, "status": "Proposed", "rules": {k: model.get(k, "") for k in RULE_KEYS},
            "groups": groups_of(None), "proposal": True,
            "approved_by": "", "approved_at": "", "updated_by": "", "updated_at": "", "history": []}


RULE_KEYS = [k for k, *_ in LINES] + [k for k, *_ in CLUSTERS]


def groups_of(m):
    """{cluster: KPI line} of a mapping: the pack's, with what Finance changed."""
    g = (m or {}).get("groups") or {}
    return {k: g.get(k) if g.get(k) in PL_LINES else CLUSTER[k]["group"] for k in CLUSTER}


def _to_pack(saved):
    now = datetime.now().isoformat(timespec="seconds")
    rules = dict(saved.get("rules") or {})
    rules.update({k: "" for k in PL_LINES})
    rules.update(PACK_PL)
    hist = list(saved.get("history") or []) + [{
        "ts": now, "by": "pulse", "status": "Reviewed", "changed": sorted(PACK_PL) + sorted(PL_LINES),
        "note": "P&L to the lines of the management pack (Chart of Account MNG Pack, P&L GL Category, P&L Cluster 2);"
                f" was {saved.get('status') or 'Proposed'}. Approve it again."}]
    return dict(saved, rules=rules, status="Reviewed", approved_by="", approved_at="", updated_by="pulse",
                updated_at=now, history=hist[-50:])


def save(company, rules, actor, action="save", note="", groups=None):
    """action: save (status Reviewed), approve (Approved, by name and date), reset (back to the proposal)."""
    key = _norm(company)
    now = datetime.now().isoformat(timespec="seconds")
    with _lock:
        d = _read()
        cur = d.get(key) or {}
        hist = list(cur.get("history") or [])
        if action == "reset":
            d.pop(key, None)
            _write(d)
            store.audit("kpi mapping", company, actor, {"status": cur.get("status")}, {"status": "Proposed"}, note)
            return mapping(company)
        base = (cur.get("rules") if cur else None) or PROPOSALS.get(key) or {}
        new = {k: _clean(rules.get(k, base.get(k, ""))) if rules is not None else _clean(base.get(k, ""))
               for k in RULE_KEYS}
        changed = {k: [base.get(k, ""), v] for k, v in new.items() if _clean(base.get(k, "")) != v}
        old_g = groups_of(cur)
        new_g = {k: (groups or {}).get(k) if (groups or {}).get(k) in PL_LINES else old_g[k] for k in CLUSTER}
        changed.update({f"{k} (group)": [old_g[k], v] for k, v in new_g.items() if old_g[k] != v})
        status = "Approved" if action == "approve" else "Reviewed"
        hist.append({"ts": now, "by": actor, "status": status, "changed": sorted(changed), "note": note[:300]})
        d[key] = {"status": status, "rules": new, "groups": new_g, "updated_by": actor, "updated_at": now,
                  "approved_by": actor if action == "approve" else "", "approved_at": now if action == "approve" else "",
                  "history": hist[-50:]}
        _write(d)
    store.audit("kpi mapping", company, actor, {"status": cur.get("status") or "Proposed"},
                {"status": status, "changed": sorted(changed)}, note)
    return mapping(company)


def _clean(text):
    parts = [p.strip() for p in re.split(r"[,;\s]+", str(text or "")) if p.strip()]
    return ", ".join(p for p in dict.fromkeys(parts) if re.fullmatch(r"[0-9A-Za-z.\-]+\*?", p))


# ---------------------------------------------------------------- classification
def classifier(m):
    """account number → KPI line key (or None), the most specific rule first; an account in a line of the
    management pack is in that line's group."""
    of = cluster_of(m)
    g = groups_of(m)

    def line_of(no):
        k = of(no)
        return g[k] if k in CLUSTER else k
    return line_of


def cluster_of(m):
    """account number → the rule it falls in: a line of the management pack, a KPI line, or None."""
    exact, prefixes = {}, []
    for line, text in ((m or {}).get("rules") or {}).items():
        for p in re.split(r"[,;\s]+", text or ""):
            p = p.strip()
            if not p:
                continue
            if p.endswith("*"):
                prefixes.append((p[:-1], line))
            else:
                exact[p] = line
    prefixes.sort(key=lambda x: -len(x[0]))

    def line_of(no):
        no = str(no or "").strip()
        if no in exact:
            return exact[no]
        for p, line in prefixes:
            if no.startswith(p):
                return line
        return None
    return line_of


def signed(line, net):
    """Management sign of a ledger net change or balance on a line."""
    sg = LINE[line]["sign"]
    return -net if sg in ("income", "credit") else net


def _f(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def pl_lines(rows, m, field="Net_Change"):
    """The P&L lines of chart-of-accounts rows: {"lines": {line: value}, "accounts": {line: [(no, name, value)]},
    "unmapped": [(no, name, net)], "net_profit", "check"}. Net profit is − Σ every income-statement account, so a
    mapping gap shows as a check difference, never as a wrong profit."""
    rule_of, groups = cluster_of(m), groups_of(m)
    vals, comps, unmapped = {k: 0.0 for k in PL_LINES}, {k: [] for k in PL_LINES}, []
    cvals, ccomps = {k: 0.0 for k in CLUSTER}, {k: [] for k in CLUSTER}
    total = 0.0
    for r in rows or []:
        if r.get("Account_Type") not in (None, "Posting") or r.get("Income_Balance") not in (None, "Income Statement"):
            continue
        no, net = str(r.get("No") or ""), _f(r.get(field))
        if r.get("Income_Balance") is None and not no[:1] in ("6", "7"):
            continue
        total += net
        rule = rule_of(no)
        line = groups[rule] if rule in CLUSTER else rule
        if line not in PL_LINES:
            if abs(net) > 0.005 or line is None:
                unmapped.append((no, r.get("Name") or "", round(net, 2), line))
            continue
        v = signed(line, net)
        vals[line] += v
        if rule in CLUSTER:
            cvals[rule] += v
            if net:
                ccomps[rule].append((no, r.get("Name") or "", round(v, 2)))
        if net:
            comps[line].append((no, r.get("Name") or "", round(v, 2)))
    for k in comps:
        comps[k].sort(key=lambda x: -abs(x[2]))
    for k in ccomps:
        ccomps[k].sort(key=lambda x: -abs(x[2]))
    net_profit = -total
    derived = derive(vals)
    return {"lines": {k: round(v, 2) for k, v in vals.items()}, "accounts": comps,
            "clusters": {k: round(v, 2) for k, v in cvals.items()}, "cluster_accounts": ccomps,
            "pack": has_pack(m),
            "unmapped": [u for u in unmapped if abs(u[2]) > 0.005 or u[3] is None],
            "net_profit": round(net_profit, 2), "derived": derived,
            "check": round(derived["net_profit_from_lines"] - net_profit, 2)}


def has_pack(m):
    """True when the P&L of the mapping is by the lines of the management pack."""
    return any(str(((m or {}).get("rules") or {}).get(k) or "").strip() for k in CLUSTER)


def derive(v):
    """The subtotals of the P&L from its lines (management sign). Gross margin as the management pack: revenue
    total (service revenue and the other income) less the cost of goods sold."""
    gross = v["revenue"] + v["other_income"] - v["cogs"]
    ebitda = v["revenue"] + v["other_income"] - v["cogs"] - v["labour"] - v["occupancy"] - v["opex"]
    ebit = ebitda - v["da"]
    pbt = ebit - v["finance"] - v["nonop"]
    return {"revenue_total": v["revenue"] + v["other_income"], "opex_total": v["labour"] + v["occupancy"] + v["opex"],
            "gross_profit": gross, "ebitda": ebitda, "ebit": ebit, "profit_before_tax": pbt,
            "net_profit_from_lines": pbt - v["tax"]}


def bs_lines(rows, m, field="Balance_at_Date"):
    """The balance-sheet lines at a date; the result still sitting in the income-statement accounts is carried
    into equity, because RHG has never closed them."""
    line_of = classifier(m)
    vals, comps, unmapped = {k: 0.0 for k in BS_LINES}, {k: [] for k in BS_LINES}, []
    result = 0.0
    for r in rows or []:
        if r.get("Account_Type") not in (None, "Posting"):
            continue
        no, bal = str(r.get("No") or ""), _f(r.get(field))
        if r.get("Income_Balance") == "Income Statement":
            result -= bal
            continue
        line = line_of(no)
        if line not in BS_LINES:
            if abs(bal) > 0.005:
                unmapped.append((no, r.get("Name") or "", round(bal, 2), line))
            continue
        v = signed(line, bal)
        vals[line] += v
        if bal:
            comps[line].append((no, r.get("Name") or "", round(v, 2)))
    vals["equity"] += result
    if abs(result) > 0.005:
        comps["equity"].append(("—", "Result not yet closed to equity", round(result, 2)))
    for k in comps:
        comps[k].sort(key=lambda x: -abs(x[2]))
    assets = sum(vals[k] for k in BS_LINES if LINE[k]["sign"] == "asset")
    liab = sum(vals[k] for k in BS_LINES if LINE[k]["sign"] == "credit" and k not in EQUITY_LINES)
    equity = sum(vals[k] for k in EQUITY_LINES)
    return {"lines": {k: round(v, 2) for k, v in vals.items()}, "accounts": comps, "unmapped": unmapped,
            "totals": {"assets": round(assets, 2), "liabilities": round(liab, 2), "equity": round(equity, 2),
                       "difference": round(assets - liab - equity, 2)},
            "result_in_equity": round(result, 2)}


# ---------------------------------------------------------------- KPI values with their status
def filled(m, line):
    """A KPI line has accounts: its own rule, or a line of the management pack in it."""
    rules = (m or {}).get("rules") or {}
    if str(rules.get(line) or "").strip():
        return True
    g = groups_of(m)
    return any(g[c] == line and str(rules.get(c) or "").strip() for c in CLUSTER)


def kpi_status(m, needs):
    if m is None:
        return "Content required"
    empty = [n for n in needs if n in LINE and not filled(m, n) or n not in LINE and
             not str((m.get("rules") or {}).get(n) or "").strip()]
    if empty:
        return "Content required"
    return "Approved" if m.get("status") == "Approved" else "Pilot"


def pl_kpis(cur, m, ly=None):
    """cur, ly: pl_lines results. {key: {"value", "status", "formula", "missing"}}."""
    v, d = cur["lines"], cur["derived"]
    rev = v["revenue"]

    def pct(a):
        return a / rev * 100 if rev and rev > 0 else None
    values = {
        "net_revenue": rev, "gross_profit": d["gross_profit"], "gross_margin": pct(d["gross_profit"]),
        "ebitda": d["ebitda"], "ebitda_margin": pct(d["ebitda"]), "ebit": d["ebit"],
        "net_profit": cur["net_profit"], "net_margin": pct(cur["net_profit"]),
        "cogs_pct": pct(v["cogs"]), "labour_pct": pct(v["labour"]), "prime_cost_pct": pct(v["cogs"] + v["labour"]),
        "occupancy_pct": pct(v["occupancy"]),
        "revenue_growth": ((rev - ly["lines"]["revenue"]) / abs(ly["lines"]["revenue"]) * 100)
        if ly and ly["lines"]["revenue"] else None}
    out = {}
    for k, val in values.items():
        D = KPI[k]
        st = kpi_status(m, D["needs"])
        out[k] = {"value": None if st == "Content required" and k != "net_profit" else
                  (round(val, 2) if val is not None else None),
                  "status": "Pilot" if k == "net_profit" and st == "Content required" else st,
                  "label": D["label"], "unit": D["unit"], "formula": D["formula"],
                  "missing": [n for n in D["needs"] if not (filled(m, n) if n in LINE else
                                                            str(((m or {}).get("rules") or {}).get(n) or "").strip())]}
    return out


def definitions():
    return {"lines": [LINE[k] for k, *_ in LINES], "clusters": [CLUSTER[k] for k, *_ in CLUSTERS], "kpis": KPIS,
            "statuses": STATUSES}


# ---------------------------------------------------------------- the Executive dashboard buckets
EXEC_BUCKET = {"revenue": "sales", "other_income": "other_income", "cogs": "food", "labour": "labor",
               "occupancy": "opex", "opex": "opex", "da": "da", "finance": "below", "nonop": "below", "tax": "below"}


def exec_bucket(line, name):
    """The Executive dashboard bucket of a P&L line (its EBITDA = sales + other income − food − labour − opex);
    labour is split by the words of the account name."""
    b = EXEC_BUCKET.get(line)
    if b == "labor":
        n = (name or "").lower()
        for word, bucket in (("kitchen", "labor_kitchen"), ("service", "labor_service"),
                             ("management", "labor_management")):
            if word in n:
                return bucket
        return "labor_other"
    return b or "unmapped"
