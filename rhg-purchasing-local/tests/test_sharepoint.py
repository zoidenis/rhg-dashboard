"""RHG Home's navigation register read through Graph: never contacts Microsoft; every reply is a stand-in."""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sharepoint

G = "https://graph.microsoft.com/v1.0"
COLUMNS = [{"name": "Title", "displayName": "Title"}, {"name": "field_1", "displayName": "KodiDestinacionit"},
           {"name": "field_2", "displayName": "Lloji"}, {"name": "field_6", "displayName": "Prindi"},
           {"name": "field_7", "displayName": "URL"}, {"name": "field_10", "displayName": "Statusi"},
           {"name": "field_11", "displayName": "Verifikimi"}, {"name": "field_12", "displayName": "Miratuar"},
           {"name": "field_13", "displayName": "Renditja"}]


def row(title, code, url, parent="", status="Draft", verified="Po", order=10):
    return {"fields": {"Title": title, "field_1": code, "field_2": "Site", "field_6": parent,
                       "field_7": {"Url": url, "Description": url} if url else None,
                       "field_10": status, "field_11": verified, "field_12": "Jo", "field_13": order}}


ROWS = [row("OPA Bllok", "NAV-LOC-0007", "https://revoal.sharepoint.com/sites/RHGDocumentHub/x", "NAV-OPA", order=107),
        row("RHG Home", "NAV-HOME", "https://revoal.sharepoint.com/sites/RHG-Home", order=10),
        row("OPA Shkodër", "NAV-LOC-0009", None, "NAV-OPA", verified="Jo", order=109),
        row("Old", "NAV-OLD", "https://revoal.sharepoint.com/sites/Old", status="Çaktivizuar", order=5),
        row("Elsewhere", "NAV-X", "https://evil.example.com/sharepoint.com", order=60),
        row("Script", "NAV-Y", "javascript:alert(1)", order=61)]


def reply(body, status=200):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body
    return r


def graph(method, url, **_kw):
    if url.endswith("RHG-Home?$select=id,displayName,webUrl"):
        return reply({"id": "site1", "displayName": "RHG-Home", "webUrl": "https://revoal.sharepoint.com/sites/RHG-Home"})
    if url.endswith("/lists?$select=id,displayName,webUrl"):
        return reply({"value": [{"id": "l0", "displayName": "Documents"},
                                {"id": "l1", "displayName": "Regjistri Navigimit RHG", "webUrl": "https://x/Lists/R"}]})
    if "/columns" in url:
        return reply({"value": COLUMNS})
    if "/items" in url and "skiptoken" not in url:
        return reply({"value": ROWS[:3], "@odata.nextLink": f"{G}/sites/site1/lists/l1/items?skiptoken=2"})
    if "skiptoken" in url:
        return reply({"value": ROWS[3:]})
    raise AssertionError(url)


class Navigation(unittest.TestCase):
    def setUp(self):
        sharepoint._cache.update(at=0.0, value=None)
        self.addCleanup(sharepoint._cache.update, at=0.0, value=None)
        for name, value in (("GRAPH_TENANT_ID", "t"), ("GRAPH_CLIENT_ID", "c"),
                            ("SP_HOME_SITE", "revoal.sharepoint.com:/sites/RHG-Home"),
                            ("SP_NAV_LIST", "Regjistri Navigimit RHG")):
            p = patch.object(sharepoint.config, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.call = patch.object(sharepoint.graph_mail, "_call", side_effect=graph).start()
        self.addCleanup(patch.stopall)

    def test_register_is_read_in_order_with_its_rules(self):
        j = sharepoint.navigation()
        self.assertTrue(j["available"])
        self.assertEqual([x["code"] for x in j["items"]], ["NAV-HOME", "NAV-LOC-0007"])   # by Renditja, all pages
        home, bllok = j["items"]
        self.assertEqual((bllok["parent"], bllok["status"], bllok["order"]), ("NAV-OPA", "Draft", 107.0))
        self.assertEqual(home["url"], "https://revoal.sharepoint.com/sites/RHG-Home")
        reasons = {h["code"]: h["reason"] for h in j["hidden"]}
        self.assertEqual(reasons, {"NAV-LOC-0009": "not verified", "NAV-OLD": "switched off",
                                   "NAV-X": "no SharePoint address", "NAV-Y": "no SharePoint address"})
        self.assertEqual(j["list_url"], "https://x/Lists/R")

    def test_reading_is_cached_and_can_be_forced(self):
        sharepoint.navigation()
        n = self.call.call_count
        sharepoint.navigation()
        self.assertEqual(self.call.call_count, n)
        sharepoint.navigation(force=True)
        self.assertGreater(self.call.call_count, n)

    def test_last_good_reading_is_kept_when_sharepoint_fails(self):
        sharepoint.navigation()
        self.call.side_effect = lambda *a, **k: reply({"error": {"code": "accessDenied", "message": "no"}}, 403)
        j = sharepoint.navigation(force=True)
        self.assertTrue(j["available"] and j["stale"])
        self.assertIn("Sites.Selected grant", j["error"])
        sharepoint._cache.update(at=0.0, value=None)
        j = sharepoint.navigation(force=True)
        self.assertFalse(j["available"])
        self.assertIn("accessDenied", j["error"])

    def test_missing_list_and_missing_connection_are_explained(self):
        with patch.object(sharepoint.config, "SP_NAV_LIST", "Nope"):
            self.assertIn('"Nope" was not found', sharepoint.navigation()["error"])
        with patch.object(sharepoint.config, "GRAPH_CLIENT_ID", ""):
            self.assertIn("not connected", sharepoint.navigation()["error"])


class Documents(unittest.TestCase):
    """The checks made on each contract and policy file, and the reading of the libraries."""
    TODAY = __import__("datetime").date(2026, 11, 18)

    def test_library_names(self):
        self.assertEqual(sharepoint._library("Kontrata - RHG SHPK - Të nënshkruara"), ("RHG SHPK", "Të nënshkruara"))
        self.assertEqual(sharepoint._library("Politika dhe SOP - OPA – Greek Street Food - Në punë"),
                         ("OPA – Greek Street Food", "Në punë"))
        self.assertEqual(sharepoint._library("Arkiv publikimi - Politika dhe SOP - BR-SAL"), ("BR-SAL", "Arkiv"))
        self.assertEqual(sharepoint._library("Dokumente të përgjithshme"), ("", "Dokumente të përgjithshme"))

    def test_date_only_columns_are_the_tirana_day(self):
        self.assertEqual(sharepoint._day("2027-08-30T22:00:00Z"), "2027-08-31")     # summer, UTC+2
        self.assertEqual(sharepoint._day("2026-12-31T23:00:00Z"), "2027-01-01")     # winter, UTC+1
        self.assertEqual(sharepoint._day(""), "")
        self.assertEqual(sharepoint._day("not a date"), "")

    def contract(self, **kw):
        x = {"contract_id": "C1", "counterparty": "Furnitor", "type": "Furnizim", "scope": ["LOC-0001 | SALT Tirana"],
             "start": "2026-01-01", "end": "2027-12-31", "notice_deadline": "2027-11-30", "owner": "F. Kuka",
             "signed": "2026-01-01", "status": "Aktiv", "open_ended": False}
        x.update(kw)
        return x

    def test_complete_signed_contract_has_nothing_to_report(self):
        x = sharepoint._contract_checks(self.contract(), "Të nënshkruara", self.TODAY)
        self.assertEqual((x["missing"], x["flags"]), ([], []))

    def test_draft_in_signed_library_and_missing_fields(self):
        x = sharepoint._contract_checks(self.contract(status="Draft", counterparty="", owner="", end="",
                                                      notice_deadline="", scope=[]), "Të nënshkruara", self.TODAY)
        self.assertEqual(x["missing"], ["Counterparty", "Brand / location", "End date", "Notice deadline", "Owner"])
        self.assertEqual([f["text"] for f in x["flags"]], ["In a signed library but its status is Draft"])

    def test_open_ended_needs_no_end_or_notice(self):
        x = sharepoint._contract_checks(self.contract(open_ended=True, end="", notice_deadline=""), "Të nënshkruara",
                                        self.TODAY)
        self.assertEqual(x["missing"], [])

    def test_notice_and_end_deadlines(self):
        soon = sharepoint._contract_checks(self.contract(notice_deadline="2026-12-10"), "Të nënshkruara", self.TODAY)
        self.assertEqual((soon["days_to_notice"], soon["flags"][0]["level"]), (22, "critical"))
        later = sharepoint._contract_checks(self.contract(notice_deadline="2027-01-10"), "Të nënshkruara", self.TODAY)
        self.assertEqual(later["flags"][0], {"level": "warning", "text": "Notice deadline in 53 days"})
        passed = sharepoint._contract_checks(self.contract(notice_deadline="2026-11-01"), "Të nënshkruara", self.TODAY)
        self.assertEqual(passed["flags"][0]["text"], "Notice deadline passed 17 days ago")
        ended = sharepoint._contract_checks(self.contract(end="2026-10-31", notice_deadline="2026-09-30"),
                                            "Të nënshkruara", self.TODAY)
        self.assertEqual([f["text"] for f in ended["flags"]], ["Ended 18 days ago but still Aktiv"])
        closed = sharepoint._contract_checks(self.contract(status="Skaduar", end="2026-10-31",
                                                           notice_deadline="2026-09-30"), "Të nënshkruara", self.TODAY)
        self.assertEqual(closed["flags"], [])

    def test_signed_contract_left_in_work_library(self):
        x = sharepoint._contract_checks(self.contract(status="Nënshkruar"), "Në punë", self.TODAY)
        self.assertIn("Status Nënshkruar but still in Në punë", [f["text"] for f in x["flags"]])
        self.assertNotIn("Signature date", x["missing"])

    def test_policy_checks(self):
        x = sharepoint._policy_checks({"code": "SOP-1", "category": "CAT", "status": "Publikuar", "owner": "A",
                                       "department": "DEP", "review": "2026-10-01", "version": "0.3",
                                       "approval": "Pending", "expiry": ""}, "Të publikuara", self.TODAY)
        self.assertEqual(x["missing"], [])
        self.assertEqual([f["text"] for f in x["flags"]], ["Draft version 0.3", "In a published library, approval Pending",
                                                          "Review date passed (2026-10-01)"])
        y = sharepoint._policy_checks({"version": "4.0", "approval": ""}, "Në punë", self.TODAY)
        self.assertEqual(len(y["missing"]), 6)
        self.assertEqual(y["flags"], [])

    def test_person_columns_resolve_to_names(self):
        people = {"7": "Fabian Kuka"}
        self.assertEqual(sharepoint._value({"OwnerLookupId": "7"}, "Owner", "personOrGroup", people), "Fabian Kuka")
        self.assertEqual(sharepoint._value({"OwnerLookupId": ["7", "9"]}, "Owner", "personOrGroup", people),
                         "Fabian Kuka, set")
        self.assertEqual(sharepoint._value({}, "Owner", "personOrGroup", people), "")
        self.assertEqual(sharepoint._value({"Scope": ["A", "", None]}, "Scope", "choice", people), ["A"])


if __name__ == "__main__":
    unittest.main()
