"""Notifications: who hears of what when a task changes, never the person who changed it; reading and marking."""
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
import notifications as N  # noqa: E402

store.connect()
U = {"sidorela@rhg.al": {"username": "sidorela@rhg.al", "name": "Sidorela Brahaj", "role": "Finance", "scope": None},
     "geri@rhg.al": {"username": "geri@rhg.al", "name": "Geri Kuka", "role": "Administrator", "scope": None},
     "erjon@rhg.al": {"username": "erjon@rhg.al", "name": "Erjon Matraku", "role": "Purchasing", "scope": None},
     "coo@rhg.al": {"username": "coo@rhg.al", "name": "Coo", "role": "COO", "scope": None}}
TODAY = "2026-11-18"


def item(**k):
    x = {"uid": "finance:7", "title": "Wolt Albania: 106,950 receivable overdue", "status": "Open", "owners": ["sidorela@rhg.al"],
         "due": "2026-11-25", "escalation": 0, "overdue": False, "due_soon": False, "active": True, "history": [],
         "first_seen": TODAY, "company": "SALT", "location": "", "department": "Finance", "severity": "critical", "tower": "Finance"}
    x.update(k)
    return x


def run(before, after, **kw):
    snaps = {before["uid"]: N._snap(before)} if before else {}
    return [(e, k, d) for e, k, _x, d in N.compare([after], snaps, U, today=TODAY, **kw)]


class Compare(unittest.TestCase):
    def test_comment_reaches_the_owner_not_the_writer(self):
        b = item()
        a = item(history=[{"ts": "2026-11-18T10:00:00", "by": "Geri Kuka <geri@rhg.al>", "to": "Open", "note": "Call Wolt today"}])
        got = run(b, a)
        self.assertEqual(got, [("sidorela@rhg.al", "comment", {"actor": "Geri Kuka", "text": "Call Wolt today"})])

    def test_a_reply_reaches_the_one_who_commented(self):
        h1 = {"ts": "2026-11-18T10:00:00", "by": "Geri Kuka <geri@rhg.al>", "to": "Open", "note": "Call Wolt today"}
        h2 = {"ts": "2026-11-18T11:00:00", "by": "sidorela@rhg.al", "to": "In progress", "note": "Called, they pay Friday"}
        got = run(item(history=[h1]), item(status="In progress", history=[h1, h2]))
        # the status and its comment come as one notification, to the one who commented before
        self.assertEqual(got, [("geri@rhg.al", "status", {"actor": "Sidorela Brahaj", "status": "In progress",
                                                          "from_status": "Open", "text": "Called, they pay Friday"})])

    def test_handover(self):
        h = {"ts": "2026-11-18T12:00:00", "by": "Geri Kuka <geri@rhg.al>", "to": "Open", "note": "Owner: erjon@rhg.al"}
        got = run(item(), item(owners=["erjon@rhg.al"], history=[h]))
        self.assertIn(("erjon@rhg.al", "assigned", {"actor": "Geri Kuka", "text": ""}), got)
        self.assertIn(("sidorela@rhg.al", "handed_over", {"actor": "Geri Kuka", "to": "Erjon Matraku"}), got)
        self.assertFalse([g for g in got if g[0] == "geri@rhg.al"])

    def test_deadline_overdue_and_escalation(self):
        got = run(item(), item(overdue=True, escalation=1), escalation_people=lambda x, lvl: ["coo@rhg.al"])
        self.assertIn(("sidorela@rhg.al", "overdue", {"due": "2026-11-25"}), got)
        self.assertIn(("sidorela@rhg.al", "escalated", {"level": 1, "due": "2026-11-25"}), got)
        self.assertIn(("coo@rhg.al", "escalated", {"level": 1, "due": "2026-11-25", "to": "you"}), got)
        # nothing new: nothing again
        self.assertEqual(run(item(overdue=True, escalation=1), item(overdue=True, escalation=1)), [])

    def test_new_items_only_after_the_first_picture_and_only_recent(self):
        self.assertEqual(run(None, item(), baseline=False), [])
        self.assertEqual(run(None, item()), [("sidorela@rhg.al", "new", {})])
        self.assertEqual(run(None, item(first_seen="2026-10-01")), [])        # came back after a tower did not answer
        self.assertEqual(run(None, item(status="Awaiting approval", owners=[]), approvers=["geri@rhg.al"]),
                         [("geri@rhg.al", "proposal", {})])

    def test_closed_by_the_data(self):
        h = {"ts": "2026-11-18T09:00:00", "by": "pulse", "to": "Closed", "note": "Verified: no longer found in the data"}
        got = run(item(), item(status="Closed", active=False, history=[h]))
        self.assertEqual(got, [("sidorela@rhg.al", "closed", {"actor": "", "from_status": "Open"})])


class Store(unittest.TestCase):
    def test_read_and_prefs(self):
        N._init()
        store.run("DELETE FROM notifications")
        for i in range(3):
            store.run("INSERT INTO notifications(username, at, kind, uid, title, data, link) VALUES(?,?,?,?,?,?,?)",
                      ("ana", f"2026-11-18T10:0{i}:00", "comment", "finance:1", "T", '{"actor": "Geri"}', "/actions"))
        self.assertEqual(N.unread("ana"), 3)
        first = N.listing("ana")[0]["id"]
        N.mark_read("ana", [first])
        self.assertEqual(N.unread("ana"), 2)
        self.assertEqual(len(N.listing("ana", only_unread=True)), 2)
        N.mark_read("ana", everything=True)
        self.assertEqual(N.unread("ana"), 0)
        self.assertEqual(N.unread("other"), 0)
        p = N.save_prefs("ana", {"comment": {"email": False}, "bogus": {"app": False}})
        self.assertFalse(p["comment"]["email"])
        self.assertTrue(p["comment"]["app"])
        self.assertNotIn("bogus", p)


if __name__ == "__main__":
    unittest.main()
