"""Live check of the new message shapes."""
import sys, time
sys.path.insert(0, "/src/bot")
import ai_engine
from seed_network import read_locations_csv
rows = [r for r in read_locations_csv() if r["active"]]
ai_engine.LOCATION_CACHE["codes"] = [r["code"] for r in rows]

CASES = [
    ("clock in",                              "CASE_CLOCK_IN",  False),
    ("clocked in",                            "CASE_CLOCK_IN",  False),
    ("clock out",                             "CASE_CLOCK_OUT", False),
    ("clocking out for the day",              "CASE_CLOCK_OUT", False),
    ("live unloading finished",               "CASE_WORK_FINISHED", True),
    ("unload done",                           "CASE_WORK_FINISHED", True),
    ("live loading finished load 200 to E2F", "CASE_1_ORIGIN_DEPARTURE", True),
    ("200 to e2f loaded",                     "CASE_1_ORIGIN_DEPARTURE", False),
    ("arrived e2f door 45",                   "CASE_2_DESTINATION_ARRIVAL", False),
    ("empty move #13 to #47",                 "CASE_3_INTRA_FACILITY_MOVE", False),
    ("anyone want lunch",                     "NONE_WORK_RELATED", False),
]
ok = 0
for text, want_case, want_finished in CASES:
    out = ai_engine.parse_text_with_llm(text)
    got_case = out.get("case_type")
    got_fin = bool(out.get("work_finished"))
    good = got_case == want_case and got_fin == want_finished
    ok += good
    mark = "ok  " if good else "MISS"
    extra = "" if good else f"   got {got_case} finished={got_fin}"
    print(f"  {mark} {text!r:42} -> {want_case}, finished={want_finished}{extra}")
print(f"\n  {ok}/{len(CASES)} correct")
