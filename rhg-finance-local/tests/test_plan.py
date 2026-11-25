"""Price simulator → plan for a future period (plan.py)."""
from datetime import date

import plan
import simphony


def test_periods_and_base():
    assert [o["code"] for o in plan.options(date(2026, 11, 25))][:4] == ["2027", "2027-H1", "2027-H2", "2027-Q1"]
    assert plan.span("2027-q2") == (date(2027, 4, 1), date(2027, 6, 30), "Q2 2027")
    last = date(2026, 11, 24)
    assert plan.base_window(*plan.span("2027-Q1")[:2], last) == (date(2026, 1, 1), date(2026, 3, 31), None)
    # 2027 against 2026, sold to 24 Nov: cut there, completed with the season of 2025
    assert plan.base_window(*plan.span("2027")[:2], last) == (date(2026, 1, 1), last, (date(2025, 1, 1), date(2025, 12, 31), date(2025, 11, 24)))


def test_build_by_channel_with_the_completion(monkeypatch):
    held = {}
    for d in plan.days(date(2025, 10, 1), date(2025, 12, 31)):            # 2025: Dec doubles the checks of a day
        held[d] = {"rvcs": [{"netSlsTtl": 2000 if d >= "2025-12-01" else 1000, "chkCnt": 20 if d >= "2025-12-01" else 10, "gstCnt": 0}]}
    monkeypatch.setattr(simphony, "held_day", lambda ref, d: held.get(d))
    monkeypatch.setattr(simphony, "day_figures", lambda rv: {"net": rv[0]["netSlsTtl"], "checks": rv[0]["chkCnt"]})
    monkeypatch.setattr(simphony, "latest_business_date", lambda ref: "2026-11-30")
    monkeypatch.setattr(simphony, "order_type_dims", lambda ref: {1: "Eat in", 2: "DE - WOLT"})
    monkeypatch.setattr(plan, "_q", lambda ref, d: {"1|1||12": [500.0, 0, 5, 6], "1|2||13": [300.0, 0, 2, 2]} if d.startswith("2026") else None)
    monkeypatch.setattr(plan, "read_behind", lambda jobs: None)
    monkeypatch.setattr(plan.mkkpi, "read_behind", lambda jobs: None)
    r = plan.build("2027-Q4", ["R1"], {"R1": "OPA Bllok"}, today=date(2026, 12, 1))
    assert r["base"] == {"from": "2026-10-01", "to": "2026-11-30", "days": 61, "cut": True, "season": ["2025-10-01", "2025-12-31"]}
    L = r["locs"][0]
    assert L["channels"]["EAT IN"] == {"net": 500.0 * 61, "checks": 5 * 61, "guests": 6 * 61} and L["channels"]["DE - WOLT"]["checks"] == 122
    # 2025: Oct–Nov 61 days × 10 checks, Dec 31 × 20: (610 + 620) / 610
    assert L["factor"] == {"net": round(123000 / 61000, 4), "checks": round(1230 / 610, 4), "source": "season of 2025"}
    assert r["months"] == ["2027-10", "2027-11", "2027-12"] and not r["started"] and r["actual"] == []
