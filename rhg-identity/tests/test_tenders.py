"""Tenders & procurement: APP's CSV of the year, the hospitality matches, their status (22 Nov 2026)."""
import io
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import permissions
import server
import tenders

HEAD = ("Autoriteti_kontraktues,Numri_i_references,Objekti_i_prokurimit,Lloji_i_procedures,Tipi_i_kontrates,"
        "Lloji_i_marreveshjes_kuader,Fondi_limit,Data_e_publikimit,Data_e_hapjes,Data_e_mbylljes,Anulluar,"
        "Arsyeja_e_anullimit,Pezulluar,Fituesi,NIPT_i_fituesit,Vlera_e_fituesit,Vlera_e_fituesit_ne_lidhjen_e_kontrates,"
        "Lidhja_e_kontrates_me_TVSH,Numri_i_ofertave_te_dorezuara,Numri_i_ofertave_te_kualifikuara,Kodet_CPV")
# the first three as APP's file of 22 Nov 2026 has them, then hospitality ones shaped on it
ROWS = [
    'Bashkia Polican,REF-80765-04-02-2026,"’Blerje Vaj Hidraulik ,Vaj Motori Dhe Te Tjera Per Bashkinë Poliçan’’",Small Value,Supplies,,"125,000.00",02.04.2026,02.04.2026,03.04.2026,Jo,,Jo,MOTOREX,K61508501T,"78,350.00",,Jo,20,8,"09135000-4 Vajra karburanti, 09211100-2 Vajra motorike"',
    'Qendra e Studimeve dhe Publikimeve per Arbereshet,REF-88472-06-02-2026,"Shërbim akomodimi në hotel, shërbim taksi hotel- aeroport per 11 persona",Small Value,Services,,,02.06.2026,02.06.2026,03.06.2026,Po,konstaton se dokumentet e tenderit përmbajnë gabime ose mangësi të rëndësishme,Jo,,,,,Jo,0,0," 15800000-6 Produkte ushqimore të ndryshme, 55000000-0 Shërbime hotelerie, restorantesh dhe shërbime të tregtisë me pakicë, 55110000-4 Shërbime të akomodimit në hotele"',
    'Ministria e Turizmit,REF-90001-11-18-2026,Shërbim kateringu për konferencën vjetore,Open,Services,,"2,400,000.00",18.11.2026,18.11.2026,24.11.2026,Jo,,Jo,,,,,Jo,0,0," 55520000-1 Shërbime kateringu"',
    'Bashkia Tirane,REF-90002-11-01-2026,Organizim pritje zyrtare dhe dreke per delegacionin,Small Value,Services,,"800,000.00",01.11.2026,01.11.2026,30.11.2026,Jo,,Jo,,,,,Jo,0,0," 79952000-2 Shërbime eventesh"',
    'Spitali Rajonal Durres,REF-90003-10-01-2026,Blerje ushqime per pacientet,Open,Supplies,,"5,000,000.00",01.10.2026,01.10.2026,20.10.2026,Jo,,Jo,FRESH FOOD,L12345678A,"4,500,000.00","4,500,000.00",Po,3,2," 15800000-6 Produkte ushqimore të ndryshme"',
    'Universiteti i Tiranes,REF-90004-09-01-2026,Sherbim restoranti per studentet,Open,Services,,"3,000,000.00",01.09.2026,01.09.2026,10.09.2026,Jo,,Jo,GASTRO SHPK,K98765432B,"2,700,000.00",,Jo,2,2," 55300000-3 Shërbime restorant dhe shpërndarjeje ushqimesh"',
    'Ministria e Brendshme,REF-90005-11-15-2026,Blerje pajisje zyre per sallen e pritjes,Small Value,Supplies,,"300,000.00",15.11.2026,15.11.2026,25.11.2026,Jo,,Jo,,,,,Jo,0,0," 30190000-7 Furnizime dhe pajisje të ndryshme zyre"']
CSV = HEAD + "\n" + "\n".join(ROWS) + "\n"
TODAY = date(2026, 11, 22)


class Tenders(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = patch.object(tenders, "FILE", Path(self.tmp.name) / "tenders.json")
        self.p.start()
        tenders._held.update(at=0, data=None)

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def rows(self):
        return list(tenders.rows_of(io.StringIO(CSV), tenders.parse_rules(tenders.DEFAULT_RULES)))

    def test_only_hospitality_matches(self):
        refs = {t["ref"]: t for t in self.rows()}
        self.assertNotIn("REF-80765-04-02-2026", refs)                      # hydraulic oil
        self.assertNotIn("REF-90005-11-15-2026", refs)                      # office furniture for the waiting room
        self.assertIn("Accommodation", refs["REF-88472-06-02-2026"]["categories"])
        self.assertEqual(refs["REF-90001-11-18-2026"]["categories"], ["Catering", "Events"])   # kateringu, CPV 5552, konferencën
        self.assertEqual(set(refs["REF-90002-11-01-2026"]["categories"]), {"Catering", "Events"})
        self.assertTrue(refs["REF-90003-10-01-2026"]["related_only"])      # food supplies: related
        self.assertEqual(refs["REF-90001-11-18-2026"]["fund"], 2400000.0)
        self.assertEqual(refs["REF-90001-11-18-2026"]["closes"], "2026-11-24")

    def test_cpv_names_with_commas(self):
        c = tenders.cpv_codes(" 15800000-6 Produkte ushqimore të ndryshme, 55000000-0 Shërbime hotelerie, restorantesh dhe shërbime të tregtisë me pakicë, 55110000-4 Shërbime të akomodimit në hotele")
        self.assertEqual([x[0] for x in c], ["15800000", "55000000", "55110000"])
        self.assertEqual(c[1][1], "Shërbime hotelerie, restorantesh dhe shërbime të tregtisë me pakicë")

    def test_status_and_screen(self):
        v = tenders.view({"tenders": self.rows(), "read_at": "x"}, today=TODAY)
        st = {t["ref"]: t["status"] for t in v["tenders"]}
        self.assertEqual(st["REF-90001-11-18-2026"], "soon")                 # closes on the 24th: in two days
        self.assertEqual(st["REF-90002-11-01-2026"], "open")
        self.assertEqual(st["REF-88472-06-02-2026"], "cancelled")
        self.assertEqual(st["REF-90004-09-01-2026"], "awarded")
        self.assertEqual(v["tenders"][0]["ref"], "REF-90001-11-18-2026")     # the open one closing first
        self.assertEqual(v["kpis"]["open"], 2)
        self.assertEqual(v["kpis"]["open_fund"], 3200000.0)
        self.assertEqual([w["winner"] for w in v["winners"]], ["GASTRO SHPK"])  # food supplies left out
        soon = tenders.view({"tenders": self.rows()}, today=date(2026, 11, 22))["tenders"][0]
        self.assertEqual(soon["days_left"], 2)

    def test_keywords_are_a_setting(self):
        tenders.save_settings("Only events | konferenc | cpv=79952", "geri")
        rows = list(tenders.rows_of(io.StringIO(CSV), tenders.parse_rules(tenders.settings()["rules"])))
        self.assertEqual({t["ref"] for t in rows}, {"REF-90001-11-18-2026", "REF-90002-11-01-2026"})
        with self.assertRaises(ValueError):
            tenders.save_settings("# nothing", "geri")

    def test_read_from_app(self):
        class R:
            status_code = 200
            def iter_lines(self, decode_unicode=False):
                return iter(("﻿" + CSV).encode("utf-8").split(b"\n"))
        with patch.object(tenders.requests, "get", return_value=R()) as g:
            d = tenders.read(refresh=True, today=TODAY)
        self.assertIn("year=2026", g.call_args[0][0])
        self.assertEqual(len(d["tenders"]), 5)
        self.assertFalse(d["errors"])

    def test_who_may_open_it(self):
        self.assertIn("platform.s.tenders.tenders", permissions.PERMISSIONS)
        self.assertNotIn("platform.s.tenders.tenders", permissions.NOT_FOR_ADMIN)
        self.assertTrue(server.tenders_allowed({"username": "x", "permissions": ["platform.s.tenders.tenders"]}))
        self.assertFalse(server.tenders_allowed({"username": "x", "permissions": ["finance.view"]}))
        self.assertFalse(server.tenders_allowed({"username": "x", "report": True, "permissions": ["platform.s.tenders.tenders"]}))


if __name__ == "__main__":
    unittest.main()
