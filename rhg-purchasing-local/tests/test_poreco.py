"""Orders vs recommendation (22 Nov 2026): what was bought against the SKU card's maximum, its cost."""
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import poreco
import stock

P = {"tolerance_pct": 10.0, "carry_pct": 25.0, "fresh_days": 4.0, "cadence": stock.parse_cadence("*=2/3/2, MAGAZINE=45/30/10")}
CAT = {"A1": {"desc": "Fileto vici", "code": "MISH&SHPENDE", "uom": "KG"}, "B2": {"desc": "Vaj ulliri", "code": "VAJRA", "uom": "LITER"},
       "C3": {"desc": "Kripe", "code": "ERZA", "uom": "KG"}}


def mv(no, item, day, qty, kind="Sale"):
    return {"Entry_No": no, "Entry_Type": kind, "Item_No": item, "Posting_Date": day, "Quantity": qty}


class PoReco(unittest.TestCase):
    def setUp(self):
        # LIFT, 1–30 Nov: fileto 10 kg a day, maximum 60 on its card; 40 kg on hand when 80 kg arrived on the 10th
        self.moves = {"LIFT": [mv(1, "A1", "2026-11-05", -10), mv(2, "A1", "2026-11-10", 80, "Purchase"), mv(3, "A1", "2026-11-20", -290),
                               mv(4, "B2", "2026-11-12", 20, "Purchase"), mv(5, "B2", "2026-11-25", -15),
                               mv(6, "C3", "2026-11-15", 5, "Purchase")]}
        self.receipts = [{"Entry_No": 2, "Item_No": "A1", "Location_Code": "LIFT", "Posting_Date": "2026-11-10", "Quantity": 80,
                          "Cost_Amount_Actual": 120000, "Document_No": "RCPT1", "Source_No": "V1"},
                         {"Entry_No": 4, "Item_No": "B2", "Location_Code": "LIFT", "Posting_Date": "2026-11-12", "Quantity": 20,
                          "Cost_Amount_Actual": 20000, "Document_No": "RCPT2", "Source_No": "V2"},
                         {"Entry_No": 6, "Item_No": "C3", "Location_Code": "LIFT", "Posting_Date": "2026-11-15", "Quantity": 5,
                          "Cost_Amount_Actual": 500, "Document_No": "RCPT3", "Source_No": "V3"}]
        self.opening = {"LIFT": {"A1": 50, "B2": 0}}
        self.skus = {("A1", "LIFT"): {"maximum": 60, "reorder_point": 30}}

    def lines(self, open_lines=()):
        return {x["item"] + x["kind"]: x for x in poreco.analyse(self.receipts, list(open_lines), self.opening, self.moves, self.skus, CAT, P,
                                                          "2026-11-01", "2026-11-30", date(2026, 12, 1))}

    def test_over_the_card_maximum(self):
        x = self.lines()["A1received"]
        self.assertEqual((x["on_hand"], x["max"], x["rec"], x["qty"], x["over"]), (40, 60, 20, 80, 60))
        self.assertEqual(x["excess_value"], 90000.0)               # 60 kg at 1,500
        self.assertTrue(x["early"])                                 # 40 on hand above the reorder point of 30
        self.assertEqual(x["basis"], "SKU card in BC")
        self.assertEqual(x["daily"], 10.0)                          # 300 kg left in 30 days
        self.assertEqual(x["cover_days"], 6.0)
        self.assertAlmostEqual(x["carry"], 90000 * 0.25 * 6 / 365, places=1)
        self.assertTrue(x["fresh"])
        self.assertEqual(x["waste_risk"], (60 - 40) * 1500)         # beyond 4 days of use (40 kg)

    def test_estimate_without_a_card_and_no_basis(self):
        L = self.lines()
        b = L["B2received"]                                         # 0.5 a day × (2+3+2) days = 3.5 → 16.5 over
        self.assertEqual((b["basis"], b["max"], b["rec"]), ("Pulse estimate", 3.5, 3.5))
        self.assertEqual(b["over"], 16.5)
        self.assertEqual(L["C3received"]["basis"], "no basis")       # no maximum and no use: not judged
        self.assertEqual(L["C3received"]["over"], 0)

    def test_open_order_counts_what_is_on_order(self):
        o = [{"order": "PO1", "line": 0, "item": "A1", "location": "LIFT", "qty_left": 30, "unit_cost": 1500, "order_date": "2026-11-28", "purchaser": "BESNIK", "vendor": "V1"},
             {"order": "PO2", "line": 0, "item": "A1", "location": "LIFT", "qty_left": 30, "unit_cost": 1500, "order_date": "2026-11-29", "purchaser": "ERI", "vendor": "V1"}]
        L = [x for x in poreco.analyse([], o, self.opening, self.moves, self.skus, CAT, P, "2026-11-01", "2026-11-30") if x["kind"] == "open"]
        # on hand now: 50 - 10 + 80 - 290 = -170 → max 60 leaves 230; the second order counts the first on order
        self.assertEqual([(x["doc"], x["rec"], x["over"], x["who"]) for x in L], [("PO1", 230, 0, "BESNIK"), ("PO2", 200, 0, "ERI")])

    def test_summary_and_decisions(self):
        s = poreco.summarise(list(self.lines().values()), P)
        self.assertEqual(s["kpis"]["over_lines"], 2)
        self.assertEqual(s["locations"][0]["key"], "LIFT")
        self.assertEqual(s["lines"][0]["item"], "A1")
        self.assertTrue(any(a["owner"] == "CFO" and "approval" in a["text"] for a in s["actions"]))


if __name__ == "__main__":
    unittest.main()
