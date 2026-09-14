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
import collections
import html
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
CRAWL_PATH = os.path.join(HERE, "crawl.json")
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
# Two, not six. A IIIF host behind a shared egress will drop
# connections under concurrency long before it runs out of capacity:
# measured against Digital Commonwealth, six workers failed 13 of 20
# requests after 11s each while the seven that got through took 0.6s.
# Fewer workers finish more work.
DIMS_WORKERS = 2        # parallel info.json fetches
DIMS_BUDGET = 2500      # info.json fetches per run
MEASURE_BUDGET = 900    # image fetches per run, for render quality
POOL_VERSION = 1

# A crawl of 60,000 records takes long enough that something will
# interrupt it -- a runner timing out, a process reaped, a network that
# gives up. Losing an hour's paging to that is the difference between a
# refresh that finishes and one that never does, so each source's
# progress is written to crawl.json as it goes and a later run picks up
# from the page it stopped on. A cache older than this is re-crawled
# from the start, because by then the collections have moved.
CRAWL_MAX_AGE_DAYS = 20
CRAWL_SAVE_EVERY = 10


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
    r"unidentified|untitled|no\s?title|ohne\s?titel|senza\s?titolo|"
    r"\[?n\.?\s?t\.?\]?"
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

# The ratio above is a whole-card average, so it only catches a card
# that is text all over. It is blind to the commonest spoiler of the
# lot: a card that is half picture and half small print, an Italian view
# with a column of history set beside it. The picture half pulls the
# average back down and the card sails through -- "Milano. Castello
# Sforzesco. Sala delle Asse" scored 1.17 against a limit of 3.0 and
# went out as a postcard of the day.
#
# So look for the panel itself rather than for a texty card: a tall
# strip of paper ruled into regular lines. Lines of type repeat at a
# fixed pitch and keep repeating for the height of the panel, which is
# what separates them from the things in a photograph that also repeat
# -- masonry courses, balconies, railings, waves -- since those drift
# and die out within a few cycles.
#
# Rhythm alone is not enough, because a brick wall in the right light
# rules a card as neatly as a typesetter does. The second half of the
# test is that the strip be a printed surface: type is a few dark marks
# on one flat tone, so most of the strip sits within a narrow band of
# its own commonest tone. A photograph's tones are spread out -- the
# Philadelphia gateway that scored 0.64 on its brickwork holds only 0.31
# of its strip near one tone, against 0.66 for the Milano panel.
#
# Deliberately measured against the strip's *own* modal tone rather than
# against white: half these scans are sepia or underexposed, and an
# absolute brightness test threw away every dark one. La Brabanconne --
# the Belgian anthem, printed in full, nothing else on the card -- has
# 0.07 of its strip above the usual paper cut and 0.73 of it near its
# own tone.
#
# Calibrated on 1,839 cards drawn at random from the pool. The rule
# rejects 9 of them, 0.49%: three cards that are nothing but a printed
# poem, two portraits with the poem set beside them, a card written
# across in ink, a scan with a photographic step wedge in the frame, a
# board of toll rates, and the Milano card. Nothing that is a picture.
PANEL_RULED = 0.40          # strength of the line rhythm down the strip
PANEL_FLAT = 0.48           # ... on a strip that is a printed surface
PAGE_RULED = 0.30           # a weaker rhythm will do if the card is
PAGE_FLAT = 0.70            # ... a page of print from edge to edge
PANEL_WIDTH = 0.25          # strip width, as a fraction of the card
PANEL_STEP = 0.0625         # and how far it slides each time
PANEL_PITCH = (8, 36)       # plausible line spacing, in pixels
PANEL_TONE = (16, 24)       # how near the modal tone still counts as it
PANEL_WIDE = 900            # measured at this width, so the pitch holds


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
    ("rijksmuseum", "rijksmuseum", "Rijksmuseum",
     {"type": "prentbriefkaart"}),
    ("graz", "gams", "University of Graz",
     {"prefix": "o:gm.", "max_id": 9000,
      "collection": "GrazMuseum Ansichtskarten"}),
]

# Resolving one Rijksmuseum card costs three requests -- the object, the
# visual item it shows, and the digital object that visual item is
# served as -- so the full 16,020 is around 48,000 requests and not
# something to do in one sitting.
# Budgeted and cached like everything else here: each run resolves a few
# thousand more and the pool fills in over a handful of months.
RIJKS_BUDGET = 1500
RIJKS_WORKERS = 2

# GAMS ids are sequential, so the collection is walked rather than
# searched: one Dublin Core record per card, no key and no aggregator in
# between. Gaps are ordinary and 404s are skipped.
GAMS_BUDGET = 3000
GAMS_WORKERS = 4

# Licences that let a screen show the card. NC and ND are dropped: a
# panel in a living room is arguably neither commercial nor a derivative,
# but "arguably" is not a licence and there is plenty without them.
LICENCE_OK = re.compile(
    r"(publicdomain|/zero/|no known|no copyright|"
    r"licenses/by/|licenses/by-sa/)", re.I)
LICENCE_BAD = re.compile(r"(-nc|-nd|noncommercial|noderiv)", re.I)


# ============================================================
# the resumable crawl cache
# ============================================================

def write_json_atomic(path, payload):
    """
    Write via a temp file and rename. A checkpoint exists precisely
    because the process may be interrupted, so writing in place means
    the one event it is defending against is also the one that can
    destroy it -- a crawl killed mid-write left a truncated crawl.json
    and cost 12,147 harvested records. os.replace is atomic on POSIX.
    """
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(payload, fh, separators=(",", ":"), sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)

def load_crawl():
    try:
        with open(CRAWL_PATH) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    fresh = time.time() - CRAWL_MAX_AGE_DAYS * 86400
    return {key: state for key, state in (data.get("sources") or {}).items()
            if state.get("started", 0) >= fresh}


def save_crawl(cache):
    write_json_atomic(CRAWL_PATH, {"version": 1, "sources": cache})


def crawl_state(cache, source_key):
    state = cache.get(source_key)
    if not state:
        state = {"page": 1, "done": False, "entries": [],
                 "started": time.time()}
        cache[source_key] = state
    return state


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


def dc_crawl(source_key, config, max_pages, stats, cache):
    params = {
        "f[genre_specific_ssim][]": "Postcards",
        "f[reuse_allowed_ssi][]": config["reuse"],
        "per_page": 100,
    }
    state = crawl_state(cache, source_key)
    if state["done"]:
        sys.stderr.write(f"  resumed: complete, {len(state['entries'])} cards\n")
        return state["entries"]
    entries, page, failures = state["entries"], state["page"], 0
    if page > 1:
        sys.stderr.write(f"  resuming at page {page}, {len(entries)} cards so far\n")
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
        if page % 10 == 0:
            sys.stderr.write(f"  page {page}, {len(entries)} kept\n")
            sys.stderr.flush()
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
        if page % CRAWL_SAVE_EVERY == 0:
            state["page"] = page
            save_crawl(cache)
        time.sleep(REQUEST_DELAY)
    state["page"], state["done"] = page, True
    save_crawl(cache)
    return entries


# ============================================================
# source: a Library of Congress collection
# ============================================================

LOC_IIIF = "https://tile.loc.gov/image-services/iiif/"
LOC_SERVICE_RE = re.compile(r"/image-services/iiif/([^/]+)/")
LOC_DIMS_RE = re.compile(r"#h=(\d+)&w=(\d+)")
LOC_PCT_RE = re.compile(r"/full/pct:([\d.]+)/")
YEAR_RE = re.compile(r"\b(1[89]\d\d|20\d\d)\b")

# When a card was posted beats when it was printed, and occasionally a
# cataloguer transcribed the postmark: "Postmarked 1905", "Cancelled
# Sierra Leone stamp postmarked 1912". It is about 1% of records, so it
# is not something to build an axis on -- but where it exists it is the
# better answer to "how old is this card", and it is worth saying so on
# the screen.
POSTMARK_RE = re.compile(
    r"(?:post\s?marked|postmark|cancell?ed[^.]{0,40}?)\D{0,12}(1[89]\d\d|20\d\d)",
    re.I)


def loc_postmark(record):
    notes = list(record.get("description") or [])
    notes += list((record.get("item") or {}).get("notes") or [])
    for note in notes:
        m = POSTMARK_RE.search(str(note))
        if m:
            year = int(m.group(1))
            if MIN_YEAR <= year <= MAX_YEAR:
                return year
    return None

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

    postmark = loc_postmark(record)
    year = postmark or loc_year(record)
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
    if postmark:
        # Marked, so the caption can say "Posted 1912" rather than
        # implying a printing date is a posting date.
        entry["pm"] = postmark
    if country:
        entry["cn"] = country
        entry["c"] = country_slug(country)
    if width and height:
        entry["w"], entry["h_px"] = width, height
    return entry


def loc_crawl(source_key, config, max_pages, stats, cache):
    """Either a named collection or a faceted search; both page the same way."""
    if config.get("slug"):
        base = "https://www.loc.gov/collections/{}/".format(config["slug"])
        query = {}
        collection = config["slug"].replace("-", " ").title()
    else:
        base = "https://www.loc.gov/{}/".format(config.get("path", "search"))
        query = {"fa": config["fa"]}
        collection = config.get("collection", "Library of Congress")

    state = crawl_state(cache, source_key)
    if state["done"]:
        sys.stderr.write(f"  resumed: complete, {len(state['entries'])} cards\n")
        return state["entries"]
    entries, page, failures = state["entries"], state["page"], 0
    if page > 1:
        sys.stderr.write(f"  resuming at page {page}, {len(entries)} cards so far\n")
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
        if page % 10 == 0:
            sys.stderr.write(f"  page {page}, {len(entries)} kept\n")
            sys.stderr.flush()
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
        if page % CRAWL_SAVE_EVERY == 0:
            state["page"] = page
            save_crawl(cache)
        time.sleep(REQUEST_DELAY)
    state["page"], state["done"] = page, True
    save_crawl(cache)
    return entries


CRAWLERS = {
    "digital_commonwealth": dc_crawl,
    "loc_collection": loc_crawl,
    "loc_search": loc_crawl,
}


# ============================================================
# source: GAMS, University of Graz
# ============================================================

GAMS_BASE = "https://gams.uni-graz.at"
GAMS_DC = re.compile(r"<dc:(\w+)>(.*?)</dc:\1>", re.S)
GAMS_YEARS = re.compile(r"(1[89]\d\d|20\d\d)")


def gams_dc(object_id):
    """The Dublin Core record, as {element: [values]}."""
    body = fetch(f"{GAMS_BASE}/{object_id}/DC", accept="application/xml",
                 raw=True, tries=2)
    if not body:
        return None
    text = body.decode("utf-8", "replace")
    fields = collections.defaultdict(list)
    for match in GAMS_DC.finditer(text):
        value = squash(html.unescape(match.group(2)))
        if value:
            fields[match.group(1)].append(value)
    return fields


def gams_evaluate(object_id, config, stats):
    fields = gams_dc(object_id)
    if not fields:
        stats["missing"] += 1
        return None
    if not any(t.lower().startswith("postkarte")
               for t in fields.get("type", [])):
        stats["not a postcard"] += 1
        return None

    rights = " ".join(fields.get("rights", []))
    if not LICENCE_OK.search(rights) or LICENCE_BAD.search(rights):
        # A quarter of this collection is CC BY-NC-ND, which the same
        # rule that excluded the British Museum excludes here.
        stats["rights"] += 1
        return None

    title = tidy_title((fields.get("title") or [""])[0])
    if not usable_title(title):
        stats["title"] += 1
        return None

    # Dates come as a year or a range: "1881", "1910-1920", "1905-1910".
    years = [int(y) for y in GAMS_YEARS.findall(" ".join(fields.get("date", [])))]
    if not years:
        stats["no date"] += 1
        return None
    y1, y2 = min(years), max(years)
    if y1 < MIN_YEAR or y1 > MAX_YEAR:
        stats["out of range"] += 1
        return None

    # The GrazMuseum's own holdings, and they are of Graz: in a sample of
    # 40 every card was published by the museum and every title named
    # Graz or somewhere in Styria. So Austria is the default rather than
    # an assumption -- but a title that names somewhere else wins, since
    # a card of Venice bought in Graz is a card of Venice.
    country = "Austria"
    lowered = (title + " " + " ".join(fields.get("subject", []))).lower()
    for name in sorted(COUNTRY_REGION, key=len, reverse=True):
        if name != "austria" and re.search(r"\b" + re.escape(name) + r"\b", lowered):
            country = normalise_country(name)
            break

    entry = {
        "id": "graz:" + object_id,
        # RECTO is the picture side; these cards are scanned front and
        # back, and the back is an address panel.
        "b": "{}/iiif/{}%2FRECTO".format(GAMS_BASE, object_id),
        "k": "iiif",
        "t": title,
        "y": y1,
        "src": "graz",
        "r": (fields.get("rights") or ["CC BY-SA 3.0 AT"])[0],
        "u": "{}/{}".format(GAMS_BASE, object_id),
        "h": "University of Graz",
        "col": config.get("collection", "GAMS"),
        "cn": country,
        "c": country_slug(country),
    }
    if y2 != y1:
        entry["y2"] = y2
    description = (fields.get("description") or [""])[0]
    if description:
        entry["pl"] = description[:80]
    return entry


def gams_crawl(source_key, config, max_pages, stats, cache):
    """Walk the id range, resolving a budget of new cards each run."""
    state = crawl_state(cache, source_key)
    state.setdefault("resolved", {})
    prefix, top = config["prefix"], config["max_id"]

    todo = [prefix + str(i) for i in range(1, top + 1)
            if prefix + str(i) not in state["resolved"]]
    todo = todo[:config.get("budget", GAMS_BUDGET)]
    if todo:
        sys.stderr.write(f"  walking {len(todo)} ids "
                         f"({len(state['resolved'])} seen so far)\n")
        done = 0
        with ThreadPoolExecutor(max_workers=GAMS_WORKERS) as pool:
            for object_id, entry in zip(todo, pool.map(
                    lambda i: gams_evaluate(i, config, stats), todo)):
                state["resolved"][object_id] = entry
                done += 1
                if done % 250 == 0:
                    save_crawl(cache)
                    sys.stderr.write(f"    {done}/{len(todo)}\n")
                    sys.stderr.flush()
        save_crawl(cache)
    if len(state["resolved"]) >= top:
        state["done"] = True
        save_crawl(cache)
    return [e for e in state["resolved"].values() if e]


CRAWLERS["gams"] = gams_crawl


# ============================================================
# source: the Rijksmuseum
# ============================================================

RIJKS_SEARCH = "https://data.rijksmuseum.nl/search/collection"

# Linked Art says what a thing is by pointing at a Getty AAT number
# rather than by naming a field, so these are the three that matter.
AAT_PRIMARY_TITLE = "300404670"
AAT_OBJECT_NUMBER = "300312355"

RIJKS_DIMS = re.compile(r"height\s+(\d+)\s*mm\s*x\s*width\s+(\d+)\s*mm", re.I)

# What a postcard measures, in millimetres, generously. The continental
# standard is 90x140 and A6 is 105x148; early cards run smaller. Outside
# this and it is an album page or a mounted group, not a card.
CARD_MM = (60, 180)
RIJKS_IIIF_TAIL = re.compile(r"/full/[^/]+/\d+/\w+\.jpg$")


def la_names(node, aat=None):
    """The `identified_by` strings on a Linked Art node, optionally by type."""
    out = []
    for name in node.get("identified_by") or []:
        kinds = [c.get("id", "").rsplit("/", 1)[-1]
                 for c in (name.get("classified_as") or [])]
        if aat is None or aat in kinds:
            content = squash(name.get("content"))
            if content:
                out.append(content)
    return out


def rijks_country(record, title):
    """
    What the card shows. These records carry no depicted-place field --
    no `about`, no `represents`, no subject headings -- so it has to be
    read out of the prose, in three places, in this order.

    The first is the production credit, and using it needs justifying,
    because in general a printer's country says nothing about a card's
    subject: the postcard trade of the 1900s ran on German lithographers
    printing views of Italy and Egypt and everywhere else.

    That argument does not describe this collection, which is the point.
    Sampled across the set, the credits read "photographer: Knud
    Knudsen, Norway", "publisher: Fujisawa Bunjirô, Japan",
    "photographer, Suriname" -- photographers and local publishers, not
    export lithographers. A photographer's country is where the
    photograph was taken. Checked by eye, the Japanese cards are
    Hiroshige's Tokaido stations, the Surinamese ones are the Paramaribo
    market and the colony's arms, the Norwegian ones are waterfalls and
    hotels. The credit tracks the subject here.

    Germany and Switzerland do appear as producers, and those are the
    ones where the general objection could bite. They are 8 of 106
    matches in the sample, and left in: a wrong region on a handful
    beats no region on two thousand.

    Then the descriptive note, which is straightforwardly about the
    subject, and last the title.
    """
    prose = []
    for part in ((record.get("produced_by") or {}).get("part") or []):
        for note in part.get("referred_to_by") or []:
            prose.append(squash(note.get("content")))
    for note in record.get("referred_to_by") or []:
        prose.append(squash(note.get("content")))

    for text in prose + [title]:
        lowered = (text or "").lower()
        for name in sorted(COUNTRY_REGION, key=len, reverse=True):
            if re.search(r"\b" + re.escape(name) + r"\b", lowered):
                return normalise_country(name)
    return None


def rijks_resolve(object_id, stats):
    """One card, from its Linked Art record and the visual item it shows."""
    record = fetch(object_id, accept="application/ld+json", tries=2)
    if not isinstance(record, dict):
        return None

    title = tidy_title((la_names(record, AAT_PRIMARY_TITLE)
                        or la_names(record) or [""])[0])
    if not usable_title(title):
        stats["title"] += 1
        return None

    span = (record.get("produced_by") or {}).get("timespan") or {}
    def year(key):
        m = YEAR_RE.search(str(span.get(key) or ""))
        return int(m.group(1)) if m else None
    y1, y2 = year("begin_of_the_begin"), year("end_of_the_end")
    if y1 is None:
        stats["no date"] += 1
        return None
    if y1 < MIN_YEAR or y1 > MAX_YEAR:
        stats["out of range"] += 1
        return None

    # The card's own measurements, which do two jobs.
    #
    # They say which way up it is, far more reliably than the shape of
    # somebody's scan does. And they say whether it is a card at all:
    # the Rijksmuseum files boxes, albums and multi-card lots under
    # "prentbriefkaart" too, and those are useless on a panel -- an
    # album page photographs as four stamps of a picture. Measured:
    #
    #   single card   height 90 mm x width 141 mm
    #   box of 55     height 98 mm x width 147 mm x depth 32 mm
    #   album spread  height 199 mm x width 255 mm
    #
    # A depth means a container. Anything outside a postcard's size
    # envelope is a group of them mounted together.
    orientation = None
    for note in record.get("referred_to_by") or []:
        content = str(note.get("content") or "")
        m = RIJKS_DIMS.search(content)
        if not m:
            continue
        if re.search(r"\bdepth\b|\bdiepte\b", content, re.I):
            stats["a box, not a card"] += 1
            return None
        height, width = int(m.group(1)), int(m.group(2))
        if not (CARD_MM[0] <= height <= CARD_MM[1]
                and CARD_MM[0] <= width <= CARD_MM[1]):
            stats["a group, not a card"] += 1
            return None
        if abs(height - width) > 4:
            orientation = "landscape" if width > height else "portrait"
        break
    if not orientation:
        stats["no shape"] += 1
        return None

    shows = (record.get("shows") or [{}])[0].get("id")
    if not shows:
        stats["no image"] += 1
        return None
    # Three records deep, because Linked Art separates the object from
    # the image *of* the object from the file that image is served as:
    #   HumanMadeObject -> shows -> VisualItem
    #   VisualItem      -> digitally_shown_by -> DigitalObject
    #   DigitalObject   -> access_point -> the IIIF endpoint
    # It is tempting to stop at the VisualItem's subject_of, which also
    # carries a digital object -- that one is the catalogue web page.
    visual = fetch(shows, accept="application/ld+json", tries=2)
    iiif = None
    for shown in ((visual or {}).get("digitally_shown_by") or []):
        for point in shown.get("access_point") or []:
            if "iiif" in str(point.get("id") or ""):
                iiif = point["id"]
        if iiif or not shown.get("id"):
            continue
        digital = fetch(shown["id"], accept="application/ld+json", tries=2)
        for point in ((digital or {}).get("access_point") or []):
            if "iiif" in str(point.get("id") or ""):
                iiif = point["id"]
        if iiif:
            break
    if not iiif:
        stats["no image"] += 1
        return None

    number = (la_names(record, AAT_OBJECT_NUMBER) or [""])[0]
    entry = {
        "id": "rijks:" + (number or object_id.rsplit("/", 1)[-1]),
        "b": RIJKS_IIIF_TAIL.sub("", iiif),
        "k": "iiif",
        "t": title,
        "y": y1,
        "o": orientation,
        "src": "rijks",
        "r": "Public Domain (CC0 metadata)",
        "u": object_id,
        "h": "Rijksmuseum",
        "col": "Rijksmuseum",
    }
    if y2 and y2 != y1:
        entry["y2"] = y2
    country = rijks_country(record, title)
    if country:
        entry["cn"] = country
        entry["c"] = country_slug(country)
    return entry


def rijks_crawl(source_key, config, max_pages, stats, cache):
    """
    Two passes. The search API is cheap and only hands back identifiers,
    so that runs to completion; resolving those identifiers into cards
    costs two requests each and runs to a budget.
    """
    state = crawl_state(cache, source_key)
    state.setdefault("ids", [])
    state.setdefault("resolved", {})

    if not state["done"]:
        url = state.get("token") or (RIJKS_SEARCH + "?" + urllib.parse.urlencode(
            {"imageAvailable": "true", "type": config["type"]}))
        page = 0
        while url and page < max_pages:
            data = fetch(url)
            if data is None:
                sys.stderr.write("  search page failed, pausing this source\n")
                break
            if page == 0 and not state["ids"]:
                total = (data.get("partOf") or {}).get("totalItems")
                sys.stderr.write(f"  {source_key}: {total} records\n")
            state["ids"].extend(x["id"] for x in data.get("orderedItems") or [])
            url = data.get("next", {}).get("id") if isinstance(
                data.get("next"), dict) else data.get("next")
            state["token"] = url
            page += 1
            if page % CRAWL_SAVE_EVERY == 0:
                sys.stderr.write(f"  page {page}, {len(state['ids'])} ids\n")
                save_crawl(cache)
            time.sleep(REQUEST_DELAY)
        if not url:
            state["done"] = True
        save_crawl(cache)

    budget = config.get("budget", RIJKS_BUDGET)
    todo = [i for i in state["ids"] if i not in state["resolved"]][:budget]
    if todo:
        sys.stderr.write(f"  resolving {len(todo)} of "
                         f"{len(state['ids']) - len(state['resolved'])} left\n")
        done = 0
        with ThreadPoolExecutor(max_workers=RIJKS_WORKERS) as pool:
            for object_id, entry in zip(todo, pool.map(
                    lambda i: rijks_resolve(i, stats), todo)):
                state["resolved"][object_id] = entry
                done += 1
                if done % 250 == 0:
                    save_crawl(cache)
                    sys.stderr.write(f"    {done}/{len(todo)}\n")
        save_crawl(cache)

    return [e for e in state["resolved"].values() if e]


CRAWLERS["rijksmuseum"] = rijks_crawl


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
    write_json_atomic(path, {"version": 1, name: len(entries),
                             "entries": entries})


def fetch_dimensions(base):
    """(width, height) from IIIF info.json, or None."""
    info = fetch(base + "/info.json", tries=2)
    if not isinstance(info, dict):
        return None
    try:
        return int(info["width"]), int(info["height"])
    except (KeyError, TypeError, ValueError):
        return None


def fill_dimensions(entries, cache, budget, only_source=None):
    """
    Ask each card's IIIF endpoint how big it is. 883 bytes a card, and it
    is the only way to know which way up a postcard is, so it is worth
    the requests. Cached between runs; a run that hits the budget leaves
    the rest for next month.
    """
    todo = [e for e in entries
            if e.get("k") == "iiif" and e["id"] not in cache
            and not (e.get("w") and e.get("h_px"))]
    if only_source:
        # Spend the whole budget on one source. Worth having when a
        # source has just been added, and worth having because hosts
        # differ enormously: Graz answered 50 of 50 info.json requests
        # while Digital Commonwealth was dropping almost all of them, so
        # sharing a budget between them wastes it on the one that fails.
        todo = [e for e in todo if e.get("src") == only_source]

    # Measure the cards most likely to survive first. Most of an
    # unmeasured backlog is American, and the per-country cap throws
    # most of that away unmeasured anyway -- whereas a card from a
    # country with two hundred behind it is one the region axis leans
    # on. So walk the countries round-robin, rarest first: a budget that
    # cannot cover everything then covers the thin countries whole
    # rather than taking a slice off the top of the biggest one.
    groups = collections.defaultdict(list)
    for entry in todo:
        groups[entry.get("c") or "unknown"].append(entry)
    ordered = sorted(groups.values(), key=len)
    todo, depth = [], 0
    while len(todo) < budget and any(len(g) > depth for g in ordered):
        for group in ordered:
            if depth < len(group):
                todo.append(group[depth])
        depth += 1
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
            # Checkpoint. This phase is the long pole of a refresh, and
            # a run that dies two thirds of the way through should leave
            # those two thirds behind for the next one rather than
            # asking for them all over again.
            if done % 250 == 0:
                save_cache(DIMS_PATH, cache, "measured")
                sys.stderr.write(f"    {done}/{len(todo)}\n")
                sys.stderr.flush()
    return done


def apply_dimensions(entries, cache, stats):
    """Attach size and orientation; drop cards that are too small or oddly shaped."""
    kept = []
    for entry in entries:
        # A source that tells us the card's real measurements has given
        # better evidence than the scan's proportions ever could, so it
        # is taken at its word and skips the pixel gate entirely.
        if entry.get("o") and not entry.get("w"):
            kept.append(entry)
            continue
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

def ruled_panel(image):
    """
    (rhythm, flat) for the most text-like strip of a card.

    rhythm is how strongly the brightness down that strip repeats at one
    fixed spacing -- the autocorrelation peak over plausible line
    pitches, after a moving average takes out the slow shading that
    every scan has. flat is how much of the strip sits within a narrow
    band of the strip's own commonest tone.

    Both are needed. Lines of type give rhythm on a flat ground; a
    flight of steps gives rhythm on stone.

    (None, None) when numpy is not installed, which reads downstream as
    not measured rather than as clean.
    """
    try:
        import numpy as np
    except ImportError:
        return None, None
    if image.width != PANEL_WIDE:
        height = max(int(PANEL_WIDE * image.height / image.width), 1)
        image = image.resize((PANEL_WIDE, height))
    card = np.asarray(image, dtype=np.float64)
    height, width = card.shape
    low, high = PANEL_PITCH
    if height < 3 * high:
        return None, None

    window = 19
    strip_w = max(int(width * PANEL_WIDTH), 8)
    step = max(int(width * PANEL_STEP), 1)
    best = (0.0, 0.0)
    for left in range(0, width - strip_w + 1, step):
        strip = card[:, left:left + strip_w]
        down = strip.mean(axis=1)
        down = down - down.mean()
        # Subtract the local mean: a scan that darkens towards one edge
        # correlates with itself at every lag, and would score as type.
        padded = np.pad(down, window // 2, mode="edge")
        down = down - np.convolve(padded, np.ones(window) / window,
                                  mode="valid")[:height]
        energy = float(down @ down) or 1e-9
        rhythm = max(float(down[:height - lag] @ down[lag:]) / energy
                     for lag in range(low, high + 1))
        if rhythm > best[0]:
            bins = np.histogram(strip, bins=32, range=(0, 256))[0]
            tone = int(np.argmax(bins)) * 8
            near, above = PANEL_TONE
            flat = float(((strip >= tone - near) &
                          (strip <= tone + above)).mean())
            best = (rhythm, flat)
    return round(best[0], 3), round(best[1], 3)


def measure(entry):
    """
    (mush, detail, texty, rhythm, flat) for one card, or None if it
    could not be fetched.
    """
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
    rhythm, flat = ruled_panel(image)
    return round(mush, 1), round(detail, 1), round(texty, 2), rhythm, flat


def readable(score):
    """None means not measured yet, which is not held against a card."""
    if score is None:
        return True
    detail = score[1]
    texty = score[2] if len(score) > 2 else 0.0
    if not (detail > UNREADABLE_DETAIL and texty < TEXT_RATIO):
        return False
    rhythm = score[3] if len(score) > 3 else None
    flat = score[4] if len(score) > 4 else None
    if rhythm is None or flat is None:
        return True
    if rhythm >= PANEL_RULED and flat >= PANEL_FLAT:
        return False                       # a ruled panel beside the picture
    return not (rhythm >= PAGE_RULED and flat >= PAGE_FLAT)


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

def build_pool(max_pages, do_measure, dims_budget=DIMS_BUDGET,
               rijks_budget=RIJKS_BUDGET, dims_source=None):
    stats = collections.Counter()
    entries, seen_ids = [], set()

    cache = load_crawl()
    for source_key, kind, label, config in SOURCES:
        sys.stderr.write(f"\n{source_key} ({label})\n")
        crawler = CRAWLERS[kind]
        found = crawler(source_key, dict(config, budget=(
                            rijks_budget if kind == "rijksmuseum"
                            else GAMS_BUDGET)),
                        max_pages, stats, cache)
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
    fill_dimensions(entries, dims_cache, dims_budget, dims_source)
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
    ap.add_argument("--dims-budget", type=int, default=DIMS_BUDGET,
                    help="info.json lookups this run (the orientation axis)")
    ap.add_argument("--rijks-budget", type=int, default=RIJKS_BUDGET,
                    help="Rijksmuseum cards to resolve this run")
    ap.add_argument("--dims-source",
                    help="spend the whole dimension budget on one source")
    ap.add_argument("--report", action="store_true",
                    help="describe the existing pool and exit")
    args = ap.parse_args()

    if args.report:
        with open(POOL_PATH) as fh:
            print(describe(json.load(fh)["entries"]))
        return 0

    entries, stats, scored, sized = build_pool(
        args.pages, not args.no_measure, args.dims_budget, args.rijks_budget,
        args.dims_source)
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
    write_json_atomic(POOL_PATH, payload)

    sys.stderr.write("\ndropped:\n")
    for reason, n in stats.most_common():
        sys.stderr.write(f"  {reason:14s} {n}\n")
    sys.stderr.write("\n" + describe(entries) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
