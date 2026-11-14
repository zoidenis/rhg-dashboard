"""Supplier performance (suppliers.build) and Item history (itemhist._stats)."""
import unittest

import itemhist
import suppliers


def inv(no, v, amt, loc="SALT", cur="", day="2026-10-05", incl=None):
    return {"No": no, "Buy_from_Vendor_No": v, "Buy_from_Vendor_Name": "N" + v, "Posting_Date": day, "Amount": amt,
            "Amount_Including_VAT": incl if incl is not None else amt * 1.2, "Currency_Code": cur, "Location_Code": loc}


class Suppliers(unittest.TestCase):
    def test_net_credit_currency_and_new(self):
        ci = [inv("1", "F1", 1000), inv("2", "F1", 500, loc="LIFT"), inv("3", "F2", 10, cur="EUR", incl=12)]
        cc = [inv("C1", "F1", 200)]
        vle = [{"Document_Type": "Invoice", "Document_No": "3", "Amount_LCY": -1200}]
        pi = [inv("9", "F1", 1000, day="2026-09-05")]
        ve = [{"Document_Type": "Purchase Invoice", "Document_No": "1", "Cost_Amount_Actual": 800, "Item_No": "A"},
              {"Document_Type": "Purchase Credit Memo", "Document_No": "C1", "Cost_Amount_Actual": -150, "Item_No": "A"}]
        r = suppliers.build(ci, cc, vle, pi, [], [], ve, "ALL", {"thresholds": {"supplier_concentration_pct": 35}})
        f1 = next(x for x in r["rows"] if x["no"] == "F1")
        f2 = next(x for x in r["rows"] if x["no"] == "F2")
        self.assertEqual((f1["invoiced"], f1["credit"], f1["net"]), (1500, 200, 1300))
        self.assertEqual(f1["change_pct"], 30.0)
        self.assertEqual(f1["items_cost"], 650)
        self.assertEqual(f2["net"], 1000)            # 10 EUR at the posting's rate: 1200 / 12 = 100
        self.assertIn("new", f2["notes"])
        self.assertEqual(r["kpis"]["net"], 2300)
        self.assertEqual(dict(f1["locations"]), {"SALT": 800, "LIFT": 500})

    def test_unconverted_and_location(self):
        r = suppliers.build([inv("3", "F2", 10, cur="EUR"), inv("4", "F1", 50, loc="LIFT")], [], [], [], [], [], [],
                            "ALL", {}, location="LIFT")
        self.assertEqual([x["no"] for x in r["rows"]], ["F1"])
        r = suppliers.build([inv("3", "F2", 10, cur="EUR")], [], [], [], [], [], [], "ALL", {})
        self.assertEqual(r["kpis"]["unconverted"], 1)
        self.assertEqual(r["rows"], [])


class ItemPrices(unittest.TestCase):
    def test_stats_and_outliers(self):
        buys = [("2026-10-01", "SALT", "F1", 100, 10), ("2026-10-03", "SALT", "F2", 120, 10),
                ("2026-10-05", "LIFT", "F1", 110, 10), ("2026-10-06", "LIFT", "F1", 1100, 1)]   # the last: a unit error
        s = itemhist._stats(buys)
        self.assertEqual((s["min"], s["max"], s["last"], s["outliers"]), (10, 12, 11, 1))
        self.assertAlmostEqual(s["avg"], 1430 / 31, places=3)
        self.assertEqual(s["spread"], 20.0)


if __name__ == "__main__":
    unittest.main()
