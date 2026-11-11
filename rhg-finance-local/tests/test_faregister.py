"""Fixed assets register: every asset at the period's end, its depreciation and the checks (faregister.py)."""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import faregister
import locintel

locintel.registry = lambda settings=None: [{"ref": "R1", "name": "OPA Blloku", "company": "SALT", "bc": "OPA1"}]
P = (date(2026, 10, 1), date(2026, 10, 31), None, None, None, None, False, "October 2026")
S = {"books": {"FISCAL": 50, "OTHER": 1}, "entries": 51, "assets": {
    # bought in 2025, depreciated every month 100: Oct 2026 posted
    "FA1": {"desc": "Oven", "class": "EQUIP", "loc": "OPA1", "b": {"FISCAL": {
        "a": {"2025-01": 2000}, "d": {f"2026-{m:02d}": -100 for m in range(1, 11)} | {"2025-12": -1000}}}},
    # depreciated until September, nothing in October: missed
    "FA2": {"desc": "Fridge", "class": "EQUIP", "loc": "OPA1", "b": {"FISCAL": {
        "a": {"2026-02": 1200}, "d": {"2026-03": -100, "2026-09": -100}}}},
    # never depreciated
    "FA3": {"desc": "Chairs", "class": "FURN", "loc": "", "b": {"FISCAL": {"a": {"2026-05": 500}}}},
    # fully depreciated
    "FA4": {"desc": "Laptop", "class": "IT", "loc": "HQ", "b": {"FISCAL": {"a": {"2023-01": 900}, "d": {"2025-06": -900}}}},
    # disposed in 2024: off the register
    "FA5": {"desc": "Old van", "class": "VEH", "loc": "", "b": {"FISCAL": {"a": {"2020-01": 5000}, "ax": {"2024-03": -5000},
                                                                       "d": {"2021-01": -1000}, "dx": {"2024-03": 1000}}}},
    # bought after the period
    "FA6": {"desc": "New sign", "class": "FURN", "loc": "OPA1", "b": {"FISCAL": {"a": {"2026-11": 700}}}}}}


def test_every_asset_in_use_with_cost_depreciation_and_nbv():
    r = faregister.register(S, "SALT", P, "ALL", {}, today=date(2026, 11, 11))
    rows = {x[0]: dict(zip(r["columns"], x)) for x in r["rows"]}
    assert set(rows) == {"FA1", "FA2", "FA3", "FA4"}
    assert rows["FA1"]["nbv"] == 0 and rows["FA1"]["acc_dep"] == 2000 and rows["FA1"]["dep_ytd"] == 1000
    assert rows["FA1"]["location_name"] == "OPA Blloku" and rows["FA1"]["dep_month"] == 100
    assert rows["FA2"]["nbv"] == 1000 and rows["FA4"]["status"] == "fully"
    t = r["totals"]
    assert t["assets"] == 4 and t["cost"] == 2000 + 1200 + 500 + 900 and t["dep_ytd"] == 1000 + 200


def test_depreciation_checks():
    c = faregister.register(S, "SALT", P, "ALL", {}, today=date(2026, 11, 11))["checks"]
    assert c["missed"] == ["FA2"] and c["never"] == ["FA3"] and set(c["fully"]) == {"FA1", "FA4"}


def test_month_not_ended_has_no_missed_check():
    c = faregister.register(S, "SALT", P, "ALL", {}, today=date(2026, 10, 20))["checks"]
    assert c["missed"] == []


def test_one_asset_month_by_month():
    a = faregister.asset(S, "FA2", P)
    assert a["cost"] == 1200 and a["nbv"] == 1000 and a["history"][0][0] == "2026-02"
    assert a["history"][-1][7] == 1000


def test_one_month():
    m = faregister.month(S, "2026-09")
    assert {r[0] for r in m["rows"]} == {"FA1", "FA2"}
