"""
Render selection.liquid against a stand-in feed and a stand-in settings
blob, and check that every case resolves to a pick.

The Liquid is the one part of this that cannot be reasoned about
reliably. Three bugs in the map recipe's equivalent were found by
running it and by nothing else: a filter inside a bracket index is a
syntax error, `split` leaves a trailing empty string that indexes into
nothing, and a feed key collides with a settings keyname of the same
name. So it gets run.

    pip install python-liquid && python3 trmnl/test_selection.py
"""
import os, sys
from liquid import Environment

env = Environment()
HERE = os.path.dirname(os.path.abspath(__file__))
src = open(os.path.join(HERE, "selection.liquid")).read()
try:
    tmpl = env.from_string(src)
except Exception as e:
    print("PARSE FAIL:", e); sys.exit(1)
print("parse ok")

DAY = 20706
TS = 1789031899          # the timestamp from a real device dump

_cells = {
  "all__all__all":              ["IMG-all","Everything","1907","","",""],
  "landscape__all__all":        ["IMG-l","Landscape","1907","","",""],
  "portrait__all__all":         ["IMG-p","Portrait","1907","","",""],
  "all__europe__all":           ["IMG-eu","Europe","1907","","",""],
  "all__africa__all":           ["IMG-af","Africa","1907","","",""],
  "portrait__europe__all":      ["IMG-eup","Europe portrait","1907","","",""],
  "all__all__era-1900-1914":    ["IMG-e1","1900-1914","1907","","",""],
  "landscape__europe__era-1900-1914": ["IMG-x","EU landscape 1900s","1907","","",""],
}

# One file, three days. Yesterday and tomorrow carry marker titles so a
# test can tell which day the markup actually landed on.
picks = {
  str(DAY - 1): {k: ["IMG-y", "YESTERDAY " + v[1], "1900", "", "", ""] for k, v in _cells.items()},
  str(DAY):     {k: v for k, v in _cells.items()},
  str(DAY + 1): {k: ["IMG-t", "TOMORROW " + v[1], "1900", "", "", ""] for k, v in _cells.items()},
}
feed = {
  "default":"all__all__all",
  "day_index": 20705,
  "cell_keys": ",".join(sorted(_cells)),
  "keys_by_label": {
     "landscape":"landscape","Landscape":"landscape",
     "portrait":"portrait","Portrait":"portrait",
     "africa":"africa","Africa":"africa",
     "1900_-_1914":"era-1900-1914","1900 - 1914":"era-1900-1914",
     "before_1900":"era-pre-1900",
     "europe":"europe","Europe":"europe",
  },
  "days": picks,
  "default_day": str(DAY),
  "pick_fields": ["image","title","date","place","publisher","credit"],
}

# Local time decides the day, so these are the cases that matter, and
# each needs a clock that actually straddles a midnight somewhere.
#
#   EVENING = 20:00 UTC on day 20706. Auckland (+12) is already 08:00 on
#             the 20707; Berlin and Los Angeles are still on the 20706.
#   EARLY   = 02:00 UTC on day 20706. Los Angeles (-7) is still 19:00 on
#             the 20705; Berlin and Auckland are on the 20706.
#
# A case whose expected day is not "today" asserts on the marker title
# baked into that day's picks, so a markup that quietly ignored the
# device clock would fail here rather than pass silently.
EVENING = DAY * 86400 + 20 * 3600
EARLY = DAY * 86400 + 2 * 3600

OFFSETS = {
  "Auckland, evening UTC":    12 * 3600,
  "Berlin, evening UTC":       2 * 3600,
  "Los Angeles, early UTC":   -7 * 3600,
  "Berlin, early UTC":         2 * 3600,
  "device clock missing":      2 * 3600,
}
CLOCKS = {
  "Auckland, evening UTC":   EVENING,
  "Berlin, evening UTC":     EVENING,
  "Los Angeles, early UTC":  EARLY,
  "Berlin, early UTC":       EARLY,
  "device clock missing":    None,
}
EXPECT = {
  "Auckland, evening UTC":   "TOMORROW",
  "Berlin, evening UTC":     "",
  "Los Angeles, early UTC":  "YESTERDAY",
  "Berlin, early UTC":       "",
  "device clock missing":    "",
}

CASES = [
  ("Auckland, evening UTC", {}),
  ("Berlin, evening UTC", {}),
  ("Los Angeles, early UTC", {}),
  ("Berlin, early UTC", {}),
  ("device clock missing", {}),
  ("nothing selected", {}),
  ("orientation only", {"orientation":["landscape"]}),
  ("two regions", {"region":["europe","africa"]}),
  ("era only", {"era":["1900_-_1914"]}),
  ("all three, exists", {"orientation":["landscape"],"region":["europe"],"era":["1900_-_1914"]}),
  ("all three, missing -> loosen", {"orientation":["portrait"],"region":["africa"],"era":["before_1900"]}),
  ("string not array", {"orientation":"portrait"}),
  ("unknown value", {"region":["atlantis"]}),
  ("caption off", {"show_caption":"false"}),
  ("credit on", {"show_credit":"true"}),
  ("one region", {"region":["europe"]}),
  ("both orientations", {"orientation":["landscape","portrait"]}),
  ("region + orientation", {"region":["europe"],"orientation":["portrait"]}),
]
probe = src + "\n<<{{ chosen_key }}|{{ card_title }}|{{ card_image }}|c{{ want_caption }}|p{{ want_place }}|r{{ want_credit }}>>"
t2 = env.from_string(probe)
bad = 0
for name, settings in CASES:
    ctx = dict(feed)
    offset = OFFSETS.get(name, 7200)
    ctx["trmnl"] = {"plugin_settings": {"custom_fields_values": settings},
                    "device": {"width": 800, "height": 480},
                    "system": {"timestamp_utc": CLOCKS.get(name, TS)},
                    "user": {"utc_offset": offset}}
    out = t2.render(**ctx)
    tail = out[out.rfind("<<")+2:out.rfind(">>")]
    ok = "|" in tail and not tail.startswith("|")
    want = EXPECT.get(name)
    if ok and want is not None:
        title = tail.split("|")[1]
        ok = title.startswith(want) if want else not (
            title.startswith("YESTERDAY") or title.startswith("TOMORROW"))
    bad += 0 if ok else 1
    print(f"  {'ok  ' if ok else 'FAIL'} {name:32s} {tail}")
sys.exit(1 if bad else 0)
