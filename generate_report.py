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

# Verifierad artfakta (AVONET + Wikidata + rödlista). Valfri på samma sätt: saknas
# modulen eller species_facts.json säger värdarna helt enkelt inget om arterna.
try:
    import species_facts
except Exception:
    species_facts = None

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
# Rullande minneslapp över vilka artfakta som använts per dygn. Ersätter det gamla
# upplägget där hela tidigare manus matades in i prompten – se recent_openings().
FACT_LOG_PATH = (Path(_TEST_DIR) / "fakta_logg.json") if _TEST_DIR else Path("fakta_logg.json")
FEED_PATH    = DOCS_DIR / "feed.xml"
INDEX_PATH   = DOCS_DIR / "index.html"
SV_NAMES_PATH = Path("species_sv.json")   # cache: vetenskapligt namn -> svenskt namn
NOTIS_PATH    = Path("dagens_notis.txt")  # frivillig tillfällig notis (shoutout m.m.); tom = ingen notis

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
# ---------------------------------------------------------------------------
# Artrikedomens signifikans (kalibrerat mot verklig historik 2026-07-30)
#
# 19 dygns data gav median 19 arter, min 4, max 26. De två lägsta dygnen
# (2026-07-20/21: 6 och 4 arter) är sannolikt stationshaverier, inte tysta dygn –
# därför median och inte medelvärde, så de inte drar ner baslinjen.
#
# Normal dygnsvariation är ca ±3 arter. Allt inom det är BRUS och ska inte bli en
# poäng i manuset. Överstyrbart via env om historiken växer och spridningen ändras.
# ---------------------------------------------------------------------------
NOISE_ARTER     = int(os.environ.get("NOISE_ARTER", "3"))      # ± inom detta = brus
MAX_STREAKS     = int(os.environ.get("MAX_STREAKS", "3"))      # antal sviter som skickas in
AVBRUTEN_MIN    = int(os.environ.get("AVBRUTEN_MIN", "5"))     # svitlängd för att en tystnad ska räknas
MAX_AVBRUTNA    = int(os.environ.get("MAX_AVBRUTNA", "2"))     # antal avbrutna sviter som skickas in

# Veckodag på svenska. Räknas ut i koden – se _datumfras().
VECKODAGAR = {0: "måndag", 1: "tisdag", 2: "onsdag", 3: "torsdag",
              4: "fredag", 5: "lördag", 6: "söndag"}
REKORD_MARGINAL = int(os.environ.get("REKORD_MARGINAL", "3"))  # krävs för "nytt rekord"
OMDOME_MIN_DAGAR = int(os.environ.get("OMDOME_MIN_DAGAR", "7"))  # minsta underlag


def _median(values):
    v = sorted(values)
    if not v:
        return None
    mid = len(v) // 2
    return v[mid] if len(v) % 2 else (v[mid - 1] + v[mid]) / 2


def artrikedom_omdome(idag, prev_counts):
    """Kvalitativt omdöme om dygnets artrikedom mot medianen av tidigare dygn.

    Ersätter råa jämförelser ("fem fler än igår", "nytt rekord med en art"), som
    fick värdarna att dramatisera normal variation. Returnerar None när dygnet är
    normalt ELLER när underlaget är för tunt – då säger prompten inget alls.

    Bara "ovanligt"-nivåerna är värda att nämna; "artrikt"/"magert" finns med för
    att koden ska kunna skilja dem, men prompten tonar ner dem."""
    prev = [c for c in prev_counts if c]
    if len(prev) < OMDOME_MIN_DAGAR or not idag:
        return None
    med = _median(prev)
    if med is None:
        return None
    diff = idag - med
    if diff >= 2 * NOISE_ARTER:
        return "ovanligt artrikt"
    if diff > NOISE_ARTER:
        return "artrikt"
    if diff <= -2 * NOISE_ARTER:
        return "ovanligt magert"
    if diff < -NOISE_ARTER:
        return "magert"
    return None          # normalt dygn – ingen notering


def _artrikedom_mening(omdome, diff):
    """En färdig, motsägelsefri mening om dygnets artrikedom.

    `omdome` är nivån mot historikens median, `diff` skillnaden mot igår (None om
    okänd). Nivån väger tyngre än gårdagen: en enskild dag är brusigare än en median
    över tjugo. Är dygnet normalt OCH skillnaden inom bruset returneras None – då
    finns inget att säga, och prompten säger inget.

    Meningarna är skrivna för att kunna sägas rakt ut, så modellen inte behöver väga
    två fält mot varandra (vilket gav "klart färre än igår – men artrikt ändå")."""
    stort_skifte = diff is not None and abs(diff) > NOISE_ARTER
    riktning = "fler" if (diff or 0) > 0 else "färre"

    if omdome in ("ovanligt artrikt", "artrikt"):
        if stort_skifte and riktning == "färre":
            # Kärnfallet som gav motsägelsen: högt mot historiken, ned mot igår.
            return "fortsatt högt i artrikedom, om än en bit under igår"
        if stort_skifte:
            return f"{omdome}, och tydligt fler arter än igår"
        return omdome
    if omdome in ("ovanligt magert", "magert"):
        if stort_skifte and riktning == "fler":
            return "fortfarande i det magrare spannet, men fler arter än igår"
        if stort_skifte:
            return f"{omdome}, och tydligt färre arter än igår"
        return omdome
    # Normal nivå mot historiken.
    if stort_skifte:
        return f"ungefär som vanligt i nivå, men tydligt {riktning} arter än igår"
    return None


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
        #
        # HINKAT 2026-07-30: skillnader inom ±NOISE_ARTER är normal dygnsvariation
        # och ska INTE bli en poäng i manuset. Förut gav vi två råa tal och
        # modellen räknade differensen själv ("fem fler än igår"), vilket lät som
        # en händelse även när det var brus.
        igar = yesterday.get("species_count")
        vs_yesterday = {"artrikedom_igar": igar, "artrikedom_idag": today["species_count"]}
        if igar:
            diff = today["species_count"] - igar
            if abs(diff) <= NOISE_ARTER:
                vs_yesterday["forandring"] = "i nivå med igår"
            else:
                vs_yesterday["forandring"] = ("klart fler arter än igår" if diff > 0
                                              else "klart färre arter än igår")

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

    # TOPPA LISTAN. Prompten hade ett tak på två sviter per avsnitt, men det
    # ignorerades två körningar i rad (2026-07-30/31: "nittonde ... tjugonde ...
    # tjugonde ... sjätte"). Kan modellen se sjutton sviter radar den upp dem, så
    # taket flyttas hit: bara de längsta skickas in. En regel som inte följs två
    # gånger hör inte i en prompt.
    streaks.sort(key=lambda s: s["dagar_i_rad"], reverse=True)
    streaks = streaks[:MAX_STREAKS]

    # AVBRUTNA SVITER (nytt 2026-07-31). En art som hörts länge i följd och sedan
    # tystnar är minst lika intressant som en som fortsätter – och det var material
    # frasverket saknade helt, vilket bidrog till att punktlistan blev tunn och
    # modellen fyllde ut med omdömen.
    #
    # FORMULERAS SOM UTEBLIVEN NOTERING, aldrig som frånvaro eller flytt. Mikrofonen
    # vet vad som hörs, inte var fåglarna tog vägen (samma princip som i beslutet om
    # årstidsfältet).
    # NAMNEN TAS UR species_sv.json, ALDRIG UR HISTORIKEN. Historiken bär kvar 33
    # ENGELSKA namn från tiden före GBIF-fixen ("Eurasian Linnet", "Gray Heron") –
    # de skulle gå rakt ut i sändning. Finns inget svenskt namn utelämnas arten
    # hellre än att nämnas på engelska. (Upptäckt 2026-07-31 när avbrutna sviter
    # byggdes; gäller varje framtida funktion som läser namn ur historiken.)
    namn_ur_historik = _load_sv_cache()

    avbrutna_sviter = []
    igar_iso = (TODAY - dt.timedelta(days=1)).isoformat()
    for k in by_date.get(igar_iso, set()):
        if k in today_keys or not namn_ur_historik.get(k):
            continue
        n, d = 0, TODAY - dt.timedelta(days=1)
        while k in by_date.get(d.isoformat(), set()):
            n += 1
            d -= dt.timedelta(days=1)
        if n >= AVBRUTEN_MIN:
            avbrutna_sviter.append({"art": namn_ur_historik[k], "dagar_i_rad": n})
    avbrutna_sviter.sort(key=lambda s: s["dagar_i_rad"], reverse=True)
    avbrutna_sviter = avbrutna_sviter[:MAX_AVBRUTNA]

    # Artrikedom i kontext.
    #
    # OMBYGGD 2026-07-30. Den gamla versionen satte nytt_rekord=True så snart
    # dygnet slog det gamla med EN art. På 19 dygns historik utlöste det fyra
    # gånger – var femte dygn – och tre av dem med marginalen +1 eller +2.
    # Prompten sa "lyft det", modellen gjorde som den blev tillsagd, och avsnitten
    # blev exalterade över brus ("tjugosju arter, ett kliv uppåt, fem fler än
    # igår!"). Felet låg i koden, inte i tonen: den lämnade över en boolean som
    # överdrev signifikansen.
    #
    # Normalvariationen mellan dygn är ca ±3 arter (median 19, spridning ~3 när de
    # två haverdygnen 2026-07-20/21 med 4 och 6 arter räknas bort). Därför:
    #   - rekord kräver MARGINAL, annars är det inte en händelse
    #   - ett kvalitativt omdöme ersätter råa jämförelser
    #   - median, inte medelvärde, så haverdygn inte drar ner baslinjen
    prev_counts = [c for c in (d.get("species_count", 0) for d in recent) if c]
    idag = today["species_count"]
    rekord_tidigare = max(prev_counts) if prev_counts else None

    artrikedom_kontext = {
        "idag": idag,
        "rekord_tidigare": rekord_tidigare,
        # Rekord bara med marginal. 27 mot 26 är inte ett rekord, det är brus.
        "nytt_rekord": (rekord_tidigare is not None
                        and idag >= rekord_tidigare + REKORD_MARGINAL),
    }

    # ETT SAMMANHÄNGANDE OMDÖME, inte två som kan krocka.
    #
    # Förut skickades `omdome` (mot medianen) och `forandring` (mot igår) som
    # separata fält, utan att modellen fick veta hur de hänger ihop. Resultatet
    # 2026-07-31: "ett artrikt sådant ... klart färre än igår – men artrikt är det
    # ändå". Båda påståendena var sanna men lästes som en motsägelse.
    #
    # Nu vägs de samman till EN mening som modellen kan säga rakt av. Nivån mot
    # historiken väger tyngre än gårdagen, eftersom en enskild dag är brusigare än
    # en median över tjugo.
    omdome = artrikedom_omdome(idag, prev_counts)
    igar_c = yesterday.get("species_count") if yesterday else None
    diff = (idag - igar_c) if (igar_c and idag) else None
    if omdome:
        artrikedom_kontext["omdome"] = omdome
    samlat = _artrikedom_mening(omdome, diff)
    if samlat:
        artrikedom_kontext["sammanfattning"] = samlat

    # Lokal ovanlighet från Artportalen: läses ur cache (inga nätanrop). Tyst
    # tom lista om modulen/cachen saknas eller något strular – ska ALDRIG kunna
    # fälla den dagliga körningen.
    lokal_kontext = []
    if artportalen is not None:
        try:
            lokal_kontext = artportalen.local_context(today_species)
        except Exception:
            lokal_kontext = []

    # Verifierad artfakta: läses ur species_facts.json (inga nätanrop). Ersätter
    # modellens eget minne som källa till artkunskap – prompten får bara påstå det
    # som står här. Tyst tom lista om modulen/cachen saknas.
    artfakta = []
    artfakta_jamforelser = {}
    if species_facts is not None:
        try:
            artfakta = species_facts.facts_for(today_species)
        except Exception:
            artfakta = []
        try:
            # Härledda jämförelser över dygnets artlista (minsta/tyngsta art,
            # andel flyttfåglar). Sanna per konstruktion – uträknade, inte hämtade.
            artfakta_jamforelser = species_facts.comparisons(today_species)
        except Exception:
            artfakta_jamforelser = {}

    # Signalerna innehåller SVENSKA visningsnamn (inte de vetenskapliga nycklarna)
    # så prompten får rätt namn precis som förut.
    return {
        "new_species":         [display[k] for k in new_keys],
        "first_this_year":     [display[k] for k in first_this_year],
        "returning_after_gap": [display[k] for k in returning],
        "returning_details":   returning_details,
        "streaks":             streaks,
        "avbrutna_sviter":     avbrutna_sviter,
        "artrikedom_kontext":  artrikedom_kontext,
        "vs_yesterday":        vs_yesterday,
        "lokal_kontext":       lokal_kontext,
        # Jämförelserna hör hit (härledda sanningar om just det här dygnet, som
        # streaks och artrikedom_kontext). Artfaktan själv är tidlös och plockas ut
        # av frasverket (build_facts) i stället.
        "artfakta_jamforelser": artfakta_jamforelser,
        "artfakta":            artfakta,
        # OMDÖPTA 2026-07-31. Hette "days_recorded" och "total_species_ever" och
        # förväxlades med varandra och med dygnets artantal: en körning sa
        # "tjugotredje dygnet" (fel, det var 21) och sedan "tjugotredje arten ... var
        # blåmesen". Namnen säger nu vad talen ÄR, på svenska som resten av signalerna.
        "antal_dygn_vi_spelat_in":   len(recent) + 1,
        "antal_arter_nagonsin_horda": len(species_ever) + len(new_keys),
    }


# ---------------------------------------------------------------------------
# 2b. FRASVERKET – gör derive_signals råa tal till färdiga svenska fraser
#
# Detta lager finns för att modellen aldrig ska se ett tal. Bakgrunden (2026-07-31):
# prompten fick nio tal i signalerna och sa "tjugotredje dygnet" – 23 fanns inte
# bland dem. Den missbrukade inte datan, den hittade på. Ett anrop som får talet 10
# och ska skriva "tionde dygnet i rad" kan lika gärna skriva "tjugotredje"; ett
# anrop som får frasen "tio dygn i rad" har ingenting att räkna på.
#
# KONTRAKT (verifieras av test_faktapunkter.py mot hela den verkliga historiken):
#   1. Ingen punkttext innehåller en SIFFRA.
#   2. Ingen punkttext innehåller ett INTERNT FÄLTNAMN. ("Den klöv i dygnet utan
#      streak-historik heller" gick ut i sändning 2026-07-31.)
#   3. Varje talord i en punkttext kommer ur tabellerna nedan – vilket gör den
#      tillåtna talordsmängden för dygnet KÄND, och därmed manusvalideringen
#      (lager 1) bevisande i stället för heuristisk.
#
# PUNKTERNA ÄR TELEGRAM, INTE MENINGAR. "tornseglare – tio dygn i rad", aldrig
# "Tornseglaren har hörts tio dygn i rad nu". En färdig mening blir uppläst rakt
# av; ett telegram måste skrivas om. Det är den enskilt viktigaste detaljen i hela
# upplägget – den är vad som håller avsnitten från att bli platta.
# ---------------------------------------------------------------------------

RAKNEORD = {
    1: "ett", 2: "två", 3: "tre", 4: "fyra", 5: "fem", 6: "sex", 7: "sju",
    8: "åtta", 9: "nio", 10: "tio", 11: "elva", 12: "tolv", 13: "tretton",
    14: "fjorton", 15: "femton", 16: "sexton", 17: "sjutton", 18: "arton",
    19: "nitton", 20: "tjugo", 21: "tjugoen", 22: "tjugotvå", 23: "tjugotre",
    24: "tjugofyra", 25: "tjugofem", 26: "tjugosex", 27: "tjugosju",
    28: "tjugoåtta", 29: "tjugonio", 30: "trettio", 31: "trettioen",
    32: "trettiotvå", 33: "trettiotre", 34: "trettiofyra", 35: "trettiofem",
    36: "trettiosex", 37: "trettiosju", 38: "trettioåtta", 39: "trettionio",
    40: "fyrtio", 41: "fyrtioen", 42: "fyrtiotvå", 43: "fyrtiotre",
    44: "fyrtiofyra", 45: "fyrtiofem", 46: "fyrtiosex", 47: "fyrtiosju",
    48: "fyrtioåtta", 49: "fyrtionio", 50: "femtio", 51: "femtioen",
    52: "femtiotvå", 53: "femtiotre", 54: "femtiofyra", 55: "femtiofem",
    56: "femtiosex", 57: "femtiosju", 58: "femtioåtta", 59: "femtionio",
    60: "sextio",
}

# Ordningstal används BARA till datumet. Modellen har böjt dem fel förut
# ("nittoende"), och veckodagen fel ("fredagen den trettionde juli" om en torsdag).
ORDNINGSTAL = {
    1: "första", 2: "andra", 3: "tredje", 4: "fjärde", 5: "femte", 6: "sjätte",
    7: "sjunde", 8: "åttonde", 9: "nionde", 10: "tionde", 11: "elfte",
    12: "tolfte", 13: "trettonde", 14: "fjortonde", 15: "femtonde",
    16: "sextonde", 17: "sjuttonde", 18: "artonde", 19: "nittonde",
    20: "tjugonde", 21: "tjugoförsta", 22: "tjugoandra", 23: "tjugotredje",
    24: "tjugofjärde", 25: "tjugofemte", 26: "tjugosjätte", 27: "tjugosjunde",
    28: "tjugoåttonde", 29: "tjugonionde", 30: "trettionde", 31: "trettioförsta",
}

MANADER = {1: "januari", 2: "februari", 3: "mars", 4: "april", 5: "maj",
           6: "juni", 7: "juli", 8: "augusti", 9: "september", 10: "oktober",
           11: "november", 12: "december"}

TIOTAL = {10: "ett tiotal", 20: "ett tjugotal", 30: "ett trettiotal",
          40: "ett fyrtiotal", 50: "ett femtiotal", 60: "ett sextiotal"}

# Interna fältnamn som ALDRIG får synas i en punkttext. Listan är felhistoriken
# gjord körbar – fyll på när ett nytt fältnamn tillkommer. "familj" står medvetet
# INTE här: det är ett normalt svenskt ord som gärna får sägas.
FALTNAMN = (
    "streak", "lokal_kontext", "artfakta", "omdome", "vs_yesterday", "forandring",
    "artrikedom_kontext", "anvand_fakta", "dagar_i_rad", "antal_klass", "rodlista",
    "nytt_rekord", "vingform", "kosthallning", "levnadssatt", "habitat",
    "sammanfattning", "new_species", "first_this_year", "returning_after_gap",
    "returning_details", "top_species", "species_count", "fakta_id", "prioritet",
    "kategori", "aktivitet", "har_svenskt_namn", "vetenskapligt", "artrikedom",
)

# Rovfåglar och ugglor lyfts även när de bara hördes någon enstaka gång. Matchas
# på svenska namnled – mekaniskt, inte via modellens omdöme.
ROVFAGEL_LED = ("vråk", "glada", "falk", "hök", "örn", "uggla", "gjuse")

# Prioritet per kategori. Styr det deterministiska reservurvalet när anrop 1
# faller, och inget annat – anrop 1 får välja fritt bland alla kandidater.
PRIORITET = {
    "ny_art": 90, "forsta_for_aret": 80, "atervandande": 70, "lokal_ovanlig": 65,
    "rodlistad": 60, "speciell_gast": 55, "rekord": 50, "uteblev": 48,
    "familjegrupp": 45, "svit": 40, "artrikedom": 35, "artfaktum": 30,
    "jamforelse": 25, "aktivitet": 20,
}

# FAKTABUDGETEN HAR BÅDE TAK OCH BOTTEN (2026-07-31, efter första skarpa
# testkörningen). Första versionen hade bara ett tak, och anrop 1 valde då bort
# BÅDA artfaktapunkterna – avsnittet blev faktafritt, vilket bryter mot regeln
# sedan 2026-07-24 att varje avsnitt ska ha minst en färsk detalj.
#
# Kandidaterna är fler än budgeten med flit: anrop 1 ska ha något att välja
# MELLAN, annars blir rotationen ingen rotation. Taket sätts vid urvalet i
# stället, av `_kapa_artfakta()`.
ARTFAKTA_KANDIDATER = 6   # så många arter som får en faktapunkt att välja bland
MAX_ARTFAKTA   = 2    # så många som får komma MED i ett avsnitt (taket)
MIN_ARTFAKTA   = 1    # så många som MÅSTE med när det finns någon (botten)
MAX_PER_ART    = 2    # så många punkter om SAMMA art som får med i ett avsnitt
MAX_AKTIVITET  = 3    # antal arter som får en aktivitetspunkt
MIN_FAMILJEGRUPP = 3  # så många arter ur samma familj innan gruppen är värd en punkt
FAKTALOGG_DAGAR = 4   # så många dygn bakåt en art är "nyss använd" och hoppas över


def _talord(n):
    """Räkneord i klartext, eller None om talet ligger utanför tabellen. None
    betyder att punkten UTELÄMNAS – hellre tyst än en siffra i manuset."""
    return RAKNEORD.get(int(n)) if n else None


def _datumfras(datum_iso):
    """"fredagen den trettioförsta juli". Veckodag och böjning räknas här, aldrig
    av modellen (se felet 2026-07-30)."""
    d = dt.date.fromisoformat(datum_iso)
    veckodag = VECKODAGAR.get(d.weekday())
    ordningstal = ORDNINGSTAL.get(d.day)
    manad = MANADER.get(d.month)
    if not (veckodag and ordningstal and manad):
        return None
    return f"{veckodag}en den {ordningstal} {manad}"


def _artrikedomsfras(n):
    """Avrundad artrikedom. Prompten har alltid krävt avrundning; nu är den
    mekanisk. "drygt ett tjugotal" i stället för "tjugotre"."""
    if not n:
        return None
    if n <= 3:
        return "bara ett par arter"
    if n <= 7:
        return "en handfull arter"
    bas, rest = (n // 10) * 10, n % 10
    if rest <= 2 and bas in TIOTAL:
        return f"{TIOTAL[bas]} arter"
    if rest <= 6 and bas in TIOTAL:
        return f"drygt {TIOTAL[bas]} arter"
    if bas + 10 in TIOTAL:
        return f"knappt {TIOTAL[bas + 10]} arter"
    return None


def _uppehallsfras(dagar):
    """"efter drygt tre veckors tystnad". Aldrig ett antal dagar."""
    d = int(dagar or 0)
    if d < 7:
        return None
    if d >= 300:
        return "efter nästan ett års tystnad"
    if d >= 60:
        manader = _talord(round(d / 30))
        return f"efter {manader} månaders tystnad" if manader else None
    veckor, rest = d // 7, d % 7
    if veckor == 1:
        return "efter en dryg veckas tystnad" if rest else "efter en veckas tystnad"
    if rest >= 5:
        veckor += 1
        prefix = "nästan "
    elif rest >= 2:
        prefix = "drygt "
    else:
        prefix = ""
    ord_ = _talord(veckor)
    return f"efter {prefix}{ord_} veckors tystnad" if ord_ else None


def _svitfras(dagar):
    """"tio dygn i rad", eller "tre veckor i rad" när det går jämnt upp – det
    säger samma sak men låter mindre som statistik."""
    n = int(dagar or 0)
    if n < 3:
        return None
    if n >= 14 and n % 7 == 0:
        veckor = _talord(n // 7)
        return f"{veckor} veckor i rad" if veckor else None
    ord_ = _talord(n)
    return f"{ord_} dygn i rad" if ord_ else None


AKTIVITETSFRAS = {
    "ofta hord":     "hördes flitigt hela dygnet",
    "hordes en del": "hördes en hel del",
    "enstaka":       "hördes någon enstaka gång",
}


def _ar_rovfagel(art):
    a = (art or "").lower()
    return any(led in a for led in ROVFAGEL_LED)


def _nyss_anvand(fact_log, dagar=FAKTALOGG_DAGAR):
    """Artfakta som använts de senaste dygnen, som mängd av "art/fält"-strängar.
    Faktarotationen var en promptregel ("landa inte gång på gång på samma
    favorit"); här blir den ett filter."""
    anvanda = set()
    for _, poster in sorted((fact_log or {}).items(), reverse=True)[:dagar]:
        for p in poster or []:
            anvanda.add(str(p).strip().lower())
    return anvanda


# Fälten i artfakta, i den ordning de är intressanta att säga. familj först –
# det är det faktum lyssnaren oftast har nytta av.
ARTFAKTA_FALT = ("familj", "vingform", "kosthallning", "levnadssatt", "habitat")


def _artfaktumfras(art, falt, varde):
    if falt == "familj":
        return f"{art} – familjen {varde}"
    if falt == "vingform":
        return f"{art} – {varde}"
    if falt == "habitat":
        return f"{art} – hör hemma i {varde}"
    return f"{art} – {varde}"          # kosthallning, levnadssatt


def _punkt(text, kategori, art=None, fakta_id=None, alltid_med=False, arter=None):
    """`arter` är ALLA arter punkten nämner, `art` den den handlar om.

    Skillnaden spelar roll för uppräkningen sist i avsnittet: en punkt som nämner
    tre måsfåglar ska ta bort alla tre ur "hördes också", inte bara den första.
    Utan det räknades arter upp två gånger i samma avsnitt (sett 2026-07-31)."""
    return {
        "text": text,
        "kategori": kategori,
        "prioritet": PRIORITET.get(kategori, 0),
        "art": art,
        "arter": arter if arter is not None else ([art] if art else []),
        "fakta_id": fakta_id,
        "alltid_med": alltid_med,
    }


def build_facts(today, signals, fact_log=None):
    """Kandidatpunkter för dygnet. REN FUNKTION: inga nätanrop, ingen modell,
    ingen fil utöver den faktalogg som skickas in.

    Returnerar en lista dictar med nycklarna text/kategori/prioritet/art/
    fakta_id/alltid_med. Punkter med alltid_med=True ligger UTANFÖR budgeten på
    5–8 punkter och skickas aldrig till anrop 1 för omröstning – de är ramen
    (datumet) respektive den avslutande artuppräkningen.

    Allt som inte hamnar här kan aldrig nå avsnittet. Frasverket är alltså både
    garantin och flaskhalsen; det är avsiktligt, för det är den enda delen som
    går att testa."""
    punkter = []
    nyss = _nyss_anvand(fact_log)
    display_till_art = {}

    # --- Ram: datumet. Alltid med, räknas inte mot budgeten. ---------------
    datum = _datumfras(today.get("date", ""))
    if datum:
        punkter.append(_punkt(datum, "ram", alltid_med=True))

    # --- Nya arter, första för året, återvändande --------------------------
    for art in signals.get("new_species") or []:
        punkter.append(_punkt(f"{art} – aldrig hörd här förut", "ny_art", art=art))

    for art in signals.get("first_this_year") or []:
        punkter.append(_punkt(f"{art} – första gången i år", "forsta_for_aret", art=art))

    for d in signals.get("returning_details") or []:
        fras = _uppehallsfras(d.get("dagars_uppehall"))
        if fras:
            punkter.append(_punkt(f"{d['art']} – tillbaka {fras}", "atervandande",
                                  art=d.get("art")))

    # --- Lokal kontext från Artportalen ------------------------------------
    for post in signals.get("lokal_kontext") or []:
        art = post.get("art")
        if not art:
            continue
        klass = post.get("klass")
        if klass == "ingen_lokal_notering":
            punkter.append(_punkt(
                f"{art} – i princip aldrig noterad i trakten så här års",
                "lokal_ovanlig", art=art))
        elif klass == "mycket_ovanlig":
            punkter.append(_punkt(f"{art} – mycket ovanlig i trakten så här års",
                                  "lokal_ovanlig", art=art))
        elif klass == "ovanlig":
            punkter.append(_punkt(f"{art} – ovanlig i trakten så här års",
                                  "lokal_ovanlig", art=art))

        if post.get("antal_klass") == "enstaka_noteringar":
            punkter.append(_punkt(f"{art} – bara noterad ett fåtal gånger i trakten",
                                  "lokal_ovanlig", art=art))
        elif post.get("antal_klass") == "fa_noteringar":
            punkter.append(_punkt(f"{art} – få noteringar i trakten genom åren",
                                  "lokal_ovanlig", art=art))

        namn = (post.get("rodlista_namn") or "").strip().lower()
        if namn:
            punkter.append(_punkt(f"{art} – rödlistad, {namn}", "rodlistad", art=art))
        elif post.get("rodlista"):
            punkter.append(_punkt(f"{art} – rödlistad", "rodlistad", art=art))

    # --- Rovfåglar och ugglor ----------------------------------------------
    # Prompten sa "lyft dem även om de bara hördes någon enstaka gång", modellen
    # hade inget att lägga till och lade till ett OMDÖME i stället ("alltid ett
    # litet lyft när rovfåglarna är med", fem körningar i rad). Här blir de en
    # punkt som alla andra, utan uppmaning att tycka något.
    for s in today.get("top_species") or []:
        art = s.get("display") or s.get("name")
        display_till_art[art] = s
        if art and _ar_rovfagel(art):
            punkter.append(_punkt(f"{art} – hördes", "speciell_gast", art=art))

    # --- Artrikedom och rekord ---------------------------------------------
    ak = signals.get("artrikedom_kontext") or {}
    if ak.get("nytt_rekord"):
        punkter.append(_punkt("fler arter än något tidigare dygn", "rekord"))
    rikedom = _artrikedomsfras(ak.get("idag"))
    if rikedom:
        samlat = ak.get("sammanfattning")
        text = f"{rikedom} under dygnet" + (f" – {samlat}" if samlat else "")
        punkter.append(_punkt(text, "artrikedom"))

    # --- Sviter. Lika långa sviter slås ihop till EN punkt --------------------
    # Två rader som säger exakt samma sak om olika arter ("kaja – tre veckor i
    # rad", "gråsparv – tre veckor i rad") äter två platser i budgeten och ger
    # inget nytt. Ihopslagna läser de dessutom bättre.
    per_langd = {}
    for s in signals.get("streaks") or []:
        per_langd.setdefault(s.get("dagar_i_rad"), []).append(s.get("art"))
    for dagar, arter in sorted(per_langd.items(), key=lambda t: -(t[0] or 0)):
        fras = _svitfras(dagar)
        if not fras:
            continue
        arter = [a for a in arter if a]
        if len(arter) == 1:
            punkter.append(_punkt(f"{arter[0]} – {fras}", "svit", art=arter[0]))
        elif len(arter) == 2:
            punkter.append(_punkt(f"{arter[0]} och {arter[1]} – {fras} båda två",
                                  "svit", art=arter[0], arter=arter))
        else:
            punkter.append(_punkt(f"{', '.join(arter[:-1])} och {arter[-1]} – "
                                  f"{fras} allihop", "svit", art=arter[0], arter=arter))

    # --- Sviter som BRUTITS: arter som tystnat efter lång tid i följd --------
    for s in signals.get("avbrutna_sviter") or []:
        fras = _svitfras(s.get("dagar_i_rad"))
        if fras:
            punkter.append(_punkt(f"{s['art']} – hördes inte i dag, efter {fras}",
                                  "uteblev", art=s.get("art")))

    # --- Artfakta. Fler KANDIDATER än budgeten, taket sätts vid urvalet. ----
    for post in signals.get("artfakta") or []:
        if len([p for p in punkter if p["kategori"] == "artfaktum"]) >= ARTFAKTA_KANDIDATER:
            break
        art = post.get("art")
        if not art:
            continue
        for falt in ARTFAKTA_FALT:
            varde = post.get(falt)
            if not varde:
                continue
            fid = f"{art}/{falt}"
            if fid.lower() in nyss:          # rotation: nyss använt hoppas över
                continue
            punkter.append(_punkt(_artfaktumfras(art, falt, varde), "artfaktum",
                                  art=art, fakta_id=fid))
            break                            # högst ETT faktum per art

    # --- Familjegrupper i dygnets lista -------------------------------------
    # Sant per konstruktion, som jämförelserna: vi grupperar arter som DELAR
    # familjevärde i den verifierade cachen. Avsnitten har gång på gång sträckt
    # sig efter just det här ("fyra trut-arter är inte illa") och fått ta till ett
    # OMDÖME för att det saknades ett faktum. Nu finns faktumet.
    #
    # Bonus: gruppen säger uttryckligen VILKA arter som hör ihop, vilket är den
    # bästa tänkbara motmedicinen mot poddens vanligaste fel – ladusvala och
    # hussvala hamnar i "svalor", tornseglaren gör det inte.
    for text, arter in _familjegrupper(today):
        punkter.append(_punkt(text, "familjegrupp", art=arter[0], arter=arter))

    # --- Härledda jämförelser över dygnets artlista ------------------------
    jmf = signals.get("artfakta_jamforelser") or {}
    if jmf.get("minsta_art"):
        punkter.append(_punkt(f"{jmf['minsta_art']} – dygnets minsta art",
                              "jamforelse", art=jmf["minsta_art"]))
    if jmf.get("tyngsta_art"):
        punkter.append(_punkt(f"{jmf['tyngsta_art']} – dygnets tyngsta art",
                              "jamforelse", art=jmf["tyngsta_art"]))
    if jmf.get("kraftigaste_nabben"):
        punkter.append(_punkt(
            f"{jmf['kraftigaste_nabben']} – kraftigast näbb av dygnets arter",
            "jamforelse", art=jmf["kraftigaste_nabben"]))
    if jmf.get("vanligaste_kosthallning"):
        punkter.append(_punkt(
            f"de flesta av dygnets arter är {jmf['vanligaste_kosthallning']}",
            "jamforelse"))

    # --- Aktivitet: de arter som faktiskt hördes mer än enstaka -------------
    # Mellannivån ("hordes en del") togs med 2026-07-31: ett dygn där ingen art
    # dominerar gav förut noll aktivitetspunkter, och tunn punktlista är det som
    # driver modellen till utfyllnad.
    flitiga = [s for s in (today.get("top_species") or [])
               if s.get("activity") in ("ofta hord", "hordes en del")][:MAX_AKTIVITET]
    for s in flitiga:
        art = s.get("display") or s.get("name")
        fras = AKTIVITETSFRAS.get(s.get("activity"))
        if art and fras:
            punkter.append(_punkt(f"{art} – {fras}", "aktivitet", art=art))

    return [p for p in punkter if _punkt_ar_ren(p["text"])]


def _familjegrupper(today):
    """(text, art) för varje familj som dygnet bjöd på minst MIN_FAMILJEGRUPP
    arter ur. Läses direkt ur den verifierade cachen – inte ur signals["artfakta"],
    som med flit undertrycker cirkulära familjenamn per art. En grupp är aldrig
    cirkulär: den räknar upp arterna, så rundgången uppstår inte."""
    if species_facts is None:
        return []
    try:
        grupper = {}
        for s in today.get("top_species") or []:
            fam = species_facts.familj_for(s.get("scientific", ""))
            art = s.get("display") or s.get("name")
            if fam and art:
                grupper.setdefault(fam, []).append(art)
    except Exception:
        return []

    ut = []
    for fam, arter in sorted(grupper.items()):
        if len(arter) < MIN_FAMILJEGRUPP:
            continue
        antal = _talord(len(arter))
        if not antal:
            continue
        ut.append((f"{antal} arter ur familjen {fam}: "
                   f"{', '.join(arter[:-1])} och {arter[-1]}", arter))
    return ut


def _punkt_ar_ren(text):
    """Sista spärren före prompten: ingen siffra, inget internt fältnamn. En punkt
    som bryter kontraktet slängs tyst hellre än att gå vidare – det är billigare
    att sakna en punkt än att sända ett fältnamn."""
    if not text or any(c.isdigit() for c in text):
        return False
    lag = text.lower()
    return not any(f in lag for f in FALTNAMN)


def ovriga_punkt(today, valda):
    """Uppräkning av de arter som ingen vald punkt nämner. Ligger UTANFÖR budgeten
    (Robins beslut): lyssnaren vill veta vad som lät, även när det inte är
    märkvärdigt, och uppräkningen ska inte kunna tränga undan dagens nyheter."""
    namnda = {a.lower() for p in valda for a in (p.get("arter") or []) if a}
    kvar = []
    for s in today.get("top_species") or []:
        art = s.get("display") or s.get("name")
        if art and art.lower() not in namnda:
            kvar.append(art)
    if not kvar:
        return None
    return _punkt("hördes också: " + ", ".join(kvar), "ovriga", alltid_med=True)


# ---------------------------------------------------------------------------
# 3. Generate a two-host DIALOGUE with Claude (returns list of turns)
# ---------------------------------------------------------------------------
# Manusledet är två anrop sedan 2026-07-31 – se prompt_fakta.txt / prompt_ton.txt
# och avsnittet "FRASVERKET" ovan. `prompt.txt` och `prompt_dialog.txt` är
# avvecklade: OpenAI-vägen underhålls inte längre, och dialog-prompten (330 rader,
# femton absoluta förbud) ersätts av den korta ton-prompten. Filerna ligger kvar
# tills tvåstegsupplägget körts skarpt några dygn, men läses inte av koden.

# Sidofiler som ligger bredvid manuset i episodes/ och ALDRIG är manus.
# .facts.txt tillkom 2026-07-31; utan den här filtreringen skulle punktlistans
# rubrikrad matas in som "föregående avsnitts inledning".
ICKE_MANUS = (".data.txt", ".facts.txt", ".checks.txt")


def _episode_scripts(n=4):
    """Sökvägar till de n senaste manusfilerna (nyast först)."""
    EPISODES_DIR.mkdir(parents=True, exist_ok=True)
    return sorted(
        (p for p in EPISODES_DIR.glob("*.txt")
         if not p.name.endswith(ICKE_MANUS)),
        reverse=True,
    )[:n]


def load_fact_log():
    """{datum: [använda fakta]} ur fakta_logg.json. Tom dict om filen saknas."""
    try:
        if FACT_LOG_PATH.exists():
            data = json.loads(FACT_LOG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except (json.JSONDecodeError, ValueError, OSError):
        pass
    return {}


def save_fact_log(date_iso, used):
    """Logga dagens använda artfakta. Håller bara de senaste 30 dagarna – filen
    ska vara en kort minneslapp, inte ett arkiv."""
    try:
        log = load_fact_log()
        # OBS: filtrera på u FÖRE str() – str(None) blir strängen "None" och skulle
        # annars hamna i loggen som ett "använt faktum".
        log[date_iso] = sorted({str(u).strip() for u in (used or [])
                                if u and str(u).strip()})
        for old in sorted(log)[:-30]:
            del log[old]
        FACT_LOG_PATH.write_text(
            json.dumps(log, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8")
    except Exception:
        pass          # en minneslapp får aldrig fälla körningen


def _first_line(path, limit=90):
    """Öppningsrepliken ur ett manus, kapad. Bara inledningen behövs för att
    modellen ska variera sig – inte hela repliken."""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                if ":" in line[:20]:
                    line = line.split(":", 1)[1].strip()
                return line[:limit] + ("..." if len(line) > limit else "")
    except OSError:
        pass
    return ""


def recent_openings(n=4):
    """Hur de n senaste avsnitten INLETTS, en rad per avsnitt.

    ARVET: fram till 2026-07-30 matades FYRA KOMPLETTA MANUS (~7 000 tecken) in i
    prompten som "anti-upprepning". För en språkmodell är fyra hela avsnitt inte
    kontext att undvika utan fyra EXEMPEL på hur ett avsnitt ska se ut, och exempel
    styr hårdare än instruktioner. Följden var likformiga avsnitt, faktafel som
    reproducerade sig själva (tornseglaren som svala) och ett engångsinslag som bar
    sig vidare trots tömd notisfil.

    Bara inledningarna behövs för att modellen ska variera sin öppning – och
    inledningen är för kort att imitera ett helt avsnitt ur. ~400 tecken.

    ARTFAKTA-ROTATIONEN bor INTE här längre: den sköts av faktaloggen i anrop 1
    (och som filter i build_facts). Anrop 2 ska aldrig se ett faktanamn.

    Tyst tom sträng vid fel – ska ALDRIG kunna fälla den dagliga körningen."""
    try:
        rader = [f'  "{o}"' for o in (_first_line(p) for p in _episode_scripts(n)) if o]
        return "\n".join(rader)
    except Exception:
        return ""


def daily_note():
    """Frivillig tillfällig notis (t.ex. en shoutout) ur dagens_notis.txt.
    Rader som börjar med # är kommentarer och skickas inte med. Är filen tom
    eller saknas injiceras INGENTING – notisen kan alltså aldrig fastna och
    upprepas dag efter dag av misstag (töm filen efter körningen)."""
    try:
        if not NOTIS_PATH.exists():
            return ""
        lines = [ln for ln in NOTIS_PATH.read_text(encoding="utf-8").splitlines()
                 if not ln.lstrip().startswith("#")]
        return "\n".join(lines).strip()
    except Exception:
        return ""


PROMPT_FAKTA_PATH = Path("prompt_fakta.txt")   # anrop 1: väljer punkter
PROMPT_TON_PATH   = Path("prompt_ton.txt")     # anrop 2: gör dialog av punkterna

MIN_PUNKTER = 4
MAX_PUNKTER = 8


def _las_prompt(path, markorer):
    """Promptfil utan #-kommentarrader, med varning för saknad platshållare.
    Osynk mellan kod och prompt vid uppladdning ska skrika i loggen – se
    HANDOFF 2026-07-25, då en saknad platshållare tyst föll bort."""
    raw = path.read_text(encoding="utf-8")
    lines = [ln for ln in raw.splitlines() if not ln.lstrip().startswith("#")]
    template = "\n".join(lines).strip()
    for m in markorer:
        if m not in template:
            print(f"  VARNING: platshållaren {m} saknas i {path}")
    return template


def _anropa_claude(prompt, max_tokens):
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": CLAUDE_MODEL,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=90,
    )
    r.raise_for_status()
    return "".join(
        b["text"] for b in r.json()["content"] if b.get("type") == "text"
    ).strip()


# ---------------------------------------------------------------------------
# ANROP 1 – FAKTA. Väljer bland kandidatpunkterna, skriver inte ett eget ord.
#
# Svaret är INDEX, inte text. Det är hela poängen: en modell som aldrig återger
# punkttexten kan inte skriva om den, och ordagrannheten blir mekanisk i stället
# för ombedd. Den enda bedömning som lämnas åt modellen är URVAL och ORDNING –
# alltså vad avsnittet ska handla om, vilket är just det som gör avsnitt olika.
# ---------------------------------------------------------------------------
def _valbara(facts):
    return [p for p in facts if not p.get("alltid_med")]


def build_facts_prompt(facts, fact_log=None):
    valbara = _valbara(facts)
    lista = "\n".join(f"{i + 1}. {p['text']}" for i, p in enumerate(valbara))
    logg = ""
    for datum, poster in sorted((fact_log or {}).items(), reverse=True)[:FAKTALOGG_DAGAR]:
        if poster:
            logg += f"  {datum}: {', '.join(poster)}\n"
    template = _las_prompt(PROMPT_FAKTA_PATH, ("{{PUNKTER}}",))
    return (template
            .replace("{{PUNKTER}}", lista)
            .replace("{{TIDIGARE_FAKTA}}", logg.strip() or "(inget loggat än)"))


def _reservurval(facts):
    """Deterministiskt urval på prioritet. Används när anrop 1 faller eller svarar
    obrukbart. Podden ska aldrig tystna för att ett anrop strular."""
    valbara = _valbara(facts)
    ordnade = sorted(range(len(valbara)),
                     key=lambda i: (-valbara[i]["prioritet"], i))[:6]
    ordnade.sort(key=lambda i: -valbara[i]["prioritet"])
    return ordnade, (ordnade[0] if ordnade else None)


def _kapa_per_art(valbara, rensade):
    """Högst MAX_PER_ART punkter om samma art i ett avsnitt.

    Punktlistan 2026-07-31 bar TRE tornseglarpunkter (svit + aktivitet + vingform)
    och avsnittet blev en tornseglarpodd. Anrop 1 är ombett att undvika det, men
    det är mekaniskt kontrollerbart – alltså hör det här, inte i prompten."""
    ut, per_art = [], {}
    for i in rensade:
        art = (valbara[i].get("art") or "").lower()
        if art:
            if per_art.get(art, 0) >= MAX_PER_ART:
                continue
            per_art[art] = per_art.get(art, 0) + 1
        ut.append(i)
    return ut


def _kapa_artfakta(valbara, rensade):
    """TAKET: högst MAX_ARTFAKTA artfaktapunkter i ett avsnitt.

    Som promptregel hölls det inte – fem arter fick fakta 2026-07-31 trots
    "EXAKT EN ART, HÖGST TVÅ". Nu är kandidaterna avsiktligt fler än budgeten (så
    anrop 1 har något att välja mellan) och taket sätts här i stället."""
    ut, antal = [], 0
    for i in rensade:
        if valbara[i].get("fakta_id"):
            if antal >= MAX_ARTFAKTA:
                continue
            antal += 1
        ut.append(i)
    return ut


def _garantera_artfaktum(valbara, rensade):
    """BOTTEN: minst MIN_ARTFAKTA artfaktapunkt när det finns någon att ta.

    Robins regel sedan 2026-07-24: varje avsnitt ska ha minst en färsk detalj,
    att vara faktafri är inget alternativ. Första skarpa körningen av
    tvåstegsupplägget (2026-07-31) valde bort BÅDA faktapunkterna och blev
    faktafri – jag hade byggt taket men glömt botten.

    Får inte plats punkten inom MAX_PUNKTER byts den lägst prioriterade valda
    punkten ut. Ett avsnitt ska hellre tappa ett "hördes" än sitt enda faktum."""
    if sum(1 for i in rensade if valbara[i].get("fakta_id")) >= MIN_ARTFAKTA:
        return rensade
    kandidater = [i for i in range(len(valbara))
                  if valbara[i].get("fakta_id") and i not in rensade]
    if not kandidater:
        return rensade                      # ingen artfakta att ta – tyst, inte fel

    # HELLRE EN ART SOM REDAN ÄR MED I URVALET.
    #
    # OMVÄNT 2026-07-31, samma dag som det infördes. Första versionen valde helst
    # en art som INTE redan var med, för att bryta tornseglardominansen. Det gav
    # motsatt effekt: ett faktum om en art avsnittet annars inte har anledning att
    # dröja vid blir en lös ände, och lösa ändar är det första anrop 2 stryker.
    #
    #   körning 2: tornseglare/vingform (arten redan i fokus)  -> SAGT
    #   körning 3: ladusvala/kosthallning (bara omnämnd)       -> TAPPAT
    #
    # Rotationen MELLAN dagar sköts av fakta_logg.json och behöver inte skötas
    # inom dagen också – det var den sammanblandningen som gjorde faktumet
    # hemlöst. Taket i _kapa_per_art hindrar fortfarande att en art får tre
    # punkter; faktumet blir artens andra, inte en tredje arts första.
    # Kroken får inte spränga taket per art – då vore vi tillbaka i tre punkter
    # om samma fågel. Bara arter med plats kvar räknas som krok.
    per_art = {}
    for i in rensade:
        a = (valbara[i].get("art") or "").lower()
        if a:
            per_art[a] = per_art.get(a, 0) + 1
    def _antal(i):
        return per_art.get((valbara[i].get("art") or "").lower(), 0)

    krokade = [i for i in kandidater if 0 < _antal(i) < MAX_PER_ART]
    fria    = [i for i in kandidater if _antal(i) == 0]
    # Ordningen är hela poängen: helst en art som redan är med men har plats kvar
    # (faktumet får en krok), annars en art som inte är med alls, och först i
    # sista hand en art som redan nått taket. Utan det sista ledet lade reserven
    # tillbaka tornseglarens tredje punkt när kroken var upptagen.
    val = (krokade or fria or kandidater)[0]
    if len(rensade) < MAX_PUNKTER:
        return rensade + [val]
    lagst = min(rensade, key=lambda i: valbara[i]["prioritet"])
    return [val if i == lagst else i for i in rensade]


def validate_selection(facts, punkter, huvudsak):
    """Rensar anrop 1:s svar. Index utanför listan slängs, dubbletter tas bort,
    listan kapas till MAX_PUNKTER och fylls på från prioritetsordningen om den är
    för kort. Returnerar (valda punkter i ordning, huvudsaksindex i den listan)."""
    valbara = _valbara(facts)
    n = len(valbara)
    rensade, sedda = [], set()
    for i in punkter or []:
        try:
            idx = int(i) - 1
        except (TypeError, ValueError):
            continue
        if 0 <= idx < n and idx not in sedda:
            sedda.add(idx)
            rensade.append(idx)
    rensade = rensade[:MAX_PUNKTER]

    if len(rensade) < MIN_PUNKTER:
        for idx, _ in sorted(enumerate(valbara), key=lambda t: -t[1]["prioritet"]):
            if idx not in sedda:
                sedda.add(idx)
                rensade.append(idx)
            if len(rensade) >= MIN_PUNKTER:
                break

    rensade = _kapa_per_art(valbara, rensade)
    rensade = _kapa_artfakta(valbara, rensade)
    rensade = _garantera_artfaktum(valbara, rensade)

    try:
        h = int(huvudsak) - 1
    except (TypeError, ValueError):
        h = None
    huvud_pos = rensade.index(h) if (h is not None and h in rensade) else None
    return [valbara[i] for i in rensade], huvud_pos


def select_facts(facts, fact_log=None):
    """Anrop 1. Returnerar (valda punkter, huvudsakens position i listan)."""
    try:
        svar = _anropa_claude(build_facts_prompt(facts, fact_log), max_tokens=300)
        start, end = svar.find("{"), svar.rfind("}")
        obj = json.loads(svar[start:end + 1]) if start != -1 and end > start else {}
        return validate_selection(facts, obj.get("punkter"), obj.get("huvudsak"))
    except Exception as e:
        print(f"  VARNING: faktaanropet gick inte att använda ({e}) "
              f"– faller tillbaka på prioritetsurval")
        idx, huvud = _reservurval(facts)
        return validate_selection(facts, [i + 1 for i in idx],
                                  (huvud + 1) if huvud is not None else None)


def punktlista(facts, valda, today, huvud_pos=None):
    """Den slutliga punktlistan: ramen först, de valda punkterna, uppräkningen
    sist. Detta – och ingenting annat – är vad anrop 2 får veta om fåglarna."""
    rader = []
    for p in facts:
        if p.get("alltid_med") and p["kategori"] == "ram":
            rader.append(p)
    for i, p in enumerate(valda):
        p = dict(p)
        if huvud_pos is not None and i == huvud_pos:
            p["text"] = p["text"] + "   (dagens huvudsak)"
        rader.append(p)
    ovriga = ovriga_punkt(today, valda)
    if ovriga:
        rader.append(ovriga)
    return rader


# ---------------------------------------------------------------------------
# ANROP 2 – TON. Ser punktlistan och ingenting annat: inget tal, inget fältnamn,
# ingen JSON-struktur. Behöver därför inte ett enda faktaförbud om dygnet, platsen
# eller arterna – bara ETT förbud mot att lägga till något som inte står i listan.
# ---------------------------------------------------------------------------
def build_tone_prompt(rader):
    template = _las_prompt(PROMPT_TON_PATH,
                           ("{{PUNKTLISTA}}", "{{RECENT_SCRIPTS}}", "{{DAGENS_NOTIS}}"))
    note = daily_note()
    note_block = (
        "TILLFÄLLIG NOTIS – ta med den EN gång, väv in den naturligt och gå sedan "
        "vidare som vanligt. Följ de ton- och placeringsönskemål som står i själva "
        "notisen nedan:\n" + note
    ) if note else ""
    inledningar = recent_openings()
    return (template
            .replace("{{HOST_A}}", HOST_A)
            .replace("{{HOST_B}}", HOST_B)
            .replace("{{PODD_NAMN}}", PODCAST_TITLE)
            .replace("{{PUNKTLISTA}}", "\n".join(f"- {p['text']}" for p in rader))
            .replace("{{RECENT_SCRIPTS}}", inledningar or "(inget att undvika än)")
            .replace("{{DAGENS_NOTIS}}", note_block))


# ---------------------------------------------------------------------------
# MANUSVALIDERING, LAGER 1 – deterministiska kontroller, LOGGLÄGE
#
# Ingen modell, ingen kostnad, inga nya beroenden. Skriver `.checks.txt` bredvid
# transkriptet och AGERAR INTE på utfallet. Det är avsiktligt: först ska vi se vad
# den fångar och hur många falsklarm den ger, innan någon retry-logik kopplas på.
#
# BLOCKERAR ALDRIG PUBLICERINGEN. Ett obevakat dagligt jobb som tystnar för att en
# kontroll var missnöjd är ett sämre utfall än ett avsnitt med ett skavande ord.
#
# TALKONTROLLEN ÄR NU BEVISANDE, inte heuristisk. Alla tal är förformaterade i
# frasverket, så den tillåtna talordsmängden för dygnet är exakt känd: varje talord
# i manuset som inte står i punktlistan är påhittat. Det gick inte att säga förut.
# ---------------------------------------------------------------------------

# Felhistoriken i detta projekt, gjord körbar. Fyll på när ett nytt fel dyker upp –
# det är billigare än en promptregel och det glöms inte bort.
FORBJUDNA_MONSTER = [
    (r"\bi natt\b|\bi morse\b|\bi kväll\b|\binatt\b",
     "tidsinramning – fönstret är hela dygnet"),
    # TRÄDGÅRDEN, SIMRISHAMN OCH ÖSTERLEN ÄR TILLÅTNA (Robin 2026-07-31). Det är
    # där mikrofonen står, alltså den enda plats som är sann – värdarna ska kunna
    # säga att fåglarna hörts i trädgården. En första version förbjöd "trädgård"
    # generellt, vilket vände regeln mot det enda korrekta svaret. Förbudet gäller
    # platser mikrofonen INTE kan veta något om.
    (r"\bnere vid kusten\b|\bkring kusten\b|\buppe i luften\b|\bvid vattnet\b"
     r"|\bute på (fälten|ängarna|havet)\b|\binne i skogen\b|\bpå stranden\b",
     "placerar fågeln någon annanstans än vid mikrofonen"),
    (r"\bjag (väntade|trodde|tycker|gissar|misstänker|räknade)\b"
     r"|\bvad jag (vet|minns)\b",
     "personligt påstående – värdarna har ingen egen iakttagelse"),
    (r"\bstannfågel\w*\b|\bflyttfågel\w*\b|\bövervintrar\b|\bdrar söderut\b"
     r"|\bkommer tillbaka\b|\bflyttar\b",
     "flyttning – vi har ingen pålitlig uppgift"),
    # VAD FÅGELN GJORDE. Gränsen dras vid PASSERADE kontra GJORDE NÅGOT:
    # "drog förbi" och "flög" säger i praktiken bara att fågeln var här en stund
    # och är i sin ordning (Robin 2026-07-31). Häckning, födosök och sträck är
    # påståenden om beteende som mikrofonen inte kan bära – där bor felen.
    # En öppen fundering ("man undrar om den kommer tillbaka") är inget påstående.
    (r"\bhäckad?e?\b|\bbyggde bo\b|\bmatade\b|\bjagade\b|\bletade föda\b"
     r"|\bsträckte\b|\bdrog (söderut|norrut|söder|norr)\b|\bvar på väg\b"
     r"|\båt \w+|\bdök ner\b",
     "påstår vad fågeln gjorde – det vet vi inte"),
    (r"\bjag kollade\b|\bvi såg\b|\bjag såg\b|\bjag hörde\b|\bvi hörde\b",
     "värdarna har inte själva hört fåglarna"),
    (r"\bgrader\b|\bregn\w*\b|\bblås\w*\b|\bmoln\w*\b|\bvind\w*\b",
     "väder"),
    (r"\blistan\b|\bpunkt(en|erna|lista\w*)\b|\bdatan\b|\bstatistik\w*\b"
     r"|\bloggen\b|\bstationen\b",
     "metareferens till underlaget"),
    (r"\bförlåt\b|\bnej, förlåt\b|\bfaktiskt, nej\b|\brättar\b",
     "värdarna rättar sig själva eller varandra"),
]

# Rovfågeltics­en: sex körningar i rad med ett OMDÖME om rovfåglar, i sex olika
# formuleringar. Omdömen går inte att variera bort, så de får en egen kontroll.
# DET ÄR INTE EN ROVFÅGELTICS – DET ÄR EN HUVUDSAKSTICS (konstaterat 2026-08-01).
#
# Avsnittet 2026-08-01 hade INGEN rovfågel i datan. Omdömena kom ändå, ord för ord
# ur samma register, men riktade mot dagens huvudsak (tre måsfåglar): "det är
# alltid ett fint inslag", "alltid ett välkommet inslag", "det var det som LYFTE
# dygnet". Den sista är samma formulering som HANDOFF noterat om rovfåglar sedan
# 2026-07-27 ("det lyfter listan lite extra").
#
# Fem promptförsök mot rovfågelraden misslyckades eftersom diagnosen var fel.
# Modellen värderar det den fått veta är dagens viktigaste sak – rovfåglarna var
# bara oftast det. Kontrollen följer därför huvudsaken, inte artgruppen.
FOKUS_OMDOME = re.compile(
    r"(kul|roligt|fint|extra|aldrig fel|lyft\w*|krydda|höjdpunkt|händelse"
    r"|stack ut|inte ingenting|inte illa|man ska stanna|alltid lite"
    r"|välkommet|inte självklart|verkliga samtalsämnet)",
    re.IGNORECASE)

HUVUDSAK_MARKOR = "(dagens huvudsak)"

# Omdömet står ofta i NÄSTA replik, med ett pronomen i stället för artnamnet:
# "Men Erik – röd glada!" / "Ja! Det var ett fint inslag." Därför söks omdömet i
# ett FÖNSTER efter att rovfågeln nämnts, inte bara i samma mening. Tre sådana
# omdömen slank igenom en enmeningsversion (2026-07-31), och ett fönster på två
# meningar räckte inte heller – korta medhåll ("Ja!", "Absolut.") åt upp det.
#
# Därför: fönstret är fem meningar, korta inpass räknas inte, och det STÄNGS så
# snart en annan art nämns. Byter samtalet art har rovfågeln lämnats.
FOKUS_FONSTER = 5
KORT_MENING = 4          # ord; kortare än så räknas inte mot fönstret

# Mönster som är TILLÅTNA om de står i punktlistan, men påhitt annars. Samma
# princip som talkontrollen: listan definierar vad som får sägas.
VILLKORADE_MONSTER = [
    # Unikhet flyttades hit 2026-08-01: "den första" flaggades i "Lördagen den
    # FÖRSTA augusti", alltså i datumet ur punktlistan. Superlativen är förbjudna
    # ur eget minne men helt i sin ordning när de kommer ur de uträknade
    # jämförelserna ("dygnets minsta art").
    (r"\bden enda\b|\bden största\b|\bden minsta\b|\bden snabbaste\b|\bden första\b",
     "unikhetspåstående som inte står i punktlistan"),
    (r"för den här tiden|så här års|den här årstiden|för årstiden",
     "säsongspåstående som inte står i punktlistan"),
    (r"ovanlig\w*|sällsynt\w*|rödlistad\w*",
     "ovanlighetspåstående som inte står i punktlistan"),
    (r"rekord\w*|fler än någon|aldrig tidigare",
     "rekordpåstående som inte står i punktlistan"),
]

# SVAG KONTROLL – utvärderande fyllnad. Bredare och bullrigare än de andra, och
# därför märkt som svag träff. Den finns för att fyllnadsomdömen är det enda
# felet som överlevt tvåstegsupplägget, och de går inte att variera bort.
OMDOMESFYLLNAD = re.compile(
    r"(utan tvekan|inte illa|aldrig fel|får man leta efter|höjdpunkt|som krydda"
    r"|det stora|stack ut|inte ingenting|är alltid en|alltid ett|gedigen"
    r"|bättre .{0,20} får man)", re.IGNORECASE)

# Ord som SER UT som en svensk fågelart. Fångar både påhittade arter och trasiga
# sammansättningar ("truttarter").
# OBS: "and" (som i gräsand) är MEDVETET UTE. Det matchade "ibland", och listan är
# full av liknande fällor – ett suffix som inte kan skilja en fågel från ett vanligt
# ord är sämre än inget suffix.
FAGEL_SUFFIX = ("sparv", "mes", "trast", "trut", "mås", "svala", "vråk", "falk",
                "snäppa", "sångare", "ärla", "duva", "gås", "tärna", "änder",
                "hök", "uggla", "häger", "vipa", "glada", "seglare", "fink",
                "stare", "skata", "kaja", "kråka", "spett")

# Vanliga svenska ord som råkar bära ett fågelsuffix. Fyll på när ett falsklarm
# dyker upp – kontrollen är heuristisk och ska hållas tystare än de bevisande.
FAGEL_STOPPORD = {
    "ibland", "bland", "sand", "hand", "land", "band", "strand", "sedan",
    "mestadels", "mesta", "pärla", "pärlan", "pärlor", "måste", "måsten",
    "glada", "gladare", "gladast", "gladast", "stundtals", "framstegen",
    "gången", "gångerna", "duvet", "kraftig", "kraftigast",
}

ALLTID_TILLATNA_TALORD = {"en", "ett", "båda", "bägge", "par", "handfull", "fåtal",
                          "hälften", "första", "andra"}


def _meningar(text):
    return [m.strip() for m in re.split(r"(?<=[.!?…])\s+|\n", text) if m.strip()]


def _ordstam(ord_):
    """Grov stam för att känna igen böjningar av ett artnamn ("svalorna" -> "sval").
    Trubbig med flit – den ska bara skilja en böjning från en påhittad art."""
    for slut in ("arna", "orna", "erna", "arnas", "ar", "or", "er", "en", "na",
                 "ns", "s", "n", "r"):
        if len(ord_) > len(slut) + 3 and ord_.endswith(slut):
            return ord_[: -len(slut)]
    return ord_


def validate_script(turns, rader, today=None):
    """Deterministiska kontroller av manuset. Returnerar en lista strängar.

    `rader` är punktlistan manuset skulle bygga på – den definierar vad som är
    tillåtet. Tom lista tillbaka betyder att inget mönster slog till, inte att
    manuset är sant."""
    trafffar = []
    try:
        text = " ".join((t.get("text") or "") for t in turns)
        ren = re.sub(r"\[[^\]]*\]", " ", text)          # audio-taggar räknas inte
        # Poddnamnet innehåller en siffra ("Ö24 Bird Data") och gav ett falsklarm
        # i varje körning. Det är inte ett tal om fåglarna.
        utan_namn = ren.replace(PODCAST_TITLE, " ")
        lag = ren.lower()
        tillatet = " ".join(p["text"] for p in rader).lower()

        # 1. Siffror. Prompten kräver klartext, så en siffra är per definition fel.
        for m in re.finditer(r"\d+", utan_namn):
            trafffar.append(f"SIFFRA: {m.group()!r} i klartext")

        # 2. Talord som inte finns i punktlistan. Bevisande, inte heuristiskt.
        kanda = set(RAKNEORD.values()) | set(ORDNINGSTAL.values())
        for ord_ in re.findall(r"[a-zåäöéA-ZÅÄÖ]+", lag):
            if ord_ in kanda and ord_ not in ALLTID_TILLATNA_TALORD \
                    and ord_ not in tillatet:
                trafffar.append(f"PÅHITTAT TAL: {ord_!r} finns inte i punktlistan")

        # 3. Interna fältnamn på vift.
        for falt in FALTNAMN:
            if falt in lag:
                trafffar.append(f"FÄLTNAMN: {falt!r} läckte ut i manuset")

        # 4. Förbjudna mönster ur felhistoriken.
        for monster, varfor in FORBJUDNA_MONSTER:
            for m in re.finditer(monster, lag):
                trafffar.append(f"FORMULERING: {m.group()!r} – {varfor}")

        # 4b. Villkorade mönster: tillåtna bara om punktlistan bär dem.
        for monster, varfor in VILLKORADE_MONSTER:
            for m in re.finditer(monster, lag):
                if m.group() not in tillatet:
                    trafffar.append(f"FORMULERING: {m.group()!r} – {varfor}")

        # ARTER SOM STÅR I PUNKTLISTAN RÄKNAS OCKSÅ. En avbruten svit handlar per
        # definition om en art som INTE hördes i dag ("gråhäger – hördes inte i
        # dag"), och den flaggades som påhittad art i första versionen.
        # Namnen tas ur punkternas `arter`-fält, INTE ur punkttexten. Att plocka
        # alla ord ur texten förgiftade mängden med "hördes", "dygn" och "efter",
        # vilket stängde rovfågelfönstret så fort någon sa "Hördes under dygnet".
        arter = {(s.get("display") or s.get("name") or "").lower()
                 for s in (today or {}).get("top_species", [])}
        arter |= {str(a).lower() for p in (rader or []) for a in (p.get("arter") or [])}
        arter.discard("")

        # FOKUSARTER: rovfåglar OCH dagens huvudsak. Utvidgat 2026-08-01 – se
        # kommentaren vid FOKUS_OMDOME. Omdömena följer huvudsaken, inte
        # rovfåglarna.
        fokus = {a for a in arter if _ar_rovfagel(a)}
        for p in rader or []:
            txt = p.get("text") or ""
            if HUVUDSAK_MARKOR not in txt:
                continue
            fokus |= {str(a).lower() for a in (p.get("arter") or [])}
            # Familjenamnet räknas också: värdarna säger ofta "tre arter ur
            # familjen måsfåglar" utan att nämna en enda art i samma mening.
            fam = re.search(r"familjen (\w+)", txt.lower())
            if fam:
                fokus.add(fam.group(1))
        ovriga_arter = {a for a in arter if a and a not in fokus}

        # 5. Omdöme om dagens fokus, i ett fönster efter att den nämnts (pronomen).
        kvar = 0
        for mening in _meningar(ren):
            lag_m = mening.lower()
            # Rovfåglar öppnar fönstret oavsett vad listorna säger – de har varit
            # tics­ens vanligaste mål och ska inte kunna slinka igenom för att en
            # artlista råkar saknas.
            if _ar_rovfagel(mening) or any(a in lag_m for a in fokus):
                kvar = FOKUS_FONSTER
            elif kvar > 0:
                if any(a in lag_m for a in ovriga_arter):
                    kvar = 0                      # samtalet har bytt art
                    continue
                if len(mening.split()) >= KORT_MENING:
                    kvar -= 1
            else:
                continue
            if FOKUS_OMDOME.search(mening):
                trafffar.append(f"HUVUDSAKSOMDÖME: {mening.strip()!r}")

        # 6. Ord som SER UT som en art men inte står i dygnets lista. Heuristisk –
        # den enda kontrollen här som inte är bevisande.
        #
        # Suffixet måste sökas i HELA ordet, inte bara i slutet: "truttarter"
        # (trasig sammansättning, gick ut 2026-07-30) slutar på "arter", inte på
        # "trut". Böjningar av verkliga arter fångas bort via stammen, annars
        # skulle "svalorna" flaggas när ladusvala står i listan.
        # Utan artlista skulle VARJE artnamn flaggas. Hellre ingen kontroll.
        # Familjenamn ur punktlistan räknas också ("måsfåglar" flaggades som
        # påhittad art 2026-08-01, trots att det stod i en familjegruppspunkt).
        # Punktlistans TEXT är verifierad sanning och duger som ordlista här –
        # till skillnad från i fokusfönstret ovan, där den förgiftade mängden.
        ur_listan = set(re.findall(r"[a-zåäöé]{4,}", tillatet))
        for ord_ in sorted(set(re.findall(r"[a-zåäöé]+", lag))) if arter else []:
            if ord_ in arter or ord_ in ur_listan:
                continue
            if len(ord_) < 6 or ord_ in FAGEL_STOPPORD:
                continue
            if not any(suf in ord_ for suf in FAGEL_SUFFIX):
                continue
            stam = _ordstam(ord_)
            if any(stam and (stam in a or a in ord_) for a in arter):
                continue
            trafffar.append(f"OKÄND ART: {ord_!r} står inte i dygnets artlista")

        # 7. Svala-räkning. "de tre svalarterna" räknade in tornseglaren.
        #
        # MEN: står det en familjegrupp för svalor i punktlistan är räkningen
        # verifierad och ska inte flaggas. 2026-08-01 fanns tre ÄKTA svalor
        # (ladusvala, hussvala, backsvala) som en egen punkt, och "alla tre" var
        # helt korrekt. Bara antal som INTE stämmer med punkten är ett fel.
        svalgrupp = re.search(r"(\w+) arter ur familjen svalor", tillatet)
        tillatet_antal = svalgrupp.group(1) if svalgrupp else None
        for mening in _meningar(lag):
            if "sval" not in mening:
                continue
            m = re.search(r"\b(två|tre|fyra|fem|alla|samtliga)\b", mening)
            if not m:
                continue
            if tillatet_antal and m.group() in (tillatet_antal, "alla", "samtliga"):
                continue
            trafffar.append(f"SVALRÄKNING: {mening.strip()!r}")

        # 8. SVAG: utvärderande fyllnad. Bullrigare än de andra – därför märkt.
        for m in OMDOMESFYLLNAD.finditer(ren):
            trafffar.append(f"OMDÖME (svag träff): {m.group()!r}")

        # 9. PUNKTER SOM INTE ANVÄNDES.
        #
        # Botten i faktabudgeten garanterar att ett artfaktum når PUNKTLISTAN. Den
        # garanterar inte att anrop 2 säger det: 2026-07-31 låg "ladusvala –
        # insektsätare" i listan och avsnittet blev ändå faktafritt, eftersom
        # modellen hoppade över punkten. Ett tak i koden kan inte tvinga någon att
        # tala, så kontrollen hör här.
        for p in rader or []:
            for namn in (p.get("arter") or []):
                if namn and namn.lower()[:5] not in lag:
                    trafffar.append(f"OANVÄND PUNKT: {namn!r} nämns inte i manuset")
            if not p.get("fakta_id"):
                continue
            stammar = [o[:5] for o in re.findall(r"[a-zåäöé]{5,}",
                                                 p["text"].split("–")[-1].lower())
                       if o not in ("familjen", "hemma")]
            if stammar and not any(s in lag for s in stammar):
                trafffar.append(f"OANVÄNT ARTFAKTUM: {p['text']!r} – "
                                f"avsnittet blev faktafritt")
    except Exception as e:
        trafffar.append(f"(valideringen kraschade: {e})")
    return trafffar


def write_dialogue(today, signals, fact_log=None):
    """Hela manusledet: frasverk -> anrop 1 -> anrop 2.

    Returnerar (turns, anvand_fakta, rader). `anvand_fakta` HÄRLEDS ur vilka
    artfaktapunkter som kom med – den är inte längre modellens självredovisning,
    som förutsatte att modellen räknat rätt på sin egen faktabudget (den gjorde
    inte det: fem arter fick fakta 2026-07-31 trots taket två)."""
    facts = build_facts(today, signals, fact_log)
    valda, huvud_pos = select_facts(facts, fact_log)
    rader = punktlista(facts, valda, today, huvud_pos)

    turns, _ = _parse_dialogue(_anropa_claude(build_tone_prompt(rader), max_tokens=2000))
    anvand_fakta = [p["fakta_id"] for p in valda if p.get("fakta_id")]
    return turns, anvand_fakta, rader


def _parse_dialogue(text):
    """Tolka modellens svar → (turns, anvand_fakta).

    Nytt format (2026-07-30): ett objekt {"turns": [...], "anvand_fakta": [...]},
    där anvand_fakta säger vilka artfakta manuset faktiskt använde ("art/fält").
    Det loggas och matas tillbaka som upprepningsminne i stället för hela manus.

    BAKÅTKOMPATIBELT: en bar lista av repliker (det gamla formatet) accepteras
    fortfarande. Det är medvetet – en promptfil och en kodfil kan hamna i osynk vid
    uppladdning, och då ska podden hellre gå utan faktalogg än inte gå alls."""
    if text.startswith("```"):
        text = text.strip("`")
        # Hoppa ev. språktagg ("json") på första raden.
        nl = text.find("\n")
        if nl != -1 and "{" not in text[:nl] and "[" not in text[:nl]:
            text = text[nl + 1:]

    # 1. Försök med objektformatet.
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(text[start:end + 1])
            if isinstance(obj, dict) and isinstance(obj.get("turns"), list):
                turns = [t for t in obj["turns"] if isinstance(t, dict) and t.get("text")]
                if turns:
                    used = obj.get("anvand_fakta") or []
                    if not isinstance(used, list):
                        used = [str(used)]
                    return turns, [str(u) for u in used]
        except (json.JSONDecodeError, ValueError):
            pass

    # 2. Gammalt format: bar lista av repliker.
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end > start:
        try:
            turns = json.loads(text[start:end + 1])
            turns = [t for t in turns if isinstance(t, dict) and t.get("text")]
            if turns:
                return turns, []
        except (json.JSONDecodeError, ValueError):
            pass

    # 3. Gick inget att tolka: leverera allt som en monolog, hellre än att falla.
    return [{"speaker": HOST_A, "text": text}], []


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
        for sido in ICKE_MANUS:                                      # och sidofilerna
            old.with_name(f"{old.stem}{sido}").unlink(missing_ok=True)
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

    print(f"Writing dialogue with Claude ({CLAUDE_MODEL}), två anrop...")
    turns, anvand_fakta, rader = write_dialogue(today, signals, load_fact_log())
    print(f"  {len(rader)} punkter -> {len(turns)} lines of dialogue")
    if anvand_fakta:
        print(f"  använda artfakta: {', '.join(anvand_fakta)}")

    EPISODES_DIR.mkdir(parents=True, exist_ok=True)
    out_path = EPISODES_DIR / f"{today['date']}.mp3"

    # Punktlistan sparas bredvid transkriptet och PUBLICERAS (Robins beslut
    # 2026-07-31). Den är granskningsunderlaget: läs den FÖRE manuset och se om
    # manuset håller sig till den. Varje tal i manuset som inte finns här är
    # bevisligen påhittat – det är den kontrollen som blir möjlig av att alla tal
    # är förformaterade.
    facts_path = EPISODES_DIR / f"{today['date']}.facts.txt"
    facts_path.write_text(
        f"Punktlista {today['date']} – underlaget för avsnittet\n"
        + "=" * 56 + "\n\n"
        + "\n".join(f"- {p['text']}" for p in rader)
        + "\n",
        encoding="utf-8")
    print(f"  sparade punktlista: {facts_path}")

    # Manusvalidering, lager 1. LOGGLÄGE: skriver träffarna, agerar inte på dem
    # och stoppar aldrig publiceringen.
    trafffar = validate_script(turns, rader, today)
    checks_path = EPISODES_DIR / f"{today['date']}.checks.txt"
    checks_path.write_text(
        f"Manuskontroll {today['date']} – LOGGLÄGE, inget blockeras\n"
        + "=" * 56 + "\n\n"
        + ("\n".join(f"- {t}" for t in trafffar) if trafffar
           else "Inga träffar.\n\nOBS: det betyder att inget MÖNSTER slog till,\n"
                "inte att manuset är sant.")
        + "\n",
        encoding="utf-8")
    print(f"  manuskontroll: {len(trafffar)} träffar -> {checks_path}")
    for t in trafffar[:10]:
        print(f"    · {t}")

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

    # Logga vilka artfakta som användes. Detta är hela upprepningsminnet numera –
    # nästa körning läser listan i stället för att få hela manuset som exempel.
    save_fact_log(today["date"], anvand_fakta)

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
