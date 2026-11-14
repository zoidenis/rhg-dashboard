"""People & Productivity: Odoo hours split by the hour and business day, the mapping, the personnel cost."""
from datetime import date, datetime, timezone
import unittest

import odoo
import people


class Spread(unittest.TestCase):
    def test_after_midnight_counts_for_the_day_before(self):
        got = []
        # 21:30 to 02:15 Tirana time (UTC+1 in November): 20:30 to 01:15 UTC
        odoo._spread(datetime(2026, 11, 13, 20, 30, tzinfo=timezone.utc), datetime(2026, 11, 14, 1, 15, tzinfo=timezone.utc),
                     lambda d, h, x: got.append((d.isoformat(), h, round(x, 2))))
        self.assertEqual(got[0], ("2026-11-13", 21, 0.5))
        self.assertEqual(got[-1], ("2026-11-13", 2, 0.25))
        self.assertAlmostEqual(sum(x for _, _, x in got), 4.75)
        self.assertTrue(all(d == "2026-11-13" for d, _, _ in got))

    def test_morning_starts_a_new_business_day(self):
        got = []
        odoo._spread(datetime(2026, 11, 14, 4, 0, tzinfo=timezone.utc), datetime(2026, 11, 14, 5, 0, tzinfo=timezone.utc),
                     lambda d, h, x: got.append((d.isoformat(), h, x)))
        self.assertEqual(got, [("2026-11-14", 5, 1.0)])          # 05:00 local: the new business day


class Mapping(unittest.TestCase):
    def test_defaults_lines_and_review(self):
        m = odoo.mapping("OPA ARENA = X9\nMAGAZINA = -\n# comment\n")
        self.assertEqual(m["SALT TIRANA"], "SALT")
        self.assertEqual(m["OPA ARENA"], "X9")
        reg = {"SALT": {}, "OPA_B": {}}
        self.assertEqual(people.unit_ref("Salt Tirana", reg, m), ("SALT", "mapped"))
        self.assertEqual(people.unit_ref("OPA ARENA", reg, m), ("X9", "needs review"))
        self.assertEqual(people.unit_ref("MAGAZINA", reg, m), ("", "left out"))
        self.assertEqual(people.unit_ref("CENTRALIZIM", reg, m), ("", "no restaurant"))

    def test_only_reads(self):
        with self.assertRaises(odoo.Error):
            odoo.call("hr.attendance", "write", [[1], {"check_out": False}])

    def test_weekly_hours(self):
        self.assertEqual(odoo._weekly_hours({"name": "Standard 40 hours/week"}), 40)
        self.assertIsNone(odoo._weekly_hours({"name": "Working Hours of RHG"}))


class Personnel(unittest.TestCase):
    def test_cost_and_pct(self):
        cube = {"accounts": {"6411": ["Kitchen", "labor_kitchen"], "644": ["Insurance", "labor_kitchen"], "7011": ["Sales", "sales"]},
                "actual": {"SALT": {"2026-09": {"6411": 100, "644": 20, "7011": 1000}, "2026-09~ly": {"6411": 5}}}}
        r = people.personnel(cube, 2026)
        x = r["locations"][0]
        self.assertEqual((x["cost"], x["sales"], x["pct"]), (120, 1000, 12.0))
        self.assertEqual(r["months"], ["2026-09"])


if __name__ == "__main__":
    unittest.main()
