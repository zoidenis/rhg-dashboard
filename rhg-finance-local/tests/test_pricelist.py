"""Menu prices: active and inactive items, the tax % of each item, the same product by brand, PLU and name
(QA 21 Nov 2026)."""
import io
from datetime import date, timedelta

import pytest
from openpyxl import load_workbook

import locintel
import menueng
import pricelist
import recipes
import simphony
import store

PAST = (date.today() - timedelta(days=40)).isoformat()
LATER = (date.today() + timedelta(days=10)).isoformat()


def price(num, p, to="", fr="2025-01-01", rvc=1, level=1):
    return {"num": num, "rvc": rvc, "level": level, "level_name": "Base", "price": p, "cost": None, "from": fr, "to": to}


ACTIVE = {"OPA_B": [price(1, 470.0), price(2, 300.0)], "OPA_T": [price(1, 490.0), price(2, 300.0)],
          "SALT": [price(1, 900.0)]}
HIST = {"OPA_B": ACTIVE["OPA_B"] + [price(9, 250.0, to=PAST), price(9, 240.0, to="2025-06-01"), price(8, 99.0, fr=LATER)],
        "OPA_T": ACTIVE["OPA_T"], "SALT": ACTIVE["SALT"]}
NAMES = {"OPA_B": {1: "Truffle Gyros", 2: "Chicken Gyros", 9: "Old Pita", 8: "New Wrap"},
         "OPA_T": {1: "Truffle  gyros", 2: "Chicken Gyros Big"}, "SALT": {1: "Truffle Gyros"}}
REG = [{"ref": "OPA_B", "name": "OPA Bllok", "brand": "OPA", "company": "OPA", "currency": "ALL"},
       {"ref": "OPA_T", "name": "OPA TEG", "brand": "OPA", "company": "OPA", "currency": "ALL"},
       {"ref": "SALT", "name": "SALT", "brand": "SALT", "company": "SALT", "currency": "ALL"}]


@pytest.fixture(autouse=True)
def mocked(monkeypatch):
    monkeypatch.setattr(pricelist, "_held", {"at": 0, "data": None})
    monkeypatch.setattr(simphony, "locations", lambda: [{"locRef": r["ref"], "name": r["name"]} for r in REG])
    monkeypatch.setattr(locintel, "in_use", lambda l: True)
    monkeypatch.setattr(locintel, "registry", lambda: REG)
    monkeypatch.setattr(store, "get_settings", lambda: {})
    monkeypatch.setattr(simphony, "menu_item_prices", lambda ref, max_age=3600, history=False: (HIST if history else ACTIVE)[ref])
    monkeypatch.setattr(simphony, "menu_items", lambda ref, max_age=0: {n: {"name": v, "major": "FOOD", "family": "PITA"}
                                                                         for n, v in NAMES[ref].items()})
    monkeypatch.setattr(simphony, "rvc_dims", lambda ref: {1: "Restaurant"})
    monkeypatch.setattr(menueng, "tax_rates", lambda ref, a, b: {1: 20.0, 2: 6.0} if ref == "OPA_B" else {})
    monkeypatch.setattr(menueng, "bc_map_held", lambda co: {"1": "10025033", "2": "A2405"} if co == "OPA" else {})
    monkeypatch.setattr(recipes, "master_cached", lambda co: {"items": {"10025033": {"VAT_Prod_Posting_Group": "TVSH20"},
                                                                        "A2405": {"VAT_Prod_Posting_Group": "TVSH20"}}})
    yield


def test_active_is_the_default_and_inactive_items_are_listed_apart():
    r = pricelist.request(None)
    assert {x["num"] for x in r["rows"] if x["ref"] == "OPA_B"} == {1, 2}
    assert r["status_counts"] == {"active": 5, "inactive": 1, "future": 1}
    gone = pricelist.request(None, status="inactive")["rows"]
    # item 9 had two ended prices at the same revenue centre and level: the one that ended last is shown
    assert [(x["num"], x["price"], x["to"]) for x in gone] == [(9, 250.0, PAST)]
    assert [x["num"] for x in pricelist.request(None, status="future")["rows"]] == [8]
    assert pricelist.request(None, status="all")["count"] == 7


def test_tax_rate_of_simphony_and_of_the_bc_item():
    r = {(x["ref"], x["num"]): x for x in pricelist.request(None)["rows"]}
    assert r[("OPA_B", 1)]["tax_sim"] == 20.0 and r[("OPA_B", 1)]["vat_bc"] == 20.0
    assert r[("OPA_B", 2)]["tax_sim"] == 6.0 and r[("OPA_B", 2)]["vat_bc"] == 20.0 and r[("OPA_B", 2)]["bc_item"] == "A2405"
    assert r[("SALT", 1)]["tax_sim"] is None and r[("SALT", 1)]["vat_bc"] is None


def test_same_product_by_brand_plu_and_name():
    sp = pricelist.request(None)["spread"]
    # Truffle Gyros at two OPAs (same name once spaces and case are set aside); SALT's item 1 is another brand;
    # item 2 is named differently at OPA TEG, so it is not the same product
    assert [(x["brand"], x["num"], x["low"], x["high"], x["locations"]) for x in sp] == [("OPA", 1, 470.0, 490.0, 2)]


def test_excel_has_the_status_the_tax_and_the_comparison():
    r = pricelist.request(None, status="all", limit=0)
    wb = load_workbook(io.BytesIO(pricelist.render_xlsx(r)))
    head = [c.value for c in wb["Price list"][1]]
    assert {"Status", "Tax % Simphony", "VAT % BC item", "Tax differs"} <= set(head)
    rows = [dict(zip(head, [c.value for c in row])) for row in wb["Price list"].iter_rows(min_row=2)]
    assert any(x["Item no."] == 2 and x["Tax differs"] == "yes" for x in rows)
    assert any(x["Status"] == "Inactive (prices ended)" for x in rows)
    sp = list(wb["Same item, different prices"].iter_rows(min_row=2, values_only=True))
    assert sp[0][:3] == ("OPA", 1, "Truffle Gyros")
    pdf = load_workbook(io.BytesIO(pricelist.render_xlsx(r, pdf=True)))
    assert "Tax % Simphony" in [c.value for c in pdf["Price list"][1]]


def test_tax_rates_from_the_sales_held(tmp_path, monkeypatch):
    monkeypatch.setattr(menueng, "DB_PATH", tmp_path / "mi.db")
    monkeypatch.setattr(menueng, "_conn", None)
    monkeypatch.undo()
    monkeypatch.setattr(menueng, "DB_PATH", tmp_path / "mi.db")
    monkeypatch.setattr(menueng, "_conn", None)
    db = menueng._db()
    # 1,200 VAT included, 200 VAT → 20%; 1,060 with 60 VAT → 6%
    db.executemany("INSERT INTO miv VALUES (?,?,?,?,?,?)", [("X", "2026-11-10", 1, 1200.0, 2, 0), ("X", "2026-11-10", 2, 1060.0, 1, 0)])
    db.executemany("INSERT INTO mio VALUES (?,?,?,?,?,?,?,?,?)", [("X", "2026-11-10", 0, 1, 1000.0, 2, 0, 0, 0),
                                                                 ("X", "2026-11-10", 0, 2, 1000.0, 1, 0, 0, 0)])
    db.commit()
    assert menueng.tax_rates("X", "2026-11-01", "2026-11-20") == {1: 20.0, 2: 6.0}
