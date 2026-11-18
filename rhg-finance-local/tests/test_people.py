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

    def _cube(self):
        acc = {"6411": ["Kitchen", "labor_kitchen"], "6412": ["Service", "labor_service"], "7011": ["Sales", "sales"]}
        m = lambda k, sv: {"2026-09": {"6411": k, "6412": sv, "7011": 10000}}
        return {"accounts": acc, "actual": {"SALT": m(300, 200), "MINI": m(90, 0), "HQ": {"2026-09": {"6411": 500}}}}

    def _run(self, heads):
        from unittest import mock
        reg = [{"dept": "SALT", "ref": "SALT", "name": "Salt"}, {"dept": "MINI", "ref": "MINI", "name": "Mini"}]
        with mock.patch.object(people.locintel, "registry", return_value=reg):
            return people.personnel(self._cube(), 2026, None, heads)

    def test_confidential_no_accounts_small_teams_together(self):
        r = self._run({"SALT": {"FOH": 5, "BOH": 4, "MGMT": 1, "Other": 0, "total": 10}, "MINI": {"FOH": 1, "BOH": 1, "MGMT": 0, "Other": 0, "total": 2}})
        by = {x["dept"]: x for x in r["locations"]}
        self.assertEqual(set(by), {"SALT", "*SUPPORT"})
        self.assertNotIn("accounts", by["SALT"])
        self.assertEqual(by["*SUPPORT"]["departments"], 2)          # MINI (2 people) with head office
        self.assertEqual(by["*SUPPORT"]["cost"], 590)
        self.assertIsNone(by["*SUPPORT"]["sections"])
        # every section with cost has 3 people or more: split
        self.assertEqual(by["SALT"]["sections"]["labor_kitchen"], {"cost": 300, "people": 4})

    def test_no_split_when_a_section_is_small(self):
        r = self._run({"SALT": {"FOH": 2, "BOH": 4, "MGMT": 1, "Other": 0, "total": 7}})
        x = next(x for x in r["locations"] if x["dept"] == "SALT")
        self.assertIsNone(x["sections"])
        self.assertTrue(all(m[5] is None for m in x["months"]))

    def test_a_lone_small_team_is_withheld(self):
        from unittest import mock
        reg = [{"dept": "SALT", "ref": "SALT", "name": "Salt"}, {"dept": "MINI", "ref": "MINI", "name": "Mini"}]
        with mock.patch.object(people.locintel, "registry", return_value=reg):
            r = people.personnel(self._cube(), 2026, {"MINI"}, {"MINI": {"FOH": 1, "BOH": 1, "MGMT": 0, "Other": 0, "total": 2}})
        self.assertEqual(r["locations"], [])
        self.assertEqual(r["withheld"], 1)


if __name__ == "__main__":
    unittest.main()
