"""The department filter offers a person limited to some locations only the departments of their access, and the
servers refuse one outside it (Geri, 15 Nov 2026: a universal rule, by the access given in Administration)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class DeptAccess(unittest.TestCase):
    def setUp(self):
        import locintel
        import server
        self.server = server
        self.old = locintel.registry
        locintel.registry = lambda settings=None, inactive=False: [
            {"ref": "OPA_B", "company": "SALT", "dept": "OPA1", "bc": "OPA1"},
            {"ref": "OPA_TEG", "company": "SALT", "dept": "OPA TEG", "bc": "OPA TEG"},
            {"ref": "LIFT", "company": "SALT", "dept": "LIFT", "bc": "LIFT"}]
        self.addCleanup(lambda: setattr(locintel, "registry", self.old))
        self.franc = {"username": "fmatraku", "permissions": [],
                      "scope": {"companies": ["SALT"], "locations": {"SALT": ["CENTRAL", "OPA1", "OPA TEG"]}}}
        self.dims = [["ADM", "Administrata"], ["CENTRAL", "Centralizim"], ["LIFT", "LIFT"], ["OPA1", "OPA Blloku"],
                     ["OPA TEG", "OPA TEG"]]

    def handler(self):
        h = self.server.Handler.__new__(self.server.Handler)
        h._scope_filter, h._allowed = None, None
        return h

    def test_only_the_departments_of_the_access(self):
        h = self.handler()
        self.assertIsNone(h._access("/api/companies", {"company": "SALT"}, self.franc))
        out = h._scope_filter({"companies": ["SALT", "TBM"], "dimensions": self.dims})
        self.assertEqual(out["companies"], ["SALT"])
        self.assertEqual([d[0] for d in out["dimensions"]], ["CENTRAL", "OPA1", "OPA TEG"])
        self.assertTrue(out["limited"])

    def test_a_company_outside_the_access_has_no_department(self):
        h = self.handler()
        h._access("/api/companies", {"company": "TBM"}, self.franc)
        self.assertEqual(h._scope_filter({"companies": ["SALT", "TBM"], "dimensions": self.dims})["dimensions"], [])

    def test_everyone_else_sees_every_department(self):
        h = self.handler()
        self.assertIsNone(h._access("/api/companies", {"company": "SALT"}, {"scope": {"companies": "*"}}))
        self.assertIsNone(h._scope_filter)

    def test_a_department_outside_the_access_is_refused(self):
        h = self.handler()
        r = h._access("/api/sales", {"company": "SALT", "dimension": "LIFT"}, self.franc)
        self.assertIn("outside your access", r["error"])
        self.assertIsNone(self.handler()._access("/api/sales", {"company": "SALT", "dimension": "OPA TEG,opa1"}, self.franc))


if __name__ == "__main__":
    unittest.main()
