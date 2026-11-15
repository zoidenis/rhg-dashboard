"""Executive Action Center: one list over the registers and Unicorn, SLA, escalation, owners, updates, email."""
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
os.environ.setdefault("SERVICE_TOKEN", "test")
import config  # noqa: E402

_tmp = Path(tempfile.mkdtemp())
config.DB_PATH = _tmp / "identity.db"
import store  # noqa: E402
import actioncenter as ac  # noqa: E402
import unicorn_registers as ureg  # noqa: E402

ac.FILE = _tmp / "actioncenter.json"
ureg.init()
store.connect()


def d(n):
    return (date.today() + timedelta(days=n)).isoformat()


REG = {"finance": {"tower": "Finance", "link": "/finance/#register", "sources": {"negative_stock": "Negative stock", "ap_ar": "Payables & receivables"},
                   "roles": {"stock_manager": "Stock manager", "finance": "Finance"}, "runs": [],
                   "items": [
                       {"id": "n1", "source": "negative_stock", "kind": "stock", "company": "SALT", "location": "OPA1", "subject": "i1",
                        "title": "Pule below zero", "detail": "d", "severity": "critical", "value": 5000, "role": "stock_manager",
                        "owner": "ana@rhg.al", "owner_set": 0, "status": "Open", "first_seen": d(-6) + "T08:00:00", "last_seen": d(0),
                        "due": d(-4), "closed_at": None, "closed_by": None, "note": "", "reopened": 0, "overdue": True, "age": 6, "history": []},
                       {"id": "a1", "source": "ap_ar", "kind": "", "company": "TBM", "location": "", "subject": "v1",
                        "title": "Old payable", "detail": "d", "severity": "warning", "value": 90000, "role": "finance",
                        "owner": "", "owner_set": 0, "status": "In progress", "first_seen": d(-1) + "T08:00:00", "last_seen": d(0),
                        "due": d(1), "closed_at": None, "closed_by": None, "note": "", "reopened": 0, "overdue": False, "age": 1, "history": []},
                       {"id": "a2", "source": "ap_ar", "kind": "", "company": "SALT", "location": "", "subject": "v2",
                        "title": "Closed one", "detail": "d", "severity": "info", "value": 1, "role": "finance",
                        "owner": "bo@rhg.al", "owner_set": 0, "status": "Closed", "first_seen": d(-10) + "T08:00:00", "last_seen": d(-3),
                        "due": d(-2), "closed_at": d(-3) + "T09:00:00", "closed_by": "control run", "note": "", "reopened": 0, "overdue": False, "age": 7, "history": []}]},
       "purchasing": {"error": "Purchasing could not be reached: test"}}
DIRECTORY = {"roles": [{"key": "stock_manager", "label": "Stock manager"}, {"key": "coo", "label": "COO"}, {"key": "ceo", "label": "CEO"},
                       {"key": "cfo", "label": "CFO"}, {"key": "finance", "label": "Finance"}],
             "people": {"SALT": {"coo": "coo@rhg.al", "stock_manager": "ana@rhg.al"}, "*": {"ceo": "ceo@rhg.al", "cfo": "cfo@rhg.al"}},
             "locations": {"SALT": {"OPA1": {"stock_manager": "ana@rhg.al"}}}}


class Base(unittest.TestCase):
    def setUp(self):
        ac._tower = lambda key, max_age=60: REG[key]
        ac._directory = lambda: DIRECTORY
        if not store.one("SELECT 1 FROM users WHERE username='ana'"):
            for u, n, e, r in (("ana", "Ana Stock", "ana@rhg.al", "Stock manager"), ("geri", "Geri", "geri@rhg.al", "CEO")):
                store.run("INSERT INTO users(username,name,email,role,salt,hash,active,must_change) VALUES(?,?,?,?,?,?,1,0)",
                          (u, n, e, r, "s", "h"))


class Collect(Base):
    def test_unified_items(self):
        r = ac.collect()
        X = {x["uid"]: x for x in r["items"]}
        n = X["finance:n1"]
        self.assertEqual((n["department"], n["severity"], n["overdue"], n["days_overdue"], n["escalation"]), ("Warehouse", "critical", True, 4, 2))
        self.assertEqual(n["owner_accounts"][0]["name"], "Ana Stock")
        self.assertEqual(n["sla_due"], d(-4))                               # 6 days ago + Warehouse critical 2
        a = X["finance:a1"]
        self.assertEqual((a["department"], a["severity"], a["owners"], a["due_soon"]), ("Finance", "high", [], True))
        self.assertTrue(X["finance:a2"]["on_time"])                          # due d-2, closed d-3: on time
        self.assertIn("Purchasing could not be reached: test", r["errors"])

    def test_scope_cuts_companies(self):
        r = ac.collect({"companies": ["SALT"], "locations": {}})
        self.assertEqual({x["company"] for x in r["items"]}, {"SALT"})

    def test_stats(self):
        r = ac.collect()
        s = ac.stats(r["items"], r["today"])
        self.assertEqual((s["total"]["open"], s["total"]["overdue"], s["total"]["no_owner"]), (2, 1, 1))
        self.assertEqual(s["departments"]["Warehouse"]["overdue"], 1)
        self.assertEqual(s["owners"][0]["owner"], "ana@rhg.al")


class Change(Base):
    def test_who_may_change(self):
        x = next(i for i in ac.collect()["items"] if i["uid"] == "finance:n1")
        can = lambda u, p: p in u.get("permissions", [])  # noqa: E731
        self.assertTrue(ac.may_change({"email": "ana@rhg.al"}, x, can))
        self.assertFalse(ac.may_change({"email": "bo@rhg.al", "permissions": []}, x, can))
        self.assertTrue(ac.may_change({"email": "bo@rhg.al", "permissions": ["finance.actions.manage"]}, x, can))

    def test_update_goes_back_to_its_register(self):
        sent = {}

        class R:
            ok, status_code, content = True, 200, b"{}"

            def json(self):
                return {"item": {}}

        def fake_post(url, timeout=None, json=None, headers=None):
            sent.update(url=url, body=json)
            return R()
        import requests
        old = requests.post
        requests.post = fake_post
        try:
            can = lambda u, p: p == "finance.actions.manage"  # noqa: E731
            ac.update("finance:n1", {"status": "In progress", "comment": "on it", "due": d(3)},
                      {"username": "bo", "name": "Bo", "email": "bo@rhg.al", "permissions": []}, can)
        finally:
            requests.post = old
        self.assertTrue(sent["url"].endswith("/internal/register/update"))
        self.assertEqual(sent["body"]["patch"], {"status": "In progress", "comment": "on it", "due": d(3)})
        self.assertIn("bo@rhg.al", sent["body"]["actor"])

    def test_owner_cannot_move_the_deadline(self):
        can = lambda u, p: False  # noqa: E731
        with self.assertRaises(PermissionError):
            ac.update("finance:n1", {"due": d(9)}, {"username": "ana", "email": "ana@rhg.al"}, can)


class Settings(Base):
    def test_sla_validation_and_policy(self):
        with self.assertRaises(ValueError):
            ac.save_settings({"sla": {"Finance": {"critical": 0}}}, "geri")
        ac.save_settings({"sla": {"Finance": {"critical": 1, "high": 4, "medium": 6, "low": 8}}, "digest": {"enabled": True, "time": "07:45"}}, "geri")
        p = ac.policy()
        self.assertEqual((p["sla"]["Finance"]["high"], p["combined_digest"]), (4, True))
        with self.assertRaises(ValueError):
            ac.save_settings({"escalation": {"level1_days": 5, "level2_days": 2}}, "geri")
        ac.save_settings({"digest": {"enabled": False}}, "geri")


class Email(Base):
    def test_one_email_per_owner_with_escalation(self):
        mails = []
        ac._send_mail = lambda to, cc, subject, body, text: mails.append((to, cc, subject))
        sent = ac.send_digests("test")
        self.assertEqual(len(sent), 1)
        to, cc, subject = mails[0]
        self.assertEqual(to, ["ana@rhg.al"])
        self.assertEqual(set(cc), {"coo@rhg.al", "ceo@rhg.al", "cfo@rhg.al"})          # L1 head (Warehouse → coo), L2 executives
        self.assertIn("1 open, 1 past the deadline", subject)


if __name__ == "__main__":
    unittest.main()
