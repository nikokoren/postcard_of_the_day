#!/usr/bin/env python3
"""
A frozen feed for the marketplace screenshot.

The plugin listing needs a picture of the recipe looking its best, which
means a real render of a hand-picked card rather than whatever today
happens to serve. This writes showcase.json in exactly the shape
today.json has, so the recipe renders it without knowing the difference
-- point the plugin's polling URL at it, take the screenshot, point it
back.

    python3 showcase.py
    python3 showcase.py --id dc:commonwealth:bc3879911
"""

import argparse
import json
import os
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import daily  # noqa: E402

OUT = os.path.join(HERE, "showcase.json")

# "Greetings from Ciudad Juarez, Old Mexico", Tichnor Brothers, c.1930 --
# a large-letter card, the single most recognisable postcard format there
# is. Chosen by rendering the shortlist at panel size and 2-bit depth and
# looking: at thumbnail size a tropical view reads as a photograph, while
# this reads as a postcard, which is the whole job of a marketplace tile.
DEFAULT_HERO = "dc:commonwealth:bc3879911"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--id", default=DEFAULT_HERO)
    ap.add_argument("--date", help="YYYY-MM-DD shown in the feed")
    args = ap.parse_args()

    entries = daily.load_pool()
    entry = next((e for e in entries if e["id"] == args.id), None)
    if entry is None:
        raise SystemExit(f"{args.id} is not in the pool")

    day = date.fromisoformat(args.date) if args.date else date.today()
    pick = daily.build_payload(entry)
    regions = daily.regions_in(entries)

    # One cell, not ninety-five. The markup falls back to the default
    # cell whenever the reader's exact combination is missing, so a feed
    # carrying only the default renders the same card for every possible
    # setting -- which is exactly what a screenshot wants, and keeps the
    # file at a few kilobytes instead of the 121KB that shipping every
    # cell produced, well over what TRMNL will accept.
    default_key = daily.cell_key("all", "all", "all")
    keys = [default_key]
    picks = {default_key: pick}
    index = daily.day_index(day)

    feed = {
        "version": 1,
        "generated": daily.time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                         daily.time.gmtime()),
        "day": day.isoformat(),
        "day_index": index,
        "default_day": str(index),
        "pool": len(entries),
        "default": default_key,
        "cell_separator": daily.CELL_SEP,
        "cell_keys": ",".join(sorted(keys)),
        "pick_fields": list(daily.PICK_FIELDS),
        "keys_by_label": daily.label_aliases(daily.ORIENTATIONS, regions,
                                             daily.ERAS),
        "orientation_options": [{"key": s, "label": l}
                                for s, l in daily.ORIENTATIONS],
        "region_options": [{"key": s, "label": l, "count": n}
                           for s, l, n in regions],
        "era_options": [{"key": s, "label": l} for s, l, _, _ in daily.ERAS],
        "days": {str(index + shift): picks for shift in daily.DAY_SPAN},
    }
    with open(OUT, "w") as fh:
        json.dump(feed, fh, indent=1, sort_keys=True)
        fh.write("\n")
    size = os.path.getsize(OUT)
    print(f"wrote showcase.json ({size} bytes)")
    for name, value in zip(daily.PICK_FIELDS, pick):
        print(f"  {name:11s} {str(value)[:92]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
