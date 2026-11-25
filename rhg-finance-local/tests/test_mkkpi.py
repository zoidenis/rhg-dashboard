"""Marketing → Campaigns (mkkpi.py): nl360 against Simphony's sales (Geri, 25 Nov 2026)."""
import pytest

import locintel
import mkkpi
import simphony

MONTHS = ["2026-09", "2026-10"]


def day(net, checks=10, guests=12):
    return {"rvcs": [{"netSlsTtl": net, "chkCnt": checks, "gstCnt": guests}], "final": True}


@pytest.fixture
def held(monkeypatch):
    # daily net: OPA_B 1,000 this year and 800 last year; SALT 2,000 and 2,000 (the rest of RHG flat)
    def h(ref, d):
        ly = d.startswith("2025")
        return day({"OPA_B": 800 if ly else 1000, "SALT": 2000}[ref], 10 if ly else 12)
    monkeypatch.setattr(simphony, "held_day", h)
    monkeypatch.setattr(simphony, "day_figures", lambda rvcs: {"net": rvcs[0]["netSlsTtl"], "checks": rvcs[0]["chkCnt"], "guests": rvcs[0]["gstCnt"]})
    monkeypatch.setattr(locintel, "registry", lambda settings=None: [{"ref": "OPA_B", "brand": "OPA", "name": "OPA Bllok"},
                                                                     {"ref": "SALT", "brand": "SALT", "name": "SALT Tirana"}])
    monkeypatch.setattr(mkkpi.menueng, "_rates", lambda s: ("ALL", {"ALL": 1.0, "EUR": 100.0}))
    monkeypatch.setattr(mkkpi, "read_behind", lambda missing: None)


PUB = {"available": True, "months": MONTHS, "brands": [
    {"id": "opa", "name": "OPA", "ads": [{"metaSpend": 200, "gSpend": 100, "metaClicks": 900, "gClicks": 100, "res": 20, "gDirs": 50, "estRev": 3000}] * 2,
     "gbp": {"total": [400, 500], "reviews": {"n": [6, 9]}},
     "locs": [{"name": "OPA Blloku", "slug": "blloku", "till": "OPA_B", "ads": [{"metaSpend": 200, "gSpend": 100}] * 2}]},
    {"id": "salt", "name": "SALT Tirana", "ads": [], "gbp": None, "locs": []},
    {"id": "vesper", "name": "Vesper", "ads": [], "locs": []}]}


def test_brands_and_units_are_matched_to_simphony(held):
    ents, unmatched = mkkpi.entities(PUB, locintel.registry())
    assert [(e["id"], e["refs"]) for e in ents] == [("opa", ["OPA_B"]), ("opa/blloku", ["OPA_B"]), ("salt", ["SALT"]), ("vesper", [])]
    assert unmatched == ["Vesper"]


def test_the_kpis_and_the_verdict(held):
    r = mkkpi.build(PUB, {})
    opa = next(e for e in r["entities"] if e["id"] == "opa")["months"][1]          # October: 31 days
    assert opa["sales"] == 31000 and opa["sales_ly"] == 24800 and opa["growth"] == 25.0
    assert opa["group_growth"] == 0.0 and opa["lift"] == 25.0 and opa["verdict"] == "working"
    assert opa["spend"] == 30000 and opa["mkt_pct"] == round(30000 / 31000 * 100, 2)      # 300 € × 100
    assert opa["cost_guest"] == round(30000 / (31 * 12), 2) and opa["checks_growth"] == 20.0
    assert opa["cpc"] == 30.0 and opa["cost_res"] == 1500.0 and opa["res_rate"] == 2.0 and opa["est_roas"] == 10.0
    assert opa["inter_100"] == round(500 / 372 * 100, 2) and opa["reviews_1000"] == round(9 / 372 * 1000, 2)
    salt = next(e for e in r["entities"] if e["id"] == "salt")["months"][1]
    assert salt["verdict"] == "no_ads" and salt["growth"] == 0.0


def test_no_rate_no_lek(held, monkeypatch):
    monkeypatch.setattr(mkkpi.menueng, "_rates", lambda s: ("ALL", {"ALL": 1.0}))
    r = mkkpi.build(dict(PUB, fx_rate=99.0), {})
    assert r["rate"] == 99.0 and r["rate_source"] == "nl360's rate"
