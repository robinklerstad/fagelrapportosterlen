#!/usr/bin/env python3
"""Tester för upprepningsminnet i generate_report.py.

BAKGRUND (2026-07-30): `recent_scripts()` injicerade fyra KOMPLETTA manus (~7 000
tecken) i prompten. För en språkmodell är det inte kontext att undvika utan fyra
EXEMPEL att imitera. Följden var likformiga avsnitt, reproducerade faktafel
(tornseglaren som svala) och ett engångsinslag som bar sig vidare av sig själv.

Nu matas bara en kompakt lista in: använda artfakta + inledningar.

Inga nätanrop. Kör: python3 test_upprepning.py
"""

import os
import json
import tempfile
import unittest
from pathlib import Path

_TMP = Path(tempfile.mkdtemp())
os.environ["TEST_OUTPUT_DIR"] = str(_TMP)
os.environ.setdefault("BW_STATION_ID", "28650")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")
os.environ.setdefault("SITE_BASE_URL", "https://example.com")

import generate_report as gr


def clear():
    if gr.FACT_LOG_PATH.exists():
        gr.FACT_LOG_PATH.unlink()
    gr.EPISODES_DIR.mkdir(parents=True, exist_ok=True)
    for p in gr.EPISODES_DIR.glob("*.txt"):
        p.unlink()


class ParseDialogue(unittest.TestCase):
    """Nytt objektformat + bakåtkompatibilitet med det gamla listformatet."""

    def test_new_object_format(self):
        raw = json.dumps({"turns": [{"speaker": "Astrid", "text": "God morgon"}],
                          "anvand_fakta": ["gråhäger/familj"]}, ensure_ascii=False)
        turns, used = gr._parse_dialogue(raw)
        self.assertEqual(len(turns), 1)
        self.assertEqual(used, ["gråhäger/familj"])

    def test_old_bare_list_still_works(self):
        # En promptfil och en kodfil kan hamna i osynk vid uppladdning. Då ska
        # podden gå utan faktalogg snarare än inte gå alls.
        raw = json.dumps([{"speaker": "Astrid", "text": "God morgon"}])
        turns, used = gr._parse_dialogue(raw)
        self.assertEqual(len(turns), 1)
        self.assertEqual(used, [])

    def test_code_fences_are_stripped(self):
        raw = '```json\n{"turns": [{"speaker": "Erik", "text": "Hej"}], "anvand_fakta": []}\n```'
        turns, used = gr._parse_dialogue(raw)
        self.assertEqual(turns[0]["text"], "Hej")

    def test_non_list_anvand_fakta_is_coerced(self):
        raw = json.dumps({"turns": [{"speaker": "Astrid", "text": "x"}],
                          "anvand_fakta": "gråsparv/kosthallning"})
        _, used = gr._parse_dialogue(raw)
        self.assertEqual(used, ["gråsparv/kosthallning"])

    def test_unparsable_becomes_a_monologue(self):
        turns, used = gr._parse_dialogue("bara löptext, ingen json")
        self.assertEqual(len(turns), 1)
        self.assertEqual(used, [])
        self.assertIn("löptext", turns[0]["text"])

    def test_empty_turns_falls_through_to_monologue(self):
        turns, _ = gr._parse_dialogue(json.dumps({"turns": [], "anvand_fakta": []}))
        self.assertEqual(len(turns), 1)


class FactLog(unittest.TestCase):
    def setUp(self):
        clear()

    def test_roundtrip(self):
        gr.save_fact_log("2026-07-30", ["gråhäger/familj", "gråsparv/kosthallning"])
        self.assertEqual(gr.load_fact_log()["2026-07-30"],
                         ["gråhäger/familj", "gråsparv/kosthallning"])

    def test_deduplicates_and_trims(self):
        gr.save_fact_log("2026-07-30", ["  a/b ", "a/b", "", None])
        self.assertEqual(gr.load_fact_log()["2026-07-30"], ["a/b"])

    def test_keeps_only_last_30_days(self):
        for d in range(1, 41):
            gr.save_fact_log(f"2026-06-{d:02d}" if d <= 30 else f"2026-07-{d-30:02d}",
                             [f"art{d}/familj"])
        self.assertEqual(len(gr.load_fact_log()), 30)

    def test_corrupt_log_is_ignored(self):
        gr.FACT_LOG_PATH.write_text("{ trasig", encoding="utf-8")
        self.assertEqual(gr.load_fact_log(), {})

    def test_missing_log_is_silent(self):
        self.assertEqual(gr.load_fact_log(), {})


class RecentOpenings(unittest.TestCase):
    """OMSKRIVEN 2026-07-31: `recent_context()` blandade två saker som nu hör hemma
    i olika anrop. Faktarotationen sköts av faktaloggen i ANROP 1 (och som filter i
    build_facts); inledningarna hör till ANROP 2. Anrop 2 ska aldrig se ett
    faktanamn – därför är funktionen delad."""

    def setUp(self):
        clear()

    def _episode(self, date, first_line):
        (gr.EPISODES_DIR / f"{date}.txt").write_text(
            f"Astrid: {first_line}\n\nErik: Och så vidare.", encoding="utf-8")

    def test_lists_openings_without_full_scripts(self):
        self._episode("2026-07-29", "God morgon och välkommen till Ö24 Bird Data!")
        ctx = gr.recent_openings()
        self.assertIn("God morgon och välkommen", ctx)
        # Resten av manuset ska INTE med – det är hela poängen.
        self.assertNotIn("Och så vidare", ctx)

    def test_speaker_prefix_is_stripped_from_opening(self):
        self._episode("2026-07-29", "God morgon!")
        self.assertNotIn("Astrid:", gr.recent_openings())

    def test_is_compact(self):
        # Gamla lösningen var ~7000 tecken. Den nya ska vara en bråkdel.
        for i in range(4):
            self._episode(f"2026-07-2{6+i}", "God morgon och välkommen till Ö24 Bird "
                                             "Data! Det är en fin dag med mycket att "
                                             "berätta om " + "x" * 200)
        ctx = gr.recent_openings()
        self.assertLess(len(ctx), 1200, f"upprepningsminnet svällde: {len(ctx)} tecken")

    def test_only_n_most_recent(self):
        for i in range(1, 9):
            self._episode(f"2026-07-{i:02d}", f"Inledning nummer {i}")
        ctx = gr.recent_openings(n=2)
        self.assertIn("nummer 8", ctx)
        self.assertNotIn("nummer 1 ", ctx)

    def test_empty_when_nothing_exists(self):
        self.assertEqual(gr.recent_openings(), "")

    def test_data_txt_files_are_not_treated_as_scripts(self):
        (gr.EPISODES_DIR / "2026-07-29.data.txt").write_text(
            "RÅDATA: gråsparv, kaja", encoding="utf-8")
        self.assertNotIn("RÅDATA", gr.recent_openings())

    def test_facts_txt_files_are_not_treated_as_scripts(self):
        # NY 2026-07-31. Punktlistan ligger bredvid manuset i episodes/. Utan
        # filtreringen skulle dess rubrikrad matas in som "föregående inledning".
        (gr.EPISODES_DIR / "2026-07-29.facts.txt").write_text(
            "Punktlista 2026-07-29 – underlaget för avsnittet", encoding="utf-8")
        self.assertNotIn("Punktlista", gr.recent_openings())


class PromptIntegration(unittest.TestCase):
    def setUp(self):
        clear()

    def _today(self):
        return {"date": "2026-07-30", "species_count": 2,
                "top_species": [
                    {"name": "gråsparv", "scientific": "Passer domesticus",
                     "display": "gråsparv", "activity": "enstaka"},
                    {"name": "kaja", "scientific": "Coloeus monedula",
                     "display": "kaja", "activity": "ofta hord"}]}

    def test_faktaprompten_numrerar_punkterna_och_visar_loggen(self):
        gr.save_fact_log("2026-07-29", ["gråhäger/familj"])
        today = self._today()
        sig = gr.derive_signals(today, {"species_ever": {}, "recent_days": []})
        facts = gr.build_facts(today, sig, gr.load_fact_log())
        prompt = gr.build_facts_prompt(facts, gr.load_fact_log())
        self.assertNotIn("{{PUNKTER}}", prompt)
        self.assertIn("gråhäger/familj", prompt)
        self.assertIn("1. ", prompt)

    def test_tonprompten_ser_varken_tal_eller_faltnamn(self):
        """Kärnan i hela upplägget: får anrop 2 'tionde dygnet i rad' som färdig
        fras finns inget att räkna på."""
        today = self._today()
        sig = gr.derive_signals(today, {"species_ever": {}, "recent_days": []})
        facts = gr.build_facts(today, sig, {})
        rader = gr.punktlista(facts, gr._valbara(facts)[:5], today)
        prompt = gr.build_tone_prompt(rader)
        self.assertNotIn("{{PUNKTLISTA}}", prompt)
        self.assertIn("inget att undvika än", prompt)
        # Bara punktlistedelen granskas – promptens egen brödtext får innehålla
        # exempelord som "[warmly]" och citerade felformuleringar.
        block = prompt.split("DAGENS PUNKTLISTA")[1].split("DEN ENDA REGELN")[0]
        self.assertFalse(any(c.isdigit() for c in block), block)
        for falt in gr.FALTNAMN:
            self.assertNotIn(falt, block.lower())

    def test_tonprompten_ar_vasentligt_kortare_an_den_gamla(self):
        # Den gamla dialog-prompten var 330 rader med femton absoluta förbud.
        rader = [gr._punkt("fredagen den trettionde juli", "ram", alltid_med=True)]
        prompt = gr.build_tone_prompt(rader)
        self.assertLess(len(prompt.splitlines()), 120,
                        "ton-prompten växer – faktaregler hör i frasverket")


if __name__ == "__main__":
    unittest.main(verbosity=2)
