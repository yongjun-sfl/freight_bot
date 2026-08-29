"""Run real driver phrasings through the live parser and score the output."""
import json, sys, time
sys.path.insert(0, "/src/bot")
import ai_engine
from seed_network import read_distance_csv

# Prime the location cache exactly as production will after seeding, so the
# prompt carries the real code list rather than the dynamic-extraction fallback.
codes, _ = read_distance_csv()
ai_engine.LOCATION_CACHE["codes"] = codes
ai_engine.LOCATION_CACHE["alias_map"] = {c: c for c in codes} | {"200": "200F"}

CASES = [
    # --- CASE 1: departures ---
    ("200 to e2f loaded",            dict(case="CASE_1_ORIGIN_DEPARTURE", o="200", d="E2F", ls="LOADED")),
    ("live loading finished load 200 to E2F", dict(case="CASE_1_ORIGIN_DEPARTURE", o="200", d="E2F")),
    ("heading to 200 bobtail",       dict(case="CASE_1_ORIGIN_DEPARTURE", d="200", ls="BOBTAIL")),
    ("mt trailer back to 200",       dict(case="CASE_1_ORIGIN_DEPARTURE", d="200", ls="EMPTY")),
    ("leaving 200 to E2F, no doubt running late", dict(case="CASE_1_ORIGIN_DEPARTURE", o="200", d="E2F")),
    ("sds to 7634 loaded",           dict(case="CASE_1_ORIGIN_DEPARTURE", o="SDS", d="7634", ls="LOADED")),
    ("e1 to 210",                    dict(case="CASE_1_ORIGIN_DEPARTURE", o="E1", d="210")),
    ("3551 to e2r",                  dict(case="CASE_1_ORIGIN_DEPARTURE", o="3551", d="E2R")),
    # --- CASE 2: arrivals ---
    ("arrived e2f door 45",          dict(case="CASE_2_DESTINATION_ARRIVAL", d="E2F", door="45")),
    ("at 200 in yard",               dict(case="CASE_2_DESTINATION_ARRIVAL", d="200")),
    # --- CASE 3: within-facility moves (the new case) ---
    ("empty move #13 to #47",        dict(case="CASE_3_INTRA_FACILITY_MOVE", od="13", dd="47", ls="EMPTY")),
    ("loaded move #4 to #13",        dict(case="CASE_3_INTRA_FACILITY_MOVE", od="4", dd="13", ls="LOADED")),
    ("empty dropped yard",           dict(case="CASE_3_INTRA_FACILITY_MOVE", dd="YARD", ls="EMPTY")),
    ("moved 44821 door 7 to yard",   dict(case="CASE_3_INTRA_FACILITY_MOVE", od="7", dd="YARD")),
    # --- chatter ---
    ("anyone want lunch",            dict(case="NONE_WORK_RELATED")),
    ("ok thanks",                    dict(case="NONE_WORK_RELATED")),
]

FIELDS = {"o": "origin_location", "d": "destination_location", "ls": "load_status",
          "door": "door_number", "od": "origin_dock", "dd": "destination_dock"}

ok = tot = 0
misses = []
for text, want in CASES:
    t0 = time.time()
    got = ai_engine.parse_text_with_llm(text)
    dt = time.time() - t0
    line, bad = [], []

    tot += 1
    if got.get("case_type") == want["case"]:
        ok += 1
    else:
        bad.append(f"case_type={got.get('case_type')} want {want['case']}")

    for key, field in FIELDS.items():
        if key not in want:
            continue
        tot += 1
        actual = (got.get(field) or "")
        if str(actual).upper() == want[key].upper():
            ok += 1
        else:
            bad.append(f"{field}={actual!r} want {want[key]!r}")

    mark = "OK  " if not bad else "MISS"
    print(f"  {mark} {dt:4.1f}s  {text!r}")
    for b in bad:
        print(f"          {b}")
    if bad:
        misses.append((text, bad))

print(f"\n  model: {ai_engine.MODEL_NAME}")
print(f"  fields correct: {ok}/{tot}  ({100*ok/tot:.0f}%)")
print(f"  messages with any miss: {len(misses)}/{len(CASES)}")
