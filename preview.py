#!/usr/bin/env python3
"""
Renders upcoming picks as a contact sheet at the panel's grey depth, so
a person can judge whether the pool is worth looking at.

Depth matters more than it sounds: at 1-bit everything mid-toned turns
to noise and half a collection looks broken, while at the 2-bit and
4-bit depths the panels actually have, the same cards read cleanly.
Default is 2-bit, the conservative case.

Nothing here measures anything. Legibility can be measured; whether a
postcard is interesting cannot, and this is the cheapest way to put that
question in front of someone who can answer it.

    python3 preview.py                                   # the next 24 days
    python3 preview.py --cell landscape__france__all
    python3 preview.py --days 40 --levels 16
"""

import argparse
import io
import os
import sys
import urllib.request
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import daily  # noqa: E402

TILE_W, TILE_H = 330, 230
CAPTION_H = 30
COLS = 4


def fetch(entry, width, height):
    from PIL import Image
    if entry.get("k") == "fixed":
        url = entry["b"]
    else:
        url = "{}/full/!{},{}/0/gray.jpg".format(entry["b"], width, height)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": daily.UA})
        raw = urllib.request.urlopen(req, timeout=60).read()
        image = Image.open(io.BytesIO(raw)).convert("L")
    except Exception:
        return None
    image.thumbnail((width, height))
    return image


def main():
    from PIL import Image, ImageDraw
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=24)
    parser.add_argument("--cell", default="all__all__all",
                        help="orientation__country__era")
    parser.add_argument("--levels", type=int, default=4,
                        help="grey levels: 2 = 1-bit, 4 = 2-bit, 16 = 4-bit")
    parser.add_argument("--out", default="preview.png")
    args = parser.parse_args()

    entries = daily.load_pool()
    subset = daily.cards_for(entries, args.cell)
    if not subset:
        sys.stderr.write("no cards in cell {}\n".format(args.cell))
        return 1
    sys.stderr.write("{}: {} cards\n".format(args.cell, len(subset)))

    today = date.today()
    tiles = []
    for offset in range(args.days):
        day = today + timedelta(days=offset)
        entry = daily.candidates_for(subset, args.cell, day)[0]
        image = fetch(entry, TILE_W, TILE_H)
        tile = Image.new("L", (TILE_W, TILE_H + CAPTION_H), 255)
        if image:
            tile.paste(image, ((TILE_W - image.size[0]) // 2,
                               (TILE_H - image.size[1]) // 2))
        draw = ImageDraw.Draw(tile)
        draw.text((3, TILE_H + 4),
                  "{}  {}".format(entry["y"], daily.title_line(entry))[:62],
                  fill=0)
        draw.text((3, TILE_H + 16),
                  "{}  {}".format(entry.get("cn") or "?",
                                  entry.get("o") or "?")[:62], fill=90)
        tiles.append(tile.quantize(colors=args.levels,
                                   dither=Image.Dither.FLOYDSTEINBERG))
        sys.stderr.write("  {} {}\n".format(day, entry["t"][:60]))

    rows = (len(tiles) + COLS - 1) // COLS
    sheet = Image.new("L", (TILE_W * COLS, (TILE_H + CAPTION_H) * rows), 255)
    for i, tile in enumerate(tiles):
        sheet.paste(tile.convert("L"),
                    ((i % COLS) * TILE_W, (i // COLS) * (TILE_H + CAPTION_H)))
    sheet.save(args.out)
    print("wrote {} ({} days, cell {})".format(args.out, args.days, args.cell))
    return 0


if __name__ == "__main__":
    sys.exit(main())
