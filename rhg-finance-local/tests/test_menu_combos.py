"""Menu engineering: units rung inside combo meals at no price are counted apart (20 Nov 2026)."""
import pytest

import menucost
import menueng
import store


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(menueng, "DB_PATH", tmp_path / "mi.db")
    monkeypatch.setattr(menueng, "_conn", None)
    yield


def hold(ref, rows):
    db = menueng._db()
    db.executemany("INSERT INTO miv VALUES (?,?,?,?,?,?)", rows)
    db.commit()


def test_truffle_gyros_big_like_in_bc():
    # MIDSimphonyArchive 1-19 Nov 2026, item 10025033 at five OPAs: 140 units, 38,540 VAT included at 470 a unit
    hold("OPA_B", [("OPA_B", "2026-11-01", 10025033, 38540.0, 140.0, 0.0)])
    s = menueng.priced_shares("OPA_B", "2026-11-01", "2026-11-19", prices={10025033: 470.0})
    assert round(s[10025033] * 140) == 82                     # 58 rung at 0 inside the combo
    # a unit sold at a higher price level is never taken for a combo unit; an item without a price counts in full
    hold("OPA_B", [("OPA_B", "2026-11-02", 7, 1500.0, 3.0, 0.0), ("OPA_B", "2026-11-02", 8, 0.0, 4.0, 0.0)])
    s = menueng.priced_shares("OPA_B", "2026-11-01", "2026-11-19", prices={10025033: 470.0, 7: 400.0})
    assert 7 not in s and 8 not in s


def ds(share):
    return {"locations": [{"ref": "OPA_B", "name": "OPA Bllok", "brand": "OPA", "currency": "ALL"}],
            "period": {"from": "2026-11-01", "to": "2026-11-19", "label": ""}, "read_at": "", "errors": [],
            "rows": {"OPA_B": [
                {"mi": 10025033, "name": "Truffle Chicken Gyros Big", "major": "FOOD", "family": "SIGNATURE&PITAS", "qty": 140.0,
                 "net": 32116.67, "disc": 0, "rtn": 0, "unit_cost": 122.31, "basis": "Recipe", "technical": False, "priced_share": share},
                {"mi": 10025002, "name": "Chicken Gyros", "major": "FOOD", "family": "SIGNATURE&PITAS", "qty": 300.0,
                 "net": 39875.0, "disc": 0, "rtn": 0, "unit_cost": 40.0, "basis": "Recipe", "technical": False, "priced_share": 1.0}]}}


def test_price_and_cost_of_the_units_sold_at_a_price(monkeypatch):
    monkeypatch.setattr(store, "get_settings", lambda: {})
    old = menueng.analyse(ds(1.0), settings={})
    it = [i for i in old["items"] if i["mi"] == 10025033][0]
    assert round(it["cost_pct"]) == 53                          # the figure Geri saw
    new = menueng.analyse(ds(82 / 140), settings={})
    it = [i for i in new["items"] if i["mi"] == 10025033][0]
    assert round(it["price"]) == 392 and round(it["cost_pct"]) == 31 and round(it["qty"]) == 82
    assert round(it["combo_qty"]) == 58 and round(it["combo_cost"]) == round(58 * 122.31)
    assert any("combo" in f for f in it["flags"]) and not any("53%" in f for f in it["flags"])
    assert round(new["kpis"]["in_combos"]) == 58


def test_only_in_combos_is_listed_apart(monkeypatch):
    r = menueng.analyse(ds(0.0), settings={})
    assert not [i for i in r["items"] if i["mi"] == 10025033]
    assert r["combo_only"][0]["mi"] == 10025033 and r["combo_only"][0]["combo_qty"] == 140


def test_food_cost_weeks_leave_the_combo_units_out(monkeypatch):
    db = menueng._db()
    db.executemany("INSERT INTO mio VALUES (?,?,?,?,?,?,?,?,?)",
                   [("OPA_B", "2026-11-03", 0, 10025033, 32116.67, 140.0, 0, 0, 0)])
    db.commit()
    w = menucost.weekly(ds(82 / 140), settings={})
    assert round(w["total"]["cost_pct"]) == 31                  # 82 × 122.31 / 32,117 (was 140 × 122.31: 53%)
