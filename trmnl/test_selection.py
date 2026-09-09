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

picks = {
  "all__all__all":              {"title":"Everything","image":"IMG-all","place":"","date":"1907"},
  "landscape__all__all":        {"title":"Landscape","image":"IMG-l","place":"","date":"1907"},
  "portrait__all__all":         {"title":"Portrait","image":"IMG-p","place":"","date":"1907"},
  "all__europe__all":           {"title":"Europe","image":"IMG-eu","place":"","date":"1907"},
  "all__africa__all":           {"title":"Africa","image":"IMG-af","place":"","date":"1907"},
  "portrait__europe__all":      {"title":"Europe portrait","image":"IMG-eup","place":"","date":"1907"},
  "all__all__era-1900-1914":    {"title":"1900-1914","image":"IMG-e1","place":"","date":"1907"},
  "landscape__europe__era-1900-1914": {"title":"EU landscape 1900s","image":"IMG-x","place":"","date":"1907"},
}
feed = {
  "default":"all__all__all",
  "day_index": 20705,
  "cell_keys": ",".join(sorted(picks)),
  "keys_by_label": {
     "landscape":"landscape","Landscape":"landscape",
     "portrait":"portrait","Portrait":"portrait",
     "africa":"africa","Africa":"africa",
     "1900_-_1914":"era-1900-1914","1900 - 1914":"era-1900-1914",
     "before_1900":"era-pre-1900",
     "europe":"europe","Europe":"europe",
  },
  "picks": picks,
}

CASES = [
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
probe = src + "\n<<{{ chosen_key }}|{{ pick.title }}|{{ card_image }}|c{{ want_caption }}|p{{ want_place }}|r{{ want_credit }}>>"
t2 = env.from_string(probe)
bad = 0
for name, settings in CASES:
    ctx = dict(feed)
    ctx["trmnl"] = {"plugin_settings":{"custom_fields_values":settings},
                    "device":{"width":800,"height":480}}
    out = t2.render(**ctx)
    tail = out[out.rfind("<<")+2:out.rfind(">>")]
    ok = "|" in tail and not tail.startswith("|")
    bad += 0 if ok else 1
    print(f"  {'ok  ' if ok else 'FAIL'} {name:32s} {tail}")
sys.exit(1 if bad else 0)
