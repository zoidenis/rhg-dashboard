"""Control → EMC privileges: a user's rights are those of all their roles; two users compared right by right."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import emcpriv

ROLES = [{"objectNum": 1, "name": "Manager",
          "emcModules": [{"moduleNum": 20, "moduleName": "Menu Items", "privilegesExt": {"view": True, "edit": True}}],
          "operations": [{"privilegeNum": 102, "name": "Reopen closed checks", "enable": True}],
          "fields": [{"moduleNum": 20, "moduleName": "Menu Items", "controls": [{"controlNum": 1, "controlName": "Price", "privilegesExt": {"editable": True}}]}]},
         {"objectNum": 2, "name": "Cashier",
          "emcModules": [{"moduleNum": 20, "moduleName": "Menu Items", "privilegesExt": {"view": True}}],
          "operations": [{"privilegeNum": 101, "name": "Void", "enable": False}, {"privilegeNum": 104, "name": "Discounts", "enable": True}],
          "fields": [{"moduleNum": 20, "moduleName": "Menu Items", "controls": [{"controlNum": 1, "controlName": "Price", "privilegesExt": {"viewOnly": True}}]}]},
         {"objectNum": 3, "name": "Supervisor", "operations": [{"privilegeNum": 102, "name": "Reopen closed checks", "enable": True}],
          "emcModules": [{"moduleNum": 20, "moduleName": "Menu Items", "privilegesExt": {"add": True}}]}]
EMPS = [emcpriv._emp(x) for x in [
    {"objectNum": 1, "firstName": "A", "userName": "a", "pin": "1234", "payrollId": "X", "roles": [{"roleObjNum": 1}]},
    {"objectNum": 2, "firstName": "B", "userName": "b", "pin": "9999", "roles": [{"roleObjNum": 2}, {"roleObjNum": 3}]},
    {"objectNum": 3, "firstName": "Gone", "isDeleted": True, "roles": [{"roleObjNum": 1}]}]]


class EmcPriv(unittest.TestCase):
    def test_the_pin_is_never_kept(self):
        self.assertNotIn("pin", json.dumps(EMPS))
        self.assertNotIn("payrollId", json.dumps(EMPS))

    def test_rights_of_all_roles_together_and_compared(self):
        m = emcpriv.model(EMPS, ROLES)
        self.assertEqual([e["num"] for e in m["employees"]], [1, 2])          # the deleted one left out
        b = emcpriv.effective(m, m["employees"][1])
        self.assertEqual(b["emc:20"], ["view", "add"])                         # Cashier view + Supervisor add
        self.assertEqual(b["fld:20:1"], "view only")
        self.assertNotIn("op:101", b)                                           # a privilege switched off
        rows = {r["key"]: r["st"] for r in emcpriv.compare(m, m["employees"][0], m["employees"][1])}
        self.assertEqual(rows, {"emc:20": "diff", "fld:20:1": "diff", "op:102": "same", "op:104": "b"})

    def test_the_password_stays_on_the_server(self):
        d = Path(tempfile.mkdtemp())
        with patch.object(emcpriv, "CONFIG_FILE", d / "cc.json"), patch.object(emcpriv.store, "DATA_DIR", d), \
             patch.object(emcpriv.store, "audit", lambda *a, **k: None), \
             patch.object(emcpriv.simphony, "info", lambda: {"auth_server": "https://idm.example", "org": "RHG"}):
            i = emcpriv.save({"cc_server": "https://cc.example/", "client_id": "abcdefghijkl", "username": "api_cc",
                              "password": "s3cret"}, "geri")
            self.assertTrue(i["configured"])                                  # auth server and org from Settings → Simphony
            self.assertNotIn("s3cret", json.dumps(i))
            self.assertEqual(i["cc_server"], "https://cc.example")
            with self.assertRaises(ValueError):
                emcpriv.save({"cc_server": "http://plain.example"}, "geri")
            i = emcpriv.save({"username": "other"}, "geri")                   # another account: the password is dropped
            self.assertFalse(i["has_password"])


if __name__ == "__main__":
    unittest.main()
