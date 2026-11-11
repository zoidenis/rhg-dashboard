"""Transfers in transit: goods shipped from one location and not yet received at the other.

When a transfer order is shipped, Business Central moves the quantity out of the sending location into an
in-transit location (Location card: Use As In-Transit); receiving it moves it from there into the receiving
location. So what is in transit right now is proved by the item ledger: the entries at an in-transit
location that are still open, with quantity remaining. Each one carries the posted shipment number, the
item, the quantity and its cost; the share not yet received is Remaining Quantity over Quantity, at the
same cost per unit.

The sending location is the other side of the same shipment (the entry taking the goods out, same
document number). The receiving location sits on the transfer order, and the published services do not
give the shipment its order number: the order is found by its lines, the open transfer order from the
same location whose shipped lines carry the shipment's items. A shipment no order matches, or that two
orders match equally, is shown with its destination unknown, never guessed.

Business Central is only read.
"""
from collections import defaultdict
from datetime import date

TOL = 0.0005
AGES = (("0–1 days", 0, 1), ("2–3 days", 2, 3), ("4–7 days", 4, 7), ("8–30 days", 8, 30), ("Over 30 days", 31, None))


def _f(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _day(v):
    s = str(v or "")[:10]
    return "" if not s or s.startswith("0001") else s


def is_transit(code, cards=None):
    """An in-transit location: flagged on its card, else (card not read) a code naming it."""
    code = str(code or "").strip().upper()
    if cards is not None and code in cards:
        return bool(cards[code].get("transit"))
    return "TRANSIT" in code or "TRANZIT" in code


def link_orders(shipments, headers, lines):
    """{shipment: (order, how)} — the open transfer order behind each posted shipment, by its lines.

    shipments: {doc: {"from": code or "", "items": {item: qty}, "date": "YYYY-MM-DD"}}
    A candidate order has shipped lines (Quantity Shipped above zero) and, when both are known, the same
    sending location. The score is the share of the shipment's items found shipped on the order; ties are
    broken by the order's date being the shipment's date, then by the quantities agreeing. Below half the
    items, or a tie that stays, links nothing."""
    shipped = defaultdict(lambda: defaultdict(float))
    for l in lines or []:
        q = _f(l.get("Quantity_Shipped"))
        if q > TOL and l.get("Item_No"):
            shipped[str(l.get("Document_No"))][str(l["Item_No"])] += q
    heads = {str(h.get("No")): h for h in headers or []}
    out = {}
    for doc, s in shipments.items():
        items = set(s["items"])
        if not items:
            out[doc] = (None, "none")
            continue
        best = []
        for no, got in shipped.items():
            h = heads.get(no)
            if not h:
                continue
            frm = str(h.get("Transfer_from_Code") or "").upper()
            if s.get("from") and frm and frm != s["from"].upper():
                continue
            share = len(items & set(got)) / len(items)
            if share < 0.5:
                continue
            same_day = _day(h.get("Posting_Date")) == s.get("date")
            qty_ok = sum(1 for i in items & set(got) if abs(got[i] - s["items"][i]) <= max(TOL, 0.01 * got[i]))
            best.append(((share, same_day, qty_ok), no))
        if not best:
            out[doc] = (None, "none")
            continue
        best.sort(reverse=True)
        if len(best) > 1 and best[0][0] == best[1][0]:
            out[doc] = (None, "ambiguous")
        else:
            out[doc] = (best[0][1], "items" if best[0][0][0] == 1 else "most items")
    return out


def build(company, entries, headers, lines, ship_from, cards, today=None, flag_days=3, currency=""):
    """The report from the reads.

    entries: open item ledger entries at the in-transit locations (Remaining Quantity above zero)
    headers, lines: the open transfer orders (navtransferheaders / navtransferlines)
    ship_from: {shipment: sending location}, from the shipment's outgoing entries; may be empty
    cards: {code: {"name", "transit"}} from the location cards, or None when they could not be read
    """
    today = today or date.today()
    names = {k: (v.get("name") or "") for k, v in (cards or {}).items()}
    heads = {str(h.get("No")): h for h in headers or []}

    by_doc = defaultdict(list)
    for e in entries or []:
        if _f(e.get("Remaining_Quantity")) > TOL:
            by_doc[str(e.get("Document_No") or "")].append(e)
    shipments = {}
    for doc, es in by_doc.items():
        items = defaultdict(float)
        for e in es:
            items[str(e.get("Item_No") or "")] += _f(e.get("Quantity"))
        shipments[doc] = {"from": (ship_from or {}).get(doc, ""), "items": dict(items),
                          "date": min(_day(e.get("Posting_Date")) for e in es)}
    links = link_orders(shipments, headers, lines)

    transfers, rows = [], []
    for doc, es in by_doc.items():
        s = shipments[doc]
        order, how = links.get(doc, (None, "none"))
        h = heads.get(order) if order else None
        frm = s["from"] or (str(h.get("Transfer_from_Code") or "") if h else "")
        to = str(h.get("Transfer_to_Code") or "") if h else ""
        shipped = s["date"]
        try:
            days = (today - date.fromisoformat(shipped)).days
        except ValueError:
            days = None
        t = {"shipment": doc, "order": order or "", "link": how, "from": frm, "from_name": names.get(frm.upper(), ""),
             "to": to, "to_name": names.get(to.upper(), ""), "via": sorted({str(e.get("Location_Code") or "") for e in es}),
             "shipped": shipped, "days": days, "lines": 0, "items": len({str(e.get("Item_No")) for e in es}),
             "value": 0.0, "value_shipped": 0.0, "partly": False,
             "over": days is not None and days > flag_days}
        for e in sorted(es, key=lambda e: (str(e.get("Item_No")), e.get("Entry_No") or 0)):
            q, left = _f(e.get("Quantity")), _f(e.get("Remaining_Quantity"))
            cost = _f(e.get("Cost_Amount_Actual")) + _f(e.get("Cost_Amount_Expected"))
            value = cost * left / q if q > TOL else 0.0
            t["lines"] += 1
            t["value"] += value
            t["value_shipped"] += cost
            if left < q - TOL:
                t["partly"] = True
            rows.append({"shipment": doc, "order": order or "", "from": frm, "to": to, "shipped": shipped, "days": days,
                         "item": str(e.get("Item_No") or ""), "description": (e.get("Item_Description") or "").strip(),
                         "category": e.get("Item_Category_Code") or "", "uom": e.get("Unit_of_Measure_Code") or "",
                         "quantity": q, "remaining": left, "value": value, "entry": e.get("Entry_No"),
                         "via": str(e.get("Location_Code") or ""), "no_cost": cost == 0})
        transfers.append(t)
    transfers.sort(key=lambda t: (-(t["days"] or 0), -t["value"]))

    out = {"company": company, "as_of": today.isoformat(), "currency": currency, "flag_days": flag_days,
           "transit_locations": sorted({c for c, v in (cards or {}).items() if v.get("transit")})
           or sorted({str(e.get("Location_Code") or "") for e in entries or []}),
           "cards_read": cards is not None, "names": names,
           "transfers": transfers, "rows": rows,
           "orders_open": len(heads),
           "definitions": definitions(flag_days)}
    out.update(summarise(transfers, rows, flag_days))
    return out


def summarise(transfers, rows, flag_days):
    """The totals, routes, destinations and ages of a list of transfers (also after a cut to a scope)."""
    tot = {"transfers": len(transfers), "lines": len(rows), "items": len({r["item"] for r in rows}),
           "value": sum(t["value"] for t in transfers),
           "over": sum(1 for t in transfers if t["over"]),
           "over_value": sum(t["value"] for t in transfers if t["over"]),
           "oldest_days": max((t["days"] or 0 for t in transfers), default=0),
           "unlinked": sum(1 for t in transfers if not t["order"]),
           "partly": sum(1 for t in transfers if t["partly"]),
           "no_cost": sum(1 for r in rows if r["no_cost"])}

    def group(key, name_key=None):
        g = defaultdict(lambda: {"transfers": 0, "value": 0.0, "oldest": 0, "over": 0, "name": ""})
        for t in transfers:
            k = t[key] or ""
            x = g[k]
            x["transfers"] += 1
            x["value"] += t["value"]
            x["oldest"] = max(x["oldest"], t["days"] or 0)
            x["over"] += 1 if t["over"] else 0
            if name_key:
                x["name"] = t[name_key]
        return sorted(({"location": k, **v} for k, v in g.items()), key=lambda r: -r["value"])

    routes = defaultdict(lambda: {"transfers": 0, "value": 0.0, "oldest": 0})
    for t in transfers:
        r = routes[(t["from"], t["to"])]
        r["transfers"] += 1
        r["value"] += t["value"]
        r["oldest"] = max(r["oldest"], t["days"] or 0)
    ages = []
    for label, lo, hi in AGES:
        ts = [t for t in transfers if t["days"] is not None and t["days"] >= lo and (hi is None or t["days"] <= hi)]
        ages.append({"age": label, "from": lo, "to": hi, "transfers": len(ts), "value": sum(t["value"] for t in ts)})
    cats = defaultdict(lambda: {"lines": 0, "value": 0.0})
    for r in rows:
        c = cats[r["category"] or "(no category)"]
        c["lines"] += 1
        c["value"] += r["value"]
    return {"totals": tot, "destinations": group("to", "to_name"), "origins": group("from", "from_name"),
            "routes": sorted(({"from": a, "to": b, **v} for (a, b), v in routes.items()), key=lambda r: -r["value"]),
            "ages": ages,
            "categories": sorted(({"category": k, **v} for k, v in cats.items()), key=lambda r: -r["value"])}


def cut(body, allowed):
    """The report for a person limited to some locations: the transfers leaving or reaching one of them."""
    if not isinstance(body, dict) or allowed is None or "transfers" not in body:
        return body
    A = {str(a).upper() for a in allowed}
    keep = [t for t in body["transfers"] if (t["from"] or "").upper() in A or (t["to"] or "").upper() in A]
    docs = {t["shipment"] for t in keep}
    out = dict(body, transfers=keep, rows=[r for r in body["rows"] if r["shipment"] in docs], scoped=True)
    out.update(summarise(keep, out["rows"], body.get("flag_days", 3)))
    return out


def definitions(flag_days):
    return {
        "in_transit": "Open item ledger entries at an in-transit location (Location card: Use As In-Transit) with "
                      "quantity still remaining: shipped from one location and not yet received at the other.",
        "value": "The entry's cost (actual plus expected) for the quantity not yet received: cost × remaining ÷ "
                 "quantity. Items without a cost in BC show zero and are counted apart.",
        "days": "Days from the shipment's posting date to today.",
        "flag": f"A transfer in transit more than {flag_days} days is flagged (Settings → thresholds: "
                "Transfer in transit, flag after).",
        "from": "The location the shipment took the goods out of (the other side of the same posted shipment).",
        "to": "The receiving location of the transfer order behind the shipment. BC does not publish the order "
              "number on the shipment, so the order is found by its lines: the open transfer order from the same "
              "location whose shipped lines carry the shipment's items. Where none matches, or two match equally, "
              "the destination is left unknown.",
        "fix": "A transfer stays in transit until it is received in BC. If the goods arrived, post the receipt on the "
               "transfer order; if they did not, find them; if the shipment was wrong, undo it.",
    }
