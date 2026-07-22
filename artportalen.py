#!/usr/bin/env python3
"""
Lokal artkontext från Artportalen (SLU Artdatabanken) för Ö24 Bird Data.

Ger värdarna EN sak modellen omöjligt kan veta: är en art ovanlig i just det
här närområdet och för årstiden? Allt bygger på verifierad data, aldrig gissning.

TVÅ CACHEFILER byggs OFFLINE (kräver nyckel + nät) och läses sedan GRATIS i den
dagliga poddkörningen. Inga nätanrop sker i daglig drift – det är medvetet:
pipelinen ska aldrig hänga eller falla på att Artportalen är nere.

  species_taxon.json   vetenskapligt namn -> {id, ev. rodlista}   (via Artfakta)
  species_local.json   taxonID -> säsongsregularitet + all-tids-antal (via SOS)

TRE SIGNALER byggs och slås ihop per art i local_context():
  - säsongsregularitet: är arten ovanlig i trakten just den här årstiden
  - absolut sällsynthet (A): totalt få noteringar i trakten genom åren
  - nationell rödlistestatus (C): NT/VU/EN/CR/RE från Artfakta
(Fenologi/tidigt-sent, "B", ligger i roadmappen – kräver fynddatum vi inte får
ur ren antalsaggregering; se ARTPORTALEN-RESEARCH.md.)

HUR "OVANLIG" MÄTS – regularitet över åren, inte råa antal.
Råa observationsantal är rapporteringspåverkade (iögonfallande arter
överrapporteras; en raritet kan spika när skådare flockas). Istället körs
säsongsfönstret ±N veckor runt dagens datum EN gång per år i M år, och klassen
bygger på hur många år arten alls setts lokalt:
    <20% av åren -> mycket_ovanlig
    <30%         -> ovanlig
    <=60%        -> periodvis
    annars       -> regelbunden

GDPR: endast AGGREGERADE antal på taxonnivå används. Aldrig observatörsnamn
eller enskilda fynd hämtas, sparas eller publiceras.

ATTRIBUTION (villkorskrav v1.0): data kommer från SLU Artdatabanken. Appen
(webbsidan) måste visa källhänvisning + länk till reglerna, och får inte se ut
att ägas av SLU. Villkor:
https://www.slu.se/artdatabanken/rapportering-och-fynd/oppna-data-och-apier/api-villkor/

BYGG CACHERNA (kör vid behov, t.ex. månadsvis via ett separat jobb):
    export ARTFAKTA_API_KEY=...   # nyckel för Species Information API
    export SOS_API_KEY=...        # nyckel för Species Observation System
    python artportalen.py build           # bygg båda
    python artportalen.py build-taxon      # bara namn->taxonID
    python artportalen.py build-local      # bara lokal regularitet
    python artportalen.py show             # felsök: visa kontext för historikens arter
"""

import os
import sys
import json
import datetime as dt
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Endpoints (bekräftade mot fungerande anrop 2026-07-22)
# ---------------------------------------------------------------------------
API_ROOT      = "https://api.artdatabanken.se"
SOS_BASE      = API_ROOT + "/species-observation-system/v1"
ARTFAKTA_BASE = API_ROOT + "/information/v1/speciesdataservice/v1"

AVES_TAXON_ID       = 4000104   # klassen "Fåglar" – brus, exkluderas alltid
SPECIES_CATEGORY_ID = 17        # Dyntaxa-kategori "Art" – filtrera bort ordningar/grupper

# Rödlistekategorier (IUCN-koder). Namn på svenska för podden.
_REDLIST_CODES = {"RE", "CR", "EN", "VU", "NT", "DD", "LC", "NA", "NE"}
_REDLIST_NAMES = {
    "RE": "nationellt utdöd", "CR": "akut hotad", "EN": "starkt hotad",
    "VU": "sårbar", "NT": "nära hotad", "DD": "kunskapsbrist",
    "LC": "livskraftig", "NA": "ej tillämplig", "NE": "ej bedömd",
}
# Bara verkligt hotade/nära hotade kategorier lyfts i podden. LC/DD/NA/NE = ingen nyhet.
REDLIST_NOTEWORTHY = {"RE", "CR", "EN", "VU", "NT"}

# ---------------------------------------------------------------------------
# Konfiguration (överstyrs via miljövariabler)
# ---------------------------------------------------------------------------
# Nycklar: separata per produkt, med gemensam AP_API_KEY som fallback.
def _artfakta_key():
    return os.environ.get("ARTFAKTA_API_KEY") or os.environ.get("AP_API_KEY") or ""


def _sos_key():
    return os.environ.get("SOS_API_KEY") or os.environ.get("AP_API_KEY") or ""


# Stationens position (Östergatan 24, Simrishamn). WGS84.
STATION_LAT = float(os.environ.get("AP_LAT", "55.5566"))
STATION_LON = float(os.environ.get("AP_LON", "14.3510"))
RADIUS_M    = int(os.environ.get("AP_RADIUS_KM", "25")) * 1000

SEASON_WEEKS = int(os.environ.get("AP_SEASON_WEEKS", "3"))  # ± runt dagens datum
YEARS        = int(os.environ.get("AP_YEARS", "10"))        # antal år bakåt

TAXON_CACHE = Path(os.environ.get("AP_TAXON_CACHE", "species_taxon.json"))
LOCAL_CACHE = Path(os.environ.get("AP_LOCAL_CACHE", "species_local.json"))

# Klasser som är värda att lyfta i podden. "periodvis"/"regelbunden" är inte nyheter.
NOTEWORTHY = {"mycket_ovanlig", "ovanlig"}

# Absolut sällsynthet (A): grova trösklar på TOTALT antal noteringar i trakten
# genom åren. Endast grova hinkar – exakta siffror är rapporteringspåverkade och
# reciteras aldrig i podden.
ABS_ENSTAKA_MAX = int(os.environ.get("AP_ABS_ENSTAKA_MAX", "5"))   # <= -> "enstaka_noteringar"
ABS_FA_MAX      = int(os.environ.get("AP_ABS_FA_MAX", "20"))       # <= -> "fa_noteringar"

ATTRIBUTION = "Lokal artdata från SLU Artdatabanken (Artportalen)."

HTTP_TIMEOUT = int(os.environ.get("AP_HTTP_TIMEOUT", "60"))


# ---------------------------------------------------------------------------
# Cache-I/O (samma mönster som species_sv.json i generate_report.py)
# ---------------------------------------------------------------------------
def _load_json(path):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError):
            return None
    return None


def _save_json(path, data):
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def _headers(key):
    return {"Ocp-Apim-Subscription-Key": key}


def _get(url, key, params=None):
    r = requests.get(url, headers=_headers(key), params=params, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


def _post(url, key, body, params=None):
    h = _headers(key)
    h["Content-Type"] = "application/json"
    r = requests.post(url, headers=h, json=body, params=params, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Regularitets-klassning
# ---------------------------------------------------------------------------
def rarity_class(years_seen, years_total):
    """Klassa hur regelbunden en art är lokalt givet hur många år (av totalt) den
    setts i säsongsfönstret. Fraktioner så trösklarna följer YEARS om det ändras."""
    frac = (years_seen / years_total) if years_total else 0.0
    if frac < 0.20:
        return "mycket_ovanlig"
    if frac < 0.30:
        return "ovanlig"
    if frac <= 0.60:
        return "periodvis"
    return "regelbunden"


def abs_rarity_class(antal):
    """A: grov klass för TOTALT antal noteringar i trakten genom åren. None om
    antalet inte är lågt nog att vara en nyhet (eller saknas)."""
    if antal is None:
        return None
    if antal <= ABS_ENSTAKA_MAX:
        return "enstaka_noteringar"
    if antal <= ABS_FA_MAX:
        return "fa_noteringar"
    return None


# ---------------------------------------------------------------------------
# taxon-cache-värde: bakåtkompatibelt (bart int ELLER dict {id, rodlista, ...})
# ---------------------------------------------------------------------------
def _taxon_id(val):
    if isinstance(val, dict):
        return _taxon_id(val.get("id"))
    if isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, str) and val.isdigit():
        return int(val)
    return None


def _taxon_redlist(val):
    if isinstance(val, dict):
        return val.get("rodlista"), val.get("rodlista_namn")
    return None, None


# ---------------------------------------------------------------------------
# Rödlista (C): tolerant extraktion ur Artfakta-artpost (struktur ej helt känd)
# ---------------------------------------------------------------------------
def _redlist_from_dict(d):
    """(kod, namn) ur en dict som kan bära rödlistekategori i olika former."""
    if not isinstance(d, dict):
        return None, None
    code = None
    cat = d.get("category")
    if isinstance(cat, str) and cat.strip().upper() in _REDLIST_CODES:
        code = cat.strip().upper()
    elif isinstance(cat, dict):
        for k in ("value", "code", "id", "shortName", "name"):
            v = cat.get(k)
            if isinstance(v, str) and v.strip().upper() in _REDLIST_CODES:
                code = v.strip().upper()
                break
    if not code:
        for k in ("categoryCode", "redlistCategory", "shortName", "value", "code"):
            v = d.get(k)
            if isinstance(v, str) and v.strip().upper() in _REDLIST_CODES:
                code = v.strip().upper()
                break
    if not code:
        return None, None
    return code, _REDLIST_NAMES.get(code)


def _extract_redlist(rec):
    """Returnera (kod, svenskt_namn) för senaste rödlistebedömning i artposten,
    tolerant mot okänd struktur. (None, None) om ingen kategori hittas."""
    if not isinstance(rec, dict):
        return None, None
    info = rec.get("redlistInfo") or rec.get("redListInfo") or rec.get("redlist")
    if isinstance(info, dict):
        candidates = [info]
    elif isinstance(info, list):
        candidates = [c for c in info if isinstance(c, dict)]
    elif isinstance(info, str) and info.strip().upper() in _REDLIST_CODES:
        return info.strip().upper(), _REDLIST_NAMES.get(info.strip().upper())
    else:
        candidates = []

    def _period(d):
        for k in ("periodId", "period", "assessmentYear", "year"):
            v = d.get(k)
            if isinstance(v, int):
                return v
            if isinstance(v, str) and v.isdigit():
                return int(v)
        return -1

    for d in sorted(candidates, key=_period, reverse=True):
        code, name = _redlist_from_dict(d)
        if code:
            return code, name
    return None, None


def _year_of(value):
    """Plocka ett årtal (int) ur ett datum-/tidssträngfält, annars None."""
    if isinstance(value, int) and 1800 < value < 2200:
        return value
    if isinstance(value, str) and len(value) >= 4 and value[:4].isdigit():
        y = int(value[:4])
        if 1800 < y < 2200:
            return y
    return None


# ---------------------------------------------------------------------------
# Artfakta: vetenskapligt namn -> taxonID
# ---------------------------------------------------------------------------
def _extract_taxon_id(rec):
    if not isinstance(rec, dict):
        return None
    for k in ("taxonId", "taxonID", "id", "dyntaxaTaxonId"):
        v = rec.get(k)
        if isinstance(v, int):
            return v
        if isinstance(v, str) and v.isdigit():
            return int(v)
    return None


def _record_scientific(rec):
    if not isinstance(rec, dict):
        return ""
    return (rec.get("scientificName") or rec.get("ScientificName") or "").strip()


def _lookup(scientific, key, verbose=False):
    """Slå upp Dyntaxa-taxonID + artpost för ett vetenskapligt namn via Artfakta.

    1) Sök på namnet (speciesdata/search) för att få kandidat-ID.
    2) Bekräfta via speciesdata?taxa=<id> (det anrop vi VET fungerar) att
       scientificName matchar och att det är en art (category.id == 17).
    Returnerar (taxonID, artpost) eller (None, None) om ingen säker träff.
    """
    target = scientific.strip().lower()
    try:
        hits = _get(ARTFAKTA_BASE + "/speciesdata/search", key,
                    params={"searchString": scientific})
    except requests.RequestException as e:
        if verbose:
            print(f"    sök misslyckades för {scientific!r}: {e}", file=sys.stderr)
        raise
    if not isinstance(hits, list):
        hits = [hits] if hits else []

    # Samla kandidat-ID i träffordning.
    candidate_ids = []
    for h in hits:
        tid = _extract_taxon_id(h)
        if tid and tid not in candidate_ids:
            # Om sökträffen redan bär scientificName och det matchar exakt – prioritera.
            if _record_scientific(h).lower() == target:
                candidate_ids.insert(0, tid)
            else:
                candidate_ids.append(tid)

    for tid in candidate_ids:
        rec = confirm_taxon(tid, key, verbose=verbose)
        if rec is None:
            continue
        sci = _record_scientific(rec).lower()
        cat = ((rec.get("category") or {}).get("id"))
        if sci == target and (cat is None or cat == SPECIES_CATEGORY_ID):
            return tid, rec
    # Ingen exakt vetenskaplig matchning – returnera första bekräftade art om någon.
    for tid in candidate_ids:
        rec = confirm_taxon(tid, key)
        if rec and ((rec.get("category") or {}).get("id") in (None, SPECIES_CATEGORY_ID)):
            if verbose:
                print(f"    OBS: ingen exakt namnmatch för {scientific!r}, "
                      f"använder taxonID {tid} ({_record_scientific(rec)})")
            return tid, rec
    return None, None


def lookup_taxon_id(scientific, key, verbose=False):
    """Bakåtkompatibel: returnerar bara taxonID (int) eller None."""
    return _lookup(scientific, key, verbose=verbose)[0]


def confirm_taxon(taxon_id, key, verbose=False):
    """Hämta artposten för ett taxonID (bekräftat anrop). None om tomt/fel."""
    try:
        data = _get(ARTFAKTA_BASE + "/speciesdata", key, params={"taxa": taxon_id})
    except requests.RequestException as e:
        if verbose:
            print(f"    speciesdata?taxa={taxon_id} misslyckades: {e}", file=sys.stderr)
        return None
    if isinstance(data, list):
        return data[0] if data else None
    return data or None


def build_taxon_cache(scientific_names, verbose=True):
    """Bygg/utöka namn->taxonID-cachen. None sparas för namn utan säker träff
    (som "" i sv-cachen) så vi inte slår upp samma miss om och om igen.

    För en träff sparas en dict {"id", ev. "rodlista", "rodlista_namn"} – rödlistan
    plockas ur samma artpost vi redan hämtat, alltså utan extra anrop (signal C)."""
    key = _artfakta_key()
    if not key:
        raise RuntimeError("Saknar ARTFAKTA_API_KEY (eller AP_API_KEY).")
    cache = _load_json(TAXON_CACHE) or {}
    changed = False
    for sci in scientific_names:
        sci = (sci or "").strip()
        if not sci or sci in cache:
            continue
        try:
            tid, rec = _lookup(sci, key, verbose=verbose)
        except requests.RequestException:
            # Nätfel: hoppa arten denna körning, försök igen nästa gång (spara ej).
            if verbose:
                print(f"  {sci} -> (hoppar, nätfel)")
            continue
        if tid is None:
            cache[sci] = None
        else:
            entry = {"id": tid}
            code, name = _extract_redlist(rec)
            if code:
                entry["rodlista"] = code
                entry["rodlista_namn"] = name
            cache[sci] = entry
        changed = True
        if verbose:
            rl = ""
            if isinstance(cache[sci], dict) and cache[sci].get("rodlista"):
                rl = f" [{cache[sci]['rodlista']}]"
            print(f"  {sci} -> {tid}{rl}")
    if changed:
        _save_json(TAXON_CACHE, cache)
    return cache


# ---------------------------------------------------------------------------
# SOS: aggregera fåglar i närområdet för ett datumfönster
# ---------------------------------------------------------------------------
def _aggregation_body(start_iso, end_iso):
    return {
        "taxon": {
            "ids": [AVES_TAXON_ID],
            "includeUnderlyingTaxa": True,
            "taxonCategories": [SPECIES_CATEGORY_ID],  # bara arter, inte grupper/ordningar
        },
        "geographics": {
            "geometries": [{"type": "point", "coordinates": [STATION_LON, STATION_LAT]}],
            "maxDistanceFromPoint": RADIUS_M,
        },
        "date": {
            "startDate": start_iso,
            "endDate": end_iso,
            "dateFilterType": "OverlappingStartDateAndEndDate",
        },
    }


def aggregate_taxon_ids(start_iso, end_iso, key, verbose=False, take=1000):
    """Returnerar mängden art-taxonID observerade i fönstret inom radien.
    Paginerar tills alla poster hämtats. Exkluderar Aves-klassen (brus)."""
    body = _aggregation_body(start_iso, end_iso)
    ids = set()
    skip = 0
    while True:
        page = _post(SOS_BASE + "/Observations/TaxonAggregation", key, body,
                     params={"skip": skip, "take": take})
        records = page.get("records") or []
        for rec in records:
            tid = rec.get("taxonId")
            if tid and tid != AVES_TAXON_ID:
                ids.add(int(tid))
        total = page.get("totalCount", len(records))
        skip += take
        if skip >= total or not records:
            break
    if verbose:
        print(f"    {start_iso}..{end_iso}: {len(ids)} arter")
    return ids


def aggregate_taxon_counts(start_iso, end_iso, key, verbose=False, take=1000):
    """A: som aggregate_taxon_ids men behåller TOTALT antal noteringar per taxon,
    samt första/senaste år OM aggregeringssvaret råkar bära fynddatum (tolerant –
    saknas fälten hoppas de tyst). Returnerar {taxonID: {"antal", ["forsta_ar"],
    ["senaste_ar"]}}. Exkluderar Aves-klassen."""
    body = _aggregation_body(start_iso, end_iso)
    out = {}
    skip = 0
    while True:
        page = _post(SOS_BASE + "/Observations/TaxonAggregation", key, body,
                     params={"skip": skip, "take": take})
        records = page.get("records") or []
        for rec in records:
            tid = rec.get("taxonId")
            if not tid or tid == AVES_TAXON_ID:
                continue
            info = out.setdefault(int(tid), {"antal": 0})
            info["antal"] += int(rec.get("observationCount") or 0)
            fy = _year_of(rec.get("firstSighting") or rec.get("firstObservation"))
            ly = _year_of(rec.get("lastSighting") or rec.get("lastObservation"))
            if fy:
                info["forsta_ar"] = min(fy, info.get("forsta_ar", fy))
            if ly:
                info["senaste_ar"] = max(ly, info.get("senaste_ar", ly))
        total = page.get("totalCount", len(records))
        skip += take
        if skip >= total or not records:
            break
    if verbose:
        print(f"    all-tid {start_iso}..{end_iso}: {len(out)} arter")
    return out


def _season_window(center, weeks):
    delta = dt.timedelta(weeks=weeks)
    return (center - delta), (center + delta)


def _anniversary(center, year):
    """center-datumet i ett annat år (skottdag -> 28 feb)."""
    try:
        return center.replace(year=year)
    except ValueError:
        return center.replace(year=year, day=28)


def build_local_cache(center=None, verbose=True):
    """Bygg lokal regularitets-cache. För var och en av de senaste YEARS
    HELA åren körs säsongsfönstret (±SEASON_WEEKS runt center-datumet) och vi
    räknar i hur många år varje taxonID setts. Innevarande år hoppas över
    eftersom dess fönster kan ligga i framtiden och skulle deflatera statistiken.

    Aborteras (reser) om något årsanrop fallerar, så en skev cache aldrig
    skrivs – daglig drift fortsätter då på förra giltiga cachen."""
    key = _sos_key()
    if not key:
        raise RuntimeError("Saknar SOS_API_KEY (eller AP_API_KEY).")
    center = center or _today()

    years_counted = 0
    seen_counts = {}   # taxonID -> antal år setts
    for i in range(1, YEARS + 1):
        ctr = _anniversary(center, center.year - i)
        start, end = _season_window(ctr, SEASON_WEEKS)
        ids = aggregate_taxon_ids(start.isoformat(), end.isoformat(), key, verbose=verbose)
        years_counted += 1
        for tid in ids:
            seen_counts[tid] = seen_counts.get(tid, 0) + 1

    species = {
        str(tid): {"ar_sedda": yrs, "klass": rarity_class(yrs, years_counted)}
        for tid, yrs in seen_counts.items()
    }

    # A: en all-tids-fråga (hela perioden, inte bara säsongsfönstret) för totalt
    # antal noteringar per art. Supplementär – ett fel här får ALDRIG fälla kärnan
    # (säsongsregulariteten), så den wrappas separat.
    all_time = {}
    try:
        at_start = dt.date(center.year - YEARS, 1, 1).isoformat()
        at_end = center.isoformat()
        counts = aggregate_taxon_counts(at_start, at_end, key, verbose=verbose)
        all_time = {str(tid): info for tid, info in counts.items()}
    except Exception as e:  # noqa: BLE001 – supplementär signal, degradera tyst
        if verbose:
            print(f"  (all-tids-antal hoppades: {e})", file=sys.stderr)

    cache = {
        "built": center.isoformat(),
        "window_center_md": center.strftime("%m-%d"),
        "window_weeks": SEASON_WEEKS,
        "years": years_counted,
        "radius_km": RADIUS_M // 1000,
        "lat": STATION_LAT,
        "lon": STATION_LON,
        "attribution": ATTRIBUTION,
        "species": species,
        "all_time": all_time,
    }
    _save_json(LOCAL_CACHE, cache)
    if verbose:
        print(f"  {len(species)} arter över {years_counted} år, "
              f"{len(all_time)} arter all-tid -> {LOCAL_CACHE}")
    return cache


# ---------------------------------------------------------------------------
# Daglig drift: läs cacherna (INGA nätanrop) och bygg lokal_kontext-signalen
# ---------------------------------------------------------------------------
def local_context(today_species):
    """Returnera lokal-kontext för de av dagens arter där vi har något värt att
    lyfta, läst enbart ur cacherna. Tre möjliga signaler slås ihop per art:
      - säsongsregularitet (klass: mycket_ovanlig/ovanlig/ingen_lokal_notering)
      - absolut sällsynthet (antal_klass: enstaka_noteringar/fa_noteringar)   [A]
      - nationell rödlistestatus (rodlista/rodlista_namn)                     [C]
    En art tas bara med om minst en signal är en nyhet. Tyst tom lista om cacher
    saknas eller en art inte kan mappas – aldrig nätanrop, aldrig gissning."""
    taxon = _load_json(TAXON_CACHE)
    local = _load_json(LOCAL_CACHE)
    if not taxon or not local:
        return []
    species_map = local.get("species", {})
    all_time = local.get("all_time", {}) or {}
    years = local.get("years")

    out = []
    for s in today_species:
        sci = (s.get("scientific") or "").strip()
        if not sci:
            continue
        val = taxon.get(sci)
        tid = _taxon_id(val)
        if not tid:                     # ej uppslaget / ingen säker träff
            continue
        art = s.get("display") or s.get("name")
        info = species_map.get(str(tid))
        rod, rod_namn = _taxon_redlist(val)
        at = all_time.get(str(tid)) or {}
        abs_klass = abs_rarity_class(at.get("antal"))

        entry = {"art": art, "av_ar": years}
        noteworthy = False

        # Säsongsregularitet
        if info is None:
            # TaxonID känt men saknas i säsongsstatistiken = aldrig noterad i
            # fönstret dessa år. Verkligt intressant, flaggas försiktigt.
            entry["klass"] = "ingen_lokal_notering"
            entry["ar_sedda"] = 0
            noteworthy = True
        else:
            entry["ar_sedda"] = info.get("ar_sedda")
            if info.get("klass") in NOTEWORTHY:
                entry["klass"] = info["klass"]
                noteworthy = True

        # A: absolut sällsynthet (totalt få noteringar i trakten genom åren)
        if abs_klass:
            entry["antal_klass"] = abs_klass
            if at.get("forsta_ar"):
                entry["forsta_ar"] = at["forsta_ar"]
            noteworthy = True

        # C: nationell rödlistestatus
        if rod in REDLIST_NOTEWORTHY:
            entry["rodlista"] = rod
            entry["rodlista_namn"] = rod_namn
            noteworthy = True

        if noteworthy:
            out.append(entry)
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _today():
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo("Europe/Stockholm")).date()
    except Exception:
        return dt.date.today()


def _history_scientific_names(path=Path("history.json")):
    """Alla vetenskapliga namn vi någonsin hört, ur history.json (nycklarna i
    species_ever är vetenskapliga)."""
    hist = _load_json(path) or {}
    names = set(hist.get("species_ever", {}).keys())
    for day in hist.get("recent_days", []):
        for t in day.get("top", []):
            if t.get("sci"):
                names.add(t["sci"])
    return sorted(n for n in names if n)


def _cmd_build_taxon():
    names = _history_scientific_names()
    print(f"Bygger namn->taxonID för {len(names)} arter ur history.json ...")
    build_taxon_cache(names)


def _cmd_build_local():
    print(f"Bygger lokal regularitet ({YEARS} år, ±{SEASON_WEEKS}v, "
          f"{RADIUS_M // 1000} km) ...")
    build_local_cache()


def _cmd_show():
    taxon = _load_json(TAXON_CACHE) or {}
    local = _load_json(LOCAL_CACHE) or {}
    print(f"species_taxon.json: {len(taxon)} arter")
    print(f"species_local.json: {len((local or {}).get('species', {}))} arter säsong, "
          f"{len((local or {}).get('all_time', {}))} all-tid, byggd {local.get('built')}")
    fake = [{"scientific": sci, "display": sci} for sci in taxon if _taxon_id(taxon[sci])]
    ctx = local_context(fake)
    print(f"\nNoterbart ({len(ctx)}):")
    for c in ctx:
        bits = []
        if c.get("klass"):
            bits.append(f"{c['klass']} ({c.get('ar_sedda')}/{c.get('av_ar')} år)")
        if c.get("antal_klass"):
            fa = f", första {c['forsta_ar']}" if c.get("forsta_ar") else ""
            bits.append(f"{c['antal_klass']}{fa}")
        if c.get("rodlista"):
            bits.append(f"rödlistad {c['rodlista']} ({c.get('rodlista_namn')})")
        print(f"  {c['art']}: {'; '.join(bits)}")


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "build"
    if cmd == "build":
        _cmd_build_taxon()
        _cmd_build_local()
    elif cmd == "build-taxon":
        _cmd_build_taxon()
    elif cmd == "build-local":
        _cmd_build_local()
    elif cmd == "show":
        _cmd_show()
    else:
        print(__doc__)
        print(f"Okänt kommando: {cmd!r}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except requests.HTTPError as e:
        print(f"HTTP-fel: {e}\n{getattr(e.response, 'text', '')[:400]}", file=sys.stderr)
        sys.exit(1)
    except RuntimeError as e:
        print(f"Fel: {e}", file=sys.stderr)
        sys.exit(1)
