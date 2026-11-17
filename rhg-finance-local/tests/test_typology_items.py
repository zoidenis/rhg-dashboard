"""Typology & turnover → a family's items: the items add up to the family's row of the table."""
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import typology  # noqa: E402


def data():
    random.seed(11)
    items = {f"I{i}": {"Item_Category_Group_2": random.choice(["LP", "AMBALAZH", "PN"]),
                       "Item_Category_Group_4": random.choice(["MISH&SHPENDE", "PERIME&FRUTE", ""]),
                       "Description": f"Item {i}", "Base_Unit_of_Measure": "KG"} for i in range(120)}
    typo = [[loc, it, random.uniform(-3, 40), random.uniform(10, 900), random.uniform(0, 90), random.uniform(-5, 0),
             random.choice([0, 5, 12])] for it in items for loc in random.sample(["OPA1", "SALT", "MAGAZINE"], 2)]
    return {"company": "SALT", "as_of": "2026-11-17", "history_days": 90, "_typo": typo}, items


class FamilyItems(unittest.TestCase):
    def test_items_add_up_to_the_family(self):
        mv, items = data()
        t = typology.analyse(mv, items, {})
        f = typology.family_items(mv, items, {}, "LP", ["MISH&SHPENDE"])
        fam = [r for r in t["rows"] if r[1] == "LP" and r[2] == "MISH&SHPENDE"]
        self.assertTrue(f["rows"])
        for col_t, col_f in ((4, 7), (5, 8), (7, 10), (9, 12)):      # lines, value, use, above target
            self.assertAlmostEqual(sum(r[col_t] for r in fam), sum(r[col_f] for r in f["rows"]), delta=len(fam) + 1)
        self.assertTrue(all(r[4] == "MISH&SHPENDE" for r in f["rows"]))
        self.assertEqual(f["rows"], sorted(f["rows"], key=lambda r: -r[8]))

    def test_several_families(self):
        mv, items = data()
        a = typology.family_items(mv, items, {}, "LP", ["MISH&SHPENDE"])
        b = typology.family_items(mv, items, {}, "LP", ["PERIME&FRUTE"])
        ab = typology.family_items(mv, items, {}, "lp", ["mish&shpende", "PERIME&FRUTE"])
        self.assertEqual(len(ab["rows"]), len(a["rows"]) + len(b["rows"]))


if __name__ == "__main__":
    unittest.main()
