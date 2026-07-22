#!/usr/bin/env python3
"""
Daily BirdWeather -> two-host Swedish voice podcast with memory of past days.

Two AI hosts (Astrid & Erik) chat about the last day's birds, like a NotebookLM-style
deep dive but automated, scheduled, in Swedish, and tuned to one station.

Pipeline (runs on GitHub Actions, no server needed):
  1. Load history.json (the repo IS the database).
  2. Pull the last 24h of detections from the BirdWeather API.
  3. Compute continuity FACTS in Python (new / returning / first-of-year / vs-yesterday)
     so the hosts reference real things, never hallucinated ones.
  4. Ask Claude for a two-host DIALOGUE as JSON: [{speaker, text}, ...].
  5. Synthesize each line with that host's voice, stitch into one mp3 via ffmpeg.
  6. Write the mp3, regenerate feed.xml + index.html, update history.json.
  7. (The workflow commits everything; GitHub Pages serves it.)

Secrets / env vars (set as GitHub Actions secrets):
  Required:
    BW_STATION_ID       public BirdWeather station ID (a number)
    ANTHROPIC_API_KEY   Claude API key
    SITE_BASE_URL       e.g. https://<you>.github.io/<repo>   (no trailing slash)
  TTS (pick one provider):
    TTS_PROVIDER        "openai" (default) or "elevenlabs"
    # OpenAI:
    OPENAI_API_KEY
    OPENAI_VOICE_A      optional, default "nova"  (Astrid)
    OPENAI_VOICE_B      optional, default "onyx"  (Erik)
    # ElevenLabs:
    ELEVENLABS_API_KEY
    ELEVENLABS_VOICE_A  voice id for Astrid
    ELEVENLABS_VOICE_B  voice id for Erik

Requires ffmpeg on PATH (the workflow installs it).
"""

import os
import sys
import json
import re
import subprocess
import tempfile
import datetime as dt
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

import requests

# Lokal artkontext från Artportalen (SLU Artdatabanken). Valfri: saknas modulen
# eller dess cacher körs podden precis som förut, bara utan lokal-ovanlig-signal.
try:
    import artportalen
except Exception:
    artportalen = None

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BW_STATION_ID     = os.environ["BW_STATION_ID"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
SITE_BASE_URL     = os.environ["SITE_BASE_URL"].rstrip("/")
TTS_PROVIDER      = os.environ.get("TTS_PROVIDER", "openai").lower()

# SKIP_TTS=1: generera och spara MANUSET (Claude körs), men hoppa över
# röstläggningen helt – och rör inte historik/feed/sida. Syfte: iterera på
# persona/prompt utan att bränna ElevenLabs-credits. Kombinera gärna med
# TEST_OUTPUT_DIR för att skriva till en testmapp. Ingen mp3 skapas.
SKIP_TTS = os.environ.get("SKIP_TTS", "") not in ("", "0", "false", "no")

CLAUDE_MODEL  = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")  # set in the workflow

# Avsnitt (mp3-filer) och historik (artminne) är två OLIKA knappar – blanda dem inte:
#  - KEEP_EPISODES: hur många mp3-filer som ligger kvar i repot/Pages. Håll litet;
#    varje avsnitt är ~1 MB, så ett år vore ~365 MB och sväller repot i onödan.
#  - KEEP_HISTORY: hur många DAGAR av artminne som sparas. Datan är pytteliten
#    (bara namn + antal per dag, storleksordning 100–200 KB/år), så ett par år är i
#    praktiken gratis – och nödvändigt för årscykel-logiken ("första för året",
#    återvändande efter uppehåll). Default ~2,2 år.
# Båda kan överstyras via miljövariabel i workflowen (driftsvärden bor där).
KEEP_EPISODES = int(os.environ.get("KEEP_EPISODES", "30"))
KEEP_HISTORY  = int(os.environ.get("KEEP_HISTORY", "800"))
RETURN_GAP    = int(os.environ.get("RETURN_GAP", "14"))

HOST_A = "Astrid"
HOST_B = "Erik"

# Sätt TEST_OUTPUT_DIR för att köra lokalt mot en testmapp utan att röra docs/
# eller history.json. Ex: TEST_OUTPUT_DIR=test_output python generate_report.py
_TEST_DIR = os.environ.get("TEST_OUTPUT_DIR")
if _TEST_DIR:
    DOCS_DIR     = Path(_TEST_DIR)
    HISTORY_PATH = Path(_TEST_DIR) / "history.json"
    print(f"** TESTLÄGE: skriver till {_TEST_DIR}/ (rör inte docs/ eller history.json) **")
else:
    DOCS_DIR     = Path("docs")
    HISTORY_PATH = Path("history.json")

EPISODES_DIR = DOCS_DIR / "episodes"
FEED_PATH    = DOCS_DIR / "feed.xml"
INDEX_PATH   = DOCS_DIR / "index.html"
SV_NAMES_PATH = Path("species_sv.json")   # cache: vetenskapligt namn -> svenskt namn

PODCAST_TITLE  = "Ö24 Bird Data"
PODCAST_DESC   = "Daglig fågelrapport från vår BirdWeather-station i Simrishamn – skriven och uppläst av AI-rösterna Astrid och Erik."
PODCAST_AUTHOR = "Ö24 Bird Data"
PODCAST_LANG   = "sv"
COVER_FILE     = "cover.png"   # ligger i docs/ ; byt till cover.jpg om du använder JPG

BW_GRAPHQL = "https://app.birdweather.com/graphql"
# Datumet ska följa svensk tid, inte runnerns UTC – annars blir ett avsnitt som
# genereras sent på kvällen svensk tid daterat till gårdagen.
TODAY      = dt.datetime.now(ZoneInfo("Europe/Stockholm")).date()


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------
def load_history():
    if HISTORY_PATH.exists():
        return json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    return {"first_run": TODAY.isoformat(), "species_ever": {}, "recent_days": []}


def save_history(history):
    HISTORY_PATH.write_text(
        json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Historik-nycklar. ALLT nycklas på VETENSKAPLIGT namn – det är språkoberoende
# och stabilt. Visningsnamnet (svenskt) kan ändras över tid (nytt GBIF-namn, en
# art som saknar svenskt namn en dag och får ett senare); gör det ALDRIG till
# nyckel, annars tappar minnet matchning. (Det var exakt buggen 2026-07 när
# nycklarna gick från engelska till svenska namn och allt såg "nytt" ut.)
# ---------------------------------------------------------------------------
def _sci_key(s):
    """Kanonisk, stabil nyckel för en art: vetenskapligt namn (fallback: namn)."""
    return s.get("scientific") or s.get("display") or s.get("name")


def _day_keys(day):
    """Artnycklar för en historikdag. Klarar både nytt schema (t['sci']) och
    gammalt (t['name'])."""
    return {t.get("sci") or t.get("name") for t in day.get("top", [])}


def _reverse_sv_map():
    """{svenskt namn (gemener) -> vetenskapligt} byggt ur species_sv.json, för
    att migrera gamla display-nycklar tillbaka till vetenskapliga."""
    rev = {}
    for sci, sv in _load_sv_cache().items():
        if sv:
            rev[sv.strip().lower()] = sci
    return rev


def migrate_history(history):
    """Uppgradera gammal display-nycklad historik till vetenskapliga nycklar.
    Idempotent: redan migrerad data lämnas orörd. Svenska namn mappas via
    species_sv.json; namn som inte kan mappas (t.ex. gamla engelska) behålls som
    de är och matchar då först när arten hörs på nytt."""
    rev = _reverse_sv_map()

    def to_sci(name):
        return rev.get(name.strip().lower(), name) if name else name

    old_ever = history.get("species_ever", {})
    new_ever = {}
    for name, date in old_ever.items():
        key = to_sci(name)
        # behåll tidigaste datum om två gamla namn mappar till samma art
        if key not in new_ever or date < new_ever[key]:
            new_ever[key] = date
    history["species_ever"] = new_ever

    for day in history.get("recent_days", []):
        for t in day.get("top", []):
            if "sci" not in t:
                t["sci"] = to_sci(t.get("name"))
    return history


def reset_today(history, today_iso):
    """Ta bort ev. redan sparad post för DAGENS datum (omkörning samma dag), så
    signaler räknas korrekt och historiken inte dubbellagras. Tar även bort
    arter som fick sitt förstasett-datum satt till idag av en tidigare körning."""
    history["recent_days"] = [
        d for d in history.get("recent_days", []) if d.get("date") != today_iso
    ]
    ever = history.get("species_ever", {})
    for k in [k for k, v in ever.items() if v == today_iso]:
        del ever[k]
    return history


# ---------------------------------------------------------------------------
# Svenska artnamn via GBIF (deterministiskt uppslag – INGEN översättning av LLM)
# ---------------------------------------------------------------------------
def _load_sv_cache():
    if SV_NAMES_PATH.exists():
        try:
            return json.loads(SV_NAMES_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


def _save_sv_cache(cache):
    SV_NAMES_PATH.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _gbif_swedish_name(scientific):
    """Slå upp svenskt trivialnamn för ett vetenskapligt namn via GBIF.
    Föredrar poster märkta 'preferred'; annars det vanligaste namnet bland
    de svenska träffarna (skyddar mot enstaka udda stavningar). None om inget."""
    try:
        m = requests.get(
            "https://api.gbif.org/v1/species/match",
            params={"name": scientific}, timeout=20,
        )
        m.raise_for_status()
        key = m.json().get("usageKey")
        if not key:
            return None
        v = requests.get(
            f"https://api.gbif.org/v1/species/{key}/vernacularNames",
            params={"limit": 200}, timeout=20,
        )
        v.raise_for_status()
        rows = [
            r for r in v.json().get("results", [])
            if r.get("language") == "swe" and r.get("vernacularName")
        ]
        if not rows:
            return None

        # 1) Föredra en post som är märkt 'preferred'.
        for r in rows:
            if r.get("preferred"):
                return r["vernacularName"].strip().lower()

        # 2) Annars: rösta fram det vanligaste namnet (skyddar mot "hus-swala").
        from collections import Counter
        counts = Counter(r["vernacularName"].strip().lower() for r in rows)
        return counts.most_common(1)[0][0]
    except requests.RequestException:
        return None


def swedish_names_for(species):
    """Fyll i svenskt namn per art (via cache + GBIF). Muterar listan in-place."""
    cache = _load_sv_cache()
    changed = False
    for s in species:
        sci = s.get("scientific") or ""
        if not sci:
            continue
        if sci not in cache:
            cache[sci] = _gbif_swedish_name(sci) or ""   # "" = sökt men inget svenskt namn
            changed = True
        if cache[sci]:
            s["name_sv"] = cache[sci]
    if changed:
        _save_sv_cache(cache)


# ---------------------------------------------------------------------------
# 1. Fetch last night's data via the public GraphQL API (no token needed)
# ---------------------------------------------------------------------------
def fetch_birdweather():
    # topSpecies over the last 24h gives per-species counts for the day; we sum
    # them for the total and count the list for species richness. A high limit
    # makes sure we capture every species heard, not just the very top ones.
    # 24 timmar (ett helt dygn) bakåt från körningen. Vid morgonkörning (~06)
    # täcker fönstret gårdagens dag + kväll + natt + morgonens gryning fram till
    # körtid – alltså BÅDE dag- och nattfåglar. OBS: fönstret räknas rullande
    # bakåt från NÄR jobbet kör, inte från fasta klockslag eller kalenderdygn.
    # (Tidigare 8h ≈ natten; breddat till 24h-dygn 2026-07-24.)
    query = """
    query ($id: ID!) {
      station(id: $id) {
        id
        name
        topSpecies(limit: 200, period: {count: 24, unit: "hour"}) {
          count
          species {
            commonName
            scientificName
            imageUrl
          }
        }
      }
    }
    """
    r = requests.post(
        BW_GRAPHQL,
        json={"query": query, "variables": {"id": str(BW_STATION_ID)}},
        timeout=30,
    )
    r.raise_for_status()
    payload = r.json()
    if payload.get("errors"):
        raise RuntimeError(f"GraphQL errors: {payload['errors']}")

    station = (payload.get("data") or {}).get("station")
    if not station:
        raise RuntimeError(
            f"No public station found for id {BW_STATION_ID} "
            "(check the id exists and the station is public)."
        )

    rows = station.get("topSpecies") or []
    all_species = []
    for item in rows:
        sp = item.get("species") or {}
        all_species.append({
            "name": sp.get("commonName") or sp.get("scientificName") or "Okand art",
            "scientific": sp.get("scientificName") or "",
            "count": item.get("count") or 0,
        })
    all_species.sort(key=lambda x: x["count"], reverse=True)

    # Grov aktivitetsnivå. OBS: antal detektioner speglar hur mycket LJUD som
    # fångats (en pratsam individ ger hundratals), inte hur många fåglar eller
    # hur intressant arten är. Nivån används därför bara som svag färg i manuset,
    # aldrig som exakta siffror – och sällsynta arter (låga tal) filtreras ALDRIG
    # bort, eftersom de ofta är det intressantaste.
    max_count = all_species[0]["count"] if all_species else 0

    def activity(c):
        if max_count and c >= 0.5 * max_count:
            return "ofta hord"
        if max_count and c >= 0.15 * max_count:
            return "hordes en del"
        return "enstaka"

    for s in all_species:
        s["activity"] = activity(s["count"])

    # Fyll i korrekt svenskt namn per art (GBIF + cache). Deterministiskt –
    # ingen LLM-översättning. Arter utan svenskt namn behåller bara sci/engelskt.
    swedish_names_for(all_species)

    # Kanoniskt visningsnamn som används överallt nedströms (manus, signaler,
    # historik) så allt är konsekvent: svenskt namn först, annars vetenskapligt.
    for s in all_species:
        s["display"] = s.get("name_sv") or s.get("scientific") or s["name"]

    return {
        "date": TODAY.isoformat(),
        "station_name": station.get("name"),
        "total_detections": sum(s["count"] for s in all_species),  # internt, ej i manus
        "species_count": len(all_species),
        "top_species": all_species,   # ALLA arter – count används internt, ej i manus
    }


# ---------------------------------------------------------------------------
# 2. Derive continuity FACTS in Python
# ---------------------------------------------------------------------------
def derive_signals(today, history):
    species_ever = history.get("species_ever", {})
    recent       = history.get("recent_days", [])

    today_species = today["top_species"]
    today_keys    = [_sci_key(s) for s in today_species]
    # nyckel (vetenskapligt) -> svenskt visningsnamn (det som ska stå i manuset)
    display = {_sci_key(s): (s.get("display") or s["name"]) for s in today_species}

    # Helt nya arter (någonsin): saknas i species_ever, som är obegränsat
    # all-time-minne och därför oberoende av KEEP_HISTORY.
    new_keys = [k for k in today_keys if k not in species_ever]

    # Första för året: hörd tidigare, men inte tidigare i ÅR. Kräver dagsdata som
    # täcker hela året – därför hålls KEEP_HISTORY stort.
    year_prefix = f"{TODAY.year}-"
    seen_this_year = set()
    for day in recent:
        if day["date"].startswith(year_prefix):
            seen_this_year |= _day_keys(day)
    first_this_year = [
        k for k in today_keys if k not in new_keys and k not in seen_this_year
    ]

    # Återvändande: inte hörd de senaste RETURN_GAP dagarna.
    recently_seen = set()
    cutoff = (TODAY - dt.timedelta(days=RETURN_GAP)).isoformat()
    for day in recent:
        if day["date"] > cutoff:
            recently_seen |= _day_keys(day)
    returning = [
        k for k in today_keys
        if k not in new_keys and k not in first_this_year and k not in recently_seen
    ]

    yesterday = recent[-1] if recent else None
    vs_yesterday = None
    if yesterday:
        # Jämför ARTRIKEDOM (antal olika arter) – det är meningsfullt, till
        # skillnad från antal detektioner som mest speglar hur pratsamma
        # fåglarna var.
        vs_yesterday = {
            "artrikedom_igar": yesterday.get("species_count"),
            "artrikedom_idag": today["species_count"],
        }

    # --- Rikare, DATA-GRUNDADE expert-krokar. Allt nedan är RÄKNAT ur den
    # verkliga historiken – inga påståenden om beteende/väder/plats. Ger värdarna
    # konkreta, sanna detaljer att låta kunniga på ("efter 23 dagars tystnad",
    # "tredje dygnet i rad", "ett av de artrikaste dygnen hittills"). ---------
    by_date = {d["date"]: _day_keys(d) for d in recent}

    # Uppehållets längd i dagar för varje återvändande art (sedan senast hörd).
    returning_details = []
    for k in returning:
        prev_dates = [dstr for dstr, keys in by_date.items() if k in keys]
        if prev_dates:
            gap = (TODAY - dt.date.fromisoformat(max(prev_dates))).days
            returning_details.append({"art": display[k], "dagars_uppehall": gap})

    # Svit: hur många dagar i följd (inkl. innevarande dygn) arten hörts. Bara >=3 är värt
    # att nämna. Kräver att historiken faktiskt har posterna för mellandagarna –
    # ett missat dygn bryter sviten (ärligt: då vet vi inte att den var i rad).
    streaks = []
    for k in today_keys:
        n, d = 1, TODAY - dt.timedelta(days=1)
        while k in by_date.get(d.isoformat(), set()):
            n += 1
            d -= dt.timedelta(days=1)
        if n >= 3:
            streaks.append({"art": display[k], "dagar_i_rad": n})

    # Artrikedom i kontext: dagens antal arter mot tidigare rekord.
    prev_counts = [d.get("species_count", 0) for d in recent]
    rekord_tidigare = max(prev_counts) if prev_counts else None
    artrikedom_kontext = {
        "idag": today["species_count"],
        "rekord_tidigare": rekord_tidigare,
        "nytt_rekord": rekord_tidigare is not None
        and today["species_count"] > rekord_tidigare,
    }

    # Lokal ovanlighet från Artportalen: läses ur cache (inga nätanrop). Tyst
    # tom lista om modulen/cachen saknas eller något strular – ska ALDRIG kunna
    # fälla den dagliga körningen.
    lokal_kontext = []
    if artportalen is not None:
        try:
            lokal_kontext = artportalen.local_context(today_species)
        except Exception:
            lokal_kontext = []

    # Signalerna innehåller SVENSKA visningsnamn (inte de vetenskapliga nycklarna)
    # så prompten får rätt namn precis som förut.
    return {
        "new_species":         [display[k] for k in new_keys],
        "first_this_year":     [display[k] for k in first_this_year],
        "returning_after_gap": [display[k] for k in returning],
        "returning_details":   returning_details,
        "streaks":             streaks,
        "artrikedom_kontext":  artrikedom_kontext,
        "vs_yesterday":        vs_yesterday,
        "lokal_kontext":       lokal_kontext,
        "days_recorded":       len(recent) + 1,
        "total_species_ever":  len(species_ever) + len(new_keys),
    }


# ---------------------------------------------------------------------------
# 3. Generate a two-host DIALOGUE with Claude (returns list of turns)
# ---------------------------------------------------------------------------
# The wording/tone lives in an editable file (prompt.txt) so it can be tuned
# without touching this code. Lines starting with # there are treated as
# comments and stripped. Placeholders below are filled in at runtime.
PROMPT_PATH        = Path("prompt.txt")
PROMPT_DIALOG_PATH = Path("prompt_dialog.txt")   # används när TTS_PROVIDER=elevenlabs

# Minimal built-in fallback, only used if prompt.txt is missing.
# Edit prompt.txt, NOT this string.
DEFAULT_PROMPT = """Du skriver manus till en kort daglig morgonpodd om faglarna.
Tva programledare samtalar: {{HOST_A}} (nyfiken, varm) och {{HOST_B}} (lugn, kunnig).
DYGNETS DATA (JSON):
{{DATA_JSON}}
KONTINUITET – verifierade fakta, hitta inte pa egna:
{{SIGNALS_JSON}}
Skriv ett naturligt samtal pa svenska, ca 400 ord, avslappnat, korta repliker.
Returnera ENBART giltig JSON: lista av objekt med "speaker" ("{{HOST_A}}" eller
"{{HOST_B}}") och "text". Ingen markdown, ingen text utanfor listan."""


def _script_view(today):
    # Vad modellen faktiskt får se. Medvetet UTAN råa antal/totaler. När ett
    # svenskt namn finns (från GBIF) ges det som "art" och modellen ska bara
    # anvanda det – ingen oversattning. Saknas svenskt namn ges vetenskapligt
    # som fallback.
    arter = []
    for s in today.get("top_species", []):
        arter.append({
            "art": s.get("display") or s["name"],
            "har_svenskt_namn": bool(s.get("name_sv")),
            "vetenskapligt": s.get("scientific", ""),
            "aktivitet": s.get("activity", "enstaka"),
        })
    return {
        "datum": today["date"],
        "artrikedom": today.get("species_count"),
        "arter": arter,
    }


def build_prompt(today, signals):
    # I ElevenLabs-läge används den samtalsanpassade dialog-prompten om den finns;
    # annars faller vi tillbaka på den vanliga prompten (och sist på inbyggd default).
    if TTS_PROVIDER == "elevenlabs" and PROMPT_DIALOG_PATH.exists():
        raw = PROMPT_DIALOG_PATH.read_text(encoding="utf-8")
    elif PROMPT_PATH.exists():
        raw = PROMPT_PATH.read_text(encoding="utf-8")
    else:
        print("  (ingen promptfil hittad – använder inbyggd standardprompt)")
        raw = DEFAULT_PROMPT

    # Drop comment lines (those starting with #) so they aren't sent to the model.
    lines = [ln for ln in raw.splitlines() if not ln.lstrip().startswith("#")]
    template = "\n".join(lines).strip()

    for marker in ("{{DATA_JSON}}", "{{SIGNALS_JSON}}"):
        if marker not in template:
            print(f"  VARNING: platshållaren {marker} saknas i promptfilen")

    return (
        template
        .replace("{{HOST_A}}", HOST_A)
        .replace("{{HOST_B}}", HOST_B)
        .replace("{{PODD_NAMN}}", PODCAST_TITLE)
        .replace("{{DATA_JSON}}", json.dumps(_script_view(today), ensure_ascii=False, indent=2))
        .replace("{{SIGNALS_JSON}}", json.dumps(signals, ensure_ascii=False, indent=2))
    )


def write_dialogue(today, signals):
    prompt = build_prompt(today, signals)

    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": CLAUDE_MODEL,
            "max_tokens": 2000,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=90,
    )
    r.raise_for_status()
    text = "".join(
        b["text"] for b in r.json()["content"] if b.get("type") == "text"
    ).strip()

    # Strip accidental code fences, then parse.
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("[") :]
    try:
        turns = json.loads(text[text.find("[") : text.rfind("]") + 1])
        turns = [t for t in turns if t.get("text")]
        if turns:
            return turns
    except (json.JSONDecodeError, ValueError):
        pass
    # Fallback: if parsing failed, deliver the whole thing as one host's monologue.
    return [{"speaker": HOST_A, "text": text}]


# ---------------------------------------------------------------------------
# 4. Text-to-speech, per speaker, then stitch with ffmpeg
# ---------------------------------------------------------------------------
def voice_for(speaker):
    if TTS_PROVIDER == "elevenlabs":
        a = os.environ["ELEVENLABS_VOICE_A"]
        b = os.environ["ELEVENLABS_VOICE_B"]
    else:
        a = os.environ.get("OPENAI_VOICE_A", "nova")
        b = os.environ.get("OPENAI_VOICE_B", "onyx")
    return a if speaker == HOST_A else b


def tts_segment(text, voice, out_path):
    if TTS_PROVIDER == "elevenlabs":
        r = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice}",
            headers={
                "xi-api-key": os.environ["ELEVENLABS_API_KEY"],
                "content-type": "application/json",
                "accept": "audio/mpeg",
            },
            json={
                "text": text,
                "model_id": "eleven_multilingual_v2",
                "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
            },
            timeout=120,
        )
    else:
        model = os.environ.get("OPENAI_TTS_MODEL", "gpt-4o-mini-tts")
        body = {
            "model": model,
            "voice": voice,
            "input": text,
            "response_format": "mp3",
        }
        # Ton-styrning (instructions) stöds bara av gpt-4o-mini-tts,
        # inte av de äldre tts-1 / tts-1-hd.
        if "mini-tts" in model or "gpt-4o" in model:
            body["instructions"] = (
                "Tala som en levande, varm radiopratare pa svenska: naturligt "
                "tempo med sma pauser, tydlig men avslappnad intonation och lite "
                "variation i tonfallet. Lat engagerad och samtalande, aldrig "
                "monoton eller upplasande."
            )
        r = requests.post(
            "https://api.openai.com/v1/audio/speech",
            headers={
                "Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=120,
        )
    r.raise_for_status()
    out_path.write_bytes(r.content)


def make_silence(path, seconds=0.35):
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i",
         "anullsrc=channel_layout=mono:sample_rate=44100",
         "-t", str(seconds), "-q:a", "9", str(path)],
        check=True, capture_output=True,
    )


def stitch(segment_paths, out_path):
    """Concatenate mp3 segments, normalising sample rate/channels so mixed
    inputs (and the silence clip) always join cleanly."""
    cmd = ["ffmpeg", "-y"]
    for p in segment_paths:
        cmd += ["-i", str(p)]
    n = len(segment_paths)
    pre = ";".join(
        f"[{i}:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=mono[a{i}]"
        for i in range(n)
    )
    labels = "".join(f"[a{i}]" for i in range(n))
    filt = f"{pre};{labels}concat=n={n}:v=0:a=1[out]"
    cmd += ["-filter_complex", filt, "-map", "[out]",
            "-c:a", "libmp3lame", "-q:a", "4", str(out_path)]
    subprocess.run(cmd, check=True, capture_output=True)


def synthesize_openai_dialogue(turns, out_path):
    """OpenAI: en TTS-snutt per replik, ihopsydda med ffmpeg (per-replik)."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        silence = tmp / "silence.mp3"
        make_silence(silence)

        segments = []
        for i, turn in enumerate(turns):
            seg = tmp / f"seg_{i:03d}.mp3"
            tts_segment(turn["text"], voice_for(turn.get("speaker", HOST_A)), seg)
            segments.append(seg)
            if i < len(turns) - 1:
                segments.append(silence)   # small gap between speakers

        stitch(segments, out_path)


# --- ElevenLabs v3 Text-to-Dialogue: hela samtalet vävs i ett svep ---
EL_DIALOGUE_URL = "https://api.elevenlabs.io/v1/text-to-dialogue"
EL_MODEL        = "eleven_v3"
EL_MAX_CHARS    = 1900   # v3-gräns är 2000/anrop; håll marginal

# Efterbehandling: korta ner de långa pauserna som text-to-dialogue lägger vid
# talarbyten. silenceremove behåller EL_PAUSE_KEEP sekunder tystnad och klipper
# bort överskottet. Justera via env om det blir för aggressivt/för milt.
EL_TRIM_PAUSES  = os.environ.get("EL_TRIM_PAUSES", "1") not in ("0", "false", "no")
EL_PAUSE_KEEP   = os.environ.get("EL_PAUSE_KEEP", "0.5")    # sekunder att behålla
EL_PAUSE_THRESH = os.environ.get("EL_PAUSE_THRESH", "-40dB")  # tystnadströskel

# Normalisering: jämnar ut volymskillnaden MELLAN rösterna (dynaudnorm justerar
# nivån dynamiskt över tid), så de inte låter som olika inspelningar. Dämpar
# "olika rum"-känslan – men bara volymdelen, inte rumsklang/timbre.
EL_NORMALIZE    = os.environ.get("EL_NORMALIZE", "1") not in ("0", "false", "no")


def _run_ffmpeg_filter(path, filt, label):
    """Kör ett ffmpeg-ljudfilter in-place. Misslyckas det behålls originalet."""
    tmp = path.with_name(path.stem + "_f.mp3")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(path), "-af", filt,
             "-c:a", "libmp3lame", "-q:a", "4", str(tmp)],
            check=True, capture_output=True,
        )
        tmp.replace(path)
        return True
    except subprocess.CalledProcessError as e:
        print(f"  ({label} hoppades över: {e.stderr.decode(errors='ignore')[:200]})",
              file=sys.stderr)
        if tmp.exists():
            tmp.unlink()
        return False


def _trim_pauses(path):
    """Korta ner långa tystnader (talarbytes-pauser) till ~EL_PAUSE_KEEP sek."""
    filt = (
        f"silenceremove=stop_periods=-1:"
        f"stop_duration={EL_PAUSE_KEEP}:stop_threshold={EL_PAUSE_THRESH}"
    )
    _run_ffmpeg_filter(path, filt, "pausklippning")


def _normalize(path):
    """Jämna ut volym mellan rösterna så de inte låter som olika inspelningar.
    m=5 begränsar hur mycket tysta partier lyfts (undviker att brus pumpas upp)."""
    _run_ffmpeg_filter(path, "dynaudnorm=f=500:g=31:m=5:p=0.95", "normalisering")


def _el_inputs(turns):
    return [
        {"text": t["text"], "voice_id": voice_for(t.get("speaker", HOST_A))}
        for t in turns
    ]


def _el_chunks(turns, limit=EL_MAX_CHARS):
    """Dela dialogen i grupper vars sammanlagda text håller sig under gränsen."""
    chunks, cur, count = [], [], 0
    for t in turns:
        n = len(t["text"])
        if cur and count + n > limit:
            chunks.append(cur)
            cur, count = [], 0
        cur.append(t)
        count += n
    if cur:
        chunks.append(cur)
    return chunks


def _el_call(turns_chunk, out_path):
    r = requests.post(
        EL_DIALOGUE_URL,
        headers={
            "xi-api-key": os.environ["ELEVENLABS_API_KEY"],
            "Content-Type": "application/json",
        },
        json={
            "inputs": _el_inputs(turns_chunk),
            "model_id": EL_MODEL,
            "language_code": "sv",
        },
        timeout=180,
    )
    if r.status_code != 200:
        print(f"  ElevenLabs {r.status_code}: {r.text[:400]}", file=sys.stderr)
    r.raise_for_status()
    out_path.write_bytes(r.content)


def synthesize_elevenlabs_dialogue(turns, out_path):
    """ElevenLabs v3: skicka hela dialogen (chunkad vid behov) till
    text-to-dialogue, så rösterna delar kontext och flödet blir naturligt.
    Kortar sedan ner de långa talarbytes-pauserna (om EL_TRIM_PAUSES)."""
    chunks = _el_chunks(turns)
    if len(chunks) == 1:
        _el_call(chunks[0], out_path)
    else:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            parts = []
            for i, ch in enumerate(chunks):
                p = tmp / f"part_{i:02d}.mp3"
                _el_call(ch, p)
                parts.append(p)
            stitch(parts, out_path)

    if EL_TRIM_PAUSES:
        _trim_pauses(out_path)
        print(f"  pauser nedkortade (behåller ~{EL_PAUSE_KEEP}s)")
    if EL_NORMALIZE:
        _normalize(out_path)
        print("  röster normaliserade (utjämnad volym)")


def synthesize_dialogue(turns, out_path):
    if TTS_PROVIDER == "elevenlabs":
        synthesize_elevenlabs_dialogue(turns, out_path)
    else:
        synthesize_openai_dialogue(turns, out_path)


# ---------------------------------------------------------------------------
# 5. Feed + landing page (self-healing from episodes on disk)
# ---------------------------------------------------------------------------
def episodes_on_disk():
    EPISODES_DIR.mkdir(parents=True, exist_ok=True)
    mp3s = sorted(EPISODES_DIR.glob("*.mp3"), reverse=True)
    for old in mp3s[KEEP_EPISODES:]:
        old.unlink()
        old.with_suffix(".txt").unlink(missing_ok=True)              # ta även manuset
        old.with_name(f"{old.stem}.data.txt").unlink(missing_ok=True)  # och rådatan
    return mp3s[:KEEP_EPISODES]


def build_feed(mp3s):
    items = []
    for mp3 in mp3s:
        date_str = mp3.stem
        try:
            pub = dt.datetime.fromisoformat(date_str).replace(hour=6, tzinfo=dt.timezone.utc)
        except ValueError:
            pub = dt.datetime.now(dt.timezone.utc)
        url = f"{SITE_BASE_URL}/episodes/{mp3.name}"
        items.append(f"""    <item>
      <title>{escape(PODCAST_TITLE)} – {escape(date_str)}</title>
      <guid isPermaLink="false">{escape(url)}</guid>
      <pubDate>{format_datetime(pub)}</pubDate>
      <enclosure url="{escape(url)}" length="{mp3.stat().st_size}" type="audio/mpeg"/>
    </item>""")

    cover_url = f"{SITE_BASE_URL}/{COVER_FILE}"
    FEED_PATH.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>{escape(PODCAST_TITLE)}</title>
    <link>{escape(SITE_BASE_URL)}</link>
    <language>{PODCAST_LANG}</language>
    <description>{escape(PODCAST_DESC)}</description>
    <itunes:author>{escape(PODCAST_AUTHOR)}</itunes:author>
    <itunes:explicit>false</itunes:explicit>
    <itunes:image href="{escape(cover_url)}"/>
    <image>
      <url>{escape(cover_url)}</url>
      <title>{escape(PODCAST_TITLE)}</title>
      <link>{escape(SITE_BASE_URL)}</link>
    </image>
{chr(10).join(items)}
  </channel>
</rss>
""", encoding="utf-8")


TEMPLATE_PATH = Path("template.html")

# Minimal fallback om template.html saknas. Redigera template.html, inte denna.
FALLBACK_TEMPLATE = """<!doctype html><html lang="sv"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>%%TITLE%%</title>
</head><body><h1>%%TITLE%%</h1><p>%%DESC%%</p>
<p><a href="%%FEED_URL%%">RSS</a> · <a href="%%APPLE_URL%%">Apple Podcasts</a>
· <a href="%%BIRDTUNES_URL%%">Stationens data</a></p>
<div>%%LATEST%%</div><h2>Tidigare avsnitt</h2><ul>%%ROWS%%</ul>
<footer><p>Lokal artdata från Artportalen (SLU Artdatabanken), använd enligt
<a href="https://www.slu.se/artdatabanken/rapportering-och-fynd/oppna-data-och-apier/api-villkor/" rel="nofollow">deras API-villkor</a>.
Sidan drivs inte av och representerar inte SLU.</p></footer></body></html>"""


def build_index(mp3s):
    feed_url = f"{SITE_BASE_URL}/feed.xml"
    apple_url = "podcast://" + feed_url.split("://", 1)[1]
    cover_url = f"{SITE_BASE_URL}/{COVER_FILE}"
    birdtunes_url = f"https://birdtunes.net/?station={BW_STATION_ID}&lang=sv"

    def episode_li(mp3):
        url = f"{SITE_BASE_URL}/episodes/{mp3.name}"
        txt = f"{SITE_BASE_URL}/episodes/{mp3.stem}.txt"
        return (
            f'    <li class="ep">\n'
            f'      <span class="ep-date">{escape(mp3.stem)}</span>\n'
            f'      <audio controls preload="none" src="{escape(url)}"></audio>\n'
            f'      <a class="manus" href="{escape(txt)}">Visa manus</a>\n'
            f'    </li>'
        )

    if mp3s:
        latest = mp3s[0]
        latest_url = f"{SITE_BASE_URL}/episodes/{latest.name}"
        latest_txt = f"{SITE_BASE_URL}/episodes/{latest.stem}.txt"
        latest_html = (
            f'<span class="eyebrow">Senaste avsnittet</span>\n'
            f'      <span class="latest-date">{escape(latest.stem)}</span>\n'
            f'      <audio class="latest-audio" controls preload="auto" src="{escape(latest_url)}"></audio>\n'
            f'      <a class="manus" href="{escape(latest_txt)}">Visa manus</a>'
        )
        older = mp3s[1:]
        rows = "\n".join(episode_li(m) for m in older) or \
            '    <li class="ep empty">Fler avsnitt dyker upp här.</li>'
    else:
        latest_html = '<span class="eyebrow">Snart</span>\n      <p>Första avsnittet är på väg.</p>'
        rows = '    <li class="ep empty">Inga avsnitt än.</li>'

    if TEMPLATE_PATH.exists():
        template = TEMPLATE_PATH.read_text(encoding="utf-8")
    else:
        print("  (template.html saknas – använder inbyggd fallback-mall)")
        template = FALLBACK_TEMPLATE

    html = (
        template
        .replace("%%TITLE%%", escape(PODCAST_TITLE))
        .replace("%%DESC%%", escape(PODCAST_DESC))
        .replace("%%COVER_URL%%", escape(cover_url))
        .replace("%%FEED_URL%%", escape(feed_url))
        .replace("%%APPLE_URL%%", escape(apple_url))
        .replace("%%BIRDTUNES_URL%%", escape(birdtunes_url))
        .replace("%%LATEST%%", latest_html)
        .replace("%%ROWS%%", rows)
    )
    INDEX_PATH.write_text(html, encoding="utf-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    history = load_history()
    migrate_history(history)                  # uppgradera ev. gammalt (display-nycklat) schema
    reset_today(history, TODAY.isoformat())   # rensa ev. omkörning samma dag

    print("Fetching BirdWeather data...")
    today = fetch_birdweather()
    print(f"  {today['total_detections']} detections, {today['species_count']} species")

    signals = derive_signals(today, history)
    if signals["new_species"]:
        print(f"  NEW species today: {', '.join(signals['new_species'])}")

    print(f"Writing dialogue with Claude ({CLAUDE_MODEL})...")
    turns = write_dialogue(today, signals)
    print(f"  {len(turns)} lines of dialogue")

    EPISODES_DIR.mkdir(parents=True, exist_ok=True)
    out_path = EPISODES_DIR / f"{today['date']}.mp3"

    # Spara manuset som läsbar text bredvid ljudet, för granskning/feedback.
    # Ev. v3-audio-taggar ([warmly] osv.) strippas så transkriptionen blir ren.
    def _clean(s):
        return re.sub(r"\s{2,}", " ", re.sub(r"\[[^\]]*\]", "", s)).strip()
    script_path = EPISODES_DIR / f"{today['date']}.txt"
    script_text = "\n\n".join(
        f"{t.get('speaker', HOST_A)}: {_clean(t.get('text', ''))}" for t in turns
    )
    script_path.write_text(script_text, encoding="utf-8")
    print(f"  sparade manus: {script_path}")

    # Spara EXAKT de arter som hämtades från API:t, så manuset kan verifieras
    # mot faktisk data (för att skilja hallucination från vy-/tidsfönster-skillnad).
    data_path = EPISODES_DIR / f"{today['date']}.data.txt"
    data_lines = [
        f"Hämtat {today['date']} – fönster: senaste dygnet (24 timmar)",
        f"Station: {today.get('station_name')}",
        f"Artrikedom: {today['species_count']}",
        "",
        "Arter i datan (namn + aktivitet):",
    ]
    data_lines += [
        f"  - {s.get('display') or s['name']}  [{s.get('scientific','?')}]  ({s.get('activity', '?')})"
        for s in today["top_species"]
    ]
    data_path.write_text("\n".join(data_lines), encoding="utf-8")
    print(f"  sparade rådata: {data_path}")

    if SKIP_TTS:
        # Persona-/prompt-iteration: manus klart, ingen röst, ingen mp3, och vi
        # rör INTE historik/feed/sida (så experiment inte förorenar minnet eller
        # publicerar ett avsnitt utan ljud). Skriv ut manuset direkt för snabb läsning.
        print("\n** SKIP_TTS: hoppar över röstläggning, historik och feed/sida. **")
        print(f"** Manus sparat: {script_path} **\n")
        print(script_text)
        return

    print(f"Synthesizing two-host audio via {TTS_PROVIDER}...")
    synthesize_dialogue(turns, out_path)
    print(f"  wrote {out_path} ({out_path.stat().st_size // 1024} KB)")

    # Update history. Spara ALLA arter så att sällsynta arter – som ofta har lågt
    # antal – registreras korrekt för "nytt/första för året/återvändande". Nyckeln
    # är VETENSKAPLIGT namn (stabilt); svenska visningsnamnet sparas bredvid för
    # läsbarhet. Råa antal sparas inte; de är inte meningsfulla.
    species_ever = history.setdefault("species_ever", {})
    for s in today["top_species"]:
        species_ever.setdefault(_sci_key(s), today["date"])
    history.setdefault("recent_days", []).append({
        "date": today["date"],
        "species_count": today["species_count"],
        "top": [
            {"sci": _sci_key(s), "name": s.get("display") or s["name"]}
            for s in today["top_species"]
        ],
    })
    history["recent_days"] = history["recent_days"][-KEEP_HISTORY:]
    save_history(history)

    print("Rebuilding feed + landing page...")
    mp3s = episodes_on_disk()
    build_feed(mp3s)
    build_index(mp3s)
    print("Done.")


if __name__ == "__main__":
    try:
        main()
    except requests.HTTPError as e:
        print(f"HTTP error: {e}\n{getattr(e.response, 'text', '')}", file=sys.stderr)
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        print(f"ffmpeg error: {e.stderr.decode(errors='ignore')}", file=sys.stderr)
        sys.exit(1)
