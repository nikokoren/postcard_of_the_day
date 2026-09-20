#!/usr/bin/env python3
"""
Pick the day's postcard.

Runs every morning. Reads pool.json, works out which card each
combination of settings gets today, and writes two files:

  postcard.json  the day's card for the full catalogue, on its own
  today.json     one card per *cell* -- orientation x country x era --
                 which is what the TRMNL recipe polls

Nothing here talks to an archive's search API. The pool is already on
disk, so a bad day at the Library of Congress cannot blank the screen;
the worst it can do is leave yesterday's file in place.

The same card shows all day, and no card comes back until the pool has
been all the way through. Both fall out of the same trick: the day
number picks a position, and a hash of the card id sorts the pool into
a shuffle that is stable everywhere and reshuffles each time round.

A day that has already been published does not change. The feed carries
three days, so two of them were on screens before this run started, and
they are carried across from the live file rather than chosen again --
see picks_for_day.

Usage:
    python3 daily.py                    # write today's files
    python3 daily.py --date 2026-12-25  # any day, for checking
    python3 daily.py --no-check         # skip the image liveness probe
    python3 daily.py --recompute        # choose every day afresh
    python3 daily.py --selftest         # prove the schedule behaves
"""

import argparse
import collections
import hashlib
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
POOL_PATH = os.path.join(HERE, "pool.json")
DEFAULT_PATH = os.path.join(HERE, "postcard.json")
FEED_PATH = os.path.join(HERE, "today.json")

UA = ("postcard-of-the-day/1.0 "
      "(+https://github.com/nikokoren/postcard_of_the_day)")

# Changing this reshuffles every schedule. Don't.
SALT = "postcard-of-the-day/v1"

# The panels. An OG is 800x480, an X is 1872x1404 at a squarer aspect,
# and there are Minis and Cores in between. Asking for the largest and
# letting the smaller panels scale down costs nothing and means one URL
# serves every device.
X_BOX = (1872, 1404)
DEFAULT_BOX = X_BOX

# A IIIF server renders a derivative on demand from a master that can be
# 4,000 pixels across, and the FIRST request for a given size is the one
# that pays for it -- 12 to 18 seconds on a large scan, against under a
# second once cached. TRMNL's renderer gives up long before that and the
# panel comes up blank with the caption still on it.
#
# So the daily job asks for every image it is about to publish, before
# publishing the file that points at it, and the markup uses that exact
# URL rather than composing a size of its own. A HEAD is enough: the
# service still has to produce the image to report its length.
WARM_WORKERS = 6
WARM_TIMEOUT = 75

MAX_SKIPS = 4
DEAD_CODES = (403, 404, 410, 451)
CHECK_TIMEOUT = 12
CHECK_BUDGET = 400
MIN_INK_BYTES = 12_000

EPOCH = date(1970, 1, 1)

# Fields that change on every run without the card having changed. If
# only these differ the file is left alone, so a re-run does not make a
# commit that says nothing.
VOLATILE_FIELDS = ("generated", "image_checked")


# ============================================================
# the three axes
# ============================================================

ERAS = [
    ("era-pre-1900",  "Before 1900",  1800, 1899),
    ("era-1900-1914", "1900 - 1914",  1900, 1914),
    ("era-1915-1929", "1915 - 1929",  1915, 1929),
    ("era-1930-1945", "1930 - 1945",  1930, 1945),
    ("era-1946-on",   "1946 onwards", 1946, 2100),
]

ORIENTATIONS = [
    ("landscape", "Landscape"),
    ("portrait",  "Portrait"),
]

# Defined in harvest.py, where the country-to-region table lives, and
# imported rather than restated so the two cannot drift apart.
from harvest import REGIONS, region_for  # noqa: E402
REGION_SLUGS = {slug for slug, _ in REGIONS}

# The place axis is regions only. Countries were tried and dropped:
# outside the United States, France and Italy the counts fall away fast,
# and crossing them with two orientations and five eras leaves most
# countries unable to fill a year. A selector that offers Japan and then
# shows the same eleven cards every other month is worse than one that
# offers Asia and always has something.
#
# The country is still on every card, and still printed in the caption
# -- it is just not something to sort by.
REGION_MIN = 25

# A cell is one point in orientation x country x era, with "all"
# allowed in any slot -- "a portrait card from France, before 1900".
# Selections cannot be precomputed one file each (three axes, dozens of
# values, 2^n combinations), but the cells can: someone picking two
# countries and two eras is choosing among four of these, and the
# markup rotates over whichever ones exist.
CELL_MIN = 20
CELL_SEP = "__"

# TRMNL rejects a polling payload over 100KB. This is the line the build
# refuses to cross, with room to spare.
MAX_FEED_BYTES = 95_000
MAX_CELLS = 140

# The feed carries three days, not one, and this is why.
#
# The pick used to be chosen here, against the UTC date, and baked into
# the file -- so the card changed at the same instant worldwide. In
# Berlin that is 02:05, which reads as a new day. In Los Angeles it is
# 17:05 the *previous* afternoon, and in Auckland it lands at midday,
# changing the card while somebody is looking at it.
#
# The device knows better than we do: trmnl.system.timestamp_utc plus
# trmnl.user.utc_offset gives the viewer's own local time, so the markup
# can work out its own local day and ask for that day's card.
#
# Which days can it ask for? Offsets run from -12 to +14, so across the
# 24 hours one published file is live, the local day-index seen on a
# device spans exactly three values -- and it is three for any publish
# hour, so there is nothing to tune. Hence yesterday, today, tomorrow.
DAY_SPAN = (-1, 0, 1)

# One pick as a list, not an object: at 282 of them, field names alone
# would cost around 17KB of the 95KB budget. Order is part of the
# contract with the markup -- see PICK_FIELDS in the README.
#   0 image  1 title  2 date  3 place  4 publisher  5 credit  6 source

TITLE_LIMIT = 120
SUBTITLE_MARKERS = (" : ", " ; ", " -- ", " — ")


def cell_key(orientation, country, era):
    return CELL_SEP.join((orientation, country, era))


def in_era(entry, era):
    for slug, _, lo, hi in ERAS:
        if slug == era:
            return lo <= entry["y"] <= hi
    return False


def cards_for(entries, key):
    """The subset of the pool one cell selects."""
    orientation, region, era = key.split(CELL_SEP)
    out = entries
    if orientation != "all":
        out = [e for e in out if e.get("o") == orientation]
    if region != "all":
        out = [e for e in out if e.get("rg") == region]
    if era != "all":
        out = [e for e in out if in_era(e, era)]
    return out


def specificity(key):
    return sum(1 for part in key.split(CELL_SEP) if part != "all")


def _tally(entries, slug_field, label_field, minimum):
    counts = collections.Counter()
    labels = {}
    for entry in entries:
        slug = entry.get(slug_field)
        if not slug:
            continue
        counts[slug] += 1
        labels[slug] = entry.get(label_field) or slug
    return sorted(((slug, labels[slug], n) for slug, n in counts.items()
                   if n >= minimum),
                  key=lambda row: (-row[2], row[1]))


def regions_in(entries):
    """(slug, label, count) for every region big enough to offer."""
    return _tally(entries, "rg", "rgn", REGION_MIN)





def build_cells(entries, regions):
    """
    Every cell with enough cards behind it, coarsest first. Coarse cells
    are what the markup falls back to when a reader's exact combination
    is empty, so if the budget bites it bites the specific ones.
    """
    region_slugs = ["all"] + [slug for slug, _, _ in regions]
    era_slugs = ["all"] + [slug for slug, _, _, _ in ERAS]
    orientations = ["all"] + [slug for slug, _ in ORIENTATIONS]

    keys = []
    for orientation in orientations:
        for region in region_slugs:
            for era in era_slugs:
                key = cell_key(orientation, region, era)
                if len(cards_for(entries, key)) >= CELL_MIN:
                    keys.append(key)
    keys.sort(key=lambda k: (specificity(k), k))
    return keys[:MAX_CELLS]


def label_aliases(orientations, regions, eras):
    """
    Every spelling a setting might arrive as, mapped to the key this
    feed uses.

    TRMNL does not send back the label a reader picked. It sends a value
    derived from it -- "United States" comes back as "united_states",
    "1900 - 1914" as "1900_-_1914" -- and the exact derivation is not
    documented. So rather than guess one rule, record all of them: the
    label, its lowercase form, its snake_case form, the key itself, and
    the key with underscores. A lookup that misses would silently fall
    back to the whole catalogue, which looks like the recipe ignoring
    the settings.
    """
    aliases = {}

    def add(key, label):
        lower = label.lower()
        snake = re.sub(r"[^a-z0-9]+", "_", lower).strip("_")
        for form in (label, lower, snake, key, key.replace("-", "_"),
                     re.sub(r"\s+", "_", lower),
                     re.sub(r"[^a-z0-9]+", "-", lower).strip("-")):
            if form:
                aliases[form] = key

    for key, label in orientations:
        add(key, label)
    for key, label, _ in regions:
        add(key, label)
    for key, label, _, _ in eras:
        add(key, label)
    return aliases


# ============================================================
# selection
# ============================================================

def day_index(day):
    """Days since the epoch. The one number the whole schedule turns on."""
    return (day - EPOCH).days


def order_for(entries, key, cycle):
    """
    The order this cell's cards come out in during one pass through its
    pool. Sorting by a hash of the id is a shuffle that is stable (same
    inputs, same order, on any machine and in any Python) without
    storing a schedule anywhere. The cycle number is in the hash, so the
    next pass comes out in a different order.
    """
    def sort_key(entry):
        seed = "{}|{}|{}|{}".format(SALT, key, cycle, entry["id"])
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return sorted(entries, key=sort_key)


def standin_offsets(total):
    """
    Where to look for a stand-in, as offsets from the day's own position
    in the cycle.

    The obvious answer -- the next card along -- is the one wrong answer.
    A card whose image is gone is gone every day, so day N would borrow
    day N+1's card and day N+1 would then show it again: the reader sees
    the same card two mornings running and reasonably concludes the
    recipe is stuck. Taking the one before has the same fault pointed
    backwards.

    So stand-ins are drawn from the far side of the cycle, nearest the
    halfway mark, where the borrowed card's own day is months away.
    """
    reach = min(MAX_SKIPS, total - 1)
    # Offset i is i days ahead and total-i days behind; the collision
    # that matters is whichever is nearer.
    spread = sorted(range(1, total), key=lambda i: (-min(i, total - i), i))
    return [0] + spread[:reach]


def candidates_for(entries, key, day):
    """The day's card, then the ones that stand in if its image is gone."""
    total = len(entries)
    if not total:
        return []
    cycle, position = divmod(day_index(day), total)
    ordered = order_for(entries, key, cycle)
    return [ordered[(position + offset) % total]
            for offset in standin_offsets(total)]


# ============================================================
# images
# ============================================================

def image_url(entry, box=DEFAULT_BOX, quality="default"):
    """
    IIIF sources give us any size we ask for. Fixed sources -- the
    Library of Congress postcard files -- have one derivative and that
    is what everyone gets.
    """
    if entry.get("k") == "fixed":
        return entry["b"]
    return "{}/full/!{},{}/0/{}.jpg".format(entry["b"], box[0], box[1], quality)


def warm(urls):
    """
    Pre-render every image the feed points at. Failures are not fatal:
    an image that would not warm is one the device will wait for, which
    is the situation this improves on rather than one it guarantees.
    """
    urls = sorted(set(urls))
    if not urls:
        return 0

    def touch(url):
        req = urllib.request.Request(url, method="HEAD",
                                     headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=WARM_TIMEOUT) as resp:
                return resp.status == 200
        except Exception:
            return False

    started = time.monotonic()
    total, warmed = len(urls), 0
    # Two passes. A render that ran past the timeout on the first pass
    # has usually finished by the second, and is then sitting in the
    # cache waiting to be acknowledged rather than made again.
    for _ in (1, 2):
        with ThreadPoolExecutor(max_workers=WARM_WORKERS) as pool:
            results = list(pool.map(touch, urls))
        warmed += sum(1 for ok in results if ok)
        urls = [url for url, ok in zip(urls, results) if not ok]
        if not urls:
            break
    sys.stderr.write("warmed {}/{} images in {:.0f}s{}\n".format(
        warmed, total, time.monotonic() - started,
        ", {} still cold".format(len(urls)) if urls else ""))
    return warmed


_checked = {"n": 0}


def budget_left():
    return _checked["n"] < CHECK_BUDGET


def image_ok(url):
    """
    True if the URL still serves a real image. A card whose scan has
    been withdrawn should not take a day off the calendar, and a
    zero-length or error-page response is worse than a substitution.
    """
    _checked["n"] += 1
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=CHECK_TIMEOUT) as resp:
            if resp.status != 200:
                return False
            ctype = resp.headers.get("Content-Type") or ""
            if not ctype.startswith("image/"):
                return False
            length = resp.headers.get("Content-Length")
            if length and int(length) < MIN_INK_BYTES:
                return False
            if not length:
                return len(resp.read(MIN_INK_BYTES)) >= MIN_INK_BYTES
            return True
    except urllib.error.HTTPError as e:
        return e.code not in DEAD_CODES
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError,
            ConnectionError, OSError, ValueError):
        # A timeout is the network's fault, not the card's. Keep it.
        return True


# ============================================================
# the caption
# ============================================================

def balance_brackets(text):
    if text.count("[") > text.count("]"):
        text += "]"
    if text.count("(") > text.count(")"):
        text += ")"
    return text


def title_line(entry):
    """
    A title that fits a panel. Cut at a subtitle marker if there is one,
    then at a clause boundary, and only fall back to a word boundary --
    with an ellipsis to admit it -- if neither exists.

    English wins where translate.py has produced one. It replaces the
    original rather than joining it, because the panel has room for one
    caption and a reader who cannot read Greek is not helped by being
    shown the Greek as well. The original stays in the pool.
    """
    title = entry.get("te") or entry["t"]
    if len(title) <= TITLE_LIMIT:
        return balance_brackets(title)
    for marker in SUBTITLE_MARKERS:
        head = title.split(marker)[0]
        if 12 <= len(head) <= TITLE_LIMIT:
            return balance_brackets(head.strip(" ,;:-"))
    cut = title[:TITLE_LIMIT]
    for boundary in (", ", " - "):
        index = cut.rfind(boundary)
        if index >= 40:
            return balance_brackets(cut[:index].strip(" ,;:-"))
    index = cut.rfind(" ")
    return balance_brackets(cut[:index].strip(" ,;:-")) + "…"


def date_line(entry):
    """
    What the date on the screen means.

    Almost always it is when the card was *printed*, because that is
    what a catalogue records -- the postmark is on the back, and the
    back is usually not even scanned. About 1% of records transcribe one
    anyway, and where they do it is the better answer to "how old is
    this card", so it is used and labelled as what it is rather than
    quietly passed off as a publication date.
    """
    if entry.get("pm"):
        return "Posted {}".format(entry["pm"])
    year, end = entry["y"], entry.get("y2")
    if end and end != year:
        return "{}–{}".format(year, end)
    return str(year)


def place_line(entry):
    bits = [entry.get("ple") or entry.get("pl"), entry.get("cn")]
    bits = [b for b in bits if b]
    if not bits:
        return ""
    if len(bits) == 2 and bits[1] in bits[0]:
        return bits[0]
    return ", ".join(bits)


# Catalogues record a publisher as it is printed on the card, which is
# a sentence rather than a name: "Made only by Tichnor Bros., Inc. Pub.
# by Sandoval News Service, El Paso, Texas". The layout puts "Printed
# by" in front of it, so the lead-in has to go or it reads "Printed by
# Made only by", and the trailing distributor is more than a caption
# line can hold.
PUBLISHER_LEAD = re.compile(
    r"^(?:made\s+(?:only\s+)?by|published\s+by|pub(?:lished)?\.?\s+by|"
    r"printed\s+by|printed\s+for|copyright\s+by)\s+", re.I)
PUBLISHER_TAIL = re.compile(
    r"(\.)?\s+(?:pub(?:lished)?\.?\s+by|made\s+only\s+by|"
    r"distributed\s+by|sold\s+by)\s+.*$", re.I)


def publisher_line(entry):
    name = PUBLISHER_LEAD.sub("", entry.get("pub") or "").strip()
    # Keep a full stop that belongs to the abbreviation before the cut,
    # so "Tichnor Bros., Inc. Pub. by ..." ends at "Inc." and not "Inc".
    name = PUBLISHER_TAIL.sub(lambda m: m.group(1) or "", name).strip(" ,;")
    if len(name) > 60:
        name = name[:60].rsplit(" ", 1)[0]
    # Catalogues gloss a non-Latin name in brackets. Half a bracket is
    # worse than none, so drop an unclosed one rather than cut inside it.
    if name.count("[") > name.count("]"):
        name = name[:name.rindex("[")]
    return name.strip(" ,;")


def credit_line(entry):
    holder = entry.get("h") or ""
    collection = entry.get("col") or ""
    if collection and collection.lower() not in holder.lower():
        return "{}, {}".format(holder, collection) if holder else collection
    return holder


# ============================================================
# payload
# ============================================================

PICK_FIELDS = ("image", "title", "date", "place", "publisher", "credit")


def build_payload(entry):
    """
    One card as a list, in PICK_FIELDS order. The cell key and the day
    are already the keys this sits under, and the pool size is
    feed-level, so repeating any of them costs bytes for nothing.
    """
    return [
        image_url(entry),
        title_line(entry),
        date_line(entry),
        place_line(entry),
        publisher_line(entry),
        credit_line(entry),
    ]


def full_payload(entry, day):
    """The single-card file, which can afford to be readable."""
    return {
        "id": entry["id"],
        "title": title_line(entry),
        "date": date_line(entry),
        "year": entry["y"],
        "place": place_line(entry),
        "country": entry.get("cn") or "",
        "orientation": entry.get("o") or "",
        "publisher": publisher_line(entry),
        "credit": credit_line(entry),
        "rights": entry.get("r") or "",
        "source_url": entry.get("u") or "",
        "image": image_url(entry),
        "day": day.isoformat(),
    }


def pick(entries, key, day, check):
    """The day's card for one cell, skipping any whose image has gone."""
    candidates = candidates_for(entries, key, day)
    if not candidates:
        return None, False
    if not check:
        return candidates[0], False
    for candidate in candidates:
        if not budget_left():
            return candidates[0], False
        if image_ok(image_url(candidate)):
            return candidate, True
    return candidates[0], True


# ============================================================
# what is already on the screens
# ============================================================

def published_days(feed):
    """
    The picks a feed is already handing out, as {day: {cell: pick}}.

    Nothing is kept from a file written to a different contract: a
    changed PICK_FIELDS makes every list in it mean something else, and
    a pick that means something else is not a pick.
    """
    if not isinstance(feed, dict) or feed.get("version") != 1:
        return {}
    if list(feed.get("pick_fields") or []) != list(PICK_FIELDS):
        return {}
    days = feed.get("days")
    return days if isinstance(days, dict) else {}


def load_published(path=FEED_PATH):
    """The live feed, or nothing if there isn't one to read."""
    try:
        with open(path) as fh:
            return published_days(json.load(fh))
    except (OSError, ValueError):
        return {}


def still_stands(standing, probe):
    """
    Whether a pick that has already gone out can stay. Shape first --
    anything the markup could not unpack is not a pick -- and then, on
    the day it matters, whether the image is still there.
    """
    if not isinstance(standing, list) or len(standing) != len(PICK_FIELDS):
        return False
    if not all(isinstance(field, str) for field in standing):
        return False
    if not standing[0]:
        return False
    if not probe or not budget_left():
        return True
    return image_ok(standing[0])


def picks_for_day(entries, cells, that_day, published, probe):
    """
    One day's picks, cell by cell, preferring whatever has already been
    published for that day. Returns (picks, carried, probed, misses).

    A day the last run published is a day somebody is already looking
    at. Offsets run to +14, so a viewer can be on tomorrow's card
    eighteen hours before the next run replaces the file, and a viewer
    at -12 is still on yesterday's when it lands. Choosing those days
    again from the current pool is what made the card change twice: once
    at the viewer's own midnight, which is the point of the whole
    three-day feed, and again when the new file arrived, which is not.

    It changed because the schedule turns on the pool -- `divmod` by its
    size, and a hash ordering over its membership -- so anything that
    moves the pool moves every cell's calendar with it. Six cards
    dropped on render score out of 17,785 moved 23 of 95 cells to a
    different card for a day that was already on screens. The image
    probe does the same on a smaller scale, since only the middle day is
    probed and a skip there disagrees with the unprobed copy published
    yesterday.

    So a published pick stands. The one thing that unseats it is its
    image having gone, which is worse than the change -- and that is
    only looked for on the middle day, the one about to be everybody's.

    A card dropped from the pool between runs therefore keeps the day it
    already holds and loses every day after it, which is what score.py
    means by a card being out of *tomorrow's* picks.
    """
    picks, carried, probed, misses = {}, 0, 0, 0
    for key in cells:
        standing = published.get(key)
        if still_stands(standing, probe):
            picks[key] = standing
            carried += 1
            continue
        entry, checked = pick(cards_for(entries, key), key, that_day, probe)
        if entry is None:
            misses += 1
            continue
        probed += 1 if checked else 0
        picks[key] = build_payload(entry)
    return picks, carried, probed, misses


# ============================================================
# writing
# ============================================================

def substantive(new, old):
    """True if anything but the volatile fields changed."""
    def strip(value):
        if isinstance(value, dict):
            return {k: strip(v) for k, v in value.items()
                    if k not in VOLATILE_FIELDS}
        if isinstance(value, list):
            return [strip(v) for v in value]
        return value
    return strip(new) != strip(old)


def write_json(path, payload):
    try:
        with open(path) as fh:
            old = json.load(fh)
    except (OSError, ValueError):
        old = None
    if old is not None and not substantive(payload, old):
        sys.stderr.write(f"  {os.path.basename(path)}: unchanged\n")
        return False
    with open(path, "w") as fh:
        json.dump(payload, fh, indent=1, sort_keys=True)
        fh.write("\n")
    sys.stderr.write(f"  {os.path.basename(path)}: written "
                     f"({os.path.getsize(path)} bytes)\n")
    return True


TRANSLATIONS_PATH = os.path.join(HERE, "translations.json")
QUALITY_PATH = os.path.join(HERE, "quality.json")


def load_quality():
    try:
        with open(QUALITY_PATH) as fh:
            return json.load(fh).get("scored") or {}
    except (OSError, ValueError):
        return {}


def load_translations():
    """Version 1 called this "titles", before place lines joined them."""
    try:
        with open(TRANSLATIONS_PATH) as fh:
            data = json.load(fh)
        return data.get("texts") or data.get("titles") or {}
    except (OSError, ValueError):
        return {}


class NoPoolYet(Exception):
    """The repo is live but the first harvest has not landed."""


def load_pool():
    if not os.path.exists(POOL_PATH):
        raise NoPoolYet
    with open(POOL_PATH) as fh:
        data = json.load(fh)
    entries = [e for e in data.get("entries") or [] if e.get("o") and e.get("b")]
    if not entries:
        raise SystemExit("pool.json has no usable entries")
    # Place each card in a region here rather than at harvest time. The
    # country-to-region table is a judgement call that will want
    # correcting -- whether Egypt files under Africa or the Middle East,
    # what to do with Hawaii -- and correcting it should not mean
    # re-crawling 30,000 records.
    # Applied here rather than in harvest.py, so a better translation --
    # or a corrected one -- never needs a re-crawl.
    # Same reasoning for the render scores: score.py measures a budget
    # of cards a day and a card that turns out to be a column of small
    # print is out of tomorrow's picks, rather than out of whichever
    # month the next crawl lands in. A card nobody has measured yet is
    # kept, as it always was.
    scores = load_quality()
    if scores:
        import harvest
        before = len(entries)
        entries = [e for e in entries
                   if harvest.readable(scores.get(e["id"]))]
        dropped = before - len(entries)
        if dropped:
            sys.stderr.write(f"{dropped} cards dropped on render score\n")
        if not entries:
            raise SystemExit("every card was dropped on render score")

    # And the subject balance, for the same reason: what counts as one
    # holding's speciality swamping the catalogue is a judgement that
    # will want revising, and revising it should not mean re-crawling
    # 30,000 records.
    import harvest
    before = len(entries)
    entries = harvest.balance_subjects(entries)
    if before != len(entries):
        sys.stderr.write(f"{before - len(entries)} cards dropped on subject\n")

    english = load_translations()
    for entry in entries:
        slug, label = region_for(entry.get("cn"), entry.get("ct"))
        if slug:
            entry["rg"], entry["rgn"] = slug, label
        rendered = english.get(entry["t"])
        if rendered and rendered.get("en"):
            entry["te"] = rendered["en"]
        # The place line too. At Graz it is the catalogue's own German
        # description of the view rather than a place name, and it was
        # the one line on the panel still speaking German.
        if entry.get("pl"):
            rendered = english.get(entry["pl"])
            if rendered and rendered.get("en"):
                entry["ple"] = rendered["en"]
    return entries


# ============================================================
# selftest
# ============================================================

def selftest(entries, day):
    failures = []

    def check(name, ok, detail=""):
        if not ok:
            failures.append(f"{name}: {detail}")
        print(("  ok   " if ok else "  FAIL ") + name +
              (f"  {detail}" if detail and not ok else ""))

    ids = [e["id"] for e in entries]
    check("ids are unique", len(ids) == len(set(ids)),
          f"{len(ids) - len(set(ids))} duplicates")

    # The subject rule, at the captions that decided where it sits. A
    # sitter nobody can name goes; a monarch against a studio curtain
    # goes; a monarch doing something somewhere stays, and so does a
    # street named after one. Graz is here because it must never be
    # caught by any of it.
    import harvest
    subjects = [
        (False, "Portret van een onbekende vrouw"),
        (False, "Studioportret van een onbekende jongen met een hoed"),
        (False, "Portret van Juliana, koningin der Nederlanden"),
        (False, "Portret van Wilhelmina, koningin der Nederlanden"),
        (True,  "Bezoek van H.M. de koningin aan Leeuwarden"),
        (True,  "De Rouwkoets van de begrafenisstoet van Emma"),
        (True,  "Ooievaar aan de poort van Paleis Noordeinde te Den Haag"),
        (True,  "Amsterdam. Prins Hendrikkade"),
        (True,  "Koninginnekerk. Rotterdam"),
        (True,  "Graz"),
        (True,  "Graz gegen Norden"),
        (True,  "Gruss aus Graz"),
    ]
    for want, title in subjects:
        card = [{"id": "x", "t": title}]
        got = bool(harvest.balance_subjects(card))
        check(f"subject: {'keeps' if want else 'drops'} {title[:48]}", got is want)

    # And the cap, which only bites on the third copy of one caption.
    same = [{"id": f"r{n}", "t": "Doop van Juliana, koningin der Nederlanden"}
            for n in range(5)]
    check("subject: five photographs of one christening become two",
          len(harvest.balance_subjects(same)) == harvest.ROYAL_REPEAT,
          f"kept {len(harvest.balance_subjects(same))}")
    graz = [{"id": f"g{n}", "t": "Graz"} for n in range(9)]
    check("subject: nine views of Graz are nine views of Graz",
          len(harvest.balance_subjects(graz)) == 9,
          f"kept {len(harvest.balance_subjects(graz))}")

    key = cell_key("all", "all", "all")
    total = len(entries)

    first = candidates_for(entries, key, day)[0]["id"]
    again = candidates_for(entries, key, day)[0]["id"]
    check("same day, same card", first == again, f"{first} vs {again}")

    tomorrow = candidates_for(entries, key, day + timedelta(days=1))[0]["id"]
    check("next day is a different card", first != tomorrow, first)

    # A stand-in is never a neighbouring day's card. The obvious
    # fallback -- the next card along -- makes a dead image show
    # tomorrow's card today and the same card again tomorrow, which
    # reads as a recipe that has stopped moving.
    standins = {e["id"] for e in candidates_for(entries, key, day)[1:]}
    yesterday = candidates_for(entries, key, day - timedelta(days=1))[0]["id"]
    check("a stand-in is not a neighbouring day's card",
          not standins & {tomorrow, yesterday})
    gaps = [min(i, total - i) for i in standin_offsets(total)[1:]]
    check("stand-ins are drawn from the far side of the cycle",
          bool(gaps) and min(gaps) >= total // 4,
          f"nearest sits {min(gaps) if gaps else 0} days away")

    # One full cycle must visit every card exactly once.
    cycle_start = day - timedelta(days=day_index(day) % total)
    sample = min(total, 400)
    seen = [candidates_for(entries, key, cycle_start + timedelta(days=i))[0]["id"]
            for i in range(sample)]
    check("no repeats inside a cycle", len(set(seen)) == sample,
          f"{sample - len(set(seen))} repeats in {sample} days")

    later = candidates_for(entries, key,
                           cycle_start + timedelta(days=total))[0]["id"]
    check("the next cycle reshuffles", later != seen[0], later)

    regions = regions_in(entries)
    check("enough regions to be worth a selector", len(regions) >= 3,
          f"{len(regions)}")

    check("every offered region resolves to cards",
          all(cards_for(entries, cell_key("all", slug, "all"))
              for slug, _, _ in regions), "an empty region got offered")

    # The point of regions: a region should survive being crossed with
    # an orientation, which is where most countries stop being viable.
    deep = [slug for slug, _, _ in regions
            if all(len(cards_for(entries, cell_key(o, slug, "all"))) >= CELL_MIN
                   for o, _ in ORIENTATIONS)]
    check("regions survive an orientation filter", len(deep) >= 2,
          f"{len(deep)} of {len(regions)}")

    cells = build_cells(entries, regions)
    check("every cell has cards",
          all(cards_for(entries, k) for k in cells), "an empty cell got through")
    check("the catch-all cell exists", key in cells)

    for slug, _ in ORIENTATIONS:
        k = cell_key(slug, "all", "all")
        check(f"cell {k}", k in cells)

    payload = build_payload(entries[0])
    check("payload is complete",
          len(payload) == len(PICK_FIELDS) and all(
              isinstance(v, str) for v in payload))
    check("a device can name any day the feed carries",
          set(DAY_SPAN) == {-1, 0, 1})

    # A day that has been published stays where it is, whatever the pool
    # has done since. The one thing that can unseat a standing pick is
    # its image having gone, and that is the network's business rather
    # than a selftest's, so what is checked here is everything else.
    #
    # The fixture is a card no pool will ever hold, so "it came back
    # unchanged" cannot be a coincidence.
    gone_out = ["https://example.invalid/already-on-a-screen.jpg",
                "A card that went out yesterday"]
    gone_out += [""] * (len(PICK_FIELDS) - len(gone_out))

    fresh, _, _, _ = picks_for_day(entries, [key], day, {}, False)
    kept, carried, _, _ = picks_for_day(entries, [key], day, {key: gone_out},
                                        False)
    check("a published pick is carried forward",
          kept[key] == gone_out and carried == 1, f"{carried} carried")
    check("even though the pool would have chosen otherwise",
          fresh[key] != gone_out)

    for broken, what in ((gone_out[:-1], "too short"),
                         ([gone_out[0]] + [None] * (len(PICK_FIELDS) - 1),
                          "not all strings"),
                         ([""] + gone_out[1:], "no image"),
                         ("not a list", "not a list")):
        again, carried, _, _ = picks_for_day(entries, [key], day,
                                             {key: broken}, False)
        check(f"a standing pick that is {what} is chosen again",
              carried == 0 and again[key] == fresh[key])

    check("a feed written to another contract carries nothing",
          published_days({"version": 1, "pick_fields": ["image"],
                          "days": {"1": {key: gone_out}}}) == {})
    check("and neither does one from another version",
          published_days({"version": 2, "pick_fields": list(PICK_FIELDS),
                          "days": {"1": {key: gone_out}}}) == {})
    check("a feed of this contract carries its days",
          published_days({"version": 1, "pick_fields": list(PICK_FIELDS),
                          "days": {"1": {key: gone_out}}}) ==
          {"1": {key: gone_out}})
    check("a file that is not there carries nothing",
          load_published(os.path.join(HERE, "no-such-feed.json")) == {})

    print()
    if failures:
        print(f"{len(failures)} failed")
        return 1
    print("all good")
    return 0


# ============================================================
# main
# ============================================================

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD, defaults to today (UTC)")
    ap.add_argument("--no-check", action="store_true",
                    help="skip the image liveness probe")
    # The way back out. A published day is otherwise immovable, which is
    # the point of it -- but a bad pick that got published would sit
    # there for two more runs, and this is how it is unstuck.
    ap.add_argument("--recompute", action="store_true",
                    help="ignore the published feed and choose every day "
                         "afresh")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    day = (date.fromisoformat(args.date) if args.date
           else date.fromtimestamp(time.time()))
    try:
        entries = load_pool()
    except NoPoolYet:
        # The scheduled job starts running the moment the workflow file
        # reaches the default branch, which is normally before the first
        # harvest has been committed. That is a state to wait out, not a
        # failure to shout about every morning -- so say so and exit
        # clean. A pool that exists but is unusable still fails loudly.
        sys.stderr.write(
            "no pool.json yet, so there is nothing to publish. Run the "
            "Refresh Postcard Pool workflow first.\n")
        return 0

    if args.selftest:
        return selftest(entries, day)

    check = not args.no_check
    regions = regions_in(entries)
    cells = build_cells(entries, regions)
    sys.stderr.write(f"{len(entries)} cards, {len(regions)} regions, "
                     f"{len(cells)} cells\n")

    # What the live feed is already handing out. Two of the three days
    # it carries have been on screens since it was published, and a day
    # that has been published does not move -- see picks_for_day.
    published = {} if args.recompute else load_published()

    # Every cell, for every day a device might be on. A card that is
    # tomorrow's here is today's for somebody fourteen hours ahead.
    days, misses, probed, carried = {}, 0, 0, 0
    for shift in DAY_SPAN:
        that_day = day + timedelta(days=shift)
        key_day = str(day_index(that_day))
        # Only probe the images for the middle day. The other two are
        # the same cards a day either side of their own turn, and get
        # probed when it comes.
        picks, kept, checked, missed = picks_for_day(
            entries, cells, that_day, published.get(key_day) or {},
            check and shift == 0)
        days[key_day] = picks
        carried += kept
        probed += checked
        misses += missed

    default_key = cell_key("all", "all", "all")
    today = days[str(day_index(day))]
    if default_key not in today:
        raise SystemExit("no pick for the full catalogue; refusing to publish")

    # Every card the pool can still name, by the URL the feed points at.
    # A carried pick is a list of strings and nothing else, so this is
    # the way back from one to the card it came from.
    by_url = {image_url(entry): entry for entry in entries}

    # Warm before publishing, so no device is ever the one that triggers
    # a render -- across all three days, because a device fourteen hours
    # ahead is already on tomorrow's. Fixed-derivative sources are
    # static files with nothing to render and are skipped; a URL the
    # pool no longer carries is warmed anyway, because a HEAD costs less
    # than being wrong about a card that is on a screen right now.
    if check:
        warm(pick_list[0]
             for picks in days.values() for pick_list in picks.values()
             if (by_url.get(pick_list[0]) or {}).get("k", "iiif") == "iiif")

    generated = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    # The Liquid reads these names directly, so they are part of the
    # contract: renaming one breaks every installed recipe. The option
    # lists are called *_options rather than orientations/countries/eras
    # because a feed key and a settings keyname of the same name collide
    # in the template scope.
    feed = {
        "version": 1,
        "generated": generated,
        "day": day.isoformat(),
        "day_index": day_index(day),
        "pool": len(entries),
        "default": default_key,
        "default_day": str(day_index(day)),
        "cell_separator": CELL_SEP,
        "cell_keys": ",".join(sorted(today)),
        "pick_fields": list(PICK_FIELDS),
        "keys_by_label": label_aliases(ORIENTATIONS, regions, ERAS),
        "orientation_options": [{"key": s, "label": l} for s, l in ORIENTATIONS],
        "region_options": [{"key": s, "label": l, "count": n}
                           for s, l, n in regions],
        "era_options": [{"key": s, "label": l} for s, l, _, _ in ERAS],
        "days": days,
    }

    # TRMNL rejects a payload over 100KB outright, and the pool only ever
    # grows. Rather than fail a morning's run when it crosses the line,
    # drop the most specific cells until it fits -- the markup already
    # falls back to a coarser cell, so a reader loses precision, not a
    # postcard.
    def measure(payload):
        return len(json.dumps(payload, indent=1,
                              sort_keys=True).encode("utf-8"))

    dropped = 0
    size = measure(feed)
    while size > MAX_FEED_BYTES and len(feed["cell_keys"]) > 1:
        worst = max(feed["days"][feed["default_day"]],
                    key=lambda k: (specificity(k), k))
        for picks in feed["days"].values():
            picks.pop(worst, None)
        feed["cell_keys"] = ",".join(sorted(feed["days"][feed["default_day"]]))
        dropped += 1
        size = measure(feed)
    if size > MAX_FEED_BYTES:
        raise SystemExit(f"feed is {size} bytes and cannot be trimmed further")

    sys.stderr.write(
        f"feed {size} bytes, {len(feed['days'])} days x "
        f"{len(feed['days'][feed['default_day']])} cells"
        f"{f', {carried} picks carried forward' if carried else ''}"
        f"{f', {probed} images probed' if probed else ''}"
        f"{f', {misses} empty cells' if misses else ''}"
        f"{f', {dropped} cells dropped to fit' if dropped else ''}\n")

    # The single-card file is the feed's catch-all cell resolved back to
    # its card, not a pick of its own: choosing again here would disagree
    # with the feed on every day the feed is carrying forward. A card
    # that has since left the pool cannot be written out in full, and
    # then today's pick is the only thing left to say.
    standing = by_url.get(today[default_key][0])
    if standing is None:
        standing = pick(cards_for(entries, default_key),
                        default_key, day, False)[0]
    single = full_payload(standing, day)
    single["generated"] = generated
    single["pool"] = len(entries)
    write_json(DEFAULT_PATH, single)
    write_json(FEED_PATH, feed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
