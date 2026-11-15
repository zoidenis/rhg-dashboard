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
