"""Simphony ↔ Billy part by part: the same tables whatever the size of the parts, the rows on disk a page at a time,
the scope of a person, the register's cases and the Excel. A synthetic store of Billy and Simphony; nothing is read
from either."""
import json
import random
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import billy
import billyrec
import locintel
import simphony
import store

NUIS = "L91520027A"
END = date(2026, 11, 9)
DAYS = 9


def make_store(d):
    """Two restaurants (R1 with orders, R2) and a kiosk till: late invoices (to 70 h, across parts), checks after
    midnight, returns closed at the till, by amount and by reference days later, cancels, reopens, never fiscalized."""
    rnd = random.Random(4)
    inv, det, chk = [], [], []
    n = 0

    def put(k, total, t, tcr, both=True, **kw):
        x = {"iic": k, "fic": kw.pop("fic", "F" + k), "invNum": f"{k[-6:]}/26", "issueDateTime": t.isoformat(timespec="seconds"),
             "totPrice": total, "totPriceWoVAT": round(total / 1.2, 2), "totVATAmt": round(total - total / 1.2, 2), "tcrcode": tcr,
             "businUnitCode": "bu", "operatorCode": "op" + tcr[-1], "typeOfInv": "CASH", "isIssuerInVAT": True}
        x.update(kw)
        for src in (("bridge", "fiscal") if both else ("bridge",)):
            inv.append((NUIS, src, x["issueDateTime"][:10], k, json.dumps(x)))
        return x
    later = []
    for k in range(DAYS):
        day = END - timedelta(days=DAYS - 1 - k)
        for ref, tcr in (("R1", "t1"), ("R2", "t2")):
            for j in range(40):
                n += 1
                t = datetime(day.year, day.month, day.day, 9) + timedelta(seconds=rnd.randint(0, 17 * 3600))
                tot = float(rnd.choice([300, 400, 500, 800, 1200]) if rnd.random() < 0.7 else rnd.randint(16, 90) * 100)
                g = {"guestCheckId": n, "chkNum": n, "chkTtl": tot, "clsdLcl": t.isoformat(), "opnLcl": (t - timedelta(minutes=30)).isoformat(),
                     "empNum": rnd.randint(1, 4), "rvcNum": 1}
                key, r = f"IIC{n:08d}", rnd.random()
                if r < 0.04:                                   # a return fiscalized at closing, or later by amount
                    g["chkTtl"] = -tot / 10
                    put(key, -tot / 10, t + timedelta(seconds=rnd.choice([30, 9000, 150000])), tcr,
                        **({"correctiveInv": {"IICRef": "X" + key}} if rnd.random() < 0.5 else {}))
                elif r < 0.07:                                 # cancelled after its invoice, corrected 0.2 to 5 days later
                    g.update(chkTtl=0.0, cancelFlag=True, chkInfo="FISK " + key)
                    put(key, tot, t - timedelta(minutes=5), tcr)
                    later.append((key + "C", -tot, t + timedelta(days=rnd.choice([0.2, 2, 5])), tcr, {"correctiveInv": {"IICRef": key}}))
                elif r < 0.09:
                    g["reopnToChk"] = n + 90000
                    put(key, tot, t + timedelta(seconds=60), tcr)
                elif r < 0.11:
                    pass                                       # never fiscalized
                elif r < 0.17:                                 # fiscalized 2 to 70 hours later
                    put(key, tot, t + timedelta(seconds=rnd.randint(7200, 70 * 3600)), tcr)
                elif r < 0.19:
                    put(key, tot, t + timedelta(seconds=40), tcr, both=False, fic="")
                elif r < 0.21:
                    put(key, -77.0, t, tcr, correctiveInv={"IICRef": "NOWHERE" + key})
                elif ref == "R1" and r < 0.5:                  # table service: an order, closed by the invoice
                    put(key + "O", tot, t - timedelta(minutes=20), tcr)
                    full = put(key, tot, t + timedelta(seconds=70), tcr)
                    det.append((NUIS, key, 0.0, json.dumps(dict(full, payMethods=[{"type": "BANKNOTE", "amt": tot}], sumInvIICRefs=[{"iic": key + "O"}]))))
                else:
                    put(key, tot, t + timedelta(seconds=rnd.randint(5, 900)), tcr)
                chk.append((ref, day.isoformat(), str(n), g))
        for j in range(6):                                     # the kiosk: no Simphony
            n += 1
            put(f"IIC{n:08d}", 250.0, datetime(day.year, day.month, day.day, 12, j), "k9")
    for key, total, t, tcr, kw in later:
        if t.date() <= END:
            put(key, total, t, tcr, **kw)
    con = sqlite3.connect(d / "billy_v2.db")
    con.executescript("""CREATE TABLE inv (nuis TEXT, src TEXT, day TEXT, k TEXT, j TEXT, PRIMARY KEY (nuis, src, k));
        CREATE INDEX inv_day ON inv (nuis, src, day); CREATE TABLE det (nuis TEXT, iic TEXT, at REAL, j TEXT, PRIMARY KEY (nuis, iic));
        CREATE TABLE days (nuis TEXT, src TEXT, day TEXT, final INTEGER, at REAL, n INTEGER, PRIMARY KEY (nuis, src, day));""")
    con.executemany("INSERT OR REPLACE INTO inv VALUES (?,?,?,?,?)", inv)
    con.executemany("INSERT OR REPLACE INTO det VALUES (?,?,?,?)", det)
    con.executemany("INSERT INTO days VALUES (?,?,?,1,0,0)", [(NUIS, s, (END - timedelta(days=k)).isoformat()) for k in range(DAYS) for s in ("bridge", "fiscal")])
    con.commit()
    con.close()
    return chk


class Parts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = Path(cls.tmp.name)
        cls.saved = (store.DATA_DIR, billy.DB_PATH, billyrec.DB_PATH, store.get_settings, billy.account, billy._token,
                     billy.read_day, billy.detail, simphony.info, locintel.registry, billyrec.CHUNK_DAYS)
        store.DATA_DIR, billy.DB_PATH, billyrec.DB_PATH = d, d / "billy_v2.db", d / "guest_checks.db"
        billy._conn = billyrec._conn = None
        chk = make_store(d)
        store.get_settings = lambda: {"billy_rec": {"days": str(DAYS), "map": "t1 = R1\nt2 = R2\nk9 = R2 | Kiosk"}}
        billy.account = lambda company: {"nuis": NUIS}
        billy._token = lambda acct: "T"
        billy.read_day = lambda *a, **k: 0
        billy.detail = lambda acct, fic: None
        simphony.info = lambda: {"configured": True}
        locintel.registry = lambda settings=None: [{"ref": r, "name": "Rest " + r, "company": "SALT", "bc": "B" + r, "dept": ""} for r in ("R1", "R2")]
        db = billyrec._db()
        db.executemany("INSERT INTO chk VALUES (?,?,?,?)", [(r, dd, i, json.dumps(billyrec.check_of(g))) for r, dd, i, g in chk])
        db.executemany("INSERT OR REPLACE INTO chkdays VALUES (?,?,1,0)", {(r, dd) for r, dd, i, g in chk})
        db.commit()
        cls.read = {}
        for size in (DAYS, 3, 1):
            billyrec.CHUNK_DAYS = size
            r = billyrec.build("SALT", lambda ref: END.isoformat(), today=END + timedelta(days=1))
            billyrec.remember("SALT", r)
            cls.read[size] = (r, billyrec.expand(r))

    @classmethod
    def tearDownClass(cls):
        (store.DATA_DIR, billy.DB_PATH, billyrec.DB_PATH, store.get_settings, billy.account, billy._token, billy.read_day,
         billy.detail, simphony.info, locintel.registry, billyrec.CHUNK_DAYS) = cls.saved
        billy._conn = billyrec._conn = None
        billyrec._results.clear()
        cls.tmp.cleanup()

    @staticmethod
    def canon(x):
        def norm(v):
            if isinstance(v, float):
                return round(v, 2)
            if isinstance(v, dict):
                return {k: norm(y) for k, y in v.items()}
            if isinstance(v, list):
                return [norm(y) for y in v]
            return v
        return sorted(json.dumps(norm(y), sort_keys=True) for y in x)

    def test_the_same_tables_whatever_the_parts(self):
        whole = self.read[DAYS][1]
        self.assertEqual(self.read[DAYS][0]["parts"], 1)
        for size in (3, 1):
            r, part = self.read[size]
            self.assertEqual(r["parts"], -(-DAYS // size))
            self.assertEqual(part["kpis"], whole["kpis"], size)
            self.assertEqual(part["returns"]["kpis"], whole["returns"]["kpis"], size)
            for k in ("checks", "invoices", "book", "orders", "tills", "operators", "summary", "locations"):
                self.assertEqual(self.canon(part[k]), self.canon(whole[k]), (size, k))
            for k in ("events", "loose"):
                self.assertEqual(self.canon(part["returns"][k]), self.canon(whole["returns"][k]), (size, k))
            self.assertEqual(self.canon(billyrec.findings(r)), self.canon(billyrec.findings(self.read[DAYS][0])), size)

    def test_what_the_store_holds(self):
        r, x = self.read[1]
        k = x["kpis"]
        self.assertGreater(k["late"] + k["late48"], 5)                 # fiscalized later, some across parts
        self.assertGreater(k["missing"], 3)
        self.assertGreater(k["orders"], 50)
        self.assertGreater(x["returns"]["kpis"]["closed_ref"], 3)       # a corrective days later closes its cancel
        self.assertTrue(any(t["tcr"] == "k9" and t["matched"] == 0 for t in x["tills"]))
        self.assertTrue(x["returns"]["loose"])
        self.assertEqual(len(x["book"]), k["invoices"])
        self.assertLess(len(json.dumps(r)), 60_000)                     # the rows are on disk, not in the result

    def test_pages_filters_and_scope(self):
        r = billyrec.held("SALT")
        self.assertEqual(r["gen"], self.read[1][0]["gen"])
        con = billyrec._rcon()
        self.assertEqual({g for (g,) in con.execute("SELECT DISTINCT gen FROM rw")}, {r["gen"]})   # the reads before: gone
        every = billyrec.rows(r, "checks", None, st="all", limit=5000)
        self.assertEqual(every["total"], len(self.read[1][1]["checks"]))
        page = billyrec.rows(r, "checks", None, st="all", offset=10, limit=7)
        self.assertEqual([c["id"] for c in page["rows"]], [c["id"] for c in every["rows"][10:17]])
        self.assertEqual(every["rows"][0]["day"], END.isoformat())        # the newest first
        miss = billyrec.rows(r, "checks", None)                            # the screen's default: not fiscalized
        self.assertEqual(miss["total"], r["kpis"]["missing"])
        one = every["rows"][3]
        hit = billyrec.rows(r, "checks", None, st="all", q=str(one["num"]))
        self.assertIn(one["id"], [c["id"] for c in hit["rows"]])
        cases = billyrec.rows(r, "book", None, st="*")
        self.assertEqual(cases["total"], r["kpis"]["book_errors"])
        nofic = billyrec.rows(r, "book", None, st="no_fic")
        self.assertEqual(nofic["total"], r["book_errors"]["no_fic"])
        # a person of R1 only: the totals and every page are R1's
        c = billyrec.cut(r, {"BR1"})
        v = billyrec.public(c)
        self.assertEqual([L["ref"] for L in v["locations"]], ["R1"])
        r1 = billyrec._keep_of(r, {"BR1"})
        rows = billyrec.rows(r, "checks", r1, st="all", limit=5000)["rows"]
        self.assertTrue(rows and all(x["ref"] == "R1" for x in rows))
        self.assertEqual(v["kpis"]["checks"], sum(1 for x in rows if x["status"] not in ("zero", "pending")))
        ev = billyrec.rows(r, "events", r1, st="all", limit=5000)
        self.assertEqual(ev["total"], v["returns"]["chips"]["all"])
        self.assertEqual(billyrec.rows(r, "events", r1, st="unfiscalized")["total"], v["returns"]["kpis"]["unfiscalized"])
        self.assertEqual(billyrec.rows(r, "orders", r1, st="open")["total"], v["orders_n"]["open"])
        self.assertNotIn("agg", v)
        with self.assertRaises(ValueError):
            billyrec.rows(r, "nothing")

    def test_the_excel(self):
        import io
        from openpyxl import load_workbook
        r = billyrec.held("SALT")
        wb = load_workbook(io.BytesIO(billyrec.render_xlsx(r)), read_only=True)
        self.assertEqual(wb.sheetnames[:2], ["Libri i Shitjes", "Checks not fiscalized"])
        book = list(wb["Libri i Shitjes"].iter_rows(values_only=True))
        self.assertEqual(len(book), 2 + r["kpis"]["invoices"] + 1)        # title, header, every invoice, total
        self.assertEqual(book[-1][0], "Totali")
        self.assertAlmostEqual(book[-1][8], sum(x[8] or 0 for x in book[2:-1]), places=1)
        ret = load_workbook(io.BytesIO(billyrec.render_returns_xlsx(r, None, st="unfiscalized")), read_only=True)
        self.assertEqual(sum(1 for _ in ret["Returns tracking"].iter_rows()) - 1, r["returns"]["kpis"]["unfiscalized"])


if __name__ == "__main__":
    unittest.main()
