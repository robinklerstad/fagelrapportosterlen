#!/usr/bin/env python3
"""Tester för species_facts.py + integrationen i generate_report.derive_signals.
Alla nätanrop mockas – ingen riktig trafik. Kör: python3 test_species_facts.py"""

import os
import sys
import json
import tempfile
import unittest
from pathlib import Path

# Isolera cache-filerna till en tempmapp INNAN modulen importeras (sökvägarna läses
# på modulnivå ur env).
_TMP = Path(tempfile.mkdtemp())
os.environ["SF_FACTS_CACHE"] = str(_TMP / "species_facts.json")
os.environ["AP_TAXON_CACHE"] = str(_TMP / "species_taxon.json")
os.environ["AP_LOCAL_CACHE"] = str(_TMP / "species_local.json")
os.environ["AVONET_PATH"] = str(_TMP / "avonet.csv")

# Dummy-env så generate_report går att importera (läser secrets på modulnivå).
os.environ.setdefault("BW_STATION_ID", "28650")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")
os.environ.setdefault("SITE_BASE_URL", "https://example.com")

import species_facts as sf

FACTS = Path(os.environ["SF_FACTS_CACHE"])
TAXON = Path(os.environ["AP_TAXON_CACHE"])
AVONET = Path(os.environ["AVONET_PATH"])


def write_facts(species, meta=None):
    """Skriv en cache med _meta + arter, som build() gör."""
    cache = {"_meta": meta or {"version": 1, "byggd": "2026-08-02"}}
    cache.update(species)
    FACTS.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")


def day(*pairs):
    """Bygg en dagslista i samma form som generate_report skickar in."""
    return [{"scientific": sci, "display": disp} for sci, disp in pairs]


def clear():
    for p in (FACTS, TAXON, AVONET):
        if p.exists():
            p.unlink()


def _build_minimal_xlsx(path, sheet_name="AVONET2_eBird"):
    """Snickra en minimal xlsx för hand (zip + XML) så testet inte behöver openpyxl.
    Använder inline-strängar, vilket är den variant som är enklast att skriva."""
    import zipfile

    rows = [
        ["Species2", "Family2", "Order2", "Migration", "Primary.Lifestyle",
         "Trophic.Niche", "Habitat", "Mass"],
        ["Ardea cinerea", "Ardeidae", "Pelecaniformes", "2", "Aquatic",
         "Aquatic predator", "Wetland", "1443"],
    ]

    def cell(col, r, val):
        ref = f"{chr(65 + col)}{r}"
        return (f'<c r="{ref}" t="inlineStr"><is><t>{val}</t></is></c>')

    xml_rows = "".join(
        f'<row r="{i + 1}">' + "".join(cell(j, i + 1, v) for j, v in enumerate(row))
        + "</row>"
        for i, row in enumerate(rows))
    sheet = ('<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org'
             f'/spreadsheetml/2006/main"><sheetData>{xml_rows}</sheetData></worksheet>')
    workbook = ('<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org'
                '/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org'
                '/officeDocument/2006/relationships"><sheets>'
                f'<sheet name="{sheet_name}" sheetId="1" r:id="rId1"/>'
                "</sheets></workbook>")
    rels = ('<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org'
            '/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/'
            'sheet1.xml" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/worksheet"/></Relationships>')

    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", rels)
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    return path


# ---------------------------------------------------------------------------
# Kolumnmatchning och cellstädning
# ---------------------------------------------------------------------------
class ColumnMatching(unittest.TestCase):
    def test_norm_col_ignores_punctuation_and_case(self):
        for variant in ("Trophic.Niche", "Trophic Niche", "trophic_niche", "TROPHICNICHE"):
            self.assertEqual(sf._norm_col(variant), "trophicniche", variant)

    def test_pick_col_finds_first_matching_candidate(self):
        cols = ["Species2", "Migration", "Primary.Lifestyle"]
        self.assertEqual(sf._pick_col(cols, "Species1", "Species2"), "Species2")
        self.assertEqual(sf._pick_col(cols, "PrimaryLifestyle"), "Primary.Lifestyle")

    def test_pick_col_returns_none_when_absent(self):
        self.assertIsNone(sf._pick_col(["Species2"], "Habitat"))

    def test_clean_treats_na_variants_as_missing(self):
        for junk in ("", "  ", "NA", "nan", "NULL", "-"):
            self.assertIsNone(sf._clean(junk), repr(junk))
        self.assertEqual(sf._clean("  Aerial "), "Aerial")

    def test_as_int_accepts_float_strings(self):
        # AVONET-export via R skriver ofta heltal som "2.0".
        self.assertEqual(sf._as_int("2.0"), 2)
        self.assertEqual(sf._as_int(3), 3)
        self.assertIsNone(sf._as_int("NA"))


# ---------------------------------------------------------------------------
# AVONET-läsning
# ---------------------------------------------------------------------------
AVONET_CSV = """Species2,Family2,Order2,Migration,Primary.Lifestyle,Trophic.Niche,Habitat,Mass,Hand-Wing.Index,Beak.Depth
Passer domesticus,Passeridae,Passeriformes,1,Terrestrial,Granivore,Human Modified,28.0,27.3,8.4
Ardea cinerea,Ardeidae,Pelecaniformes,2,Aquatic,Aquatic predator,Wetland,1443.0,32.0,25.9
Apus apus,Apodidae,Apodiformes,3,Aerial,Invertivore,Human Modified,42.0,71.6,3.1
Turdus merula,Turdidae,Passeriformes,2,Terrestrial,Omnivore,Forest,102.0,23.1,7.5
Dendrocopos major,Picidae,Piciformes,1,Insessorial,Omnivore,Woodland,74.9,11.8,7.9
Obscurus fictus,NA,NA,NA,NA,NA,NA,NA,NA,NA
"""


class ReadAvonet(unittest.TestCase):
    def setUp(self):
        clear()
        AVONET.write_text(AVONET_CSV, encoding="utf-8")

    def test_translates_enums_to_swedish(self):
        got = sf.read_avonet(wanted={"Passer domesticus"})
        self.assertEqual(got["Passer domesticus"], {
            "_flytt_globalt": "stannfågel",
            "levnadssatt": "marklevande",
            "kosthallning": "fröätare",
            "habitat": "människopräglad mark",
            "vikt_g": 28.0,
            "_familj_latin": "Passeridae",
            "_ordning_latin": "Passeriformes",
            "_hwi": 27.3,
            "_nabbdjup": 8.4,
        })
        # HWI 27,3 ligger i mittfältet -> ingen vingform, hellre tyst.
        self.assertNotIn("vingform", got["Passer domesticus"])

    def test_latin_family_is_captured_for_later_translation(self):
        got = sf.read_avonet(wanted={"Ardea cinerea"})
        self.assertEqual(got["Ardea cinerea"]["_familj_latin"], "Ardeidae")
        # Latinet får ALDRIG bli det talade fältet.
        self.assertNotIn("familj", got["Ardea cinerea"])

    def test_wing_shape_from_hand_wing_index(self):
        got = sf.read_avonet()
        self.assertEqual(got["Apus apus"]["vingform"], "långa, spetsiga vingar")
        self.assertEqual(got["Dendrocopos major"]["vingform"], "korta, rundade vingar")
        # Mittfältet ska tiga – "medellånga vingar" säger ingenting.
        for art in ("Passer domesticus", "Turdus merula", "Ardea cinerea"):
            self.assertNotIn("vingform", got[art], art)

    def test_wing_shape_thresholds(self):
        self.assertEqual(sf.wing_shape(74.3), "långa, spetsiga vingar")   # max
        self.assertEqual(sf.wing_shape(71.6), "långa, spetsiga vingar")   # tornseglare
        self.assertEqual(sf.wing_shape(65.0), "långa, spetsiga vingar")
        self.assertIsNone(sf.wing_shape(64.9))
        self.assertIsNone(sf.wing_shape(21.1))          # global median
        self.assertIsNone(sf.wing_shape(12.4))
        self.assertEqual(sf.wing_shape(12.3), "korta, rundade vingar")
        self.assertIsNone(sf.wing_shape(None))

    def test_soaring_raptors_and_gulls_get_no_wing_shape(self):
        # Kalibreringen 2026-07-28: röd glada 49,9 och gråtrut 54,9 ska INTE få
        # "spetsiga vingar" – indexet mäter spetsighet via Kipps avstånd, så långa
        # breda vingar ger höga värden utan att vara spetsiga.
        for hwi in (49.9, 51.2, 54.9, 58.6):
            self.assertIsNone(sf.wing_shape(hwi), hwi)

    def test_beak_depth_is_internal_only(self):
        got = sf.read_avonet(wanted={"Ardea cinerea"})
        self.assertEqual(got["Ardea cinerea"]["_nabbdjup"], 25.9)
        self.assertNotIn("nabbdjup", got["Ardea cinerea"])

    def test_migration_scale(self):
        got = sf.read_avonet()
        self.assertEqual(got["Passer domesticus"]["_flytt_globalt"], "stannfågel")
        self.assertEqual(got["Ardea cinerea"]["_flytt_globalt"], "delvis flyttfågel")
        self.assertEqual(got["Apus apus"]["_flytt_globalt"], "flyttfågel")

    def test_wanted_filters_rows(self):
        got = sf.read_avonet(wanted={"Apus apus"})
        self.assertEqual(list(got), ["Apus apus"])

    def test_row_with_only_na_is_dropped(self):
        # En art utan ett enda användbart värde ska inte belamra cachen.
        self.assertNotIn("Obscurus fictus", sf.read_avonet())

    def test_semicolon_separated_file_also_works(self):
        AVONET.write_text(AVONET_CSV.replace(",", ";"), encoding="utf-8")
        got = sf.read_avonet(wanted={"Turdus merula"})
        self.assertEqual(got["Turdus merula"]["kosthallning"], "allätare")

    def test_missing_file_raises_with_instructions(self):
        AVONET.unlink()
        with self.assertRaises(FileNotFoundError) as ctx:
            sf.read_avonet()
        self.assertIn("figshare", str(ctx.exception))

    def test_reads_xlsx_with_stdlib_only(self):
        # AVONET distribueras som xlsx. Läsaren ska klara det utan openpyxl, och
        # ska välja AVONET-fliken själv så man inte kan råka läsa "Metadata".
        xlsx = _build_minimal_xlsx(_TMP / "avonet_test.xlsx")
        got = sf.read_avonet(xlsx, wanted={"Ardea cinerea"})
        self.assertEqual(got["Ardea cinerea"]["_flytt_globalt"], "delvis flyttfågel")
        self.assertEqual(got["Ardea cinerea"]["kosthallning"], "tar sin föda i vatten")
        self.assertEqual(got["Ardea cinerea"]["_familj_latin"], "Ardeidae")

    def test_xlsx_without_matching_sheet_says_so(self):
        xlsx = _build_minimal_xlsx(_TMP / "fel_flik.xlsx", sheet_name="Blad1")
        with self.assertRaises(ValueError) as ctx:
            sf.read_avonet(xlsx)
        self.assertIn("Blad1", str(ctx.exception))

    def test_old_xls_is_rejected_clearly(self):
        xls = _TMP / "avonet.xls"
        xls.write_text("gammalt format", encoding="utf-8")
        with self.assertRaises(ValueError) as ctx:
            sf.read_avonet(xls)
        self.assertIn(".xlsx", str(ctx.exception))

    def test_alias_matches_avonet_spelling(self):
        # Kajan heter Corvus monedula i pipelinen men Coloeus monedula i eBird/AVONET.
        AVONET.write_text(
            "Species2,Migration,Trophic.Niche,Mass\n"
            "Coloeus monedula,1,Omnivore,228.0\n", encoding="utf-8")
        got = sf.read_avonet(wanted={"Corvus monedula"})
        # Resultatet ska ligga under PIPELINENS stavning, inte AVONETs.
        self.assertIn("Corvus monedula", got)
        self.assertNotIn("Coloeus monedula", got)
        self.assertEqual(got["Corvus monedula"]["kosthallning"], "allätare")

    def test_unknown_enum_value_is_skipped_not_guessed(self):
        AVONET.write_text(
            "Species2,Migration,Primary.Lifestyle\nCorvus corax,9,Subterranean\n",
            encoding="utf-8")
        got = sf.read_avonet()
        # Okända koder ska inte översättas till något – hellre tomt än fel.
        self.assertNotIn("Corvus corax", got)


# ---------------------------------------------------------------------------
# Rödlista ur species_taxon.json
# ---------------------------------------------------------------------------
class RedlistReuse(unittest.TestCase):
    def setUp(self):
        clear()

    def test_only_noteworthy_categories_are_kept(self):
        TAXON.write_text(json.dumps({
            "Sturnus vulgaris": {"id": 103037, "rodlista": "VU"},
            "Passer domesticus": {"id": 103038, "rodlista": "LC"},
            "Turdus merula": {"id": 102998},
            "Corvus monedula": None,
            "Apus apus": 102976,          # bart int, äldre cacheformat
        }), encoding="utf-8")
        got = sf.redlist_from_taxon_cache()
        self.assertEqual(got, {"Sturnus vulgaris": "VU"})

    def test_missing_cache_is_silent(self):
        self.assertEqual(sf.redlist_from_taxon_cache(), {})


# ---------------------------------------------------------------------------
# facts_for – vad prompten faktiskt får se
# ---------------------------------------------------------------------------
class FactsFor(unittest.TestCase):
    def setUp(self):
        clear()

    def test_returns_swedish_display_name_and_fields(self):
        # Fixturen innehåller BÅDE talbara fält och interna (_-prefix, vikt_g).
        # Bara de talbara ska komma ut – interna fält är en läckagerisk.
        write_facts({"Passer domesticus": {
            "familj": "sparvfinkar", "kosthallning": "fröätare",
            "_flytt_globalt": "stannfågel", "_familj_latin": "Passeridae",
            "vikt_g": 28.0}})
        got = sf.facts_for(day(("Passer domesticus", "gråsparv")))
        self.assertEqual(got, [{
            "art": "gråsparv", "familj": "sparvfinkar",
            "kosthallning": "fröätare"}])

    def test_weight_is_never_exposed_to_the_prompt(self):
        write_facts({"Ardea cinerea": {"vikt_g": 1443.0, "familj": "hägrar"}})
        got = sf.facts_for(day(("Ardea cinerea", "gråhäger")))
        self.assertNotIn("vikt_g", got[0])

    def test_unknown_species_is_silently_omitted(self):
        write_facts({"Passer domesticus": {"familj": "sparvfinkar"}})
        got = sf.facts_for(day(("Nonexistens fictus", "påhittad fågel")))
        self.assertEqual(got, [])

    def test_species_with_no_usable_fields_is_omitted(self):
        # Bara namnet är inget fakta – skicka inte in en tom post.
        write_facts({"Passer domesticus": {"vikt_g": 28.0}})
        self.assertEqual(sf.facts_for(day(("Passer domesticus", "gråsparv"))), [])

    def test_circular_family_is_suppressed(self):
        # "Kärrsnäppan hör till snäpporna" är en rundgång. Låg först som en
        # promptregel och ignorerades TVÅ gånger – därför i koden nu.
        write_facts({
            "Calidris alpina":      {"familj": "snäppor", "habitat": "våtmark"},
            "Haematopus ostralegus": {"familj": "strandskator", "habitat": "kustmiljö"},
            "Ciconia ciconia":      {"familj": "storkar", "habitat": "öppen mark"}})
        got = sf.facts_for(day(("Calidris alpina", "kärrsnäppa"),
                               ("Haematopus ostralegus", "strandskata"),
                               ("Ciconia ciconia", "stork")))
        for e in got:
            self.assertNotIn("familj", e, e["art"])
            self.assertIn("habitat", e, e["art"])   # övriga fält är orörda

    def test_real_family_survives(self):
        # Familjenamn som TILLFÖR något ska komma fram.
        write_facts({
            "Ardea cinerea":     {"familj": "hägrar"},
            "Passer domesticus": {"familj": "sparvfinkar"},
            "Buteo buteo":       {"familj": "hökar"},
            "Sturnus vulgaris":  {"familj": "starar"}})
        got = {e["art"]: e.get("familj") for e in sf.facts_for(
            day(("Ardea cinerea", "gråhäger"), ("Passer domesticus", "gråsparv"),
                ("Buteo buteo", "ormvråk"), ("Sturnus vulgaris", "stare")))}
        self.assertEqual(got["gråsparv"], "sparvfinkar")
        self.assertEqual(got["ormvråk"], "hökar")
        self.assertEqual(got["gråhäger"], "hägrar")
        # "starar" i "stare" ÄR en rundgång och faller bort.
        self.assertIsNone(got.get("stare"))

    def test_stem_handles_swedish_plurals(self):
        self.assertEqual(sf._stem("snäppor"), "snäpp")
        self.assertEqual(sf._stem("strandskator"), "strandskat")
        self.assertEqual(sf._stem("hägrar"), "hägr")

    def test_known_limits_of_the_heuristic(self):
        """Dokumenterar var heuristiken är ojämn. LÄS INNAN DU "FIXAR" DEN.

        Substrängsmatchning kan inte skilja en informativ huvudordsfamilj från en
        rundgång – de är språkligt identiska. Följderna:

        1. tornseglare + "seglare" flaggas som cirkulär, trots att "tornseglaren är
           en seglare" är poddens VIKTIGASTE familjefaktum (den förväxlas med
           svalor). Informationen är INTE förlorad: prompten har en egen
           TORNSEGLAREN-regel som säger just detta. Låt fältet falla.
        2. gråhäger + "hägrar" flaggas INTE, eftersom omljudet gör att stammen
           "hägr" inte är en substräng av "gråhäger". Mildt cirkulärt men ofarligt
           – regeln fyrar bara inte av.

        Att laga (2) kräver riktig svensk morfologi och skulle förvärra (1)."""
        self.assertTrue(sf._familj_ar_cirkular("seglare", "tornseglare"))
        self.assertFalse(sf._familj_ar_cirkular("hägrar", "gråhäger"))

    def test_circular_check_is_conservative(self):
        # Olika ord ska aldrig flaggas, och tomma värden får inte krascha.
        self.assertFalse(sf._familj_ar_cirkular("hökar", "ormvråk"))
        self.assertFalse(sf._familj_ar_cirkular("finkar", "steglits"))
        self.assertFalse(sf._familj_ar_cirkular("", "kärrsnäppa"))
        self.assertFalse(sf._familj_ar_cirkular("hägrar", ""))
        self.assertFalse(sf._familj_ar_cirkular(None, None))

    def test_redlist_is_included(self):
        write_facts({"Sturnus vulgaris": {"familj": "starar", "rodlista": "VU"}})
        got = sf.facts_for(day(("Sturnus vulgaris", "stare")))
        self.assertEqual(got[0]["rodlista"], "VU")

    def test_missing_cache_gives_empty_list(self):
        self.assertEqual(sf.facts_for(day(("Passer domesticus", "gråsparv"))), [])

    def test_corrupt_cache_gives_empty_list(self):
        FACTS.write_text("{ trasig json", encoding="utf-8")
        self.assertEqual(sf.facts_for(day(("Passer domesticus", "gråsparv"))), [])

    def test_meta_key_is_never_treated_as_a_species(self):
        write_facts({"Passer domesticus": {"familj": "sparvfinkar"}})
        got = sf.facts_for(day(("_meta", "_meta")))
        self.assertEqual(got, [])

    def test_species_without_scientific_name_is_skipped(self):
        write_facts({"Passer domesticus": {"familj": "sparvfinkar"}})
        self.assertEqual(sf.facts_for([{"scientific": "", "display": "okänd"}]), [])


# ---------------------------------------------------------------------------
# comparisons – härledda jämförelser
# ---------------------------------------------------------------------------
FIVE = {
    "Passer domesticus": {"vikt_g": 28.0,   "_flytt_globalt": "stannfågel",       "kosthallning": "fröätare"},
    "Apus apus":         {"vikt_g": 42.0,   "_flytt_globalt": "flyttfågel",       "kosthallning": "insektsätare"},
    "Turdus merula":     {"vikt_g": 102.0,  "_flytt_globalt": "delvis flyttfågel", "kosthallning": "allätare"},
    "Corvus corax":      {"vikt_g": 1200.0, "_flytt_globalt": "stannfågel",       "kosthallning": "allätare"},
    "Ardea cinerea":     {"vikt_g": 1443.0, "_flytt_globalt": "delvis flyttfågel", "kosthallning": "fiskätare"},
}

ALL_FIVE = day(*[(sci, sci) for sci in FIVE])


class Comparisons(unittest.TestCase):
    def setUp(self):
        clear()
        write_facts(FIVE)

    def test_lightest_and_heaviest(self):
        got = sf.comparisons(ALL_FIVE)
        self.assertEqual(got["minsta_art"], "Passer domesticus")
        self.assertEqual(got["tyngsta_art"], "Ardea cinerea")

    def test_tie_suppresses_the_claim(self):
        # Två arter med samma vikt: "dygnets minsta" vore då ett falskt påstående.
        write_facts({
            "A sp": {"vikt_g": 10.0}, "B sp": {"vikt_g": 10.0}, "C sp": {"vikt_g": 50.0}})
        got = sf.comparisons(day(("A sp", "A"), ("B sp", "B"), ("C sp", "C")))
        self.assertNotIn("minsta_art", got)
        self.assertEqual(got["tyngsta_art"], "C")

    def test_too_few_species_gives_no_extremes(self):
        write_facts({"A sp": {"vikt_g": 10.0}, "B sp": {"vikt_g": 50.0}})
        got = sf.comparisons(day(("A sp", "A"), ("B sp", "B")))
        self.assertNotIn("minsta_art", got)
        self.assertNotIn("tyngsta_art", got)

    def test_migration_never_reaches_the_prompt(self):
        # AVONETs Migration är GLOBAL och fel för svenska förhållanden. Den läses in
        # som _flytt_globalt men får varken bli ett talat fält eller en jämförelse.
        # Regressionsspärr: skulle någon råka lägga tillbaka det ska testet gå sönder.
        got = sf.comparisons(ALL_FIVE)
        self.assertNotIn("flyttfaglar", got)
        spoken = sf.facts_for(ALL_FIVE)
        blob = json.dumps(spoken, ensure_ascii=False)
        for word in ("stannfågel", "flyttfågel", "_flytt_globalt"):
            self.assertNotIn(word, blob, f"{word} läckte till prompten")
        self.assertNotIn("flytt", sf.SPOKEN_FIELDS)

    def test_shares_are_buckets_not_numbers(self):
        write_facts({f"Art{i} sp": {"kosthallning": "insektsätare", "vikt_g": 10.0 + i}
                     for i in range(6)})
        got = sf.comparisons(day(*[(f"Art{i} sp", f"art{i}") for i in range(6)]))
        # Inga råa tal eller procent ut till prompten.
        blob = json.dumps(got, ensure_ascii=False)
        self.assertNotIn("100", blob)
        self.assertNotIn("%", blob)

    def test_dominant_diet_only_when_it_dominates(self):
        got = sf.comparisons(ALL_FIVE)
        # allätare 2/5 = 40 % -> ingen dominans, säg inget.
        self.assertNotIn("vanligaste_kosthallning", got)

        write_facts({f"Art{i} sp": {"kosthallning": "insektsätare", "vikt_g": 10.0 + i}
                     for i in range(6)})
        got = sf.comparisons(day(*[(f"Art{i} sp", f"art{i}") for i in range(6)]))
        self.assertEqual(got["vanligaste_kosthallning"], "insektsätare")

    def test_strongest_beak_needs_a_clear_margin(self):
        # Havstrut 23,5 mot gråtrut 18,5 -> tydlig vinnare, får nämnas.
        write_facts({
            "Larus marinus":    {"_nabbdjup": 23.5, "vikt_g": 1650.0},
            "Larus argentatus": {"_nabbdjup": 18.5, "vikt_g": 1091.0},
            "Larus canus":      {"_nabbdjup": 10.1, "vikt_g": 412.0}})
        got = sf.comparisons(day(("Larus marinus", "havstrut"),
                                 ("Larus argentatus", "gråtrut"),
                                 ("Larus canus", "fiskmås")))
        self.assertEqual(got["kraftigaste_nabben"], "havstrut")

    def test_strongest_beak_suppressed_when_too_close(self):
        # Gråtrut 18,5 mot medelhavstrut 18,2 är i praktiken lika – att utse en
        # vinnare vore skenprecision.
        write_facts({
            "Larus argentatus":  {"_nabbdjup": 18.5, "vikt_g": 1091.0},
            "Larus michahellis": {"_nabbdjup": 18.2, "vikt_g": 1112.0},
            "Larus canus":       {"_nabbdjup": 10.1, "vikt_g": 412.0}})
        got = sf.comparisons(day(("Larus argentatus", "gråtrut"),
                                 ("Larus michahellis", "medelhavstrut"),
                                 ("Larus canus", "fiskmås")))
        self.assertNotIn("kraftigaste_nabben", got)

    def test_beak_millimetres_never_reach_the_prompt(self):
        write_facts({
            "Larus marinus":    {"_nabbdjup": 23.5, "vikt_g": 1650.0},
            "Larus argentatus": {"_nabbdjup": 18.5, "vikt_g": 1091.0},
            "Larus canus":      {"_nabbdjup": 10.1, "vikt_g": 412.0}})
        arter = day(("Larus marinus", "havstrut"), ("Larus argentatus", "gråtrut"),
                    ("Larus canus", "fiskmås"))
        blob = json.dumps([sf.comparisons(arter), sf.facts_for(arter)], ensure_ascii=False)
        for tal in ("23.5", "18.5", "10.1", "_nabbdjup"):
            self.assertNotIn(tal, blob, tal)

    def test_thin_coverage_suppresses_shares(self):
        # Fem arter men bara en har kosthållning -> uttala dig inte om andelar.
        write_facts({
            "A sp": {"kosthallning": "fröätare", "vikt_g": 10.0},
            "B sp": {"vikt_g": 20.0}, "C sp": {"vikt_g": 30.0},
            "D sp": {"vikt_g": 40.0}, "E sp": {"vikt_g": 50.0}})
        got = sf.comparisons(day(*[(f"{c} sp", c) for c in "ABCDE"]))
        self.assertNotIn("vanligaste_kosthallning", got)
        # Storleksjämförelsen bygger på ett annat fält och ska finnas kvar.
        self.assertEqual(got["minsta_art"], "A")

    def test_missing_cache_gives_empty_dict(self):
        clear()
        self.assertEqual(sf.comparisons(ALL_FIVE), {})

    def test_no_matching_species_gives_empty_dict(self):
        self.assertEqual(sf.comparisons(day(("Okand sp", "okänd"))), {})

    def test_share_buckets(self):
        cases = {(10, 10): "så gott som alla", (7, 10): "de flesta",
                 (6, 10): "drygt hälften", (5, 10): "ungefär hälften",
                 (2, 10): "en del", (1, 10): "ett fåtal", (0, 10): None}
        for (part, whole), expected in cases.items():
            self.assertEqual(sf._share_bucket(part, whole), expected, f"{part}/{whole}")


# ---------------------------------------------------------------------------
# build – sammanslagningen av källorna
# ---------------------------------------------------------------------------
class Build(unittest.TestCase):
    def setUp(self):
        clear()
        AVONET.write_text(AVONET_CSV, encoding="utf-8")
        TAXON.write_text(json.dumps(
            {"Sturnus vulgaris": {"id": 103037, "rodlista": "VU"}}), encoding="utf-8")

    def test_merges_avonet_wikidata_and_redlist(self):
        orig = sf.lookup_swedish_labels
        sf.lookup_swedish_labels = lambda latin, verbose=False: {
            "Passeridae": "sparvfinkar", "Passeriformes": "tättingar"}
        try:
            cache = sf.build(["Passer domesticus", "Sturnus vulgaris"], verbose=False)
        finally:
            sf.lookup_swedish_labels = orig
        self.assertEqual(cache["Passer domesticus"]["familj"], "sparvfinkar")
        self.assertEqual(cache["Passer domesticus"]["_flytt_globalt"], "stannfågel")
        self.assertEqual(cache["Sturnus vulgaris"]["rodlista"], "VU")

    def test_family_without_swedish_label_is_omitted_not_latin(self):
        orig = sf.lookup_swedish_labels
        sf.lookup_swedish_labels = lambda latin, verbose=False: {}
        try:
            cache = sf.build(["Ardea cinerea"], verbose=False)
        finally:
            sf.lookup_swedish_labels = orig
        self.assertNotIn("familj", cache["Ardea cinerea"])
        # Latinet ligger kvar internt för ombygge, men aldrig som talat fält.
        self.assertEqual(cache["Ardea cinerea"]["_familj_latin"], "Ardeidae")
        spoken = sf.facts_for([{"scientific": "Ardea cinerea", "display": "gråhäger"}])
        self.assertNotIn("Ardeidae", json.dumps(spoken, ensure_ascii=False))

    def test_meta_records_sources(self):
        sf.build(["Passer domesticus"], verbose=False, skip_wikidata=True)
        meta = json.loads(FACTS.read_text(encoding="utf-8"))["_meta"]
        self.assertIn("avonet", meta["kallor"])
        self.assertIn("CC BY 4.0", meta["kallor"]["avonet"])

    def test_wikidata_failure_does_not_fell_the_build(self):
        import requests
        orig = sf.lookup_swedish_labels

        def boom(latin, verbose=False):
            raise requests.ConnectionError("nätet nere")

        sf.lookup_swedish_labels = boom
        try:
            cache = sf.build(["Passer domesticus"], verbose=False)
        finally:
            sf.lookup_swedish_labels = orig
        # AVONET-fälten ska finnas kvar även när taxonomin uteblev.
        self.assertEqual(cache["Passer domesticus"]["_flytt_globalt"], "stannfågel")
        self.assertNotIn("familj", cache["Passer domesticus"])

    def test_wikidata_fields_survive_a_rebuild(self):
        write_facts({"Passer domesticus": {"familj": "sparvfinkar"}})
        cache = sf.build(["Passer domesticus"], verbose=False, skip_wikidata=True)
        self.assertEqual(cache["Passer domesticus"]["familj"], "sparvfinkar")
        self.assertEqual(cache["Passer domesticus"]["_flytt_globalt"], "stannfågel")

    def test_stale_avonet_fields_are_cleared_on_rebuild(self):
        # Ändras en tröskel ska ett fält som inte längre kvalificerar FÖRSVINNA.
        # Ärvda värden här vore samma fälla som den förgiftade GBIF-cachen.
        write_facts({"Passer domesticus": {
            "familj": "sparvfinkar",            # Wikidata -> ska bevaras
            "vingform": "långa, spetsiga vingar",  # AVONET -> ska rensas
            "kosthallning": "fiskätare"}})         # AVONET -> ska skrivas om
        cache = sf.build(["Passer domesticus"], verbose=False, skip_wikidata=True)
        rec = cache["Passer domesticus"]
        self.assertEqual(rec["familj"], "sparvfinkar")
        self.assertNotIn("vingform", rec)          # HWI 27,3 kvalificerar inte
        self.assertEqual(rec["kosthallning"], "fröätare")

    def test_species_outside_this_run_are_kept(self):
        write_facts({"Gammal art": {"familj": "gamlingar"}})
        cache = sf.build(["Passer domesticus"], verbose=False, skip_wikidata=True)
        self.assertIn("Gammal art", cache)

    def test_redlist_removed_when_no_longer_noteworthy(self):
        write_facts({"Passer domesticus": {"familj": "sparvfinkar", "rodlista": "VU"}})
        cache = sf.build(["Passer domesticus"], verbose=False, skip_wikidata=True)
        self.assertNotIn("rodlista", cache["Passer domesticus"])


# ---------------------------------------------------------------------------
# Wikidata-parsning (svaret mockat)
# ---------------------------------------------------------------------------
class WikidataParsing(unittest.TestCase):
    def _fake(self, bindings):
        return {"results": {"bindings": bindings}}

    def test_translates_latin_taxon_names_to_swedish(self):
        orig = sf._sparql
        sf._sparql = lambda q, verbose=False: self._fake([
            {"lat": {"value": "Ardeidae"}, "label": {"value": "hägrar"}},
            {"lat": {"value": "Pelecaniformes"}, "label": {"value": "pelikanfåglar"}}])
        try:
            got = sf.lookup_swedish_labels(["Ardeidae", "Pelecaniformes"])
        finally:
            sf._sparql = orig
        self.assertEqual(got, {"Ardeidae": "hägrar",
                               "Pelecaniformes": "pelikanfåglar"})

    def test_missing_swedish_label_is_simply_absent(self):
        # Frågan filtrerar på lang="sv", så ett taxon utan svensk etikett ger
        # ingen rad alls – och latinet får aldrig smyga med som reserv.
        orig = sf._sparql
        sf._sparql = lambda q, verbose=False: self._fake([
            {"lat": {"value": "Ardeidae"}, "label": {"value": "hägrar"}}])
        try:
            got = sf.lookup_swedish_labels(["Ardeidae", "Obscuridae"])
        finally:
            sf._sparql = orig
        self.assertNotIn("Obscuridae", got)

    def test_qid_labels_are_discarded(self):
        orig = sf._sparql
        sf._sparql = lambda q, verbose=False: self._fake([
            {"lat": {"value": "Obscuridae"}, "label": {"value": "Q123456"}}])
        try:
            got = sf.lookup_swedish_labels(["Obscuridae"])
        finally:
            sf._sparql = orig
        self.assertEqual(got, {})

    def test_labels_are_lowercased(self):
        # Wikidata versaliserar många taxonetiketter; svenska gruppnamn är gemena.
        orig = sf._sparql
        sf._sparql = lambda q, verbose=False: self._fake([
            {"lat": {"value": "Laridae"}, "label": {"value": "Måsfåglar"}}])
        try:
            got = sf.lookup_swedish_labels(["Laridae"])
        finally:
            sf._sparql = orig
        self.assertEqual(got["Laridae"], "måsfåglar")

    def test_order_is_never_spoken(self):
        # Ordningsnivån är vilseledande i tal (tornseglare -> "skärrfåglar").
        self.assertNotIn("ordning", sf.SPOKEN_FIELDS)
        write_facts({"Ardea cinerea": {"familj": "hägrar", "ordning": "pelikanfåglar"}})
        got = sf.facts_for(day(("Ardea cinerea", "gråhäger")))
        self.assertEqual(got, [{"art": "gråhäger", "familj": "hägrar"}])

    def test_looks_like_qid(self):
        self.assertTrue(sf._looks_like_qid("Q42"))
        self.assertFalse(sf._looks_like_qid("hägrar"))
        self.assertFalse(sf._looks_like_qid("Quercus"))

    def test_batching_covers_all_names(self):
        orig_batch = sf.WIKIDATA_BATCH
        orig = sf._sparql
        seen = []

        def fake(q, verbose=False):
            seen.append(q)
            return self._fake([])

        sf.WIKIDATA_BATCH = 2
        sf._sparql = fake
        try:
            sf.lookup_swedish_labels([f"Familj{i}dae" for i in range(5)])
        finally:
            sf.WIKIDATA_BATCH = orig_batch
            sf._sparql = orig
        self.assertEqual(len(seen), 3)   # 2 + 2 + 1


# ---------------------------------------------------------------------------
# Integrationen i generate_report.derive_signals
# ---------------------------------------------------------------------------
class DeriveSignalsIntegration(unittest.TestCase):
    def setUp(self):
        clear()
        write_facts(FIVE)

    def _today(self):
        return {
            "date": "2026-08-02",
            "species_count": len(FIVE),
            "top_species": [
                {"name": sci, "scientific": sci, "display": sci, "activity": "enstaka"}
                for sci in FIVE
            ],
        }

    def test_artfakta_and_comparisons_land_in_signals(self):
        import generate_report as gr
        sig = gr.derive_signals(self._today(), {"species_ever": {}, "recent_days": []})
        self.assertIn("artfakta", sig)
        self.assertTrue(sig["artfakta"], "artfakta ska vara ifylld")
        self.assertEqual(sig["artfakta_jamforelser"]["tyngsta_art"], "Ardea cinerea")

    def test_missing_module_degrades_silently(self):
        import generate_report as gr
        orig = gr.species_facts
        gr.species_facts = None
        try:
            sig = gr.derive_signals(self._today(), {"species_ever": {}, "recent_days": []})
            self.assertEqual(sig["artfakta"], [])
            self.assertEqual(sig["artfakta_jamforelser"], {})
        finally:
            gr.species_facts = orig

    def test_exception_in_module_degrades_silently(self):
        import generate_report as gr

        class Boom:
            def facts_for(self, _):
                raise RuntimeError("trasigt")

            def comparisons(self, _):
                raise RuntimeError("trasigt")

        orig = gr.species_facts
        gr.species_facts = Boom()
        try:
            sig = gr.derive_signals(self._today(), {"species_ever": {}, "recent_days": []})
            self.assertEqual(sig["artfakta"], [])
            self.assertEqual(sig["artfakta_jamforelser"], {})
        finally:
            gr.species_facts = orig

    def test_artfakta_nar_prompten_via_frasverket(self):
        """OMSKRIVET 2026-07-31 (tvåstegsupplägget). Artfakta injiceras inte
        längre som JSON via {{ARTFAKTA_JSON}} – de blir en färdig punkt i
        frasverket, och prompten ser aldrig ett fältnamn eller ett latinskt namn."""
        import generate_report as gr
        today = self._today()
        sig = gr.derive_signals(today, {"species_ever": {}, "recent_days": []})
        punkter = gr.build_facts(today, sig, {})
        fakta = [p for p in punkter if p["kategori"] == "artfaktum"]
        self.assertTrue(fakta, "artfaktan nådde inte fram till punktlistan")
        # Kandidaterna är fler än budgeten med flit; taket sätts vid URVALET.
        self.assertLessEqual(len(fakta), gr.ARTFAKTA_KANDIDATER)
        alla = list(range(1, len(gr._valbara(punkter)) + 1))
        valda, _ = gr.validate_selection(punkter, alla, None)
        antal = len([p for p in valda if p.get("fakta_id")])
        self.assertLessEqual(antal, gr.MAX_ARTFAKTA)      # taket
        self.assertGreaterEqual(antal, gr.MIN_ARTFAKTA)   # botten
        # OBS: fixturen saknar svenska namn, så visningsnamnet ÄR det latinska –
        # samma fallback som i drift när GBIF inte har ett svenskt namn. Det som
        # ska granskas är att inga FÄLTNAMN eller SIFFROR följer med.
        for p in punkter:
            self.assertFalse(any(c.isdigit() for c in p["text"]), p["text"])
            for falt in gr.FALTNAMN:
                self.assertNotIn(falt, p["text"].lower())

    def test_prompterna_har_sina_platshallare(self):
        import generate_report as gr
        krav = {gr.PROMPT_FAKTA_PATH: ("{{PUNKTER}}", "{{TIDIGARE_FAKTA}}"),
                gr.PROMPT_TON_PATH:   ("{{PUNKTLISTA}}", "{{RECENT_SCRIPTS}}",
                                       "{{DAGENS_NOTIS}}", "{{HOST_A}}", "{{HOST_B}}")}
        for path, markorer in krav.items():
            if not path.exists():
                continue
            raw = path.read_text(encoding="utf-8")
            for m in markorer:
                self.assertIn(m, raw, f"{path} saknar {m}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
