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

PODCAST_TITLE  = "Morgonfåglar"
PODCAST_DESC   = "Daglig fågelrapport från min BirdWeather-station, med Astrid och Erik."
PODCAST_AUTHOR = "BirdWeather"
PODCAST_LANG   = "sv"

BW_GRAPHQL = "https://app.birdweather.com/graphql"
TODAY      = dt.date.today()


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
    query = """
    query ($id: ID!) {
      station(id: $id) {
        id
        name
        topSpecies(limit: 200, period: {count: 24, unit: "hour"}) {
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

    return {
        "date": TODAY.isoformat(),
        "station_name": station.get("name"),
        "total_detections": sum(s["count"] for s in all_species),
        "species_count": len(all_species),
        "top_species": all_species[:12],
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
        vs_yesterday = {
            "yesterday_date": yesterday["date"],
            "yesterday_total": yesterday.get("total"),
            "today_total": today["total_detections"],
            "yesterday_top": yesterday.get("top", [])[:3],
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
        .replace("{{DATA_JSON}}", json.dumps(today, ensure_ascii=False, indent=2))
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
        r = requests.post(
            "https://api.openai.com/v1/audio/speech",
            headers={
                "Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}",
                "Content-Type": "application/json",
            },
            json={
                "model": "gpt-4o-mini-tts",
                "voice": voice,
                "input": text,
                "response_format": "mp3",
                "instructions": (
                    "Tala som en levande, varm radiopratare pa svenska: naturligt "
                    "tempo med sma pauser, tydlig men avslappnad intonation och lite "
                    "variation i tonfallet. Lat engagerad och samtalande, aldrig "
                    "monoton eller upplasande."
                ),
            },
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

    FEED_PATH.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>{escape(PODCAST_TITLE)}</title>
    <link>{escape(SITE_BASE_URL)}</link>
    <language>{PODCAST_LANG}</language>
    <description>{escape(PODCAST_DESC)}</description>
    <itunes:author>{escape(PODCAST_AUTHOR)}</itunes:author>
    <itunes:explicit>false</itunes:explicit>
{chr(10).join(items)}
  </channel>
</rss>
""", encoding="utf-8")


def build_index(mp3s):
    feed_url = f"{SITE_BASE_URL}/feed.xml"
    rows = []
    for mp3 in mp3s:
        url = f"{SITE_BASE_URL}/episodes/{mp3.name}"
        rows.append(f"""    <li>
      <span class="date">{escape(mp3.stem)}</span>
      <audio controls preload="none" src="{escape(url)}"></audio>
    </li>""")

    INDEX_PATH.write_text(f"""<!doctype html>
<html lang="sv">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(PODCAST_TITLE)}</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 640px; margin: 2rem auto;
         padding: 0 1rem; color: #1a1a1a; background: #faf8f4; }}
  h1 {{ margin-bottom: .2rem; }}
  p.sub {{ color: #666; margin-top: 0; }}
  .subscribe {{ display: inline-block; margin: 1rem 0; padding: .6rem 1rem;
               background: #2e5d34; color: #fff; border-radius: 8px;
               text-decoration: none; }}
  ul {{ list-style: none; padding: 0; }}
  li {{ padding: 1rem 0; border-top: 1px solid #e3ded4; }}
  .date {{ display: block; font-weight: 600; margin-bottom: .4rem; }}
  audio {{ width: 100%; }}
</style>
</head>
<body>
  <h1>{escape(PODCAST_TITLE)}</h1>
  <p class="sub">{escape(PODCAST_DESC)}</p>
  <a class="subscribe" href="{escape(feed_url)}">Prenumerera (RSS) i din poddapp</a>
  <p class="sub">Klistra in lanken ovan i valfri poddapp – eller lyssna direkt har nedan.</p>
  <ul>
{chr(10).join(rows)}
  </ul>
</body>
</html>
""", encoding="utf-8")


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
    print(f"Synthesizing two-host audio via {TTS_PROVIDER}...")
    synthesize_dialogue(turns, out_path)
    print(f"  wrote {out_path} ({out_path.stat().st_size // 1024} KB)")

    # Update history.
    species_ever = history.setdefault("species_ever", {})
    for name in [s["name"] for s in today["top_species"]]:
        species_ever.setdefault(name, today["date"])
    history.setdefault("recent_days", []).append({
        "date": today["date"],
        "total": today["total_detections"],
        "species_count": today["species_count"],
        "top": today["top_species"][:6],
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
