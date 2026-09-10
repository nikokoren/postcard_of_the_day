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

Usage:
    python3 daily.py                    # write today's files
    python3 daily.py --date 2026-12-25  # any day, for checking
    python3 daily.py --no-check         # skip the image liveness probe
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


def countries_in(entries):
    """(slug, label, count) for every country big enough to offer."""
    return _tally(entries, "c", "cn", COUNTRY_MIN)





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


def candidates_for(entries, key, day):
    """The day's card, then the ones that stand in if its image is gone."""
    total = len(entries)
    if not total:
        return []
    cycle, position = divmod(day_index(day), total)
    ordered = order_for(entries, key, cycle)
    return [ordered[(position + offset) % total]
            for offset in range(min(MAX_SKIPS + 1, total))]


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
    """
    title = entry["t"]
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
    year, end = entry["y"], entry.get("y2")
    if end and end != year:
        return "{}–{}".format(year, end)
    return str(year)


def place_line(entry):
    bits = [entry.get("pl"), entry.get("cn")]
    bits = [b for b in bits if b]
    if not bits:
        return ""
    if len(bits) == 2 and bits[1] in bits[0]:
        return bits[0]
    return ", ".join(bits)


def credit_line(entry):
    holder = entry.get("h") or ""
    collection = entry.get("col") or ""
    if collection and collection.lower() not in holder.lower():
        return "{}, {}".format(holder, collection) if holder else collection
    return holder


# ============================================================
# payload
# ============================================================

def build_payload(entry):
    """
    One card, as the markup sees it. Deliberately lean: the cell key is
    already the dict key, and the day and pool size are feed-level, so
    repeating any of them costs a byte a card for nothing.
    """
    return {
        "id": entry["id"],
        "title": title_line(entry),
        "date": date_line(entry),
        "year": entry["y"],
        "place": place_line(entry),
        "country": entry.get("cn") or "",
        "orientation": entry.get("o") or "",
        "publisher": entry.get("pub") or "",
        "credit": credit_line(entry),
        "rights": entry.get("r") or "",
        "source_url": entry.get("u") or "",
        "image": image_url(entry),
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
    for entry in entries:
        slug, label = region_for(entry.get("cn"), entry.get("ct"))
        if slug:
            entry["rg"], entry["rgn"] = slug, label
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

    key = cell_key("all", "all", "all")
    total = len(entries)

    first = candidates_for(entries, key, day)[0]["id"]
    again = candidates_for(entries, key, day)[0]["id"]
    check("same day, same card", first == again, f"{first} vs {again}")

    tomorrow = candidates_for(entries, key, day + timedelta(days=1))[0]["id"]
    check("next day is a different card", first != tomorrow, first)

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
          all(payload.get(f) not in (None,) for f in
              ("id", "title", "date", "image", "credit")))

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
    countries = countries_in(entries)
    places = places_in(entries)
    cells = build_cells(entries, regions)
    sys.stderr.write(f"{len(entries)} cards, {len(regions)} regions, "
                     f"{len(countries)} countries, {len(cells)} cells\n")

    picks, misses, probed = {}, 0, 0
    for key in cells:
        subset = cards_for(entries, key)
        entry, checked = pick(subset, key, day, check)
        if entry is None:
            misses += 1
            continue
        probed += 1 if checked else 0
        picks[key] = build_payload(entry)

    default_key = cell_key("all", "all", "all")
    if default_key not in picks:
        raise SystemExit("no pick for the full catalogue; refusing to publish")

    # Warm before publishing, so no device is ever the one that triggers
    # a render. Fixed-derivative sources are static files and skip this.
    if check:
        warm(p["image"] for p in picks.values()
             if "/full/" in p["image"])

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
        "cell_separator": CELL_SEP,
        "cell_keys": ",".join(sorted(picks)),
        "keys_by_label": label_aliases(ORIENTATIONS, regions, ERAS),
        "orientation_options": [{"key": s, "label": l} for s, l in ORIENTATIONS],
        "region_options": [{"key": s, "label": l, "count": n}
                           for s, l, n in regions],
        "era_options": [{"key": s, "label": l} for s, l, _, _ in ERAS],
        "picks": picks,
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
    while size > MAX_FEED_BYTES and len(feed["picks"]) > 1:
        worst = max(feed["picks"], key=lambda k: (specificity(k), k))
        del feed["picks"][worst]
        feed["cell_keys"] = ",".join(sorted(feed["picks"]))
        dropped += 1
        size = measure(feed)
    if size > MAX_FEED_BYTES:
        raise SystemExit(f"feed is {size} bytes and cannot be trimmed further")

    sys.stderr.write(
        f"feed {size} bytes, {len(feed['picks'])} picks"
        f"{f', {probed} images probed' if probed else ''}"
        f"{f', {misses} empty cells' if misses else ''}"
        f"{f', {dropped} cells dropped to fit' if dropped else ''}\n")

    single = dict(picks[default_key])
    single["generated"] = generated
    single["day"] = day.isoformat()
    single["pool"] = len(entries)
    write_json(DEFAULT_PATH, single)
    write_json(FEED_PATH, feed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
