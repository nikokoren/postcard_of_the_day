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
| Region | seven, listed below | the place the card **shows**, not where it was printed |
| Era | Before 1900, 1900-1914, 1915-1929, 1930-1945, 1946 onwards | when it was printed, or posted where a postmark was transcribed |

### Depicted, not printed

"Where is this card from" has two answers and only one of them is
interesting. A card of Sorrento printed in Leipzig is a card of
Sorrento — and that is not a hypothetical: the postcard trade of the
1900s ran on German lithographers printing views of everywhere, so the
place of production is actively misleading.

The Library of Congress and Digital Commonwealth both record the
depicted place, in `location_country` and `subject_hiergeo_geojson_ssm`
respectively. Spot-checked against titles, those fields follow the
subject — Rothenburg ob der Tauber, Mexico City, Villeneuve-sur-Yonne —
and not the publisher.

The Rijksmuseum records no depicted place at all — no `about`, no
`represents`, no subject headings — so its place has to come out of the
production credit, the descriptive note or the title.

Using a production credit for a card's subject is normally
indefensible, for the reason above. But that describes an export
industry, not this collection: sampled across the set, the credits read
"photographer: Knud Knudsen, Norway", "publisher: Fujisawa Bunjirô,
Japan", "photographer, Suriname" — photographers and local publishers,
whose country is where the photograph was taken. Checked by eye, the
Japanese cards are Hiroshige's Tōkaidō stations and the Surinamese ones
the Paramaribo market. Germany and Switzerland appear as producers and
are where the general objection could still bite; they are 8 of 106
matches in a sample, and left in.

### When is a card from

Almost always the date is when the card was **printed**. The postmark is
on the back, and the back is usually not scanned, let alone transcribed.
About 1% of Library of Congress records do transcribe one — "Postmarked
1905", "Cancelled Sierra Leone stamp postmarked 1912" — and where they
do it is the better answer to how old a card is, so it wins and the
caption says "Posted 1912" rather than passing a printing date off as a
posting date.

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

The last two are unpacked but no longer drawn: the printer line and the
credit line came out of the layout, and `show_credit` with them. They
still ride in the payload, which costs **27KB of the 89KB feed** — worth
knowing, because `daily.py` drops cells to stay under the cap, so dead
fields are paid for in selectable combinations. Note before removing
them that 2,999 cards -- every Graz card, 16.9% of the pool -- are
CC BY-SA and their licence asks for the attribution that credit line
carried.

### A day that has been published does not move

The three-day feed only works if a day means the same thing tomorrow as
it did when it went out. It did not.

Every run used to choose all three days from the pool as it stood that
morning, and the pool moves: `score.py` rewrites `quality.json` daily,
and `daily.py` filters on it. The schedule turns on `divmod` by the
pool's size and a hash ordering over its membership, so **six cards
dropped out of 17,785 moved 23 of 95 cells** to a different card — for
days that were already on screens. A viewer east of UTC would see the
card turn over at their own midnight, correctly, and then turn over
*again* when the next file landed. The image probe did the same on a
smaller scale: only the middle day is probed, so a skip there disagreed
with the unprobed copy published the day before.

So `daily.py` now reads the live `today.json` before it writes one, and
any day that file already carries is copied across rather than chosen
again. The only thing that unseats a published pick is its image having
gone, which is worse than the change, and that is looked for on the
middle day alone. `--recompute` ignores the published feed, for when
something wrong has been published and needs to be unstuck.

One consequence worth naming: a card dropped from the pool keeps the day
it already holds and loses every day after it, which is what `score.py`
means by a card being out of *tomorrow's* picks. Another: a carried pick
was chosen against a slightly different pool, so the no-repeat guarantee
below is a guarantee about one schedule, not across a pool that changed
underneath it — as it always was, only now the seam is where it can be
seen rather than on somebody's panel.

### What that costs at each hop

| step | when |
| --- | --- |
| Actions cron fires | 00:05 UTC — in practice 04:20–05:15 UTC, since that slot is a busy one |
| `daily.py` picks, warms every image, commits | ~1–4 min |
| `raw.githubusercontent.com` serves it | `max-age=300`, so up to 5 min |
| TRMNL polls | `refresh_interval: 60`, so up to 60 min |

The daily job publishes all three days at once, so a device does not
wait on the cron to see *its* midnight — the card for its tomorrow is
already in the file it fetched today. That is also why the queue delay
is survivable rather than urgent: a device at +14 only falls off the end
of the three days if a run lands later than 10:00 UTC.

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

These describe one schedule, against one pool. A day that has already
been published is pinned to what it published, so a pool that changes
moves only the days nobody has seen yet — see *A day that has been
published does not move*.

---

## Picture quality

Postcards are small, and a lot of what an archive files under "postcards"
is the *back* of one — an address panel, a postmark and someone's
handwriting. That is a real card and a useless picture.

Each card is fetched at panel size in greyscale, **the mount is cropped
off**, and the card is measured:

| metric | what it catches |
| --- | --- |
| `detail` | mean edge magnitude. A flat, empty or blank scan scores low. Limit 10.0 — see below. |
| `texty` | how much more the darkness alternates down the card than across it. Ruled lines of writing sit at 3 and up; a picture side sits near 1.5. |
| `rhythm` | how strongly the brightness down the most text-like quarter-width strip repeats at one fixed line pitch. |
| `flat` | how much of that same strip sits within a narrow band of its own commonest tone. |
| `mush` | the share of mid-grey. Recorded, not judged — see below. |

`texty` is a whole-card average, which means it only catches a card that
is text all over. It is blind to the commonest spoiler of the lot: a
card that is *half* picture and half small print. "Milano. Castello
Sforzesco. Sala delle Asse" is two thirds of a photograph and one third
of a column of Italian history set in six-point type; it scored 1.17
against a limit of 3.0, and went out as a postcard of the day.

`rhythm` and `flat` look for the panel rather than for a texty card.
Lines of type repeat at a fixed spacing and keep repeating for the
height of the panel, which is what separates them from the things in a
photograph that also repeat — masonry courses, balconies, railings,
waves — since those drift and die out within a few cycles. And type is a
few dark marks on one flat ground, so most of the strip sits near a
single tone; a photograph's tones are spread out. Both are needed: a
Philadelphia gateway scores 0.64 on its brickwork and holds only 0.31 of
its strip near one tone.

`flat` is measured against the strip's own modal tone rather than
against white, because half these scans are sepia or underexposed and an
absolute brightness test threw away every dark one. *La Brabançonne* —
the Belgian anthem printed in full, nothing else on the card — has 0.07
of its strip above a normal paper cut and 0.73 of it near its own tone.

### The mount

Several archives scan the card on a dark board and keep the board. Graz
does it on nearly every card, sometimes at half the frame. Every metric
here is an average over what it is given, so the mount quietly ruins all
of them — and it did. Turning scoring on for the first time dropped 101
cards as blank scans, of which 26 were sharp Graz street scenes whose
`detail` had been halved by the board around them; and a strip of plain
board is flat by definition, so the panel rule threw out a Graz street
with a horse cart on it, at 0.492 and 0.72.

So the mount comes off before anything is measured. A row of mount is
flat, so the card is the run of rows and columns whose spread rises
clear of the flattest; if that leaves less than a third of the frame the
crop is refused, on the grounds that something other than a mount is
going on.

It also settled the flat-scan limit, which had been 14.0 since before
anything was ever measured. On the first real run of 3,000 cards that
turns out to be the **5th percentile**, and it was throwing out the
Jakominiplatz, the Graz Herrengasse, the Erechtheion and the Library of
Congress — ordinary, perfectly sharp cards whose only fault was a soft
archive scan. It is now 10.0, the 1st percentile, and the band beneath
it really is washed out: faded studio portraits, ghosts of photographs,
one blank card back. Rejections overall went from 4.7% of a scored
batch to 1.6%.

Taking the mount off moves every number, which is why the panel
thresholds were calibrated twice. It also enlarges the card inside the fixed measuring
width, so every line pitch grows with it: *The New Colossus* fell from
0.413 to 0.069 because its lines had walked out of the range being
searched, and the ceiling went from 36 pixels to 44.

### Where the line is

Calibrated on 2,725 cards drawn at random, every rejection looked at:
the rule drops **12, or 0.44%**. Four cards that are nothing but a
printed poem or a board of rates, three portraits with the poem set
beside them, two written across in ink, a scan with a photographic step
wedge in the frame, and both Milano cards. Nothing that is a picture.

It is set for precision over recall, because the two failures are not
equal: a false positive quietly deletes a good card from the catalogue,
where a miss only means somebody gets a dull morning. The cards just
inside the line are real pictures that happen to repeat — a YWCA
cafeteria's shelves of tins reach 0.483, a Graz card with a handwritten
third reaches 0.486 — so the two are not separable, and the cut goes
above both. Known misses, accepted: that Graz card, a Boulogne quay with
a table of shipping fares beside it, and a French château with its
history printed alongside.

`score.py --selftest` holds all twenty-two of those measurements, and
both workflows run it.

`mush` is measured but not used as a rejection, and that is deliberate.
An earlier version of this filter was calibrated at 1-bit, where every
mid-tone collapses into noise and perfectly good material looks broken.
Most TRMNL panels are 2-bit or 4-bit. Re-rendered at the depth the
hardware actually has, cards that looked unreadable at 1-bit read fine,
so the threshold came off.

Measuring lives in **`score.py`**, which the daily job runs, and is
cached forever in `quality.json`. `harvest.py` can do it too, but it
only runs monthly and has a crawl to get through first, so at its budget
the pool would have been covered some time in 2046 — and until a card is
measured it is eligible to be somebody's postcard of the day. That is
exactly how the Milano card got out.

The order is what makes it useful on day one. The schedule is
arithmetic, so the cards due on a screen this fortnight are knowable
rather than guessable, and those are measured first; the rest of the
pool follows a budget at a time.

One trap, worth knowing because both `score.py` and `translate.py` fell
into it: work that walks the schedule must load the pool through
`daily.load_pool()`, never straight from `pool.json`. Regions are
attached at load time, so raw entries carry no `rg`, every region cell
selects nothing, and the walk silently returns a fraction of the
schedule and calls it the whole of it — 375 cards instead of 1,693,
missing the one that was on a screen that morning. The verdict is applied when `daily.py`
loads the pool, not when `harvest.py` writes it, so a card that scores
badly is out of tomorrow's picks rather than out of whichever month the
next crawl lands in. An unmeasured card is still not held against
itself.

    python3 score.py --budget 2000 --upcoming 21
    python3 score.py --report

## Subject balance

The per-country cap is the only thing standing between one holding and
the whole catalogue, and at 6,000 it has never once bound. It also
counts the wrong thing: nobody notices a country, they notice a subject.

The Rijksmuseum's postcards turn out to be, in large part, the photo
archive of the Dutch royal house. 240 cards — only **1.4%** of the pool,
but **26% of Portrait + Europe + 1930-1945**, one every four days,
because that cell holds 139 cards in total. The same collection
contributes 239 studio portraits of people the museum itself cannot
name.

Two rules, and deliberately not a third:

- A card whose subject is a sitter nobody can identify is not a postcard
  from anywhere. It goes.
- A monarch photographed against a studio curtain is the same problem
  with a name attached, so that goes too — but the royal cards that show
  something *happening somewhere* stay: the funeral cortège at Delft,
  the stork at Paleis Noordeinde, the state visit to Leeuwarden. What is
  left is capped at two per identical caption, because the archive holds
  ten photographs of one wedding and a viewer cannot tell them apart
  from the line under the picture.

Together that takes the royals down by 53% and the worst cell from 26%
to 10.4% — one every ten days instead of one every four. Across the
whole catalogue they fall to 0.7%, one every 153 days.

The third rule, a general cap on repeated captions, is deliberately
absent. 108 cards in this pool are titled simply `Graz`, and they are
different views of a city somebody wants to keep seeing. `daily.py
--selftest` holds that case alongside the rest.

Like the region table and the translations, this is applied where
`daily.py` loads the pool, so the judgement can be revised without a
re-crawl.

---

One filter that only exists because somebody looked: the Rijksmuseum
files **boxes, albums and mounted lots** under the same type as single
cards, and an album spread renders as four stamps of a picture. The
stated dimensions give them away — a `depth` means a container, and
`199 x 255 mm` is a page rather than a card — and 629 of 2,500 resolved
records fail one of those tests.

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

## Captions in English

Just under a third of the pool is catalogued in the language of whoever
wrote the card. Faithful to the object, no use at all to someone who
cannot read it, and a panel has room for one caption rather than two —
so the English replaces the original rather than sitting beside it. The
original stays in `pool.json` either way, because a machine translation
is not the record.

Translation is offline and free: Argos Translate, no key, no service to
depend on. Roughly a second per string, so it is budgeted per run and
cached forever in `translations.json`, keyed by the **string** rather
than by the card — 17,785 cards carry about 16,800 distinct captions, and the
duplicates are free. Applied when `daily.py` loads the pool, so a better
translation never needs a re-crawl.

**Both lines of the caption, not just the title.** The place line is a
real place name at Digital Commonwealth ("Kingston", "Istanbul"), but at
Graz it is the catalogue's own German description of the view — *"Blick
zum Schloßberg vom Süden mit Tegetthoffbrücke"*. That is the more
interesting of the two lines, and for 3,001 cards it was going out
untranslated underneath a setting that calls it the place. Proper nouns
survive the trip: Schloßberg, Herrengasse and Hauptplatz come back
intact, and Tegetthoffbrücke becomes Tegetthoff Bridge.

Three things the translator gets wrong, and a guard against each. A
place name at the head of a caption is vocabulary to a translator —
"Dameron. Le coin des laveuses" came back as "Lady. The corner of the
washing machines" — so the head is held back and only the rest is
translated, for Latin-script heads where holding it back still leaves
something readable. Detection on a five-word caption is a coin toss
between neighbouring languages, so where a source speaks one language
its word beats the detector's guess.

And a full stop is not always the end of anything. Half the
abbreviations a catalogue uses end in one, and splitting the head there
hands the translator a fragment starting mid-phrase. Both ways that
goes wrong were live until somebody read the output: *"Arkaden im
Innenhof Hauptplatz Nr. 16"* split after "Nr" and translated "16", so
the German went out untouched; and *"'Vulcan Face' in Mt. Lassen
eruption"* split after "Mt" and translated the remainder as German,
where *lassen* means *let*, so it went out as **"Mt. Let eruption"**.
A lone capital is the same trap wearing a hat: *"Vier portretten van
acteur M. Lüzenkirchen"* split after the M and handed the surname to
the translator on its own.

1,279 captions were being cut at an abbreviation or an initial, and
they were redone.

### A head is only held back if it is a name

The head rule took up to five words on trust, which swept in whole
German clauses. *"Deutscher Gruß aus Graz. Jakominiplatz"* held back
everything before the stop and went out with the German intact, and so
did *"Aus Vorarlberg"*, *"Blick auf Riga"* and *"Gruss aus Baden bei
Wien"* — 166 captions. A head that opens with a preposition or with
greetings-from / view-of, or that carries a German particle between its
words, is a sentence fragment, so it now goes through with the rest.
French and Italian name particles are deliberately not in that test:
"Musee de Cluny" and "Abbaye St Germain des Pres" are names.

### And a short glossary, for what the translator will not touch

Some terms it reads as names at any length. **`Alt-Graz`** is the
archive's own word for its historical-views series — *old Graz* — and it
came back as `Alt-Graz` when translated in context, and as **"Old
Great"** when translated alone, the translator having decided *Graz* was
a superlative. `Gruss` survives the same way.

So a handful of terms are substituted after translation, against
captions that were translated, never against English ones. The guard
that matters is the word boundary: `Schlossberg` keeps its *Schloss*,
and Altenburg and Altona keep their *Alt*. `translate.py --selftest`
holds those three as must-not-change alongside the substitutions
themselves, and holds every rule above next to the heads that must
still be held back — Dameron, Constantinople, Graz, Milano — so the
next person to touch the expression can see what each clause is for.

---

## Files

| file | what it is |
| --- | --- |
| `harvest.py` | monthly crawl of every source into `pool.json` |
| `daily.py` | the day's picks → `postcard.json`, `today.json` |
| `make_settings.py` | regenerates `trmnl/settings.yml` from the pool |
| `score.py` | measures cards for whether they read on a panel |
| `translate.py` | puts the captions into English |
| `preview.py` | contact sheet at panel grey depth |
| `sources.md` | every source considered, and why each is in or out |
| `pool.json` | the cached catalogue |
| `quality.json` | render measurements, accumulated across runs |
| `translations.json` | English captions, accumulated across runs |
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
the layout. If you edit one, keep the other in step — and
`trmnl/test_selection.py` now checks that you did.

That check exists because they drifted. The local-midnight rotation was
fixed in `selection.liquid` on 14 September and not in the markup, so
for three days the fix was in the repo and not on any screen. **The
plugin runs `example-markup.liquid`; every test above reads
`selection.liquid`.** A divergence means the tests are passing against a
file nobody runs.

The markup gets `pick` (the day's card) and `card_image` (its URL, ready
to use at any panel size). The rest is layout.

### What the settings do

| keyname | type | default |
| --- | --- | --- |
| `orientation` | multi-select | empty — both |
| `region` | multi-select | empty — everywhere |
| `era` | multi-select | empty — every era |
| `show_caption` | boolean | true |
| `show_place` | boolean | true |

Two things about TRMNL settings that cost real debugging time and are
worth knowing before you edit the Liquid:

- **A boolean arrives as the string `"true"` or `"false"`.** So
  `{% if show_caption %}` is true even when the reader switched it off.
  Compare against `"true"`, and give every field a sensible default for
  when it arrives as nothing at all. The markup accepts `false`, `"0"`
  and `"no"` as well — the string is what TRMNL sends today, but their
  docs describe what a field *declares*, not what Liquid *receives*, and
  guessing wrong here fails silently.
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
python3 daily.py --recompute                # unpin the days already published
python3 harvest.py --report                 # what is in the pool
python3 preview.py --cell landscape__europe__all --days 24
```

`harvest.py` and `score.py` need Pillow and numpy for the quality pass.
`harvest.py --no-measure` skips it; `score.py` without them scores
nothing rather than scoring wrongly, and an unmeasured card is kept.

---

## Licensing

Public domain and openly licensed material only. Cards under a
Creative Commons licence carrying an **NC** or **ND** clause are dropped
at harvest time. A panel in a living room is arguably neither a
commercial use nor a derivative work, but "arguably" is not a licence,
and there is enough material without them.

Every pick carries its own `rights` string and a `source_url` back to the
catalogue record. `sources.md` has the per-source detail.

### Europeana, and the one secret

Europeana is an aggregator: the metadata is central, the image stays on the
contributing museum's own server. Asked for openly-licensed postcard images it
offers 202,453, and a pooled sample of those has a median short side of 525
pixels — so it is a **discovery engine**, not a source. Providers are
allow-listed one at a time, each checked by hand before it goes in.

Germany, checked in full: 59,433 openly-licensed postcard images, of which
49,795 are Deutsche Fotothek's — excellent scans, and every card titled
`Postkarte`, with the real caption unreachable through Europeana, their own
site, or the Deutsche Digitale Bibliothek. Two providers clear everything, and
they are the two that are in.

This is the only part of the repo that needs a credential. It reads
`EUROPEANA_KEY` from the environment and is set in Actions as a repository
secret; **the key is never a file in the repo**. Without it the source prints a
line and skips itself, and the rest of the harvest runs exactly as before, so a
clone works unchanged.

    python3 harvest.py            # Europeana skipped, everything else runs
    EUROPEANA_KEY=... python3 harvest.py
