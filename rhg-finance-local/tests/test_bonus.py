"""Sales → Bonus management (bonus.py): the workbook's scoring, the plans, the sources, locks and the payout."""
import pathlib
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

import bonus
import store

UNIT = {"ref": "OPA_B", "name": "OPA Bllok", "brand": "OPA", "company": "SALT", "dept": "OPA1", "bc": "OPA1"}
FRANCHISE = {"ref": "OPA_SK", "name": "OPA Shkodër", "brand": "OPA", "company": "SALT", "dept": "OPASK", "ownership": "Franchise"}
QTU = {"ref": "OPA_Q", "name": "OPA QTU", "brand": "OPA", "company": "SALT", "dept": "OPA QTU"}
OTHER = {"ref": "LIFT", "name": "LIFT", "brand": "LIFT", "company": "SALT", "dept": "LIFT"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.p = patch.object(bonus, "FILE", self.tmp / "bonus.json")
        self.p.start()
        self.a = patch.object(store, "audit", lambda *a, **k: None)
        self.a.start()

    def tearDown(self):
        self.p.stop()
        self.a.stop()


class Scoring(Base):
    def plan(self, u=UNIT):
        return bonus.plan_for(u)["plan"]

    def k(self, key, u=UNIT):
        return next(x for x in self.plan(u)["kpis"] if x["key"] == key)

    def test_sales_bands_of_the_workbook(self):
        k = self.k("sales_budget")
        for pct, want in ((94.9, 1), (95, 2), (99.9, 2), (100, 3), (103.9, 3), (104, 4), (107, 5), (109.9, 5), (110, 6), (130, 6)):
            self.assertEqual(bonus.score_of(k, pct, 100)[0], want, pct)
        self.assertEqual(bonus.score_of(k, 100, None), (None, None))      # no budget: no score

    def test_food_cost_against_plan_in_points(self):
        k = self.k("food_cost")                                      # 40/39/38/37.5/37/36 around a 38% plan
        for v, want in ((40.5, 1), (39, 2), (38, 3), (37.5, 4), (37, 5), (36, 6), (35, 6)):
            self.assertEqual(bonus.score_of(k, v, 38)[0], want, v)

    def test_value_kpis_lower_and_higher(self):
        self.assertEqual(bonus.score_of(self.k("tm_turnover"), 4.5, 10)[0], 6)
        self.assertEqual(bonus.score_of(self.k("tm_turnover"), 18.2, 10)[0], 1)
        self.assertEqual(bonus.score_of(self.k("ops_reporting"), 93, 80)[0], 5)
        self.assertEqual(bonus.score_of(self.k("food_safety"), 95, 80)[0], 6)

    def test_weighted_score_shares_the_missing_weight(self):
        auto = {"sales_budget": {"value": 100, "target": 100}, "sales_ly": {"value": 110, "target": 100}}
        r = bonus.evaluate(UNIT, self.plan(), auto, {})
        # 16 × 3 + 17 × 6 over 33
        self.assertAlmostEqual(r["score"], (16 * 3 + 17 * 6) / 33, places=4)
        self.assertEqual(r["coverage"], 33.0)
        self.assertEqual((r["amount"], r["band"]), (120000, "≥ 4.50"))   # 4.55

    def test_ranges(self):
        p = self.plan()
        self.assertEqual(bonus.band_of(p, 2.99), (0, None))
        self.assertEqual(bonus.band_of(p, 3.0), (60000, "3.00 – 3.49"))
        self.assertEqual(bonus.band_of(p, 3.494), (60000, "3.00 – 3.49"))
        self.assertEqual(bonus.band_of(p, 3.5), (70000, "3.50 – 3.99"))
        self.assertEqual(bonus.band_of(p, 4.49), (85000, "4.00 – 4.49"))
        self.assertEqual(bonus.band_of(p, 5.2), (120000, "≥ 4.50"))
        self.assertEqual(bonus.band_of(self.plan(QTU), 4.6)[0], 60000)     # QTU's own range (the workbook)

    def test_manual_wins_and_reviews_minimum(self):
        p = self.plan()
        auto = {"reviews": {"value": 4.8, "count": 4}}
        r = bonus.evaluate(UNIT, p, auto, {})
        rv = next(x for x in r["kpis"] if x["key"] == "reviews")
        self.assertEqual(rv["score"], 1)                              # 4 of the 10 reviews needed
        r = bonus.evaluate(UNIT, p, auto, {"reviews": {"value": 4.5, "by": "ceo"}})
        rv = next(x for x in r["kpis"] if x["key"] == "reviews")
        self.assertEqual((rv["value"], rv["score"], rv["status"]), (4.5, 4, "entered by hand"))


class Plans(Base):
    def test_teg_weights_and_default_brand(self):
        teg = dict(UNIT, ref="OPA_TE")
        p = bonus.plan_for(teg)
        self.assertEqual({k["key"]: k["weight"] for k in p["plan"]["kpis"]}["sales_budget"], 15)
        self.assertEqual(sum(k["weight"] for k in p["plan"]["kpis"]), 100)
        self.assertEqual(bonus.plan_for(OTHER)["template"], "*")

    def test_validation(self):
        good = bonus.plan_for(UNIT)["plan"]
        bonus.validate_plan(good)
        bad = dict(good, kpis=[dict(good["kpis"][0], weight=50)] + good["kpis"][1:])
        with self.assertRaisesRegex(ValueError, "add up"):
            bonus.validate_plan(bad)
        bad = dict(good, kpis=[dict(good["kpis"][0], cuts=[110, 107, 104, 100, 95])] + good["kpis"][1:])
        with self.assertRaisesRegex(ValueError, "go up"):
            bonus.validate_plan(bad)
        with self.assertRaises(ValueError):
            bonus.validate_ranges([{"min": 3, "amount": -1}])

    def test_brand_plan_and_unit_override(self):
        p = bonus.plan_for(UNIT)["plan"]
        bonus.save_template("lift", p, "ceo")
        self.assertEqual(bonus.plan_for(OTHER)["template"], "LIFT")
        with self.assertRaisesRegex(ValueError, "100%"):
            bonus.save_override("SALT", UNIT, {"weights": {"sales_budget": 30}}, "ceo")
        bonus.save_override("SALT", UNIT, {"weights": {"sales_budget": 20, "team_trained": 0, "tm_turnover": 0, "ops_reporting": 0,
                                                       "sales_ly": 20}, "eligible": False}, "ceo")
        self.assertFalse(bonus.eligible(UNIT))
        self.assertFalse(bonus.eligible(FRANCHISE))                   # a franchise does not qualify by default
        bonus.delete_template("LIFT", "ceo")
        self.assertEqual(bonus.plan_for(OTHER)["template"], "*")


EX = {
    "departments": [{"code": "OPA1"}], "provisional": ["2026-10"],
    "accounts": {"700": ["Sales", "sales", "revenue"], "601": ["Food", "food", "cogs"], "641": ["Salaries", "labor_other", "labour"]},
    "actual": {"OPA1": {"2026-09": {"700": 1000.0, "601": 330.0, "641": 180.0}}},
    "budget": {"cells": {"OPA1": {"2026-09": {"700": 950.0, "601": 304.0, "641": 190.0}}}},
    "simphony": {"locations": {"OPA_B": {"dept": "OPA1", "months": {"2026-09": {"net": 19951036}, "2025-09": {"net": 18417209.6}}}}},
}


class Sources(Base):
    def test_from_exec(self):
        a = bonus.from_exec(EX, UNIT, "2026-09")
        self.assertEqual(a["sales_budget"]["value"], 19951036)
        self.assertEqual(a["sales_budget"]["target"], 950.0)
        self.assertEqual(a["sales_ly"]["target"], 18417209.6)
        self.assertEqual((a["food_cost"]["value"], a["food_cost"]["target"]), (33.0, 32.0))
        self.assertEqual((a["labor_cost"]["value"], a["labor_cost"]["target"]), (18.0, 20.0))
        b = bonus.from_exec(EX, dict(UNIT, dept="NOPE", ref="X"), "2026-09")
        self.assertEqual(b["food_cost"]["status"], "the unit's department is not in BC")

    def test_running_month_prorates_the_budget(self):
        ex = {**EX, "simphony": {"locations": {"OPA_B": {"dept": "OPA1", "months": {"2026-09": {"net": 500}}}}}}
        a = bonus.from_exec(ex, UNIT, "2026-09", True, date(2026, 9, 1), date(2026, 9, 15), 400)
        self.assertEqual(a["sales_budget"]["target"], 475.0)          # 15 of 30 days
        self.assertEqual(a["sales_ly"]["target"], 400)

    def test_workforce_ops_feedback(self):
        wf = {"locations": [{"ref": "OPA_B", "trend": [["2026-08", 20], ["2026-09", 22]], "leavers": [["2026-09", 2]]}]}
        self.assertEqual(bonus.from_workforce(wf, UNIT, "2026-09")["tm_turnover"]["value"], round(2 / 21 * 100, 2))
        ops = bonus.from_ops({"completion": 93.0, "tasks": {"due": 100, "done": 93, "late": 4}, "temp_n": 90, "temp_out": 5,
                              "oil_n": 10, "oil_over": 0, "trace_n": 0, "trace_unfit": 0})
        self.assertEqual(ops["ops_reporting"]["value"], 93.0)
        self.assertEqual(ops["food_safety"]["value"], 95.0)
        fb = bonus.from_feedback([{"overall": 5}, {"overall": 4}, {"overall": None}])
        self.assertEqual((fb["value"], fb["count"]), (4.5, 2))

    def test_reviews_combined_by_their_number(self):
        ch = {"google": {"value": 4.8, "count": 30, "approximate": False}, "wolt": {"value": 4.0, "count": 10, "approximate": False},
              "sevenrooms": {"value": 3.0, "count": 5}}
        r = bonus.combine_reviews(ch, ("google", "wolt"))
        self.assertEqual((r["value"], r["count"], r["approximate"]), (4.6, 40, False))
        self.assertNotIn("sevenrooms", r["channels"])
        # Google Places only: the overall rating, marked approximate
        r = bonus.combine_reviews({"google": {"value": 4.7, "count": 0, "approximate": True}})
        self.assertEqual((r["value"], r["count"], r["approximate"]), (4.7, 0, True))
        self.assertIsNone(bonus.combine_reviews({}, ("google",)))


class Flow(Base):
    def card(self, ym="2026-09"):
        auto = {"OPA_B": bonus.from_exec(EX, UNIT, ym)}
        return bonus.scorecard("SALT", ym, [UNIT, FRANCHISE], auto, {"BC": "ok"})

    def test_scorecard_lock_and_year(self):
        c = self.card()
        u = next(x for x in c["units"] if x["ref"] == "OPA_B")
        self.assertIsNotNone(u["score"])
        f = next(x for x in c["units"] if x["ref"] == "OPA_SK")
        self.assertFalse(f["eligible"])
        bonus.set_manual("SALT", "OPA_B", "2026-09", "team_trained", 95, None, "HR list", "ceo")
        u = next(x for x in self.card()["units"] if x["ref"] == "OPA_B")
        self.assertEqual(next(k for k in u["kpis"] if k["key"] == "team_trained")["value"], 95.0)
        bonus.lock("SALT", "OPA_B", "2026-09", u, "ceo")
        with self.assertRaisesRegex(ValueError, "locked"):
            bonus.set_manual("SALT", "OPA_B", "2026-09", "reviews", 4, None, "", "ceo")
        # the source changes after the lock: the locked month does not
        ex2 = {**EX, "actual": {"OPA1": {"2026-09": {"700": 1000.0, "601": 400.0, "641": 300.0}}}}
        c2 = bonus.scorecard("SALT", "2026-09", [UNIT], {"OPA_B": bonus.from_exec(ex2, UNIT, "2026-09")}, {})
        self.assertEqual(c2["units"][0]["score"], u["score"])
        self.assertTrue(c2["units"][0]["locked"])
        bonus.add_super("SALT", "OPA_B", "2026-Q3", 50000, "Best quarter of the brand", "ceo")
        with self.assertRaises(ValueError):
            bonus.add_super("SALT", "OPA_B", "2026-09", 50000, "Best quarter", "ceo")
        y = bonus.year_view("SALT", 2026, [UNIT, FRANCHISE])
        b = next(x for x in y["units"] if x["ref"] == "OPA_B")
        q3 = b["quarters"][2]
        self.assertEqual((q3["months"], q3["locked"], q3["super"]), (1, 1, 50000))
        self.assertEqual(q3["earned"], u["amount"])
        self.assertEqual(q3["paid"], u["amount"] / 2)
        self.assertEqual(b["year_end"], u["amount"] / 2)
        self.assertEqual(b["total"], u["amount"] + 50000)
        bonus.unlock("SALT", "OPA_B", "2026-09", "ceo", "correction")
        self.assertFalse(bonus.scorecard("SALT", "2026-09", [UNIT], {}, {})["units"][0]["locked"])


if __name__ == "__main__":
    unittest.main()
