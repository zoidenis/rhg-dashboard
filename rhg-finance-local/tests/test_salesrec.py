"""Simphony ↔ BC: open = not posted, the four-way split of the sales, exact totals, the cases explained."""
import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import salesrec  # noqa: E402

S = {"days": 14, "import_lag": 2, "post_lag": 4, "settle_days": 7, "tol_abs": 1000, "tol_pct": 0.5}
REG = [{"ref": "SALT", "name": "SALT Tirana", "customer": "SALT", "bc": "SALT", "dept": "SALT1"},
       {"ref": "OPA_TI", "name": "OPA TIA", "customer": "OPA TIA", "bc": "OPA TIA", "dept": "OPATIA"}]
DAYS = ["2026-11-01", "2026-11-11"]


def run(**k):
    # SALT 11 Nov as BC holds it: import 958,043 − 6,910 = 951,133, posted F047494 951,133 with VAT 158,472.17
    sim = {"SALT": {"2026-11-01": {1: (500000.5, 10, 0.0)}, "2026-11-11": {1: (958043.0, 1036, 6910.0)}},
           "OPA_TI": {"2026-11-01": {2: (682304.0, 50, 0.0)}}}
    sim_vat = {"SALT": {"2026-11-11": {1: 158522.17}}}
    imp = {("SALT", "2026-11-11"): {"mi": {"1": [958043.0, 1036, -6910.0, "I1", "Item 1"]}, "unposted": 0, "rows": 191,
                                    "imported_at": ""},
           ("SALT", "2026-11-01"): {"mi": {"1": [500000.5, 10, 0.0, "I1", "Item 1"]}, "unposted": 3, "rows": 5,
                                    "imported_at": ""},
           ("OPA TIA", "2026-11-01"): {"mi": {"2": [682304.0, 50, 0.0, "I2", "Item 2"]}, "unposted": 0, "rows": 9,
                                       "imported_at": ""}}
    inv = {("SALT", "2026-11-11"): {"amount": 951133.0, "remaining": 951133.0, "docs": ["F047494"]},
           ("OPA TIA", "2026-11-01"): {"amount": 682304.0, "remaining": 682304.0, "docs": ["F047388"]}}
    posted = {("SALT", "2026-11-11"): {"ex": 792660.83, "incl": 951133.0, "disc": 0.0, "lines": 191}}
    return salesrec.analyse("SALT", REG, DAYS, sim, {}, imp, inv, {}, S, date(2026, 11, 13), "ALL", True, {}, posted,
                            {"I1": 20.0}, sim_vat, **k)


class SalesRec(unittest.TestCase):
    def test_open_is_not_posted_and_takings_are_not_a_case(self):
        r = run()
        salt, opa = r["locations"]
        self.assertEqual(salt["cells"]["2026-11-01"]["status"], "not_posted")          # 3 lines not posted, 12 days
        self.assertEqual(salt["cells"]["2026-11-11"]["status"], "ok")                  # posted, though unpaid
        self.assertEqual(opa["cells"]["2026-11-01"]["status"], "ok")                    # unpaid 12 days: Takings' job
        self.assertEqual(opa["cells"]["2026-11-01"]["remaining"], 682304.0)
        self.assertIn("not_posted", salt["cells"]["2026-11-01"]["why"])
        self.assertIn("3 of 5 imported lines", salt["cells"]["2026-11-01"]["why"]["not_posted"])
        self.assertEqual(r["kpis"]["cases"], 1)
        self.assertEqual(r["kpis"]["sim"], 500000.5 + 951133 + 682304)                  # to the cent, not rounded

    def test_the_split(self):
        c = run()["locations"][0]["cells"]["2026-11-11"]
        self.assertEqual(c["b"]["sim"], {"gross": 958043.0, "disc": 6910.0, "incl": 951133.0, "vat": 158522.17,
                                         "ex": 792610.83, "vat_unknown": 0.0})
        self.assertEqual(c["b"]["imp"]["incl"], 951133.0)
        self.assertAlmostEqual(c["b"]["imp"]["vat"], 951133 * 20 / 120, 2)
        self.assertEqual(c["b"]["inv"]["vat"], 158472.17)
        self.assertEqual(c["b"]["inv"]["ex"], 792660.83)

    def test_departments_and_scope(self):
        r = salesrec.cut(run(), ["OPATIA"])
        self.assertEqual([L["name"] for L in r["locations"]], ["OPA TIA"])
        self.assertEqual(r["kpis"]["sim"], 682304.0)

    def test_range(self):
        a, b = salesrec.check_range("2026-11-01", "2026-12-31", date(2026, 11, 13))
        self.assertEqual((a.isoformat(), b.isoformat()), ("2026-11-01", "2026-11-12"))
        with self.assertRaises(ValueError):
            salesrec.check_range("2026-01-01", "2026-11-12", date(2026, 11, 13))


if __name__ == "__main__":
    unittest.main()
