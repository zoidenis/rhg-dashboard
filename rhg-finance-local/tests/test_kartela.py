"""Kartela: every open party with its age split, what falls due soon, open payments to apply; one party's entries."""
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import finance

T = date(2026, 11, 10)
S = {"thresholds": {"ap_critical_days": 60, "ar_critical_days": 60, "materiality": 1000, "concentration_pct": 99}}


def e(no, n, due, amt, typ="Invoice", post="2026-09-01"):
    return {"Entry_No": n, "Vendor_No": no, "Vendor_Name": "Furnitor " + no, "Posting_Date": post, "Due_Date": due,
            "Document_Type": typ, "Document_No": f"D{n}", "Remaining_Amt_LCY": -amt, "Original_Amt_LCY": -amt}


ROWS = [e("F1", 1, "2026-11-15", 100), e("F1", 2, "2026-07-01", 300), e("F1", 3, "2026-10-25", 50),
        e("F1", 4, "2026-11-01", -40, "Payment"), e("F2", 5, "2026-12-30", 70)] + \
       [e(f"F{i}", 10 + i, "2026-11-30", 5) for i in range(3, 60)]


class Kartela(unittest.TestCase):
    def test_every_party_with_its_split(self):
        ap = finance.payables(ROWS, T, S)
        self.assertEqual(len(ap["parties"]), 59)                       # all of them, not the first 40
        f1 = next(p for p in ap["parties"] if p["no"] == "F1")
        self.assertEqual(f1["total"], 410)
        self.assertEqual(f1["buckets"], [100, 10, 0, 0, 300])           # not due, 1–30 (50 − 40), …, over 90
        self.assertEqual(f1["overdue"], 310)
        self.assertEqual(f1["oldest_days"], 132)
        self.assertEqual((f1["due7"], f1["next_due"]), (100, "2026-11-15"))
        self.assertEqual((f1["unapplied"], f1["unapplied_amt"]), (1, -40))
        f2 = next(p for p in ap["parties"] if p["no"] == "F2")
        self.assertEqual((f2["due7"], f2["due30"], f2["next_due"]), (0, 0, "2026-12-30"))

    def test_one_partys_entries(self):
        x = finance.party_entries(ROWS, "ap", "F1", T)
        self.assertEqual([r["document"] for r in x], ["D2", "D3", "D4", "D1"])   # oldest due first
        self.assertEqual(x[0]["days"], 132)
        self.assertEqual(x[2]["remaining"], -40)


if __name__ == "__main__":
    unittest.main()
