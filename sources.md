# Where the postcards come from

Everything here is a **real printed postcard** -- a card that was published,
sold and (often) posted -- not a photograph of a place, not a negative, not a
photographic print that happens to look like one. That distinction is what the
source filters below buy us.

Every source must clear five bars before it goes in:

1. **Machine readable.** A documented JSON API, no scraping.
2. **No credential the recipe owner has to babysit.** Anonymous, or a free key
   that lives in an Actions secret.
3. **Openly licensed.** Public domain, no known restrictions, CC0, CC BY or
   CC BY-SA. Anything NC or ND stays out -- see *Rights* below.
4. **Resizable images.** A IIIF Image API endpoint, so one record serves an
   800x480 OG panel and a 1872x1404 X panel from the same source file.
5. **A title worth printing.** A card captioned only "Postcard" is no use on a
   screen whose whole job is to tell you what you are looking at.

---

## In the pool

### Digital Commonwealth (Boston Public Library and ~200 Massachusetts partners)

- API: `https://www.digitalcommonwealth.org/search.json` (Blacklight JSON, no key)
- Filter: `f[genre_specific_ssim][]=Postcards`
- Images: IIIF Image API 2.1 at `https://iiif.digitalcommonwealth.org/iiif/2/<key>/`
- Size: 52,159 records carry the Postcards genre; 47,692 of those are openly
  licensed (30,165 "no restrictions" + 17,527 "creative commons").

Why it leads: the metadata is unusually complete for this kind of material.
Every record has a date (`date_start_dtsi` / `date_end_dtsi`), every record has
an image, and 92-96% carry `subject_hiergeo_geojson_ssm` -- a structured
`{continent, country, region, city}` blob, which is where the country axis
comes from. Titles are the real card captions, not "Postcard".

Its weakness is provenance: this is a Massachusetts aggregator, so the country
distribution is roughly 90% United States. Two collections dominate --
Tichnor Brothers (~29,000 American linen cards, almost all 1930s) and the
Nicholas Catsimpoolas Collection (Greek cards, mostly 1900-1910).

### Library of Congress -- Africana Historic Postcard Collection

- API: `https://www.loc.gov/collections/africana-historic-postcard-collection/?fo=json`
- Images: IIIF at `https://tile.loc.gov/image-services/iiif/<service>/`
- Size: ~1,300 cards

Small, but it is the only source here that is *entirely* printed postcards from
outside Europe and North America, and the titles are the cards' own printed
captions ("UN CAMP DANS LE RUANDA", "RUTSHURU -- LE POSTE"). It carries the
country axis somewhere Digital Commonwealth cannot.

---

## Rights

Digital Commonwealth exposes reuse as a facet, `reuse_allowed_ssi`, with two
values. The harvest takes both and records which applies per card:

| value | count (Postcards) | what it means |
| --- | --- | --- |
| `no restrictions` | 30,165 | `rightsstatement_ss` = "No Copyright - United States" |
| `creative commons` | 17,527 | a CC licence, per-record in `license_ss` |

The remaining ~4,467 carry neither and are skipped. Note that the two facet
values **AND** together in Blacklight, so asking for both in one query returns
zero -- the harvest issues one crawl per value.

Within the CC bucket, records whose licence carries an `NC` or `ND` clause are
dropped at harvest time. A TRMNL screen is arguably neither commercial nor a
derivative, but "arguably" is not a licence, and there is enough material
without them.

### Rijksmuseum

- API: `https://data.rijksmuseum.nl/search/collection` (Linked Art Search, **no key**)
- Filter: `type=prentbriefkaart&imageAvailable=true` — the search takes Dutch or
  English terms interchangeably
- Images: IIIF Image API 2 (Level 2) at `iiif.micr.io`
- Size: **16,020** cards with images
- Rights: **Public Domain Mark** on the image, **CC0** on the metadata

Verified end to end rather than from the documentation. A sampled record --
*Reclame voor de Beverwijksche Conservenfabriek*, 91 x 142 mm, with
`Postkarte - Carte postale` printed on the verso -- resolved to a 5546 x 3633
master, which served `!800,480` in 1.22s and `!1872,1404` in 1.98s.

The one cost is that the image takes three hops to reach: the object record
carries `shows` -> a VisualItem, which carries `subject_of` -> a DigitalObject,
which finally carries the IIIF URL. Two extra requests per card on a monthly
crawl, and cacheable like everything else.

Two things turned out differently from the documentation.

**The image is three records deep, not one.** Linked Art separates the object
from the image *of* the object from the file that image is served as:
`HumanMadeObject -> shows -> VisualItem`, then
`VisualItem -> digitally_shown_by -> DigitalObject`, and only then an
`access_point` carrying the IIIF endpoint. It is tempting to stop at the
visual item's `subject_of`, which also holds a digital object -- that one is
the catalogue web page, and taking it fails silently on every record. So one
card costs three requests, the full 16,020 costs about 48,000, and resolution
is budgeted and cached like everything else here.

**Every record states the card's own physical size** -- "height 91 mm x width
142 mm" -- which is a better orientation signal than the scan's proportions,
because it describes the card rather than somebody's scanning decision. So
these entries skip the pixel-dimension gate entirely and take the museum at
its word. It is the cross-check the Library of Congress could not provide.

Where it disappoints is place. The record has no depicted place at all -- no
`about`, no `represents`, no subject headings -- and its only geography is
`took_place_at` under production, which is where the card was *printed*.

That is not a usable substitute. The postcard trade of the 1900s ran on German
lithographers printing views of Italy, Egypt and everywhere else, so a printer's
country says nothing about what is on the front. An earlier version of this
adapter used it and placed 533 cards; every one was the publisher rather than
the subject. It now reads the title instead, which places 24 -- the ones whose
caption happens to name a country rather than a town.

Losing 509 placements to gain correctness is the right trade. The unplaced
cards are still in the pool and still appear in the all-regions rotation, which
is what an unconfigured recipe shows anyway; they simply do not claim to be
from somewhere they are not. A gazetteer of city names would recover a lot of
them -- most of these titles name a Dutch town -- and is the obvious next
improvement if the region axis needs the depth.

---

## Looked at and left out

**Europeana** (154,196 openly-licensed postcards, 29 countries) is the obvious
candidate for breadth and it is the one that hurts to lose. Two things
disqualify it *as a primary source*:

- *Image size.* `edmIsShownBy` is a raw provider URL with no IIIF and no
  resizing. In a sample of 54 fetched images the median short side was **525
  pixels** and only 7 cleared 900. Most providers publish an 800px web copy and
  nothing larger. That is below the OG panel, never mind the X.
- *Titles.* The largest provider, Deutsche Fotothek (49,695 items, 32% of the
  pool), titles every record "Postkarte". The second largest, Estonia's muis.ee,
  titles them "Postkaart".

The way back in is to use it as a **discovery engine** rather than a source.
Europeana is an aggregator: it holds the metadata centrally but the image stays
on the contributing institution's own server, which is exactly why the average
is so poor. So query Europeana to find which individual providers serve
full-resolution IIIF with real captions, allow-list those providers, and then
fetch from them directly. That turns a fatal average into a usable subset, and
it is how any further European postcard source is likely to be found.

**Wikimedia Commons** is the other big one -- `Postcards of France` alone has
86,235 files, `Postcards of Germany` 81,166, across ~180 country categories,
with arbitrary-width thumbnails via `iiurlwidth`. It is the best answer to the
country axis that exists. It is left out of the first cut for one operational
reason: anonymous API access rate-limits hard and unpredictably from shared
egress IPs (the kind GitHub Actions runs on). A 429 mid-crawl is survivable; a
429 for the whole window is not. Adding Commons means a resumable, heavily
throttled crawler and probably an OAuth client, which is its own piece of work.

**Library of Congress -- Photochrom Prints** (7,501) and **Detroit Publishing
Company** (25,400) are gorgeous, worldwide, high-resolution and public domain,
and the photochrom process is the direct ancestor of the picture postcard. They
are out because they are *views*, not cards -- no caption panel, no divided
back, never printed as postage. If the definition ever loosens, these are the
first two collections to add.

**Smithsonian (National Postal Museum)** holds real cards and the key is
already in hand from the object-of-the-day work, but its postcard holdings lean
philatelic -- postal stationery and covers rather than picture postcards.
Worth a proper count before adding.

**NYPL Digital Collections** has substantial international postcard holdings at
good resolution. It needs an access token issued by a request form.

**The Metropolitan Museum of Art** is key-free with CC0 images and is an
excellent source of *objects*, but it holds no postcards to speak of: 142 hits
for "postcard", 41 of them public domain, and the search is loose enough that a
Patinir triptych came back among them.

**The British Museum** is ruled out on licensing, not on quality. Its images are
CC BY-NC-SA. The `SA` clause is survivable -- ShareAlike only bites on
adaptations, and Creative Commons treats resizing as format-shifting rather than
adaptation -- and `BY` costs nothing, since the credit is printed anyway. `NC`
is the problem: a screen in someone's home is plainly non-commercial, but the
recipe is published to a public library, on hardware someone sells, and can be
installed in a shop. That is not a use anyone can promise stays
non-commercial.
