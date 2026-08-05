#!/usr/bin/env python3
"""Bygg species_sv.json: svenskt namn för varje svensk fågelart. ENGÅNGSKÖRNING.

VARFÖR DEN HÄR FILEN FINNS
--------------------------
Före 2026-08-04 slogs svenska namn upp MITT I DEN DAGLIGA KÖRNINGEN, en art i
taget, och resultatet cachades i species_sv.json. Det gick sönder 2026-08-04:

  * GBIF:s `species/match` gav ingen usageKey för "Chloris chloris", eftersom
    Chloris också är ett grässläkte och matchningen vägrar välja mellan riken.
  * Uppslaget returnerade då None, som cachades som tom sträng – för alltid,
    eftersom uppslag bara görs `if sci not in cache`.
  * `display` föll tillbaka på det vetenskapliga namnet, så punktlistan bar
    "Chloris chloris" i tre punkter, varav en som dagens huvudsak.
  * Anrop 2 sa latinet rakt ut och fyllde sedan i ett svenskt namn ur eget
    minne: "grönsiskan". Grönsiska är Spinus spinus. Chloris chloris är grönfink.

Namnet fanns hela tiden i GBIF. Uppslaget var enradigt fel. Slutsatsen är inte
att laga uppslaget utan att TA BORT DET UR DRIFTEN: en tabell byggd i förväg kan
inte falla klockan 06:08, och en art som saknas i den kan aldrig bli latin i
sändning – den blir utesluten och loggad i stället (se generate_report.py).

KÄLLA
-----
Dyntaxa. Svensk taxonomisk databas, SLU Artdatabanken. Licens CC0 1.0.
  datasetKey  de8934f4-a136-481c-a87a-b0b202b80a31
  Aves        taxonKey 159935840
Dyntaxa är normativ för svenska artnamn och är den taxonomi Artportalen använder,
alltså samma källa projektet redan läser via artportalen.py.

HUR NAMNET VÄLJS
----------------
Dyntaxa bär ofta FLERA svenska namn per art, och GBIF returnerar dem i
bokstavsordning – så den sämsta varianten kommer först. "hus-swala" före
"hussvala", "glada" före "röd glada", "gulfotad gråtrut, rasen michahellis" före
"medelhavstrut". Att ta det första namnet är därför systematiskt fel.

Lösningen är källans egen: endpointen `species/{key}/vernacularNames` bär ett
`preferred`-fält som `species/search` inte returnerar. Den flaggan avgjorde alla
79 flertydiga arter entydigt vid provet 2026-08-04, inklusive de fall där ingen
uppfunnen regel hade dugt (skogsgås mot tajgasädgås) och de fall där INGET av
kandidatnamnen var artens namn (Pluvialis apricaria -> ljungpipare, där båda
alternativen var underartsnamn). Ingen heuristik behövs alltså, och ingen finns
här: varje art med fler än ett namn avgörs av flaggan, inte av kod.

ANVÄNDNING
----------
    python3 bygg_artnamn.py                 # skriver species_sv.json
    python3 bygg_artnamn.py --torrkor       # visar utfallet, skriver ingenting
"""

import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

DATASET = "de8934f4-a136-481c-a87a-b0b202b80a31"
AVES_TAXONKEY = 159935840
SEARCH = "https://api.gbif.org/v1/species/search"
UA = "O24-Bird-Data/1.0 (artnamnsbygge; https://github.com/)"

UT = Path("species_sv.json")
OVERRIDE = Path("species_namn_override.json")
ATTRIBUTION = "Svenska artnamn från Dyntaxa, SLU Artdatabanken (CC0)."

# Ett vetenskapligt binomen: "Chloris chloris". Får ALDRIG bli ett svenskt namn –
# det är exakt felet 2026-08-04. Kontrolleras på varje värde innan filen skrivs.
BINOMEN = re.compile(r"^[A-Z][a-z]+ [a-z]+$")


def get(url, **params):
    q = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{url}?{q}", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def hamta_arter():
    """canonicalName -> {"key": gbif-key, "namn": [alla svenska namn]}.

    Hybridtaxa hoppas över: BirdNET rapporterar aldrig "Anas acuta x
    platyrhynchos", och deras namnsträngar förorenar riktiga artposter.
    """
    arter, offset = {}, 0
    while True:
        d = get(SEARCH, datasetKey=DATASET, highertaxonKey=AVES_TAXONKEY,
                rank="SPECIES", status="ACCEPTED", limit=1000, offset=offset)
        for rec in d.get("results", []):
            lat = rec.get("canonicalName") or ""
            if not lat or " " not in lat or " x " in lat or "/" in lat:
                continue
            namn = sorted({v["vernacularName"].strip()
                           for v in rec.get("vernacularNames", [])
                           if v.get("language") == "swe" and v.get("vernacularName")})
            if namn:
                arter.setdefault(lat, {"key": rec["key"], "namn": namn})
        offset += d.get("limit", 1000)
        if d.get("endOfRecords") or offset > 60000:
            break
    return arter


def rekommenderat(key):
    """Dyntaxas rekommenderade svenska namn för ett taxon, eller None."""
    d = get(f"https://api.gbif.org/v1/species/{key}/vernacularNames", limit=200)
    pref = sorted({v["vernacularName"].strip() for v in d.get("results", [])
                   if v.get("language") == "swe" and v.get("preferred")
                   and v.get("vernacularName")})
    return pref[0] if len(pref) == 1 else None


def gemena(namn):
    """Svenska fågelnamn skrivs gement. Dyntaxa har enstaka versalerade poster
    ("Karolinasumphöna"), och det tidigare runtime-uppslaget gjorde .lower() –
    så tabellen behåller den formen för att inte ändra namn som varit i sändning."""
    return namn.strip().lower()


def main(argv):
    torrkor = "--torrkor" in argv

    arter = hamta_arter()
    print(f"Dyntaxa Aves: {len(arter)} arter med minst ett svenskt namn")

    flertydiga = {k: v for k, v in arter.items() if len(v["namn"]) > 1}
    print(f"  varav flertydiga (fler än ett namn): {len(flertydiga)}")
    print(f"  frågar preferred-flaggan för dessa ...")

    tabell, oavgjorda = {}, {}
    for lat, v in arter.items():
        if len(v["namn"]) == 1:
            tabell[lat] = gemena(v["namn"][0])
    for i, (lat, v) in enumerate(sorted(flertydiga.items()), 1):
        try:
            namn = rekommenderat(v["key"])
        except Exception as e:
            namn = None
            print(f"    anrop misslyckades för {lat}: {e}", file=sys.stderr)
        if namn:
            tabell[lat] = gemena(namn)
        else:
            oavgjorda[lat] = v["namn"]
        if i % 25 == 0:
            print(f"    {i}/{len(flertydiga)}")
        time.sleep(0.25)

    # Överstyrningar och alias sist – de vinner över Dyntaxa.
    alias_n = over_n = 0
    if OVERRIDE.exists():
        ov = json.loads(OVERRIDE.read_text(encoding="utf-8"))
        for lat, rec in (ov.get("alias") or {}).items():
            tabell[lat] = gemena(rec["namn"] if isinstance(rec, dict) else rec)
            alias_n += 1
        for lat, rec in (ov.get("overstyrning") or {}).items():
            tabell[lat] = gemena(rec["namn"] if isinstance(rec, dict) else rec)
            over_n += 1
        print(f"  överstyrningsfilen: {alias_n} alias, {over_n} överstyrningar")
    else:
        print(f"  VARNING: {OVERRIDE} saknas – inga alias pålagda", file=sys.stderr)

    # ---- kontroller som ska falla högt, inte tystna ----
    fel = []
    for lat, namn in sorted(tabell.items()):
        if not namn or not namn.strip():
            fel.append(f"tomt namn för {lat}")
        elif BINOMEN.match(namn):
            fel.append(f"vetenskapligt binomen som svenskt namn: {lat} -> {namn!r}")
        elif re.search(r",|\brasen\b|\bunderarten\b", namn, re.I):
            fel.append(f"rasbeskrivning som namn: {lat} -> {namn!r}")
    if oavgjorda:
        for lat, namn in sorted(oavgjorda.items()):
            fel.append(f"ingen preferred-flagga, flera namn: {lat} -> {' | '.join(namn)}")

    print(f"\nTabellen: {len(tabell)} arter, {len(json.dumps(tabell))/1024:.1f} kB")
    if fel:
        print(f"\n{len(fel)} PROBLEM – ingenting skrivs:", file=sys.stderr)
        for f in fel:
            print(f"  {f}", file=sys.stderr)
        print("\nÅtgärda i species_namn_override.json och kör om.", file=sys.stderr)
        return 1

    # Diff mot den befintliga filen, så en ändring aldrig går obemärkt förbi.
    if UT.exists():
        gammal = json.loads(UT.read_text(encoding="utf-8"))
        # Metadata är inga arter. Utan detta rapporterades "_provisorisk" som en
        # försvunnen art som "behöver kanske ett alias" (2026-08-04).
        gammal = {k: v for k, v in gammal.items() if not k.startswith("_")}
        andrade = [(k, gammal[k], tabell[k]) for k in sorted(gammal)
                   if k in tabell and gammal[k] and gammal[k] != tabell[k]]
        borta = [k for k in sorted(gammal) if k not in tabell]
        print(f"\nMot befintlig {UT} ({len(gammal)} poster): "
              f"{len(andrade)} ändrade, {len(borta)} försvinner, "
              f"{len(tabell) - len(gammal)} nya netto")
        for k, a, b in andrade:
            print(f"  ÄNDRAS   {k}: {a!r} -> {b!r}")
        for k in borta:
            print(f"  BORTA    {k} (var: {gammal[k]!r}) – behöver kanske ett alias")

    if torrkor:
        print("\n--torrkor: ingenting skrivet.")
        return 0

    ut = dict(tabell)
    ut["_attribution"] = ATTRIBUTION
    UT.write_text(json.dumps(ut, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                  encoding="utf-8")
    print(f"\nSkrivet: {UT}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
