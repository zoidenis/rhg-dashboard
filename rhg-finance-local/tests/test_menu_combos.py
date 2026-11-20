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


def test_standard_cost_of_a_recipe_and_the_bc_item_of_the_pos_import(tmp_path, monkeypatch):
    m = {"items": {"10025033": {"Unit_Cost": 121.51, "Standard_Cost": 123.07866, "Base_Unit_of_Measure": "COPE",
                                "VAT_Prod_Posting_Group": "TVSH20"},
                   "A2405": {"Unit_Cost": 80.86, "Standard_Cost": 0, "Base_Unit_of_Measure": "COPE"},
                   "A2537": {"Unit_Cost": 333.33, "Base_Unit_of_Measure": "KG"},
                   "A7000": {"Unit_Cost": 14.23, "Base_Unit_of_Measure": "COPE"},
                   "20015001": {"Unit_Cost": 50.62, "Base_Unit_of_Measure": "COPE"}},
         "bom": [["10025033", 1, "Item", "A2537", "", False, 0.205, "KG"], ["10025033", 2, "Item", "A7000", "", False, 1, "COPE"],
                 ["A2405", 1, "Item", "A2537", "", False, 0.2, "KG"]],
         "uom": []}
    cost = menueng._coster(m)
    assert cost("10025033") == (123.08, "Standard cost (Assembly BOM)")
    assert cost.recipe("10025033") == round(0.205 * 333.33 + 14.23, 2)
    assert cost("A2405") == (round(0.2 * 333.33, 2), "Recipe")            # no Standard Cost: the recipe
    assert cost("20015001") == (50.62, "Item cost") and cost.recipe("20015001") is None
    assert cost("10025002")[1] == "Not in BC"
    assert menueng.vat_pct(m["items"]["10025033"]) == 20 and menueng.vat_pct({"VAT_Prod_Posting_Group": "PERJASHTUAR"}) is None

    class Client:
        def _url(self, company, e): return e
        def _get_all(self, url, params, page_size=None):
            return [{"Menu_Item_Number": "10025002", "ItemNo": "A2405", "TransactionDateArch": "2026-11-12"},
                    {"Menu_Item_Number": "10025033", "ItemNo": "10025033", "TransactionDateArch": "2026-11-12"}]
    monkeypatch.setattr(menueng, "MAP_FILE", tmp_path / "map.json")
    monkeypatch.setattr(menueng, "_bcmap", {})
    mp = menueng.bc_map(Client(), "SALT", "2026-11-01", "2026-11-19")
    assert mp["10025002"] == "A2405"
    assert cost(mp["10025002"])[1] == "Recipe"                          # 10025002 is costed through A2405


def test_margin_on_the_emc_price_and_the_read_me(monkeypatch):
    d = ds(1.0)
    for r in d["rows"]["OPA_B"]:
        r.update(emc=470.0, vat=20.0, emc_ex=470 / 1.2, bc_item=str(r["mi"]), std_cost=r["unit_cost"], recipe_cost=None)
    r = menueng.analyse(d, settings={})
    it = [i for i in r["items"] if i["mi"] == 10025033][0]
    assert it["price_source"] == "EMC" and round(it["price"], 2) == 391.67 and round(it["cost_pct"]) == 31
    assert round(it["avg_price"]) == 229                               # what came in per unit, beside it
    import io, openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(menueng.render_xlsx(r)))
    head = [c.value for c in wb["Items"][1]]
    assert "EMC price incl. VAT ALL" in head and "Cost basis" in head and "BC item" in head
    readme = "\n".join(str(c.value) for c in wb["Read me"]["A"])
    assert "Menu mix %: Items sold of the item / Items sold of every item of its section × 100." in readme
