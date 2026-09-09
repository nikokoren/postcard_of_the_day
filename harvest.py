#!/usr/bin/env python3
"""
Build the postcard pool.

Runs monthly. Crawls every source in SOURCES, keeps the cards that are
openly licensed and worth putting on a small e-ink panel, and writes
pool.json. daily.py never talks to a source -- it only reads the pool --
so a bad day at any one archive cannot blank the screen.

Two caches sit beside the pool and survive between runs:

  dimensions.json  pixel size per card, from IIIF info.json. This is what
                   the orientation axis is built on, so it matters.
  quality.json     (mush, detail, texty) per card, measured by fetching
                   the image at panel size. Budgeted, so each run fills in
                   a bit more of the pool.

Usage:
    python3 harvest.py                  # full crawl
    python3 harvest.py --pages 3        # short crawl, for trying things out
    python3 harvest.py --no-measure     # skip render quality (no Pillow needed)
    python3 harvest.py --report         # describe the pool we already have
"""

import argparse
import collections
import http.client
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
POOL_PATH = os.path.join(HERE, "pool.json")
QUALITY_PATH = os.path.join(HERE, "quality.json")
DIMS_PATH = os.path.join(HERE, "dimensions.json")

UA = ("postcard-of-the-day/1.0 "
      "(+https://github.com/nikokoren/postcard_of_the_day)")

TIMEOUT = 90
RETRIES = 5

# A paged crawl of 36,000 records will drop connections; loc.gov in
# particular truncates a 500KB response often enough to matter. One bad
# page is not a reason to abandon a source and lose the other 350, so
# pages are skipped and only a run of them stops the crawl.
MAX_PAGE_FAILURES = 8   # consecutive
REQUEST_DELAY = 1.0     # between search pages
DIMS_WORKERS = 6        # parallel info.json fetches
DIMS_BUDGET = 20000     # info.json fetches per run
MEASURE_BUDGET = 900    # image fetches per run, for render quality
POOL_VERSION = 1


# ============================================================
# what counts as a postcard worth showing
# ============================================================

# A card has to be a real scan, not a thumbnail. Digital Commonwealth
# masters run to 4000px; anything under this is a web copy that will
# look soft on the 1872x1404 X panel.
MIN_SHORT_SIDE = 900
MIN_PIXELS = 700_000

# Some archives publish fixed-size derivatives instead of a IIIF endpoint.
# The Library of Congress postcard collection tops out at a 1024px JPEG,
# which fills an 800x480 OG panel outright and upscales acceptably on the
# 1872x1404 X. Below this it is not worth showing.
MIN_LONG_SIDE_FIXED = 1000

# Postcards are close to 1.4-1.6 one way or the other. Anything outside
# this is a folder, a strip of several cards, or a scan of both sides
# side by side -- none of which read as a postcard on a panel.
MIN_ASPECT = 0.52
MAX_ASPECT = 1.95

# Cards from before photography was printable, or from after the medium
# stopped being interesting, are out.
MIN_YEAR = 1860
MAX_YEAR = 1975

# A title that is only the word "postcard" in some language tells the
# viewer nothing, and the whole point of the caption is to say what they
# are looking at.
GENERIC_TITLE = re.compile(
    r"^\W*(?:"
    r"post\s?card|postkarte|postkaart|postkort|postikaart|briefkaart|"
    r"carte\s?postale|cartolina|tarjeta\s?postal|ansichtskarte|"
    r"vykort|k[ao]rtti|pohlednice|pocztowka|otkritka|"
    r"unidentified|untitled|no\s?title|\[?n\.?\s?t\.?\]?"
    r")\W*(?:\d+)?\W*$",
    re.I)

# The back of a card, an envelope, or a page of an album. All real, none
# of them the picture side.
TITLE_REJECT_RE = re.compile(
    r"\b(?:address side|back of|reverse of|verso|envelope|cover sheet|"
    r"album page|scrapbook page|index|blank)\b", re.I)

TITLE_LIMIT = 240
PER_COUNTRY_CAP = 6000

# Render quality, calibrated on 2-bit greyscale -- what the panels
# actually show. See README.
UNREADABLE_DETAIL = 14.0    # a flat, empty scan
TEXT_RATIO = 3.0            # the address side got in anyway
MEASURE_SIZE = "!800,480"


# ============================================================
# sources
# ============================================================

SOURCES = [
    # (key, kind, label, config)
    ("loc-postcards", "loc_search", "Library of Congress",
     {"path": "photos", "fa": "subject:postcards",
      "collection": "Prints and Photographs Division"}),
    ("dc-open", "digital_commonwealth",
     "Digital Commonwealth", {"reuse": "no restrictions"}),
    ("dc-cc", "digital_commonwealth",
     "Digital Commonwealth", {"reuse": "creative commons"}),
]

# Licences that let a screen show the card. NC and ND are dropped: a
# panel in a living room is arguably neither commercial nor a derivative,
# but "arguably" is not a licence and there is plenty without them.
LICENCE_OK = re.compile(
    r"(publicdomain|/zero/|no known|no copyright|"
    r"licenses/by/|licenses/by-sa/)", re.I)
LICENCE_BAD = re.compile(r"(-nc|-nd|noncommercial|noderiv)", re.I)


# ============================================================
# http
# ============================================================

def fetch(url, accept="application/json", raw=False, tries=RETRIES):
    """GET with backoff. Returns None if it never works."""
    delay = 4
    for attempt in range(tries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": UA, "Accept": accept})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                body = resp.read()
            return body if raw else json.loads(body.decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504):
                sys.stderr.write(f"  http {e.code}, retry in {delay}s\n")
            else:
                return None
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError,
                ConnectionError, OSError, ValueError) as e:
            # Truncated responses are ordinary on a long paged crawl.
            sys.stderr.write(f"  {type(e).__name__}: {e}, retry in {delay}s\n")
        if attempt < tries - 1:
            time.sleep(delay)
            delay *= 2
    return None


# ============================================================
# text
# ============================================================

def squash(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()


def tidy_title(text):
    """
    Cards were catalogued from what is printed on them, so titles arrive
    SHOUTING as often as not. Sentence-case anything that is more than
    half capitals, leaving acronyms and roman numerals alone.
    """
    title = squash(text)
    title = re.sub(r"^[\[\(]|[\]\)]$", "", title).strip()
    letters = [c for c in title if c.isalpha()]
    if letters and sum(c.isupper() for c in letters) / len(letters) > 0.7:
        words = []
        for word in title.split():
            if len(word) <= 3 and word.isupper():
                words.append(word)          # US, SS, XIV
            else:
                words.append(word.capitalize() if word.isupper()
                             else word[:1].upper() + word[1:].lower())
        title = " ".join(words)
    return title[:TITLE_LIMIT].strip(" .,;:-")


def usable_title(title):
    if len(title) < 3:
        return False
    if GENERIC_TITLE.match(title):
        return False
    if TITLE_REJECT_RE.search(title):
        return False
    return True


# ============================================================
# countries
# ============================================================

# The constituent countries fragment a selector nobody wants fragmented,
# and the historical names are what the cards were catalogued under.
COUNTRY_ALIASES = {
    "england": "United Kingdom", "scotland": "United Kingdom",
    "wales": "United Kingdom", "northern ireland": "United Kingdom",
    "great britain": "United Kingdom", "united kingdom": "United Kingdom",
    "usa": "United States", "united states of america": "United States",
    "u.s.": "United States", "united states": "United States",
    "deutschland": "Germany", "west germany": "Germany",
    "east germany": "Germany", "german empire": "Germany",
    "czechoslovakia": "Czech Republic", "czechia": "Czech Republic",
    "ussr": "Russia", "soviet union": "Russia",
    "burma": "Myanmar", "siam": "Thailand", "persia": "Iran",
    "ceylon": "Sri Lanka", "rhodesia": "Zimbabwe",
    "zaire": "Democratic Republic of the Congo",
    "belgian congo": "Democratic Republic of the Congo",
    "democratic republic of congo": "Democratic Republic of the Congo",
    "holland": "Netherlands", "the netherlands": "Netherlands",
    "ottoman empire": "Turkey",
    "congo (democratic republic)": "Democratic Republic of the Congo",
    "congo (brazzaville)": "Republic of the Congo",
    "korea (north)": "North Korea", "korea (south)": "South Korea",
    "vietnam (democratic republic)": "Vietnam",
    "yugoslavia": "Serbia", "palestine": "Israel",
    "german democratic republic": "Germany",
}

# "united states" -> "United States", but "isle of man" -> "Isle of Man".
COUNTRY_SMALL_WORDS = {"of", "the", "and", "de", "du", "da", "di"}


# Where a card is from, one level up. Countries are the axis a reader
# reaches for first, but most of them are thin -- outside the United
# States, France and Italy the counts fall away fast, and thinner still
# once orientation and era cut across them. A region is always deep
# enough to fill a year, so the selector offers both and the markup
# falls back from one to the other.
#
# These are postcard regions, not strict continents. The Middle East is
# split out because "views of the Holy Land" is its own publishing genre
# and a reader looking for it will not look under Asia; North Africa
# stays in Africa because that is where the cards were catalogued.
REGIONS = [
    ("north-america",  "North America"),
    ("latin-america",  "Latin America & the Caribbean"),
    ("europe",         "Europe"),
    ("africa",         "Africa"),
    ("middle-east",    "Middle East"),
    ("asia",           "Asia"),
    ("oceania",        "Oceania"),
]

COUNTRY_REGION = {}


def _region(slug, countries):
    for country in countries:
        COUNTRY_REGION[country.lower()] = slug


_region("north-america", [
    "United States", "Canada", "Greenland", "Bermuda", "Saint Pierre and Miquelon",
])
_region("latin-america", [
    "Mexico", "Guatemala", "Belize", "Honduras", "El Salvador", "Nicaragua",
    "Costa Rica", "Panama", "Cuba", "Jamaica", "Haiti", "Dominican Republic",
    "Puerto Rico", "Bahamas", "Barbados", "Trinidad and Tobago", "Aruba",
    "Curacao", "Martinique", "Guadeloupe", "Saint Lucia", "Grenada",
    "Antigua and Barbuda", "Dominica", "Saint Kitts and Nevis", "Virgin Islands",
    "Colombia", "Venezuela", "Ecuador", "Peru", "Bolivia", "Chile", "Argentina",
    "Uruguay", "Paraguay", "Brazil", "Guyana", "Suriname", "French Guiana",
    "British West Indies",
])
_region("europe", [
    "United Kingdom", "Ireland", "France", "Germany", "Italy", "Spain",
    "Portugal", "Netherlands", "Belgium", "Luxembourg", "Switzerland",
    "Austria", "Denmark", "Norway", "Sweden", "Finland", "Iceland",
    "Poland", "Czech Republic", "Slovakia", "Hungary", "Romania", "Bulgaria",
    "Greece", "Albania", "Serbia", "Croatia", "Slovenia", "Bosnia and Herzegovina",
    "Montenegro", "North Macedonia", "Kosovo", "Estonia", "Latvia", "Lithuania",
    "Belarus", "Ukraine", "Moldova", "Russia", "Malta", "Monaco", "Andorra",
    "San Marino", "Liechtenstein", "Vatican City", "Gibraltar", "Cyprus",
    "Channel Islands", "Isle of Man", "Faroe Islands", "East Prussia",
])
_region("africa", [
    "Morocco", "Algeria", "Tunisia", "Libya", "Egypt", "Sudan", "South Sudan",
    "Ethiopia", "Eritrea", "Djibouti", "Somalia", "Kenya", "Uganda", "Tanzania",
    "Rwanda", "Burundi", "Democratic Republic of the Congo", "Republic of the Congo",
    "Gabon", "Equatorial Guinea", "Cameroon", "Central African Republic", "Chad",
    "Niger", "Nigeria", "Benin", "Togo", "Ghana", "Ivory Coast", "Liberia",
    "Sierra Leone", "Guinea", "Guinea-Bissau", "Senegal", "Gambia", "Mali",
    "Burkina Faso", "Mauritania", "Cape Verde", "Angola", "Zambia", "Zimbabwe",
    "Malawi", "Mozambique", "Botswana", "Namibia", "South Africa", "Lesotho",
    "Eswatini", "Madagascar", "Mauritius", "Seychelles", "Comoros",
    "Sao Tome and Principe", "Western Sahara",
])
_region("middle-east", [
    "Turkey", "Syria", "Lebanon", "Israel", "Jordan", "Iraq", "Iran",
    "Saudi Arabia", "Yemen", "Oman", "United Arab Emirates", "Qatar",
    "Bahrain", "Kuwait", "Afghanistan",
])
_region("asia", [
    "China", "Japan", "North Korea", "South Korea", "Mongolia", "Taiwan",
    "Hong Kong", "Macau", "Vietnam", "Laos", "Cambodia", "Thailand", "Myanmar",
    "Malaysia", "Singapore", "Indonesia", "Philippines", "Brunei", "East Timor",
    "India", "Pakistan", "Bangladesh", "Nepal", "Bhutan", "Sri Lanka",
    "Maldives", "Kazakhstan", "Uzbekistan", "Turkmenistan", "Kyrgyzstan",
    "Tajikistan", "Georgia", "Armenia", "Azerbaijan",
])
_region("oceania", [
    "Australia", "New Zealand", "Fiji", "Papua New Guinea", "Samoa", "Tonga",
    "Vanuatu", "Solomon Islands", "New Caledonia", "French Polynesia",
    "Hawaii", "Guam", "Micronesia", "Palau", "Marshall Islands", "Kiribati",
    "Nauru", "Tuvalu", "Cook Islands", "Tahiti",
])

# What Digital Commonwealth calls a continent, in our terms. A card
# whose country we do not recognise can still be placed by this.
CONTINENT_REGION = {
    "north and central america": "north-america",
    "north america": "north-america",
    "central america": "latin-america",
    "south america": "latin-america",
    "caribbean": "latin-america",
    "europe": "europe",
    "africa": "africa",
    "asia": "asia",
    "oceania": "oceania",
    "australia": "oceania",
}

REGION_LABELS = dict(REGIONS)


def region_for(country, continent=None):
    """(slug, label) for a card, or (None, None) if we cannot place it."""
    if country:
        slug = COUNTRY_REGION.get(country.lower())
        if slug:
            return slug, REGION_LABELS[slug]
    if continent:
        slug = CONTINENT_REGION.get(squash(continent).lower())
        if slug:
            return slug, REGION_LABELS[slug]
    return None, None


def normalise_country(name):
    name = squash(name)
    if not name:
        return None
    key = name.lower().strip(" .")
    if key in COUNTRY_ALIASES:
        return COUNTRY_ALIASES[key]
    # Title-case anything that arrived lowercase, keeping small words small.
    if name.islower():
        parts = name.split()
        name = " ".join(p if p in COUNTRY_SMALL_WORDS and i else p.capitalize()
                        for i, p in enumerate(parts))
    return name


def country_slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


# ============================================================
# source: Digital Commonwealth
# ============================================================

DC_SEARCH = "https://www.digitalcommonwealth.org/search.json"
DC_IIIF = "https://iiif.digitalcommonwealth.org/iiif/2/"


def dc_hiergeo(attrs):
    """The structured {continent, country, region, city} blobs."""
    for blob in attrs.get("subject_hiergeo_geojson_ssm") or []:
        try:
            props = json.loads(blob).get("properties") or {}
        except ValueError:
            continue
        if props:
            yield props


def dc_year(attrs):
    start = attrs.get("date_start_dtsi") or ""
    end = attrs.get("date_end_dtsi") or ""
    try:
        y1 = int(start[:4])
    except ValueError:
        return None, None
    try:
        y2 = int(end[:4])
    except ValueError:
        y2 = y1
    return y1, max(y1, y2)


def dc_rights(attrs):
    """(short label, uri, ok?)"""
    statement = squash(attrs.get("rightsstatement_ss"))
    licence = squash(attrs.get("license_ss"))
    uri = squash(attrs.get("rightsstatement_uri_ss"))
    label = statement or licence or squash(attrs.get("rights_ss"))
    blob = " ".join((statement, licence, uri, squash(attrs.get("rights_ss"))))
    ok = bool(LICENCE_OK.search(blob)) and not LICENCE_BAD.search(blob)
    return label, uri, ok


def dc_evaluate(record, source_key, stats):
    attrs = record.get("attributes") or {}

    label, rights_uri, ok = dc_rights(attrs)
    if not ok:
        stats["rights"] += 1
        return None

    key_base = squash(attrs.get("exemplary_image_key_base_ss"))
    if not key_base:
        stats["no image"] += 1
        return None
    iiif_id = key_base.split("/")[-1]

    title = tidy_title(attrs.get("title_info_primary_tsi"))
    if not usable_title(title):
        stats["title"] += 1
        return None

    y1, y2 = dc_year(attrs)
    if y1 is None:
        stats["no date"] += 1
        return None
    if y1 < MIN_YEAR or y1 > MAX_YEAR:
        stats["out of range"] += 1
        return None

    geo = list(dc_hiergeo(attrs))
    country = next((normalise_country(g["country"]) for g in geo
                    if g.get("country")), None)
    continent = next((g["continent"] for g in geo if g.get("continent")), None)
    place = None
    for g in geo:
        bits = [g.get("city"), g.get("region")]
        bits = [squash(b) for b in bits if b]
        if bits:
            place = ", ".join(bits)
            break

    entry = {
        "id": "dc:" + squash(record.get("id") or attrs.get("id")),
        "b": DC_IIIF + iiif_id,
        "k": "iiif",
        "t": title,
        "y": y1,
        "src": "dc",
        "r": label,
        "u": squash(attrs.get("identifier_uri_ss")),
    }
    if y2 and y2 != y1:
        entry["y2"] = y2
    if country:
        entry["cn"] = country
        entry["c"] = country_slug(country)
    if continent:
        # Stored raw. Turning this into a region is daily.py's job, so
        # the country-to-region table can be corrected without a crawl.
        entry["ct"] = squash(continent)
    if place:
        entry["pl"] = place[:80]
    publisher = squash(attrs.get("publisher_tsi"))
    if publisher:
        entry["pub"] = publisher[:80]
    holder = squash(attrs.get("physical_location_ssim", [None])[0]
                    if attrs.get("physical_location_ssim") else None)
    if holder:
        entry["h"] = holder
    collection = (attrs.get("collection_name_ssim") or [None])[0]
    if collection:
        entry["col"] = squash(collection)[:90]
    if rights_uri:
        entry["ru"] = rights_uri
    return entry


def dc_crawl(source_key, config, max_pages, stats):
    params = {
        "f[genre_specific_ssim][]": "Postcards",
        "f[reuse_allowed_ssi][]": config["reuse"],
        "per_page": 100,
    }
    entries, page, failures = [], 1, 0
    while page <= max_pages:
        url = DC_SEARCH + "?" + urllib.parse.urlencode(
            {**params, "page": page}, doseq=True)
        data = fetch(url)
        if data is None:
            failures += 1
            sys.stderr.write(f"  page {page} failed ({failures} so far), skipping\n")
            if failures >= MAX_PAGE_FAILURES:
                sys.stderr.write("  too many failed pages, stopping this source\n")
                break
            page += 1
            time.sleep(REQUEST_DELAY * 3)
            continue
        failures = 0
        rows = data.get("data") or []
        if not rows:
            break
        if page == 1:
            total = ((data.get("meta") or {}).get("pages") or {}).get(
                "total_count")
            sys.stderr.write(f"  {config['reuse']}: {total} records\n")
        for record in rows:
            stats["seen"] += 1
            entry = dc_evaluate(record, source_key, stats)
            if entry:
                entries.append(entry)
        page += 1
        time.sleep(REQUEST_DELAY)
    return entries


# ============================================================
# source: a Library of Congress collection
# ============================================================

LOC_IIIF = "https://tile.loc.gov/image-services/iiif/"
LOC_SERVICE_RE = re.compile(r"/image-services/iiif/([^/]+)/")
LOC_DIMS_RE = re.compile(r"#h=(\d+)&w=(\d+)")
LOC_PCT_RE = re.compile(r"/full/pct:([\d.]+)/")
YEAR_RE = re.compile(r"\b(1[89]\d\d|20\d\d)\b")

# A record only counts if the catalogue calls it a postcard. The genre
# terms carry their own date range -- "Postcards--1900-1910" -- and some
# older records carry none at all, in which case the collection they sit
# in has to say it.
POSTCARD_GENRE = re.compile(r"^postcard", re.I)
POSTCARD_PARTOF = re.compile(r"postcard", re.I)


def loc_year(record):
    for value in (record.get("dates") or []) + [record.get("date")]:
        m = YEAR_RE.search(str(value or ""))
        if m:
            return int(m.group(1))
    # Failing that, the genre term often is the date: Postcards--1900-1910.
    for genre in (record.get("item") or {}).get("genre") or []:
        m = YEAR_RE.search(str(genre))
        if m:
            return int(m.group(1))
    return None


def loc_is_postcard(record):
    genres = (record.get("item") or {}).get("genre") or []
    if any(POSTCARD_GENRE.match(str(g).strip()) for g in genres):
        return True
    if genres:
        return False        # catalogued, and catalogued as something else
    return any(POSTCARD_PARTOF.search(str(p)) for p in record.get("partof") or [])


def loc_image(record):
    """
    (kind, base-or-url, width, height).

    Two shapes turn up. Digitised-from-the-original items get a IIIF
    endpoint and every size is ours for the asking. The Prints and
    Photographs postcard files get a ladder of fixed JPEGs instead --
    _150px, r (640), v (1024) -- and the top rung is all there is.
    """
    iiif_service = None
    iiif_native = None
    best_fixed = None
    for url in record.get("image_url") or []:
        dims = LOC_DIMS_RE.search(url)
        size = ((int(dims.group(2)), int(dims.group(1))) if dims else None)
        m = LOC_SERVICE_RE.search(url)
        if m:
            iiif_service = iiif_service or m.group(1)
            pct = LOC_PCT_RE.search(url)
            if size and pct:
                scale = float(pct.group(1)) / 100.0
                if scale > 0:
                    native = (round(size[0] / scale), round(size[1] / scale))
                    if not iiif_native or native[0] > iiif_native[0]:
                        iiif_native = native
        elif size:
            plain = url.split("#")[0]
            if not best_fixed or max(size) > max(best_fixed[1]):
                best_fixed = (plain, size)

    if iiif_service:
        w, h = iiif_native or (None, None)
        return "iiif", LOC_IIIF + iiif_service, w, h
    if best_fixed:
        return "fixed", best_fixed[0], best_fixed[1][0], best_fixed[1][1]
    return None, None, None, None


def loc_evaluate(record, label, collection, stats):
    if record.get("access_restricted"):
        stats["rights"] += 1
        return None
    if not loc_is_postcard(record):
        stats["not a postcard"] += 1
        return None

    kind, base, width, height = loc_image(record)
    if not base:
        stats["no image"] += 1
        return None
    if kind == "fixed" and (not width or max(width, height) < MIN_LONG_SIDE_FIXED):
        stats["too small"] += 1
        return None

    title = tidy_title(record.get("title"))
    if not usable_title(title):
        stats["title"] += 1
        return None

    year = loc_year(record)
    if year is None:
        stats["no date"] += 1
        return None
    if year < MIN_YEAR or year > MAX_YEAR:
        stats["out of range"] += 1
        return None

    country = None
    for name in record.get("location_country") or []:
        country = normalise_country(name)
        if country:
            break

    number = record.get("number_lccn") or []
    entry = {
        "id": "loc:" + squash(number[0] if number else record.get("id")),
        "b": base,
        "k": kind,
        "t": title,
        "y": year,
        "src": "loc",
        "r": "No known restrictions on publication",
        "u": squash(record.get("url")),
        "h": "Library of Congress",
        "col": collection,
    }
    if country:
        entry["cn"] = country
        entry["c"] = country_slug(country)
    if width and height:
        entry["w"], entry["h_px"] = width, height
    return entry


def loc_crawl(source_key, config, max_pages, stats):
    """Either a named collection or a faceted search; both page the same way."""
    if config.get("slug"):
        base = "https://www.loc.gov/collections/{}/".format(config["slug"])
        query = {}
        collection = config["slug"].replace("-", " ").title()
    else:
        base = "https://www.loc.gov/{}/".format(config.get("path", "search"))
        query = {"fa": config["fa"]}
        collection = config.get("collection", "Library of Congress")

    entries, page, failures = [], 1, 0
    while page <= max_pages:
        url = base + "?" + urllib.parse.urlencode(dict(
            query, fo="json", c=100, sp=page, at="results,pagination"))
        data = fetch(url)
        if data is None:
            failures += 1
            sys.stderr.write(f"  page {page} failed ({failures} so far), skipping\n")
            if failures >= MAX_PAGE_FAILURES:
                sys.stderr.write("  too many failed pages, stopping this source\n")
                break
            page += 1
            time.sleep(REQUEST_DELAY * 3)
            continue
        failures = 0
        rows = data.get("results") or []
        if not rows:
            break
        if page == 1:
            total = (data.get("pagination") or {}).get("of")
            sys.stderr.write(f"  {source_key}: {total} records\n")
        for record in rows:
            stats["seen"] += 1
            entry = loc_evaluate(record, source_key, collection, stats)
            if entry:
                entries.append(entry)
        if not (data.get("pagination") or {}).get("next"):
            break
        page += 1
        time.sleep(REQUEST_DELAY)
    return entries


CRAWLERS = {
    "digital_commonwealth": dc_crawl,
    "loc_collection": loc_crawl,
    "loc_search": loc_crawl,
}


# ============================================================
# dimensions -- what the orientation axis is built on
# ============================================================

def load_cache(path):
    try:
        with open(path) as fh:
            return json.load(fh).get("entries") or {}
    except (OSError, ValueError):
        return {}


def save_cache(path, entries, name):
    with open(path, "w") as fh:
        json.dump({"version": 1, name: len(entries), "entries": entries},
                  fh, separators=(",", ":"), sort_keys=True)
        fh.write("\n")


def fetch_dimensions(base):
    """(width, height) from IIIF info.json, or None."""
    info = fetch(base + "/info.json", tries=2)
    if not isinstance(info, dict):
        return None
    try:
        return int(info["width"]), int(info["height"])
    except (KeyError, TypeError, ValueError):
        return None


def fill_dimensions(entries, cache, budget):
    """
    Ask each card's IIIF endpoint how big it is. 883 bytes a card, and it
    is the only way to know which way up a postcard is, so it is worth
    the requests. Cached between runs; a run that hits the budget leaves
    the rest for next month.
    """
    todo = [e for e in entries
            if e.get("k") == "iiif" and e["id"] not in cache
            and not (e.get("w") and e.get("h_px"))]
    todo = todo[:budget]
    if not todo:
        return 0
    sys.stderr.write(f"  measuring {len(todo)} of {len(entries)} cards\n")
    done = 0
    with ThreadPoolExecutor(max_workers=DIMS_WORKERS) as pool:
        for entry, dims in zip(todo, pool.map(
                lambda e: fetch_dimensions(e["b"]), todo)):
            cache[entry["id"]] = list(dims) if dims else None
            done += 1
            if done % 1000 == 0:
                sys.stderr.write(f"    {done}/{len(todo)}\n")
    return done


def apply_dimensions(entries, cache, stats):
    """Attach size and orientation; drop cards that are too small or oddly shaped."""
    kept = []
    for entry in entries:
        dims = cache.get(entry["id"])
        if dims is None and entry.get("w") and entry.get("h_px"):
            dims = [entry["w"], entry["h_px"]]
        if not dims:
            stats["unmeasured"] += 1
            continue
        width, height = dims
        if not width or not height:
            stats["unmeasured"] += 1
            continue
        if entry.get("k") == "fixed":
            if max(width, height) < MIN_LONG_SIDE_FIXED:
                stats["too small"] += 1
                continue
        elif min(width, height) < MIN_SHORT_SIDE or width * height < MIN_PIXELS:
            stats["too small"] += 1
            continue
        aspect = width / float(height)
        if aspect < MIN_ASPECT or aspect > MAX_ASPECT:
            stats["odd shape"] += 1
            continue
        entry["w"], entry["h_px"] = width, height
        entry["o"] = "landscape" if width >= height else "portrait"
        kept.append(entry)
    return kept


# ============================================================
# render quality
# ============================================================

def measure(entry):
    """(mush, detail, texty) for one card, or None if it could not be fetched."""
    try:
        from PIL import Image, ImageFilter
    except ImportError:
        return None
    url = (f"{entry['b']}/full/{MEASURE_SIZE}/0/gray.jpg"
           if entry.get("k") == "iiif" else entry["b"])
    raw = fetch(url, accept="image/jpeg", raw=True, tries=2)
    if not raw:
        return None
    try:
        image = Image.open(io.BytesIO(raw)).convert("L")
    except Exception:
        return None
    pixels = list(image.getdata())
    total = len(pixels) or 1
    ink = sum(1 for p in pixels if p < 90) / total
    paper = sum(1 for p in pixels if p > 200) / total
    mush = (1.0 - ink - paper) * 100.0
    edges = image.filter(ImageFilter.FIND_EDGES).getdata()
    detail = sum(edges) / float(len(edges) or 1)

    # Handwriting and printed address panels rule the card into
    # horizontal lines, so darkness alternates far more down the card
    # than across it. A picture side sits near 1.5, a written back at 3
    # and up. This is what keeps the wrong side of a card out.
    width, height = image.size
    load = image.load()
    rows = [sum(load[x, y] for x in range(0, width, 2)) / (width / 2.0)
            for y in range(height)]
    cols = [sum(load[x, y] for y in range(0, height, 2)) / (height / 2.0)
            for x in range(width)]
    row_alt = sum(abs(rows[i + 1] - rows[i])
                  for i in range(len(rows) - 1)) / max(len(rows) - 1, 1)
    col_alt = sum(abs(cols[i + 1] - cols[i])
                  for i in range(len(cols) - 1)) / max(len(cols) - 1, 1)
    texty = row_alt / max(col_alt, 0.01)
    return round(mush, 1), round(detail, 1), round(texty, 2)


def readable(score):
    """None means not measured yet, which is not held against a card."""
    if score is None:
        return True
    detail = score[1]
    texty = score[2] if len(score) > 2 else 0.0
    return detail > UNREADABLE_DETAIL and texty < TEXT_RATIO


def fill_quality(entries, cache, budget):
    todo = [e for e in entries if e["id"] not in cache][:budget]
    if not todo:
        return 0
    sys.stderr.write(f"  rendering {len(todo)} cards to score them\n")
    done = 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        for entry, score in zip(todo, pool.map(measure, todo)):
            cache[entry["id"]] = list(score) if score else None
            done += 1
            if done % 100 == 0:
                sys.stderr.write(f"    {done}/{len(todo)}\n")
    return done


# ============================================================
# pool
# ============================================================

def build_pool(max_pages, do_measure):
    stats = collections.Counter()
    entries, seen_ids = [], set()

    for source_key, kind, label, config in SOURCES:
        sys.stderr.write(f"\n{source_key} ({label})\n")
        crawler = CRAWLERS[kind]
        found = crawler(source_key, config, max_pages, stats)
        added = 0
        for entry in found:
            if entry["id"] in seen_ids:
                stats["duplicate"] += 1
                continue
            seen_ids.add(entry["id"])
            entry.setdefault("h", label)
            entries.append(entry)
            added += 1
        sys.stderr.write(f"  kept {added}\n")

    sys.stderr.write("\ndimensions\n")
    dims_cache = load_cache(DIMS_PATH)
    fill_dimensions(entries, dims_cache, DIMS_BUDGET)
    save_cache(DIMS_PATH, dims_cache, "measured")
    entries = apply_dimensions(entries, dims_cache, stats)
    sys.stderr.write(f"  {len(entries)} cards with usable images\n")

    quality = load_cache(QUALITY_PATH)
    if do_measure:
        sys.stderr.write("\nrender quality\n")
        fill_quality(entries, quality, MEASURE_BUDGET)
        save_cache(QUALITY_PATH, quality, "scored")

    kept = []
    for entry in entries:
        score = quality.get(entry["id"])
        if not readable(score):
            stats["unreadable"] += 1
            continue
        if score:
            entry["q"] = score
        kept.append(entry)

    # No one country is allowed to swamp the pool. Cards with a measured
    # score sort first, so the cap falls on the unproven ones.
    by_country = collections.defaultdict(list)
    for entry in kept:
        by_country[entry.get("c") or "unknown"].append(entry)
    final = []
    for slug, group in by_country.items():
        group.sort(key=lambda e: (-(e.get("q") or [0, 0, 0])[1], e["id"]))
        if len(group) > PER_COUNTRY_CAP:
            stats["over cap"] += len(group) - PER_COUNTRY_CAP
        final.extend(group[:PER_COUNTRY_CAP])
    final.sort(key=lambda e: e["id"])

    return final, stats, len(quality), len(dims_cache)


def describe(entries):
    countries = collections.Counter(e.get("cn") or "unknown" for e in entries)
    regions = collections.Counter(
        region_for(e.get("cn"), e.get("ct"))[1] or "unplaced" for e in entries)
    orient = collections.Counter(e.get("o") for e in entries)
    decades = collections.Counter(e["y"] // 10 * 10 for e in entries)
    scored = sum(1 for e in entries if e.get("q"))
    out = [f"{len(entries)} postcards, {scored} scored for render quality",
           "", f"countries ({len(countries)}):"]
    for name, n in countries.most_common(30):
        out.append(f"  {name:32s} {n:6d}")
    out.append("")
    out.append("regions:")
    for name, n in regions.most_common():
        out.append(f"  {name:32s} {n:6d}")
    out.append("")
    out.append("orientation: " + ", ".join(
        f"{k} {v}" for k, v in orient.most_common()))
    out.append("")
    out.append("decades:")
    for decade in sorted(decades):
        out.append(f"  {decade}s {decades[decade]:6d}")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=400,
                    help="max search pages per source (100 records a page)")
    ap.add_argument("--no-measure", action="store_true",
                    help="skip render-quality measurement")
    ap.add_argument("--report", action="store_true",
                    help="describe the existing pool and exit")
    args = ap.parse_args()

    if args.report:
        with open(POOL_PATH) as fh:
            print(describe(json.load(fh)["entries"]))
        return 0

    entries, stats, scored, sized = build_pool(args.pages, not args.no_measure)
    if not entries:
        sys.stderr.write("\nnothing harvested; leaving the old pool alone\n")
        return 1

    payload = {
        "version": POOL_VERSION,
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "count": len(entries),
        "scored": scored,
        "sized": sized,
        "entries": entries,
    }
    with open(POOL_PATH, "w") as fh:
        json.dump(payload, fh, separators=(",", ":"), sort_keys=True)
        fh.write("\n")

    sys.stderr.write("\ndropped:\n")
    for reason, n in stats.most_common():
        sys.stderr.write(f"  {reason:14s} {n}\n")
    sys.stderr.write("\n" + describe(entries) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
