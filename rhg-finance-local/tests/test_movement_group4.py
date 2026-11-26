"""Movement & dead stock: the Item Category Group 4 filter (Geri, 26 Nov 2026)."""
import movement


def row(loc, item, cat, g4=None):
    x = [loc, item, 5.0, 100.0, 0, 0, 0, 0, 0, 0, "", "", None, "DEAD STOCK", "", cat, ""]
    return x + [g4] if g4 is not None else x                    # a result read before the column: 17 fields


def test_rows_filtered_by_item_category_group_4():
    r = {"_rows": [row("OPA1", "A", "MISH", "MISH&SHPENDE"), row("OPA1", "B", "PIJE", "BIRRE"), row("SALT", "C", "PIJE")],
         "_names": {"A": "Pule", "B": "Peroni", "C": "Uje"}}
    out = movement.select_rows(r, group4="birre")
    assert [x[1] for x in out["rows"]] == ["B"] and out["groups4"] == ["BIRRE", "MISH&SHPENDE"]
    assert movement.COLS[-1] == "group4" and movement.select_rows(r)["total"] == 3
