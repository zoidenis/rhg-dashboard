"""A screen's table as Excel: numbers stay numbers, text stays text (never a formula), sizes are bounded."""
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import openpyxl

import xlsxtable


class XlsxTable(unittest.TestCase):
    def test_numbers_text_and_notes(self):
        b = xlsxtable.render([{"title": "By category: Sep/2026", "header": ["Category", "Net", "Ratio"],
                               "formats": ["text", "num", "pct"],
                               "rows": [["=HYPERLINK(\"http://x\")", -523237.93, 0.216], ["LP", "12", None]]}],
                             ["Shrinkage KPIs · SIN"])
        wb = openpyxl.load_workbook(io.BytesIO(b))
        ws = wb.worksheets[0]
        self.assertEqual(ws.title, "By category  Sep 2026")
        self.assertEqual(ws["A2"].data_type, "s")
        self.assertEqual(ws["A2"].value, "=HYPERLINK(\"http://x\")")
        self.assertEqual(ws["B2"].value, -523237.93)
        self.assertEqual(ws["B3"].value, 12.0)
        self.assertEqual(ws["C2"].number_format, "0.0%;[Red]-0.0%")
        self.assertIsNone(ws["C3"].value)
        self.assertEqual(wb["Read me"]["A1"].value, "Shrinkage KPIs · SIN")

    def test_refusals_and_name(self):
        with self.assertRaises(ValueError):
            xlsxtable.render([])
        with self.assertRaises(ValueError):
            xlsxtable.render([{"title": "x", "header": ["a"] * 10, "rows": [[1] * 10] * 70000}])
        self.assertEqual(xlsxtable.filename('RHG SALT/SIN "x"'), "RHG_SALTSIN_x.xlsx")


if __name__ == "__main__":
    unittest.main()
