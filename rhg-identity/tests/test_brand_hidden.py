"""brand.css is the same file in every app and makes the hidden attribute win over any page's own display."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent


class Hidden(unittest.TestCase):
    def test_hidden_wins_everywhere(self):
        copies = [ROOT / a / "static" / "brand.css" for a in ("rhg-identity", "rhg-finance-local", "rhg-purchasing-local")]
        texts = {p.read_text(encoding="utf-8") for p in copies if p.exists()}
        self.assertEqual(len(texts), 1, "brand.css differs between the apps")
        self.assertIn("[hidden]{ display: none !important; }", texts.pop())
        self.assertIn("[hidden]{display:none !important}", (ROOT / "rhg-identity" / "static" / "app.css").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
