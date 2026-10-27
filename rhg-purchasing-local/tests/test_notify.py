"""Regression checks for asynchronous email tests; never contact BC or SMTP."""
import contextlib
import io
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import notify


class EmailTestJobs(unittest.TestCase):
    def setUp(self):
        self.resources = contextlib.ExitStack()
        self.addCleanup(self.resources.close)
        directory = self.resources.enter_context(tempfile.TemporaryDirectory())
        self.resources.enter_context(patch.object(notify, "STATE_FILE", Path(directory) / "notifications.json"))
        self.resources.enter_context(patch.object(notify.store, "audit"))
        self.resources.enter_context(patch.object(notify.store, "list_actions", return_value=[]))
        self.resources.enter_context(patch.object(notify.store, "get_settings", return_value={
            "notifications": {"coordinator": "coordinator@example.com", "manager": "manager@example.com"}}))
        self.mail = self.resources.enter_context(patch.object(notify, "send_mail"))
        self.gate = threading.Event()
        self.reading = threading.Event()
        self.worker_done = threading.Event()
        self.notifier = notify.Notifier(self.read_window, "SALT")
        self.mark = self.resources.enter_context(patch.object(self.notifier, "_mark_reported"))
        # Release any blocked worker before its mocks and temporary state go away.
        self.addCleanup(self.finish_worker)

    def read_window(self, *args):
        self.reading.set()
        if not self.gate.wait(3):
            raise RuntimeError("Test did not release the BC read")
        return {"available": True, "invoices": [], "kpis": {}}

    def finish_worker(self):
        self.gate.set()
        if self.notifier.test_status("alice"):
            self.wait_result()

    def start(self, **kwargs):
        return self.notifier.start_test("SALT", actor="alice", **kwargs)

    def wait_result(self):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            job = self.notifier.test_status("alice")
            if job and job["status"] not in ("queued", "running"):
                return job
            self.worker_done.wait(0.01)
        self.fail("Email worker did not finish")

    def test_returns_before_business_central_finishes_and_can_resume(self):
        job = self.start(only_to=["test@example.com"])
        self.assertTrue(self.reading.wait(1))
        self.assertFalse(self.gate.is_set())
        self.mail.assert_not_called()
        self.assertEqual(self.notifier.test_status("alice")["id"], job["id"])
        self.gate.set()
        result = self.wait_result()
        self.assertEqual(result["status"], "sent")
        self.assertEqual(result["result"]["to"], ["test@example.com"])
        self.assertEqual(result["result"]["cc"], [])
        self.mail.assert_called_once()
        self.mark.assert_not_called()

    def test_duplicate_request_does_not_send_twice(self):
        first = self.start()
        second = self.start()
        self.assertEqual(first["id"], second["id"])
        self.gate.set()
        self.wait_result()
        self.mail.assert_called_once()

    def test_different_request_is_rejected_while_test_is_active(self):
        self.start()
        with self.assertRaisesRegex(ValueError, "already running"):
            self.start(only_to=["another@example.com"])

    def test_results_are_scoped_to_requesting_user(self):
        job = self.start()
        self.assertIsNone(self.notifier.test_status("bob", job["id"]))
        self.assertIsNone(self.notifier.test_status("bob"))
        self.assertIsNone(self.notifier.test_status("alice", "unknown"))

    def test_blank_override_preserves_configured_to_and_cc(self):
        self.start()
        self.gate.set()
        result = self.wait_result()["result"]
        self.assertEqual(result["to"], ["coordinator@example.com"])
        self.assertEqual(result["cc"], ["manager@example.com"])

    def test_smtp_failure_is_available_in_status_and_history(self):
        self.mail.side_effect = RuntimeError("SMTP authentication rejected")
        self.start()
        with contextlib.redirect_stderr(io.StringIO()):
            self.gate.set()
            job = self.wait_result()
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["result"]["detail"], "SMTP authentication rejected")
        self.assertEqual(notify.recent_runs()[0]["status"], "failed")
        self.mark.assert_not_called()

    def test_recording_failure_does_not_leave_job_running(self):
        with patch.object(self.notifier, "send", side_effect=OSError("disk full")):
            with contextlib.redirect_stderr(io.StringIO()):
                self.start()
                job = self.wait_result()
        self.assertEqual(job["status"], "failed")
        self.assertIn("before retrying", job["result"]["detail"])


if __name__ == "__main__":
    unittest.main()
