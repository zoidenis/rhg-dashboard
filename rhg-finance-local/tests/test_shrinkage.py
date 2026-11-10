"""Shrinkage KPIs: the count difference, the ins and outs, the categories and the KPIs; never contacts BC."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import shrinkage


def e(item, et, q, v, doc="D1"):
    return {"Entry_Type": et, "Item_No": item, "Document_No": doc, "Quantity": q, "Cost_Amount_Actual": v,
            "Cost_Amount_Expected": 0}


class Client:
    """A508 (fish, KG) and A1232 (beer, COPE) as BC holds them at SIN in September 2026; AMB1 = packaging."""
    rows = [e("A508", "Assembly_x0020_Consumption", -7.59, -4800), e("A508", "Transfer", -4.8, -3000, "124202"),
            e("A508", "Negative_x0020_Adjmt_x002E_", -34.81, -22053.47, "IF 01.10.2026"),
            e("A1232", "Transfer", -432, -39240, "124202"), e("A1232", "Assembly_x0020_Consumption", -302, -27432),
            e("A1232", "Negative_x0020_Adjmt_x002E_", -96, -8720, "KONSUM STAF"),
            e("A1232", "Negative_x0020_Adjmt_x002E_", -66, -5995, "IF 01.10.2026"),
            e("A1232", "Negative_x0020_Adjmt_x002E_", 200, 1000, "SHITJE DATE 01-30.09"),
            e("A1232", "Negative_x0020_Adjmt_x002E_", -200, -1000, "DALJE E HYRJEVE"),
            e("AMB1", "Negative_x0020_Adjmt_x002E_", -100, -5000, "IFAMB 01/10/2026")]
    cards = {"A508": {"No": "A508", "Description": "Kallamare", "Base_Unit_of_Measure": "KG",
                      "Item_Category_Group_4": "PESHK&PROD.DETI", "Item_Category_Group_5": "BUTAK"},
             "A1232": {"No": "A1232", "Description": "Corona", "Base_Unit_of_Measure": "COPE",
                       "Item_Category_Group_4": "BIRRE", "Item_Category_Group_5": "LAG&PIL"},
             "AMB1": {"No": "AMB1", "Description": "Gote", "Base_Unit_of_Measure": "COPE",
                      "Item_Category_Group_4": "AMBALAZH"}}

    def _url(self, company, entity):
        return entity

    def _get_all(self, url, params=None, page_size=None, progress=None, **k):
        if url == "PbItems":
            return list(self.cards.values())
        return self.rows if "2026-09-01" in (params or {}).get("$filter", "") else []

    def stock_by_location(self, company, loc, as_of, max_age=900):
        return {"2026-08-31": {"A508": 47.2, "A1232": 896, "AMB1": 100}}.get(as_of, {})


class Shrinkage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for p in (patch.object(shrinkage, "DIR", Path(self.tmp.name) / "m"),
                  patch.object(shrinkage, "CFG_FILE", Path(self.tmp.name) / "c.json"),
                  patch.object(shrinkage, "months_of", return_value=["2026-08", "2026-09"])):
            p.start()
            self.addCleanup(p.stop)
        shrinkage._items_cache.clear()

    def test_month_of_sin(self):
        r = shrinkage.build(Client(), "SALT", "SIN", 2026)
        it = {x["no"]: x for x in r["items"]}
        op, iq, iv, oq, ov, cq, cv, cl = it["A508"]["m"]["2026-09"]
        self.assertEqual((op, iq, round(-oq, 2), cq, cl), (47.2, 0, 12.39, -34.81, 0))   # the Excel's September
        self.assertAlmostEqual(-it["A1232"]["m"]["2026-09"][3], 830)                     # 432 + 302 + 96, +200/−200 out
        self.assertEqual(it["AMB1"]["kat2"], shrinkage.OUT_OF_SCOPE)
        pm = r["per_month"]["2026-09"]
        self.assertEqual(pm["out_of_scope"], -5000)
        cats = {x["kat2"]: x for x in pm["rows"]}
        self.assertEqual(cats["Peshk"]["target"], "Investigate")            # 22,053 lost on 7,800 of outs
        self.assertAlmostEqual(cats["Peshk"]["unjust"], 22053.47 - 0.55 * 7800, 2)
        self.assertAlmostEqual(cats["Birre"]["kmsh"], 39240 + 27432 + 8720, 2)
        self.assertEqual(cats["Birre"]["target"], "Investigate")
        self.assertFalse(r["months"][0]["counted"])
        self.assertEqual(r["months"][1]["qty_off"], 0)

    def test_exception_and_status(self):
        shrinkage.save_config("SALT", {"overrides": {"A1232": "Soft Drinks"}}, "geri")
        r = shrinkage.build(Client(), "SALT", "SIN", 2026)
        self.assertEqual({x["no"]: x["kat2"] for x in r["items"]}["A1232"], "Soft Drinks")
        with self.assertRaises(ValueError):
            shrinkage.save_config("SALT", {"status": "Approved"}, "geri")
        shrinkage.save_config("SALT", {"status": "Reconciled"}, "geri", "matches the Excel May-Sep")
        self.assertEqual(shrinkage.get_config()["status"], "Reconciled")


if __name__ == "__main__":
    unittest.main()
