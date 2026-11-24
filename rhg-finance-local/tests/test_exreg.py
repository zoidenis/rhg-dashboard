"""The Exceptions register holds the Action center and the screens' rules too (exreg.py)."""
import pytest

import exreg
import recipients
import store


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(exreg, "DB_PATH", tmp_path / "ex.db")
    monkeypatch.setattr(exreg, "_conn", None)
    monkeypatch.setattr(exreg, "_directory", lambda: {})
    monkeypatch.setattr(store, "audit", lambda *a, **k: None)
    yield


def test_track_opens_a_manual_item_owned_by_who_tracks():
    x = exreg.track({"title": "Check the rent", "company": "SALT", "source_exception": "k1", "priority": "High"},
                    "geri", "geri@rhg.al")
    assert x["manual"] and x["status"] == "Open" and x["owner"] == "geri@rhg.al" and x["severity"] == "warning"
    assert "k1" in exreg.tracked_keys("SALT")


def test_tracking_again_hands_over_the_same_item():
    a = exreg.track({"title": "T", "company": "SALT", "source_exception": "k1"}, "geri", "geri@rhg.al")
    b = exreg.track({"title": "T", "company": "SALT", "source_exception": "k1", "owner": "ana@rhg.al"}, "geri")
    assert a["id"] == b["id"] and b["owner"] == "ana@rhg.al"
    assert len(exreg.query("all")["items"]) == 1


def test_tracking_a_rule_already_found_hands_over_the_rule_item():
    exreg.sync("ap_ar", "SALT", [{"subject": "ap-old-5", "title": "Old balance", "severity": "critical", "value": 9}])
    x = exreg.track({"title": "Old balance", "company": "SALT", "source_exception": "ap-old-5", "owner": "ana@rhg.al"}, "geri")
    assert x["source"] == "ap_ar" and x["owner"] == "ana@rhg.al"


def test_manual_item_is_closed_by_its_owner_not_by_a_run():
    x = exreg.track({"title": "T", "company": "SALT"}, "geri", "geri@rhg.al")
    exreg.sync("manual", "SALT", [])          # no control syncs manual items; even if one did, nothing is found
    with pytest.raises(ValueError):
        exreg.update(x["id"], {"status": "Awaiting verification"}, "geri")
    y = exreg.update(x["id"], {"status": "Closed", "comment": "paid"}, "geri")
    assert y["status"] == "Closed" and y["closed_by"] == "geri"


def test_rule_item_cannot_be_closed_by_hand():
    exreg.sync("gl", "SALT", [{"subject": "gl-manual-1", "title": "Manual entry", "severity": "warning"}])
    eid = exreg.query("all")["items"][0]["id"]
    with pytest.raises(ValueError):
        exreg.update(eid, {"status": "Closed"}, "geri")


def test_rule_extra_is_kept_for_the_email():
    exreg.sync("ap_ar", "SALT", [{"subject": "ap-debit-2", "title": "Debit", "extra": {"rule": "Debit balance", "area": "AP"}}])
    assert exreg.query("all")["items"][0]["extra"]["rule"] == "Debit balance"


def test_actions_move_in_once_with_status_and_history():
    acts = [{"id": "a1", "title": "Call supplier", "company": "SALT", "status": "Resolved", "owner": "ana@rhg.al",
             "priority": "Critical", "due_date": "2026-10-01", "created_at": "2026-09-20T10:00:00",
             "history": [{"ts": "2026-09-20T10:00:00", "actor": "geri", "from": "", "to": "New", "comment": "Created"}]},
            {"id": "a2", "title": "Recount", "company": "SALT", "status": "Waiting for approval", "owner": "Ana",
             "source_exception": "inv-x", "created_at": "2026-09-21T10:00:00", "history": []}]
    assert exreg.migrate_actions(acts) == 2
    assert exreg.migrate_actions(acts) == 0
    items = {x["title"]: x for x in exreg.query("all")["items"]}
    assert items["Call supplier"]["status"] == "Closed" and items["Call supplier"]["severity"] == "critical"
    assert items["Recount"]["status"] == "In progress" and "owner there: Ana" in items["Recount"]["history"][-1]["note"]
    assert exreg.tracked_keys("SALT") == {"inv-x"}


def test_due_date_can_be_moved():
    x = exreg.track({"title": "T", "company": "SALT"}, "geri", "geri@rhg.al")
    assert exreg.update(x["id"], {"due": "2026-12-01"}, "geri")["due"] == "2026-12-01"


def test_an_owner_name_is_kept_as_a_note_not_refused():
    x = exreg.track({"title": "T", "company": "SALT", "owner": "Purchasing Coordinator"}, "geri")
    assert x["history"][0]["note"].startswith("Tracked by hand") is False and "Suggested owner" in x["history"][0]["note"]


def test_a_tracked_exception_closes_when_the_data_no_longer_has_it():
    # an item tracked by hand before the rule was in the register (moved from the old Action center)
    keep = exreg.track({"title": "Still open", "company": "SALT", "source_exception": "ar-old-1"}, "geri", "geri@rhg.al")
    gone = exreg.track({"title": "Paid in BC", "company": "SALT", "source_exception": "ar-old-2"}, "geri", "geri@rhg.al")
    other = exreg.track({"title": "Not a rule", "company": "SALT", "source_exception": "k9"}, "geri", "geri@rhg.al")
    tracks = lambda s: s.startswith(("ap-", "ar-"))
    exreg.sync("ap_ar", "SALT", [{"subject": "ar-old-1", "title": "Still open", "severity": "critical", "value": 5}], tracks=tracks)
    st = {x["id"]: x["status"] for x in exreg.query("all")["items"]}
    assert st[keep["id"]] == "Open" and st[gone["id"]] == "Closed" and st[other["id"]] == "Open"
    # a run of one location never closes them
    again = exreg.track({"title": "Paid", "company": "SALT", "source_exception": "ar-old-3"}, "geri", "geri@rhg.al")
    exreg.sync("ap_ar", "SALT", [], locations=["OPA1"], tracks=tracks)
    assert {x["id"]: x["status"] for x in exreg.query("all")["items"]}[again["id"]] == "Open"


def test_the_daily_run_is_not_repeated_after_a_restart(tmp_path, monkeypatch):
    # the day was held in memory only: every restart of the tower (a deploy) within the window sent the digests again
    from datetime import datetime
    monkeypatch.setattr(exreg, "DAY_FILE", tmp_path / "exreg_daily.json")
    at = datetime.now().strftime("%H:%M")
    monkeypatch.setattr(exreg, "settings", lambda: {"controls": False, "enabled": True, "time": at})
    monkeypatch.setattr(exreg, "policy", lambda: {})
    sent = []
    monkeypatch.setattr(exreg, "send_digests", lambda: sent.append(1))

    class Stop(Exception):
        pass

    def sleep(_s):
        raise Stop
    monkeypatch.setattr(exreg.time, "sleep", sleep)
    for _restart in range(2):
        monkeypatch.setattr(exreg, "_state", {"done": None, "controls": None, "job": None})
        with pytest.raises(Stop):
            exreg.loop()
    assert sent == [1]
