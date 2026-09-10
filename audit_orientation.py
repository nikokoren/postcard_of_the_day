#!/usr/bin/env python3
"""
Is a card we call "landscape" actually landscape?

Orientation comes from pixel dimensions, which assumes a scan is stored
the way the card is meant to be read. That assumption is worth testing,
because a sideways scan would be labelled confidently and wrongly, and
the selector is built on it.

Two ways to check, and only one of them works.

**Against the catalogue.** Records sometimes carry physical dimensions,
written height x width, so their shape can be compared with the scan's.
Run over 1,292 Library of Congress records this disagreed 21.7% of the
time -- and every disagreement read exactly "9.0 x 14.0 cm", which is
the standard postcard size applied as boilerplate to a batch rather than
measured per card. Six of those were rendered and every one was a
genuinely portrait card, upright, caption along the bottom. So the
catalogue is the unreliable side here, not the scan, and this test is
not usable as a filter.

**By looking.** Which is what this does: a contact sheet of random pool
cards, labelled with the orientation we recorded, so a person can see
whether the two agree. Thirty cards inspected this way at the time of
writing, none of them sideways -- which bounds the error rate at roughly
10% rather than proving it is zero.

    python3 audit_orientation.py --seed 7 --out audit.png
"""

import argparse
import io
import json
import os
import random
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import daily  # noqa: E402

TW, TH, CAP, COLS = 250, 260, 26, 6


def grab(entry, box):
    from PIL import Image
    url = (entry["b"] if entry.get("k") == "fixed"
           else f"{entry['b']}/full/!{box},{box}/0/default.jpg")
    try:
        raw = urllib.request.urlopen(
            urllib.request.Request(url, headers={"User-Agent": daily.UA}),
            timeout=45).read()
        image = Image.open(io.BytesIO(raw)).convert("L")
    except Exception:
        return None
    image.thumbnail((TW, TH))
    return image


def main():
    from PIL import Image, ImageDraw
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--count", type=int, default=12)
    ap.add_argument("--orientation", choices=("landscape", "portrait"))
    ap.add_argument("--out", default="audit.png")
    args = ap.parse_args()

    pool = daily.load_pool()
    if args.orientation:
        pool = [e for e in pool if e.get("o") == args.orientation]
    random.seed(args.seed)
    sample = random.sample(pool, min(args.count, len(pool)))

    tiles = []
    for entry in sample:
        image = grab(entry, 400)
        tile = Image.new("L", (TW, TH + CAP), 255)
        if image:
            tile.paste(image, ((TW - image.size[0]) // 2,
                               (TH - image.size[1]) // 2))
        draw = ImageDraw.Draw(tile)
        draw.text((3, TH + 2),
                  f"{entry['o']} {entry.get('w')}x{entry.get('h_px')}", fill=0)
        draw.text((3, TH + 13), entry["t"][:38], fill=90)
        tiles.append(tile)

    rows = (len(tiles) + COLS - 1) // COLS
    sheet = Image.new("L", (TW * COLS, (TH + CAP) * rows), 255)
    for i, tile in enumerate(tiles):
        sheet.paste(tile, ((i % COLS) * TW, (i // COLS) * (TH + CAP)))
    sheet.save(args.out)
    print(f"wrote {args.out} -- {len(tiles)} cards, seed {args.seed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
