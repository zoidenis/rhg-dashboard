"""Reports center: every report in one list, its state worked out the same way for all, on and off by permission."""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
os.environ.setdefault("SERVICE_TOKEN", "test")
import config  # noqa: E402

_tmp = Path(tempfile.mkdtemp())
config.DB_PATH = _tmp / "identity.db"
import store  # noqa: E402
import reportcenter as rc  # noqa: E402

store.connect()
NOW = datetime(2026, 11, 16, 12, 0)


def run(hours_ago, status="sent", test=False, to=("a@rhg.al",)):
    return {"at": (NOW - timedelta(hours=hours_ago)).isoformat(timespec="seconds"), "status": status, "target": "SALT",
            "to": list(to), "cc": [], "test": test, "actor": "scheduler", "detail": ""}


def item(runs, enabled=True, schedule=None, **x):
    return {"id": "finance:k", "kind": "email", "app": "finance", "enabled": enabled,
            "schedule": schedule or {"days": [0, 1, 2, 3, 4, 5, 6], "time": "08:00"}, "runs": runs} | x


class Health(unittest.TestCase):
    def st(self, it):
        return rc.health(it, NOW)["state"]

    def test_states(self):
        self.assertEqual(self.st(item([run(5)], enabled=False)), "paused")
        self.assertEqual(self.st(item([])), "waiting")
        self.assertEqual(self.st(item([run(5)])), "ok")
        self.assertEqual(self.st(item([run(80)])), "late")                      # daily, nothing for over 2 days
        self.assertEqual(self.st(item([run(80)], schedule={"weekly": 0, "time": "08:00"})), "ok")
        self.assertEqual(self.st(item([run(5, "skipped")])), "skipped")
        self.assertEqual(self.st(item([run(5), run(5, "failed")])), "failing")   # part of the day's sends failed
        self.assertEqual(self.st(item([run(5), run(30, "failed")])), "ok")        # an older failure, sent since
        self.assertEqual(self.st(item([run(1, "failed", test=True)])), "waiting")  # tests do not count
        self.assertEqual(self.st(item([], combined=True)), "combined")
        self.assertEqual(self.st(item([], logged=False)), "ok")
        self.assertEqual(self.st(item([], error="HTTP 500")), "unknown")

    def test_working_days_gap(self):
        # Mon–Fri: a send on Friday is not late on Monday morning
        self.assertEqual(rc._gap_days({"days": [0, 1, 2, 3, 4]}), 4)
        self.assertEqual(rc._gap_days({"days": [0, 1, 2, 3, 4, 5, 6]}), 2)

    def test_counts(self):
        it = rc.health(item([run(5), run(5, "failed"), run(30), run(200), run(2, test=True)]), NOW)
        self.assertEqual((it["sent_7d"], it["failed_7d"]), (2, 1))
        self.assertEqual(it["last_batch"]["to"], 1)
        self.assertEqual(it["last_sent_at"][:10], "2026-11-16")


class Permissions(unittest.TestCase):
    def user(self, *perms):
        return {"username": "u", "permissions": list(perms)}

    def test_view(self):
        self.assertFalse(rc.can_view(self.user("finance.view")))
        self.assertTrue(rc.can_view(self.user("reports.manage")))

    def test_toggle_needs_the_towers_settings(self):
        u = self.user("reports.manage", "purchasing.settings.manage")
        self.assertFalse(rc._can_toggle(u, {"id": "finance:k", "kind": "email", "app": "finance", "toggle": ["x", "enabled"]}))
        self.assertTrue(rc._can_toggle(u, {"id": "purchasing:k", "kind": "email", "app": "purchasing", "toggle": ["x", "enabled"]}))
        self.assertFalse(rc._can_toggle(u, {"id": "platform:unicorn_brief", "kind": "brief", "app": "platform", "fixed": True}))
        with self.assertRaises(PermissionError):
            rc.toggle(u, "finance:arap_daily", False)
        with self.assertRaises(PermissionError):
            rc.toggle(self.user("finance.settings.manage"), "screen:r1", False)

    def test_toggle_goes_to_the_tower(self):
        u = self.user("finance.settings.manage")
        resp = mock.Mock(ok=True, headers={"Content-Type": "application/json"}, json=lambda: {"item": {}})
        with mock.patch.object(rc.requests, "post", return_value=resp) as post:
            rc.toggle(u, "finance:arap_daily", False)
        args, kw = post.call_args
        self.assertTrue(args[0].endswith("/internal/reports/toggle"))
        self.assertEqual(kw["json"], {"key": "arap_daily", "enabled": False, "actor": "u"})


if __name__ == "__main__":
    unittest.main()
