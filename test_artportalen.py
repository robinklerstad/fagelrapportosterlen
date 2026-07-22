#!/usr/bin/env python3
"""Tester för artportalen.py + integrationen i generate_report.derive_signals.
Alla nätanrop mockas – ingen riktig trafik. Kör: python3 test_artportalen.py"""

import os
import sys
import json
import tempfile
import unittest
from pathlib import Path

# Isolera cache-filerna till en tempmapp INNAN modulerna importeras (sökvägarna
# läses på modulnivå ur env).
_TMP = Path(tempfile.mkdtemp())
os.environ["AP_TAXON_CACHE"] = str(_TMP / "species_taxon.json")
os.environ["AP_LOCAL_CACHE"] = str(_TMP / "species_local.json")
os.environ["AP_YEARS"] = "10"
os.environ["AP_SEASON_WEEKS"] = "3"
os.environ["AP_RADIUS_KM"] = "25"
os.environ["SOS_API_KEY"] = "test-sos"
os.environ["ARTFAKTA_API_KEY"] = "test-artfakta"

# Dummy-env så generate_report går att importera (den läser secrets på modulnivå).
os.environ.setdefault("BW_STATION_ID", "28650")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")
os.environ.setdefault("SITE_BASE_URL", "https://example.com")

import artportalen as ap


class RarityClass(unittest.TestCase):
    def test_thresholds_over_10_years(self):
        cases = {0: "mycket_ovanlig", 1: "mycket_ovanlig", 2: "ovanlig",
                 3: "periodvis", 6: "periodvis", 7: "regelbunden", 10: "regelbunden"}
        for yrs, expected in cases.items():
            self.assertEqual(ap.rarity_class(yrs, 10), expected, f"{yrs}/10")

    def test_zero_years_total(self):
        self.assertEqual(ap.rarity_class(0, 0), "mycket_ovanlig")


class LookupTaxonId(unittest.TestCase):
    def setUp(self):
        self._get = ap._get

    def tearDown(self):
        ap._get = self._get

    def test_confirms_via_scientific_and_category(self):
        def fake_get(url, key, params=None):
            if url.endswith("/speciesdata/search"):
                return [{"taxonId": 102936, "scientificName": "Melanitta nigra"}]
            if url.endswith("/speciesdata"):
                self.assertEqual(params["taxa"], 102936)
                return [{"taxonId": 102936, "scientificName": "Melanitta nigra",
                         "swedishName": "sjöorre", "category": {"id": 17, "name": "Art"}}]
            raise AssertionError(url)
        ap._get = fake_get
        self.assertEqual(ap.lookup_taxon_id("Melanitta nigra", "k"), 102936)

    def test_rejects_wrong_category(self):
        # Sökträff pekar på en ordning/grupp (category != 17) -> ingen träff.
        def fake_get(url, key, params=None):
            if url.endswith("/speciesdata/search"):
                return [{"taxonId": 3000284, "scientificName": "Charadriiformes"}]
            if url.endswith("/speciesdata"):
                return [{"taxonId": 3000284, "scientificName": "Charadriiformes",
                         "category": {"id": 11, "name": "Ordning"}}]
            raise AssertionError(url)
        ap._get = fake_get
        self.assertIsNone(ap.lookup_taxon_id("Charadriiformes", "k"))

    def test_no_hits(self):
        ap._get = lambda url, key, params=None: []
        self.assertIsNone(ap.lookup_taxon_id("Nonexistus fakus", "k"))


class Aggregate(unittest.TestCase):
    def setUp(self):
        self._post = ap._post

    def tearDown(self):
        ap._post = self._post

    def test_pagination_and_aves_exclusion(self):
        # take=1 tvingar fram paginering: en post per sida, total=3.
        all_records = [
            {"taxonId": 102936, "observationCount": 500},
            {"taxonId": ap.AVES_TAXON_ID, "observationCount": 1},   # brus, exkluderas
            {"taxonId": 205835, "observationCount": 3},
        ]

        def fake_post(url, key, body, params=None):
            self.assertIn("TaxonAggregation", url)
            self.assertEqual(body["taxon"]["taxonCategories"], [ap.SPECIES_CATEGORY_ID])
            skip, take = params["skip"], params["take"]
            return {"totalCount": len(all_records),
                    "records": all_records[skip:skip + take]}
        ap._post = fake_post
        ids = ap.aggregate_taxon_ids("2020-07-01", "2020-08-12", "k", take=1)
        self.assertEqual(ids, {102936, 205835})   # Aves-klassen exkluderad


class BuildLocalCache(unittest.TestCase):
    def setUp(self):
        self._agg = ap.aggregate_taxon_ids
        self._cnt = ap.aggregate_taxon_counts

    def tearDown(self):
        ap.aggregate_taxon_ids = self._agg
        ap.aggregate_taxon_counts = self._cnt
        for p in (ap.TAXON_CACHE, ap.LOCAL_CACHE):
            if p.exists():
                p.unlink()

    def test_counts_years_and_classifies(self):
        # 102936 varje år (10/10 -> regelbunden); 205835 bara 1 år (1/10 -> mkt ovanlig)
        def fake_agg(start, end, key, verbose=False):
            yr = int(start[:4])
            ids = {102936}
            if yr == 2024:
                ids.add(205835)
            return ids
        # All-tids-antal (A): 102936 vanlig, 205835 bara ett fåtal noteringar.
        def fake_counts(start, end, key, verbose=False):
            return {102936: {"antal": 4000},
                    205835: {"antal": 3, "forsta_ar": 2019}}
        ap.aggregate_taxon_ids = fake_agg
        ap.aggregate_taxon_counts = fake_counts
        import datetime as dt
        cache = ap.build_local_cache(center=dt.date(2026, 7, 22), verbose=False)
        self.assertEqual(cache["years"], 10)
        self.assertEqual(cache["species"]["102936"]["klass"], "regelbunden")
        self.assertEqual(cache["species"]["102936"]["ar_sedda"], 10)
        self.assertEqual(cache["species"]["205835"]["klass"], "mycket_ovanlig")
        self.assertEqual(cache["species"]["205835"]["ar_sedda"], 1)
        # A: all_time skrevs med och forsta_ar bevaras
        self.assertEqual(cache["all_time"]["205835"]["antal"], 3)
        self.assertEqual(cache["all_time"]["205835"]["forsta_ar"], 2019)
        # Cachen skrevs till disk
        self.assertTrue(ap.LOCAL_CACHE.exists())

    def test_survives_all_time_failure(self):
        # Ett fel i all-tids-frågan (A) får inte fälla kärn-cachen (säsong).
        def fake_agg(start, end, key, verbose=False):
            return {102936}
        def boom(*a, **k):
            raise RuntimeError("all-tid nere")
        ap.aggregate_taxon_ids = fake_agg
        ap.aggregate_taxon_counts = boom
        import datetime as dt
        cache = ap.build_local_cache(center=dt.date(2026, 7, 22), verbose=False)
        self.assertEqual(cache["species"]["102936"]["ar_sedda"], 10)
        self.assertEqual(cache["all_time"], {})


class LocalContext(unittest.TestCase):
    def tearDown(self):
        for p in (ap.TAXON_CACHE, ap.LOCAL_CACHE):
            if p.exists():
                p.unlink()

    def _write_caches(self, taxon, local):
        ap._save_json(ap.TAXON_CACHE, taxon)
        ap._save_json(ap.LOCAL_CACHE, local)

    def test_silent_empty_without_caches(self):
        self.assertEqual(ap.local_context([{"scientific": "Melanitta nigra"}]), [])

    def test_flags_only_noteworthy(self):
        self._write_caches(
            {"Melanitta nigra": 102936, "Corvus monedula": 111, "Phylloscopus x": 222},
            {"years": 10, "species": {
                "102936": {"ar_sedda": 2, "klass": "ovanlig"},
                "111": {"ar_sedda": 10, "klass": "regelbunden"}}},
        )
        today = [
            {"scientific": "Melanitta nigra", "display": "sjöorre"},   # ovanlig -> med
            {"scientific": "Corvus monedula", "display": "kaja"},      # regelbunden -> ej
            {"scientific": "Phylloscopus x", "display": "ngt"},        # saknas i local -> ingen_lokal_notering
            {"scientific": "Okänd art"},                               # ej i taxon -> skippas
        ]
        ctx = ap.local_context(today)
        arter = {c["art"]: c for c in ctx}
        self.assertIn("sjöorre", arter)
        self.assertEqual(arter["sjöorre"]["klass"], "ovanlig")
        self.assertNotIn("kaja", arter)
        self.assertIn("ngt", arter)
        self.assertEqual(arter["ngt"]["klass"], "ingen_lokal_notering")
        self.assertEqual(arter["ngt"]["ar_sedda"], 0)
        self.assertEqual(len(ctx), 2)


class AbsRarity(unittest.TestCase):
    def test_thresholds(self):
        self.assertIsNone(ap.abs_rarity_class(None))
        self.assertEqual(ap.abs_rarity_class(1), "enstaka_noteringar")
        self.assertEqual(ap.abs_rarity_class(5), "enstaka_noteringar")
        self.assertEqual(ap.abs_rarity_class(6), "fa_noteringar")
        self.assertEqual(ap.abs_rarity_class(20), "fa_noteringar")
        self.assertIsNone(ap.abs_rarity_class(21))
        self.assertIsNone(ap.abs_rarity_class(5000))


class Redlist(unittest.TestCase):
    def test_extract_various_shapes(self):
        # kategori som sträng
        self.assertEqual(ap._extract_redlist({"redlistInfo": {"category": "VU"}}),
                         ("VU", "sårbar"))
        # kategori som dict med value
        self.assertEqual(
            ap._extract_redlist({"redlistInfo": {"category": {"value": "NT"}}}),
            ("NT", "nära hotad"))
        # lista av perioder -> senaste periodId vinner
        rec = {"redlistInfo": [
            {"periodId": 2015, "category": "EN"},
            {"periodId": 2020, "category": "VU"}]}
        self.assertEqual(ap._extract_redlist(rec), ("VU", "sårbar"))
        # ingen rödlista
        self.assertEqual(ap._extract_redlist({"swedishName": "kaja"}), (None, None))
        self.assertEqual(ap._extract_redlist("skräp"), (None, None))

    def test_taxon_value_helpers(self):
        # bakåtkompatibelt: bart int
        self.assertEqual(ap._taxon_id(102936), 102936)
        self.assertEqual(ap._taxon_redlist(102936), (None, None))
        # ny form: dict
        val = {"id": 102936, "rodlista": "VU", "rodlista_namn": "sårbar"}
        self.assertEqual(ap._taxon_id(val), 102936)
        self.assertEqual(ap._taxon_redlist(val), ("VU", "sårbar"))
        self.assertIsNone(ap._taxon_id(None))
        self.assertIsNone(ap._taxon_id(True))  # bool är inte ett taxonID


class BuildTaxonCache(unittest.TestCase):
    def setUp(self):
        self._lookup = ap._lookup

    def tearDown(self):
        ap._lookup = self._lookup
        if ap.TAXON_CACHE.exists():
            ap.TAXON_CACHE.unlink()

    def test_stores_id_and_redlist(self):
        def fake_lookup(sci, key, verbose=False):
            recs = {
                "Melanitta nigra": (102936, {"scientificName": "Melanitta nigra",
                                             "redlistInfo": {"category": "NT"}}),
                "Corvus monedula": (111, {"scientificName": "Corvus monedula"}),
                "Nonexistus fakus": (None, None),
            }
            return recs[sci]
        ap._lookup = fake_lookup
        cache = ap.build_taxon_cache(
            ["Melanitta nigra", "Corvus monedula", "Nonexistus fakus"], verbose=False)
        self.assertEqual(cache["Melanitta nigra"]["id"], 102936)
        self.assertEqual(cache["Melanitta nigra"]["rodlista"], "NT")
        self.assertEqual(cache["Corvus monedula"]["id"], 111)
        self.assertNotIn("rodlista", cache["Corvus monedula"])
        self.assertIsNone(cache["Nonexistus fakus"])


class CombinedContext(unittest.TestCase):
    """local_context ska slå ihop säsong (klass), absolut sällsynthet (A) och
    rödlista (C), och ta med en art om NÅGON signal är en nyhet."""

    def tearDown(self):
        for p in (ap.TAXON_CACHE, ap.LOCAL_CACHE):
            if p.exists():
                p.unlink()

    def test_signals_merge_and_filter(self):
        ap._save_json(ap.TAXON_CACHE, {
            "Melanitta nigra": {"id": 102936},                       # bara A
            "Corvus monedula": 111,                                  # inget -> ute
            "Grus grus": {"id": 333, "rodlista": "VU",
                          "rodlista_namn": "sårbar"},                # bara C
            "Pluvialis apricaria": {"id": 444},                      # bara säsong
        })
        ap._save_json(ap.LOCAL_CACHE, {
            "years": 10,
            "species": {
                "102936": {"ar_sedda": 8, "klass": "regelbunden"},   # vanlig säsong
                "111": {"ar_sedda": 10, "klass": "regelbunden"},
                "333": {"ar_sedda": 7, "klass": "regelbunden"},
                "444": {"ar_sedda": 2, "klass": "ovanlig"},
            },
            "all_time": {
                "102936": {"antal": 3, "forsta_ar": 2018},           # enstaka -> A
                "111": {"antal": 9000},
                "333": {"antal": 8000},
                "444": {"antal": 500},
            },
        })
        today = [
            {"scientific": "Melanitta nigra", "display": "sjöorre"},
            {"scientific": "Corvus monedula", "display": "kaja"},
            {"scientific": "Grus grus", "display": "trana"},
            {"scientific": "Pluvialis apricaria", "display": "ljungpipare"},
        ]
        arter = {c["art"]: c for c in ap.local_context(today)}
        self.assertNotIn("kaja", arter)                              # helt vanlig
        # A: sjöorre – enstaka noteringar totalt, ingen säsongsklass
        self.assertEqual(arter["sjöorre"]["antal_klass"], "enstaka_noteringar")
        self.assertEqual(arter["sjöorre"]["forsta_ar"], 2018)
        self.assertNotIn("klass", arter["sjöorre"])
        # C: trana – rödlistad, inget annat
        self.assertEqual(arter["trana"]["rodlista"], "VU")
        self.assertNotIn("antal_klass", arter["trana"])
        # säsong: ljungpipare – ovanlig, inget A/C
        self.assertEqual(arter["ljungpipare"]["klass"], "ovanlig")
        self.assertNotIn("antal_klass", arter["ljungpipare"])
        self.assertNotIn("rodlista", arter["ljungpipare"])


class DeriveSignalsIntegration(unittest.TestCase):
    """Bekräfta att generate_report.derive_signals nu innehåller lokal_kontext och
    aldrig faller på Artportalen-delen."""

    def _today_history(self):
        today = {"date": "2026-07-22", "species_count": 2,
                 "top_species": [
                     {"name": "sjöorre", "scientific": "Melanitta nigra",
                      "display": "sjöorre"},
                     {"name": "kaja", "scientific": "Corvus monedula",
                      "display": "kaja"}]}
        history = {"species_ever": {"Corvus monedula": "2020-01-01"},
                   "recent_days": []}
        return today, history

    def test_includes_lokal_kontext_key(self):
        import generate_report as gr
        # Säkerställ att modulen faktiskt fick tag i artportalen.
        self.assertIsNotNone(gr.artportalen)
        # Skriv cacher så vi får en riktig träff.
        ap._save_json(ap.TAXON_CACHE, {"Melanitta nigra": 102936})
        ap._save_json(ap.LOCAL_CACHE, {"years": 10, "species": {
            "102936": {"ar_sedda": 1, "klass": "mycket_ovanlig"}}})
        today, history = self._today_history()
        sig = gr.derive_signals(today, history)
        self.assertIn("lokal_kontext", sig)
        arter = {c["art"] for c in sig["lokal_kontext"]}
        self.assertIn("sjöorre", arter)
        for p in (ap.TAXON_CACHE, ap.LOCAL_CACHE):
            p.unlink(missing_ok=True)

    def test_empty_when_caches_missing(self):
        import generate_report as gr
        for p in (ap.TAXON_CACHE, ap.LOCAL_CACHE):
            if p.exists():
                p.unlink()
        today, history = self._today_history()
        sig = gr.derive_signals(today, history)
        self.assertEqual(sig["lokal_kontext"], [])

    def test_survives_artportalen_exception(self):
        import generate_report as gr
        orig = gr.artportalen

        class Boom:
            @staticmethod
            def local_context(_):
                raise RuntimeError("nätfel")
        gr.artportalen = Boom
        try:
            today, history = self._today_history()
            sig = gr.derive_signals(today, history)
            self.assertEqual(sig["lokal_kontext"], [])
        finally:
            gr.artportalen = orig


if __name__ == "__main__":
    unittest.main(verbosity=2)
