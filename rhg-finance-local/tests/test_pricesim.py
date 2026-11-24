"""Sales → Price simulator (pricesim.py): the suggested price by class, channel and competitors; the effect of a new
price; the competitors' price files; the base by channel (Geri, 24 Nov 2026)."""
import io

import pytest

import chanprof
import menueng
import pricesim
import simphony

P = dict(pricesim.DEFAULTS)


def test_star_small_rise_rounded_up():
    p, why = pricesim.suggest("star", 500, 120, 20, P=P, step=10)
    assert p == 520 and "Star" in why[0]                     # 500 × 1.04 = 520


def test_plowhorse_towards_the_cost_target_one_step_at_a_time():
    # cost 200 without VAT, target 32% → 625 without VAT = 750 with 20% VAT; at most 8% at once: 600 → 648 → 650
    p, why = pricesim.suggest("plowhorse", 600, 200, 20, target=32, P=P, step=10)
    assert p == 650 and any("cost target" in w for w in why)


def test_puzzle_dearer_than_competitors_is_lowered():
    p, _ = pricesim.suggest("puzzle", 700, 100, 20, comps=[560, 600, 640], P=P, step=10)
    assert p == 640                                          # median 600 + 5% = 630, but at most 8% at once: 644 → 640
    p2, _ = pricesim.suggest("puzzle", 600, 100, 20, comps=[600], P=P, step=10)
    assert p2 == 600


def test_competitor_cap_and_cost_floor():
    p, why = pricesim.suggest("star", 500, 120, 20, comps=[480, 500, 505], P=P, step=10)
    assert p == 520 and not any("Capped" in w for w in why)  # median 500 + 5% = 525: 520 stays under it
    p, _ = pricesim.suggest("star", 500, 120, 20, comps=[470, 480, 490], P=P, step=10)
    assert p == 500                                          # capped at 480 × 1.05 = 504: less than half a step, hold


def test_delivery_keeps_the_margin_of_eating_in():
    # eat in 500 with VAT = 416.67 without; Wolt 30% on the sales with VAT: a = 1/1.2 − 0.3 = 0.5333 → 781; at most 8%
    p, why = pricesim.suggest("star", 500, 120, 20, k=0.30, base="vat", eat_ex=500 / 1.2, P=P, step=10)
    assert p == 540 and any("Delivery" in w for w in why)


def test_effect_of_a_price():
    e = pricesim.effect(100, 500, 550, 150, 20, el=-1.0)
    assert round(e["new_qty"], 2) == round(100 * (550 / 500) ** -1, 2)
    assert round(e["cm_unit"], 2) == round(500 / 1.2 - 150, 2)
    assert e["be_pct"] < 0                                   # a dearer price can lose some items and keep the margin


def test_parse_csv_and_match():
    data = "Competitor;Our brand;Channel;Item;Price;Our item\nX;OPA;DE-Wolt;Pita gyros pule;450;\nX;OPA;;Something else;300;10025033\nX;SALT;;Pizza;900;\n".encode()
    rows = pricesim.parse("x.csv", data)
    assert [r["channel"] for r in rows] == ["DE-WOLT", "", ""] and rows[0]["price"] == 450
    items = [{"mi": 10025002, "name": "Pita Gyros Pule"}, {"mi": 10025033, "name": "Truffle Gyros"}]
    m, unmatched = pricesim.match(rows, items, ["OPA"])
    assert [r["price"] for r in m[10025002]] == [450] and [r["price"] for r in m[10025033]] == [300]
    assert unmatched == []                                   # SALT's row is another brand, left out


def test_template_reads_back():
    rows = pricesim.parse("t.xlsx", pricesim.template_xlsx())
    assert len(rows) == 2 and rows[0]["channel"] == "DE-WOLT"


@pytest.fixture
def base(tmp_path, monkeypatch):
    monkeypatch.setattr(pricesim, "COMP_FILE", tmp_path / "c.json")
    monkeypatch.setattr(pricesim, "PARAMS_FILE", tmp_path / "p.json")
    monkeypatch.setattr(pricesim, "SCEN_FILE", tmp_path / "s.json")
    monkeypatch.setattr(pricesim.store, "audit", lambda *a, **k: None)
    an = {"title": "OPA", "level": "brand", "key": "OPA", "currency": "ALL", "converted": False, "settings": {"target": 32.0},
          "selection": [{"ref": "OPA_B", "name": "OPA Bllok", "currency": "ALL"}], "brands": ["OPA"], "locations": [],
          "labels": menueng.LABEL, "period": {"from": "2026-11-01", "to": "2026-11-30", "days": 30}, "read_at": "x",
          "items": [{"mi": 1, "name": "Pita Gyros Pule", "section": "PITA", "family": "PITA", "major": "FOOD", "cls": "star",
                     "cost": 120.0, "qty": 100, "combo_qty": 0, "net": 41667, "emc_incl": 500.0}]}
    monkeypatch.setattr(menueng, "analyse", lambda *a, **k: an)
    monkeypatch.setattr(menueng, "items_by_order_type", lambda ref, a, b: [("2026-11-02", 1, 1, 33333.3, 80, 0), ("2026-11-02", 2, 1, 7000, 20, 0)])
    monkeypatch.setattr(simphony, "order_type_dims", lambda ref: {1: "Eat in", 2: "DE-Wolt"})
    monkeypatch.setattr(chanprof, "load_config", lambda: {"vat_pct": 20, "channels": {"DE-WOLT": {"commission_pct": 30, "commission_base": "vat"}}})
    ds = {"locations": [{"ref": "OPA_B", "name": "OPA Bllok", "brand": "OPA", "currency": "ALL"}],
          "rows": {"OPA_B": [{"mi": 1, "vat": 20.0}]}, "period": an["period"], "errors": []}
    return ds


def test_build_by_channel(base):
    pricesim.import_competitors("c.csv", "Competitor,Our brand,Channel,Item,Price\nY,OPA,DE-Wolt,Pita gyros pule,470\n".encode(), "geri")
    r = pricesim.build(base, "brand", "OPA")
    assert r["eat_in"] == "EAT IN" and {c["channel"]: c["delivery"] for c in r["channels"]} == {"EAT IN": False, "DE-WOLT": True}
    ch = r["items"][0]["channels"]
    assert ch["EAT IN"]["price"] == 500.0 and ch["DE-WOLT"]["price"] == 420.0
    assert ch["EAT IN"]["suggested"] == 520                  # Star: +4%
    # Wolt: parity would need 781, the step allows 8%: 453.6 → 460, under the competitor cap (470 + 5%)
    assert ch["DE-WOLT"]["suggested"] == 460 and ch["DE-WOLT"]["comp_median"] == 470
    assert r["competitors"]["matched"] == 1


def test_scenarios(base):
    s = pricesim.save_scenario({"name": "Wolt +8%", "changes": {"1|DE-WOLT": 460}}, "geri")
    assert pricesim.load_scenarios()[0]["changes"] == {"1|DE-WOLT": 460.0}
    with pytest.raises(ValueError):
        pricesim.delete_scenario(s["id"], "other")
    assert pricesim.delete_scenario(s["id"], "geri")
    with pytest.raises(ValueError):
        pricesim.save_scenario({"name": "empty", "changes": {}}, "geri")


def test_no_suggestion_that_earns_less_at_the_elasticity():
    # a Dog with a cheap cost: 8% dearer at −1.68 sells ~12% fewer and earns less margin: hold
    p, why = pricesim.suggest("dog", 800, 149, 20, k=0.30, base="vat", target=32, eat_ex=800 / 1.2, P=P, step=10, el=-1.68)
    e0, e1 = pricesim.effect(1, 800, 800, 149, 20, 0.3, "vat", -1.68), pricesim.effect(1, 800, 870, 149, 20, 0.3, "vat", -1.68)
    assert e1["new_cm"] < e0["new_cm"] and p == 800 and "elasticity" in why[-1]
    # a Star at −0.6: +4% earns more, kept
    assert pricesim.suggest("star", 500, 120, 20, P=P, step=10, el=-0.6)[0] == 520
