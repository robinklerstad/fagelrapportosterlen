#!/usr/bin/env python3
"""
Verifierad artfakta för Ö24 Bird Data.

VARFÖR DEN HÄR MODULEN FINNS
Värdarna hämtade tidigare sin artkunskap ur MODELLENS EGET MINNE, med ett
skyddsräcke i prompten som sa "hoppa om du är osäker". Det fungerar inte: en
språkmodell vet inte när den är osäker. Avsnittet 2026-07-26 påstod med full
självsäkerhet att gråhägern är "den enda häger som häckar i Sverige" – fel, både
rördrom och ägretthäger häckar här.

Lösningen är samma princip som redan bär resten av pipelinen: svenska namn kommer
från GBIF, lokal ovanlighet från SOS, och nu artfakta från strukturerade dataset –
aldrig från modellen. Prompten får formulera fritt, men bara påstå det som står i
datan. VITLISTA, inte svartlista: mängden möjliga felaktiga påståenden är obegränsad,
så att filtrera bort fel i efterhand konvergerar aldrig.

EN CACHEFIL byggs OFFLINE och läses sedan GRATIS i den dagliga poddkörningen:

  species_facts.json   vetenskapligt namn -> {familj, ordning, flytt, levnadssatt,
                                              kosthallning, habitat, vikt_g, rodlista}

Inga nätanrop sker i daglig drift, och till skillnad från species_local.json behöver
den här filen aldrig byggas om på schema – kroppsvikt och kosthållning ändras inte.

KÄLLOR OCH LICENSER
  AVONET (Tobias et al. 2022, Ecology Letters), CC BY 4.0
      flytt, levnadssätt, kosthållning, habitat, kroppsvikt. Alla 11 009 nutida
      fågelarter. Laddas ner EN gång, läses lokalt – filen committas inte.
      https://doi.org/10.1111/ele.13898
  Wikidata, CC0
      familj och ordning med svensk etikett. Publik SPARQL, ingen nyckel.
  SLU Artdatabanken (Artfakta), villkor v1.0
      rödlistestatus – återanvänds ur species_taxon.json som artportalen.py redan
      byggt. Den här modulen gör alltså INGA egna anrop mot Artdatabanken.

MEDVETET INTE ANVÄNT: Artfaktas prosatexter (`speciesFactText`). Villkor 2.2
förbjuder att "ändra" skyddat material annat än enligt varje verks egen licens, och
någon sådan licens går inte att hitta. Dessutom är fältet tomt för vardagsarter.
Se FAS2-FAKTAFIL-UTREDNING.md §4.

ATTRIBUTION: AVONET är CC BY 4.0 och kräver källhänvisning i applikationen –
raden ska ligga i template.html:s footer tillsammans med SLU-hänvisningen.

OMFATTNING: cachen byggs för ALLA arter i AVONET (~10 700, ca 2,7 MB, läses in på
~15 ms). Det är billigare än att hålla reda på vilka arter som är "rimliga i Sverige":
en art som hörs i trädgården för första gången har redan fakta, och cachen behöver
aldrig byggas om. Prompten får ändå bara dygnets ~10–20 arter, aldrig hela filen.

BYGG CACHEN (engångskörning, kräver AVONET-filen + nät mot Wikidata):
    # 1. Ladda ner AVONET från figshare (se AVONET_PATH nedan)
    export AVONET_PATH=~/Downloads/ELEData/TraitData/AVONET2_eBird.xlsx
    python species_facts.py build           # allt: AVONET + Wikidata
    python species_facts.py build-avonet    # bara AVONET-fälten (inget nät)
    python species_facts.py build-wikidata  # bara översättningen familj/ordning
    python species_facts.py build-history   # mager cache: bara arter vi hört
    python species_facts.py show            # felsök: täckning + jämförelser
"""

import os
import sys
import csv
import json
import time
import datetime as dt
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------
FACTS_CACHE = Path(os.environ.get("SF_FACTS_CACHE", "species_facts.json"))
TAXON_CACHE = Path(os.environ.get("AP_TAXON_CACHE", "species_taxon.json"))

# AVONET-filen laddas ner manuellt och ligger UTANFÖR repot (~2 MB, 11 009 rader).
# Bara den destillerade species_facts.json committas.
#
# Nedladdning: https://figshare.com/s/b990722d72a26b5bfead
# Filen är en xlsx med tre flikar – en per taxonomi. VÄLJ eBird-fliken
# (AVONET2_eBird): BirdWeather bygger på BirdNET, som använder eBirds taxonomi, så
# de vetenskapliga namnen matchar våra utan omvägar. Spara fliken som CSV.
AVONET_PATH = Path(os.environ.get("AVONET_PATH", "avonet_ebird.csv"))

WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
# Wikidata kräver en beskrivande User-Agent med kontaktväg.
WIKIDATA_UA = os.environ.get(
    "SF_USER_AGENT",
    "O24BirdData/1.0 (https://o24birddata.se; daglig fagelpodd) python-requests",
)
WIKIDATA_BATCH = int(os.environ.get("SF_WIKIDATA_BATCH", "40"))

HTTP_TIMEOUT = int(os.environ.get("SF_HTTP_TIMEOUT", "60"))
HTTP_RETRIES = int(os.environ.get("SF_HTTP_RETRIES", "3"))

ATTRIBUTION_AVONET = (
    "Artegenskaper från AVONET (Tobias m.fl. 2022), CC BY 4.0."
)
ATTRIBUTION_WIKIDATA = "Taxonomi från Wikidata (CC0)."


# ---------------------------------------------------------------------------
# Enum-översättning: AVONET -> svenska
#
# Översättningen bor HÄR, i koden, inte i modellen. Lärdomen från GBIF-fixen:
# när modellen fick översätta artnamn blev House Sparrow "hussparv" i stället för
# gråsparv. Deterministisk tabell, inga överraskningar.
#
# Orden är valda för att kunna sägas HÖGT i ett avslappnat samtal, inte för
# taxonomisk precision. Där de skiljer sig står nyansen i kommentar.
# ---------------------------------------------------------------------------

# AVONET `Migration`: 1 = sedentary, 2 = partially migratory, 3 = migratory.
#
# AVSTÄNGT 2026-07-28 – läses in men går ALDRIG till prompten (sparas som
# _flytt_globalt). Skälet: AVONETs flyttdata är GLOBAL. Strandskata och sothöna
# klassas som "sedentary", vilket stämmer i Västeuropa men inte i Sverige, där de
# lämnar oss på hösten. Att skicka in ett fält vi vet är fel för svenska förhållanden
# återinför precis den felklass hela ombygget skulle stoppa.
#
# ROADMAP: ersätt med ett HÄRLETT årstidsfält ur vår egen SOS-data – kör
# artportalen.aggregate_taxon_ids() för ett vinterfönster mot ett sommarfönster över
# ~10 år och räkna i hur många år arten rapporterats i trakten. Blir Österlen-
# specifikt i stället för nationellt, kräver ingen ny källa och ingen kurering.
# VIKTIGT om det byggs: formulera det som FÖREKOMST ("finns här året runt",
# "försvinner härifrån på vintern"), inte som flyttbiologi ("stannfågel") –
# vinterförekomst betyder inte att samma individer stannar. Se HANDOFF.
MIGRATION_SV = {
    1: "stannfågel",
    2: "delvis flyttfågel",
    3: "flyttfågel",
}

# AVONET `Primary.Lifestyle`.
# "Insessorial" = sittande/klättrande i vegetation; "trädlevande" är den
# formulering en svensk skådare skulle använda, även om arten också sitter i buskar.
LIFESTYLE_SV = {
    "aerial":       "luftlevande",
    "aquatic":      "vattenlevande",
    "terrestrial":  "marklevande",
    "insessorial":  "trädlevande",
    "generalist":   "allsidig",
}

# AVONET `Trophic.Niche`.
# "Invertivore" = äter ryggradslösa djur brett, inte bara insekter. "Insektsätare"
# är ändå det naturliga svenska ordet i tal och det en skådare säger.
#
# "Aquatic predator" översattes först till "fiskätare". FEL – kategorin samlar allt
# som tar byten i vatten, så strandskata och rödbena hamnade där, och de lever på
# maskar och kräftdjur, inte fisk. Granskning av verklig data 2026-07-26 fångade det.
# Nu en formulering som är sann för både hägrar och vadare.
TROPHIC_SV = {
    "invertivore":           "insektsätare",
    "vertivore":             "smådjursätare",
    "aquatic predator":      "tar sin föda i vatten",
    "granivore":             "fröätare",
    "frugivore":             "fruktätare",
    "nectarivore":           "nektarätare",
    "herbivore terrestrial": "växtätare",
    "herbivore aquatic":     "vattenväxtätare",
    "omnivore":              "allätare",
    "scavenger":             "kadaverätare",
}

# Vingform ur AVONETs `Hand-Wing.Index` – det enda UTSEENDE-faktum vi kan härleda
# ur data i stället för ur modellens minne. Högt index = lång, spetsig vinge
# (seglare, svalor, måsfåglar, vadare); lågt = kort, rundad (skogslevande smyckare).
#
# Fördelningen över alla 10 661 arter (uträknat 2026-07-28): median 21,1 · p15 12,3 ·
# p90 49,0 · max 74,3. Mittfältet får medvetet INGEN beskrivning: "medellånga vingar"
# säger ingenting och är precis den sortens utfyllnad prompten ska slippa.
#
# HÖGA TRÖSKELN SATT TILL 65, inte p90 (49). Kalibrering mot våra egna 39 arter
# 2026-07-28 visade två problem med p90: (a) den fyrade av för 13 av 39 arter med
# exakt samma mening, alltså ett enfältsvärde snarare än en iakttagelse, och (b) den
# släppte in röd glada på 49,9 medan rödbena missade på 47,2 – och för en glada är
# "spetsiga" fel: indexet mäter spetsighet via Kipps avstånd, så seglande rovfåglar
# får höga värden av LÅNGA vingar, inte spetsiga. Vid 65 återstår bara de utpräglade
# luftjägarna (tornseglare 71,6 · kentsk tärna 69,3 · fisktärna 67,3), där påståendet
# är otvetydigt sant. Hellre tyst än nästan rätt – samma princip som överallt annars.
#
# LÅGA TRÖSKELN fyrar aldrig av för svenska arter (vår lägsta är gransångaren på
# 18,4; den låga svansen är tropiska undervegetationsfåglar). Behållen för
# fullständighet, men förvänta dig inga träffar.
HWI_LONG_POINTED = float(os.environ.get("SF_HWI_LONG", "65"))
HWI_SHORT_ROUND  = float(os.environ.get("SF_HWI_SHORT", "12.3"))  # p15


def wing_shape(hwi):
    """Talbar vingform ur hand-wing index. None när värdet ligger i mittfältet."""
    if hwi is None:
        return None
    if hwi >= HWI_LONG_POINTED:
        return "långa, spetsiga vingar"
    if hwi <= HWI_SHORT_ROUND:
        return "korta, rundade vingar"
    return None


# AVONET `Habitat`.
HABITAT_SV = {
    "forest":         "skog",
    "woodland":       "gles skog",
    "shrubland":      "buskmark",
    "grassland":      "öppen mark",
    "wetland":        "våtmark",
    "riverine":       "vattendrag",
    "coastal":        "kustmiljö",
    "marine":         "hav",
    "rock":           "klippmark",
    "desert":         "torrmark",
    "human modified": "människopräglad mark",
}

# Rödlistekategorier värda att lyfta (samma urval som artportalen.py).
REDLIST_NOTEWORTHY = {"RE", "CR", "EN", "VU", "NT"}

# Fältnamn som facts_for() exponerar mot prompten, i den ordning de är mest
# poddvänliga. vikt_g ingår INTE – den talas aldrig ut, se comparisons().
# OBS: "flytt" saknas medvetet – se kommentaren vid MIGRATION_SV. Värdet läses in
# men lagras som _flytt_globalt, och underscore-fält går aldrig till prompten.
#
# "ordning" saknas också, från 2026-07-28. Ordningsnivån är dels obekant i tal
# ("hackspettartade fåglar"), dels taxonomiberoende på ett vilseledande sätt: AVONET
# för tornseglaren till Caprimulgiformes, vars svenska etikett är "skärrfåglar" –
# alltså nattskärror. "Tornseglaren hör till skärrfåglarna" är försvarbart i en vid
# klassificering men låter fel för en kunnig lyssnare. Familjenivån är både naturlig
# i tal ("hör till hägrarna") och stabil. Ordningen ligger kvar i cachen.
SPOKEN_FIELDS = ("familj", "levnadssatt", "kosthallning", "habitat", "vingform")

# Fält som INTE kommer från AVONET och därför måste överleva ett ombygge: de hämtas
# från Wikidata, som kan vara nere. Allt annat ägs av AVONET och skrivs om varje gång
# (se kommentaren i build() om varför det är viktigt).
PRESERVED_FIELDS = ("familj", "ordning")

# ---------------------------------------------------------------------------
# Namnalias: pipelinens vetenskapliga namn -> namnet i AVONET/Wikidata
#
# BirdWeather (via BirdNET) och AVONET följer båda eBirds taxonomi, men inte
# nödvändigtvis samma ÅRGÅNG av den, och Wikidata följer sin egen. Där släktet
# flyttats behövs en explicit brygga. Tabellen är medvetet LITEN och handskriven:
# varje rad är ett konstaterat glapp, inte en gissning.
#
# Kajan och kråkan är särskilt viktiga – de hör till trädgårdens vanligaste fåglar
# och är samma två arter som står som None i species_taxon.json.
ALIASES = {
    "Corvus monedula": "Coloeus monedula",   # kajan förs numera till Coloeus
    "Corvus cornix":   "Corvus corone",      # kråkan behandlas ofta som underart
}


def _alias_candidates(sci):
    """Namn att prova för en art, i tur och ordning: först som pipelinen stavar
    det, sedan ett känt alias. Första träffen vinner."""
    out = [sci]
    alias = ALIASES.get(sci)
    if alias and alias not in out:
        out.append(alias)
    return out


# ---------------------------------------------------------------------------
# Cache-I/O (samma mönster som artportalen.py)
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
# AVONET-läsning
# ---------------------------------------------------------------------------
def _norm_col(name):
    """Normalisera ett kolumnnamn så "Trophic.Niche", "Trophic Niche" och
    "trophic_niche" alla matchar. AVONET-filen finns i flera dialekter beroende på
    om man exporterat från xlsx, R eller figshare-CSV:n."""
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())


def _pick_col(fieldnames, *candidates):
    """Hitta första kolumn vars normaliserade namn matchar någon kandidat."""
    norm = {_norm_col(f): f for f in (fieldnames or [])}
    for cand in candidates:
        hit = norm.get(_norm_col(cand))
        if hit:
            return hit
    return None


def _clean(value):
    """Trimma ett cellvärde. Tomt/NA/NaN blir None."""
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.upper() in ("NA", "N/A", "NAN", "NULL", "-"):
        return None
    return s


def _as_int(value):
    """Heltal ur en cell som kan vara "2", "2.0" eller 2. None om det inte går."""
    s = _clean(value)
    if s is None:
        return None
    try:
        return int(round(float(s)))
    except (TypeError, ValueError):
        return None


def _as_float(value):
    s = _clean(value)
    if s is None:
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _col_index(ref):
    """Cellreferens -> 0-baserat kolumnindex. "A1" -> 0, "AB7" -> 27."""
    n = 0
    for ch in ref:
        if ch.isalpha():
            n = n * 26 + (ord(ch.upper()) - 64)
        else:
            break
    return n - 1


def _rows_from_xlsx(path, sheet_prefix="AVONET"):
    """Läs rader ur en xlsx-flik med BARA standardbiblioteket.

    AVONET distribueras som xlsx, och vi vill inte dra in openpyxl som beroende
    (pipelinen kör i GitHub Actions med minimal miljö). En xlsx är en zip med XML,
    så det räcker gott med zipfile + ElementTree för ett platt kalkylblad.

    Väljer den första fliken vars namn börjar med `sheet_prefix` – så pekar man ut
    filen får man rätt flik automatiskt och kan inte råka välja "Metadata"."""
    import zipfile
    from xml.etree import ElementTree as ET

    NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    RID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"

    with zipfile.ZipFile(path) as z:
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        rmap = {r.get("Id"): r.get("Target") for r in rels}

        names, target = [], None
        for sh in wb.iter(NS + "sheet"):
            nm = sh.get("name") or ""
            names.append(nm)
            if target is None and nm.upper().startswith(sheet_prefix.upper()):
                target = rmap.get(sh.get(RID))
        if not target:
            raise ValueError(
                f"Hittar ingen flik som börjar med {sheet_prefix!r} i {Path(path).name}. "
                f"Flikar: {names}"
            )
        if not target.startswith("xl/"):
            target = "xl/" + target.lstrip("/")

        # Delade strängar: xlsx lagrar text en gång och refererar via index.
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            sst = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in sst.iter(NS + "si"):
                shared.append("".join(t.text or "" for t in si.iter(NS + "t")))

        sheet = ET.fromstring(z.read(target))
        for row in sheet.iter(NS + "row"):
            cells = {}
            for c in row.iter(NS + "c"):
                idx = _col_index(c.get("r") or "")
                if idx < 0:
                    continue
                kind = c.get("t")
                v = c.find(NS + "v")
                if kind == "s" and v is not None and (v.text or "").isdigit():
                    i = int(v.text)
                    val = shared[i] if i < len(shared) else None
                elif kind == "inlineStr":
                    node = c.find(NS + "is")
                    val = ("".join(t.text or "" for t in node.iter(NS + "t"))
                           if node is not None else None)
                else:
                    val = v.text if v is not None else None
                cells[idx] = val
            width = (max(cells) + 1) if cells else 0
            yield [cells.get(i) for i in range(width)]


def _rows_from_csv(path):
    """Läs rader ur en CSV. AVONET-exporter förekommer både komma- och
    semikolonseparerade, så avgränsaren sniffas."""
    with Path(path).open(encoding="utf-8-sig", newline="") as fh:
        sample = fh.read(8192)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        for row in csv.reader(fh, dialect):
            yield row


def read_avonet(path=None, wanted=None):
    """Läs AVONET och returnera {vetenskapligt namn: {fält}} för de arter vi bryr oss om.

    Tar både .xlsx (AVONET som det laddas ner) och .csv. `wanted` är en mängd
    vetenskapliga namn; None = ta alla 11 009 (bara vid felsökning).

    Kolumnmatchningen är tolerant mot namndialekter (Species1/2/3, Trophic.Niche vs
    Trophic Niche). En art som saknar ett värde får helt enkelt inte fältet – aldrig
    ett gissat standardvärde.

    OBS: använd eBird-fliken (AVONET2_eBird). BirdWeather bygger på BirdNET, som
    följer eBirds taxonomi. Verifierat 2026-07-26: eBird-fliken matchar 39/39 av
    våra arter, BirdLife-varianten missar tre (skrattmås, kråka, ärtsångare)."""
    path = Path(path or AVONET_PATH)
    if not path.exists():
        raise FileNotFoundError(
            f"Hittar inte AVONET-filen: {path}\n"
            "Ladda ner från https://figshare.com/s/b990722d72a26b5bfead och peka ut "
            "ELEData/TraitData/AVONET2_eBird.xlsx med AVONET_PATH=/sökväg/till/fil"
        )

    if path.suffix.lower() == ".xls":
        raise ValueError(f"{path.name}: gammalt .xls-format stöds inte. Använd .xlsx eller .csv.")
    rows = (_rows_from_xlsx(path) if path.suffix.lower() == ".xlsx"
            else _rows_from_csv(path))

    # Sök även på kända alias, men lägg resultatet under pipelinens stavning så
    # resten av koden bara behöver känna ett namn per art.
    back = {}
    if wanted:
        for w in wanted:
            for cand in _alias_candidates(w.strip()):
                back[cand.lower()] = w.strip()
    want = set(back) if wanted else None
    out = {}

    rows = iter(rows)
    try:
        cols = [str(c) if c is not None else "" for c in next(rows)]
    except StopIteration:
        raise ValueError(f"{path.name} är tom.")

    c_name = _pick_col(cols, "Species2", "Species1", "Species3", "Species",
                       "scientificName", "Scientific.Name")
    if not c_name:
        raise ValueError(f"Hittar ingen artnamnskolumn i {path.name}. Sett: {cols[:12]}")

    # Familj och ordning finns i AVONET som LATINSKA namn (Ardeidae). De översätts
    # till svenska via Wikidata – aldrig av modellen, och aldrig genom att latinet
    # skickas till prompten som det är.
    idx = {name: i for i, name in enumerate(cols)}
    c = {
        "name": idx[c_name],
        "fam":  idx.get(_pick_col(cols, "Family2", "Family1", "Family3", "Family")),
        "ord":  idx.get(_pick_col(cols, "Order2", "Order1", "Order3", "Order")),
        "mig":  idx.get(_pick_col(cols, "Migration")),
        "life": idx.get(_pick_col(cols, "Primary.Lifestyle", "PrimaryLifestyle")),
        "trop": idx.get(_pick_col(cols, "Trophic.Niche", "TrophicNiche")),
        "hab":  idx.get(_pick_col(cols, "Habitat")),
        "mass": idx.get(_pick_col(cols, "Mass", "Body.Mass")),
        # Morfologi: vingform talas ut (härlett utseende), näbbdjupet bara jämförs.
        "hwi":  idx.get(_pick_col(cols, "Hand-Wing.Index", "HandWingIndex", "HWI")),
        "beak": idx.get(_pick_col(cols, "Beak.Depth", "BeakDepth")),
    }

    def cell(row, key):
        i = c.get(key)
        return row[i] if i is not None and i < len(row) else None

    for row in rows:
        sci = _clean(cell(row, "name"))
        if not sci:
            continue
        if want is not None and sci.lower() not in want:
            continue

        entry = {}

        mig = _as_int(cell(row, "mig"))
        if mig in MIGRATION_SV:
            # Internt fält: sparas för framtiden men talas aldrig ut.
            entry["_flytt_globalt"] = MIGRATION_SV[mig]

        life = (_clean(cell(row, "life")) or "").lower()
        if life in LIFESTYLE_SV:
            entry["levnadssatt"] = LIFESTYLE_SV[life]

        trop = (_clean(cell(row, "trop")) or "").lower()
        if trop in TROPHIC_SV:
            entry["kosthallning"] = TROPHIC_SV[trop]

        hab = (_clean(cell(row, "hab")) or "").lower()
        if hab in HABITAT_SV:
            entry["habitat"] = HABITAT_SV[hab]

        mass = _as_float(cell(row, "mass"))
        if mass and mass > 0:
            entry["vikt_g"] = round(mass, 1)

        # Vingform: härlett UTSEENDE ur mätdata. Indexet självt sparas internt så
        # trösklarna kan justeras utan att läsa om AVONET-filen.
        hwi = _as_float(cell(row, "hwi"))
        if hwi is not None:
            entry["_hwi"] = round(hwi, 1)
            form = wing_shape(hwi)
            if form:
                entry["vingform"] = form

        # Näbbdjup (mm): talas ALDRIG ut, används bara till jämförelser mellan
        # dygnets arter i comparisons().
        beak = _as_float(cell(row, "beak"))
        if beak and beak > 0:
            entry["_nabbdjup"] = round(beak, 1)

        fam = _clean(cell(row, "fam"))
        if fam:
            entry["_familj_latin"] = fam
        ordn = _clean(cell(row, "ord"))
        if ordn:
            entry["_ordning_latin"] = ordn

        if entry:
            # Nyckla på pipelinens stavning, inte AVONETs, när de skiljer sig.
            out.setdefault(back.get(sci.lower(), sci), entry)

    return out


# ---------------------------------------------------------------------------
# Wikidata: familj och ordning med svensk etikett
# ---------------------------------------------------------------------------
# Vi frågar på LATINSKA taxonnamn (P225) och plockar den svenska etiketten.
# Enklare och robustare än att vandra P171* uppåt från arten: AVONET ger oss redan
# familj och ordning, så Wikidata behöver bara översätta. Dessutom är antalet unika
# familjer litet (~25 för våra 39 arter), så det blir en enda fråga.
#
# FILTER på lang="sv" i stället för label-tjänsten: saknas svensk etikett kommer
# ingen rad alls, i stället för ett "Q12345" som måste sorteras bort.
_SPARQL_TEMPLATE = """SELECT ?lat ?label WHERE {
  VALUES ?lat { %s }
  ?t wdt:P225 ?lat ; rdfs:label ?label .
  FILTER(LANG(?label) = "sv")
}"""


def _sparql(query, verbose=False):
    """Kör en SPARQL-fråga med backoff. Reser vid definitivt fel så anroparen kan
    välja att INTE cacha – ett tillfälligt fel får aldrig bli en sanning i cachen
    (lärdomen från GBIF-strulet där rate limiting förgiftade species_sv.json)."""
    headers = {"User-Agent": WIKIDATA_UA, "Accept": "application/sparql-results+json"}
    last = None
    for attempt in range(HTTP_RETRIES):
        try:
            r = requests.get(WIKIDATA_SPARQL, params={"query": query, "format": "json"},
                             headers=headers, timeout=HTTP_TIMEOUT)
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.HTTPError(f"HTTP {r.status_code}", response=r)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            last = e
            wait = 2 ** attempt
            if verbose:
                print(f"    Wikidata-försök {attempt + 1} misslyckades ({e}), "
                      f"väntar {wait}s", file=sys.stderr)
            time.sleep(wait)
    raise last


def lookup_swedish_labels(latin_names, verbose=False):
    """Översätt latinska taxonnamn (familjer, ordningar) till svenska via Wikidata.

    Returnerar {latinskt namn: svensk etikett} för de som HAR en svensk etikett.
    Saknas den utelämnas namnet helt – vi skickar aldrig latin till prompten och
    aldrig en maskinöversättning."""
    names = sorted({(n or "").strip() for n in latin_names if (n or "").strip()})
    out = {}
    for i in range(0, len(names), WIKIDATA_BATCH):
        batch = names[i:i + WIKIDATA_BATCH]
        values = " ".join('"%s"' % n.replace('"', "") for n in batch)
        data = _sparql(_SPARQL_TEMPLATE % values, verbose=verbose)
        for b in data.get("results", {}).get("bindings", []):
            lat = (b.get("lat") or {}).get("value")
            label = (b.get("label") or {}).get("value")
            if lat and label and not _looks_like_qid(label):
                out.setdefault(lat, _normalize_label(label))
        if verbose:
            print(f"    Wikidata: {i + len(batch)}/{len(names)} taxonnamn")
    return out


def _looks_like_qid(label):
    return bool(label) and label[0] == "Q" and label[1:].isdigit()


def _normalize_label(label):
    """Gör en Wikidata-etikett talbar.

    Wikidata versaliserar många taxonetiketter ("Måsfåglar", "Kråkfåglar") medan
    andra står gement ("hägrar", "finkar") – 160 av 192 familjenamn började med
    versal. Svenska gruppnamn för fåglar är inte egennamn, så första bokstaven
    gemenas. Det gäller även adjektiv av ortnamn ("Afrikanska sångare" ->
    "afrikanska sångare"), som också är gemena på svenska."""
    s = (label or "").strip()
    if not s:
        return ""
    # Lämna versaler i akronymer/kortformer orörda (inga kända fall, men billigt).
    if s.isupper():
        return s
    return s[0].lower() + s[1:]


# ---------------------------------------------------------------------------
# Rödlista: återanvänds ur species_taxon.json (inga egna Artdatabanken-anrop)
# ---------------------------------------------------------------------------
def redlist_from_taxon_cache(path=None):
    """{vetenskapligt namn: rödlistekod} för de arter där artportalen.py redan
    hämtat en noterbar kategori. Tom dict om cachen saknas."""
    cache = _load_json(Path(path or TAXON_CACHE)) or {}
    out = {}
    for sci, val in cache.items():
        if isinstance(val, dict):
            code = val.get("rodlista")
            if isinstance(code, str) and code.upper() in REDLIST_NOTEWORTHY:
                out[sci] = code.upper()
    return out


# ---------------------------------------------------------------------------
# Bygg cachen
# ---------------------------------------------------------------------------
def build(scientific_names=None, avonet_path=None, verbose=True, skip_wikidata=False):
    """Bygg species_facts.json.

    `scientific_names=None` (standard) bygger för ALLA arter i AVONET – ~10 700 st,
    ca 2,7 MB, läses in på ~15 ms. Det är medvetet: en art som dyker upp i trädgården
    för första gången har då redan fakta, och cachen behöver aldrig byggas om. Skicka
    en namnlista bara om du vill ha en mager cache.

    Slår ihop tre källor. En art tas med så snart NÅGON källa gav något; fält som
    ingen källa kunde fylla utelämnas helt. Befintlig cache bevaras för arter som
    inte gick att slå upp den här gången, så en tillfällig Wikidata-strul inte
    raderar tidigare gott arbete."""
    names = None
    if scientific_names is not None:
        names = sorted({(n or "").strip() for n in scientific_names if (n or "").strip()})

    existing = _load_json(FACTS_CACHE) or {}
    old_species = {k: v for k, v in existing.items() if not k.startswith("_")}

    if verbose:
        print("Bygger artfakta för "
              + (f"{len(names)} angivna arter ..." if names else "ALLA arter i AVONET ..."))

    # 1. AVONET (lokal fil, inget nät)
    avonet = read_avonet(avonet_path, wanted=names)
    if verbose and names:
        print(f"  AVONET: {len(avonet)}/{len(names)} arter matchade")
        missing = [n for n in names if n not in avonet]
        if missing:
            print(f"    utan AVONET-träff: {', '.join(missing)}")
    elif verbose:
        print(f"  AVONET: {len(avonet)} arter")
    if names is None:
        names = sorted(avonet)

    # 2. Wikidata: översätt AVONETs latinska familj/ordning till svenska.
    #    Nätberoende, men får misslyckas utan att fälla bygget.
    latin = set()
    for rec in avonet.values():
        for k in ("_familj_latin", "_ordning_latin"):
            if rec.get(k):
                latin.add(rec[k])
    labels = {}
    if latin and not skip_wikidata:
        try:
            labels = lookup_swedish_labels(latin, verbose=verbose)
            if verbose:
                print(f"  Wikidata: {len(labels)}/{len(latin)} taxonnamn fick "
                      "svensk etikett")
                missing = sorted(latin - set(labels))
                if missing:
                    print(f"    utan svensk etikett: {', '.join(missing)}")
        except requests.RequestException as e:
            if verbose:
                print(f"  Wikidata hoppades ({e}) – familj/ordning behålls från "
                      "tidigare cache", file=sys.stderr)

    # 3. Rödlista ur species_taxon.json (redan hämtad av artportalen.py)
    redlist = redlist_from_taxon_cache()
    if verbose:
        print(f"  Rödlista: {len(redlist)} arter med noterbar kategori")

    species = {}
    for sci in names:
        prev = old_species.get(sci) or {}
        fresh = avonet.get(sci)
        entry = {}

        if fresh is not None:
            # Arten finns i AVONET-läsningen: de fält AVONET äger ska komma DÄRIFRÅN
            # och ingen annanstans. Att ärva dem från förra bygget vore en fälla –
            # ändrar man en tröskel (t.ex. HWI_LONG_POINTED) ska en art som inte
            # längre kvalificerar TAPPA sitt värde, inte behålla det gamla. Samma
            # felklass som den förgiftade GBIF-cachen: en gång skrivet, för evigt sant.
            entry.update({k: v for k, v in prev.items() if k in PRESERVED_FIELDS})
            entry.update(fresh)
        else:
            # Ingen AVONET-träff denna körning – behåll allt vi hade, annars raderar
            # ett namnglapp arbete vi redan gjort.
            entry.update(prev)
        # Latinet stannar i cachen som _familj_latin/_ordning_latin (bra vid
        # ombygge och felsökning) men går aldrig till prompten – facts_for()
        # exponerar bara SPOKEN_FIELDS.
        for latin_key, sv_key in (("_familj_latin", "familj"),
                                  ("_ordning_latin", "ordning")):
            lat = entry.get(latin_key)
            if lat and labels.get(lat):
                entry[sv_key] = labels[lat]
        if sci in redlist:
            entry["rodlista"] = redlist[sci]
        else:
            entry.pop("rodlista", None)
        if entry:
            species[sci] = entry

    # Behåll arter som fanns förut men inte fanns i den här körningens namnlista.
    for sci, val in old_species.items():
        species.setdefault(sci, val)

    cache = {
        "_meta": {
            "version": 1,
            "byggd": _today().isoformat(),
            "antal_arter": len(species),
            "kallor": {
                "avonet": ATTRIBUTION_AVONET,
                "wikidata": ATTRIBUTION_WIKIDATA,
                "rodlista": "SLU Artdatabanken (Artfakta), via species_taxon.json.",
            },
        },
    }
    cache.update(species)
    _save_json(FACTS_CACHE, cache)
    if verbose:
        print(f"  {len(species)} arter -> {FACTS_CACHE}")
    return cache


# ---------------------------------------------------------------------------
# Daglig drift: läs cachen (INGA nätanrop)
# ---------------------------------------------------------------------------
def _species_only(cache):
    return {k: v for k, v in (cache or {}).items()
            if not k.startswith("_") and isinstance(v, dict)}


def _stem(word):
    """Grov stam för svensk plural/singular-jämförelse: "snäppor" -> "snäpp",
    "strandskator" -> "strandskat", "hägrar" -> "hägr". Trubbigt men räcker för att
    upptäcka att familjenamnet är artnamnet igen."""
    w = (word or "").strip().lower()
    for slut in ("orna", "arna", "erna", "or", "ar", "er", "na", "n", "r"):
        if len(w) > len(slut) + 3 and w.endswith(slut):
            return w[: -len(slut)]
    return w


def _familj_ar_cirkular(familj, art):
    """True när familjenamnet bara upprepar artnamnet: "kärrsnäppan hör till
    snäpporna", "strandskatan hör till strandskatornas familj", "storken hör till
    storkarna". Sådana påståenden låter som kunskap men är en rundgång.

    DETTA LÅG FÖRST I PROMPTEN och ignorerades TVÅ gånger av modellen (strandskata
    2026-07-29, kärrsnäppa 2026-07-31). Regeln är mekaniskt kontrollerbar, så den
    hör i koden – en regel som inte följs två gånger ska inte bo i en prompt."""
    f, a = _stem(familj), (art or "").strip().lower()
    if not f or not a or len(f) < 4:
        return False
    return f in a.replace(" ", "")


def familj_for(scientific):
    """Familjenamnet för ETT vetenskapligt namn, rakt ur cachen. None om okänt.

    Skiljer sig från facts_for() på en punkt: cirkulära familjenamn undertrycks
    INTE här. Funktionen finns för familjegrupperingen i generate_report, som
    räknar upp arterna i gruppen ("tre arter ur familjen måsfåglar: ...") – och
    en uppräkning kan inte bli en rundgång, eftersom den säger något nytt om
    dygnet i stället för att bara böja om artnamnet.

    Aldrig nätanrop, aldrig gissning."""
    sci = (scientific or "").strip()
    if not sci:
        return None
    cache = _load_json(FACTS_CACHE)
    if not cache:
        return None
    rec = _species_only(cache).get(ALIASES.get(sci, sci)) or \
        _species_only(cache).get(sci)
    return (rec or {}).get("familj") or None


def facts_for(today_species):
    """Verifierad artfakta för dagens arter, läst enbart ur cachen.

    Returnerar en lista dictar med svenskt visningsnamn + de fält vi faktiskt har.
    `vikt_g` utelämnas medvetet – den talas aldrig ut, se comparisons(). Tyst tom
    lista om cachen saknas. Aldrig nätanrop, aldrig gissning."""
    cache = _load_json(FACTS_CACHE)
    if not cache:
        return []
    species = _species_only(cache)

    out = []
    for s in today_species or []:
        sci = (s.get("scientific") or "").strip()
        if not sci:
            continue
        rec = species.get(sci)
        if not rec:
            continue
        art = s.get("display") or s.get("name") or sci
        entry = {"art": art}
        for f in SPOKEN_FIELDS:
            if not rec.get(f):
                continue
            # Skicka aldrig in ett familjenamn som bara upprepar artnamnet.
            if f == "familj" and _familj_ar_cirkular(rec[f], art):
                continue
            entry[f] = rec[f]
        if rec.get("rodlista"):
            entry["rodlista"] = rec["rodlista"]
        # Bara arter där vi har något utöver namnet är värda att skicka in.
        if len(entry) > 1:
            out.append(entry)
    return out


# ---------------------------------------------------------------------------
# Härledda jämförelser över dygnets artlista
#
# Dessa är SANNA PER KONSTRUKTION – uträknade, inte hämtade, precis som streaks
# och artrikedom_kontext. Superlativ ur modellens minne är den farligaste
# faktaklassen; superlativ uträknade ur datan är riskfria, och ingen annan podd kan
# säga dem eftersom de kräver just den dagens artlista.
#
# Allt rundas av till hinkar och relationer. Aldrig gram, aldrig procent.
# ---------------------------------------------------------------------------
MIN_FOR_EXTREMES = 3      # minsta antal arter med vikt för att lyfta minsta/tyngsta
BEAK_MARGIN      = 1.15   # kraftigaste näbben måste slå tvåan med 15 % för att nämnas
MIN_FOR_SHARE    = 5      # minsta antal arter för att uttala sig om andelar
MIN_COVERAGE     = 0.6    # andel av dygnets arter som måste ha fältet


def _share_bucket(part, whole):
    """Grov, talbar andel. None om underlaget är för tunt att uttala sig om."""
    if not whole:
        return None
    frac = part / whole
    if frac >= 0.9:
        return "så gott som alla"
    if frac >= 0.7:
        return "de flesta"
    if frac >= 0.55:
        return "drygt hälften"
    if frac >= 0.45:
        return "ungefär hälften"
    if frac >= 0.2:
        return "en del"
    if frac > 0:
        return "ett fåtal"
    return None


def comparisons(today_species):
    """Jämförelser över dygnets arter, härledda ur cachen. Tom dict när underlaget
    är för tunt – hellre tyst än missvisande."""
    cache = _load_json(FACTS_CACHE)
    if not cache:
        return {}
    species = _species_only(cache)

    rows = []
    for s in today_species or []:
        sci = (s.get("scientific") or "").strip()
        rec = species.get(sci) if sci else None
        if rec:
            rows.append((s.get("display") or s.get("name") or sci, rec))
    if not rows:
        return {}

    out = {}

    # Minsta och tyngsta art. Kräver att extremvärdet är ENTYDIGT – vid delad
    # plats sägs inget, annars blir påståendet falskt.
    weighed = [(art, rec["vikt_g"]) for art, rec in rows if rec.get("vikt_g")]
    if len(weighed) >= MIN_FOR_EXTREMES:
        weighed.sort(key=lambda t: t[1])
        if weighed[0][1] < weighed[1][1]:
            out["minsta_art"] = weighed[0][0]
        if weighed[-1][1] > weighed[-2][1]:
            out["tyngsta_art"] = weighed[-1][0]

    # Kraftigaste näbben bland dygnets arter, ur näbbdjup. Kräver att vinnaren är
    # ENTYDIG med marginal – två arter på 18,5 och 18,2 mm är i praktiken lika, och
    # då vore påståendet en skenprecision. 15 % marginal mot tvåan krävs.
    beaks = [(art, rec["_nabbdjup"]) for art, rec in rows if rec.get("_nabbdjup")]
    if len(beaks) >= MIN_FOR_EXTREMES:
        beaks.sort(key=lambda t: t[1], reverse=True)
        if beaks[0][1] >= beaks[1][1] * BEAK_MARGIN:
            out["kraftigaste_nabben"] = beaks[0][0]

    # (Signalen "andel flyttfåglar" är BORTTAGEN 2026-07-28. Den byggde på AVONETs
    # globala Migration-fält, som är fel för svenska förhållanden – ett aggregat av
    # opålitliga värden är inte mer pålitligt än värdena. Kommer tillbaka om
    # årstidsfältet härleds ur SOS, se MIGRATION_SV.)

    # Dominerande kosthållning, om någon kategori verkligen dominerar.
    kost = [rec["kosthallning"] for _, rec in rows if rec.get("kosthallning")]
    if len(rows) >= MIN_FOR_SHARE and len(kost) / len(rows) >= MIN_COVERAGE:
        counts = {}
        for k in kost:
            counts[k] = counts.get(k, 0) + 1
        top, n = max(counts.items(), key=lambda t: t[1])
        if n / len(kost) >= 0.5:
            out["vanligaste_kosthallning"] = top

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
    """Alla vetenskapliga namn vi någonsin hört. Samma källa som artportalen.py
    använder, så cacherna täcker samma artmängd."""
    hist = _load_json(path) or {}
    names = set(hist.get("species_ever", {}).keys())
    for day in hist.get("recent_days", []):
        for t in day.get("top", []):
            if t.get("sci"):
                names.add(t["sci"])
    return sorted(n for n in names if n)


def _cmd_build(skip_wikidata=False, skip_avonet=False, only_history=False):
    # Standard: bygg för alla arter i AVONET. only_history ger en mager cache med
    # bara de arter vi hittills hört (går snabbare, men nya arter blir faktafria).
    names = _history_scientific_names() if only_history else None
    if only_history and not names:
        print("Inga arter i history.json – inget att bygga.", file=sys.stderr)
        return 1
    if skip_avonet:
        # Bara översättningen: läs befintlig cache och fyll på svenska familj-
        # och ordningsnamn ur latinet som redan ligger där. Kräver att ett
        # AVONET-bygge körts först (latinet kommer därifrån).
        existing = _load_json(FACTS_CACHE) or {}
        species = _species_only(existing)
        latin = {rec[k] for rec in species.values()
                 for k in ("_familj_latin", "_ordning_latin") if rec.get(k)}
        if not latin:
            print("Inga latinska familj-/ordningsnamn i cachen – kör "
                  "'build-avonet' först.", file=sys.stderr)
            return 1
        print(f"Översätter {len(latin)} taxonnamn till svenska ...")
        labels = lookup_swedish_labels(latin, verbose=True)
        for rec in species.values():
            for latin_key, sv_key in (("_familj_latin", "familj"),
                                      ("_ordning_latin", "ordning")):
                lat = rec.get(latin_key)
                if lat and labels.get(lat):
                    rec[sv_key] = labels[lat]
        cache = {"_meta": (existing.get("_meta") or {})}
        cache["_meta"]["byggd"] = _today().isoformat()
        cache.update(species)
        _save_json(FACTS_CACHE, cache)
        print(f"  {len(species)} arter, {len(labels)}/{len(latin)} taxonnamn "
              f"översatta -> {FACTS_CACHE}")
        return 0
    build(names, verbose=True, skip_wikidata=skip_wikidata)
    return 0


def _cmd_show():
    cache = _load_json(FACTS_CACHE) or {}
    species = _species_only(cache)
    meta = cache.get("_meta") or {}
    print(f"species_facts.json: {len(species)} arter, byggd {meta.get('byggd')}")
    if not species:
        print("  (tom – kör 'build' först)")
        return 0

    # Täckning per fält: visar direkt om en källa inte gick igenom.
    print("\nTäckning:")
    for f in SPOKEN_FIELDS + ("vikt_g", "rodlista"):
        n = sum(1 for rec in species.values() if rec.get(f))
        print(f"  {f:14} {n:3}/{len(species)}")

    print("\nPer art:")
    for sci, rec in sorted(species.items()):
        bits = [f"{f}={rec[f]}" for f in SPOKEN_FIELDS if rec.get(f)]
        if rec.get("vikt_g"):
            bits.append(f"{rec['vikt_g']} g")
        if rec.get("rodlista"):
            bits.append(f"rödlistad {rec['rodlista']}")
        print(f"  {sci:28} {'; '.join(bits) or '(inget)'}")

    # Simulera en dag med hela artmängden, så jämförelserna går att syna.
    fake = [{"scientific": sci, "display": sci} for sci in species]
    print("\nJämförelser (hela artmängden som testdag):")
    for k, v in (comparisons(fake) or {}).items():
        print(f"  {k}: {v}")
    return 0


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "build"
    if cmd == "build":
        return _cmd_build()
    if cmd == "build-avonet":
        return _cmd_build(skip_wikidata=True)
    if cmd == "build-wikidata":
        return _cmd_build(skip_avonet=True)
    if cmd == "build-history":
        return _cmd_build(only_history=True)
    if cmd == "show":
        return _cmd_show()
    print(__doc__)
    print(f"Okänt kommando: {cmd!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except FileNotFoundError as e:
        print(f"Fel: {e}", file=sys.stderr)
        sys.exit(1)
    except requests.HTTPError as e:
        print(f"HTTP-fel: {e}\n{getattr(e.response, 'text', '')[:400]}", file=sys.stderr)
        sys.exit(1)
    except (RuntimeError, ValueError) as e:
        print(f"Fel: {e}", file=sys.stderr)
        sys.exit(1)
