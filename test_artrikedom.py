#!/usr/bin/env python3
"""Tester för artrikedomens signifikans i generate_report.py.

Bakgrund: den gamla logiken satte nytt_rekord=True så snart dygnet slog det gamla
med EN art, vilket gjorde avsnitten exalterade över normal dygnsvariation
("tjugosju arter, ett kliv uppåt, fem fler än igår!"). Kalibrerat 2026-07-30 mot
19 dygns verklig historik: median 19 arter, normalvariation ca ±3.

Inga nätanrop. Kör: python3 test_artrikedom.py
"""

import os
import unittest

os.environ.setdefault("BW_STATION_ID", "28650")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")
os.environ.setdefault("SITE_BASE_URL", "https://example.com")

import generate_report as gr

# Den verkliga historiken 2026-07-11..29 (ur history.json).
VERKLIG = [19, 21, 17, 18, 22, 16, 16, 19, 18, 6, 4, 19, 18, 23, 26, 22, 26, 23, 22]


class Median(unittest.TestCase):
    def test_odd_and_even(self):
        self.assertEqual(gr._median([3, 1, 2]), 2)
        self.assertEqual(gr._median([4, 1, 2, 3]), 2.5)

    def test_empty(self):
        self.assertIsNone(gr._median([]))


class Omdome(unittest.TestCase):
    def test_normal_day_gives_no_verdict(self):
        # Median 19; 19-22 ligger inom bruset -> ingen notering alls.
        for idag in (17, 19, 21, 22):
            self.assertIsNone(gr.artrikedom_omdome(idag, VERKLIG), idag)

    def test_clearly_rich_day(self):
        self.assertEqual(gr.artrikedom_omdome(23, VERKLIG), "artrikt")
        self.assertEqual(gr.artrikedom_omdome(25, VERKLIG), "ovanligt artrikt")

    def test_clearly_poor_day(self):
        self.assertEqual(gr.artrikedom_omdome(15, VERKLIG), "magert")
        self.assertEqual(gr.artrikedom_omdome(12, VERKLIG), "ovanligt magert")

    def test_station_outage_days_are_flagged_as_poor(self):
        # 2026-07-20/21 gav 6 och 4 arter (sannolikt haveri) – ska synas som
        # ovanligt magert, inte som ett normalt dygn.
        self.assertEqual(gr.artrikedom_omdome(6, VERKLIG), "ovanligt magert")

    def test_outages_do_not_drag_the_baseline(self):
        # Median (19) ska inte påverkas av de två haverdygnen så som ett
        # medelvärde (18,7 -> men känsligt) skulle göra. Ett dygn på 22 arter är
        # normalt, inte "artrikt".
        self.assertIsNone(gr.artrikedom_omdome(22, VERKLIG))

    def test_thin_history_gives_no_verdict(self):
        # Med färre än OMDOME_MIN_DAGAR dygn finns ingen baslinje att döma mot.
        self.assertIsNone(gr.artrikedom_omdome(30, [19, 21, 17]))

    def test_zero_counts_are_ignored(self):
        self.assertIsNone(gr.artrikedom_omdome(0, VERKLIG))


class Rekord(unittest.TestCase):
    """Rekord ska vara en händelse, inte något som inträffar var femte dygn."""

    def _signals(self, counts, idag):
        recent = [{"date": f"2026-07-{11 + i:02d}", "species_count": c, "top": []}
                  for i, c in enumerate(counts)]
        history = {"species_ever": {}, "recent_days": recent}
        today = {"date": "2026-07-30", "species_count": idag,
                 "top_species": [{"name": "gråsparv", "scientific": "Passer domesticus",
                                  "display": "gråsparv", "activity": "enstaka"}]}
        return gr.derive_signals(today, history)

    def test_one_species_over_record_is_not_a_record(self):
        # Det konkreta klagomålet: 27 mot 26 ska INTE vara ett rekord.
        sig = self._signals(VERKLIG, 27)
        self.assertFalse(sig["artrikedom_kontext"]["nytt_rekord"])

    def test_two_species_over_record_is_not_a_record(self):
        self.assertFalse(self._signals(VERKLIG, 28)["artrikedom_kontext"]["nytt_rekord"])

    def test_clear_margin_is_a_record(self):
        self.assertTrue(self._signals(VERKLIG, 29)["artrikedom_kontext"]["nytt_rekord"])

    def test_old_logic_would_have_fired_four_times_new_logic_once(self):
        # Regressionsvakt mot hela problemet: räkna hur ofta rekord utlöses över
        # den verkliga historiken, gammal regel (> rekord) mot ny (>= rekord + 3).
        gammal = ny = 0
        for i, c in enumerate(VERKLIG):
            tidigare = [x for x in VERKLIG[:i] if x]
            if not tidigare:
                continue
            rek = max(tidigare)
            if c > rek:
                gammal += 1
            if c >= rek + gr.REKORD_MARGINAL:
                ny += 1
        self.assertEqual(gammal, 4, "gamla regeln utlöste var femte dygn")
        self.assertEqual(ny, 1, "nya regeln ska utlösa sällan")


class SamladMening(unittest.TestCase):
    """Ett omdöme som inte kan motsäga sig självt.

    Utlösaren: "ett artrikt sådant ... klart färre än igår – men artrikt är det ändå"
    (2026-07-31). Båda påståendena var sanna, men modellen fick väga dem själv."""

    def test_rich_but_down_from_yesterday_is_one_coherent_sentence(self):
        m = gr._artrikedom_mening("artrikt", -8)
        self.assertEqual(m, "fortsatt högt i artrikedom, om än en bit under igår")
        self.assertNotIn("men artrikt", m)

    def test_rich_and_up(self):
        self.assertIn("tydligt fler", gr._artrikedom_mening("ovanligt artrikt", 9))

    def test_poor_but_up_from_yesterday(self):
        self.assertEqual(gr._artrikedom_mening("magert", 8),
                         "fortfarande i det magrare spannet, men fler arter än igår")

    def test_normal_level_with_real_shift(self):
        self.assertIn("tydligt färre", gr._artrikedom_mening(None, -9))
        self.assertIn("tydligt fler", gr._artrikedom_mening(None, 9))

    def test_nothing_to_say_stays_silent(self):
        # Normal nivå OCH skillnad inom bruset -> inget fält, inget att nämna.
        self.assertIsNone(gr._artrikedom_mening(None, 1))
        self.assertIsNone(gr._artrikedom_mening(None, None))

    def test_level_alone_when_shift_is_noise(self):
        self.assertEqual(gr._artrikedom_mening("ovanligt artrikt", 2), "ovanligt artrikt")

    def test_signals_carry_the_sentence(self):
        recent = [{"date": f"2026-07-{11+i:02d}", "species_count": c, "top": []}
                  for i, c in enumerate(VERKLIG)]
        today = {"date": "2026-07-31", "species_count": 24,
                 "top_species": [{"name": "gråsparv", "scientific": "Passer domesticus",
                                  "display": "gråsparv", "activity": "enstaka"}]}
        sig = gr.derive_signals(today, {"species_ever": {}, "recent_days": recent})
        # 24 mot median 19 = artrikt; 24 mot igår 22 = brus -> bara nivån.
        self.assertEqual(sig["artrikedom_kontext"]["sammanfattning"], "artrikt")


class Veckodag(unittest.TestCase):
    """Veckodagen räknas i koden – modellen sa "fredagen den trettionde juli"
    när 2026-07-30 var en torsdag.

    FLYTTAD 2026-07-31: veckodagen skickades förut som fältet `veckodag` i
    _script_view och sattes ihop med datumet av modellen. Nu levereras hela
    datumfrasen färdig av frasverket, så det finns inget att sätta ihop fel."""

    def test_known_weekdays(self):
        self.assertTrue(gr._datumfras("2026-07-30").startswith("torsdagen"))
        self.assertTrue(gr._datumfras("2026-07-31").startswith("fredagen"))
        self.assertTrue(gr._datumfras("2026-08-01").startswith("lördagen"))
        self.assertTrue(gr._datumfras("2026-08-03").startswith("måndagen"))


class Rakneverk(unittest.TestCase):
    """De två totalerna blandades ihop ("tjugotredje dygnet", "tjugotredje arten")."""

    def test_counters_have_unambiguous_names(self):
        recent = [{"date": f"2026-07-{11+i:02d}", "species_count": c, "top": []}
                  for i, c in enumerate(VERKLIG)]
        today = {"date": "2026-07-31", "species_count": 23,
                 "top_species": [{"name": "gråsparv", "scientific": "Passer domesticus",
                                  "display": "gråsparv", "activity": "enstaka"}]}
        sig = gr.derive_signals(today, {"species_ever": {}, "recent_days": recent})
        self.assertEqual(sig["antal_dygn_vi_spelat_in"], len(VERKLIG) + 1)
        self.assertIn("antal_arter_nagonsin_horda", sig)
        # De gamla, förväxlingsbara namnen ska vara borta.
        self.assertNotIn("days_recorded", sig)
        self.assertNotIn("total_species_ever", sig)


class VsYesterday(unittest.TestCase):
    def _vs(self, igar, idag):
        history = {"species_ever": {},
                   "recent_days": [{"date": "2026-07-29", "species_count": igar, "top": []}]}
        today = {"date": "2026-07-30", "species_count": idag,
                 "top_species": [{"name": "gråsparv", "scientific": "Passer domesticus",
                                  "display": "gråsparv", "activity": "enstaka"}]}
        return gr.derive_signals(today, history)["vs_yesterday"]

    def test_small_difference_is_noise(self):
        # "Fem fler än igår" var det som lät som en händelse. 22 -> 25 är brus.
        for igar, idag in ((22, 25), (26, 27), (23, 22), (20, 20)):
            self.assertEqual(self._vs(igar, idag)["forandring"], "i nivå med igår",
                             f"{igar}->{idag}")

    def test_real_shift_gets_words(self):
        self.assertEqual(self._vs(18, 26)["forandring"], "klart fler arter än igår")
        self.assertEqual(self._vs(26, 18)["forandring"], "klart färre arter än igår")

    def test_raw_counts_are_still_available(self):
        vs = self._vs(22, 25)
        self.assertEqual(vs["artrikedom_igar"], 22)
        self.assertEqual(vs["artrikedom_idag"], 25)


if __name__ == "__main__":
    unittest.main(verbosity=2)
