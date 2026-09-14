#!/usr/bin/env python3
"""
Score the cards for whether they are worth looking at on a panel.

harvest.py can do this while it crawls, but it only runs monthly and it
has a crawl to get through first, so at 900 cards a run the pool would
have been fully scored some time in 2046. Meanwhile every unscored card
is eligible to be somebody's postcard of the day. That is how "Milano.
Castello Sforzesco. Sala delle Asse" -- two thirds of a photograph and
one third of a column of Italian history, set in six-point type -- went
out on a 2-bit screen where none of it could be read.

So the measuring lives here too, on its own, and the daily job runs it:
cheap once the pool is covered, and self-healing when the pool grows.

The order is what makes it useful on day one. The schedule is
arithmetic, so the cards that will be on a screen this fortnight are
knowable rather than guessable, and those are measured first. The rest
of the pool follows, a budget at a time, cached forever in quality.json.

Applied when daily.py loads the pool, not when harvest.py writes it, so
a card that scores badly is out of tomorrow's picks without waiting for
a re-crawl.

    python3 score.py                  # a budget of cards
    python3 score.py --budget 4000
    python3 score.py --upcoming 30
    python3 score.py --report
"""

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
POOL_PATH = os.path.join(HERE, "pool.json")
CACHE_PATH = os.path.join(HERE, "quality.json")

BUDGET = 1500
UPCOMING_DAYS = 21
WORKERS = 4
SAVE_EVERY = 250


def load_pool():
    with open(POOL_PATH) as fh:
        return json.load(fh).get("entries") or []


def load_cache():
    try:
        with open(CACHE_PATH) as fh:
            return json.load(fh).get("scored") or {}
    except (OSError, ValueError):
        return {}


def save_cache(cache):
    tmp = CACHE_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"version": 1, "count": len(cache), "scored": cache},
                  fh, ensure_ascii=False, separators=(",", ":"),
                  sort_keys=True)
        fh.write("\n")
    os.replace(tmp, CACHE_PATH)


def upcoming_ids(entries, days):
    """
    The cards that will actually be on a screen in the next `days`,
    soonest first.

    For each cell the order within a cycle is fixed, so the order is
    computed once and walked, rather than asking for each day
    separately. Straight out of translate.py, which needs the same list
    for the same reason.
    """
    sys.path.insert(0, HERE)
    import daily
    from datetime import date

    cells = daily.build_cells(entries, daily.regions_in(entries))
    seen, ordered = set(), []
    for key in cells:
        subset = daily.cards_for(entries, key)
        if not subset:
            continue
        total = len(subset)
        cycle, position = divmod(daily.day_index(date.today()), total)
        run = daily.order_for(subset, key, cycle)
        for step in range(min(days, total)):
            index = position + step
            entry = (run[index] if index < total
                     else daily.order_for(subset, key, cycle + 1)[index - total])
            if entry["id"] not in seen:
                seen.add(entry["id"])
                ordered.append((step, entry["id"]))
    ordered.sort(key=lambda row: row[0])
    return [card_id for _, card_id in ordered]


def to_measure(entries, cache, days, budget):
    """Unscored cards, the ones on their way to a screen first."""
    unscored = {e["id"]: e for e in entries if e["id"] not in cache}
    queue = []
    for card_id in upcoming_ids(entries, days):
        entry = unscored.pop(card_id, None)
        if entry:
            queue.append(entry)
    soon = len(queue)
    queue.extend(unscored[card_id] for card_id in sorted(unscored))
    return queue[:budget], min(soon, budget)


def report(entries, cache):
    import harvest
    scored = [(e, cache.get(e["id"])) for e in entries]
    have = [(e, s) for e, s in scored if s]
    rejected = [(e, s) for e, s in have if not harvest.readable(s)]
    print(f"{len(entries)} cards, {len(have)} scored "
          f"({100.0 * len(have) / max(len(entries), 1):.1f}%), "
          f"{len(rejected)} of those rejected")
    if not rejected:
        return
    print("\nrejected:")
    for entry, s in sorted(rejected, key=lambda r: -(r[1][3] or 0))[:40]:
        rhythm = s[3] if len(s) > 3 and s[3] is not None else 0.0
        flat = s[4] if len(s) > 4 and s[4] is not None else 0.0
        if s[1] <= harvest.UNREADABLE_DETAIL:
            why = "flat scan"
        elif s[2] >= harvest.TEXT_RATIO:
            why = "wrong side"
        elif rhythm >= harvest.PANEL_RULED and flat >= harvest.PANEL_FLAT:
            why = "ruled panel"
        else:
            why = "page of print"
        print(f"  {why:14s} rhythm {rhythm:.2f} flat {flat:.2f}  "
              f"{(entry.get('t') or '')[:54]}")


# Ten cards this filter rejects and nine it keeps, all real
# measurements, all looked at. The pair of thresholds is a judgement
# call about where a picture stops being a picture, so if anybody moves
# them this table says what they just let through -- or threw away.
#
# The awkward ones are the last three kept: a Kyoto woodblock and a
# Bruges view are flat-toned enough to look like paper, and a YWCA
# cafeteria rules up almost hard enough to look like type. Each fails
# the other half of the test, which is the whole reason there are two.
SELFTEST = [
    (False, "Gone West -- illustration, and a poem beside it", 0.693, 0.49),
    (False, "Wilhelmina, with the poem set beside her",        0.644, 0.62),
    (False, "Russia and Japanese peace envoys, written across", 0.557, 0.59),
    (False, "La Brabanconne -- the anthem, printed in full",   0.517, 0.73),
    (False, "Wilhelmina again",                                0.510, 0.76),
    (False, "Addison PA -- a board of toll rates",             0.488, 0.57),
    (False, "Milano. Castello Sforzesco. Sala delle Asse",     0.485, 0.66),
    (False, "Gaynor Rowlands -- a step wedge in the frame",    0.454, 0.60),
    (False, "Milano. Castello Sforzesco. Pusterla dei Fabbri", 0.409, 0.69),
    (False, "The New Colossus -- nothing but the sonnet",      0.339, 0.76),
    (True,  "Philadelphia gateway -- brickwork, not type",     0.641, 0.31),
    (True,  "Pistoia. Battistero -- masonry courses",          0.535, 0.46),
    (True,  "Portret van Juliana",                             0.407, 0.47),
    (True,  "Zuiderzee dyke, running to the horizon",          0.402, 0.45),
    (True,  "Amsterdam. Frederiksplein",                       0.402, 0.05),
    (True,  "Cafeteria, Y.W.C.A -- shelves of tins",           0.378, 0.58),
    (True,  "Kyoto -- a woodblock, flat-toned sky",            0.359, 0.65),
    (True,  "The mills by the Kruispoort, Bruges",             0.310, 0.67),
    (True,  "12in mortar, Fort H. G. Wright",                  0.309, 0.65),
]


def selftest():
    import harvest
    bad = 0
    print("the panel rule")
    for want, name, rhythm, flat in SELFTEST:
        got = harvest.readable([40.0, 45.0, 1.2, rhythm, flat])
        ok = got is want
        bad += 0 if ok else 1
        verb = "keeps" if got else "drops"
        print(f"  {'ok  ' if ok else 'FAIL'} {verb} {name} "
              f"(rhythm {rhythm}, flat {flat})")

    print("the older rules, and the cards nobody has measured")
    for want, name, score in (
            (False, "a flat, empty scan",   [40.0, 9.0, 1.2, 0.05, 0.30]),
            (False, "the address side",     [40.0, 45.0, 3.4, 0.05, 0.30]),
            (True,  "not measured yet",     None),
            (True,  "measured without numpy", [40.0, 45.0, 1.2, None, None]),
            (True,  "an older three-part score", [40.0, 45.0, 1.2]),
    ):
        got = harvest.readable(score)
        ok = got is want
        bad += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} "
              f"{'keeps' if got else 'drops'} {name}")
    return bad


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--budget", type=int, default=BUDGET,
                    help="cards to fetch and score this run")
    ap.add_argument("--upcoming", type=int, default=UPCOMING_DAYS,
                    metavar="DAYS", help="score the next DAYS of picks first")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--selftest", action="store_true",
                    help="check the thresholds against cards we looked at")
    args = ap.parse_args()

    if args.selftest:
        failed = selftest()
        print(f"\n{failed} failed" if failed else "\nall good")
        return 1 if failed else 0

    if not os.path.exists(POOL_PATH):
        print("no pool.json yet -- run harvest.py first")
        return 0

    import harvest
    entries = load_pool()
    cache = load_cache()

    if args.report:
        report(entries, cache)
        return 0

    queue, soon = to_measure(entries, cache, args.upcoming, args.budget)
    if not queue:
        print(f"all {len(entries)} cards scored")
        return 0

    print(f"{len(entries)} cards, {len(cache)} already scored")
    print(f"scoring {len(queue)} ({soon} of them due on a screen "
          f"within {args.upcoming} days)")

    started = time.time()
    done = rejected = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for entry, result in zip(queue, pool.map(harvest.measure, queue)):
            cache[entry["id"]] = list(result) if result else None
            done += 1
            if result and not harvest.readable(list(result)):
                rejected += 1
                print(f"  drop  {(entry.get('t') or '')[:58]}")
            if done % SAVE_EVERY == 0:
                save_cache(cache)
                print(f"    {done}/{len(queue)}  ({time.time() - started:.0f}s)")
    save_cache(cache)
    print(f"scored {done} in {time.time() - started:.0f}s, "
          f"{rejected} rejected, {len(cache)} in the cache")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
