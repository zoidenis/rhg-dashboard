"""Purchase type classification.

Business Central does not carry a "purchase type" field, so the type is derived
from data BC does publish and from a mapping the user controls in Settings:

  item purchases      -> the item's category code (Item Category Code on the item card)
  non-item purchases  -> the G/L account the cost was posted to, and the account's
                         own category in the chart of accounts (Assets -> CAPEX)

Anything the mapping does not cover is left Unclassified. Nothing is guessed into
a category, because a wrong class here would quietly move spend between streams.
"""
from collections import defaultdict

TYPES = ["Food & Beverage", "Non-Food Inventory", "Operating Supplies", "Services",
         "Recurring Contracts", "CAPEX", "Prepayments and advances", "Unclassified"]

# Accounts RHG posts capital expenditure to. Only these become CAPEX: an asset account
# is not evidence of capital expenditure by itself, and prepayments, related-party
# balances and inventory all sit on asset accounts too.
DEFAULT_CAPEX_ACCOUNTS = ["203", "208", "213", "2131", "2134", "2135", "215",
                          "2181", "2182", "21822", "2188"]
# Expense accounts (class 6) are the running cost of services and supplies.
DEFAULT_SERVICE_PREFIX = "6"
# Goods accounts: the accounting side of the same purchases that arrive as items through
# the value entries. Counting them again as services would double the spend, so they are
# excluded from every stream and reported separately as a reconciliation figure.
DEFAULT_GOODS_PREFIXES = "60,605,6051,65801,65802,65803"
# Prepayments and advances (class 4 asset accounts) are money paid before the cost
# belongs to the period. They are shown separately, never added to spend.
DEFAULT_PREPAYMENT_PREFIX = "4"


def accounts_to_read(accounts, rules):
    """Which G/L accounts a purchase can legitimately land on, so the ledger is read
    account by account instead of swept by date. Payables, VAT, income and equity are
    left out: they are the other side of an invoice, never the spend itself."""
    capex = {str(a).strip() for a in rules.get("capex_accounts", [])}
    service_prefix = tuple(p.strip() for p in (rules.get("service_prefix") or DEFAULT_SERVICE_PREFIX).split(",") if p.strip())
    prepay_prefix = tuple(p.strip() for p in (rules.get("prepayment_prefix") or DEFAULT_PREPAYMENT_PREFIX).split(",") if p.strip())
    goods = tuple(p.strip() for p in (rules.get("goods_prefixes") or DEFAULT_GOODS_PREFIXES).split(",") if p.strip())
    wanted = set(capex) | set(rules.get("service_map", {}))
    for no, acc in (accounts or {}).items():
        no = str(no).strip()
        category = (acc.get("Account_Category") or "").strip()
        if (acc.get("Account_Type") or "Posting") != "Posting":
            continue
        if no in capex:
            wanted.add(no)
            continue
        if category in ("Liabilities", "Equity", "Income"):
            continue
        if no.startswith(goods):
            wanted.add(no)          # read them, then report them as the goods mirror
            continue
        if no.startswith(service_prefix) or category == "Expense" or no.startswith(prepay_prefix):
            wanted.add(no)
    return sorted(wanted)

# The item card carries five category groups. Group 3 describes what the goods are
# ("USHQIME", "PIJE", "JO USHQIMORE"), which is exactly the question a purchase type
# asks, so it is tried first. Group 2 describes how the item is handled ("LP", "BAR",
# "II") and catches the items where group 3 is blank. The old Item Category Code is the
# last fallback. Group 4 is kept as the procurement sub-category, not as the type.
DEFAULT_GROUP3_MAP = {
    "USHQIME": "Food & Beverage",
    "PIJE": "Food & Beverage",
    "ALCOHOL": "Food & Beverage",
    "WINE": "Food & Beverage",
    "RESTORANT": "Food & Beverage",
    "AMBALAZHIME": "Non-Food Inventory",
    "JO USHQIMORE": "Non-Food Inventory",
    "PASTRIMI & KIMIKATE": "Operating Supplies",
    "TE KONSUMUESHME": "Operating Supplies",
}
DEFAULT_GROUP2_MAP = {
    "LP": "Food & Beverage",
    "LP E SHITSHME": "Food & Beverage",
    "BAR": "Food & Beverage",
    "RESTORANT": "Food & Beverage",
    "I SHITSHEM": "Food & Beverage",
    "PN": "Food & Beverage",
    "II": "Non-Food Inventory",
    "AMBALAZH": "Non-Food Inventory",
    "UNIFORMA": "Non-Food Inventory",
    "MIREMBAJTJE": "Non-Food Inventory",
    "PRODUKT PASTRIMI": "Operating Supplies",
    "LENDE DJEGESE": "Operating Supplies",
}
DEFAULT_ITEM_MAP = {
    "LP": "Food & Beverage",
    "BAR": "Food & Beverage",
    "RESTORANT": "Food & Beverage",
    "BREAKFAST": "Food & Beverage",
    "MINIBAR": "Food & Beverage",
    "PRODUKT I NDERMJETEM": "Food & Beverage",
    "LD": "Food & Beverage",
    "AMBALAZH": "Non-Food Inventory",
    "INVENTAR I IMET": "Non-Food Inventory",
    "UNIFORMA": "Non-Food Inventory",
    "PASTRIM": "Operating Supplies",
    "MJETE KANCELARIE": "Operating Supplies",
    "CHECK IN": "Operating Supplies",
}


def _f(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def parse_maps(settings_block):
    """The three mapping levels, each falling back to the defaults when left empty."""
    return {"g3": parse_map(settings_block.get("group3"), DEFAULT_GROUP3_MAP),
            "g2": parse_map(settings_block.get("group2"), DEFAULT_GROUP2_MAP),
            "code": parse_map(settings_block.get("item_categories"), DEFAULT_ITEM_MAP)}


def parse_map(text, fallback=None):
    """'LP=Food & Beverage, BAR=Food & Beverage' -> dict. Empty text keeps the fallback."""
    if not (text or "").strip():
        return dict(fallback or {})
    out = {}
    for part in text.split(","):
        if "=" in part:
            k, _, v = part.partition("=")
            key, value = k.strip().upper(), v.strip()
            if value in TYPES:
                out[key] = value
    return out or dict(fallback or {})


def classify_item(card, maps):
    """Purchase type for one item card, with the field that decided it.

    The card is tried in order: category group 3 (what the goods are), then group 2
    (how they are handled), then the item category code. The first field that has a
    value and a mapping wins, and the field is reported so the classification can be
    checked rather than trusted.
    """
    chain = [("Item Category Group 3", (card.get("g3") or "").strip(), maps["g3"]),
             ("Item Category Group 2", (card.get("g2") or "").strip(), maps["g2"]),
             ("Item Category Code", (card.get("code") or "").strip(), maps["code"])]
    for field, value, mapping in chain:
        if value and mapping.get(value.upper()):
            return mapping[value.upper()], field, value
    first = next((f"{f}: {v}" for f, v, _ in chain if v), "")
    return "Unclassified", ("no category on the card" if not first else "no mapping"), first


def item_types(ve_rows, catalog, maps):
    """Item purchases grouped by purchase type, using the item card's category groups."""
    streams = defaultdict(lambda: {"value": 0.0, "lines": 0, "items": set(), "documents": set(),
                                   "categories": defaultdict(float), "suppliers": defaultdict(float)})
    unclassified_items = defaultdict(lambda: {"value": 0.0, "desc": "", "category": "", "reason": ""})
    by_field = defaultdict(float)
    rows = []
    for r in ve_rows:
        item = str(r.get("Item_No") or "")
        card = catalog.get(item) or {}
        ptype, field, value = classify_item(card, maps)
        sub = (card.get("g4") or card.get("g3") or card.get("code") or "").strip()
        cost = _f(r.get("Cost_Amount_Actual")) + _f(r.get("Cost_Amount_Expected"))
        s = streams[ptype]
        s["value"] += cost
        s["lines"] += 1
        s["items"].add(item)
        s["documents"].add(r.get("Document_No"))
        s["categories"][sub or "(no sub-category)"] += cost
        by_field[field] += cost
        if ptype == "Unclassified" and item:
            u = unclassified_items[item]
            u["value"] += cost
            u["desc"] = (r.get("Item_Description") or "").strip() or u["desc"]
            u["category"] = value
            u["reason"] = field
        rows.append({"item": item, "description": (r.get("Item_Description") or "").strip(),
                     "category": sub, "group2": card.get("g2", ""), "group3": card.get("g3", ""),
                     "group4": card.get("g4", ""), "code": card.get("code", ""),
                     "classified_by": field, "type": ptype, "document": r.get("Document_No"),
                     "posting": r.get("Posting_Date"), "location": r.get("Location_Code") or "",
                     "quantity": _f(r.get("Item_Ledger_Entry_Quantity")), "value": cost})
    return streams, rows, unclassified_items, dict(by_field)


def gl_types(gl_rows, accounts, suppliers_by_doc, rules):
    """Non-item purchase postings grouped by purchase type.

    Rules, in order:
      1. an account on the CAPEX list                -> CAPEX
      2. an account mapped by hand in Settings        -> that type
      3. a liability, equity or income account        -> skipped (payables, VAT, refunds)
      4. an inventory account                         -> skipped (already in the item stream)
      5. an expense account (class 6 by default)      -> Services
      6. an asset account in class 4 by default       -> Prepayments and advances
      7. anything else                                -> Unclassified, and listed as such
    Only debit lines count: the credit side of a purchase invoice is the supplier balance.
    """
    capex = {str(a).strip() for a in rules.get("capex_accounts", [])}
    goods_prefixes = tuple(p.strip() for p in (rules.get("goods_prefixes") or DEFAULT_GOODS_PREFIXES).split(",")
                           if p.strip())
    service_map = rules.get("service_map", {})
    service_prefix = rules.get("service_prefix") or DEFAULT_SERVICE_PREFIX
    prepay_prefix = rules.get("prepayment_prefix") or DEFAULT_PREPAYMENT_PREFIX
    streams = defaultdict(lambda: {"value": 0.0, "lines": 0, "documents": set(),
                                   "accounts": defaultdict(float), "suppliers": defaultdict(float)})
    rows = []
    skipped = {"payable_or_vat": 0, "credit_lines": 0, "inventory": 0, "unknown_account": 0, "goods": 0}
    unknown_accounts = defaultdict(float)
    goods_accounts = defaultdict(float)
    for r in gl_rows:
        acc_no = str(r.get("G_L_Account_No") or "").strip()
        acc = accounts.get(acc_no) or {}
        amount = _f(r.get("Amount"))
        category = acc.get("Account_Category") or ""
        subcat = (acc.get("Account_Subcategory_Descript") or "").lower()
        name = acc.get("Name") or r.get("G_L_Account_Name") or acc_no
        if amount <= 0:
            skipped["credit_lines"] += 1
            continue
        if acc_no in capex:
            ptype = "CAPEX"
        elif acc_no.startswith(goods_prefixes) and acc_no not in service_map:
            # the goods side of an item purchase: already counted through the value entries
            skipped["goods"] += 1
            goods_accounts[f"{acc_no} {name}"] += amount
            continue
        elif acc_no in service_map:
            ptype = service_map[acc_no]
        elif category in ("Liabilities", "Equity", "Income"):
            skipped["payable_or_vat"] += 1
            continue
        elif any(h in f"{name} {subcat}".lower() for h in ("inventar", "magazin", "stok", "inventory")):
            skipped["inventory"] += 1
            continue
        elif acc_no.startswith(service_prefix) or category == "Expense":
            ptype = "Services"
        elif acc_no.startswith(prepay_prefix):
            ptype = "Prepayments and advances"
        else:
            ptype = "Unclassified"
            unknown_accounts[f"{acc_no} {name}"] += amount
        supplier = suppliers_by_doc.get(str(r.get("Document_No")), {})
        s = streams[ptype]
        s["value"] += amount
        s["lines"] += 1
        s["documents"].add(r.get("Document_No"))
        s["accounts"][f"{acc_no} {name}"] += amount
        s["suppliers"][supplier.get("vendor") or "(supplier not matched)"] += amount
        rows.append({"document": r.get("Document_No"), "posting": r.get("Posting_Date"),
                     "document_date": r.get("Document_Date"), "account": acc_no, "account_name": name,
                     "supplier": supplier.get("vendor") or "", "vendor_no": supplier.get("vendor_no") or "",
                     "type": ptype, "value": amount, "source": r.get("Source_Code") or "",
                     "category": category or "(account not found in the chart of accounts)"})
    skipped["unknown_account"] = len(unknown_accounts)
    goods = {"total": sum(goods_accounts.values()),
             "accounts": sorted(({"account": k, "value": v} for k, v in goods_accounts.items()),
                                key=lambda x: -x["value"])[:30]}
    return (streams, rows, skipped,
            sorted(({"account": k, "value": v} for k, v in unknown_accounts.items()),
                   key=lambda x: -x["value"])[:40],
            goods)


def summarize(item_streams, gl_streams):
    out = {}
    for name, s in list(item_streams.items()) + list(gl_streams.items()):
        cur = out.setdefault(name, {"type": name, "value": 0.0, "lines": 0, "documents": 0,
                                    "items": 0, "top": []})
        cur["value"] += s["value"]
        cur["lines"] += s["lines"]
        cur["documents"] += len(s["documents"])
        cur["items"] += len(s.get("items", []))
        detail = s.get("categories") or s.get("accounts") or {}
        merged = defaultdict(float, {d["label"]: d["value"] for d in cur["top"]})
        for k, v in detail.items():
            merged[k] += v
        cur["top"] = sorted(({"label": k, "value": v} for k, v in merged.items()),
                            key=lambda x: -x["value"])[:12]
    total = sum(s["value"] for s in out.values()) or 1
    for s in out.values():
        s["share"] = s["value"] / total * 100
    return sorted(out.values(), key=lambda s: -s["value"])


def service_exceptions(gl_rows, settings, suppliers_by_doc):
    """What can honestly be checked on non-item spend with the fields BC publishes."""
    th = settings["thresholds"]
    out = []
    by_doc = defaultdict(lambda: {"value": 0.0, "supplier": "", "accounts": set(), "posting": "",
                                  "document_date": ""})
    for r in gl_rows:
        d = by_doc[r["document"]]
        d["value"] += r["value"]
        d["supplier"] = r["supplier"] or d["supplier"]
        d["accounts"].add(r["account"])
        d["posting"] = r["posting"]
        d["document_date"] = r["document_date"]
    # same supplier, same amount, same document date: a possible duplicate service invoice
    seen = defaultdict(list)
    for doc, d in by_doc.items():
        if d["supplier"]:
            seen[(d["supplier"], round(d["value"], 2), (d["document_date"] or "")[:10])].append(doc)
    for (supplier, amount, doc_date), docs in seen.items():
        if len(docs) > 1 and amount >= th["exception_spend"] / 10:
            out.append({"code": f"svc-dup-{docs[0]}", "severity": "warning",
                        "rule": "Possible duplicate service invoice",
                        "title": f"{supplier}: {len(docs)} invoices of {amount:,.0f} on {doc_date}",
                        "detail": f"Documents {', '.join(str(x) for x in docs)}. Same supplier, same date, "
                                  f"same amount. It may be legitimate, it is worth checking.",
                        "impact": amount * (len(docs) - 1), "owner": settings["owners"].get("invoices", ""),
                        "document": ", ".join(str(x) for x in docs), "document_type": "Service invoice",
                        "supplier": supplier, "location": "",
                        "drill": {"kind": "services", "key": ""},
                        "next_action": "Compare the documents in BC and reverse one if it is a duplicate."})
    for doc, d in by_doc.items():
        if not d["supplier"] and d["value"] >= th["exception_spend"]:
            out.append({"code": f"svc-novendor-{doc}", "severity": "warning",
                        "rule": "Service cost with no supplier matched",
                        "title": f"Document {doc}: {d['value']:,.0f} posted with no supplier match",
                        "detail": "The vendor ledger has no entry with this document number, so the "
                                  "supplier behind the cost cannot be identified from the published data.",
                        "impact": d["value"], "owner": settings["owners"].get("invoices", ""),
                        "document": str(doc), "document_type": "Service invoice", "supplier": "",
                        "location": "", "drill": {"kind": "services", "key": ""},
                        "next_action": "Check the document in BC; it may be a journal rather than an invoice."})
    return out
