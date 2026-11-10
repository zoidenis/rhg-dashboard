"""SevenRooms ↔ Simphony: what is kept of a reservation (no personal data), venues to restaurants, reservations to
Simphony checks, the totals, the scope and the Excel. SevenRooms and Simphony are fakes; nothing is called."""
import io
import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import billyrec
import locintel
import sevenrooms
import simphony
import srsim
import store

TODAY = date(2026, 11, 10)
D1, D2 = "2026-11-05", "2026-11-06"
V1, V2, V3 = "venueSALT01", "venueOPA001", "venueKIOSK1"


def res(i, day, status, guests, checks="", arrived=None, created="2026-11-01T10:00:00", **kw):
    x = {"id": f"R{i}", "reference_code": f"REF{i}", "venue_id": kw.pop("venue", V1), "date": day, "time_slot_iso": "20:00:00",
         "shift_category": "DINNER", "max_guests": guests, "arrived_guests": arrived, "status": status, "status_display": status.title(),
         "status_simple": kw.pop("simple", ""), "is_walk_in": False, "booked_by": kw.pop("source", "Website"), "created": created,
         "check_numbers": checks, "pos_tickets": kw.pop("tickets", []), "first_name": "Ana", "last_name": "Hoxha",
         "email": "ana@example.com", "phone_number": "+355690000000", "client_id": "CLIENT-" + kw.pop("client", str(i)),
         "notes": "allergic", "table_numbers": ["12"], "served_by": "Server A", "is_vip": False, "venue_seating_area_name": "Terrace"}
    x.update(kw)
    return x


RES = {V1: {D1: [res(1, D1, "COMPLETE", 4, "101", 4, client="A"),
                 res(2, D1, "PAID", 2, "", 2, tickets=[{"code": 102, "total": 99, "table_no": "5"}], client="A"),
                 res(3, D1, "ARRIVED", 3, "", None),                                  # seated, no check
                 res(4, D1, "NO_SHOW", 2, ""),
                 res(5, D1, "CANCELED", 6, "103"),                                    # cancelled, with a check
                 res(6, D1, "COMPLETE", 2, "999", 2),                                 # a check Simphony does not have
                 res(7, D1, "CONFIRMED", 2, "")],                                     # left confirmed
            D2: [res(8, D2, "COMPLETE", 5, "201, 202", 5, created="2026-11-06T09:00:00", source="Phone"),
                 res(9, D2, "COMPLETE", 2, "201", 2)]},                              # the same check as 8
       V2: {D1: [res(10, D1, "COMPLETE", 2, "301", 2, venue=V2)], D2: []}}
FB = {V1: [{"overall": "5", "food": "4", "service": "5", "reservation_id": "R1", "reservation_date": D1, "received_date": "2026-11-06T10:00:00"},
           {"overall": "1", "food": "2", "service": "1", "reservation_id": "R8", "reservation_date": D2, "received_date": "2026-11-07T10:00:00"},
           {"overall": "4", "reservation_id": "R0", "reservation_date": "2026-10-01", "received_date": "2026-10-02T10:00:00"}],
      V2: []}
VENUES = [{"id": V1, "name": "SALT Tirana", "internal_name": "SALT", "currency_code": "ALL"},
          {"id": V2, "name": "Opa Blloku", "internal_name": "", "external_venue_id": "OPA1"},
          {"id": V3, "name": "Kiosk", "internal_name": ""}]


def fake_get(path, params=None):
    params = params or {}
    if path == "/venues":
        return {"results": VENUES}
    if path == "/reservations/export":
        return {"results": RES.get(params["venue_id"], {}).get(params["from_date"], [])}
    if path.endswith("/feedback"):
        return {"reservation_feedback": FB.get(path.split("/")[2], [])}
    raise AssertionError(path)


def chk(num, total, day, ref="R1"):
    return {"id": f"{ref}-{num}", "num": num, "total": total, "clsd": day + "T21:00:00", "cancel": False, "info": "", "emp": 1,
            "rvc": 1, "ws": 1, "sub": total, "tax": 0, "disc": 0, "opn": day + "T20:00:00", "name": "", "tenders": {}, "v": 2}


class SevenRooms(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = Path(cls.tmp.name)
        cls.saved = (store.DATA_DIR, sevenrooms.CONFIG_FILE, sevenrooms.get, sevenrooms._conn, billyrec.DB_PATH, billyrec._conn,
                     billyrec.read_checks, simphony.info, simphony.daily_totals, simphony.held_day, simphony.flush_days, locintel.registry)
        store.DATA_DIR, sevenrooms.CONFIG_FILE = d, d / "sevenrooms.json"
        sevenrooms._conn = None
        sevenrooms._venues.update(at=0.0, list=None)
        billyrec.DB_PATH, billyrec._conn = d / "guest_checks.db", None
        sevenrooms._write(dict(sevenrooms.DEFAULTS, client_id="client-123", client_secret="s3cret", map="Kiosk = KIOSK  # no such restaurant"))
        sevenrooms.get = fake_get
        db = billyrec._db()
        rows = [("R1", D1, chk(101, 4000, D1)), ("R1", D1, chk(102, 2100, D1)), ("R1", D1, chk(103, 900, D1)),
                ("R1", D1, chk(150, 1500, D1)), ("R1", D2, chk(201, 3000, D2)), ("R1", D2, chk(202, 1000, D2)),
                ("R2", D1, chk(301, 1200, D1, "R2")), ("R2", D1, chk(302, 800, D1, "R2"))]
        db.executemany("INSERT INTO chk VALUES (?,?,?,?)", [(r, dd, c["id"], json.dumps(c)) for r, dd, c in rows])
        db.commit()
        billyrec.read_checks = lambda *a, **k: False
        simphony.info = lambda: {"configured": True}
        simphony.daily_totals = lambda *a, **k: None
        simphony.flush_days = lambda: None
        G = {("R1", D1): 20, ("R1", D2): 9, ("R2", D1): 5}
        simphony.held_day = lambda ref, d: {"rvcs": [{"gstCnt": G.get((ref, d), 0), "netSlsTtl": 100.0, "chkCnt": 3}]}
        locintel.registry = lambda settings=None: [{"ref": "R1", "name": "Salt", "company": "SALT", "bc": "SALT", "dept": "D1"},
                                                   {"ref": "R2", "name": "OPA Blloku 2", "company": "SALT", "bc": "OPA1", "dept": "D2"}]
        cls.r = srsim.build("SALT", D1, D2, latest_fn=lambda ref: "2026-11-09", today=TODAY)

    @classmethod
    def tearDownClass(cls):
        (store.DATA_DIR, sevenrooms.CONFIG_FILE, sevenrooms.get, sevenrooms._conn, billyrec.DB_PATH, billyrec._conn,
         billyrec.read_checks, simphony.info, simphony.daily_totals, simphony.held_day, simphony.flush_days, locintel.registry) = cls.saved
        cls.tmp.cleanup()

    def test_no_personal_data_is_kept(self):
        con = sevenrooms._db()
        text = " ".join(j for (j,) in con.execute("SELECT j FROM res"))
        for word in ("Ana", "Hoxha", "ana@example.com", "+355690000000", "allergic", "CLIENT-"):
            self.assertNotIn(word, text)
        x = sevenrooms.slim(res(1, D1, "COMPLETE", 2, "0101, 102 ;x", client="A"))
        self.assertEqual((x["checks"], x["client"]), (["0101", "102"], sevenrooms.client_code("CLIENT-A")))
        self.assertNotIn("s3cret", json.dumps(sevenrooms.info()))
        self.assertTrue(sevenrooms.info()["has_secret"])

    def test_venues_to_restaurants(self):
        how = {v["name"]: (v["ref"], v["how"]) for v in self.r["venues"]}
        self.assertEqual(how["SALT Tirana"], ("R1", "same name"))                   # internal name SALT = Salt
        self.assertEqual(how["Opa Blloku"], ("R2", "external venue id"))
        self.assertTrue(how["Kiosk"][1].startswith("mapped to KIOSK"))
        self.assertEqual(how["Kiosk"][0], "")

    def test_reservations_to_checks(self):
        row = {x["res"]: x for x in self.r["rows"]}
        self.assertEqual((row["REF1"]["link"], row["REF1"]["spend"]), ("linked", 4000))
        self.assertEqual((row["REF2"]["link"], row["REF2"]["spend"]), ("linked", 2100))            # by the POS ticket
        self.assertEqual(row["REF3"]["link"], "no_check")
        self.assertEqual((row["REF4"]["cat"], row["REF4"]["link"]), ("no_show", ""))
        self.assertEqual((row["REF5"]["cat"], row["REF5"]["link"]), ("cancel", "check_on_noshow"))
        self.assertEqual(row["REF6"]["link"], "not_found")
        self.assertEqual(row["REF7"]["cat"], "open")                                              # 5 Nov, still confirmed
        self.assertEqual((row["REF8"]["link"], row["REF8"]["spend"], row["REF8"]["lead"]), ("linked", 4000, 0))
        self.assertEqual(row["REF9"]["link"], "shared")

    def test_totals(self):
        k = srsim.kpis(self.r["agg"])
        self.assertEqual((k["res"], k["seated"], k["no_show"], k["cancel"], k["open"]), (10, 7, 1, 1, 1))
        self.assertEqual(k["seated_covers"], 4 + 2 + 3 + 2 + 5 + 2 + 2)
        self.assertEqual(k["booked_covers"], 4 + 2 + 3 + 2 + 2 + 2 + 5 + 2 + 2)                  # not the cancelled 6
        self.assertAlmostEqual(k["no_show_rate"], 1 / 8)
        self.assertEqual(k["linked"], 5)                                                          # 1, 2, 8, 9 (shared), 10
        self.assertEqual(k["sim_sales"], 4000 + 2100 + 900 + 1500 + 3000 + 1000 + 1200 + 800)
        self.assertEqual(k["linked_sales"], 4000 + 2100 + 4000 + 3000 + 1200)
        self.assertEqual(k["sim_guests"], 34)
        self.assertEqual((k["fb_n"], k["fb_low"], k["fb_overall"]), (2, 1, 3.0))                 # October's is outside
        self.assertEqual((k["guests_known"], k["guests_back"]), (6, 1))                          # A came twice at SALT
        self.assertEqual(k["cases"], 5)

    def test_scope_and_screen(self):
        c = srsim.cut(self.r, {"OPA1"})
        v = srsim.public(c)
        self.assertEqual([L["ref"] for L in v["locations"]], ["R2"])
        self.assertTrue(all(x["ref"] == "R2" for x in v["rows"]))
        self.assertEqual((v["kpis"]["res"], v["kpis"]["sim_sales"]), (1, 2000))
        self.assertEqual(sum(m["res"] for m in v["mixes"]["shift"]), 1)                        # OPA's only, not SALT's
        self.assertEqual(sum(m["res"] for m in srsim.public(self.r)["mixes"]["shift"]), 9)       # every booking but the cancelled one
        self.assertNotIn("agg", v)
        p = srsim.public(self.r)
        self.assertEqual(p["rows"][0]["link"] in srsim.CASES or p["rows"][0]["cat"] == "open", True)
        self.assertEqual(p["locations"][0]["cells"][D1]["sim_guests"], 20)

    def test_excel(self):
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(srsim.render_xlsx(self.r)), read_only=True)
        self.assertEqual(wb.sheetnames[:3], ["Restaurants", "Days", "Reservations"])
        self.assertEqual(sum(1 for _ in wb["Reservations"].iter_rows()) - 1, 10)

    def test_an_account_that_may_not_list_the_venues(self):
        def denied(path, params=None):
            if path == "/venues":
                raise sevenrooms.Error("SevenRooms /venues: Permission denied", 403)
            if path == f"/venues/{V1}":
                return {"name": "SALT Tirana", "internal_name": "SALT"}
            if path.startswith("/venues/"):
                raise sevenrooms.Error("SevenRooms: Permission denied", 403)
            if path == "/reservations/export":
                return {"results": [dict(r, venue_id=r["venue_id"]) for r in RES[V1][D1][:2]] + [{"venue_id": V1}]}
            return fake_get(path, params)
        cfg = sevenrooms._read()
        long_id = "ahZzfnNldmVucm9vbXMtc2VjdXJlcgwLEgVWZW51ZRgB"                    # SevenRooms ids are like this
        sevenrooms._write(dict(cfg, map=long_id + " = R2"))
        sevenrooms.get = denied
        try:
            V = sevenrooms.venues(force=True)
            self.assertEqual([(v["id"], v["name"]) for v in V], [(long_id, ""), (V1, "SALT Tirana")])
            self.assertIn("may not list the venues", sevenrooms._venues["note"])
            regs = locintel.registry()
            placed = {v["id"]: (r or {}).get("ref") for v, r, _h in srsim.place(V, regs, long_id + " = R2")}
            self.assertEqual(placed, {long_id: "R2", V1: "R1"})
            sevenrooms.get = lambda path, params=None: (_ for _ in ()).throw(sevenrooms.Error("SevenRooms: Permission denied", 403))
            sevenrooms._write(dict(cfg, map=""))
            with self.assertRaises(sevenrooms.Error) as e:
                sevenrooms.venues(force=True)
            self.assertIn("ask SevenRooms", str(e.exception))
        finally:
            sevenrooms._write(cfg)
            sevenrooms.get = fake_get
            sevenrooms._venues.update(at=0.0, list=None, note="")


if __name__ == "__main__":
    unittest.main()
