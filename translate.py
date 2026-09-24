#!/usr/bin/env python3
"""
Put the captions into English.

Just under a third of the pool is catalogued in the language of whoever
wrote the card -- 3,820 German titles, 1,611 Dutch, 1,608 French, and so
on down to a few hundred Greek. Faithful to the object, no use at all to
somebody who cannot read it, and a panel has room for one caption rather
than two.

So the English replaces the original rather than sitting beside it. The
original stays in the pool either way, because a machine translation is
not the record and should never be mistaken for it -- it is a caption.

Translation is offline and free: Argos Translate, no key, no service to
depend on, language packs a couple of megabytes each. Roughly a second
per title, so the work is budgeted per run and cached forever in
translations.json, keyed by the *title* rather than by the card. That
last detail matters more than it sounds: 18,206 cards carry 15,876
distinct titles, and the duplicates are free.

Applied when daily.py loads the pool, not when harvest.py writes it, so
a better translation never needs a re-crawl.

    python3 translate.py                 # a budget of titles
    python3 translate.py --budget 4000
    python3 translate.py --report
"""

import argparse
import collections
import json
import re
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
POOL_PATH = os.path.join(HERE, "pool.json")
CACHE_PATH = os.path.join(HERE, "translations.json")

BUDGET = 1200
MIN_CONFIDENCE = 0.55

# A caption is usually "PLACE. What you are looking at", and the place is
# the whole point of a postcard. Left to itself the translator treats it
# as vocabulary: "Dameron. Le coin des laveuses" came back as "Lady. The
# corner of the washing machines". So the head is held back and only the
# description is translated.
HEAD_SPLIT = re.compile(r"^(.{2,40}?)((?:\.\s+|\s+[-–]\s+))(.+)$")

# ... except that a full stop is not always the end of anything. Half
# the abbreviations a catalogue uses end in one, and splitting there
# hands the translator a fragment starting mid-phrase. Two ways that
# goes wrong, both live before this was added: "Arkaden im Innenhof
# Hauptplatz Nr. 16" split after "Nr" and translated "16", so the
# German went out untouched; and "'Vulcan Face' in Mt. Lassen eruption"
# split after "Mt" and translated the remainder as German, where lassen
# means let -- it went out as "Mt. Let eruption".
# A lone capital is an initial -- "Vier portretten van acteur M.
# Lüzenkirchen" split after the M and translated the surname as Dutch.
ABBREV_HEAD = re.compile(
    r"(?:\b(?:Nr|No|Nos|N|St|Ste|Str|Sta|Dr|Mr|Mrs|Ms|Prof|Sgt|Capt|Gen|Col"
    r"|Bd|Av|Ave|Blvd|Pl|Sq|Rd|Ft|Mt|Mts|Is|Co|Cos|Inc|Ltd|Bros"
    r"|vol|no|ca|cca|p|pp|fig|ch)|(?<![^\W\d_])[A-Z])$", re.I)

# The head is held back on the assumption that it is a name, and up to
# five words of it were taken on trust. That swept in whole phrases:
# "Deutscher Gruß aus Graz. Jakominiplatz" held back everything before
# the stop and went out with the German intact, and so did "Aus
# Vorarlberg", "Blick auf Riga" and "Gruss aus Baden bei Wien". A head
# that opens with a preposition or with greetings-from / view-of, or
# that carries a German particle between its words, is a sentence
# fragment and not a name, so it goes through with the rest.
#
# French and Italian name particles are deliberately absent from both:
# "Musee de Cluny" and "Abbaye St Germain des Pres" are names, and the
# translator leaves them alone anyway.
PHRASE_LEAD = re.compile(
    r"^(?:aus|auf|von|vom|zum|zur|bei|um|gruss|gruß|grüsse|grüße|gruesse"
    r"|blick|ansicht|partie|souvenir|groeten|uit|vue)\b", re.I)
PHRASE_WORD = re.compile(
    r"\s(?:aus|auf|von|vom|zum|zur|bei|mit|gegen|um|nach|über|unter|im|am)\s",
    re.I)

# And a handful of terms the translator will not touch at any length,
# because it reads them as names. "Alt-Graz" is the archive's word for
# its historical-views series -- old Graz -- and came through as
# "Alt-Graz" with context and as "Old Great" without it, having decided
# Graz was a superlative. Applied after translation, to captions that
# were translated, so an English caption is never rewritten.
GLOSSARY = [
    (re.compile(r"\bAlt[-\s]Graz\b", re.I), "Old Graz"),
    (re.compile(r"\bGru(?:ss|ß)\b"), "Greetings"),
    (re.compile(r"\bDeutscher Greetings\b"), "German greetings"),
]

# Detection on a five-word caption is a coin toss between neighbouring
# languages -- "Constantinople. Obelisque de Theodose" came back as
# Portuguese, which turned Constantinople into Constantine. Where the
# source is single-language we say so instead of guessing.
SOURCE_LANG = {"graz": "de", "rijks": "nl"}

NON_LATIN = re.compile(r"[^\x00-\x7F\u00C0-\u024F\u1E00-\u1EFF]")

# Detected on short place-name captions, these are almost always a
# misread of a neighbouring language, and Argos has no pack for most of
# them anyway. Skipping is better than translating from the wrong one.
SKIP_LANGS = {"en", "af", "no", "da", "sw", "tl", "cy", "so", "et"}


def texts_of(entry):
    """Every string on a card that reaches the panel as prose."""
    return [t for t in (entry.get("t"), entry.get("pl")) if t]


def upcoming_captions(entries, days):
    """
    The captions that will actually be on a screen in the next `days`,
    soonest first.

    Both lines of the caption, not just the title. The place line is a
    real place name at Digital Commonwealth, but at Graz it is the
    catalogue's own German description of the view -- "Blick zum
    Schloßberg vom Süden mit Tegetthoffbrücke" -- which is the more
    interesting of the two lines and was going out untranslated under a
    setting that calls it the place.

    The schedule is arithmetic, so this is knowable rather than guessable
    -- and it is the difference between a translation pass that shows up
    tomorrow and one that shows up whenever it reaches that card. For
    each cell the order within a cycle is fixed, so the order is computed
    once and walked, rather than asking for each day separately.
    """
    sys.path.insert(0, HERE)
    import daily
    from datetime import date, timedelta

    today = date.today()
    cells = daily.build_cells(entries, daily.regions_in(entries))
    seen, ordered = set(), []
    for key in cells:
        subset = daily.cards_for(entries, key)
        if not subset:
            continue
        total = len(subset)
        cycle, position = divmod(daily.day_index(today), total)
        run = daily.order_for(subset, key, cycle)
        for step in range(min(days, total)):
            index = position + step
            entry = (run[index] if index < total
                     else daily.order_for(subset, key, cycle + 1)[index - total])
            for text in texts_of(entry):
                if text not in seen:
                    seen.add(text)
                    ordered.append((step, text))
    ordered.sort(key=lambda row: row[0])
    return [text for _, text in ordered]


def load_cache():
    """
    Every string we have looked at, keyed by the string itself.

    Version 1 called this "titles", because titles were all it held.
    Read both, so the 11,590 already in the file are not thrown away
    when the place lines join them.
    """
    try:
        with open(CACHE_PATH) as fh:
            data = json.load(fh)
        return data.get("texts") or data.get("titles") or {}
    except (OSError, ValueError):
        return {}


def save_cache(cache):
    tmp = CACHE_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"version": 2, "count": len(cache), "texts": cache},
                  fh, ensure_ascii=False, separators=(",", ":"),
                  sort_keys=True)
        fh.write("\n")
    os.replace(tmp, CACHE_PATH)


# ============================================================
# what a translation is not allowed to do
# ============================================================

# A caption of one word is a name -- a town, a canton, a building -- and
# a translator handed a name with no sentence around it translates it as
# vocabulary. Every one of the 107 single-word captions in the cache came
# back changed, and the changes were not improvements: Asmara, Belmar and
# Finmarken all became "Home", Graubunden became "Grey bandages", Alt-Graz
# "Old Great", Antietam "Antimony", Maine "Repute".
#
# A few were real gains -- Alpenrose to Alpine rose, Glockenturm to Bell
# Tower -- and they are not worth the trade. A German noun left in German
# is a caption a reader half-follows; a town renamed "Home" is a caption
# that lies, and the reader cannot tell which they are looking at.
def is_one_word(title):
    return len(title.split()) == 1


# Nothing may appear in a translation that was not in what it translates.
# Machine translation of a short, contextless string does not merely err,
# it occasionally invents: "Bereg Baikala" (the shore of Baikal) came back
# as "Fuck that time", the Turkish town Selcuk as "Fuck", and the Romanian
# "Tigani ciurari" -- Roma sieve-makers -- as a racial slur. These go on a
# wall in someone's home.
#
# The test is comparison, not a word list applied to the output: a caption
# about the French town of Bitche must survive, and it does, because the
# source says it too.
SLURS = re.compile(
    r"\b(fuck\w*|shit\w*|cunt\w*|bitch\w*|bastard|wank\w*|arse\w*|asshole|"
    r"nigg\w+|fag(?:got)?s?|whore|slut|piss\w*|dick(?:head)?|prick|"
    r"chink|spic|kike|wetback|retard\w*|tranny)\b", re.I)


def invents_slur(source, english):
    """True when the English says something foul the source did not."""
    if not english:
        return False
    found = {m.group(0).lower() for m in SLURS.finditer(english)}
    if not found:
        return False
    already = {m.group(0).lower() for m in SLURS.finditer(source or "")}
    return bool(found - already)


def usable(source, english):
    """Whether a translation may be published at all."""
    if not english:
        return False
    if is_one_word(source):
        return False
    return not invents_slur(source, english)


def split_head(title):
    """(place, separator, rest) if the caption opens with a place."""
    m = HEAD_SPLIT.match(title)
    if not m:
        return None, "", title
    head, sep, rest = m.groups()
    # A head with a verb in it is a sentence, not a place name.
    if len(head.split()) > 5:
        return None, "", title
    # And a head ending in an abbreviation is not a head at all -- the
    # stop belongs to the abbreviation, not to the caption.
    if ABBREV_HEAD.search(head.rstrip()):
        return None, "", title
    # Nor is a phrase a name.
    if PHRASE_LEAD.match(head) or PHRASE_WORD.search(head):
        return None, "", title
    # Only hold back a head the reader could already read. Protecting a
    # Greek or Cyrillic one leaves the caption unreadable, which is the
    # thing this whole exercise is for: "Άνατολικὴ ἄποψις ... - Άθῆναι"
    # came back with its head intact and its point lost.
    if NON_LATIN.search(head):
        return None, "", title
    return head, sep, rest


def apply_glossary(english):
    """What the translator would not translate, said in English."""
    for pattern, replacement in GLOSSARY:
        english = pattern.sub(replacement, english)
    return english


def detect(text):
    from langdetect import detect_langs, DetectorFactory, LangDetectException
    DetectorFactory.seed = 0
    try:
        best = detect_langs(text)[0]
    except LangDetectException:
        return None
    return best.lang if best.prob >= MIN_CONFIDENCE else None


def installed_pairs():
    import argostranslate.translate as t
    return {(a.code, b.code) for a in t.get_installed_languages()
            for b in a.translations_to if hasattr(a, "translations_to")} \
        if False else {
            (a.code, b.code)
            for a in t.get_installed_languages()
            for b in t.get_installed_languages()
            if a.code != b.code and a.get_translation(b)}


def ensure_pack(code, available, installed):
    """Install a language pack on demand, once."""
    if (code, "en") in installed:
        return True
    package = next((p for p in available
                    if p.from_code == code and p.to_code == "en"), None)
    if package is None:
        return False
    sys.stderr.write(f"  installing {code}->en\n")
    package.install()
    installed.add((code, "en"))
    return True


# Where the head-split must and must not cut, at the captions that
# taught it each rule. None means "translate the whole thing".
SPLIT_CASES = [
    ("Dameron",         "Dameron. Le coin des laveuses"),
    ("Constantinople",  "Constantinople. Obelisque de Theodose"),
    ("Graz",            "Graz. Schillerplatz"),
    ("Milano",          "Milano. Castello Sforzesco"),
    # An abbreviation's full stop is not the end of a head. Splitting
    # here translated "16" and let the German through, or handed the
    # translator "Lassen eruption" and got back "Let eruption".
    (None, "Arkaden im Innenhof Hauptplatz Nr. 16"),
    (None, "Alt-Graz Sackstraße Nr. 11 im Dezember 1911"),
    (None, '"Vulcan Face" in Mt. Lassen eruption, 8, 22, 14'),
    (None, "10th St. Bridge and dam, Beaver"),
    (None, "Baker's River & Mt. Moosilauke, Warren, N.H"),
    (None, "Advance Design Inc. Parsons (T-square) tables"),
    (None, "Vier portretten van acteur M. Lüzenkirchen in een rol"),
    (None, "Portret van J. P. Coen"),
    # A head with a verb in it is a sentence, not a place name.
    (None, "Blick vom Schlossberg auf die Altstadt - Graz"),
    # Nor is a phrase. These held back whole German clauses.
    (None, "Deutscher Gruß aus Graz. Jakominiplatz"),
    (None, "Aus Vorarlberg. Dornbirn, Burg Glopper"),
    (None, "Blick auf Riga - Sloof"),
    (None, "Gruss aus Baden bei Wien. Ruine Rauheneck"),
    (None, "Graz im Schnee. Hauptplatz"),
    # But a name with a French particle in it is still a name.
    ("Musee de Cluny",             "Musee de Cluny. Salle des thermes"),
    ("Abbaye St Germain des Pres", "Abbaye St Germain des Pres. Le choeur"),
    # And a head the reader cannot read is worth nothing held back.
    (None, "Άνατολικὴ ἄποψις τῶν Προπυλαίων - Άθῆναι"),
]


# What the translator will not translate, and what we say instead.
GLOSSARY_CASES = [
    ("Alt-Graz. Admontergäßchen against the Paradeishof",
     "Old Graz. Admontergäßchen against the Paradeishof"),
    ("Alt Graz: Town Hall at the corner of Schmiedgasse",
     "Old Graz: Town Hall at the corner of Schmiedgasse"),
    ("ALT-GRAZ. Burgwehr and French from the town hall",
     "Old Graz. Burgwehr and French from the town hall"),
    ("Gruss from Altenburg", "Greetings from Altenburg"),
    # A compound is not the word: Schlossberg keeps its Schloss, and
    # Altenburg and Altona keep their Alt.
    ("View to Schloss Eggenberg and Schlossberg from the west",
     "View to Schloss Eggenberg and Schlossberg from the west"),
    ("Altona town hall", "Altona town hall"),
]


def selftest():
    bad = 0
    print("where the caption splits")
    for want, caption in SPLIT_CASES:
        got, _, rest = split_head(caption)
        ok = got == want
        bad += 0 if ok else 1
        how = f"holds back {got!r}" if got else "translates the whole line"
        print(f"  {'ok  ' if ok else 'FAIL'} {how:34s} {caption[:44]}")

    print("what the translator would not translate")
    for before, want in GLOSSARY_CASES:
        got = apply_glossary(before)
        ok = got == want
        bad += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} {got[:64]}")
    return bad


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--budget", type=int, default=BUDGET)
    ap.add_argument("--upcoming", type=int, default=45, metavar="DAYS",
                    help="translate what is due in the next N days first")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--selftest", action="store_true",
                    help="check the head-split against captions we looked at")
    args = ap.parse_args()

    if args.selftest:
        failed = selftest()
        print(f"\n{failed} failed" if failed else "\nall good")
        return 1 if failed else 0

    # daily.load_pool() rather than json.load(pool.json): regions are
    # attached when daily.py loads the pool, so raw entries have no
    # "rg" and every region cell of the schedule selects nothing --
    # which left upcoming_captions walking a fraction of the schedule and
    # calling it the whole of it.
    sys.path.insert(0, HERE)
    import daily
    entries = daily.load_pool()
    cache = load_cache()

    if args.report:
        langs = collections.Counter(v.get("lang") for v in cache.values())
        print(f"{len(cache)} captions looked at")
        for field, label in (("t", "titles"), ("pl", "place lines")):
            have = [e for e in entries if e.get(field)]
            done = sum(1 for e in have if e[field] in cache)
            print(f"  {label:12s} {done:6d} of {len(have):6d} cards covered")
        for code, n in langs.most_common(12):
            print(f"  {code}  {n}")
        return 0

    import argostranslate.package as package
    import argostranslate.translate as translate

    package.update_package_index()
    available = package.get_available_packages()
    installed = installed_pairs()

    # What is due soon goes first, then everything else.
    queue = upcoming_captions(entries, args.upcoming) if args.upcoming else []
    rest = [text for e in entries for text in texts_of(e)]
    titles = [t for t in dict.fromkeys(queue + rest) if t not in cache]
    if queue:
        due = len([t for t in dict.fromkeys(queue) if t not in cache])
        sys.stderr.write(f"{due} of them are on a screen within "
                         f"{args.upcoming} days\n")
    # Where a source speaks one language, its word beats a detector's.
    hints = {}
    for entry in entries:
        code = SOURCE_LANG.get(entry.get("src"))
        if code:
            for text in texts_of(entry):
                hints.setdefault(text, code)
    sys.stderr.write(f"{len(titles)} untranslated captions, budget "
                     f"{args.budget}\n")

    done = skipped = 0
    started = time.monotonic()
    for title in titles:
        if done >= args.budget:
            break
        code = hints.get(title) or detect(title)
        if code is None or code in SKIP_LANGS:
            cache[title] = {"lang": code or "??", "en": None}
            skipped += 1
            continue
        if not ensure_pack(code, available, installed):
            cache[title] = {"lang": code, "en": None}
            skipped += 1
            continue
        head, sep, rest = split_head(title)
        try:
            english = translate.translate(rest, code, "en").strip()
        except Exception:
            continue
        if head:
            english = head + sep + english
        english = apply_glossary(english)
        # A translation identical to the original is a proper name that
        # came through untouched, which is the right answer and not worth
        # a second copy.
        cache[title] = {"lang": code,
                        "en": english if english and english != title else None}
        done += 1
        if done % 100 == 0:
            save_cache(cache)
            rate = done / max(time.monotonic() - started, 1)
            sys.stderr.write(f"    {done}/{min(args.budget, len(titles))} "
                             f"({rate:.1f}/s)\n")
            sys.stderr.flush()
    save_cache(cache)
    print(f"translated {done}, skipped {skipped}, cache now {len(cache)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())


# What a translation must never do, at the strings that taught each rule.
USABLE_CASES = [
    # a one-word caption is a name, and a name is not vocabulary
    (False, "Antietam", "Antimony"),
    (False, "Atlanta", "Home"),
    (False, "Maine", "Repute"),
    (False, "Graubunden", "Grey bandages"),
    (False, "Alt-Graz", "Old Great"),
    # including the ones it got right, which is the trade being made
    (False, "Venezia", "Venice"),
    (False, "Glockenturm", "Bell Tower"),
    # nothing foul the source did not say
    (False, "Selcuk", "Fuck."),
    (False, "Bereg Baikala", "Fuck that time."),
    (False, "Tigani ciurari", "Tiger niggers"),
    # but a real place name survives, because the source says it too
    (True, "Bitche (Lorraine), Le camp", "Bitche (Lorraine), The camp"),
    (True, "Camp de Bitche (Lorraine", "Bitche Camp (Lorraine)"),
    # and ordinary captions are untouched
    (True, "Alt Graz", "Old Graz"),
    (True, "Beleuchteter Uhrturm", "Illuminated Clock Tower"),
    (True, "Arnhem, Rijnbrug", "Arnhem, Rhine Bridge"),
    # nothing to publish is not publishable
    (False, "Graz", ""),
]


def usable_failures():
    """Empty when every guard case holds."""
    return ["usable({!r}, {!r}) = {}, want {}".format(s, e, usable(s, e), w)
            for w, s, e in USABLE_CASES if usable(s, e) is not w]
