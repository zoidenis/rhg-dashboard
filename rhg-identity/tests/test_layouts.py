"""Layouts of the towers: everyone's, a role's, a person's own; the order they win in; for everyone at once."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
os.environ.setdefault("SERVICE_TOKEN", "test")
import config  # noqa: E402

_tmp = Path(tempfile.mkdtemp())
config.DB_PATH = _tmp / "identity.db"
import store  # noqa: E402
import layouts  # noqa: E402

store.connect()
ADMIN = {"username": "geri", "role": "Administrator", "permissions": ["layout.personal", "layout.roles.manage"]}
ANA = {"username": "ana", "role": "Accountant", "permissions": ["layout.personal"]}
LAY = lambda h: {"order": {}, "hidden": [h], "nav": {"order": [], "hidden": []}}  # noqa: E731


class Everyone(unittest.TestCase):
    def setUp(self):
        store.run("DELETE FROM layouts")

    def test_everyone_and_who_wins(self):
        self.assertEqual(layouts.save(ADMIN, {"app": "finance", "scope": "all", "layout": LAY("a")})[0], 200)
        v = layouts.view(ANA, "finance")
        self.assertEqual(v["all_layout"]["hidden"], ["a"])
        self.assertIsNone(v["role_layout"])
        layouts.save(ADMIN, {"app": "finance", "scope": "role", "role": "Accountant", "layout": LAY("b")})
        layouts.save(ANA, {"app": "finance", "scope": "me", "layout": LAY("c")})
        v = layouts.view(ANA, "finance")
        self.assertEqual((v["all_layout"]["hidden"], v["role_layout"]["hidden"], v["personal"]["hidden"]), (["a"], ["b"], ["c"]))
        self.assertEqual(layouts.view(ADMIN, "finance")["own_roles"], ["Accountant"])
        self.assertEqual(layouts.view(ADMIN, "finance")["own_people"], 1)

    def test_override_removes_the_own_layouts(self):
        layouts.save(ADMIN, {"app": "finance", "scope": "role", "role": "Accountant", "layout": LAY("b")})
        layouts.save(ANA, {"app": "finance", "scope": "me", "layout": LAY("c")})
        layouts.save(ANA, {"app": "purchasing", "scope": "me", "layout": LAY("p")})
        layouts.save(ADMIN, {"app": "finance", "scope": "all", "layout": LAY("a"), "override": True})
        v = layouts.view(ANA, "finance")
        self.assertIsNone(v["role_layout"]); self.assertIsNone(v["personal"])
        self.assertEqual(layouts.view(ANA, "purchasing")["personal"]["hidden"], ["p"])   # the other tower keeps it

    def test_only_who_sets_roles(self):
        self.assertEqual(layouts.save(ANA, {"app": "finance", "scope": "all", "layout": LAY("x")})[0], 403)
        self.assertEqual(layouts.save(ADMIN, {"app": "finance", "scope": "all", "layout": None})[0], 200)


if __name__ == "__main__":
    unittest.main()
