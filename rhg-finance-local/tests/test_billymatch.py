"""Simphony ↔ Billy: a bill fiscalized while its check was still open is the check's invoice (QA 14 Nov 2026)."""
import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import billyrec  # noqa: E402

S = {"window": 20, "tol": 1, "late": 60}


def ts(s):
    return datetime.fromisoformat(s).timestamp()


def chk(cid, total, opn, clsd):
    return {"id": cid, "total": total, "info": "", "_t": ts(clsd), "_o": ts(opn), "inv": None}


def inv(k, total, issue):
    return {"_k": k, "total": total, "_t": ts(issue), "iic": "", "fic": "F" + k, "num": ""}


class OpenCheck(unittest.TestCase):
    def test_bill_fiscalized_before_the_closing(self):
        # SALT 5086: opened 23:40, the bill fiscalized 23:58, the check closed 00:35 (37 minutes later)
        c = chk(5086, 2190.0, "2026-11-10T23:40:00", "2026-11-11T00:35:00")
        i = inv("a", 2190.0, "2026-11-10T23:58:00")
        pairs = billyrec._match([c], [i], S, set())
        self.assertEqual(c["inv"], "a")
        self.assertIn("open", c["how"])
        self.assertEqual(len(pairs), 1)

    def test_the_window_first_and_never_before_the_opening(self):
        c1 = chk(1, 500.0, "2026-11-10T20:00:00", "2026-11-10T21:00:00")
        c2 = chk(2, 500.0, "2026-11-10T20:50:00", "2026-11-10T21:30:00")
        near = inv("near", 500.0, "2026-11-10T21:28:00")    # c2's, in its window
        before = inv("before", 500.0, "2026-11-10T19:30:00")  # before either check opened: no one's
        used = set()
        billyrec._match([c1, c2], [near, before], S, used)
        self.assertEqual(c2["inv"], "near")
        self.assertIsNone(c1["inv"])


if __name__ == "__main__":
    unittest.main()
