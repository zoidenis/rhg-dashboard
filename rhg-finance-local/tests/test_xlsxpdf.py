"""Export → PDF: an export's workbook laid out as a PDF; the numbers as Excel shows them."""
import io
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import xlsxpdf
import xlsxtable


class XlsxPdf(unittest.TestCase):
    def test_numbers_as_excel_shows_them(self):
        self.assertEqual(xlsxpdf.show(-523237.93, "#,##0;[Red]-#,##0"), ("-523,238", True, True))
        self.assertEqual(xlsxpdf.show(0.216, "0.0%;[Red]-0.0%"), ("21.6%", True, False))
        self.assertEqual(xlsxpdf.show(6.5, "#,##0.00"), ("6.50", True, False))
        self.assertEqual(xlsxpdf.show(12345, "General"), ("12,345", True, False))
        self.assertEqual(xlsxpdf.show(date(2026, 11, 10), "General")[0], "2026-11-10")
        self.assertEqual(xlsxpdf.text("need − on hand → short ≥ 0, ë ç"), "need - on hand -> short >= 0, ë ç")
        self.assertEqual(xlsxpdf.pdf_name("RHG_SALT_shrinkage.xlsx"), "RHG_SALT_shrinkage.pdf")

    def test_a_workbook_becomes_a_pdf(self):
        rows = [[f"A{i}", "Peshk − Kallamare →", -1000.5 * i, 0.05 * i] for i in range(1, 400)]
        wb = xlsxtable.render([{"title": "Items", "header": ["Item", "Description", "Value", "Ratio"],
                                "formats": ["text", "text", "num", "pct"], "rows": rows}],
                              ["Shrinkage KPIs · SIN · Sep 2026", "Difference = counted − theoretical."])
        pdf = xlsxpdf.render(wb, "RHG SALT SIN shrinkage by item")
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertGreater(pdf.count(b"/Type /Page\n") + pdf.count(b"/Type /Page "), 0)

    def test_long_sheet_is_cut(self):
        old = xlsxpdf.MAX_ROWS
        xlsxpdf.MAX_ROWS = 50
        try:
            wb = xlsxtable.render([{"title": "T", "header": ["a", "b"], "formats": ["text", "num"],
                                    "rows": [[str(i), i] for i in range(200)]}])
            self.assertTrue(xlsxpdf.render(wb, "t").startswith(b"%PDF"))
        finally:
            xlsxpdf.MAX_ROWS = old


if __name__ == "__main__":
    unittest.main()
