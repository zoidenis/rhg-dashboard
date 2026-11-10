"""Simphony ↔ Billy · Returns tracking: each cancel, return, reopen or change of a check and the corrective that closes it."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import billy
import billyrec

S = {"tol": 1.0, "window": 20}


def chk(cid, num, total, clsd, day="2026-11-05", **kw):
    c = {"id": cid, "num": num, "name": "", "opn": clsd[:11] + "12:00:00", "clsd": clsd, "total": total, "cancel": False,
         "info": "", "emp": 7, "day": day, "inv": None, "v": 2}
    c.update(kw)
    c["_t"] = billyrec._ts(c["clsd"])
    return c


def inv(k, total, issue, loc="R1", corr=False, corr_ref="", status="no_check"):
    return {"_k": k, "iic": k, "fic": "F" + k, "num": "N" + k, "total": total, "issue": issue, "loc": loc, "corr": corr,
            "corr_ref": corr_ref, "status": status, "tcr": "t1", "op": "o1", "_t": billyrec._ts(issue)}


class Returns(unittest.TestCase):
    def setUp(self):
        A = inv("IICA0001", 800, "2026-11-05T13:00:00", status="matched")
        B = inv("IICB0002", 1000, "2026-11-05T14:00:00", status="matched")
        own = inv("IICOWN003", -500, "2026-11-05T15:01:00", corr=True, status="matched")
        later = inv("IICLAT004", -300, "2026-11-05T18:00:00")                 # negative, not marked, by amount
        cA = inv("IICCA0005", -800, "2026-11-06T09:00:00", corr=True, corr_ref="IICA0001")
        loose = inv("IICLOO006", -99, "2026-11-05T20:00:00", corr=True)
        self.invs = [A, B, own, later, cA, loose]
        self.by = {i["_k"]: i for i in self.invs}
        self.checks = {"R1": [
            chk("1", 101, -500, "2026-11-05T15:00:00", inv="IICOWN003"),            # return fiscalized at closing
            chk("2", 102, -300, "2026-11-05T16:00:00"),                             # return closed later by amount
            chk("3", 103, 0, "2026-11-05T13:30:00", cancel=True, info="FISK IICA0001"),  # cancelled after its invoice
            chk("4", 104, 1000, "2026-11-05T14:00:00", inv="IICB0002", to=205),     # reopened to 205: B not corrected
            chk("5", 105, 2000, "2026-11-05T19:00:00", vd=150.0, vd_n=2),          # voids before closing
            chk("6", 106, 0, "2026-11-05T19:30:00", cancel=True),                   # cancelled, nothing fiscalized
            chk("7", 107, -50, "2026-11-09T10:00:00", day="2026-11-09")]}          # return on the last day
        self.R = billyrec.track_returns(self.checks, self.invs, self.by, S, {}, "2026-11-09")

    def st(self, num, kind):
        return next(e for e in self.R["events"] if e["num"] == num and e["kind"] == kind)

    def test_each_case(self):
        self.assertEqual(self.st(101, "return")["status"], "closed_self")
        e = self.st(102, "return")
        self.assertEqual((e["status"], e["corr_num"], e["corr_marked"]), ("closed_amount", "NIICLAT004", False))
        e = self.st(103, "cancel")
        self.assertEqual((e["status"], e["orig_num"], e["corr_num"]), ("closed_ref", "NIICA0001", "NIICCA0005"))
        e = self.st(104, "reopen")
        self.assertEqual((e["status"], e["amount"]), ("unfiscalized", 1000))
        self.assertEqual(self.st(105, "void")["status"], "no_effect")
        self.assertEqual(self.st(106, "cancel")["status"], "no_effect")
        self.assertEqual(self.st(107, "return")["status"], "pending")

    def test_kpis_and_loose(self):
        k = self.R["kpis"]
        self.assertEqual((k["events"], k["closed"], k["unfiscalized"], k["unfiscalized_value"], k["pending"], k["no_effect"]),
                         (5, 3, 1, 1000, 1, 2))
        self.assertEqual([x["num"] for x in self.R["loose"]], ["NIICLOO006"])

    def test_cut_to_a_location(self):
        c = billyrec._returns_cut(self.R, {"R2"})
        self.assertEqual((c["events"], c["kpis"]["events"], c["kpis"]["unfiscalized"]), ([], 0, 0))

    def test_billy_corrective_reference(self):
        self.assertEqual(billy.corrective_of({"correctiveInv": {"IICRef": "abc123", "Type": "CORRECTIVE"}}), ("ABC123", "CORRECTIVE"))
        self.assertEqual(billy.corrective_of({"correctiveInv": {"iicRef": "x9"}})[0], "X9")
        self.assertEqual(billy.corrective_of({}), ("", ""))

    def test_check_marks_from_simphony(self):
        g = {"guestCheckId": 9, "chkNum": 9, "chkTtl": -40, "reopnToChk": 12, "rtrnTtl": -40, "vdTtl": 15,
             "detailLines": [{"menuItem": {"miNum": 1, "returnFlag": True}, "dspTtl": -40, "reasonCodeNum": 3, "mgrEmpNum": 55},
                             {"menuItem": {"miNum": 2}, "vdFlag": True, "dspTtl": 15}]}
        c = billyrec.check_of(g)
        self.assertEqual((c["rt"], c["rt_n"], c["vd"], c["vd_n"], c["to"], c["reasons"], c["mgr"]), (40, 1, 15, 1, 12, ["3"], ["55"]))


if __name__ == "__main__":
    unittest.main()
