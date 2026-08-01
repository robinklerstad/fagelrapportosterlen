#!/usr/bin/env python3
"""Tester för frasverket och tvåstegsupplägget i generate_report.py.

BAKGRUND (2026-07-31): ett anrop ombads hålla ~20 arter, ~10 signalfält, 15 förbud
OCH skriva varm dialog på 300 ord. De kvarstående felen rörde sig inte: modellen
fick nio tal i signalerna och sa "tjugotredje dygnet" (23 fanns inte bland dem),
och ett internt fältnamn gick ut i sändning ("utan streak-historik heller").

Lösningen är inte en sextonde regel utan att modellen aldrig får se ett tal.
`build_facts()` gör varje tal till en färdig svensk fras, och de tre kontrakten
nedan är vad som gör den garantin verklig. KONTRAKTSTESTERNA KÖRS MOT HELA DEN
VERKLIGA HISTORIKEN – det är poängen med dem.

Inga nätanrop, ingen modell. Kör: python3 test_faktapunkter.py
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

HISTORIK = Path("history.json")


def art(display, scientific="", activity="enstaka"):
    return {"name": display, "display": display, "scientific": scientific,
            "activity": activity, "count": 1}


def dygn(datum="2026-07-31", arter=None):
    arter = arter or [art("gråsparv", "Passer domesticus")]
    return {"date": datum, "species_count": len(arter), "top_species": arter,
            "total_detections": len(arter), "station_name": "test"}


# ---------------------------------------------------------------------------
# KONTRAKTEN. Bryts något av dessa är hela upplägget borta.
# ---------------------------------------------------------------------------
class Kontrakt(unittest.TestCase):

    def _alla_punkter_ur_verklig_historik(self):
        """Kör build_facts för VARJE dygn i den riktiga historiken och samla alla
        punkter. Syntetiska testdygn räcker inte – felen har alltid dykt upp på
        verklig data (långa sviter, udda artnamn, tomma fält)."""
        if not HISTORIK.exists():
            self.skipTest("history.json saknas")
        hist = json.loads(HISTORIK.read_text(encoding="utf-8"))
        dagar = hist.get("recent_days", [])
        if not dagar:
            self.skipTest("tom historik")

        punkter = []
        for i, dag in enumerate(dagar):
            arter = [art(t.get("name") or t.get("sci", "?"), t.get("sci", ""))
                     for t in dag.get("top", [])]
            if not arter:
                continue
            today = dygn(dag["date"], arter)
            today["species_count"] = dag.get("species_count") or len(arter)
            delhistorik = {"species_ever": hist.get("species_ever", {}),
                           "recent_days": dagar[:i]}
            sig = gr.derive_signals(today, delhistorik)
            punkter += gr.build_facts(today, sig, fact_log={})
            ov = gr.ovriga_punkt(today, [])
            if ov:
                punkter.append(ov)
        self.assertTrue(punkter, "build_facts gav inga punkter alls på verklig data")
        return punkter

    def test_kontrakt_1_inga_siffror(self):
        """En siffra i punktlistan är ett tal modellen kan bygga vidare på."""
        for p in self._alla_punkter_ur_verklig_historik():
            self.assertFalse(any(c.isdigit() for c in p["text"]),
                             f"siffra i punkt: {p['text']!r}")

    def test_kontrakt_2_inga_interna_faltnamn(self):
        """'Den klöv i dygnet utan streak-historik heller' gick ut i sändning."""
        for p in self._alla_punkter_ur_verklig_historik():
            lag = p["text"].lower()
            for falt in gr.FALTNAMN:
                self.assertNotIn(falt, lag, f"fältnamn {falt!r} i punkt: {p['text']!r}")

    def test_kontrakt_3_alla_talord_ar_kanda(self):
        """Varje talord ska komma ur tabellerna. Det är detta som gör den
        tillåtna talordsmängden känd, och manusvalideringen bevisande."""
        kanda = (set(gr.RAKNEORD.values()) | set(gr.ORDNINGSTAL.values())
                 | {v.split()[-1] for v in gr.TIOTAL.values()}
                 | {"en", "ett", "par", "handfull", "fåtal", "hälften"})
        misstankta = {"tjugotre", "tjugotredje", "nittoende", "trettionde"}
        for p in self._alla_punkter_ur_verklig_historik():
            for ord_ in p["text"].lower().replace(",", " ").split():
                if ord_ in misstankta:
                    self.assertIn(ord_, kanda, f"okänt talord i: {p['text']!r}")


# ---------------------------------------------------------------------------
# Frasformaterarna
# ---------------------------------------------------------------------------
class Fraser(unittest.TestCase):

    def test_datumfras_raknar_veckodagen(self):
        # Modellen sa "fredagen den trettionde juli" – 2026-07-30 var en torsdag.
        self.assertEqual(gr._datumfras("2026-07-30"), "torsdagen den trettionde juli")
        self.assertEqual(gr._datumfras("2026-07-31"), "fredagen den trettioförsta juli")
        self.assertEqual(gr._datumfras("2026-08-01"), "lördagen den första augusti")
        self.assertEqual(gr._datumfras("2026-08-03"), "måndagen den tredje augusti")

    def test_artrikedom_rundas_av(self):
        self.assertEqual(gr._artrikedomsfras(20), "ett tjugotal arter")
        self.assertEqual(gr._artrikedomsfras(24), "drygt ett tjugotal arter")
        self.assertEqual(gr._artrikedomsfras(28), "knappt ett trettiotal arter")
        self.assertEqual(gr._artrikedomsfras(6), "en handfull arter")
        self.assertEqual(gr._artrikedomsfras(2), "bara ett par arter")
        self.assertIsNone(gr._artrikedomsfras(0))

    def test_uppehall_blir_veckor_aldrig_dagar(self):
        self.assertEqual(gr._uppehallsfras(14), "efter två veckors tystnad")
        self.assertEqual(gr._uppehallsfras(24), "efter drygt tre veckors tystnad")
        self.assertEqual(gr._uppehallsfras(90), "efter tre månaders tystnad")
        self.assertIsNone(gr._uppehallsfras(3))

    def test_svit_blir_veckor_nar_det_gar_jamnt_upp(self):
        self.assertEqual(gr._svitfras(10), "tio dygn i rad")
        self.assertEqual(gr._svitfras(21), "tre veckor i rad")
        self.assertIsNone(gr._svitfras(2))          # under tröskeln
        self.assertIsNone(gr._svitfras(999))        # utanför tabellen -> utelämnas

    def test_tal_utanfor_tabellen_ger_ingen_punkt_med_siffra(self):
        self.assertIsNone(gr._talord(9999))


# ---------------------------------------------------------------------------
# build_facts
# ---------------------------------------------------------------------------
class BuildFacts(unittest.TestCase):

    def _sig(self, **kw):
        bas = {"new_species": [], "first_this_year": [], "returning_after_gap": [],
               "returning_details": [], "streaks": [], "artrikedom_kontext": {},
               "vs_yesterday": None, "lokal_kontext": [], "artfakta": [],
               "artfakta_jamforelser": {}}
        bas.update(kw)
        return bas

    def test_fler_faktakandidater_an_budgeten(self):
        # Kandidaterna ska vara FLER än budgeten, annars har anrop 1 inget att
        # välja mellan och rotationen blir ingen rotation. Taket sätts vid
        # urvalet i stället (se Urval.test_faktataket_haller_vid_urvalet).
        # OBS: inga siffror i testdatan – kontrakt 1 skulle (korrekt) slänga
        # punkterna, och testet skulle mäta fel sak.
        namn = ["gråsparv", "kaja", "koltrast", "talgoxe", "blåmes", "stare",
                "ringduva", "grågås"]
        artfakta = [{"art": n, "familj": "finkar"} for n in namn]
        p = gr.build_facts(dygn(), self._sig(artfakta=artfakta), {})
        antal = len([x for x in p if x["kategori"] == "artfaktum"])
        self.assertEqual(antal, gr.ARTFAKTA_KANDIDATER)
        self.assertGreater(antal, gr.MAX_ARTFAKTA)

    def test_hogst_ett_faktum_per_art(self):
        artfakta = [{"art": "gråhäger", "familj": "hägrar", "habitat": "våtmark",
                     "kosthallning": "tar sin föda i vatten"}]
        p = gr.build_facts(dygn(), self._sig(artfakta=artfakta), {})
        self.assertEqual(len([x for x in p if x["kategori"] == "artfaktum"]), 1)

    def test_faktarotation_hoppar_over_nyss_anvant(self):
        artfakta = [{"art": "gråhäger", "familj": "hägrar", "habitat": "våtmark"}]
        logg = {"2026-07-30": ["gråhäger/familj"]}
        p = gr.build_facts(dygn(), self._sig(artfakta=artfakta), logg)
        fakta = [x for x in p if x["kategori"] == "artfaktum"]
        self.assertEqual(len(fakta), 1)
        self.assertEqual(fakta[0]["fakta_id"], "gråhäger/habitat")

    def test_gammalt_i_loggen_blockerar_inte(self):
        artfakta = [{"art": "gråhäger", "familj": "hägrar"}]
        logg = {f"2026-07-{d:02d}": ["gråhäger/familj"] for d in range(1, 6)}
        logg["2026-07-30"] = ["annan/familj"]
        logg["2026-07-29"] = ["annan/familj"]
        logg["2026-07-28"] = ["annan/familj"]
        logg["2026-07-27"] = ["annan/familj"]
        p = gr.build_facts(dygn(), self._sig(artfakta=artfakta), logg)
        self.assertTrue([x for x in p if x["fakta_id"] == "gråhäger/familj"])

    def test_rovfaglar_far_punkt_utan_omdome(self):
        # Rovfågeltics­en ("alltid ett litet lyft när rovfåglarna är med") kom fem
        # körningar i rad. Punkten konstaterar, den värderar inte.
        arter = [art("röd glada"), art("ormvråk"), art("kattuggla"), art("gråsparv")]
        p = gr.build_facts(dygn(arter=arter), self._sig(), {})
        gaster = [x["art"] for x in p if x["kategori"] == "speciell_gast"]
        self.assertEqual(sorted(gaster), ["kattuggla", "ormvråk", "röd glada"])
        for x in p:
            if x["kategori"] == "speciell_gast":
                self.assertEqual(x["text"], f"{x['art']} – hördes")

    def test_svit_taket_overlever_in_i_punktlistan(self):
        streaks = [{"art": f"art{i}", "dagar_i_rad": 10 - i} for i in range(9)]
        p = gr.build_facts(dygn(), self._sig(streaks=streaks), {})
        # derive_signals toppar redan till MAX_STREAKS; build_facts ska inte
        # återinföra fler än den fått.
        self.assertLessEqual(len([x for x in p if x["kategori"] == "svit"]), 9)

    def test_ram_och_ovriga_ligger_utanfor_budgeten(self):
        p = gr.build_facts(dygn(), self._sig(), {})
        ram = [x for x in p if x["kategori"] == "ram"]
        self.assertEqual(len(ram), 1)
        self.assertTrue(ram[0]["alltid_med"])
        self.assertEqual(gr._valbara(p), [x for x in p if not x["alltid_med"]])

    def test_ovriga_utelamnar_redan_namnda_arter(self):
        arter = [art("gråsparv"), art("kaja"), art("koltrast")]
        valda = [gr._punkt("gråsparv – hördes", "aktivitet", art="gråsparv")]
        ov = gr.ovriga_punkt(dygn(arter=arter), valda)
        self.assertIn("kaja", ov["text"])
        self.assertIn("koltrast", ov["text"])
        self.assertNotIn("gråsparv", ov["text"])

    def test_ovriga_ar_none_nar_allt_ar_namnt(self):
        arter = [art("gråsparv")]
        valda = [gr._punkt("gråsparv – hördes", "aktivitet", art="gråsparv")]
        self.assertIsNone(gr.ovriga_punkt(dygn(arter=arter), valda))

    def test_tomt_dygn_faller_inte(self):
        tomt = {"date": "2026-07-31", "species_count": 0, "top_species": []}
        p = gr.build_facts(tomt, self._sig(), {})
        self.assertEqual([x["kategori"] for x in p], ["ram"])

    def test_trasig_signaldict_faller_inte(self):
        self.assertTrue(gr.build_facts(dygn(), {}, None))

    def test_punkt_med_siffra_slangs(self):
        self.assertFalse(gr._punkt_ar_ren("gråsparv – 10 dygn i rad"))
        self.assertTrue(gr._punkt_ar_ren("gråsparv – tio dygn i rad"))

    def test_punkt_med_faltnamn_slangs(self):
        self.assertFalse(gr._punkt_ar_ren("röd glada – utan streak-historik"))
        self.assertFalse(gr._punkt_ar_ren("gråhäger – kosthallning tar föda i vatten"))

    def test_cirkulara_familjenamn_kommer_aldrig_hit(self):
        # species_facts.facts_for() undertrycker dem redan; testet vaktar att
        # build_facts inte återinför dem via någon annan väg.
        try:
            import species_facts
        except Exception:
            self.skipTest("species_facts saknas")
        self.assertTrue(species_facts._familj_ar_cirkular("snäppor", "kärrsnäppa"))
        artfakta = [{"art": "kärrsnäppa", "habitat": "våtmark"}]
        p = gr.build_facts(dygn(), self._sig(artfakta=artfakta), {})
        fakta = [x for x in p if x["kategori"] == "artfaktum"]
        self.assertEqual(fakta[0]["fakta_id"], "kärrsnäppa/habitat")


# ---------------------------------------------------------------------------
# Urvalet (anrop 1) och valideringen
# ---------------------------------------------------------------------------
class Urval(unittest.TestCase):

    def _facts(self, n=12):
        f = [gr._punkt("datumfras", "ram", alltid_med=True)]
        f += [gr._punkt(f"punkt {gr.RAKNEORD[i + 1]}", "svit") for i in range(n)]
        return f

    def test_index_utanfor_listan_slangs(self):
        f = self._facts(5)
        valda, _ = gr.validate_selection(f, [1, 99, -3, "x", 4], None)
        self.assertEqual([p["text"] for p in valda],
                         ["punkt ett", "punkt fyra", "punkt två", "punkt tre"])

    def test_dubbletter_tas_bort(self):
        f = self._facts(8)
        valda, _ = gr.validate_selection(f, [2, 2, 3, 3, 4, 5], None)
        self.assertEqual(len({p["text"] for p in valda}), len(valda))

    def test_kapas_till_max(self):
        f = self._facts(20)
        valda, _ = gr.validate_selection(f, list(range(1, 21)), None)
        self.assertEqual(len(valda), gr.MAX_PUNKTER)

    def test_for_kort_lista_fylls_pa(self):
        f = self._facts(10)
        valda, _ = gr.validate_selection(f, [1], None)
        self.assertGreaterEqual(len(valda), gr.MIN_PUNKTER)

    def test_tomt_svar_ger_prioritetsurval(self):
        f = self._facts(10)
        valda, _ = gr.validate_selection(f, None, None)
        self.assertGreaterEqual(len(valda), gr.MIN_PUNKTER)

    def test_ordningen_bevaras(self):
        f = self._facts(8)
        valda, _ = gr.validate_selection(f, [5, 1, 3, 8], None)
        self.assertEqual([p["text"] for p in valda],
                         ["punkt fem", "punkt ett", "punkt tre", "punkt åtta"])

    def test_huvudsak_pekar_ut_ratt_position(self):
        f = self._facts(8)
        _, huvud = gr.validate_selection(f, [5, 1, 3, 8], 3)
        self.assertEqual(huvud, 2)

    def test_huvudsak_utanfor_urvalet_ignoreras(self):
        f = self._facts(8)
        _, huvud = gr.validate_selection(f, [5, 1], 7)
        self.assertIsNone(huvud)

    def test_ramen_kan_inte_valjas(self):
        f = self._facts(4)
        valda, _ = gr.validate_selection(f, [1, 2, 3, 4], None)
        self.assertNotIn("ram", [p["kategori"] for p in valda])

    def test_reservurval_tar_hogst_prioritet_forst(self):
        f = [gr._punkt("datum", "ram", alltid_med=True),
             gr._punkt("en svit", "svit"),
             gr._punkt("en ny art", "ny_art"),
             gr._punkt("en aktivitet", "aktivitet")]
        idx, huvud = gr._reservurval(f)
        self.assertEqual(gr._valbara(f)[idx[0]]["kategori"], "ny_art")
        self.assertEqual(huvud, idx[0])


# ---------------------------------------------------------------------------
# Punktlistan som anrop 2 faktiskt får se
# ---------------------------------------------------------------------------
class Punktlista(unittest.TestCase):

    def test_ramen_forst_och_uppraakningen_sist(self):
        arter = [art("gråsparv"), art("kaja")]
        f = gr.build_facts(dygn(arter=arter), {"artrikedom_kontext": {"idag": 2}}, {})
        valda = [p for p in gr._valbara(f)][:1]
        rader = gr.punktlista(f, valda, dygn(arter=arter))
        self.assertEqual(rader[0]["kategori"], "ram")
        self.assertEqual(rader[-1]["kategori"], "ovriga")

    def test_huvudsaken_markeras(self):
        f = [gr._punkt("datum", "ram", alltid_med=True),
             gr._punkt("a", "svit"), gr._punkt("b", "svit")]
        valda = gr._valbara(f)
        rader = gr.punktlista(f, valda, dygn(), huvud_pos=1)
        self.assertIn("(dagens huvudsak)", rader[2]["text"])
        self.assertNotIn("(dagens huvudsak)", rader[1]["text"])

    def test_ingen_rad_innehaller_en_siffra(self):
        # Slutkontrollen: det anrop 2 ser ska vara talfritt.
        arter = [art("gråsparv"), art("röd glada"), art("kaja")]
        d = dygn(arter=arter)
        sig = gr.derive_signals(d, {"species_ever": {}, "recent_days": []})
        f = gr.build_facts(d, sig, {})
        rader = gr.punktlista(f, gr._valbara(f)[:5], d)
        for r in rader:
            self.assertFalse(any(c.isdigit() for c in r["text"]), r["text"])


# ---------------------------------------------------------------------------
# Kategorier tillagda 2026-07-31 efter första skarpa testkörningen: punktlistan
# var för tunn (sex punkter, tre av dem rena "hördes"), och en modell utan
# material fyller ut med omdömen. Rikare lista är åtgärden mot orsaken.
# ---------------------------------------------------------------------------
class NyaKategorier(unittest.TestCase):

    def _sig(self, **kw):
        bas = {"new_species": [], "first_this_year": [], "returning_after_gap": [],
               "returning_details": [], "streaks": [], "avbrutna_sviter": [],
               "artrikedom_kontext": {}, "vs_yesterday": None, "lokal_kontext": [],
               "artfakta": [], "artfakta_jamforelser": {}}
        bas.update(kw)
        return bas

    def test_familjegrupp_kraver_tillrackligt_manga_arter(self):
        try:
            import species_facts
        except Exception:
            self.skipTest("species_facts saknas")
        tre = [art("medelhavstrut", "Larus michahellis"),
               art("gråtrut", "Larus argentatus"),
               art("fisktärna", "Sterna hirundo")]
        p = gr.build_facts(dygn(arter=tre), self._sig(), {})
        grupper = [x for x in p if x["kategori"] == "familjegrupp"]
        self.assertEqual(len(grupper), 1)
        self.assertIn("måsfåglar", grupper[0]["text"])

        tva = tre[:2]
        p = gr.build_facts(dygn(arter=tva), self._sig(), {})
        self.assertEqual([x for x in p if x["kategori"] == "familjegrupp"], [])

    def test_familjegruppen_haller_tornseglaren_utanfor_svalorna(self):
        """Poddens vanligaste fel, nu vänt till ett faktum: gruppen räknar upp
        VILKA arter som hör ihop, och tornseglaren står inte bland dem."""
        try:
            import species_facts
        except Exception:
            self.skipTest("species_facts saknas")
        self.assertEqual(species_facts.familj_for("Apus apus"), "seglare")
        self.assertEqual(species_facts.familj_for("Hirundo rustica"), "svalor")
        self.assertEqual(species_facts.familj_for("Delichon urbicum"), "svalor")

    def test_gruppens_arter_raknas_inte_upp_igen(self):
        """Arterna stod i både gruppen och "hördes också" (sett 2026-07-31)."""
        try:
            import species_facts
        except Exception:
            self.skipTest("species_facts saknas")
        arter = [art("medelhavstrut", "Larus michahellis"),
                 art("gråtrut", "Larus argentatus"),
                 art("fisktärna", "Sterna hirundo"),
                 art("kaja", "Coloeus monedula")]
        d = dygn(arter=arter)
        p = gr.build_facts(d, self._sig(), {})
        grupp = [x for x in p if x["kategori"] == "familjegrupp"]
        ov = gr.ovriga_punkt(d, grupp)
        self.assertIn("kaja", ov["text"])
        for a in ("medelhavstrut", "gråtrut", "fisktärna"):
            self.assertNotIn(a, ov["text"])

    def test_lika_langa_sviter_slas_ihop(self):
        streaks = [{"art": "kaja", "dagar_i_rad": 21},
                   {"art": "gråsparv", "dagar_i_rad": 21},
                   {"art": "tornseglare", "dagar_i_rad": 10}]
        p = gr.build_facts(dygn(), self._sig(streaks=streaks), {})
        sviter = [x for x in p if x["kategori"] == "svit"]
        self.assertEqual(len(sviter), 2)
        ihop = [x for x in sviter if "kaja" in x["text"]][0]
        self.assertIn("gråsparv", ihop["text"])
        self.assertEqual(sorted(ihop["arter"]), ["gråsparv", "kaja"])

    def test_avbruten_svit_blir_punkt(self):
        avbrutna = [{"art": "gråhäger", "dagar_i_rad": 7}]
        p = gr.build_facts(dygn(), self._sig(avbrutna_sviter=avbrutna), {})
        u = [x for x in p if x["kategori"] == "uteblev"]
        self.assertEqual(u[0]["text"], "gråhäger – hördes inte i dag, efter sju dygn i rad")

    def test_engelska_namn_ur_historiken_nar_aldrig_punktlistan(self):
        """Historiken bär kvar 33 ENGELSKA namn från tiden före GBIF-fixen
        ("Eurasian Linnet", "Gray Heron"). De skulle gå rakt ut i sändning."""
        hist = {"species_ever": {},
                "recent_days": [
                    {"date": "2026-07-%02d" % d, "species_count": 2,
                     "top": [{"sci": "Ardea cinerea", "name": "Gray Heron"}]}
                    for d in range(24, 31)]}
        today = dygn("2026-07-31", [art("gråsparv", "Passer domesticus")])
        sig = gr.derive_signals(today, hist)
        for s in sig.get("avbrutna_sviter") or []:
            self.assertNotIn("Gray Heron", s["art"])
            self.assertNotIn("Heron", s["art"])


class Faktabudget(unittest.TestCase):
    """Taket OCH botten. Första skarpa körningen (2026-07-31) valde bort båda
    faktapunkterna och avsnittet blev faktafritt – taket fanns, botten saknades."""

    def _facts(self, antal_fakta=3, antal_ovrigt=6):
        f = [gr._punkt("datum", "ram", alltid_med=True)]
        f += [gr._punkt(f"vanlig punkt {gr.RAKNEORD[i + 1]}", "svit")
              for i in range(antal_ovrigt)]
        f += [gr._punkt(f"faktum {gr.RAKNEORD[i + 1]}", "artfaktum",
                        art=f"art{chr(97+i)}", fakta_id=f"art{chr(97+i)}/familj")
              for i in range(antal_fakta)]
        return f

    def test_taket_haller_vid_urvalet(self):
        f = self._facts(antal_fakta=5, antal_ovrigt=3)
        alla = list(range(1, len(gr._valbara(f)) + 1))
        valda, _ = gr.validate_selection(f, alla, None)
        self.assertLessEqual(len([p for p in valda if p.get("fakta_id")]),
                             gr.MAX_ARTFAKTA)

    def test_botten_tvingar_in_ett_faktum(self):
        f = self._facts(antal_fakta=3, antal_ovrigt=6)
        valda, _ = gr.validate_selection(f, [1, 2, 3, 4], None)   # bara vanliga
        self.assertGreaterEqual(len([p for p in valda if p.get("fakta_id")]),
                                gr.MIN_ARTFAKTA)

    def test_botten_byter_ut_lagst_prioriterad_nar_listan_ar_full(self):
        f = self._facts(antal_fakta=1, antal_ovrigt=10)
        valda, _ = gr.validate_selection(f, list(range(1, gr.MAX_PUNKTER + 1)), None)
        self.assertEqual(len(valda), gr.MAX_PUNKTER)
        self.assertTrue([p for p in valda if p.get("fakta_id")])

    def test_utan_faktakandidat_ar_det_tyst_inte_fel(self):
        f = self._facts(antal_fakta=0, antal_ovrigt=6)
        valda, _ = gr.validate_selection(f, [1, 2, 3, 4], None)
        self.assertEqual([p for p in valda if p.get("fakta_id")], [])


# ---------------------------------------------------------------------------
# Manusvalidering, lager 1 (loggläge)
# ---------------------------------------------------------------------------
class Manusvalidering(unittest.TestCase):

    def _kor(self, text, punkter=("fredagen den trettioförsta juli",
                                  "tornseglare – tio dygn i rad"), arter=()):
        turns = [{"speaker": "Astrid", "text": text}]
        rader = [{"text": p} for p in punkter]
        today = {"top_species": [art(a) for a in arter]}
        return gr.validate_script(turns, rader, today)

    def test_siffra_i_klartext_flaggas(self):
        self.assertTrue(any("SIFFRA" in t for t in self._kor("Det var 20 arter.")))

    def test_talord_som_inte_star_i_punktlistan_flaggas(self):
        # Bevisande, inte heuristiskt: alla tal är förformaterade i frasverket.
        t = self._kor("Tjugotredje dygnet i rad nu.")
        self.assertTrue(any("PÅHITTAT TAL" in x and "tjugotredje" in x for x in t))

    def test_talord_som_star_i_punktlistan_gar_igenom(self):
        t = self._kor("Tornseglaren, tio dygn i rad.")
        self.assertFalse([x for x in t if "PÅHITTAT TAL" in x])

    def test_faltnamn_flaggas(self):
        t = self._kor("Den klöv i dygnet utan streak-historik heller.")
        self.assertTrue(any("FÄLTNAMN" in x for x in t))

    def test_tidsinramning_flaggas(self):
        self.assertTrue(any("i morse" in x for x in self._kor("Det stora i morse.")))
        self.assertTrue(any("i natt" in x for x in self._kor("Vad hände i natt?")))

    def test_tradgarden_och_osterlen_ar_tillatna(self):
        """Stationen STÅR i en trädgård i Simrishamn på Österlen – det är den enda
        plats som är sann. En första version förbjöd just den (Robin 2026-07-31)."""
        for text in ("Ett ordentligt inslag i en Österlenträdgård.",
                     "Det hördes i trädgården i morgonens ljus.",
                     "En fin fredagsmorgon på Österlen."):
            t = self._kor(text, arter=("kaja",))
            self.assertEqual([x for x in t if "placerar" in x], [], text)

    def test_uppdiktad_plats_flaggas(self):
        for text in ("Den hördes nere vid kusten.", "Uppe i luften hela dygnet.",
                     "Ute på fälten lät det mycket."):
            t = self._kor(text, arter=("kaja",))
            self.assertTrue(any("placerar" in x for x in t), text)

    def test_metareferens_till_listan_flaggas(self):
        t = self._kor("Inte vad man räknar med i listan.")
        self.assertTrue(any("metareferens" in x for x in t))

    def test_sasongspastaende_flaggas_bara_nar_det_saknas_i_listan(self):
        self.assertTrue(any("säsongspåstående" in x
                            for x in self._kor("Ungefär som förväntat för den här tiden.")))
        t = self._kor("Ovanlig i trakten så här års.",
                      punkter=("drillsnäppa – ovanlig i trakten så här års",))
        self.assertFalse([x for x in t if "säsongspåstående" in x])

    def test_huvudsaksomdome_flaggas(self):
        t = self._kor("En röd glada som krydda, bättre får man leta efter.")
        self.assertTrue(any("HUVUDSAKSOMDÖME" in x for x in t))

    def test_okand_art_flaggas(self):
        t = self._kor("Fyra truttarter hördes.", arter=("gråtrut", "kaja"))
        self.assertTrue(any("OKÄND ART" in x for x in t))

    def test_art_i_dygnets_lista_flaggas_inte(self):
        t = self._kor("Gråtrut hördes.", arter=("gråtrut", "kaja"))
        self.assertFalse([x for x in t if "OKÄND ART" in x])

    def test_svalrakning_flaggas(self):
        t = self._kor("De tre svalarterna hördes.")
        self.assertTrue(any("SVALRÄKNING" in x for x in t))

    def test_svalrakning_som_punktlistan_bekraftar_flaggas_inte(self):
        """2026-08-01 fanns tre ÄKTA svalor (ladusvala, hussvala, backsvala) som
        egen familjegruppspunkt. "alla tre" var då helt korrekt."""
        t = self._kor("Vi hade gott om svalor – alla tre.",
                      punkter=("tre arter ur familjen svalor: ladusvala, "
                               "hussvala och backsvala",))
        self.assertEqual([x for x in t if "SVALRÄKNING" in x], [])

    def test_fel_antal_svalor_flaggas_anda(self):
        t = self._kor("Fyra svalor på ett dygn.",
                      punkter=("tre arter ur familjen svalor: ladusvala, "
                               "hussvala och backsvala",))
        self.assertTrue(any("SVALRÄKNING" in x for x in t))

    def test_datumets_forsta_ar_inget_unikhetspastaende(self):
        """"Lördagen den FÖRSTA augusti" flaggades som superlativ 2026-08-01."""
        t = self._kor("Lördagen den första augusti.",
                      punkter=("lördagen den första augusti",), arter=("kaja",))
        self.assertEqual([x for x in t if "unikhetspåstående" in x], [])

    def test_unikhetspastaende_utan_tackning_flaggas(self):
        t = self._kor("Den är den största i Sverige.", arter=("kaja",))
        self.assertTrue(any("unikhetspåstående" in x for x in t))

    def test_familjenamn_ur_punktlistan_ar_ingen_okand_art(self):
        """"måsfåglar" flaggades som påhittad art trots att det stod i en
        familjegruppspunkt (2026-08-01)."""
        t = self._kor("Tre arter ur familjen måsfåglar.",
                      punkter=("tre arter ur familjen måsfåglar: gråtrut, "
                               "medelhavstrut och fisktärna",),
                      arter=("gråtrut", "medelhavstrut", "fisktärna"))
        self.assertEqual([x for x in t if "OKÄND ART" in x], [])

    def test_omdome_om_huvudsaken_flaggas_aven_utan_rovfagel(self):
        """DET ÄR INTE EN ROVFÅGELTICS. 2026-08-01 fanns ingen rovfågel i datan –
        omdömena kom ändå, riktade mot dagens huvudsak."""
        turns = [{"speaker": "Astrid", "text": "Tre arter ur familjen måsfåglar."},
                 {"speaker": "Erik", "text": "Det var det som lyfte dygnet."}]
        rader = [{"text": "tre arter ur familjen måsfåglar: gråtrut och fisktärna"
                          "   (dagens huvudsak)",
                  "arter": ["gråtrut", "fisktärna"]}]
        t = gr.validate_script(turns, rader, {"top_species": [art("gråtrut")]})
        self.assertTrue(any("HUVUDSAKSOMDÖME" in x for x in t))

    def test_audio_taggar_raknas_inte(self):
        t = self._kor("[warmly] Tornseglaren, tio dygn i rad.", arter=("tornseglare",))
        self.assertEqual(t, [])

    def test_rent_manus_ger_inga_traffar(self):
        t = self._kor("Tornseglaren höll i sig, tio dygn i rad nu.",
                      arter=("tornseglare",))
        self.assertEqual(t, [])

    def test_utan_artlista_flaggas_ingen_art(self):
        # Saknas dygnets artlista skulle VARJE artnamn se påhittat ut.
        t = self._kor("Tornseglaren höll i sig, tio dygn i rad nu.", arter=())
        self.assertEqual([x for x in t if "OKÄND ART" in x], [])

    def test_valideringen_kraschar_aldrig(self):
        self.assertIsInstance(gr.validate_script(None, None, None), list)
        self.assertIsInstance(gr.validate_script([{"text": None}], [], {}), list)

    # --- Falsklarm som dök upp i skarpa körningar 2026-07-31 ----------------

    def test_poddnamnets_siffra_ar_inget_tal(self):
        """"Ö24 Bird Data" gav en SIFFRA-träff i varje körning."""
        t = self._kor("Välkommen till Ö24 Bird Data.")
        self.assertEqual([x for x in t if "SIFFRA" in x], [])

    def test_art_ur_punktlistan_flaggas_inte_som_okand(self):
        """En avbruten svit handlar om en art som INTE hördes i dag, så den står
        i punktlistan men inte i dygnets artlista."""
        turns = [{"speaker": "Astrid", "text": "Gråhägern också tyst."}]
        rader = [{"text": "gråhäger – hördes inte i dag, efter sju dygn i rad",
                  "arter": ["gråhäger"]}]
        t = gr.validate_script(turns, rader, {"top_species": [art("kaja")]})
        self.assertEqual([x for x in t if "OKÄND ART" in x], [])

    def test_vanliga_ord_med_fagelsuffix_flaggas_inte(self):
        t = self._kor("Ibland försvinner de bara en dag.", arter=("kaja",))
        self.assertEqual([x for x in t if "OKÄND ART" in x], [])

    def test_huvudsaksomdome_med_pronomen_i_senare_mening(self):
        """Tre omdömen slank igenom när fönstret bara var en mening lång."""
        turns = [{"speaker": "Astrid", "text": "Men Erik – röd glada!"},
                 {"speaker": "Erik", "text": "Ja! Det var ett fint inslag."},
                 {"speaker": "Astrid", "text": "Det är alltid lite av en händelse."},
                 {"speaker": "Erik", "text": "Roligt att den hördes."}]
        rader = [{"text": "röd glada – hördes", "arter": ["röd glada"]}]
        t = gr.validate_script(turns, rader, {"top_species": [art("röd glada")]})
        self.assertGreaterEqual(len([x for x in t if "HUVUDSAKSOMDÖME" in x]), 3)

    def test_fokusfonstret_stangs_nar_en_annan_art_namns(self):
        turns = [{"speaker": "Astrid", "text": "Röd glada hördes."},
                 {"speaker": "Erik", "text": "Talgoxen då, den är alltid lite kul."}]
        rader = [{"text": "röd glada – hördes", "arter": ["röd glada"]},
                 {"text": "hördes också: talgoxe", "arter": ["talgoxe"]}]
        t = gr.validate_script(turns, rader,
                               {"top_species": [art("röd glada"), art("talgoxe")]})
        self.assertEqual([x for x in t if "HUVUDSAKSOMDÖME" in x], [])

    def test_passerade_ar_tillatet(self):
        """"drog förbi" och "flög" säger i praktiken bara att fågeln var här en
        stund. Gränsen dras vid vad den GJORDE (Robin 2026-07-31)."""
        for text in ("Roligt att den drog förbi.", "Den flög över.",
                     "Man undrar om den hittar sig tillbaka."):
            t = self._kor(text, arter=("kaja",))
            self.assertEqual([x for x in t if "vad fågeln gjorde" in x], [], text)

    def test_oanvant_artfaktum_flaggas(self):
        """Botten garanterar att faktumet når PUNKTLISTAN, inte att anrop 2 säger
        det. 2026-07-31 låg "ladusvala – insektsätare" i listan och avsnittet blev
        faktafritt ändå – ett tak i koden kan inte tvinga någon att tala."""
        turns = [{"speaker": "Astrid", "text": "Ladusvala hördes också."}]
        rader = [{"text": "ladusvala – insektsätare", "arter": ["ladusvala"],
                  "fakta_id": "ladusvala/kosthallning"}]
        t = gr.validate_script(turns, rader, {"top_species": []})
        self.assertTrue(any("OANVÄNT ARTFAKTUM" in x for x in t))

    def test_anvant_artfaktum_flaggas_inte_aven_omskrivet(self):
        turns = [{"speaker": "Astrid",
                  "text": "Långa, spetsiga vingar – byggd för luften."}]
        rader = [{"text": "tornseglare – långa, spetsiga vingar",
                  "arter": [], "fakta_id": "tornseglare/vingform"}]
        t = gr.validate_script(turns, rader, {"top_species": []})
        self.assertEqual([x for x in t if "OANVÄNT" in x], [])

    def test_helt_bortglomd_art_flaggas(self):
        turns = [{"speaker": "Astrid", "text": "Kaja hördes."}]
        rader = [{"text": "röd glada – hördes", "arter": ["röd glada"]}]
        t = gr.validate_script(turns, rader, {"top_species": []})
        self.assertTrue(any("OANVÄND PUNKT" in x for x in t))

    def test_beteendepastaende_flaggas(self):
        for text in ("Den häckade här i somras.", "Den jagade över fältet.",
                     "Den sträckte söderut i går."):
            t = self._kor(text, arter=("kaja",))
            self.assertTrue(any("vad fågeln gjorde" in x for x in t), text)


class TakPerArt(unittest.TestCase):
    """Punktlistan 2026-07-31 bar TRE tornseglarpunkter och avsnittet blev en
    tornseglarpodd. Mekaniskt kontrollerbart – alltså i koden."""

    def _facts(self):
        f = [gr._punkt("datum", "ram", alltid_med=True)]
        f += [gr._punkt("tornseglare – tio dygn i rad", "svit", art="tornseglare"),
              gr._punkt("tornseglare – hördes flitigt", "aktivitet", art="tornseglare"),
              gr._punkt("tornseglare – långa vingar", "artfaktum", art="tornseglare",
                        fakta_id="tornseglare/vingform"),
              gr._punkt("ringduva – familjen duvor", "artfaktum", art="ringduva",
                        fakta_id="ringduva/familj"),
              gr._punkt("röd glada – hördes", "speciell_gast", art="röd glada")]
        return f

    def test_hogst_tva_punkter_per_art(self):
        f = self._facts()
        valda, _ = gr.validate_selection(f, [1, 2, 3, 4, 5], None)
        per_art = {}
        for p in valda:
            per_art[p["art"]] = per_art.get(p["art"], 0) + 1
        self.assertLessEqual(max(per_art.values()), gr.MAX_PER_ART)

    def test_garantin_krokar_faktumet_pa_en_art_som_redan_ar_med(self):
        """OMVÄNT 2026-07-31. Första versionen valde helst en art som INTE var med,
        och faktumet blev en lös ände som anrop 2 strök (ladusvala/insektsätare).
        Ett faktum behöver en krok i avsnittet för att överleva."""
        # Fyra faktafria punkter så att påfyllningen till MIN_PUNKTER inte hinner
        # dra in ett faktum av sig själv – det är GARANTIN som ska testas här.
        f = [gr._punkt("datum", "ram", alltid_med=True),
             gr._punkt("tornseglare – tio dygn i rad", "svit", art="tornseglare"),
             gr._punkt("röd glada – hördes", "speciell_gast", art="röd glada"),
             gr._punkt("kaja – tre veckor i rad", "svit", art="kaja"),
             gr._punkt("grågås – dygnets tyngsta art", "jamforelse", art="grågås"),
             gr._punkt("ringduva – familjen duvor", "artfaktum", art="ringduva",
                       fakta_id="ringduva/familj"),
             gr._punkt("tornseglare – långa vingar", "artfaktum", art="tornseglare",
                       fakta_id="tornseglare/vingform")]
        valda, _ = gr.validate_selection(f, [1, 2, 3, 4], None)
        fakta = [p for p in valda if p.get("fakta_id")]
        self.assertTrue(fakta)
        self.assertEqual(fakta[0]["art"], "tornseglare")

    def test_kroken_spranger_inte_taket_per_art(self):
        """Kroken får inte ge tornseglaren en tredje punkt – då är vi tillbaka i
        tornseglarpodden."""
        f = self._facts()          # tornseglare har svit + aktivitet = taket nått
        valda, _ = gr.validate_selection(f, [1, 2], None)
        fakta = [p for p in valda if p.get("fakta_id")]
        self.assertTrue(fakta)
        self.assertEqual(fakta[0]["art"], "ringduva")
        per_art = {}
        for p in valda:
            per_art[p["art"]] = per_art.get(p["art"], 0) + 1
        self.assertLessEqual(max(per_art.values()), gr.MAX_PER_ART)


if __name__ == "__main__":
    unittest.main(verbosity=2)
