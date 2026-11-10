"""Export → Send by email: which exports can go, the addresses, the email."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import exportmail


class ExportMail(unittest.TestCase):
    def test_only_an_export_of_this_tower(self):
        self.assertEqual(exportmail.clean_url("/finance/api/shrinkage/xlsx?company=SALT&location=SIN&format=pdf"),
                         "/api/shrinkage/xlsx?company=SALT&location=SIN")
        self.assertEqual(exportmail.clean_url("/api/savings/xlsx"), "/api/savings/xlsx")
        for bad in ("/api/settings", "https://evil.example/x/xlsx", "/api/../internal/xlsx", "", "/api/users?x=/xlsx"):
            with self.assertRaises(ValueError):
                exportmail.clean_url(bad)

    def test_addresses(self):
        self.assertEqual(exportmail.check({"to": "a@rhg.al; b@rhg.al a@rhg.al", "format": "pdf"}),
                         (["a@rhg.al", "b@rhg.al"], "pdf"))
        self.assertEqual(exportmail.check({"to": "a@rhg.al"})[1], "xlsx")
        for bad in ({"to": ""}, {"to": "nope"}, {"to": ",".join(f"p{i}@rhg.al" for i in range(11))}):
            with self.assertRaises(ValueError):
                exportmail.check(bad)

    def test_the_email_escapes_the_message(self):
        subj, html, text = exportmail.compose({"name": "Geri Kuka", "email": "g@rhg.al"}, "RHG SALT SIN by item",
                                              "x.pdf", "pdf", "<script>alert(1)</script>", "Finance", "Shrinkage KPIs")
        self.assertEqual(subj, "[RHG Finance] RHG SALT SIN by item")
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("as a PDF", text)
        self.assertIn("(Shrinkage KPIs)", text)


if __name__ == "__main__":
    unittest.main()
