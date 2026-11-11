"""Contract prices: who an item was bought from, and the item lists the pop-ups read (contracts.py)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import contracts

S = {"thresholds": {"price_above_contract_pct": 2}}
OK = {"available": True, "count": 2}


def row(item, doc, cost, qty, loc="L1"):
    return {"Item_No": item, "Item_Description": item, "Document_No": doc, "Cost_Amount_Actual": cost,
            "Item_Ledger_Entry_Quantity": qty, "Location_Code": loc}


def test_supplier_is_the_one_that_invoiced_most():
    rows = [row("A", "PI1", 1000, 10), row("A", "PI2", 300, 3), row("B", "RCPT9", 500, 5)]
    sup = {"PI1": {"vendor": "ROAL FOOD"}, "PI2": {"vendor": "OTHER"}}
    prices = {"A": {"price": 100, "vendor": "V1"}, "B": {"price": 90, "vendor": "V2"}}
    r = contracts.build(rows, prices, OK, {}, S, "", sup, {"V2": "Meat Co"})
    by = {x["item"]: x for x in r["items"]}
    assert by["A"]["vendor"] == "ROAL FOOD"
    assert by["B"]["vendor"] == "Meat Co"                      # received, not invoiced: the agreed price's supplier
    assert {s["supplier"] for s in r["suppliers"]} == {"ROAL FOOD", "Meat Co"}


def test_status_and_lists():
    rows = [row("A", "PI1", 1100, 10), row("B", "PI1", 800, 10), row("C", "PI1", 500, 5), row("D", "PI1", 300, 3)]
    prices = {"A": {"price": 100}, "B": {"price": 100}, "C": {"price": 100}}
    r = contracts.build(rows, prices, OK, {}, S, "", {"PI1": {"vendor": "X"}}, {})
    st = {x["item"]: x["status"] for x in r["items"]}
    assert st == {"A": "above", "B": "below", "C": "within", "D": "none"}
    assert r["items_total"] == 4 and r["suppliers"][0]["below"] < 0
