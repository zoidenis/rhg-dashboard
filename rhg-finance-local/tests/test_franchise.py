"""A franchise (OPA Shkodër) posts nothing in RHG's BC: the controls leave it out and its items close (19 Nov 2026)."""
import pytest

import exreg
import locintel
import rcpt
import salesrec
import server
import store


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(exreg, "DB_PATH", tmp_path / "ex.db")
    monkeypatch.setattr(exreg, "_conn", None)
    monkeypatch.setattr(exreg, "_directory", lambda: {})
    monkeypatch.setattr(store, "audit", lambda *a, **k: None)
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    # other tests put a registry of their own in place: the real one here
    monkeypatch.setattr(locintel, "registry", lambda settings=None, inactive=False:
                        [r for r in locintel._registry(settings) if inactive or not r["inactive"]])
    yield


def test_opa_shkoder_is_a_franchise_in_the_default_registry():
    sk = [r for r in locintel.registry({"locations": {}}) if r["ref"] == "OPA_SK"][0]
    assert locintel.is_franchise(sk)
    assert {"OPA_SK", "OPASHKODER", "OPA SHKODER"} <= locintel.franchise_codes({"locations": {}})
    assert not [r for r in salesrec.restaurants("SALT", {"locations": {}}) if r["ref"] == "OPA_SK"]


def test_controls_leave_the_franchise_out():
    import inspect
    import billyrec
    assert "is_franchise" in inspect.getsource(rcpt.build)               # Takings POS ↔ BC
    assert "is_franchise" in inspect.getsource(billyrec)                  # Simphony ↔ Billy
    assert not [c for c in salesrec.companies({"locations": {"registry": "OPA_SK | x | company=SALT | customer=K | ownership=franchise"}})]


def test_open_items_of_the_franchise_close_and_others_stay(monkeypatch):
    monkeypatch.setattr(locintel, "franchise_codes", lambda settings=None: {"OPA_SK", "OPASHKODER", "OPA SHKODER"})
    exreg.sync("receipts_rec", "SALT", [
        {"subject": "2026-10-20 card:MC", "kind": "not_received", "location": "OPA SHKODER", "title": "OPA Shkodër 2026-10-20: 13,750 of Mastercard not received in BC", "severity": "critical", "value": 13750},
        {"subject": "2026-10-20 card:VISA", "kind": "not_received", "location": "OPA1", "title": "OPA Bllok", "severity": "critical", "value": 5000}])
    assert server.franchise_retire() == 1
    items = {x["location"]: x for x in exreg.query("all")["items"]}
    assert items["OPA SHKODER"]["status"] == "Closed" and "Franchise" in items["OPA SHKODER"]["history"][-1]["note"]
    assert items["OPA1"]["status"] == "Open"
    assert server.franchise_retire() == 0


def test_a_saved_registry_gets_the_franchise_mark_once(monkeypatch):
    saved = {"locations": {"registry": "OPA_B | OPA Bllok | company=SALT | bc=OPA1\nOPA_SK | OPA Shkodër | company=SALT | dept=OPA SHKODER"}}
    monkeypatch.setattr(store, "get_settings", lambda: saved)
    monkeypatch.setattr(store, "save_settings", lambda patch, actor="": saved["locations"].update(patch["locations"]))
    assert server.franchise_mark() is True
    assert saved["locations"]["registry"].splitlines()[1].endswith("| ownership=franchise")
    assert "ownership" not in saved["locations"]["registry"].splitlines()[0]
    saved["locations"]["registry"] = saved["locations"]["registry"].replace(" | ownership=franchise", "")
    assert server.franchise_mark() is False                     # once: a later change in Settings stays
    assert "ownership" not in saved["locations"]["registry"]
