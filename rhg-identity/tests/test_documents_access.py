"""Contracts & SOPs: the accounts named in M365_DOCS_USERS, by username, and the roles that hold its tabs."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


class DocsAccess(unittest.TestCase):
    def allowed(self, user, setting=None):
        values = {} if setting is None else {"M365_DOCS_USERS": setting}
        with patch.object(server.config, "get", side_effect=lambda k, d=None: values.get(k, d)):
            return server.docs_allowed(user)

    def test_only_fabian_by_default(self):
        self.assertTrue(self.allowed({"username": "fkuka@rhg.al", "role": "Administrator"}))
        self.assertTrue(self.allowed({"username": "FKuka@rhg.al"}))
        for other in ("admin", "temp", "techi", "gkuka"):
            self.assertFalse(self.allowed({"username": other, "role": "Administrator"}), other)
        self.assertFalse(self.allowed(None))

    def test_email_does_not_open_it(self):
        self.assertFalse(self.allowed({"username": "temp", "email": "fkuka@rhg.al"}))

    def test_setting_names_the_accounts(self):
        self.assertTrue(self.allowed({"username": "gkuka"}, "fkuka@rhg.al, gkuka"))
        self.assertFalse(self.allowed({"username": "fkuka@rhg.al"}, ""))

    def tabs(self, user, setting=None):
        values = {} if setting is None else {"M365_DOCS_USERS": setting}
        with patch.object(server.config, "get", side_effect=lambda k, d=None: values.get(k, d)):
            return server.docs_tabs(user)

    def test_named_account_holds_every_tab(self):
        self.assertEqual(self.tabs({"username": "fkuka@rhg.al", "permissions": []}), ["contracts", "policies", "libraries"])

    def test_a_role_opens_the_tabs_it_holds(self):
        u = {"username": "kejsi", "permissions": ["platform.s.documents.policies", "platform.s.documents.libraries"]}
        self.assertEqual(self.tabs(u), ["policies", "libraries"])
        self.assertTrue(self.allowed(u))
        self.assertEqual(self.tabs({"username": "kejsi", "permissions": ["finance.s.sales.main"]}), [])

    def test_access_cut_to_companies_or_locations_sees_none(self):
        p = ["platform.s.documents.contracts"]
        self.assertEqual(self.tabs({"username": "a", "permissions": p, "scope": {"companies": "*", "locations": {}}}), ["contracts"])
        self.assertEqual(self.tabs({"username": "a", "permissions": p, "scope": {"companies": ["SALT"], "locations": {}}}), [])
        self.assertEqual(self.tabs({"username": "a", "permissions": p,
                                    "scope": {"companies": "*", "locations": {"SALT": ["OPA1"]}}}), [])

    def test_administrator_does_not_get_it_by_itself(self):
        import permissions
        admin = permissions.DEFAULT_ROLES["Administrator"]["permissions"]
        self.assertFalse([p for p in admin if p.startswith("platform.s.documents.")])
        self.assertTrue(permissions.NOT_FOR_ADMIN)
        self.assertFalse([n for n, r in permissions.DEFAULT_ROLES.items()
                          if any(p.startswith("platform.s.documents.") for p in r["permissions"])])
        self.assertFalse([p for p in permissions.screens_for_rights(["finance.view", "finance.pl.view", "purchasing.view"])
                          if p.startswith("platform.s.documents.")])

    def test_answer_keeps_only_the_tabs_held(self):
        body = {"available": True, "contracts": [{"title": "K"}], "policies": [{"title": "P"}],
                "libraries": [{"kind": "contracts", "library": "L1", "files": 1}, {"kind": "policies", "library": "L2", "files": 1}],
                "summary": {"contracts": 1, "contracts_flagged": 1, "notices_60": 1, "policies": 1, "policies_draft": 1},
                "contracts_site": {"name": "RHG-KONTRATA"}, "policies_site": {"name": "RHG-Politika"}}
        out = server.docs_cut(body, ["policies"])
        self.assertEqual(out["contracts"], [])
        self.assertEqual(out["policies"], [{"title": "P"}])
        self.assertEqual(out["libraries"], [])
        self.assertNotIn("contracts_site", out)
        self.assertEqual((out["summary"]["contracts"], out["summary"]["notices_60"], out["summary"]["policies"]), (0, 0, 1))
        self.assertEqual(len(server.docs_cut(body, ["contracts", "libraries"])["libraries"]), 2)
        self.assertEqual(server.docs_cut({"available": False, "error": "x"}, ["policies"]), {"available": False, "error": "x"})

    def test_not_a_scheduled_report(self):
        import reports
        self.assertFalse([s for s in reports.catalogue() if s["key"].startswith("platform.s.documents")])


if __name__ == "__main__":
    unittest.main()
