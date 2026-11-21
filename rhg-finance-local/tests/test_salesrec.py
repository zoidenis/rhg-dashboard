"""Simphony ↔ BC: open = not posted, the four-way split of the sales, exact totals, the cases explained."""
import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import salesrec  # noqa: E402

S = {"days": 14, "import_lag": 2, "post_lag": 4, "settle_days": 7, "tol_abs": 1, "tol_pct": 0, "svc_accounts": ["75"]}
REG = [{"ref": "SALT", "name": "SALT Tirana", "customer": "SALT", "bc": "SALT", "dept": "SALT1"},
       {"ref": "OPA_TI", "name": "OPA TIA", "customer": "OPA TIA", "bc": "OPA TIA", "dept": "OPATIA"}]
DAYS = ["2026-11-01", "2026-11-11"]


def run(svc=None, inv_extra=0.0, **k):
    # SALT 11 Nov as BC holds it: import 958,043 − 6,910 = 951,133, posted F047494 951,133 with VAT 158,472.17
    sim = {"SALT": {"2026-11-01": {1: (500000.5, 10, 0.0)}, "2026-11-11": {1: (958043.0, 1036, 6910.0)}},
           "OPA_TI": {"2026-11-01": {2: (682304.0, 50, 0.0)}}}
    sim_vat = {"SALT": {"2026-11-11": {1: 159673.83}}}           # Simphony's vatTtl: 20% inside the price before discounts
    imp = {("SALT", "2026-11-11"): {"mi": {"1": [958043.0, 1036, -6910.0, "I1", "Item 1"]}, "unposted": 0, "rows": 191,
                                    "imported_at": ""},
           ("SALT", "2026-11-01"): {"mi": {"1": [500000.5, 10, 0.0, "I1", "Item 1"]}, "unposted": 3, "rows": 5,
                                    "imported_at": ""},
           ("OPA TIA", "2026-11-01"): {"mi": {"2": [682304.0, 50, 0.0, "I2", "Item 2"]}, "unposted": 0, "rows": 9,
                                       "imported_at": ""}}
    inv = {("SALT", "2026-11-11"): {"amount": 951133.0 + inv_extra, "remaining": 951133.0, "docs": ["F047494"]},
           ("OPA TIA", "2026-11-01"): {"amount": 682304.0, "remaining": 682304.0, "docs": ["F047388"]}}
    posted = {("SALT", "2026-11-11"): {"ex": 792660.83, "incl": 951133.0, "disc": 0.0, "lines": 191}}
    return salesrec.analyse("SALT", REG, DAYS, sim, {}, imp, inv, {}, S, date(2026, 11, 13), "ALL", True, {}, posted,
                            {"I1": 20.0}, sim_vat, svc, **k)


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
        # without Simphony's daily totals: each item's rate (vatTtl / price before discounts) on its sales after them
        self.assertEqual(c["b"]["sim"], {"gross": 958043.0, "disc": 6910.0, "incl": 951133.0, "vat": 158522.16,
                                         "ex": 792610.84, "vat_unknown": 0.0})
        self.assertEqual(c["b"]["imp"]["incl"], 951133.0)
        self.assertAlmostEqual(c["b"]["imp"]["vat"], 951133 * 20 / 120, 2)
        self.assertEqual(c["b"]["inv"]["vat"], 158472.17)
        self.assertEqual(c["b"]["inv"]["ex"], 792660.83)

    def test_service_charge(self):
        # LIFT 1 and 6 Nov (QA 14 Nov 2026): a service charge in Simphony, the day posted as the menu items only
        svc = {"SALT": {"2026-11-11": 16973.84}}
        c = run(svc)["locations"][0]["cells"]["2026-11-11"]
        self.assertEqual(c["issues"], ["service_charge"])
        self.assertEqual(c["status"], "service_charge")
        self.assertIn("16,973.84", c["why"]["service_charge"])
        r = run(svc, inv_extra=16973.84)                      # the invoice carries it: reconciled
        c = r["locations"][0]["cells"]["2026-11-11"]
        self.assertEqual((c["status"], c["svc_posted"]), ("ok", 16973.84))
        self.assertEqual(r["kpis"]["svc"], 16973.84)
        self.assertEqual(r["kpis"]["svc_missing"], 0.0)
        small = run({"SALT": {"2026-11-11": 600.0}}, inv_extra=600.0)["locations"][0]["cells"]["2026-11-11"]
        self.assertEqual((small["status"], small["svc_posted"]), ("ok", 600.0))       # below the tolerance too
        small = run({"SALT": {"2026-11-11": 600.0}})["locations"][0]["cells"]["2026-11-11"]
        self.assertEqual(small["status"], "service_charge")

    def test_simphony_day_from_its_totals_and_vat_by_tax_type(self):
        # OPA TEG as R&A's Taxes report shows a day: net sales 7,394,902.75 with VAT 1,232,483.32; the import equal;
        # the menu items summed to 1,100 less and their vatTtl (on the price before discounts) to 1,267,913.14
        tot = {"SALT": {"2026-11-11": {"net": 951133.0, "tax": 158522.17, "disc": 6910.0, "svc": 0.0, "has": True}}}
        c = run(tot=tot)["locations"][0]["cells"]["2026-11-11"]
        self.assertEqual((c["sim"], c["b"]["sim"]["vat"], c["b"]["sim"]["incl"]), (951133.0, 158522.17, 951133.0))
        self.assertEqual(c["status"], "ok")                                   # 951,133 / 6 = 158,522.17 in BC too
        tot["SALT"]["2026-11-11"]["tax"] = 158522.17 - 35000
        c = run(tot=tot)["locations"][0]["cells"]["2026-11-11"]
        self.assertIn("vat_diff", c["issues"])                               # VAT by tax type ≠ BC item VAT %
        self.assertIn("VAT in Simphony", c["why"]["vat_diff"])
        tot["SALT"]["2026-11-11"].update(tax=158522.17, net=951133.0 - 1100)
        c = run(tot=tot)["locations"][0]["cells"]["2026-11-11"]
        self.assertEqual(c["status"], "import_diff")                         # 1,100 no longer within a 0.5% tolerance

    def test_old_tolerance_reads_as_new(self):
        S2 = salesrec.settings_of({"sales_rec": {"tol_abs": "1000", "tol_pct": "0.5"}})
        self.assertEqual((S2["tol_abs"], S2["tol_pct"]), (1.0, 0.0))
        self.assertEqual(salesrec.settings_of({"sales_rec": {"tol_abs": "50", "tol_pct": "0.5"}})["tol_abs"], 50.0)

    def test_service_charge_on_its_own_invoice(self):
        # LIFT 1 Nov: the menu items on F047379 (501,329), the service charge on F047509 (account 751, 16,077.28)
        svc = {"SALT": {"2026-11-11": 16077.28}}
        posted = {("SALT", "2026-11-11"): {"ex": 792660.83, "incl": 951133.0, "disc": 0.0, "lines": 191,
                                           "svc": 16077.28, "svc_docs": ["F047509"]}}
        c = salesrec.analyse("SALT", REG, DAYS, {"SALT": {"2026-11-11": {1: (958043.0, 1036, 6910.0)}}}, {},
                             {("SALT", "2026-11-11"): {"mi": {"1": [958043.0, 1036, -6910.0, "I1", "Item 1"]}, "unposted": 0,
                                                       "rows": 191, "imported_at": ""}},
                             {("SALT", "2026-11-11"): {"amount": 951133.0 + 16077.28, "remaining": 0, "docs": ["F047494", "F047509"]}},
                             {}, S, date(2026, 11, 13), "ALL", True, {}, posted, {"I1": 20.0}, {}, svc
                             )["locations"][0]["cells"]["2026-11-11"]
        self.assertEqual((c["status"], c["svc_posted"]), ("ok", 16077.28))
        self.assertEqual(c["b"]["inv"]["svc"], 16077.28)

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
