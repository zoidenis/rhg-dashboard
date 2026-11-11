"""Transfers in transit (transit.py): open entries at the in-transit location, linked to their transfer orders."""
import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import transit  # noqa: E402


def e(no, doc, item, q, left, cost, day="2026-11-10", loc="TRANSIT"):
    return {"Entry_No": no, "Document_No": doc, "Item_No": item, "Item_Description": item + " desc",
            "Item_Category_Code": "LP", "Location_Code": loc, "Posting_Date": day, "Quantity": q,
            "Remaining_Quantity": left, "Cost_Amount_Actual": cost, "Cost_Amount_Expected": 0,
            "Unit_of_Measure_Code": "KG"}


HEADERS = [{"No": "18097", "Transfer_from_Code": "SALT", "Transfer_to_Code": "SALT GC", "Posting_Date": "2026-09-29"},
           {"No": "18579", "Transfer_from_Code": "RHG LAB", "Transfer_to_Code": "VESPER", "Posting_Date": "2026-11-10"},
           {"No": "18592", "Transfer_from_Code": "RHG LAB", "Transfer_to_Code": "VESPER", "Posting_Date": "2026-11-11"},
           {"No": "18587", "Transfer_from_Code": "RHG LAB", "Transfer_to_Code": "SALT", "Posting_Date": "2026-11-10"}]
LINES = [{"Document_No": "18097", "Item_No": "A1", "Quantity_Shipped": 3},
         {"Document_No": "18097", "Item_No": "A2", "Quantity_Shipped": 10},
         {"Document_No": "18579", "Item_No": "B1", "Quantity_Shipped": 4},
         {"Document_No": "18579", "Item_No": "B2", "Quantity_Shipped": 1.5},
         {"Document_No": "18592", "Item_No": "B2", "Quantity_Shipped": 5.3},
         {"Document_No": "18592", "Item_No": "B3", "Quantity_Shipped": 2},
         {"Document_No": "18587", "Item_No": "B1", "Quantity_Shipped": 0}]
CARDS = {"TRANSIT": {"name": "In transit", "transit": True}, "SALT": {"name": "Salt", "transit": False},
         "SALT GC": {"name": "Salt Grand Cafe", "transit": False}, "VESPER": {"name": "Vesper", "transit": False},
         "RHG LAB": {"name": "Lab", "transit": False}}


class TransitTest(unittest.TestCase):
    def build(self, entries, ship_from=None, cards=CARDS):
        return transit.build("SALT", entries, HEADERS, LINES, ship_from or {}, cards,
                             today=date(2026, 11, 11), flag_days=3, currency="ALL")

    def test_links_and_ages(self):
        r = self.build([e(1, "124402", "A1", 3, 3, 3000, "2026-09-29"), e(2, "124402", "A2", 10, 10, 3500, "2026-09-29"),
                        e(3, "124874", "B1", 4, 4, 800), e(4, "124874", "B2", 1.5, 1.5, 900),
                        e(5, "124876", "B2", 5.3, 5.3, 3100, "2026-11-11"), e(6, "124876", "B3", 2, 2, 170, "2026-11-11")],
                       {"124402": "SALT", "124874": "RHG LAB", "124876": "RHG LAB"})
        by = {t["shipment"]: t for t in r["transfers"]}
        self.assertEqual(by["124402"]["order"], "18097")
        self.assertEqual(by["124402"]["to"], "SALT GC")
        self.assertEqual(by["124402"]["to_name"], "Salt Grand Cafe")
        self.assertEqual(by["124402"]["days"], 43)
        self.assertTrue(by["124402"]["over"])
        # two orders from RHG LAB to VESPER: told apart by their items
        self.assertEqual(by["124874"]["order"], "18579")
        self.assertEqual(by["124876"]["order"], "18592")
        self.assertEqual(by["124876"]["days"], 0)
        self.assertFalse(by["124876"]["over"])
        self.assertEqual(r["transfers"][0]["shipment"], "124402")          # the oldest first
        self.assertEqual(r["totals"]["transfers"], 3)
        self.assertEqual(r["totals"]["over"], 1)
        self.assertAlmostEqual(r["totals"]["value"], 3000 + 3500 + 800 + 900 + 3100 + 170)
        self.assertEqual(r["totals"]["oldest_days"], 43)
        ages = {a["age"]: a["transfers"] for a in r["ages"]}
        self.assertEqual(ages["Over 30 days"], 1)
        self.assertEqual(ages["0–1 days"], 2)

    def test_partly_received_value_and_no_cost(self):
        r = self.build([e(1, "124402", "A1", 3, 1, 3000, "2026-09-29"), e(2, "124402", "A2", 10, 10, 0, "2026-09-29")])
        t = r["transfers"][0]
        self.assertTrue(t["partly"])
        self.assertAlmostEqual(t["value"], 1000)                              # a third of the cost is still in transit
        self.assertEqual(r["totals"]["no_cost"], 1)
        self.assertEqual(t["from"], "SALT")                                   # from the order when the shipment is unread

    def test_unmatched_and_ambiguous(self):
        r = self.build([e(1, "200", "Z9", 1, 1, 10)])
        self.assertEqual(r["transfers"][0]["order"], "")
        self.assertEqual(r["transfers"][0]["link"], "none")
        self.assertEqual(r["transfers"][0]["to"], "")
        self.assertEqual(r["totals"]["unlinked"], 1)
        # B2 alone is on both RHG LAB → VESPER orders, same quantity on neither, other day on both: no guess
        r = self.build([e(1, "201", "B2", 2, 2, 10, "2026-11-09")], {"201": "RHG LAB"})
        self.assertEqual(r["transfers"][0]["link"], "ambiguous")
        self.assertEqual(r["transfers"][0]["order"], "")

    def test_sending_location_rules_out_an_order(self):
        r = self.build([e(1, "300", "A1", 3, 3, 10), e(2, "300", "A2", 10, 10, 10)], {"300": "MAGAZINE"})
        self.assertEqual(r["transfers"][0]["order"], "")
        self.assertEqual(r["transfers"][0]["from"], "MAGAZINE")

    def test_cut_to_scope(self):
        r = self.build([e(1, "124402", "A1", 3, 3, 3000, "2026-09-29"), e(3, "124874", "B1", 4, 4, 800),
                        e(4, "124874", "B2", 1.5, 1.5, 900)], {"124402": "SALT", "124874": "RHG LAB"})
        c = transit.cut(r, {"VESPER"})
        self.assertEqual([t["shipment"] for t in c["transfers"]], ["124874"])
        self.assertEqual(len(c["rows"]), 2)
        self.assertAlmostEqual(c["totals"]["value"], 1700)
        self.assertEqual(transit.cut(r, None), r)
        self.assertEqual(len(r["transfers"]), 2)                              # the held original is untouched

    def test_transit_codes(self):
        self.assertTrue(transit.is_transit("TRANSIT"))
        self.assertTrue(transit.is_transit("X", {"X": {"transit": True}}))
        self.assertFalse(transit.is_transit("TRANSIT", {"TRANSIT": {"transit": False}}))
        self.assertFalse(transit.is_transit("SALT"))


if __name__ == "__main__":
    unittest.main()
