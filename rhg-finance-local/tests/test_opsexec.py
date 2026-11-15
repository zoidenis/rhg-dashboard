"""Operations & Food Safety: the Crunchtime parsers, limits, location mapping, privacy of a slim submission, cuts."""
import unittest

import crunchtime
import opsexec

LIM = dict(crunchtime.DEFAULT_LIMITS)


class Kinds(unittest.TestCase):
    def test_titles(self):
        k = lambda title, cat="Operations": opsexec.kind_of({"title": title, "category_name": cat})
        self.assertEqual(k("Temperature Log - Kuzhina", "Safety"), "temp")
        self.assertEqual(k("Kontroll vaj gatimi", "Safety"), "oil")
        self.assertEqual(k("Gjurmueshmeria e produkteve", "Safety"), "trace")
        self.assertEqual(k("Spoil OPA"), "spoil")
        self.assertEqual(k("Firo e produkteve"), "firo")
        self.assertEqual(k("Konsum stafi"), "staff")
        self.assertEqual(k("Receiving KPI"), "receiving")
        self.assertEqual(k("HACCP auditim", "Safety"), "safety")
        self.assertEqual(k("Vleresim", "Human Resources"), "hr")
        self.assertEqual(k("Hapja e restorantit"), "checklist")
        self.assertEqual(k("Diçka", "Marketing"), "other")


class Temperatures(unittest.TestCase):
    def test_equipment_before_the_reading_decides_the_limit(self):
        ans = [["select", "Zgjidh pajisjen", ["Frigorifer 1"]], ["number", "Temperatura", 6.2],
               ["select", "Zgjidh pajisjen", ["Ngrirje"]], ["number", "Temperatura", -12],
               ["select", "Zgjidh pajisjen", ["Ngrirje"]], ["number", "Temperatura", -8],
               ["number", "Temperatura", "nuk u mat"]]
        self.assertEqual(opsexec.parse_temp(ans, LIM), [("cooler", 6.2, True), ("freezer", -12.0, False), ("freezer", -8.0, True)])

    def test_unknown_equipment_is_never_out_of_range(self):
        self.assertEqual(opsexec.parse_temp([["number", "Temperatura", 40]], LIM), [("other", 40.0, False)])

    def test_limits_text(self):
        self.assertEqual(crunchtime.limits("cooler_max = 4,5\n# freezer_max = 0\nunknown = 3")["cooler_max"], 4.5)
        self.assertEqual(crunchtime.limits("")["freezer_max"], -10.0)
        with self.assertRaises(ValueError):
            crunchtime.limits("cooler_max = cold")


class Forms(unittest.TestCase):
    def test_oil_yes_is_over(self):
        self.assertEqual(opsexec.parse_oil([["yesno", "A tregon matesi me shume se 23?", True],
                                            ["yesno", "A tregon matesi me shume se 23?", "false"],
                                            ["yesno", "Vaji u nderrua?", None]]), (2, 1))

    def test_trace_blocks(self):
        ans = [["text", "Produkti", "Mish pule"], ["text", "Furnitori", "Agro"], ["number", "Temperatura", 3.5], ["yesno", "A eshte i pershtatshem?", True],
               ["text", "Produkti", "Peshk"], ["text", "Furnitori", "Detari"], ["number", "Temperatura", 9], ["yesno", "A eshte i pershtatshem?", False]]
        self.assertEqual(opsexec.parse_trace(ans), [("Mish pule", "Agro", 3.5, True), ("Peshk", "Detari", 9.0, False)])

    def test_spoil_and_firo(self):
        self.assertEqual(opsexec.parse_spoil([["select", "Emertimi i produktit", ["Buke"]], ["number", "Sasia", 2], ["select", "Njesia", ["cope"]], ["text", "Arsyeja", "Skaduar"]]),
                         [("Buke", 2.0, "cope", "Skaduar")])
        self.assertEqual(opsexec.parse_firo([["select", "Produkti", ["1) Salmon"]], ["text", "Furnitori", "Detari"], ["text", "Numri i fatures", "F1"],
                                             ["number", "Pesha bruto", 10], ["number", "Pesha neto", 7.5]]), ("Salmon", "Detari", "F1", 10.0, 7.5))
        self.assertIsNone(opsexec.parse_firo([["number", "Pesha bruto", 0], ["number", "Pesha neto", 1]]))


class Mapping(unittest.TestCase):
    def test_defaults_lines_and_left_out(self):
        m = crunchtime.mapping("OPA ARENA CENTER = R9\nSALT REST = -")
        reg = {"OPA_B": {}, "LIFT": {}}
        self.assertEqual(opsexec.place_of("OPA BLLOK", reg, m), ("OPA_B", "mapped"))
        self.assertEqual(opsexec.place_of("opa arena center", reg, m), ("R9", "needs review"))
        self.assertEqual(opsexec.place_of("SALT REST", reg, m), ("", "left out"))
        self.assertEqual(opsexec.place_of("ZYRA QENDRORE", reg, m), ("", "support"))


class Privacy(unittest.TestCase):
    def test_slim_drops_names_signatures_and_photos(self):
        s = {"smetadata": {"location": {"id": 3, "name": "LIFT RESTAURANT "}, "date_submitted_local": "2026-11-14T09:12:44", "task": {"id": 1}},
             "answers": [{"field_type": "text", "title": "Emri i punonjesit", "value": "Arben"},
                         {"field_type": "signature", "title": "Firma", "value": "data:image/png"},
                         {"field_type": "photo", "title": "Foto", "value": "https://x/y.jpg"},
                         {"field_type": "number", "title": "<b>Temperatura</b>", "value": 4}]}
        loc, name, when, task, ans = crunchtime.slim(s)
        self.assertEqual((loc, name, when, task), (3, "LIFT RESTAURANT", "2026-11-14T09:12", True))
        self.assertEqual(ans, [["number", "Temperatura", 4]])

    def test_sensor_time_is_tirana(self):
        self.assertEqual(opsexec._local("2026-11-14T22:45:00Z"), "2026-11-14T23:45")
        self.assertEqual(opsexec._local("2026-07-14T22:45:00"), "2026-07-15T00:45")


class Cut(unittest.TestCase):
    def test_only_allowed_restaurants(self):
        r = {"locations": [{"location": "A", "ref": "OPA_B", "bc": "OPA1", "dept": ""}, {"location": "B", "ref": "LIFT", "bc": "LIFT", "dept": ""}],
             "support": [{"location": "Z"}], "sensors": [{"location": "A"}, {"location": "B"}], "suppliers": [["Agro", 1, 0, 3, 3]]}
        c = opsexec.cut(r, ["opa1"])
        self.assertEqual([x["location"] for x in c["locations"]], ["A"])
        self.assertEqual(c["sensors"], [{"location": "A"}])
        self.assertEqual((c["support"], c["suppliers"]), ([], []))
        self.assertIs(opsexec.cut(r, None), r)


if __name__ == "__main__":
    unittest.main()
