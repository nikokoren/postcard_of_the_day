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

---

## Looked at and left out

**Europeana** (154,196 openly-licensed postcards, 29 countries) is the obvious
candidate for country breadth and it is the one that hurts to lose. Two things
disqualify it as a primary source:

- *Image size.* `edmIsShownBy` is a raw provider URL with no IIIF and no
  resizing. In a sample of 54 fetched images the median short side was **525
  pixels** and only 7 cleared 900. Most providers publish an 800px web copy and
  nothing larger. That is below the OG panel, never mind the X.
- *Titles.* The largest provider, Deutsche Fotothek (49,695 items, 32% of the
  pool), titles every record "Postkarte". The second largest, Estonia's muis.ee,
  titles them "Postkaart".

Worth revisiting per-provider: a handful of Europeana providers do serve IIIF at
full resolution with real captions, and could be allow-listed individually.

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
good resolution. It needs an access token issued by a request form. Next in
line if the country axis needs more depth.
