import sys
sys.path.insert(0, "/src/bot")
import ai_engine
from seed_network import read_locations_csv
ai_engine.LOCATION_CACHE["codes"] = [r["code"] for r in read_locations_csv() if r["active"]]
CASES = [
    "7634 unloading finished Empty to 200R",
    "jung kim finished live unloading empty 7634 to 200",
    "200R 닥에 4대 야드에 8대 (지금 드랍하신분 포함) 200F에는 한분 언로드중",
]
for text in CASES:
    o = ai_engine.parse_text_with_llm(text)
    print(f"  {text!r}")
    print(f"      case      = {o.get('case_type')}")
    print(f"      finished  = {o.get('work_finished')}")
    print(f"      loc       = {o.get('origin_location')!r} -> {o.get('destination_location')!r}")
    print(f"      load      = {o.get('load_status')!r}   trailer={o.get('trailer_number')!r}")
