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
| Country | whatever the archives hold, with enough cards to be worth offering | `subject_hiergeo_geojson_ssm` (Digital Commonwealth), `location_country` (Library of Congress) |
| Era | Before 1900, 1900-1914, 1915-1929, 1930-1945, 1946 onwards | the catalogue date |

A country is offered only if at least **60** cards sit behind it. Below
that the selector is promising something it cannot keep — a reader who
picks it would see the same handful of cards come round every two
months.

### Why the feed ships *cells* rather than selections

Three axes with dozens of values between them is 2ⁿ possible selections,
and a static file cannot carry one entry per selection. What it can carry
is one entry per **cell** — a single point like
`portrait__france__era-1900-1914`. A reader who picks two countries and
two eras is choosing among four cells, and the markup rotates over
whichever of them the feed actually has, a day at a time.

Cells with fewer than **20** cards behind them are not shipped. Some are
genuinely empty and always will be, which is why the Liquid loosens one
axis at a time rather than falling straight through to the whole
catalogue: asking for portrait cards from Morocco before 1900 should
still get you a card from Morocco.

The feed is capped at **95KB**, under TRMNL's 100KB polling limit, and
`daily.py` refuses to publish a payload over it rather than letting the
plugin degrade.

---

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
| `country` | multi-select | empty — everywhere |
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
  "United States" comes back as `united_states`. The exact derivation is
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
python3 preview.py --cell landscape__france__all --days 24
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
