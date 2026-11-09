"""Negotiations: the baseline, the cases with no calculation and the scenarios; never contacts BC."""
import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import negotiations


class FakeClient:
    """Price lines as bc_client.price_lines gives them (already excluding VAT, in the line's unit)."""
    def __init__(self, plines, zero=()):
        self.plines, self.zero = plines, list(zero)

    def price_lines(self, company):
        return self.plines, self.zero, {"available": True}

    def vendor_cards(self, company):
        return [{"No": "V1", "Purchaser_Code": "P1"}, {"No": "V2", "Purchaser_Code": "P2"}]

    def item_catalog(self, company):
        return {"I1": {"desc": "Item one", "g3": "USHQIME", "g4": "MISH", "uom": "KG"},
                "I2": {"desc": "Item two", "g3": "USHQIME", "g4": "", "uom": "KG"},
                "I3": {"desc": "Item three", "g3": "PIJE", "g4": "SOFT", "uom": "COPE"}}


def line(item, vendor, qty, amount, kind="inv", cur="", uom="KG", base=None, ok=1, day="2026-10-10", loc="OPA1"):
    return ("SALT", kind, "D" + item + vendor + kind + str(qty), day, vendor, cur, loc, loc, item, "", uom, qty,
            qty if base is None else base, amount, ok)


def pl(item, vendor, price, uom="KG", min_qty=0, cur="", start="2026-09-01", end="0001-01-01"):
    return {"item": item, "vendor": vendor, "price": price, "price_as_listed": price * 1.2, "vat_pct": 20.0,
            "uom": uom, "min_qty": min_qty, "currency": cur, "variant": "", "list": "P00001", "starting": start,
            "ending": end, "line_disc": 0}


class Negotiations(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(negotiations, "DB_PATH", Path(self.tmp.name) / "n.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        negotiations._conn = None
        self.addCleanup(lambda: setattr(negotiations, "_conn", None))
        for p in (patch.object(negotiations, "registry", return_value=([], "")),
                  patch.object(negotiations.savings, "_uoms", return_value={("I1", "KG"): 1.0, ("I3", "COPE"): 1.0,
                                                                            ("I3", "CASE"): 24.0}),
                  patch.object(negotiations.store, "get_settings", return_value={
                      "thresholds": {"price_above_contract_pct": 2, "contract_expiry_days": 30},
                      "classification": {}})):
            p.start()
            self.addCleanup(p.stop)

    def put(self, rows):
        db = negotiations._db()
        db.executemany("INSERT INTO lines VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        db.commit()

    def view(self, plines, zero=(), **f):
        f.setdefault("_company_currency", "ALL")
        return {(r["item"], r["vendor"], r["currency"]): r
                for r in negotiations.build(FakeClient(plines, zero), "SALT", date(2026, 10, 1), date(2026, 10, 31),
                                            f, today=date(2026, 11, 9))["rows"]}

    def test_contract_credit_memo_and_above_contract(self):
        self.put([line("I1", "V1", 100, 45000), line("I1", "V1", 10, 4500, kind="cr")])
        r = self.view([pl("I1", "V1", 416.6667)])[("I1", "V1", "ALL")]
        self.assertEqual(r["source"], "contract")
        self.assertAlmostEqual(r["qty"], 90)
        self.assertAlmostEqual(r["avg"], 450)
        self.assertIn("above_contract", r["flags"])
        self.assertEqual(r["sub"], "MISH")

    def test_no_contract_weighted_average(self):
        self.put([line("I2", "V1", 10, 1000), line("I2", "V1", 30, 3600)])
        r = self.view([])[("I2", "V1", "ALL")]
        self.assertEqual(r["source"], "average")
        self.assertAlmostEqual(r["baseline"], 115)
        self.assertEqual(r["sub"], negotiations.NO_SUB)

    def test_two_prices_are_ambiguous_one_price_twice_is_not(self):
        self.put([line("I1", "V1", 10, 1000), line("I1", "V2", 10, 1000)])
        rows = self.view([pl("I1", "V1", 90), pl("I1", "V1", 95, start="2026-10-01"),
                          pl("I1", "V2", 99), pl("I1", "V2", 99, start="2026-10-01")])
        self.assertEqual(rows[("I1", "V1", "ALL")]["source"], "ambiguous")
        self.assertIsNone(rows[("I1", "V1", "ALL")]["baseline"])
        self.assertIn("Ambiguous", rows[("I1", "V1", "ALL")]["reason"])
        self.assertEqual(rows[("I1", "V2", "ALL")]["source"], "contract")

    def test_zero_quantity_has_no_baseline(self):
        self.put([line("I2", "V1", 10, 1000), line("I2", "V1", 10, 1000, kind="cr")])
        r = self.view([])[("I2", "V1", "ALL")]
        self.assertIsNone(r["baseline"])
        self.assertEqual(r["reason"], "No net purchase in the period")

    def test_units_and_currencies_kept_apart(self):
        self.put([line("I3", "V1", 2, 2400, uom="CASE", base=48), line("I3", "V1", 5, 60, uom="BOX", base=None, ok=0),
                  line("I3", "V1", 10, 8, cur="EUR", uom="COPE")])
        rows = self.view([pl("I3", "V1", 1200, uom="CASE")])
        alll = rows[("I3", "V1", "ALL")]
        self.assertAlmostEqual(alll["qty"], 48)
        self.assertAlmostEqual(alll["baseline"], 50)          # 1,200 per case of 24
        self.assertIn("unit_missing", alll["flags"])
        self.assertEqual(rows[("I3", "V1", "EUR")]["source"], "average")   # the ALL contract is not an EUR price

    def test_minimum_quantity_per_line(self):
        self.put([line("I1", "V1", 5, 500)])
        r = self.view([pl("I1", "V1", 90, min_qty=10)])[("I1", "V1", "ALL")]
        self.assertEqual(r["source"], "average")
        self.assertIn("below_min", r["flags"])

    def test_filters_and_period(self):
        self.put([line("I2", "V1", 10, 1000, loc="OPA1"), line("I2", "V1", 10, 2000, loc="SALT"),
                  line("I2", "V1", 10, 3000, day="2026-09-30")])
        self.assertAlmostEqual(self.view([], location="SALT")[("I2", "V1", "ALL")]["avg"], 200)
        self.assertAlmostEqual(self.view([])[("I2", "V1", "ALL")]["qty"], 20)

    def test_scenario_prices_and_audit_persist(self):
        sid = negotiations.save_scenario("SALT", {"name": "RHG target", "status": "Draft"}, "geri")
        negotiations.set_price("SALT", {"scenario": sid, "item": "I1", "vendor": "V1", "currency": "ALL", "price": 400}, "geri")
        negotiations.set_price("SALT", {"scenario": sid, "item": "I1", "vendor": "V1", "currency": "ALL", "price": 405}, "geri")
        with self.assertRaises(ValueError):
            negotiations.set_price("SALT", {"scenario": sid, "item": "I1", "vendor": "V1", "currency": "ALL", "price": -1}, "geri")
        with self.assertRaises(ValueError):
            negotiations.set_price("TBM", {"scenario": sid, "item": "I1", "vendor": "V1", "currency": "ALL", "price": 1}, "geri")
        negotiations._conn = None                       # reopened from disk
        sc = negotiations.scenarios("SALT")[0]
        self.assertEqual(sc["prices"][0]["price"], 405)
        changes = [(a["field"], a["old"], a["new"]) for a in negotiations.audit_of("SALT", sid)]
        self.assertIn(("proposed price", "400.0", "405.0"), changes)
        self.assertIn(("proposed price", None, "400.0"), changes)


if __name__ == "__main__":
    unittest.main()
