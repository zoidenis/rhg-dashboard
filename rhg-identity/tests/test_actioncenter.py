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


class Assign(Base):
    def setUp(self):
        super().setUp()
        if not store.one("SELECT 1 FROM users WHERE username='tbm1'"):
            store.run("INSERT INTO users(username,name,email,role,salt,hash,active,must_change,scope) VALUES(?,?,?,?,?,?,1,0,?)",
                      ("tbm1", "Tea TBM", "tea@rhg.al", "Accountant", "s", "h", '{"companies": ["TBM"], "locations": {}}'))
        self.sent, self.mails = [], []
        sent = self.sent

        class R:
            ok, status_code, content = True, 200, b"{}"

            def json(self):
                return {"item": {}}
        import requests
        old = requests.post
        requests.post = lambda url, timeout=None, json=None, headers=None: (sent.append(json), R())[1]
        self.addCleanup(lambda: setattr(requests, "post", old))
        ac._send_mail = lambda to, cc, subject, body, text: self.mails.append((to, subject))

    def test_delegates_what_has_no_owner(self):
        can = lambda u, p: p == "actioncenter.manage"  # noqa: E731
        boss = {"username": "geri", "name": "Geri", "email": "geri@rhg.al"}
        j = ac.assign(["finance:a1"], "Tea@rhg.al", d(5), "", boss, can, notify=True)
        self.assertEqual((j["assigned"], j["failed"], j["emailed"]), (1, [], True))
        self.assertEqual(self.sent[0]["patch"], {"owner": "tea@rhg.al", "due": d(5), "comment": "Assigned to Tea TBM"})
        self.assertEqual(self.mails[0][0], ["tea@rhg.al"])

    def test_only_to_an_account_whose_access_covers_the_item(self):
        can = lambda u, p: p == "actioncenter.manage"  # noqa: E731
        boss = {"username": "geri", "name": "Geri", "email": "geri@rhg.al"}
        j = ac.assign(["finance:n1", "finance:a1", "finance:zz"], "tea@rhg.al", "", "x", boss, can)
        self.assertEqual(j["assigned"], 1)                                  # n1 is SALT, out of her access; zz unknown
        self.assertEqual(len(j["failed"]), 2)
        self.assertIn("does not cover SALT", j["failed"][0]["error"])
        with self.assertRaises(ValueError):
            ac.assign(["finance:a1"], "nobody@x.al", "", "", boss, can)
        with self.assertRaises(ValueError):
            ac.update("finance:n1", {"owner": "tea@rhg.al"}, boss, can)

    def test_someone_without_rights_assigns_nothing(self):
        j = ac.assign(["finance:a1"], "tea@rhg.al", "", "", {"username": "x", "email": "x@rhg.al"}, lambda u, p: False)
        self.assertEqual(j["assigned"], 0)
        self.assertEqual(self.sent, [])


class OwnerText(Base):
    def test_an_owner_chosen_with_name_and_email_is_that_account(self):
        self.assertEqual(ac._emails("Fabian Kuka <FKuka@rhg.al>"), ["fkuka@rhg.al"])
        self.assertEqual(ac._emails("a@rhg.al, b@rhg.al; a@rhg.al"), ["a@rhg.al", "b@rhg.al"])
        self.assertEqual(ac._emails("Drejtori i LIFT"), [])

    def test_a_unicorn_action_given_to_an_account_is_in_their_items(self):
        f = ureg.upsert_finding({"rule": "pl.labor_vs_budget", "capability": "financial", "company": "SALT", "location": "OPA TEG",
                                 "location_name": "OPA TEG", "period": "2026-09", "title": "Labour", "metric": "labor_pct",
                                 "value": 22.0, "limit": 18.4, "unit": "%", "severity": "high"}, "2026-11-14")[0]
        did, ids = ureg.delegate(f, "geri", "Ana Stock <Ana@rhg.al>", "2026-12-01")
        self.addCleanup(lambda: (store.run("DELETE FROM unicorn_actions WHERE decision_id=?", (did,)),
                                 store.run("DELETE FROM unicorn_decisions WHERE id=?", (did,)),
                                 store.run("DELETE FROM unicorn_findings WHERE id=?", (f,))))
        x = next(i for i in ac.collect()["items"] if i["uid"] == f"unicorn:A{ids[0]}")
        self.assertEqual(x["owners"], ["ana@rhg.al"])
        self.assertEqual([a["name"] for a in x["owner_accounts"]], ["Ana Stock"])


class ExportItem(Base):
    def test_the_card_and_history_as_a_workbook(self):
        x = next(i for i in ac.collect()["items"] if i["uid"] == "finance:n1")
        t = ac.item_table(x, {"username": "geri", "name": "Geri"})
        rows = dict(t["sheets"][0]["rows"])
        self.assertEqual((rows["Item"], rows["Company"], rows["Severity"], rows["Value"]), ("Pule below zero", "SALT", "Critical", "5,000"))
        self.assertEqual(t["sheets"][1]["header"], ["When", "By", "Status", "Note"])
        self.assertIn("Exported by Geri", t["notes"][1])

    def test_sent_through_purchasing_with_the_file(self):
        sent = {}

        class R:
            ok, status_code, content = True, 200, b"{}"

            def json(self):
                return {"sent": {"to": ["boss@rhg.al"], "cc": ["geri@rhg.al"], "file": "x.pdf"}}
        import requests
        old = requests.post
        requests.post = lambda url, timeout=None, json=None, headers=None: (sent.update(url=url, body=json), R())[1]
        try:
            out = ac.send_item("finance:n1", ["boss@rhg.al"], "pdf", "Shiko", True, {"username": "geri", "name": "Geri", "email": "geri@rhg.al", "scope": None})
        finally:
            requests.post = old
        self.assertTrue(sent["url"].endswith("/internal/export/email"))
        self.assertEqual((sent["body"]["to"], sent["body"]["cc"], sent["body"]["format"]), ("boss@rhg.al", "geri@rhg.al", "pdf"))
        self.assertIn("Shiko", sent["body"]["html"])
        self.assertEqual(out["file"], "x.pdf")
        with self.assertRaises(ValueError):
            ac.send_item("finance:none", "a@b.al", "pdf", "", False, {"username": "geri", "scope": None})


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


class AdminRoles(Base):
    def setUp(self):
        super().setUp()
        if not store.one("SELECT 1 FROM roles WHERE name='Sales analyst'"):
            store.run("INSERT INTO roles(name,description,permissions,system,department) VALUES(?,?,?,?,?)",
                      ("Sales analyst", "", "[]", 0, "Sales"))
        if not store.one("SELECT 1 FROM users WHERE username='sa'"):
            store.run("INSERT INTO users(username,name,email,role,salt,hash,active,must_change,scope) VALUES(?,?,?,?,?,?,1,0,?)",
                      ("sa", "Sara Analyst", "sara@rhg.al", "Sales analyst", "s", "h", '{"companies": ["SALT"], "locations": {}}'))

    def test_roles_of_administration_in_the_matrix(self):
        A = {r["name"]: r for r in ac.admin_roles()}
        self.assertEqual((A["Sales analyst"]["ac_department"], A["Sales analyst"]["people"][0]["email"]), ("Sales", "sara@rhg.al"))
        rules = ac.save_owner_rule("Finance", "negative_stock", "stock", "admin:Sales analyst", "geri")
        self.assertEqual(rules["Finance|negative_stock|stock"], "admin:Sales analyst")
        p = ac.policy()
        self.assertEqual(p["admin_people"]["Sales analyst"][0]["scope"]["companies"], ["SALT"])
        with self.assertRaises(ValueError):
            ac.save_owner_rule("Finance", "negative_stock", "stock", "admin:No such role", "geri")
        ac.save_owner_rule("Finance", "negative_stock", "stock", "", "geri")
        self.assertNotIn("Finance|negative_stock|stock", ac.settings()["owner_rules"])

    def test_an_item_owned_by_an_administration_role(self):
        reg = dict(REG["finance"], items=[dict(REG["finance"]["items"][0], role="admin:Sales analyst", owner="sara@rhg.al")])
        ac._tower = lambda key, max_age=60: reg if key == "finance" else REG[key]
        x = next(i for i in ac.collect()["items"] if i["uid"] == "finance:n1")
        self.assertEqual((x["role_label"], x["department"]), ("Sales analyst", "Sales"))

    def test_sales_is_a_department_of_the_sla_matrix(self):
        s = ac.settings()
        self.assertIn("Sales", ac.DEPARTMENTS)
        self.assertEqual(s["sla"]["Sales"], {"critical": 2, "high": 5, "medium": 7, "low": 10})
        self.assertEqual(ac.policy()["admin_departments"]["Sales analyst"], "Sales")
        ac.save_settings({"sla": {"Sales": {"critical": 1, "high": 3, "medium": 5, "low": 8}}}, "geri")
        self.addCleanup(lambda: ac.save_settings({"sla": {"Sales": ac.DEFAULT_SLA["Sales"]}}, "geri"))
        self.assertEqual(ac.policy()["sla"]["Sales"]["high"], 3)

    def test_the_department_of_a_role_can_be_changed(self):
        A = {r["name"]: r for r in ac.admin_roles()}
        self.assertEqual(A["Sales analyst"]["default_department"], "Sales")
        got = ac.save_role_department("Sales analyst", "Operations", "geri")
        self.addCleanup(lambda: ac.save_role_department("Sales analyst", "", "geri"))
        self.assertEqual(got["Sales analyst"], "Operations")
        self.assertEqual(ac.policy()["admin_departments"]["Sales analyst"], "Operations")
        with self.assertRaises(ValueError):
            ac.save_role_department("Sales analyst", "Marketing", "geri")
        with self.assertRaises(ValueError):
            ac.save_role_department("No role", "Sales", "geri")

    def test_an_administration_role_as_head_of_department(self):
        before = ac.settings()["escalation"]
        self.addCleanup(lambda: ac.save_settings({"escalation": before}, "geri"))
        self.assertEqual(ac.role_people("admin:Sales analyst", "SALT"), ["sara@rhg.al"])
        self.assertEqual(ac.role_people("admin:Sales analyst", "OTHER"), [])          # her access is SALT only
        ac.save_settings({"escalation": {"level1_days": 2, "level2_days": 5, "heads": {"Sales": "admin:Sales analyst"},
                                         "executives": ["admin:Sales analyst", "ceo"]}}, "geri")
        e = ac.settings()["escalation"]
        self.assertEqual((e["heads"]["Sales"], e["executives"][0]), ("admin:Sales analyst", "admin:Sales analyst"))


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
