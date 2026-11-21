"""Simphony ↔ Billy, QA 21 Nov 2026: the invoice number, EIC and process of Billy's invoices; exports apart from the
supplies at 0%; an invoice and its corrective (storno) are not invoices without a check; where an invoice was issued."""
import billy
import billyrec


def test_number_eic_and_process_whatever_billy_calls_them():
    x = {"iic": "ab" * 16, "FIC": "71cef515-5d43", "invOrdNum": 60803, "issueDateTime": "2026-11-01T00:16:40+01:00",
         "tcrcode": "ZK574KL217", "typeOfInv": "CASH", "totPrice": 330}
    c = billy.compact(x, "fiscal")
    assert c["num"] == "60803/2026/zk574kl217" and c["num_built"] and c["fic"] == "71cef515-5d43"
    e = billy.compact({"iic": "cd" * 16, "InvNum": "10519/2026", "EIC": "BC45DBF9-42D3", "ProfileID": "P11",
                       "typeOfInv": "NONCASH", "buyer": {"idnum": "J91725007P", "idtype": "NUIS"}}, "fiscal")
    assert (e["num"], e["eic"], e["process"], e["num_built"]) == ("10519/2026", "bc45dbf9-42d3", "P11", False)
    assert billyrec.source_of(e) == "bc" and billyrec.source_of(c) == "pos"
    n = billy.compact({"iic": "ef" * 16, "invOrdNum": 86733, "issueDateTime": "2026-11-13T10:00:00", "typeOfInv": "NONCASH"}, "fiscal")
    assert n["num"] == "86733/2026"


def test_exports_apart_from_supplies_at_zero():
    S = {"rates": [20, 10, 6, 0]}
    cols, errs = billyrec.book_row({"total": 1000, "wo_vat": 1000, "vat": 0, "fic": "F", "taxes": [
        [None, "EXPORT_OF_GOODS", 600.0, 0.0, 1], [0.0, None, 300.0, 0.0, 1], [None, "TYPE_1", 100.0, 0.0, 1]]}, S)
    assert (cols["export"], cols["zero"], cols["exempt"]) == (600.0, 300.0, 100.0) and not errs


def inv(k, total, **kw):
    return dict({"_k": k, "iic": k, "num": f"{k}/2026", "total": total, "status": "no_check", "corr_ref": "", "corr": False,
                 "tcr": "t1", "_t": 0}, **kw)


def test_storno_is_not_an_invoice_without_a_check():
    a, b, c = inv("A" * 32, 141912.0), inv("B" * 32, -141912.0, corr_ref="A" * 32, corr=True), inv("C" * 32, 500.0)
    billyrec.storno([a, b, c], None)
    assert (a["status"], b["status"], c["status"]) == ("cancelled", "cancelled", "no_check")
    assert a["pair"] == "B" * 32 + "/2026" and b["pair"] == "IIC " + "A" * 12
    # the original read in one part, its corrective found in the correctives of the whole period
    d = inv("D" * 32, 700.0)
    billyrec.storno([d], {"D" * 32: [{"num": "9/2026", "total": -700.0}]})
    assert d["status"] == "cancelled" and d["pair"] == "9/2026"


def test_why_a_check_is_not_fiscalized():
    S = {"tol": 1}
    oc = {"ref": "R", "total": 330.0, "clsd": "2026-11-01T00:16:00", "status": "missing"}
    later = {"_k": "x", "loc": "R", "total": 330.0, "_t": billyrec._ts("2026-11-01T12:16:00"), "num": "60804/2026/zk",
             "iic": "X" * 32, "issue": "2026-11-01T12:16:00"}
    w = billyrec.why_missing(oc, {"reopen": True, "reopen_at": "2026-11-01T12:10:00"}, {("R", 330): [later]}, {}, S)
    assert "Reopened after closing" in w and "60804/2026/zk" in w and "720 min later" in w
    assert "No invoice of this total" in billyrec.why_missing(oc, {}, {}, {}, S)
