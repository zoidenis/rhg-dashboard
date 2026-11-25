"""The elasticity measured from RHG's own price changes (elastic.py)."""
import math
import sqlite3
from datetime import date, timedelta

import pytest

import elastic
import menueng
import simphony

CHANGE = "2026-06-01"


@pytest.fixture
def db(monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.executescript("""CREATE TABLE miv (ref TEXT, day TEXT, mi INTEGER, sls REAL, cnt REAL, dsc REAL, PRIMARY KEY (ref, day, mi));
                          CREATE TABLE dayv (ref TEXT, day TEXT, PRIMARY KEY (ref, day));""")
    monkeypatch.setattr(menueng, "_db", lambda: conn)
    # item 1: 300 → 330 on 1 June; item 2 unchanged (the control); item 3: one unit in a combo at no price each day
    monkeypatch.setattr(simphony, "menu_item_prices", lambda ref, max_age=3600, history=False: [
        {"num": 1, "rvc": 1, "level": 1, "price": 300.0, "from": "2025-01-01", "to": "2026-05-31"},
        {"num": 1, "rvc": 1, "level": 1, "price": 330.0, "from": CHANGE, "to": ""},
        {"num": 2, "rvc": 1, "level": 1, "price": 500.0, "from": "2025-01-01", "to": ""}])
    d0 = date.fromisoformat(CHANGE)
    rows, days = [], []
    for k in range(-28, 28):
        d = (d0 + timedelta(days=k)).isoformat()
        days.append(("R", d))
        after = k >= 0
        n1 = 90 / 28 if after else 100 / 28                     # 100 before, 90 after
        rows += [("R", d, 1, (330 if after else 300) * n1, n1 + 1, 0),   # +1 a day rung at no price
                 ("R", d, 2, 500 * (1100 if after else 1000) / 28, (1100 if after else 1000) / 28, 0)]
    conn.executemany("INSERT INTO miv VALUES (?,?,?,?,?,?)", rows)
    conn.executemany("INSERT INTO dayv VALUES (?,?)", days)
    return conn


def test_the_elasticity_of_a_price_change_against_the_control(db):
    r = elastic.measure(["R"], [1, 2], {1: "PITA", 2: "PITA"}, read=False, today=date(2026, 11, 25))
    e = r["items"][1]
    want = math.log((90 / 100) / (1100 / 1000)) / math.log(330 / 300)
    assert e["source"] == "item" and e["events"] == 1 and e["value"] == round(want, 2) and e["last"] == CHANGE
    assert 2 not in r["items"] and r["status"]["measured"] == 1 and r["status"]["missing_days"] == 0


def test_days_not_held_are_read_behind(db, monkeypatch):
    db.execute("DELETE FROM dayv WHERE day = ?", (CHANGE,))
    asked = []
    monkeypatch.setattr(elastic, "read_behind", lambda jobs: asked.extend(jobs))
    r = elastic.measure(["R"], [1, 2], {}, today=date(2026, 11, 25))
    assert r["items"] == {} and asked == [("R", CHANGE)] and r["status"]["missing_days"] == 1


def test_an_item_never_repriced_takes_its_familys_median(monkeypatch):
    monkeypatch.setattr(elastic, "events", lambda ref, items, today=None: [("2026-06-01", n, 100.0, 110.0) for n in (1, 2, 3)])
    monkeypatch.setattr(elastic, "_held", lambda ref, a, b: set(elastic._days(a, b)))
    monkeypatch.setattr(elastic, "one", lambda ref, d, evs: {1: (-0.5, 50), 2: (-1.0, 50), 3: (-4.0, 50)})
    r = elastic.measure(["R"], [1, 2, 3, 4], {1: "F", 2: "F", 3: "F", 4: "F"}, read=False)
    assert r["items"][3]["value"] == -3.0                                 # kept at −3
    assert r["items"][4] == {"value": -1.0, "source": "family", "events": 3, "units": 0, "last": ""}
