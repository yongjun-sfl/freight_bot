"""One-time dev tool: splitexport the ``match case_type:`` block out of the
old ``state_machine.py`` into standalone handler functions in ``leg_cases.py``.

Behavior-preserving mechanical extract:
  - each `case "X":` arm -> ``async def handle_*(ctx)``
  - bodies copied verbatim, dedented by 16 spaces
  - shared message-scope variables rebound from ``ctx`` at the top
  - shared helper closures become ``ctx.<name>`` calls
  - reassignments of state ctx methods read later write back to ctx

Usage: python3 tools/gen_cases.py <old state_machine.py>
"""

import os
import re
import sys

BINDINGS = """\
    cur = ctx.cur
    conn = ctx.conn
    p = ctx.p
    did = ctx.did
    user_name = ctx.user_name
    group_title = ctx.group_title
    orig_chat_id = ctx.orig_chat_id
    orig_msg_id = ctx.orig_msg_id
    msg_timestamp = ctx.msg_timestamp
    intent = ctx.intent
    raw_text = ctx.raw_text
    raw_lower = ctx.raw_lower
    text_trailer = ctx.text_trailer
    ocr_trailer = ctx.ocr_trailer
    bol_number = ctx.bol_number
    document_type = ctx.document_type
    action_type = ctx.action_type
    door_num = ctx.door_num
    do_num = ctx.do_num
    origin_dock = ctx.origin_dock
    destination_dock = ctx.destination_dock
    primary_image_blob = ctx.primary_image_blob
    shipper_signed = ctx.shipper_signed
    receiver_signed = ctx.receiver_signed
    origin_loc = ctx.origin_loc
    dest_loc = ctx.dest_loc
    raw_orig = ctx.raw_orig
    raw_dest = ctx.raw_dest
    load_status_val = ctx.load_status_val
    is_bobtail_flag = ctx.is_bobtail_flag
    is_load_pickup = ctx.is_load_pickup
"""

CLOSURE_NAMES = [
    "find_duplicate_bol_leg", "resolve_round", "current_round_number",
    "stamp_finished", "open_departure_leg", "start_hooked_load",
    "pickup_with_no_destination",
]

SYNC_ASSIGN = {
    "bol_number": "None",
    "document_type": '"UNKNOWN"',
    "primary_image_blob": "None",
    "shipper_signed": "False",
}

HANDLER_NAMES = {
    "CASE_LUNCH_START": "handle_lunch_start",
    "CASE_LUNCH_END": "handle_lunch_end",
    "CASE_CLOCK_IN": "handle_clock_in",
    "CASE_CLOCK_OUT": "handle_clock_out",
    "CASE_WORK_FINISHED": "handle_work_finished",
    "CASE_1_ORIGIN_DEPARTURE": "handle_case_1_departure",
    "CASE_2_DESTINATION_ARRIVAL": "handle_case_2_arrival",
    "CASE_HISTORICAL_BOL_UPDATE": "handle_bol_update",
    "CASE_AUTO_RESOLVE": "handle_auto_resolve",
    "CASE_3_INTRA_FACILITY_MOVE": "handle_case_3_intra_move",
    "CASE_MANIFEST": "handle_manifest",
    "PARSE_FAILED": "handle_parse_failed",
}


def transform_body(lines):
    seen = []
    for line in lines:
        if not line.strip():
            seen.append("")
        elif line.startswith(" " * 16):
            seen.append(line[16:])
        else:
            seen.append(line.lstrip())
    text = "\n".join(seen)
    for name in CLOSURE_NAMES:
        text = re.sub(rf"(?<![\w.]){re.escape(name)}(?=\s*\()", f"ctx.{name}", text)
    for var, value in SYNC_ASSIGN.items():
        pat = re.compile(rf"^(\s*){re.escape(var)}(\s*=\s*){re.escape(value)}(\s*)$")
        text = pat.sub(lambda m: f"{m.group(1)}{var} = ctx.{var} = {value}", text)
    return text


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: python3 tools/gen_cases.py <old state_machine.py>")
    with open(sys.argv[1], encoding="utf-8") as fh:
        lines = fh.read().splitlines()

    start = next((i for i, ln in enumerate(lines)
                  if ln.startswith("            match case_type:")), None)
    if start is None:
        sys.exit("match case_type: not found")

    records = []
    i, n = start + 1, len(lines)
    while i < n:
        m = re.match(r"^                case (.+):\s*$", lines[i])
        if not m:
            i += 1
            continue
        header = m.group(1).strip()
        label = header.strip('"') if header.startswith('"') else "DEFAULT"
        i += 1
        body = []
        while i < n and not re.match(r"^                case ", lines[i]):
            body.append(lines[i])
            i += 1
        records.append((label, transform_body(body)))

    if not records:
        sys.exit("no case arms found")

    header = (
        '"""One handler per trip case type.\n'
        "\n"
        "Each ``handle_*`` function replaces one ``case ``X``:`` arm of the\n"
        "old ``commit_trip_leg`` match block. Bodies are moved verbatim; the\n"
        "only changes are the message-scoped locals (now read off ``ctx``,\n"
        "the ``LegContext``) and the shared helpers (now ``ctx.<method>``\n"
        "calls).\n"
        '"""\n'
        "\n"
        "import logging\n"
        "\n"
        "from config import TABLE_SHUTTLE_LEGS\n"
        "from ai_engine import (\n"
        "    LOCATION_CACHE,\n"
        "    facility_for_dock,\n"
        "    normalize_location,\n"
        "    site_of,\n"
        ")\n"
        "from manifests import store_manifest\n"
        "from rm_manifest import close_rm_load\n"
        "from shifts import (\n"
        "    format_worked,\n"
        "    record_clock_in,\n"
        "    record_clock_out,\n"
        "    record_lunch,\n"
        ")\n"
        "\n"
        "from leg_helpers import REPEAT_MOVE_MINUTES\n"
        "\n"
        "logger = logging.getLogger(__name__)\n"
    )

    blocks = []
    for label, body in records:
        fname = HANDLER_NAMES.get(label, "handle_default")
        blocks.append(f"async def {fname}(ctx):\n{BINDINGS}\n\n{body}")
    merged = "\n\n\n".join(blocks) + "\n"

    here = os.path.dirname(os.path.abspath(__file__))
    out_path = os.path.abspath(os.path.join(here, "..", "bot", "leg_cases.py"))
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(header)
        fh.write("\n\n")
        fh.write(merged)
    print(f"wrote {out_path} with {len(records)} handlers")


if __name__ == "__main__":
    main()