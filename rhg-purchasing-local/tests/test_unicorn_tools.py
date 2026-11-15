"""Unicorn's read-only tools in Purchasing: answers, pending reads, refusals."""
import unittest

import unicorn_tools as ut

CTX = {"default_company": "SALT", "currency_of": lambda co: "ALL",
       "dashboard": lambda co, week, loc, mode, wait: {"kpis": {"total_cur": 1000.4, "total_pct": 5.0}, "meta": {"period_label": "Nov 2026"},
                                                      "movers": [{"item_no": "1", "desc": "Fileto", "pct": 20.0, "impact": 240000.2}]},
       "supplier_summary": lambda co, week, loc, mode: {"pending": True, "step": "invoices"},
       "nopo": lambda co, a, b: {"available": True, "kpis": {"invoices": 3, "rate": 10}}}


class Tools(unittest.TestCase):
    def test_overview(self):
        r = ut.call(CTX, "purchases_overview", {"date": "2026-11-14"})
        self.assertEqual((r["company"], r["kpis"]["total_cur"], r["price_movers"][0]["impact"]), ("SALT", 1000, 240000))

    def test_pending_and_errors(self):
        self.assertTrue(ut.call(CTX, "supplier_summary", {"date": "2026-11-14"})["pending"])
        self.assertFalse(ut.call(CTX, "purchases_overview", {"period": "decade"})["available"] is not False)
        self.assertIn("no tool", ut.call(CTX, "drop_table", {})["error"])
        self.assertFalse(ut.call(CTX, "invoices_without_po", {"date_from": "2026-01-01", "date_to": "2026-12-31"})["available"])
        r = ut.call(CTX, "invoices_without_po", {"date_from": "2026-11-01", "date_to": "2026-11-14"})
        self.assertEqual(r["kpis"]["invoices"], 3)

    def test_a_failing_read_is_an_error_not_a_crash(self):
        ctx = dict(CTX, dashboard=lambda *a: (_ for _ in ()).throw(RuntimeError("BC timeout")))
        self.assertIn("BC timeout", ut.call(ctx, "purchases_overview", {"date": "2026-11-14"})["error"])


if __name__ == "__main__":
    unittest.main()
