"""reviews.py: Google Business Profile reviews by month, Places snapshots, the Wolt webhook (signature, storage)."""
import hashlib
import hmac
import json
import pathlib
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

import reviews
import store


class Reviews(unittest.TestCase):
    def setUp(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.ps = [patch.object(reviews, "CONFIG_FILE", tmp / "reviews.json"), patch.object(reviews, "DB_FILE", tmp / "reviews.db"),
                   patch.object(store, "audit", lambda *a, **k: None)]
        for p in self.ps:
            p.start()
        reviews.save({"wolt": {"map": "OPA_B = venue-1", "new_secret": True}}, "ceo")

    def tearDown(self):
        for p in self.ps:
            p.stop()

    def test_secret_never_shown_and_kept_when_blank(self):
        i = reviews.info()
        self.assertTrue(i["wolt"]["secret_set"])
        self.assertNotIn("secret", i["wolt"])
        s = reviews.wolt_secret()
        self.assertEqual(len(s), 64)
        reviews.save({"wolt": {"secret": ""}, "google": {"api_key": "k"}}, "ceo")
        self.assertEqual(reviews.wolt_secret(), s)
        self.assertTrue(reviews.info()["google"]["places"])
        self.assertIn("OPA_B", reviews.info()["google"]["map"])           # the OPA place IDs come pre-filled

    def test_wolt_webhook(self):
        body = json.dumps({"id": "n1", "type": "order_review.notification",
                           "order": {"id": "o1", "venue_id": "venue-1", "resource_url": "x"},
                           "review": {"goods_review": {"rating": 4, "comment": "Good", "attributions": []}},
                           "created_at": "2026-09-10T09:36:50.979947759Z"}, separators=(",", ":")).encode()
        sig = hmac.new(reviews.wolt_secret().encode(), body, hashlib.sha256).hexdigest()
        self.assertTrue(reviews.wolt_verify(body, sig))
        self.assertFalse(reviews.wolt_verify(body + b" ", sig))
        self.assertFalse(reviews.wolt_verify(body, "00" * 32))
        self.assertTrue(reviews.wolt_receive(body))
        body2 = body.replace(b'"n1"', b'"n2"').replace(b'"rating":4', b'"rating":2')
        reviews.wolt_receive(body2)
        m = reviews.monthly("OPA_B", date(2026, 9, 1), date(2026, 9, 30))
        self.assertEqual((m["wolt"]["value"], m["wolt"]["count"]), (3.0, 2))
        self.assertEqual(reviews.monthly("OPA_B", date(2026, 10, 1), date(2026, 10, 31))["wolt"]["count"], 0)
        self.assertEqual(reviews.recent("OPA_B", date(2026, 9, 1), date(2026, 9, 30))[0]["rating"], 2.0)

    def test_google_business_profile_month_in_local_time(self):
        pid = "ChIJS4oQUEkxUBMRv8S5DQsJ7Uw"
        reviews._put("google", pid, "r1", "2026-08-31T23:30:00Z", 5, "")      # 1 Sep 00:30 in Tirana
        reviews._put("google", pid, "r2", "2026-09-15T10:00:00Z", 3, "slow")
        reviews._put("google", pid, "r3", "2026-10-01T10:00:00Z", 1, "")
        m = reviews.monthly("OPA_B", date(2026, 9, 1), date(2026, 9, 30))
        self.assertEqual((m["google"]["value"], m["google"]["count"], m["google"]["approximate"]), (4.0, 2, False))

    def test_places_snapshots_count_the_month(self):
        pid = "ChIJS4oQUEkxUBMRv8S5DQsJ7Uw"
        db = reviews._db()
        db.executemany("INSERT INTO snap VALUES ('places',?,?,?,?)", [(pid, "2026-08-31", 4.6, 3000), (pid, "2026-09-30", 4.6, 3064)])
        db.commit()
        db.close()
        m = reviews.monthly("OPA_B", date(2026, 9, 1), date(2026, 9, 30))
        self.assertEqual((m["google"]["value"], m["google"]["count"], m["google"]["approximate"]), (4.6, 64, True))


if __name__ == "__main__":
    unittest.main()
