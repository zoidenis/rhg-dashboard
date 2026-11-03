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
    ("equity", "Equity", "bs", "credit"),
]
LINE = {k: {"key": k, "label": lb, "statement": st, "sign": sg} for k, lb, st, sg in LINES}
PL_LINES = [k for k, _, st, _ in LINES if st == "pl"]
BS_LINES = [k for k, _, st, _ in LINES if st == "bs"]

# The chart model of SALT (TBM and Zoi Greece were opened from it). A proposal from the chart as read on
# 3 November 2026, to be reviewed by Finance; RKS has its own chart and no proposal.
SALT_MODEL = {
    "revenue": "704*, 705*, 706*, 707*, 76",
    "other_income": "7072, 708*, 75*, 79*",
    "cogs": "601*, 602*, 6034, 605*, 6271, 6581, 761",
    "labour": "641*, 644*, 648*",
    "occupancy": "636, 6361, 604, 60402, 60403, 60125, 615",
    "opex": "60124, 601241, 60401, 61*, 62*, 63*, 649, 65*, 67*, 68188",
    "da": "681*",
    "finance": "66*, 727*, 762*, 763*, 764*, 765*, 766*, 767*, 768*, 769*",
    "nonop": "77*, 657, 687",
    "tax": "69*",
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
     "formula": "Gross profit = Net revenue − Cost of goods sold"},
    {"key": "gross_margin", "label": "Gross margin %", "kind": "period", "unit": "pct", "needs": ["revenue", "cogs"],
     "formula": "Gross margin % = Gross profit / Net revenue × 100"},
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
    {"key": "net_debt", "label": "Net debt", "kind": "point", "unit": "money", "needs": ["loans", "leases", "cash"],
     "formula": "Net debt = Loans and borrowings + Lease liabilities − Cash and bank"},
    {"key": "total_equity", "label": "Total equity", "kind": "point", "unit": "money", "needs": ["equity"],
     "formula": "Total equity = the mapped equity accounts + the result not yet closed to equity"},
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
    with _lock:
        saved = _read().get(_norm(company))
    if saved:
        rules = {k: str(saved.get("rules", {}).get(k, "")) for k in LINE}
        return dict(saved, company=company, rules=rules, proposal=False)
    model = PROPOSALS.get(_norm(company))
    if not model:
        return None
    return {"company": company, "status": "Proposed", "rules": dict(model), "proposal": True,
            "approved_by": "", "approved_at": "", "updated_by": "", "updated_at": "", "history": []}


def save(company, rules, actor, action="save", note=""):
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
               for k in LINE}
        changed = {k: [base.get(k, ""), v] for k, v in new.items() if _clean(base.get(k, "")) != v}
        status = "Approved" if action == "approve" else "Reviewed"
        hist.append({"ts": now, "by": actor, "status": status, "changed": sorted(changed), "note": note[:300]})
        d[key] = {"status": status, "rules": new, "updated_by": actor, "updated_at": now,
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
    """account number → line key (or None), the most specific rule first."""
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
    line_of = classifier(m)
    vals, comps, unmapped = {k: 0.0 for k in PL_LINES}, {k: [] for k in PL_LINES}, []
    total = 0.0
    for r in rows or []:
        if r.get("Account_Type") not in (None, "Posting") or r.get("Income_Balance") not in (None, "Income Statement"):
            continue
        no, net = str(r.get("No") or ""), _f(r.get(field))
        if r.get("Income_Balance") is None and not no[:1] in ("6", "7"):
            continue
        total += net
        line = line_of(no)
        if line not in PL_LINES:
            if abs(net) > 0.005 or line is None:
                unmapped.append((no, r.get("Name") or "", round(net, 2), line))
            continue
        v = signed(line, net)
        vals[line] += v
        if net:
            comps[line].append((no, r.get("Name") or "", round(v, 2)))
    for k in comps:
        comps[k].sort(key=lambda x: -abs(x[2]))
    net_profit = -total
    derived = derive(vals)
    return {"lines": {k: round(v, 2) for k, v in vals.items()}, "accounts": comps,
            "unmapped": [u for u in unmapped if abs(u[2]) > 0.005 or u[3] is None],
            "net_profit": round(net_profit, 2), "derived": derived,
            "check": round(derived["net_profit_from_lines"] - net_profit, 2)}


def derive(v):
    """The subtotals of the P&L from its lines (management sign)."""
    gross = v["revenue"] - v["cogs"]
    ebitda = v["revenue"] + v["other_income"] - v["cogs"] - v["labour"] - v["occupancy"] - v["opex"]
    ebit = ebitda - v["da"]
    pbt = ebit - v["finance"] - v["nonop"]
    return {"gross_profit": gross, "ebitda": ebitda, "ebit": ebit, "profit_before_tax": pbt,
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
    liab = sum(vals[k] for k in BS_LINES if LINE[k]["sign"] == "credit" and k != "equity")
    return {"lines": {k: round(v, 2) for k, v in vals.items()}, "accounts": comps, "unmapped": unmapped,
            "totals": {"assets": round(assets, 2), "liabilities": round(liab, 2), "equity": round(vals["equity"], 2),
                       "difference": round(assets - liab - vals["equity"], 2)},
            "result_in_equity": round(result, 2)}


# ---------------------------------------------------------------- KPI values with their status
def kpi_status(m, needs):
    if m is None:
        return "Content required"
    empty = [n for n in needs if not str((m.get("rules") or {}).get(n) or "").strip()]
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
                  "missing": [n for n in D["needs"] if not str(((m or {}).get("rules") or {}).get(n) or "").strip()]}
    return out


def definitions():
    return {"lines": [LINE[k] for k, *_ in LINES], "kpis": KPIS, "statuses": STATUSES}


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
