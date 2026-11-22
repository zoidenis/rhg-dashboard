"""marketing.py: nl360's dashboard files (data.js, loc.js), the screen's data, and the OPA units' Google reviews."""
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

import marketing
import store

MONTHS = ["2026-08", "2026-09", "2026-10"]


def data_js(oct_units=(0, 0), oct_brand=131):
    d = {"generated": "2026-11-22T06:12:27+00:00", "syncedAt": "2026-11-22T05:01:39Z", "months": MONTHS,
         "brands": [{"id": "opa", "name": "OPA", "group": "year", "sub": "6 locations",
                     "months": [{"sales": 600000, "checks": 50000}] * 3,
                     "ads": [{"metaSpend": 1000, "gSpend": 500, "metaClicks": 40000, "gClicks": 10000, "estRev": 30000}] * 3,
                     "gbp": {"total": [9000, 10000, 11000], "reviews": {"n": [200, 224, oct_brand], "s": [900, 1000, 580],
                                                                        "cn": [8500, 8727, 8858], "cs": [39800, 40907, 41489]}},
                     "social": {"igFollowersEnd": [14000, 14800, 15211]},
                     "locs": [{"slug": "blloku", "name": "OPA Blloku", "months": [], "ads": [],
                               "gbp": {"name": "OPA Blloku", "total": [1, 2, 3],
                                       "reviews": {"n": [105, 58, oct_units[0]], "s": [485, 256, 0], "cn": [1, 2, 3], "cs": [1, 2, 3]}}},
                              {"slug": "teg", "name": "OPA TEG", "months": [], "ads": [],
                               "gbp": {"name": "OPA TEG", "total": [1, 2, 3],
                                       "reviews": {"n": [3, 4, oct_units[1]], "s": [15, 12, 0], "cn": [1, 2, 3], "cs": [1, 2, 3]}}}]},
                    {"id": "salt", "name": "SALT Tirana", "group": "year", "months": [{"sales": 400000}] * 3, "ads": [], "gbp": None,
                     "social": None, "locs": []}]}
    return "window.RHG = " + json.dumps(d) + ";"


LOC_JS = "window.RHG_LOC = " + json.dumps({"generated": "2026-11-03", "months": MONTHS[:2], "locs": [
    {"name": "OPA Blloku", "slug": "blloku", "till": "OPA_B"}, {"name": "OPA TEG", "slug": "teg", "till": "OPA_TE"}]}) + ";"


class Marketing(unittest.TestCase):
    def setUp(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.ps = [patch.object(marketing, "DIR", tmp / "marketing"), patch.object(marketing, "CONFIG_FILE", tmp / "marketing.json"),
                   patch.object(store, "audit", lambda *a, **k: None)]
        for p in self.ps:
            p.start()
        marketing._mem.update(at=0.0, value=None)

    def tearDown(self):
        for p in self.ps:
            p.stop()
        marketing._mem.update(at=0.0, value=None)

    def test_nothing_yet(self):
        p = marketing.public()
        self.assertFalse(p["available"])
        self.assertIsNone(marketing.opa_reviews("OPA_B", "2026-09"))

    def test_upload_and_screen(self):
        self.assertEqual(marketing.upload("data.js", data_js(), "ceo"), "data")
        self.assertEqual(marketing.upload("loc (1).js".replace(" (1)", ""), LOC_JS, "ceo"), "loc")
        with self.assertRaises(ValueError):
            marketing.upload("data.js", "<html>Sign in</html>", "ceo")
        with self.assertRaises(ValueError):
            marketing.upload("other.js", data_js(), "ceo")
        p = marketing.public()
        self.assertTrue(p["available"])
        self.assertEqual(p["months"], MONTHS)
        opa = p["brands"][0]
        self.assertEqual((opa["sales"][2], opa["locs"][0]["till"]), (600000, "OPA_B"))
        self.assertNotIn("locs", opa["gbp"])
        self.assertEqual(p["meta"]["data"]["how"], "uploaded")

    def test_opa_reviews_by_unit(self):
        marketing.upload("data.js", data_js(), "ceo")
        marketing.upload("loc.js", LOC_JS, "ceo")
        r = marketing.opa_reviews("OPA_B", "2026-09")
        self.assertEqual((r["value"], r["count"], r["approximate"]), (round(256 / 58, 2), 58, False))
        self.assertEqual(marketing.opa_reviews("OPA_TE", "2026-09")["value"], 3.0)
        self.assertIsNone(marketing.opa_reviews("OPA_B", "2026-10"))      # units not filled in yet, brand has 131
        self.assertIsNone(marketing.opa_reviews("OPA_9KAT", "2026-09"))   # not in loc.js
        self.assertIsNone(marketing.opa_reviews("OPA_B", "2026-11"))

    def test_a_real_month_with_no_reviews(self):
        marketing.upload("data.js", data_js(oct_units=(0, 2), oct_brand=2), "ceo")
        marketing.upload("loc.js", LOC_JS, "ceo")
        r = marketing.opa_reviews("OPA_B", "2026-10")
        self.assertEqual((r["value"], r["count"]), (None, 0))

    def test_settings_secret(self):
        s = marketing.save_settings({"client_id": "abc.access", "client_secret": "s3cret"}, "ceo")
        self.assertTrue(s["configured"] and s["client_secret_set"])
        self.assertNotIn("client_secret", s)
        marketing.save_settings({"client_secret": ""}, "ceo")
        self.assertTrue(marketing.settings()["client_secret_set"])
        with self.assertRaises(ValueError):
            marketing.save_settings({"url": "http://x"}, "ceo")

    def test_fetch_refused_by_cloudflare(self):
        marketing.save_settings({"client_id": "a", "client_secret": "b"}, "ceo")

        class R:
            status_code, headers, text = 302, {"Location": "https://nl360.cloudflareaccess.com/cdn-cgi/access/login"}, ""
        with patch.object(marketing.requests, "get", lambda *a, **k: R()):
            with self.assertRaisesRegex(RuntimeError, "Service Auth"):
                marketing.fetch()

    def test_fetch_with_token(self):
        marketing.save_settings({"client_id": "a", "client_secret": "b"}, "ceo")
        seen = []

        def get(url, headers=None, **k):
            seen.append((url, headers.get("CF-Access-Client-Id")))
            body = data_js() if url.endswith("data.js") else LOC_JS if url.endswith("loc.js") else "window.RHG_CHECKS = {\"total\": 5}"
            return type("R", (), {"status_code": 200, "headers": {}, "text": body})()
        with patch.object(marketing.requests, "get", get):
            self.assertEqual(marketing.fetch(), ["data.js", "loc.js", "checks.js"])
        self.assertEqual(seen[0], ("https://rhg.nl360.co/dashboard/data.js", "a"))
        self.assertEqual(marketing.public()["checks"]["total"], 5)


if __name__ == "__main__":
    unittest.main()
