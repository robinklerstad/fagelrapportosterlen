#!/usr/bin/env python3
"""Tester för artnamnstabellen (species_sv.json).

BAKGRUND – felet dessa tester finns för att förhindra, 2026-08-04:

  Namnuppslaget gjordes i den dagliga körningen. GBIF:s `species/match` gav
  ingen usageKey för "Chloris chloris" (Chloris är också ett grässläkte), så
  uppslaget returnerade None och cachades som tom sträng. `display` föll då
  tillbaka på det VETENSKAPLIGA namnet, punktlistan bar "Chloris chloris" i
  tre punkter, och anrop 2 sa latinet och gissade ett svenskt namn ur eget
  minne: "grönsiskan". Grönsiska är Spinus spinus. Chloris chloris är grönfink.
  Fel art, i sändning, som huvudsak.

Det viktigaste testet här är HISTORIKTÄCKNINGEN. Den ska ligga kvar permanent
och falla högt. När tabellen togs i bruk visade den omedelbart att Dyntaxa inte
har `Corvus cornix` eller `Corvus monedula` som accepterade arter – BirdNET och
Dyntaxa följer olika checklistor. Utan de två alias-raderna hade kaja och kråka
tystnat i podden, kajan mitt i en svit på tjugofem dygn, utan ett felmeddelande.

Byter BirdWeather checklista (AviList 2025 slår t.ex. ihop kråka och svartkråka)
kan de vetenskapliga namnen ändras och fler alias behövas. Då ska detta test
falla på din maskin, inte tystna klockan 06:08.
"""

import json
import os
import re
import unittest
from pathlib import Path

os.environ.setdefault("BW_STATION_ID", "28650")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")
os.environ.setdefault("SITE_BASE_URL", "https://example.com")

import generate_report as gr

TABELL_FIL = Path("species_sv.json")
OVERRIDE_FIL = Path("species_namn_override.json")
HISTORIK_FIL = Path("history.json")

# Ett vetenskapligt binomen, gement jämfört: "chloris chloris".
BINOMEN = re.compile(r"^[a-zåäö]+ [a-zåäö]+$")

# Namn som redan varit i sändning. Ändras ett av dessa har en lyssnare hört ett
# annat namn på samma fågel, och det ska vara ett medvetet beslut – inte en
# bieffekt av att tabellen byggts om. Hämtade ur produktionens species_sv.json
# 2026-08-04, efter att grönfinken lagats.
SANDA_NAMN = {
    "Anas platyrhynchos": "gräsand",
    "Anser anser": "grågås",
    "Apus apus": "tornseglare",
    "Ardea cinerea": "gråhäger",
    "Carduelis carduelis": "steglits",
    "Chloris chloris": "grönfink",
    "Columba palumbus": "ringduva",
    "Corvus cornix": "kråka",
    "Corvus monedula": "kaja",
    "Cyanistes caeruleus": "blåmes",
    "Delichon urbicum": "hussvala",
    "Haematopus ostralegus": "strandskata",
    "Hirundo rustica": "ladusvala",
    "Larus argentatus": "gråtrut",
    "Larus canus": "fiskmås",
    "Larus michahellis": "medelhavstrut",
    "Linaria cannabina": "hämpling",
    "Motacilla alba": "sädesärla",
    "Muscicapa striata": "grå flugsnappare",
    "Parus major": "talgoxe",
    "Passer domesticus": "gråsparv",
    "Passer montanus": "pilfink",
    "Phylloscopus collybita": "gransångare",
    "Riparia riparia": "backsvala",
    "Spinus spinus": "grönsiska",
    "Sturnus vulgaris": "stare",
    "Thalasseus sandvicensis": "kentsk tärna",
    "Tringa glareola": "grönbena",
}


def _tabell():
    return json.loads(TABELL_FIL.read_text(encoding="utf-8"))


class TabellensForm(unittest.TestCase):
    def test_tabellen_finns_och_ar_inte_tom(self):
        self.assertTrue(TABELL_FIL.exists(), f"{TABELL_FIL} saknas – kör bygg_artnamn.py")
        self.assertGreater(len(gr._load_sv_cache()), 500,
                           "tabellen ska täcka hela svenska Aves, inte bara hörda arter")

    def test_metadata_raknas_inte_som_art(self):
        """_attribution ligger i filen men är ingen art."""
        self.assertNotIn("_attribution", gr._load_sv_cache())

    def test_inget_namn_ar_tomt(self):
        for sci, sv in gr._load_sv_cache().items():
            self.assertTrue(sv and sv.strip(), f"{sci} har tomt namn – det var 08-04-buggen")

    def test_inget_namn_ar_ett_vetenskapligt_binomen(self):
        """Kärnan i 08-04: ett latinskt namn får aldrig stå som svenskt namn."""
        for sci, sv in gr._load_sv_cache().items():
            self.assertFalse(BINOMEN.match(sv) and sv.lower() == sci.lower(),
                             f"{sci} -> {sv!r} är det vetenskapliga namnet")

    def test_inget_namn_ar_en_rasbeskrivning(self):
        """Dyntaxa bär strängar som 'gulfotad gråtrut, rasen michahellis'. De är
        inte talbara namn och ska aldrig hamna i tabellen."""
        for sci, sv in gr._load_sv_cache().items():
            self.assertIsNone(re.search(r",|\brasen\b|\bunderarten\b", sv, re.I),
                              f"{sci} -> {sv!r} är en rasbeskrivning, inte ett namn")

    def test_namnen_ar_gemena(self):
        for sci, sv in gr._load_sv_cache().items():
            self.assertEqual(sv, sv.lower(), f"{sci} -> {sv!r} ska vara gement")


class SandaNamnAndrasInte(unittest.TestCase):
    """Regression: namn som redan hörts i podden ska ligga kvar oförändrade."""

    def test_alla_sanda_namn_finns_kvar(self):
        tabell = gr._load_sv_cache()
        for sci, sv in sorted(SANDA_NAMN.items()):
            self.assertIn(sci, tabell, f"{sci} har varit i sändning men saknas i tabellen")
            self.assertEqual(tabell[sci], sv,
                             f"{sci} hette {sv!r} i sändning, tabellen säger {tabell[sci]!r}")

    def test_gronfink_och_gronsiska_ar_inte_samma_art(self):
        """Det konkreta felet 2026-08-04: modellen sa grönsiska om en grönfink."""
        tabell = gr._load_sv_cache()
        self.assertEqual(tabell.get("Chloris chloris"), "grönfink")
        self.assertEqual(tabell.get("Spinus spinus"), "grönsiska")


class HistorikenGarAttOversatta(unittest.TestCase):
    """DET VIKTIGASTE TESTET. Varje art stationen någonsin hört ska gå att namnge.

    Faller detta har BirdWeather skickat ett vetenskapligt namn tabellen inte
    känner – och den arten skulle uteslutas ur avsnittet. Fixen är en rad i
    species_namn_override.json, inte en ändring här."""

    def test_varje_vetenskapligt_namn_i_historiken_finns_i_tabellen(self):
        # Alla historikfiler som finns: repots egen och ev. synkad testdata.
        # Den färskaste avgör – en art som hördes i går är precis den som kan
        # saknas i tabellen.
        filer = [p for p in (HISTORIK_FIL, Path("test_output/history.json"))
                 if p.exists()]
        if not filer:
            self.skipTest("ingen history.json hittad")
        tabell = gr._load_sv_cache()

        # Historiken bär 33 ENGELSKA artnamn från tiden före GBIF-fixen. De är
        # inte vetenskapliga namn och ska inte krävas översatta – bara binomen.
        binomen = set()
        for p in filer:
            hist = json.loads(p.read_text(encoding="utf-8"))
            binomen |= {k for k in hist.get("species_ever", {})
                        if re.match(r"^[A-Z][a-z]+ [a-z]+$", k)}
        saknas = sorted(k for k in binomen if k not in tabell)
        self.assertEqual(saknas, [], (
            f"{len(saknas)} arter i historiken går inte att namnge: {saknas}. "
            "Lägg en alias-rad per art i species_namn_override.json. "
            "Vanligaste orsaken: BirdNET och Dyntaxa följer olika checklistor."))


class OverstyrningsfilenArGiltig(unittest.TestCase):
    def test_filen_gar_att_lasa_och_har_ratt_form(self):
        self.assertTrue(OVERRIDE_FIL.exists(), f"{OVERRIDE_FIL} saknas")
        ov = json.loads(OVERRIDE_FIL.read_text(encoding="utf-8"))
        for nyckel in ("alias", "overstyrning"):
            self.assertIn(nyckel, ov)
            self.assertIsInstance(ov[nyckel], dict)

    def test_varje_rad_har_ett_namn_och_en_motivering(self):
        ov = json.loads(OVERRIDE_FIL.read_text(encoding="utf-8"))
        for grupp in ("alias", "overstyrning"):
            for sci, rec in ov[grupp].items():
                self.assertIsInstance(rec, dict, f"{sci}: skriv {{namn, varfor}}")
                self.assertTrue(rec.get("namn"), f"{sci} saknar namn")
                self.assertTrue(rec.get("varfor"),
                                f"{sci} saknar motivering – en handmatad rad "
                                "utan skäl går inte att granska senare")

    def test_overstyrningarna_har_slagit_igenom_i_tabellen(self):
        ov = json.loads(OVERRIDE_FIL.read_text(encoding="utf-8"))
        tabell = gr._load_sv_cache()
        for grupp in ("alias", "overstyrning"):
            for sci, rec in ov[grupp].items():
                self.assertEqual(tabell.get(sci), rec["namn"].lower(),
                                 f"{sci} står i {OVERRIDE_FIL} men inte i tabellen – "
                                 "kör bygg_artnamn.py")


class ArterUtanNamnUtesluts(unittest.TestCase):
    """Ingen återfallsväg till latin eller engelska får finnas kvar."""

    def test_okand_art_far_inget_namn(self):
        arter = [{"scientific": "Nonexistens fabricata", "name": "Fabricated Bird"}]
        onamngivna = gr.swedish_names_for(arter)
        self.assertEqual(len(onamngivna), 1)
        self.assertNotIn("name_sv", arter[0],
                         "en okänd art ska inte få ett namn – den ska uteslutas")

    def test_kand_art_far_sitt_svenska_namn(self):
        arter = [{"scientific": "Chloris chloris", "name": "European Greenfinch"}]
        onamngivna = gr.swedish_names_for(arter)
        self.assertEqual(onamngivna, [])
        self.assertEqual(arter[0]["name_sv"], "grönfink")

    def test_uppslaget_gor_inga_natanrop(self):
        """Den dagliga körningen ska inte kunna falla på att GBIF är nere."""
        import generate_report
        self.assertFalse(hasattr(generate_report, "_gbif_swedish_name"),
                         "runtime-uppslaget ska vara borta ur driften")

    def test_tabellen_skrivs_inte_av_driften(self):
        self.assertFalse(hasattr(gr, "_save_sv_cache"),
                         "den dagliga körningen ska aldrig skriva artnamnstabellen")


if __name__ == "__main__":
    unittest.main(verbosity=2)
