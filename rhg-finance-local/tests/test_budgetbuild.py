"""Budget & forecast → Build the budget (budgetbuild.py)."""
import io

import openpyxl
import pytest

import budgetbuild


def test_each_month_from_the_latest_closed():
    cube = {"months": [f"2025-{m:02d}" for m in range(1, 13)] + [f"2026-{m:02d}" for m in range(1, 12)],
            "provisional": ["2026-10", "2026-11"], "running": {"month": "2026-11"}}
    bm = budgetbuild.base_months(2027, cube)
    assert bm["01"] == "2026-01" and bm["09"] == "2026-09" and bm["10"] == "2025-10" and bm["12"] == "2025-12"


def test_food_and_drinks_to_their_accounts():
    assert budgetbuild.default_account("FOOD", ["705", "707"]) == "705"
    assert budgetbuild.default_account("BEVERAGES", ["705", "707"]) == "707"
    assert budgetbuild.default_account("PIJE", ["705"]) == "705"


@pytest.fixture
def store_file(tmp_path, monkeypatch):
    monkeypatch.setattr(budgetbuild, "FILE", tmp_path / "b.json")
    monkeypatch.setattr(budgetbuild.store, "audit", lambda *a, **k: None)


def test_draft_review_approved_and_the_file_for_bc(store_file):
    st = {"sales": {"R1": {"EAT IN": {"chk": 3, "ac": 5}}}}
    budgetbuild.save("SALT", 2027, {"action": "save", "state": st}, "geri", False)
    with pytest.raises(ValueError):                                   # approve needs a review first
        budgetbuild.save("SALT", 2027, {"action": "approve", "entries": [{"a": "705", "d": "OPA1", "m": 1, "v": -100}]}, "cfo", True)
    budgetbuild.save("SALT", 2027, {"action": "submit"}, "geri", False)
    with pytest.raises(PermissionError):
        budgetbuild.save("SALT", 2027, {"action": "approve", "entries": [{"a": "705", "d": "OPA1", "m": 1, "v": -100}]}, "geri", False)
    r = budgetbuild.save("SALT", 2027, {"action": "approve", "name": "BUDGET2027",
                                        "entries": [{"a": "705", "d": "OPA1", "m": 1, "v": -100.004}, {"a": "636", "d": "", "m": 13, "v": 5},
                                                    {"a": "636", "d": "OPA1", "m": 2, "v": 50}]}, "cfo", True)
    assert r["current"]["status"] == "Approved" and r["current"]["state"] == st and r["approved"]["approved_by"] == "cfo"
    with pytest.raises(ValueError):                                   # locked
        budgetbuild.save("SALT", 2027, {"action": "save", "state": {}}, "geri", False)
    v = budgetbuild.approved_entries("SALT", 2027)
    assert v["entries"] == [{"a": "705", "d": "OPA1", "m": 1, "v": -100.0}, {"a": "636", "d": "OPA1", "m": 2, "v": 50.0}]
    wb = openpyxl.load_workbook(io.BytesIO(budgetbuild.render_xlsx("SALT", 2027, v, {"705": ["Shitje mallrash", "sales", "revenue"]},
                                                                    [{"code": "OPA1", "name": "OPA Bllok"}])))
    rows = list(wb["G-L Budget Entries"].iter_rows(values_only=True))
    assert rows[0][:6] == ("Budget Name", "Date", "G/L Account No.", "G/L Account Name", "DEPARTMENT", "Amount")
    assert rows[2][0] == "BUDGET2027" and rows[2][2] == "705" and rows[2][4] == "OPA1" and rows[2][5] == -100.0 and str(rows[2][1])[:10] == "2027-01-01"
