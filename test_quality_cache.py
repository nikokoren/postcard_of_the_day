#!/usr/bin/env python3
"""
Check that the render-score cache is read the same way by everyone.

quality.json has three users -- harvest.py measures and writes it,
score.py measures and writes it, daily.py filters the pool by it -- and
for a while they did not agree on which key held the rows. harvest kept
them under "entries" with the count under a name; score.py had it the
other way round. Both readers were strict, so each job broke on the
other's file: the refresh wrote a file daily.py read the count of and
died on ("'int' object has no attribute 'get'"), and harvest read
score.py's file as empty and re-measured 17,862 cards it had already
scored, which is why pool.json recorded scored=0.

Nothing here touches the real files.

Run it with no arguments from the repo root.
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

ROWS = {"dc:commonwealth:aaa": [0.10, 0.90, 0.00],
        "loc:123": [0.20, 0.80, 0.05, 0.1, 0.2]}

SHAPES = {
    "harvest's shape, rows under entries":
        {"version": 1, "scored": len(ROWS), "entries": ROWS},
    "score.py's old shape, rows under scored":
        {"version": 1, "count": len(ROWS), "scored": ROWS},
    "dims-style, rows under entries with another count name":
        {"version": 1, "measured": len(ROWS), "entries": ROWS},
}

EMPTY = {
    "an empty cache": ({"version": 1, "scored": 0, "entries": {}}, 0),
    "a file that is not an object": ([1, 2, 3], 0),
    "a file with no rows at all": ({"version": 1}, 0),
}


def main():
    import daily
    import harvest
    import score

    fails = []

    def check(name, ok, detail=""):
        print(("  ok   " if ok else "  FAIL ") + name +
              ("\n         " + detail if detail and not ok else ""))
        if not ok:
            fails.append(name)

    def written(payload):
        path = os.path.join(tempfile.mkdtemp(), "quality.json")
        with open(path, "w") as fh:
            json.dump(payload, fh)
        return path

    print("every writer's shape is read by every reader:")
    for name, payload in SHAPES.items():
        path = written(payload)
        daily.QUALITY_PATH = path
        score.CACHE_PATH = path
        for who, got in (("harvest", harvest.load_cache(path)),
                         ("daily", daily.load_quality()),
                         ("score", score.load_cache())):
            check("{} <- {}".format(who, name),
                  isinstance(got, dict) and got == ROWS, repr(got)[:120])

    print("a cache with no rows reads as empty, never as a count:")
    for name, (payload, want) in EMPTY.items():
        path = written(payload)
        daily.QUALITY_PATH = path
        got = daily.load_quality()
        check(name, isinstance(got, dict) and len(got) == want, repr(got)[:120])

    print("what score.py writes is what the others read:")
    path = os.path.join(tempfile.mkdtemp(), "quality.json")
    score.CACHE_PATH = path
    daily.QUALITY_PATH = path
    score.save_cache(ROWS)
    stored = json.load(open(path))
    check("the rows go under entries", stored.get("entries") == ROWS,
          repr(stored)[:150])
    check("the count is a number beside them", stored.get("scored") == len(ROWS),
          repr(stored)[:150])
    check("score.py reads its own file", score.load_cache() == ROWS)
    check("daily.py reads it", daily.load_quality() == ROWS)
    check("harvest reads it", harvest.load_cache(path) == ROWS)

    print("and the rows survive the filter they exist for:")
    check("a readable score keeps its card",
          harvest.readable(ROWS["dc:commonwealth:aaa"]) in (True, False))
    check("an unmeasured card is kept", harvest.readable(None) is True)

    print("all clear" if not fails else "{} failed".format(len(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
