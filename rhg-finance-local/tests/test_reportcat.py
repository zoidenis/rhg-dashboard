"""Reports center (reportcat.py): every email's log read in one shape, and turned on or off through Settings."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import reportcat  # noqa: E402


class Runs(unittest.TestCase):
    def test_shapes(self):
        r = reportcat.runs([{"ts": "2026-11-15T07:30:00", "company": "SALT", "status": "sent", "to": ["a@x.al"], "cc": "b@x.al; c@x.al"},
                            {"ts": "2026-11-16T07:30:00", "target": "OPA", "status": "failed", "detail": "SMTP 550"},
                            {"ts": "2026-11-14T07:30:00", "status": "ok"},
                            {"ts": "2026-11-13T07:30:00", "to": ["a@x.al"]},          # no status: a send that went
                            "not a run"])
        self.assertEqual([x["status"] for x in r], ["failed", "sent", "sent", "sent"])
        self.assertEqual(r[1]["cc"], ["b@x.al", "c@x.al"])
        self.assertEqual(r[0]["target"], "OPA")

    def test_keep_and_target(self):
        r = reportcat.runs([{"ts": "1", "kind": "fetch"}, {"ts": "2", "kind": "email"}], keep=lambda e: e["kind"] == "email",
                           target=lambda e: "X")
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0]["target"], "X")

    def test_schedules(self):
        self.assertEqual(reportcat.days_schedule(range(5), "7:30", "08:00"), {"days": [0, 1, 2, 3, 4], "time": "08:00"})
        self.assertEqual(reportcat.weekly_schedule("9", "09:15"), {"weekly": 6, "time": "09:15"})


class Toggle(unittest.TestCase):
    def test_through_settings(self):
        saved = []
        items = [reportcat.item("a", "A", "Finance", "daily", {}, False, None, [], "settings", "c", toggle=("arap_notify", "enabled")),
                 reportcat.item("b", "B", "Finance", "daily", {}, True, None, [], "settings", "c")]
        reportcat.toggle(items, "a", True, lambda patch, actor: saved.append((patch, actor)), "geri")
        self.assertEqual(saved, [({"arap_notify": {"enabled": True}}, "geri")])
        with self.assertRaises(ValueError):
            reportcat.toggle(items, "b", False, lambda p, a: None, "geri")

    def test_safe(self):
        def bad():
            raise RuntimeError("BC down")
        it = reportcat.safe(bad, "k", "Name", "Finance", "settings", "card")
        self.assertEqual((it["key"], it["enabled"], it["error"]), ("k", False, "BC down"))


if __name__ == "__main__":
    unittest.main()
