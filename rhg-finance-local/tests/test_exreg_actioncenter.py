"""The registers under the Executive Action Center: SLA deadlines from its matrix, re-dating, its list."""
from datetime import date, timedelta

import pytest

import exreg
import store

SLA = {"sla": {"Finance": {"critical": 1, "high": 3, "medium": 6, "low": 9},
               "Warehouse": {"critical": 4, "high": 8, "medium": 12, "low": 20}}, "combined_digest": True}


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(exreg, "DB_PATH", tmp_path / "ex.db")
    monkeypatch.setattr(exreg, "_conn", None)
    monkeypatch.setattr(exreg, "_directory", lambda: {})
    monkeypatch.setattr(exreg, "policy", lambda max_age=300: SLA)
    monkeypatch.setattr(store, "audit", lambda *a, **k: None)
    yield


def d(n):
    return (date.today() + timedelta(days=n)).isoformat()


def test_due_dates_follow_the_matrix_by_department_and_severity():
    exreg.sync("negative_stock", "SALT", [{"subject": "A", "kind": "stock", "title": "A", "severity": "critical", "value": 1},
                                          {"subject": "B", "kind": "cost", "title": "B", "severity": "warning", "value": 1}])
    got = {x["subject"]: x for x in exreg.query("all")["items"]}
    assert got["A"]["role"] == "stock_manager" and got["A"]["due"] == d(4)     # Warehouse · critical
    assert got["B"]["role"] == "cost_controller" and got["B"]["due"] == d(3)   # Finance · high


def test_without_the_center_the_register_keeps_its_own_sla(monkeypatch):
    monkeypatch.setattr(exreg, "policy", lambda max_age=300: None)
    exreg.sync("ap_ar", "SALT", [{"subject": "X", "title": "X", "severity": "critical", "value": 1}])
    assert exreg.query("all")["items"][0]["due"] == d(2)


def test_apply_sla_leaves_a_deadline_set_by_hand():
    exreg.sync("ap_ar", "SALT", [{"subject": "X", "title": "X", "severity": "info", "value": 1},
                                 {"subject": "Y", "title": "Y", "severity": "info", "value": 1}])
    x = next(i for i in exreg.query("all")["items"] if i["subject"] == "X")
    exreg.update(x["id"], {"due": d(30)}, "cfo")
    SLA["sla"]["Finance"]["low"] = 2
    try:
        assert exreg.apply_sla() == 1
        got = {i["subject"]: i for i in exreg.query("all")["items"]}
        assert got["X"]["due"] == d(30) and got["Y"]["due"] == d(2)
    finally:
        SLA["sla"]["Finance"]["low"] = 9


def test_the_center_list_has_labels_and_recent_closures():
    exreg.sync("ap_ar", "SALT", [{"subject": "X", "title": "X", "severity": "warning", "value": 5}])
    exreg.sync("ap_ar", "SALT", [])                       # no longer found: closed as verified
    exreg.sync("gl", "SALT", [{"subject": "G", "title": "G", "severity": "critical", "value": 1}])
    r = exreg.for_action_center()
    st = {x["subject"]: x["status"] for x in r["items"]}
    assert st == {"X": "Closed", "G": "Open"}
    assert r["tower"] and "ap_ar" in r["sources"] and set(r["items"][0]) >= {"role", "owner", "due", "history", "overdue"}


def test_owner_from_a_role_of_administration_by_access(monkeypatch):
    pol = dict(SLA, owner_rules={f"{exreg.TOWER['name']}|negative_stock|stock": "admin:Sales analyst"},
               admin_people={"Sales analyst": [{"email": "all@rhg.al", "scope": {"companies": "*", "locations": {}}},
                                               {"email": "opa@rhg.al", "scope": {"companies": ["SALT"], "locations": {"SALT": ["OPA1"]}}},
                                               {"email": "tbm@rhg.al", "scope": {"companies": ["TBM"], "locations": {}}}]},
               admin_departments={"Sales analyst": "Operations"})
    pol["sla"] = dict(SLA["sla"], Operations={"critical": 6, "high": 9, "medium": 12, "low": 15})
    monkeypatch.setattr(exreg, "policy", lambda max_age=300: pol)
    role, owner = exreg.owner_of("negative_stock", "stock", "SALT", "OPA1")
    assert role == "admin:Sales analyst" and owner == "all@rhg.al, opa@rhg.al"
    assert exreg.owner_of("negative_stock", "stock", "SALT", "LIFT")[1] == "all@rhg.al"
    assert exreg.owner_of("negative_stock", "cost", "SALT", "OPA1")[0] == "cost_controller"      # not overridden
    exreg.sync("negative_stock", "SALT", [{"subject": "A", "kind": "stock", "location": "OPA1", "title": "A", "severity": "critical", "value": 1}])
    assert exreg.query("all")["items"][0]["due"] == d(6)                                          # Operations · critical


def test_apply_hands_open_items_to_the_matrix_owner(monkeypatch):
    exreg.sync("ap_ar", "SALT", [{"subject": "X", "title": "X", "severity": "warning", "value": 1}])
    pol = dict(SLA, owner_rules={f"{exreg.TOWER['name']}|ap_ar|*": "admin:CFO"},
               admin_people={"CFO": [{"email": "cfo@rhg.al", "scope": {"companies": "*", "locations": {}}}]},
               admin_departments={"CFO": "Executive"})
    monkeypatch.setattr(exreg, "policy", lambda max_age=300: pol)
    assert exreg.apply_sla() == 1
    x = exreg.query("all")["items"][0]
    assert (x["role"], x["owner"]) == ("admin:CFO", "cfo@rhg.al")
