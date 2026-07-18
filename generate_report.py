#!/usr/bin/env python3
"""
Daily BirdWeather -> two-host Swedish voice podcast with memory of past days.

Two AI hosts (Astrid & Erik) chat about last night's birds, like a NotebookLM-style
deep dive but automated, scheduled, in Swedish, and tuned to one station.

Pipeline (runs on GitHub Actions, no server needed):
  1. Load history.json (the repo IS the database).
  2. Pull last night's detections from the BirdWeather API.
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
import subprocess
import tempfile
import datetime as dt
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BW_STATION_ID     = os.environ["BW_STATION_ID"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
SITE_BASE_URL     = os.environ["SITE_BASE_URL"].rstrip("/")
TTS_PROVIDER      = os.environ.get("TTS_PROVIDER", "openai").lower()

CLAUDE_MODEL  = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")  # set in the workflow
KEEP_EPISODES = 30
KEEP_HISTORY  = 30
RETURN_GAP    = 14

HOST_A = "Astrid"
HOST_B = "Erik"

DOCS_DIR     = Path("docs")
EPISODES_DIR = DOCS_DIR / "episodes"
FEED_PATH    = DOCS_DIR / "feed.xml"
INDEX_PATH   = DOCS_DIR / "index.html"
HISTORY_PATH = Path("history.json")

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
# 1. Fetch last night's data via the public GraphQL API (no token needed)
# ---------------------------------------------------------------------------
def fetch_birdweather():
    # topSpecies over the last 24h gives per-species counts for the day; we sum
    # them for the total and count the list for species richness. A high limit
    # makes sure we capture every species heard, not just the very top ones.
    # 8 timmar bakåt från körningen. Vid morgonkörning (~06) motsvarar det i
    # praktiken natten (~22–06). OBS: fönstret räknas bakåt från NÄR jobbet kör,
    # inte från fasta klockslag – kör du mitt på dagen blir det inte natt.
    query = """
    query ($id: ID!) {
      station(id: $id) {
        id
        name
        topSpecies(limit: 200, period: {count: 8, unit: "hour"}) {
          count
          species { commonName scientificName imageUrl }
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
    today_names  = [s["name"] for s in today["top_species"]]

    new_species = [n for n in today_names if n not in species_ever]

    first_this_year = []
    year_prefix = f"{TODAY.year}-"
    seen_this_year = set()
    for day in recent:
        if day["date"].startswith(year_prefix):
            seen_this_year.update(s["name"] for s in day.get("top", []))
    for n in today_names:
        if n not in new_species and n not in seen_this_year:
            first_this_year.append(n)

    recently_seen = set()
    cutoff = (TODAY - dt.timedelta(days=RETURN_GAP)).isoformat()
    for day in recent:
        if day["date"] > cutoff:
            recently_seen.update(s["name"] for s in day.get("top", []))
    returning = [
        n for n in today_names
        if n not in new_species and n not in first_this_year and n not in recently_seen
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

    return {
        "new_species": new_species,
        "first_this_year": first_this_year,
        "returning_after_gap": returning,
        "vs_yesterday": vs_yesterday,
        "days_recorded": len(recent) + 1,
        "total_species_ever": len(species_ever) + len(new_species),
    }


# ---------------------------------------------------------------------------
# 3. Generate a two-host DIALOGUE with Claude (returns list of turns)
# ---------------------------------------------------------------------------
# The wording/tone lives in an editable file (prompt.txt) so it can be tuned
# without touching this code. Lines starting with # there are treated as
# comments and stripped. Placeholders below are filled in at runtime.
PROMPT_PATH = Path("prompt.txt")

# Minimal built-in fallback, only used if prompt.txt is missing.
# Edit prompt.txt, NOT this string.
DEFAULT_PROMPT = """Du skriver manus till en kort daglig morgonpodd om faglarna.
Tva programledare samtalar: {{HOST_A}} (nyfiken, varm) och {{HOST_B}} (lugn, kunnig).
GARNATTENS DATA (JSON):
{{DATA_JSON}}
KONTINUITET – verifierade fakta, hitta inte pa egna:
{{SIGNALS_JSON}}
Skriv ett naturligt samtal pa svenska, ca 400 ord, avslappnat, korta repliker.
Returnera ENBART giltig JSON: lista av objekt med "speaker" ("{{HOST_A}}" eller
"{{HOST_B}}") och "text". Ingen markdown, ingen text utanfor listan."""


def _script_view(today):
    # Vad modellen faktiskt får se. Medvetet UTAN råa antal/totaler: antal
    # detektioner speglar hur mycket ljud som fångats, inte hur intressant något
    # är. Vi ger arter + grov aktivitet + artrikedom, så manuset kan färglägga
    # utan att recitera siffror.
    return {
        "datum": today["date"],
        "artrikedom": today.get("species_count"),
        "arter": [
            {"art": s["name"], "aktivitet": s.get("activity", "enstaka")}
            for s in today.get("top_species", [])
        ],
    }


def build_prompt(today, signals):
    if PROMPT_PATH.exists():
        raw = PROMPT_PATH.read_text(encoding="utf-8")
    else:
        print("  (prompt.txt saknas – använder inbyggd standardprompt)")
        raw = DEFAULT_PROMPT

    # Drop comment lines (those starting with #) so they aren't sent to the model.
    lines = [ln for ln in raw.splitlines() if not ln.lstrip().startswith("#")]
    template = "\n".join(lines).strip()

    for marker in ("{{DATA_JSON}}", "{{SIGNALS_JSON}}"):
        if marker not in template:
            print(f"  VARNING: platshållaren {marker} saknas i prompt.txt")

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


def synthesize_dialogue(turns, out_path):
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
<div>%%LATEST%%</div><h2>Tidigare avsnitt</h2><ul>%%ROWS%%</ul></body></html>"""


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
    script_path = EPISODES_DIR / f"{today['date']}.txt"
    script_text = "\n\n".join(
        f"{t.get('speaker', HOST_A)}: {t.get('text', '')}" for t in turns
    )
    script_path.write_text(script_text, encoding="utf-8")
    print(f"  sparade manus: {script_path}")

    # Spara EXAKT de arter som hämtades från API:t, så manuset kan verifieras
    # mot faktisk data (för att skilja hallucination från vy-/tidsfönster-skillnad).
    data_path = EPISODES_DIR / f"{today['date']}.data.txt"
    data_lines = [
        f"Hämtat {today['date']} – fönster: senaste 8 timmarna",
        f"Station: {today.get('station_name')}",
        f"Artrikedom: {today['species_count']}",
        "",
        "Arter i datan (namn + aktivitet):",
    ]
    data_lines += [
        f"  - {s['name']} ({s.get('activity', '?')})" for s in today["top_species"]
    ]
    data_path.write_text("\n".join(data_lines), encoding="utf-8")
    print(f"  sparade rådata: {data_path}")

    print(f"Synthesizing two-host audio via {TTS_PROVIDER}...")
    synthesize_dialogue(turns, out_path)
    print(f"  wrote {out_path} ({out_path.stat().st_size // 1024} KB)")

    # Update history. Spara ALLA arter (bara namn) så att sällsynta arter –
    # som ofta har lågt antal – registreras korrekt för "nytt/första för
    # året/återvändande". Råa antal sparas inte; de är inte meningsfulla.
    species_ever = history.setdefault("species_ever", {})
    for name in [s["name"] for s in today["top_species"]]:
        species_ever.setdefault(name, today["date"])
    history.setdefault("recent_days", []).append({
        "date": today["date"],
        "species_count": today["species_count"],
        "top": [{"name": s["name"]} for s in today["top_species"]],
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
