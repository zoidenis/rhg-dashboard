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
        self.assertEqual((it["AMB1"]["kat2"], it["AMB1"]["from"]), ("AMBALAZH", "bc"))  # not in the map: BC's group
        pm = r["per_month"]["2026-09"]
        cats = {x["kat2"]: x for x in pm["rows"]}
        self.assertTrue(cats["AMBALAZH"]["from_bc"])
        self.assertEqual((cats["AMBALAZH"]["waste_pct"], cats["AMBALAZH"]["unjust"]), (0, 5000))
        self.assertAlmostEqual(pm["totals"]["net"], -22053.47 - 5995 - 5000, 2)
        self.assertEqual(cats["Peshk"]["target"], "Investigate")            # 22,053 lost on 7,800 of outs
        self.assertAlmostEqual(cats["Peshk"]["unjust"], 22053.47 - 0.55 * 7800, 2)
        self.assertAlmostEqual(cats["Birre"]["kmsh"], 39240 + 27432 + 8720, 2)
        self.assertEqual(cats["Birre"]["target"], "Investigate")
        self.assertFalse(r["months"][0]["counted"])
        self.assertEqual(r["months"][1]["qty_off"], 0)

    def test_count_form_before_it_is_posted(self):
        c = Client()
        c.rows = [x for x in Client.rows if not str(x["Document_No"]).startswith("IF")]       # the count not posted
        form = {"file": "count.xlsx", "rows": [{"item": "A508", "counted": 0.0}, {"item": "A1232", "counted": 830.0}],
                "counted_lines": 2}
        with patch.object(shrinkage.stockcount, "get", side_effect=lambda co, loc, m: form if m == "2026-09" else None):
            r = shrinkage.build(c, "SALT", "SIN", 2026)
        m = r["months"][1]
        self.assertEqual((m["source"], m["not_counted"]), ("form", 1))                # AMB1: stock, not on the form
        it = {x["no"]: x for x in r["items"]}
        self.assertAlmostEqual(it["A508"]["m"]["2026-09"][5], -34.81, 2)              # 0 − (47.2 − 12.39)
        self.assertAlmostEqual(it["A508"]["m"]["2026-09"][6], -34.81 * (7800 / 12.39), 1)   # at the cost of its outs
        self.assertAlmostEqual(it["A1232"]["m"]["2026-09"][5], 830 - (896 - 830), 2)  # 896 opening − 830 out → 66; +764
        self.assertEqual(it["AMB1"]["not_counted"], ["2026-09"])

    def test_exception_and_status(self):
        shrinkage.save_config("SALT", {"overrides": {"A1232": "Soft Drinks"}}, "geri")
        r = shrinkage.build(Client(), "SALT", "SIN", 2026)
        self.assertEqual({x["no"]: x["kat2"] for x in r["items"]}["A1232"], "Soft Drinks")
        with self.assertRaises(ValueError):
            shrinkage.save_config("SALT", {"status": "Approved"}, "geri")
        shrinkage.save_config("SALT", {"status": "Reconciled"}, "geri", "matches the Excel May-Sep")
        self.assertEqual(shrinkage.get_config()["status"], "Reconciled")

    def test_waste_per_item(self):
        """A month with % Waste per item: justified / unjustified item by item; not on the file = 0%."""
        import io
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.append(["Kompania", "Location Code", "Segmenti", "Item No.", "Description", "UoM", "Kategoria waste",
                   "Waste % i aplikuar"])
        ws.append(["SALT", "SIN", "Restorant", "A508", "Kallamare", "KG", "PESHK", 0.55])
        ws.append(["SALT", "SIN", "Restorant", "A1232", "Corona", "COPE", "BIRRE", 0.015])
        for row in ws.iter_rows(min_row=2):
            row[7].number_format = "0.00%"
        buf = io.BytesIO()
        wb.save(buf)
        parsed = shrinkage.parse_waste(buf.getvalue())
        self.assertEqual([(r["item"], r["pct"]) for r in parsed["rows"]], [("A508", 55.0), ("A1232", 1.5)])
        self.assertEqual(parsed["locations"], ["SIN"])
        with self.assertRaises(ValueError):
            shrinkage.save_waste("SALT", "LIFT", "2026-09", parsed, "w.xlsx", "geri")       # the file is for SIN
        shrinkage.save_waste("SALT", "SIN", "2026-09", parsed, "w.xlsx", "geri")
        r = shrinkage.build(Client(), "SALT", "SIN", 2026)
        self.assertEqual(r["months"][1]["waste"]["items"], 2)
        cats = {x["kat2"]: x for x in r["per_month"]["2026-09"]["item"]["rows"]}
        self.assertEqual(cats["Peshk"]["waste_mode"], "item")
        cat_basis = {x["kat2"]: x for x in r["per_month"]["2026-09"]["rows"]}          # the other basis, as before
        self.assertEqual(cat_basis["Peshk"]["waste_mode"], "category")
        self.assertAlmostEqual(cat_basis["Peshk"]["unjust"], 22053.47 - 0.55 * 7800, 2)
        self.assertIsNone(r["per_month"]["2026-08"]["item"])
        self.assertEqual(r["ytd"]["item"]["months"], ["2026-09"])
        # the % Waste applies to the difference (physical count against BC): 55% of 22,053 is justified
        self.assertAlmostEqual(cats["Peshk"]["justified"], 0.55 * 22053.47, 2)
        self.assertAlmostEqual(cats["Peshk"]["unjust"], 0.45 * 22053.47, 2)
        self.assertAlmostEqual(cats["Peshk"]["waste_pct"], 55, 2)
        self.assertAlmostEqual(cats["Birre"]["justified"], 0.015 * 5995, 2)
        self.assertAlmostEqual(cats["Birre"]["unjust"], 0.985 * 5995, 1)
        self.assertEqual(shrinkage.item_allowed(50, 10000), 5000)                         # Geri's example
        self.assertIsNone(cats["Birre"]["unjust_excel"])
        self.assertEqual((cats["AMBALAZH"]["justified"], cats["AMBALAZH"]["unjust"]), (0, 5000))   # not on the file
        it = {x["no"]: x for x in r["items"]}
        self.assertEqual((it["AMB1"]["w"], it["AMB1"]["w_missing"]), ({"2026-09": 0.0}, ["2026-09"]))
        self.assertEqual(it["A508"]["wcat"], "PESHK")
        t = r["per_month"]["2026-09"]["item"]["totals"]
        self.assertAlmostEqual(t["justified"] + t["unjust"], 22053.47 + 5995 + 5000, 1)
        self.assertEqual(t["waste_mode"], "item")
        self.assertTrue(shrinkage.render_xlsx(r).startswith(b"PK"))
        # 100%: the whole difference is justified, even with no outs (AMB1) or small ones (ice at SIN)
        parsed["rows"].append({"item": "AMB1", "pct": 100.0, "desc": "Gote", "unit": "COPE", "wcat": "KONSUM"})
        shrinkage.save_waste("SALT", "SIN", "2026-09", parsed, "w.xlsx", "geri")
        r = shrinkage.build(Client(), "SALT", "SIN", 2026)
        amb = {x["kat2"]: x for x in r["per_month"]["2026-09"]["item"]["rows"]}["AMBALAZH"]
        self.assertEqual((amb["justified"], amb["unjust"], amb["target"]), (5000, 0, "OK"))
        self.assertEqual(shrinkage.allowed_waste(100, 58, 41734), 41734)
        self.assertEqual(shrinkage.allowed_waste(55, 7800, 22053.47), 0.55 * 7800)
        self.assertTrue(shrinkage.delete_waste("SALT", "SIN", "2026-09", "geri"))
        r = shrinkage.build(Client(), "SALT", "SIN", 2026)
        self.assertIsNone(r["per_month"]["2026-09"]["item"])

    def test_waste_file_of_sin(self):
        f = Path(__file__).parent / "fixtures" / "SIN_WASTE_TOLERANCE.xlsx"
        if not f.exists():
            self.skipTest("no sample file")
        p = shrinkage.parse_waste(f.read_bytes())
        pct = {r["item"]: r["pct"] for r in p["rows"]}
        self.assertEqual((len(p["rows"]), p["locations"]), (470, ["SIN"]))
        self.assertEqual((pct["A508"] if "A508" in pct else None, pct["A10245"], pct["A8510"], pct["A10745"]),
                         (pct.get("A508"), 100.0, 55.0, 0.05))
        self.assertIn("201597", pct)                                   # a numeric item number read as text


if __name__ == "__main__":
    unittest.main()
