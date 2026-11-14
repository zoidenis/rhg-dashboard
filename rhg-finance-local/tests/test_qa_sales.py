"""QA of the Sales section (14 Nov 2026): prices of 1 are placeholders; a revenue centre is its restaurant's own;
Location intelligence of one company."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import channels  # noqa: E402
import pricelist  # noqa: E402


def price(ref, loc, lv, p):
    return {"num": 10019009, "name": "ALTO ADIGE", "ref": ref, "location": loc, "currency": "ALL", "level": lv, "price": p}


def part(net):
    return {p: {"net": net, "disc": 0, "checks": 10, "guests": 10, "avg": net / 10} for p in channels.PERIODS}


def row(ref, name, company, rvcs):
    return {"locRef": ref, "name": name, "reg_name": name, "company": company, "currency": "ALL",
            "total": {p: sum(rvcs.values()) for p in channels.PERIODS}, "delivery": {p: 0 for p in channels.PERIODS},
            "channels": [dict(part(sum(rvcs.values())), name="Dine in")], "dayparts": [],
            "rvcs": [dict(part(v), name=k) for k, v in rvcs.items()], "partners": []}


class QA(unittest.TestCase):
    def test_placeholder_prices_left_out(self):
        rows = [price("SALT", "SALT Tirana", 1, 1.0), price("SALT", "SALT Tirana", 2, 9000.0),
                price("LIFT", "LIFT", 1, 12000.0), price("LD", "Le District", 1, 1.0)]
        s = pricelist._spread(rows)
        self.assertEqual([(x["low"], x["high"], x["gap_pct"]) for x in s], [(9000.0, 12000.0, 33.3)])
        self.assertEqual(pricelist._spread([price("SALT", "SALT", 1, 1.0), price("LIFT", "LIFT", 1, 12000.0)]), [])

    def test_revenue_centres_by_restaurant_and_company(self):
        rows = [row("R1", "OPA Blloku", "SALT", {"Restaurant": 100}), row("R2", "SALT Tirana", "SALT", {"Restaurant": 300}),
                row("R4", "Le District", "TBM", {"Restaurant": 50})]
        r = {"locations": rows, "totals": channels.summarise(rows, [], {}), "settings": {}, "currency": "ALL"}
        names = [x["name"] for x in r["totals"]["ALL"]["rvcs"]]
        self.assertEqual(names, ["SALT Tirana · Restaurant", "OPA Blloku · Restaurant", "Le District · Restaurant"])
        c = channels.cut(r, {"R4"})
        self.assertEqual([x["name"] for x in c["locations"]], ["Le District"])
        self.assertEqual(c["totals"]["ALL"]["total"]["cur"], 50)
        self.assertEqual(c["totals"]["ALL"]["rvcs"][0]["place"], "Le District")


    def test_company_and_departments(self):
        import locintel
        import server
        reg = [{"ref": "R1", "company": "SALT", "dept": "OPA1", "bc": "OPA1"},
               {"ref": "R2", "company": "SALT", "dept": "LIFT", "bc": "LIFT"},
               {"ref": "R4", "company": "TBM", "dept": "LD", "bc": ""}]
        old = locintel.registry
        locintel.registry = lambda settings=None: reg
        try:
            self.assertIsNone(server.company_refs("", []))
            self.assertEqual(server.company_refs("SALT"), {"R1", "R2"})
            self.assertEqual(server.company_refs("SALT", ["lift"]), {"R2"})
            self.assertEqual(server.company_refs("SALT", ["LD"]), set())
            body = {"available": True, "currency": "ALL", "locations": [
                {"locRef": "R1", "name": "OPA", "currency": "ALL", "in_registry": True, "daily": {"2026-11-01": 10},
                 "cur": {"net": 10}, "prev": {"net": 5}},
                {"locRef": "R2", "name": "LIFT", "currency": "ALL", "in_registry": True, "daily": {"2026-11-01": 30},
                 "cur": {"net": 30}, "prev": {"net": 20}}]}
            r = server.sales_of_company(body, "SALT", ["LIFT"])
            self.assertEqual([x["name"] for x in r["locations"]], ["LIFT"])
            self.assertEqual((r["totals"]["ALL"]["cur"]["net"], r["days"][0]["net"]["ALL"]), (30, 30))
        finally:
            locintel.registry = old


    def test_departments_cut_every_location_screen(self):
        import locintel
        import server
        reg = [{"ref": "OPA_B", "company": "SALT", "dept": "OPA1", "bc": "OPA1"},
               {"ref": "LIFT", "company": "SALT", "dept": "LIFT", "bc": "LIFT"}]
        old = locintel.registry
        locintel.registry = lambda settings=None: reg
        try:
            A = server.dept_codes("SALT", ["opa1"])
            self.assertEqual(A, {"OPA1", "OPA_B"})
            self.assertEqual(server.dept_codes("SALT", ["MAGAZINE"]), {"MAGAZINE"})       # a warehouse: itself
            body = {"company": "SALT", "totals": {"x": 1}, "locations": {"OPA1": {"name": "OPA"}, "LIFT": {"name": "LIFT"}},
                    "rows": [{"ref": "OPA_B"}, {"code": "LIFT"}]}
            r = server.filter_codes(body, A)
            self.assertEqual(list(r["locations"]), ["OPA1"])
            self.assertEqual(r["rows"], [{"ref": "OPA_B"}])
            self.assertNotIn("totals", r)
            self.assertEqual(body["locations"].keys(), {"OPA1", "LIFT"})                  # the held answer untouched
            # every location screen is covered: cut by a function, by its handler, or by the generic filter
            for path in server.ACCESS_LOCATION_PATHS - server.DEPT_SELF:
                self.assertTrue(path in server.LOCATION_CUTS or path in server.SELF_CUT or path.startswith("/api/"), path)
        finally:
            locintel.registry = old


    def test_registry_inactive_and_franchise(self):
        import importlib.util
        import simphony
        # a fresh copy of locintel: other tests replace locintel.registry
        spec = importlib.util.spec_from_file_location("locintel_fresh", os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "locintel.py"))
        locintel = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(locintel)
        text = ("SALT | SALT Tirana | company=SALT | bc=SALT\n"
                "OLD  | Old kiosk   | company=SALT | bc=OLDK | inactive\n"
                "FR1  | OPA Durrës  | company=SALT | bc=OPADR | ownership=franchise")
        st = {"locations": {"registry": text}}
        rows = locintel.registry(st)
        self.assertEqual([r["ref"] for r in rows], ["SALT", "FR1"])
        self.assertEqual([r["ownership"] for r in rows], ["RHG", "Franchise"])
        self.assertEqual(len(locintel.registry(st, inactive=True)), 3)
        self.assertEqual(locintel.inactive_refs(st), {"OLD", "OLDK"})
        f = simphony.day_figures([{"netSlsTtl": 100, "vdTtl": -30, "mngrVdTtl": -20}])
        self.assertEqual((f["voids"], f["voids_emp"], f["voids_mgr"]), (50, 30, 20))
        import server
        body = {"locations": {"OLDK": {"x": 1}, "SALT": {"x": 2}}, "totals": {"x": 3}}
        r = server.drop_codes(body, {"OLD", "OLDK"})
        self.assertEqual((list(r["locations"]), r["totals"]), (["SALT"], {"x": 3}))


    def test_location_intelligence_parts(self):
        import server
        res = {"locations": [{"ref": "SALT", "sim": {"cur": {}}, "bc_posted": {"total": 1}, "purch": {"amount": 2},
                              "inv": {"stock": 3}, "use": 4}], "inventory": {"state": "done"}}
        P = "platform.s.executive.locations."
        analyst = {"permissions": ["platform.s.executive.locations", P + "company_sales", P + "bc_sales"]}
        r = server.li_parts_cut(res, analyst)
        L = r["locations"][0]
        self.assertEqual((L["bc_posted"], L["purch"], L["inv"], L["use"]), ({"total": 1}, None, None, None))
        self.assertEqual(r["parts"]["company_pl"], False)
        self.assertEqual(r["inventory"]["state"], "n/a")
        self.assertEqual(res["locations"][0]["purch"], {"amount": 2})                    # the held answer untouched
        every = {"permissions": [P + k for k in server.LI_PARTS]}
        self.assertEqual(server.li_parts_cut(res, every)["locations"][0]["use"], 4)


if __name__ == "__main__":
    unittest.main()
