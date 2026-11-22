"""Sales → Bonus management: only the CEO and the Administrator (Geri, 22 Nov 2026)."""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
import permissions
import store

SCREEN = "finance.s.sales.bonus"


class BonusAccess(unittest.TestCase):
    def test_default_roles(self):
        holders = sorted(n for n, r in permissions.DEFAULT_ROLES.items() if SCREEN in r["permissions"])
        self.assertEqual(holders, ["Administrator", "CEO"])

    def test_not_in_bundles_or_legacy_rights(self):
        self.assertNotIn(SCREEN, permissions._s("finance.s"))
        self.assertNotIn(SCREEN, permissions._s("finance.s.sales"))
        self.assertNotIn(SCREEN, permissions.screens_for_rights(["finance.view", "finance.pl.view", "purchasing.view"]))
        self.assertIn(SCREEN, permissions.PERMISSIONS)       # an administrator can still tick it for a role

    def test_stored_roles_get_it_by_name_once(self):
        tmp = Path(tempfile.mkdtemp())
        old, old_conn = config.DB_PATH, store._conn
        try:
            config.DB_PATH = tmp / "identity.db"
            store._conn = None
            store.connect()
            # roles stored before the screen existed
            for name in ("CEO", "COO", "CFO", "Sales & operations director", "Viewer"):
                held = [p for p in json.loads(store.one("SELECT permissions FROM roles WHERE name=?", (name,))["permissions"])
                        if p != SCREEN]
                store.run("UPDATE roles SET permissions=? WHERE name=?", (json.dumps(held), name))
            (tmp / "screens_added.json").write_text(json.dumps([]), encoding="utf-8")
            store._conn.close()
            store._conn = None
            store.connect()
            got = {r["name"] for r in store.q("SELECT name, permissions FROM roles") if SCREEN in json.loads(r["permissions"])}
            self.assertEqual(got, {"Administrator", "CEO"})
            # taken from the CEO later: it stays taken
            held = [p for p in store.role_permissions("CEO") if p != SCREEN]
            store.run("UPDATE roles SET permissions=? WHERE name=?", (json.dumps(held), "CEO"))
            store._conn.close()
            store._conn = None
            store.connect()
            self.assertNotIn(SCREEN, store.role_permissions("CEO"))
        finally:
            if store._conn:
                store._conn.close()
            config.DB_PATH, store._conn = old, old_conn


if __name__ == "__main__":
    unittest.main()
