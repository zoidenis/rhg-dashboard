"""Sales blocked by stock: short now, flagged but covered, at risk from today's Simphony sales; never contacts BC or Simphony."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tempfile
from datetime import datetime

import arap_notify
import locintel
import recipes
import recipients
import salestop
import salestop_report
import simphony

HEADS = [{"No": "A1", "Item_No": "10001", "Description": "Gyros pita", "Due_Date": "2026-11-09", "Location_Code": "OPA1", "Quantity": 40, "Remaining_Quantity": 40},
         {"No": "A2", "Item_No": "10002", "Description": "Souvlaki", "Due_Date": "2026-11-09", "Location_Code": "OPA1", "Quantity": 25, "Remaining_Quantity": 25},
         {"No": "A3", "Item_No": "10003", "Description": "Sushi roll", "Due_Date": "2026-11-08", "Location_Code": "SALT", "Quantity": 10, "Remaining_Quantity": 10},
         {"No": "A4", "Item_No": "10001", "Description": "Gyros pita", "Due_Date": "2026-11-10", "Location_Code": "OPA1", "Quantity": 5, "Remaining_Quantity": 5}]


def line(doc, warn, item, desc, loc, uom, rem, cost):
    return {"Document_No": doc, "Avail_Warning": warn, "Type": "Item", "No": item, "Description": desc, "Location_Code": loc,
            "Unit_of_Measure_Code": uom, "Qty_per_Unit_of_Measure": 1, "Remaining_Quantity": rem, "Cost_Amount": cost}


LINES = [line("A1", True, "C1", "Mish pule", "OPA1", "KG", 6, 3000), line("A1", False, "C2", "Pita", "OPA1", "COPE", 40, 800),
         line("A2", True, "C1", "Mish pule", "OPA1", "KG", 4, 2000), line("A3", True, "C3", "Gjethe bambu", "SALT", "COPE", 20, 100),
         line("A4", False, "C2", "Pita", "OPA1", "COPE", 5, 100)]
STOCK = {"OPA1": {"C1": 3.5, "C2": 60}, "SALT": {"C3": 20}}
MASTER = {"items": {"C1": {"Description": "Mish pule", "Base_Unit_of_Measure": "KG"}, "C2": {"Description": "Pita", "Base_Unit_of_Measure": "COPE"},
                    "C3": {"Description": "Gjethe bambu", "Base_Unit_of_Measure": "COPE"}},
          "bom": [["10001", 1, "Item", "C1", "Mish pule", False, 150, "GR"], ["10001", 2, "Item", "C2", "Pita", False, 1, "COPE"],
                  ["10002", 1, "Item", "C1", "", False, 0.16, "KG"]],
          "uom": [["C1", "GR", 0.001]]}


class Client:
    def _url(self, company, entity):
        return entity

    def _get_all(self, url, params=None, page_size=None, progress=None, **k):
        return {"AssemblyOrders": HEADS, "AssemblyLinesOpen": LINES}.get(url, [])

    def stock_by_location(self, company, loc, as_of, max_age=900):
        return STOCK.get(loc, {})


def call(api, body=None, _retry=True):
    if api == "getMenuItemDailyTotals" and body["locRef"] == "R1":
        return {"revenueCenters": [{"menuItems": [{"miNum": 10001, "slsCnt": 35}, {"miNum": 10002, "slsCnt": 10}, {"miNum": 99, "slsCnt": 3}]}]}
    return {}


class SaleStop(unittest.TestCase):
    def build(self):
        with patch.object(recipes, "master", lambda client, company, force=False: MASTER), \
             patch.object(locintel, "registry", lambda settings=None: [{"ref": "R1", "name": "OPA Blloku", "company": "SALT", "bc": "OPA1"},
                                                                      {"ref": "R2", "name": "SALT Tirana", "company": "SALT", "bc": "SALT"}]), \
             patch.object(simphony, "info", lambda: {"configured": True}), \
             patch.object(simphony, "latest_business_date", lambda ref: "2026-11-10"), \
             patch.object(simphony, "menu_items", lambda ref, max_age=0: {10001: {"name": "Gyros pita"}, 10002: {"name": "Souvlaki"}}), \
             patch.object(simphony, "menu_item_prices", lambda ref, max_age=0: [{"num": 10001, "level": 1, "price": 450},
                                                                                 {"num": 10002, "level": 1, "price": 600},
                                                                                 {"num": 10003, "level": 1, "price": 1200}]), \
             patch.object(simphony, "call", call):
            return salestop.build(Client(), "SALT")

    def test_short_covered_and_risk(self):
        r = self.build()
        now = {x["item"]: x for x in r["now"]}
        self.assertEqual(now["C1"]["status"], "short")
        self.assertAlmostEqual(now["C1"]["short"], 6.5)
        self.assertEqual(now["C3"]["status"], "covered")      # BC still flags it; the stock is there
        self.assertNotIn("C2", now)
        self.assertEqual(r["totals"]["components"], 1)
        self.assertEqual(r["totals"]["covered"], 1)
        self.assertAlmostEqual(r["totals"]["sales"], 40 * 450 + 25 * 600)
        risk = {x["item"]: x for x in r["risk"]}
        self.assertAlmostEqual(risk["C2"]["after"], -15)       # 60 − 45 open − 30 today (35 sold − 5 imported)
        self.assertAlmostEqual(risk["C1"]["after"], -12.6)     # 3.5 − 10 − (30 × 0.15 + 10 × 0.16)
        self.assertTrue(risk["C1"]["blocked_now"])

    def test_cut_by_location(self):
        r = salestop.cut(self.build(), {"SALT": ["SALT"]})
        self.assertEqual([x["item"] for x in r["now"]], ["C3"])
        self.assertEqual(r["risk"], [])
        self.assertEqual(r["totals"]["components"], 0)


class DailyEmail(unittest.TestCase):
    """Sales blocked by stock · daily: the time, once a day, one email per company and one per location."""
    S = {"enabled": True, "to": "", "cc": "", "time": "09:30", "days": "daily", "companies": "SALT", "email_lines": "50"}
    D = {"people": {"SALT": {"cost_controller": "cost@rhg.al", "operations": "ops@rhg.al", "stock_manager": "stock@rhg.al"}},
         "locations": {"SALT": {"OPA1": {"unit_manager": "opa@rhg.al", "head_chef": "chef@rhg.al"}}},
         "routing": {"stockstop_daily": {"to": ["cost_controller", "operations"], "cc": ["stock_manager", "unit_manager"]}}}

    def setUp(self):
        self.sent = []
        patches = [patch.object(salestop_report, "STATE_FILE", Path(tempfile.mkdtemp()) / "s.json"),
                   patch.object(salestop_report.Reporter, "settings", staticmethod(lambda: dict(self.S))),
                   patch.object(recipients, "directory", lambda max_age=60: self.D),
                   patch.object(salestop_report.store, "audit", lambda *a, **k: None),
                   patch.object(arap_notify, "send_mail", lambda to, cc, subj, body, text, att=None, **k:
                                self.sent.append((to, cc, subj, att)))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.rep = salestop_report.Reporter(Client(), lambda c: "ALL", "SALT")
        self.rep.build = lambda company: dict(SaleStop().build(), currency="ALL", label=company)

    def test_due_at_the_time_once_a_day(self):
        self.assertIsNone(self.rep.due(datetime(2026, 11, 11, 9, 29)))
        self.assertEqual(self.rep.due(datetime(2026, 11, 11, 9, 31)), "2026-11-11")
        self.rep.run("2026-11-11")
        self.assertIsNone(self.rep.due(datetime(2026, 11, 11, 9, 45)))
        self.assertEqual(self.rep.next_run(datetime(2026, 11, 11, 10, 0)), "2026-11-12T09:30")

    def test_company_and_location_emails_with_excel(self):
        res = self.rep.run("2026-11-11")
        self.assertEqual([x["status"] for x in res], ["sent", "sent"])
        (to1, cc1, subj1, att1), (to2, cc2, subj2, att2) = self.sent
        self.assertEqual((to1, cc1), (["cost@rhg.al", "ops@rhg.al"], ["stock@rhg.al"]))
        self.assertEqual((to2, cc2), (["opa@rhg.al"], []))
        self.assertIn("1 component short", subj1)
        self.assertIn("SALT · OPA1", subj2)
        self.assertTrue(att1[0][0].endswith(".xlsx") and att1[0][2][:2] == b"PK")
        import io
        import openpyxl
        ws = openpyxl.load_workbook(io.BytesIO(att2[0][2]))["Blocked now"]
        self.assertEqual({r[0] for r in ws.iter_rows(min_row=2, values_only=True)}, {"OPA1"})   # its location only


if __name__ == "__main__":
    unittest.main()
