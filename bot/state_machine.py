"""Trip-leg state machine: public entry point.

``commit_trip_leg`` orchestrates one driver message end to end:
  - build a ``LegContext`` from the parsed intent,
  - resolve load status,
  - verify the driver is on the roster,
  - apply the case-reclassification rules that precede the match
    (a pair of known facilities reads as a departure, origin inference,
    same-site departure becomes a within-facility move, a yard move with a
    cross-facility "to X" becomes a departure),
  - record any lunch boundary bundled into the same message,
  - then dispatch to a per-case handler.

Split from a ~1700-line monolith. The pieces live in three modules:
  * ``leg_helpers.py`` -- regexes and pure text matching,
  * ``leg_context.py``  -- parsed-message state + shared DB helpers,
  * ``leg_cases.py``    -- one ``async handle_*(ctx)`` per case type.
"""

import logging

from config import TABLE_DRIVERS, TABLE_SHUTTLE_LEGS, TABLE_UNKNOWN_SENDERS
from ai_engine import normalize_location, site_of
from shifts import record_lunch

from leg_helpers import (
    BOBTAIL_PATTERN,
    LOAD_PATTERN,
    PICKUP_PATTERN,
    UNLOAD_PATTERN,
    cross_facility_destination,
    facility_dock_from_text,
    two_facilities_in_order,
)
from leg_context import LegContext
from leg_cases import (
    handle_auto_resolve,
    handle_bol_update,
    handle_case_1_departure,
    handle_case_2_arrival,
    handle_case_3_intra_move,
    handle_clock_in,
    handle_clock_out,
    handle_default,
    handle_lunch_end,
    handle_lunch_start,
    handle_manifest,
    handle_parse_failed,
    handle_work_finished,
)

logger = logging.getLogger(__name__)

CASE_HANDLERS = {
    "CASE_LUNCH_START": handle_lunch_start,
    "CASE_LUNCH_END": handle_lunch_end,
    "CASE_CLOCK_IN": handle_clock_in,
    "CASE_CLOCK_OUT": handle_clock_out,
    "CASE_WORK_FINISHED": handle_work_finished,
    "CASE_1_ORIGIN_DEPARTURE": handle_case_1_departure,
    "CASE_2_DESTINATION_ARRIVAL": handle_case_2_arrival,
    "CASE_HISTORICAL_BOL_UPDATE": handle_bol_update,
    "CASE_AUTO_RESOLVE": handle_auto_resolve,
    "CASE_3_INTRA_FACILITY_MOVE": handle_case_3_intra_move,
    "CASE_MANIFEST": handle_manifest,
    "PARSE_FAILED": handle_parse_failed,
}


async def commit_trip_leg(
    p,
    did: int,
    user_name: str,
    group_title: str,
    orig_chat_id: int,
    orig_msg_id: int,
    msg_timestamp: str,
    intent: dict
) -> dict:
    ctx = LegContext(p, did, user_name, group_title,
                     orig_chat_id, orig_msg_id, msg_timestamp, intent)

    # The parser reads a wall number glued to a facility as equipment ("pick up
    # load 200 #47 to e2 f" -> trailer "47", origin dropped entirely -- Joseph
    # Kim 08:35 on 08/28). A "FACILITY #NN" is the facility and a dock: recover
    # the origin, turn the NN into the door, and never let it ride as a trailer.
    ctx.origin_loc, ctx.door_num, ctx.text_trailer = facility_dock_from_text(
        ctx.raw_text, ctx.origin_loc, ctx.door_num, ctx.text_trailer)

    # Detect Bobtail Flag
    ctx.is_bobtail_flag = 1 if BOBTAIL_PATTERN.search(ctx.raw_text) else 0

    # The driver has just handed the load over, so whatever they do next they
    # do with an empty trailer. Two independent signals because neither is
    # always present: a receiver-stamped POD is first-hand evidence off the
    # paperwork, and work_finished covers the report that arrives with no
    # photo. Hooking the next load in the same breath cancels it.
    work_finished = bool(ctx.intent.get("work_finished"))
    unload_reported = bool(UNLOAD_PATTERN.search(ctx.raw_text))
    just_delivered = bool(
        (ctx.receiver_signed or (work_finished and unload_reported))
        and not PICKUP_PATTERN.search(ctx.raw_text)
    )
    # The mirror: a finished live LOAD sends the trailer out full.
    just_loaded = bool(work_finished and not unload_reported
                       and LOAD_PATTERN.search(ctx.raw_text))

    # Extended Fallback Logic for Load Status
    parsed_load_status = ctx.intent.get("load_status")
    if ctx.is_bobtail_flag or parsed_load_status == "BOBTAIL":
        ctx.load_status_val = "EMPTY"
        ctx.is_bobtail_flag = 1
    elif parsed_load_status and parsed_load_status not in ["UNKNOWN", "NULL", "NONE"]:
        ctx.load_status_val = parsed_load_status
    elif just_delivered:
        ctx.load_status_val = "EMPTY"
    elif just_loaded:
        ctx.load_status_val = "LOADED"
    elif "load pickup" in ctx.raw_lower or "pick up" in ctx.raw_lower:
        ctx.load_status_val = "LOADED"
    elif "empty" in ctx.raw_lower:
        ctx.load_status_val = "EMPTY"
    else:
        async with p.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"""SELECT load_status
                          FROM {TABLE_SHUTTLE_LEGS}
                         WHERE user_id = %s
                           AND load_status IS NOT NULL
                      ORDER BY id DESC
                         LIMIT 1;""",
                    (did,)
                )
                last_status_row = await cur.fetchone()
                ctx.load_status_val = last_status_row[0] if (
                    last_status_row and last_status_row[0]) else "LOADED"

    # Hooking a load is the start of a trip, never a yard shuffle and never
    # just an arrival. Which case the parser filed the message under does not
    # change that, so the test lives here rather than inside one branch.
    ctx.is_load_pickup = bool(ctx.load_status_val == "LOADED"
                              and PICKUP_PATTERN.search(ctx.raw_text))

    async with p.acquire() as conn:
        async with conn.cursor() as cur:
            ctx.conn = conn
            ctx.cur = cur

            # 0. DRIVER VERIFICATION
            await cur.execute(
                f"""SELECT user_id
                      FROM {TABLE_DRIVERS}
                     WHERE user_id = %s
                     LIMIT 1;""",
                (did,)
            )
            driver_exists = await cur.fetchone()
            if not driver_exists:
                # Remember them rather than only logging. driver_profiles keys
                # on the Telegram user_id, which is not recorded anywhere else,
                # so a driver missing from the roster is silently ignored.
                # /roster turns this into the list needed to register them.
                await cur.execute(
                    f"""INSERT INTO {TABLE_UNKNOWN_SENDERS}
                            (user_id, display_name, message_count)
                        VALUES (%s, %s, 1)
                        ON DUPLICATE KEY UPDATE
                            display_name = VALUES(display_name),
                            last_seen = CURRENT_TIMESTAMP,
                            message_count = message_count + 1;""",
                    (did, user_name),
                )
                await conn.commit()
                logger.warning(
                    f"Message from unregistered Telegram user #{did} ({user_name}); "
                    f"recorded for /roster."
                )
                return {"is_clean": False, "leg_id": None, "card_text": None}

            # 0b. A PAIR OF FACILITIES IS A DEPARTURE EVEN WITHOUT THE WORD "TO"
            # "Unloading finished / Empty 200 sds" is an EMPTY 200 -> sds trip.
            if (ctx.case_type in ("CASE_WORK_FINISHED", "NONE_WORK_RELATED")
                    and ctx.raw_text):
                pair_origin, pair_dest = two_facilities_in_order(ctx.raw_text)
                if pair_origin and pair_dest:
                    logger.info(
                        f"Driver #{did} filed as {ctx.case_type} but names "
                        f"{pair_origin} -> {pair_dest}; recording as a departure."
                    )
                    ctx.case_type = "CASE_1_ORIGIN_DEPARTURE"
                    ctx.origin_loc = pair_origin
                    ctx.dest_loc = pair_dest

            # 0c. ORIGIN INFERENCE FOR DEPARTURES -- the driver is wherever
            # their last trip ended.
            if (ctx.case_type == "CASE_1_ORIGIN_DEPARTURE"
                    and (not ctx.origin_loc or ctx.origin_loc in
                         ("UNKNOWN", "NONE", "NULL", "MISSING_ORIGIN"))):
                await cur.execute(
                    f"""SELECT destination_location
                          FROM {TABLE_SHUTTLE_LEGS}
                         WHERE user_id = %s
                           AND destination_location NOT IN
                               ('UNKNOWN', 'MISSING_ORIGIN', 'MISSING_DEST', '')
                      ORDER BY id DESC
                         LIMIT 1;""",
                    (did,)
                )
                inferred = await cur.fetchone()
                if inferred and inferred[0]:
                    ctx.origin_loc = normalize_location(inferred[0])
                    logger.info(
                        f"Driver #{did} departure names no origin; using last "
                        f"destination {ctx.origin_loc}."
                    )

            # 1. A SAME-SITE DEPARTURE IS A WITHIN-FACILITY MOVE
            # "200F to 200R" is front to rear in one yard, not a trip.
            if (ctx.case_type == "CASE_1_ORIGIN_DEPARTURE"
                    and ctx.origin_loc != "UNKNOWN" and ctx.dest_loc != "UNKNOWN"
                    and site_of(ctx.origin_loc) == site_of(ctx.dest_loc)):
                logger.info(
                    f"Driver #{did} reported {ctx.origin_loc} to {ctx.dest_loc}: "
                    f"one site, recording as a within-facility move."
                )
                ctx.case_type = "CASE_3_INTRA_FACILITY_MOVE"

            # 1b. THE REVERSE: A YARD MOVE WITH A CROSS-FACILITY "TO X" IS A
            # TRIP. "Empty drop e1 yard #3, Bobtail to SDs" is a drop at E1
            # AND a bobtail to SDS -- two movements in one message.
            if ctx.case_type == "CASE_3_INTRA_FACILITY_MOVE":
                far_dest = cross_facility_destination(ctx.raw_text)
                if far_dest and ctx.origin_loc != "UNKNOWN" \
                        and site_of(far_dest) != site_of(ctx.origin_loc):
                    logger.info(
                        f"Driver #{did} filed as a yard move but says "
                        f"'{far_dest}': recording as a departure."
                    )
                    ctx.case_type = "CASE_1_ORIGIN_DEPARTURE"
                    if (not ctx.dest_loc or ctx.dest_loc in
                            ["UNKNOWN", "NONE", "NULL", "MISSING_DEST"]
                            or site_of(ctx.dest_loc) == site_of(ctx.origin_loc)):
                        ctx.dest_loc = far_dest

            # Lunch is recorded regardless of the case: drivers routinely
            # report lunch in the same breath as a departure or a yard move,
            # and the work must not be lost to the lunch or the other way.
            lunch_boundary = ctx.intent.get("lunch")
            if lunch_boundary in ("START", "END"):
                await record_lunch(cur, did, msg_timestamp, lunch_boundary)
                await conn.commit()

            handler = CASE_HANDLERS.get(ctx.case_type, handle_default)
            return await handler(ctx)

