"""Settings (static/settings.html): every link into it names a tab that exists, and every tower card it opens exists."""
import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import os  # noqa: E402
os.environ.setdefault("SERVICE_TOKEN", "test")
import reportcenter  # noqa: E402

PAGE = (HERE / "static" / "settings.html").read_text(encoding="utf-8")
TABS = set(re.findall(r'\{id: "([\w-]+)", label:', PAGE))


class Hub(unittest.TestCase):
    def test_links_name_tabs(self):
        self.assertIn("em-arap", TABS)
        for tab in reportcenter.SETTINGS_TAB.values():
            self.assertIn(tab, TABS)
        js = (HERE / "static" / "reports.js").read_text(encoding="utf-8")
        for tab in re.findall(r'tab: "([\w-]+)"', js):
            self.assertIn(tab, TABS)
        for app, ids in (("rhg-finance-local", ("svSet", "biSet", "oxSet", "ppSet", "epSet")),):
            html = (ROOT / app / "static" / "index.html").read_text(encoding="utf-8")
            for tab in re.findall(r'(conn-[\w-]+)', html):
                self.assertIn(tab, TABS)

    def test_tower_cards_exist(self):
        for tower, app in (("FIN", "rhg-finance-local"), ("PUR", "rhg-purchasing-local")):
            html = (ROOT / app / "static" / "index.html").read_text(encoding="utf-8")
            for card in re.findall(tower + r'\("(\w+)"\)', PAGE):
                self.assertIn(f'id="{card}"', html, f"{card} is not in {app}")


if __name__ == "__main__":
    unittest.main()
