"""Enhetstester för historik-/signallogiken i generate_report.py.

Kör: pytest test_signals.py

Inga nätanrop görs. Vi sätter dummy-env INNAN import (modulen läser några env-
variabler vid import) och monkeypatchar TODAY samt GBIF-cachen där det behövs.
"""

import os
import datetime as dt

# Modulen läser dessa vid import – sätt dummies så importen går utan riktiga secrets.
os.environ.setdefault("BW_STATION_ID", "28650")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("SITE_BASE_URL", "https://example.test")

import generate_report as gr  # noqa: E402


# --- hjälpare -------------------------------------------------------------
def sp(scientific, display=None, name=None, activity="enstaka"):
    """Bygg ett art-objekt som det ser ut efter fetch_birdweather()."""
    return {
        "scientific": scientific,
        "display": display or scientific,
        "name": name or scientific,
        "activity": activity,
    }


def today_payload(species, date="2026-07-20"):
    return {
        "date": date,
        "species_count": len(species),
        "top_species": species,
    }


def day(date, sci_names):
    """En historikdag i NYTT schema (nycklad på vetenskapligt namn)."""
    return {
        "date": date,
        "species_count": len(sci_names),
        "top": [{"sci": s, "name": s} for s in sci_names],
    }


def set_today(monkeypatch, iso):
    monkeypatch.setattr(gr, "TODAY", dt.date.fromisoformat(iso))


# --- new_species ----------------------------------------------------------
def test_all_new_on_empty_history(monkeypatch):
    set_today(monkeypatch, "2026-07-20")
    today = today_payload([sp("Passer domesticus", "gråsparv"),
                           sp("Apus apus", "tornseglare")])
    sig = gr.derive_signals(today, {"species_ever": {}, "recent_days": []})
    assert set(sig["new_species"]) == {"gråsparv", "tornseglare"}


# --- REGRESSIONSTEST FÖR BUGGEN: display-namn ändras, men matchning ska hålla
def test_scientific_key_survives_display_rename(monkeypatch):
    """Arten finns i minnet (nycklad på vetenskapligt namn). Idag kommer samma
    art med ETT ANNAT svenskt visningsnamn. Den får INTE flaggas som ny."""
    set_today(monkeypatch, "2026-07-20")
    history = {
        "species_ever": {"Passer domesticus": "2026-07-01"},
        "recent_days": [day("2026-07-19", ["Passer domesticus"])],
    }
    today = today_payload([sp("Passer domesticus", display="pilfink-felaktigt-namn")])
    sig = gr.derive_signals(today, history)
    assert sig["new_species"] == []          # matchar på vetenskapligt namn
    assert sig["first_this_year"] == []
    assert sig["returning_after_gap"] == []


# --- first_this_year ------------------------------------------------------
def test_first_this_year(monkeypatch):
    """Sedd i FJOL men inte i år -> första för året (inte 'ny någonsin')."""
    set_today(monkeypatch, "2026-07-20")
    history = {
        "species_ever": {"Grus grus": "2025-05-10"},
        "recent_days": [day("2025-05-10", ["Grus grus"])],   # bara fjolårsdag
    }
    today = today_payload([sp("Grus grus", "trana")])
    sig = gr.derive_signals(today, history)
    assert sig["new_species"] == []
    assert sig["first_this_year"] == ["trana"]


def test_not_first_if_seen_this_year(monkeypatch):
    set_today(monkeypatch, "2026-07-20")
    history = {
        "species_ever": {"Grus grus": "2026-03-01"},
        "recent_days": [day("2026-03-01", ["Grus grus"])],   # redan i år
    }
    today = today_payload([sp("Grus grus", "trana")])
    sig = gr.derive_signals(today, history)
    assert sig["first_this_year"] == []


# --- returning_after_gap --------------------------------------------------
def test_returning_after_gap(monkeypatch):
    """Sedd tidigare i år men inte inom RETURN_GAP dagar -> återvändande."""
    set_today(monkeypatch, "2026-07-20")
    history = {
        "species_ever": {"Ardea cinerea": "2026-06-01"},
        # sedd i år (så inte "första för året") men >14 dagar sedan
        "recent_days": [day("2026-06-01", ["Ardea cinerea"])],
    }
    today = today_payload([sp("Ardea cinerea", "gråhäger")])
    sig = gr.derive_signals(today, history)
    assert sig["new_species"] == []
    assert sig["first_this_year"] == []
    assert sig["returning_after_gap"] == ["gråhäger"]


def test_not_returning_if_seen_recently(monkeypatch):
    set_today(monkeypatch, "2026-07-20")
    history = {
        "species_ever": {"Ardea cinerea": "2026-06-01"},
        "recent_days": [
            day("2026-06-01", ["Ardea cinerea"]),
            day("2026-07-18", ["Ardea cinerea"]),   # inom 14 dagar
        ],
    }
    today = today_payload([sp("Ardea cinerea", "gråhäger")])
    sig = gr.derive_signals(today, history)
    assert sig["returning_after_gap"] == []


# --- vs_yesterday ---------------------------------------------------------
def test_vs_yesterday(monkeypatch):
    set_today(monkeypatch, "2026-07-20")
    history = {
        "species_ever": {"Apus apus": "2026-07-19"},
        "recent_days": [day("2026-07-19", ["Apus apus", "Turdus merula"])],
    }
    today = today_payload([sp("Apus apus", "tornseglare")])
    sig = gr.derive_signals(today, history)
    assert sig["vs_yesterday"] == {"artrikedom_igar": 2, "artrikedom_idag": 1}


# --- reset_today (omkörning samma dag) ------------------------------------
def test_reset_today_removes_todays_entry():
    history = {
        "species_ever": {"Apus apus": "2026-07-19", "Grus grus": "2026-07-20"},
        "recent_days": [
            day("2026-07-19", ["Apus apus"]),
            day("2026-07-20", ["Apus apus", "Grus grus"]),   # dagens (från tidigare körning)
        ],
    }
    gr.reset_today(history, "2026-07-20")
    # Dagens dag borttagen, gårdagens kvar
    assert [d["date"] for d in history["recent_days"]] == ["2026-07-19"]
    # Art som fått förstasett-datum = idag är borttagen; äldre art kvar
    assert history["species_ever"] == {"Apus apus": "2026-07-19"}


def test_rerun_same_day_recomputes_new_species(monkeypatch):
    """Simulera en tidigare körning idag som redan lagt in trana, kör om:
    reset_today ska rensa så att trana åter räknas som ny."""
    set_today(monkeypatch, "2026-07-20")
    history = {
        "species_ever": {"Grus grus": "2026-07-20"},                 # lades av tidigare körning idag
        "recent_days": [day("2026-07-20", ["Grus grus"])],           # dito
    }
    gr.reset_today(history, "2026-07-20")
    today = today_payload([sp("Grus grus", "trana")])
    sig = gr.derive_signals(today, history)
    assert sig["new_species"] == ["trana"]


# --- migrate_history ------------------------------------------------------
def test_migrate_swedish_display_keys_to_scientific(monkeypatch):
    """Gammalt schema med svenska display-nycklar migreras till vetenskapliga
    via species_sv.json-cachen."""
    monkeypatch.setattr(gr, "_load_sv_cache",
                        lambda: {"Passer domesticus": "gråsparv",
                                 "Apus apus": "tornseglare"})
    history = {
        "species_ever": {"gråsparv": "2026-07-01", "tornseglare": "2026-07-02"},
        "recent_days": [{"date": "2026-07-19", "species_count": 1,
                         "top": [{"name": "gråsparv"}]}],   # gammalt: bara name
    }
    gr.migrate_history(history)
    assert history["species_ever"] == {"Passer domesticus": "2026-07-01",
                                       "Apus apus": "2026-07-02"}
    assert history["recent_days"][0]["top"][0]["sci"] == "Passer domesticus"


def test_migrate_is_idempotent(monkeypatch):
    monkeypatch.setattr(gr, "_load_sv_cache",
                        lambda: {"Passer domesticus": "gråsparv"})
    history = {
        "species_ever": {"Passer domesticus": "2026-07-01"},
        "recent_days": [day("2026-07-19", ["Passer domesticus"])],
    }
    before = {"species_ever": dict(history["species_ever"]),
              "recent_days": [dict(d) for d in history["recent_days"]]}
    gr.migrate_history(history)
    assert history["species_ever"] == before["species_ever"]
    assert history["recent_days"][0]["top"][0]["sci"] == "Passer domesticus"


def test_migrate_keeps_unmappable_english_names(monkeypatch):
    """Engelska namn som inte finns i cachen behålls (matchar först när arten
    hörs på nytt) – ingen krasch, ingen tyst dataförlust."""
    monkeypatch.setattr(gr, "_load_sv_cache", lambda: {"Apus apus": "tornseglare"})
    history = {"species_ever": {"House Sparrow": "2026-07-01"}, "recent_days": []}
    gr.migrate_history(history)
    assert history["species_ever"] == {"House Sparrow": "2026-07-01"}
