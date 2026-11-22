"""Document control in Purchasing (moved from Finance, 22 Nov 2026): Invoices without PO with this register's
follow-up, Transfers in transit read through the Finance tower with both registers' follow-up."""
import unittest
from unittest import mock

import requests

import exreg
import server


class Resp:
    def __init__(self, code, body):
        self.status_code, self._b = code, body
        self.ok = code < 400

    def json(self):
        return self._b


REG = {"items": [
    {"subject": "transit-SALT-TS1", "status": "Open", "owner": "Stock", "due": "2026-11-25", "id": 7},
    {"subject": "transit-SALT-TS2", "status": "Closed", "owner": "Stock", "due": "", "id": 8},
    {"subject": "nopo-SALT-PI9", "status": "In progress", "owner": "Coord", "due": "2026-11-30", "id": 9}]}


class Transit(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def get(url, params=None, timeout=None, headers=None):
            self.calls.append((url, params))
            return Resp(200, {"available": True, "company": "SALT",
                              "transfers": [{"shipment": "TS1"}, {"shipment": "TS2"}, {"shipment": "TS3"}],
                              "followup": {"transit-SALT-TS3": {"status": "Open", "owner": "Finance", "due": "", "id": 1}}})
        self.p = [mock.patch.object(requests, "get", get), mock.patch.object(exreg, "query", lambda *a, **k: REG)]
        for x in self.p:
            x.start()

    def tearDown(self):
        for x in self.p:
            x.stop()

    def test_read_through_finance_with_both_registers(self):
        out = server.transit_screen("SALT")
        self.assertTrue(self.calls[0][0].endswith("/internal/transit"))
        self.assertNotIn("locations", self.calls[0][1])
        # tracked earlier in Finance (TS3) and here since (TS1); a closed item is not a follow-up
        self.assertEqual(set(out["followup"]), {"transit-SALT-TS1", "transit-SALT-TS3"})

    def test_the_scope_goes_to_finance(self):
        server.transit_screen("SALT", True, {"VESPER", "SALT GC"})
        self.assertEqual(self.calls[0][1], {"company": "SALT", "refresh": "1", "locations": "SALT GC,VESPER"})

    def test_finance_unreachable(self):
        def down(*a, **k):
            raise requests.ConnectionError("no route")
        with mock.patch.object(requests, "get", down):
            out = server.transit_screen("SALT")
        self.assertFalse(out["available"])
        self.assertIn("Finance", out["error"])


class Nopo(unittest.TestCase):
    def test_followup_of_this_register(self):
        reading = {"available": True, "company": "SALT", "invoices": [{"invoice": "PI9"}, {"invoice": "PI10"}]}
        with mock.patch.object(server, "compliance_screen", lambda c, m: reading), \
                mock.patch.object(exreg, "query", lambda *a, **k: REG):
            out = server.nopo_screen("SALT", "2026-10")
        self.assertEqual(out["followup"], {"nopo-SALT-PI9": {"status": "In progress", "owner": "Coord", "due": "2026-11-30"}})


class Access(unittest.TestCase):
    def test_transit_is_cut_by_its_handler(self):
        self.assertIn("/api/transit", server.ACCESS_SELF_CUT)
        self.assertEqual(server.WRITE_PERMISSIONS["/api/control/nopo/track"], "actions")


if __name__ == "__main__":
    unittest.main()
