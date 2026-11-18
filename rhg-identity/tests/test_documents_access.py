"""Contracts & SOPs: only the accounts named in M365_DOCS_USERS, by username."""
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


if __name__ == "__main__":
    unittest.main()
