# Postcard of the Day

A TRMNL recipe that puts one real printed postcard on the panel every
morning — the kind somebody bought at a station kiosk, wrote three lines
on, and posted. The same card all day, a new one at midnight UTC, and
nothing repeats until every card in the pool has had its turn.

Sort by **orientation**, **country** and **era**, in any combination.
Leave the settings alone and you get the whole catalogue.

---

## How it works

```
  monthly ──► harvest.py ──► pool.json      (catalogue metadata, cached)
                                 │
  daily   ──► daily.py ──────────┘──► today.json ──► raw.githubusercontent.com
                                      postcard.json          │
                                                             ▼
                                                      TRMNL polls this
```

Three things fall out of that shape, and they are the reasons for it:

**Nothing on the device talks to an archive's search API.** TRMNL polls a
static JSON file on `raw.githubusercontent.com`. That sidesteps the
Cloudflare challenge that greets a plugin trying to poll a library
catalogue directly, and it means an archive having a bad morning leaves
yesterday's file in place rather than blanking the screen.

**The pick is computed offline.** `daily.py` reads a pool that is already
on disk. The only network it touches is a liveness probe on the chosen
image, and even that is optional.

**There is no infrastructure.** GitHub Actions runs two cron jobs; the
output is committed to the repo. Nothing to host, nothing to pay for,
nothing to keep alive.

---

## The three axes

| axis | values | where it comes from |
| --- | --- | --- |
| Orientation | Landscape, Portrait | the scan's own pixel dimensions |
| Region | seven, listed below | `subject_hiergeo_geojson_ssm` (Digital Commonwealth), `location_country` (Library of Congress) |
| Era | Before 1900, 1900-1914, 1915-1929, 1930-1945, 1946 onwards | the catalogue date |

### Why regions and not countries

Countries were tried first and dropped. A random sample of 3,000
openly-licensed Digital Commonwealth postcards came back **87.7% United
States**; the Library of Congress half spans 60 countries but is 52%
American, with France at 9.5% and everything past Italy in low single
digits. Cross that with two orientations and five eras and most
countries can no longer fill a year — a selector offering Japan that
then shows the same eleven cards every other month is worse than one
offering Asia that always has something.

So the axis is seven regions:

> North America · Latin America & the Caribbean · Europe · Africa ·
> Middle East · Asia · Oceania

Postcard regions, not strict continents. The Middle East is split out
because views of the Holy Land are their own publishing genre and nobody
looking for them looks under Asia; North Africa stays in Africa, because
that is where the cards were catalogued.

The country is still on every card and still printed in the caption. It
is just not something to sort by.

The country-to-region table is applied when `daily.py` loads the pool,
not when `harvest.py` writes it. Whether Egypt files under Africa or the
Middle East is a judgement call that will want revisiting, and revisiting
it should not mean re-crawling 30,000 records.

### Every setting empty means everything

All three settings are multi-selects, and an empty one constrains
nothing. That is the whole rule, and it is why none of them is a pair of
on/off switches: two booleans have a state — both off — that has to be
given a meaning, and any meaning you give it contradicts what the
switches say. An empty multi-select has no such state to explain, and
nothing has to be set for the recipe to work.

### Why the feed ships *cells* rather than selections

Three axes with dozens of values between them is 2ⁿ possible selections,
and a static file cannot carry one entry per selection. What it can carry
is one entry per **cell** — a single point like
`portrait__europe__era-1900-1914`. A reader who picks two countries and
two eras is choosing among four cells, and the markup rotates over
whichever of them the feed actually has, a day at a time.

Cells with fewer than **20** cards behind them are not shipped. Some are
genuinely empty and always will be, which is why the Liquid loosens one
axis at a time rather than falling straight through to the whole
catalogue: asking for portrait cards from Africa before 1900 should
still get you a card from Africa.

The feed is capped at **95KB**, under TRMNL's 100KB polling limit, and
`daily.py` refuses to publish a payload over it rather than letting the
plugin degrade.

---

## Whose day is it?

The card changes at **the viewer's local midnight**, not at a fixed UTC
moment — and that distinction is the whole reason the feed is shaped the
way it is.

An earlier version chose the pick here, against the UTC date, and baked
it into the file. That means the card changes at the same instant
worldwide: 02:05 in Berlin, which reads as a new day, but **17:05 the
previous afternoon** in Los Angeles and **midday** in Auckland, where it
swaps while somebody is looking at it.

TRMNL hands the markup everything it needs to do better:

```liquid
{% assign local_seconds = trmnl.system.timestamp_utc | plus: trmnl.user.utc_offset %}
{% assign local_day = local_seconds | divided_by: 86400 %}
```

`timestamp_utc` is unix seconds and `utc_offset` is that viewer's offset
in seconds, so those two lines produce the same integer `daily.py`
counts in — for that device, in its own timezone.

So the feed carries **three days**: yesterday, today and tomorrow. Three
is not a guess. Offsets run from −12 to +14, so the 24 hours one
published file is live span about 50 hours of local time, which always
crosses two or three midnights — and it comes to three for *any* publish
hour, so there is nothing to tune. A device with no usable clock falls
back to the day the file was built for.

That is also why a pick is a **list rather than an object**: at 94 cells
across 3 days, field names alone would cost roughly 17KB of the 95KB
budget. `selection.liquid` unpacks one into `card_image`, `card_title`,
`card_date`, `card_place`, `card_publisher` and `card_credit` so the
layout stays readable.

### What that costs at each hop

| step | when |
| --- | --- |
| Actions cron fires | 00:05 UTC |
| `daily.py` picks, warms every image, commits | ~1–4 min |
| `raw.githubusercontent.com` serves it | `max-age=300`, so up to 5 min |
| TRMNL polls | `refresh_interval: 60`, so up to 60 min |

The daily job publishes all three days at once, so a device does not
wait on the cron to see *its* midnight — the card for its tomorrow is
already in the file it fetched today.

## Determinism

```python
cycle, position = divmod(day_index, len(pool))
order = sorted(pool, key=lambda e: sha256(f"{SALT}|{cell}|{cycle}|{e['id']}"))
pick = order[position]
```

`day_index` is days since 1970-01-01. The hash gives a shuffle that is
stable across machines and Python versions without a schedule stored
anywhere, and the cycle number is inside the hash, so the next pass
through the pool comes out in a different order.

Consequences, which `daily.py --selftest` checks on every pool refresh:

- the same card all day, on every device
- no repeat until the pool has been all the way through
- a different order next time round
- different cells move independently, so two settings do not lock together

---

## Picture quality

Postcards are small, and a lot of what an archive files under "postcards"
is the *back* of one — an address panel, a postmark and someone's
handwriting. That is a real card and a useless picture.

`harvest.py` fetches each card at panel size in greyscale and measures
three things:

| metric | what it catches |
| --- | --- |
| `detail` | mean edge magnitude. A flat, empty or blank scan scores low. |
| `texty` | how much more the darkness alternates down the card than across it. Ruled lines of writing sit at 3 and up; a picture side sits near 1.5. |
| `mush` | the share of mid-grey. Recorded, not judged — see below. |

`mush` is measured but not used as a rejection, and that is deliberate.
An earlier version of this filter was calibrated at 1-bit, where every
mid-tone collapses into noise and perfectly good material looks broken.
Most TRMNL panels are 2-bit or 4-bit. Re-rendered at the depth the
hardware actually has, cards that looked unreadable at 1-bit read fine,
so the threshold came off.

Measurement is budgeted (900 cards a run) and cached in `quality.json`,
so each monthly refresh covers more of the pool than the last. An
unmeasured card is not held against itself.

`audit_orientation.py` answers a different question: is a card we call
"landscape" actually landscape? Orientation comes from pixel dimensions,
which assumes the scan is stored the way the card is meant to be read.
Comparing against the catalogue's own physical dimensions does not
settle it — over 1,292 Library of Congress records that disagreed 21.7%
of the time, and every disagreement read exactly "9.0 x 14.0 cm", the
standard postcard size applied as boilerplate rather than measured. Six
of those rendered as genuinely portrait cards, upright, captions along
the bottom, so the catalogue is the unreliable side. Looking is what
works: thirty cards inspected that way, none sideways, which bounds the
error rate near 10% rather than proving it zero.

`preview.py` renders upcoming picks as a contact sheet at the panel's own
grey depth. Whether a card is *legible* can be measured; whether it is
*interesting* cannot, and a contact sheet is the cheapest way to put that
question in front of someone who can answer it.

---

## Images and device sizes

The panels differ: an OG is 800×480, an X is 1872×1404 at a squarer
aspect, and there are Minis and Cores in between. Two shapes of source
handle that differently:

- **IIIF sources** (Digital Commonwealth, part of the Library of
  Congress) serve any size on request, so the feed asks for
  `!1872,1404` — the largest panel there is — and every smaller screen
  scales it down for free.
- **Fixed sources** (the Library of Congress postcard files) publish a
  ladder of derivatives topping out at a 1024px JPEG. That fills an OG
  panel outright and upscales acceptably on an X. Cards whose largest
  derivative is smaller than 1000px are dropped.

The feed asks for the **colour** scan rather than a greyscale one. A
colour panel can then show the colour, and a monochrome one loses
nothing: converting the colour file to luminance lands within 1/255 of
the archive's own greyscale version.

### The size is fixed, and the daily job warms it first

The markup must use `pick.image` as it stands and never compose a size
from `trmnl.device`. A IIIF server renders each derivative on demand,
and the *first* request for a given size on a large scan takes 12 to 18
seconds — measured on an 11016×7176 master: 15.2s to first byte cold,
0.43s once cached. TRMNL's renderer gives up long before that, and the
panel comes up blank with the caption still on it.

A device-derived box is a size nothing has warmed, freshly cold every
time the card changes. So `daily.py` asks for every image it is about to
publish before it writes the file that points at them — a `HEAD` is
enough, since the service still has to produce the image to report its
length — and the markup uses that one warmed URL.

---

## Files

| file | what it is |
| --- | --- |
| `harvest.py` | monthly crawl of every source into `pool.json` |
| `daily.py` | the day's picks → `postcard.json`, `today.json` |
| `make_settings.py` | regenerates `trmnl/settings.yml` from the pool |
| `preview.py` | contact sheet at panel grey depth |
| `sources.md` | every source considered, and why each is in or out |
| `pool.json` | the cached catalogue |
| `quality.json` | render measurements, accumulated across runs |
| `dimensions.json` | pixel sizes from IIIF `info.json`, accumulated |
| `today.json` | **the polling URL** — one pick per cell |
| `postcard.json` | today's card for the full catalogue, on its own |

---

## Setting up the TRMNL plugin

1. New private plugin, strategy **Polling**, URL:

   ```
   https://raw.githubusercontent.com/nikokoren/postcard_of_the_day/main/today.json
   ```

2. Paste `trmnl/settings.yml` into the plugin's settings, and
   `trmnl/example-markup.liquid` into the markup.

A private plugin has no loader for `{% include %}`, so
`example-markup.liquid` contains `selection.liquid` verbatim followed by
the layout. If you edit one, keep the other in step.

The markup gets `pick` (the day's card) and `card_image` (its URL, ready
to use at any panel size). The rest is layout.

### What the settings do

| keyname | type | default |
| --- | --- | --- |
| `orientation` | multi-select | empty — both |
| `region` | multi-select | empty — everywhere |
| `era` | multi-select | empty — every era |
| `show_caption` | true/false | true |
| `show_place` | true/false | true |
| `show_credit` | true/false | false |

Two things about TRMNL settings that cost real debugging time and are
worth knowing before you edit the Liquid:

- **A boolean arrives as the string `"true"` or `"false"`.** So
  `{% if show_caption %}` is true even when the reader switched it off.
  Compare against `"true"`, and give every field a sensible default for
  when it arrives as nothing at all.
- **A select sends back a value derived from the label, not the label.**
  "Latin America & the Caribbean" comes back as something like `latin_america_the_caribbean`. The exact derivation is
  not documented, so the feed ships a `keys_by_label` map carrying every
  spelling each option might arrive as. A lookup that missed would
  silently fall back to the whole catalogue — which looks exactly like
  the recipe ignoring the settings.

---

## Running it by hand

```bash
python3 harvest.py --pages 3 --no-measure   # a quick crawl, no Pillow needed
python3 daily.py --selftest                 # prove the schedule behaves
python3 daily.py --date 2027-01-01          # any day
python3 harvest.py --report                 # what is in the pool
python3 preview.py --cell landscape__europe__all --days 24
```

`harvest.py` needs Pillow only for the quality pass; `--no-measure`
skips it.

---

## Licensing

Public domain and openly licensed material only. Cards under a
Creative Commons licence carrying an **NC** or **ND** clause are dropped
at harvest time. A panel in a living room is arguably neither a
commercial use nor a derivative work, but "arguably" is not a licence,
and there is enough material without them.

Every pick carries its own `rights` string and a `source_url` back to the
catalogue record. `sources.md` has the per-source detail.
