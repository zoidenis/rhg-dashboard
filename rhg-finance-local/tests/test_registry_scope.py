"""The registry rows a location pop-up reads are cut to the person's locations like every location screen."""
import access


def _user(locs):
    return {"scope": {"companies": ["SALT"], "locations": {"SALT": locs}}}


BODY = {"company": "SALT", "locations": [
    {"ref": "SALT-TR", "name": "Salt Tirana", "bc": "SALT1", "dept": "D-SALT1", "company": "SALT"},
    {"ref": "SALT-DR", "name": "Salt Durres", "bc": "SALT2", "dept": "D-SALT2", "company": "SALT"}]}


def test_limited_person_sees_own_rows_only():
    out = access.filter_payload(BODY, _user(["SALT1"]), "SALT")
    assert [r["ref"] for r in out["locations"]] == ["SALT-TR"]


def test_everyone_sees_all_rows():
    out = access.filter_payload(BODY, {"scope": {"companies": "*"}}, "SALT")
    assert len(out["locations"]) == 2
