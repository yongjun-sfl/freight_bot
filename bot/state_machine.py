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
from ai_engine import LOCATION_CACHE, normalize_location, site_of
from shifts import record_lunch

from leg_helpers import (
    BOBTAIL_PATTERN,
    DROP_PATTERN,
    ENGLISH_MOVEMENT_PATTERN,
    KOREAN_PLAN_PATTERN,
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

    # The LLM occasionally invents a code that is not a location at all
    # (drivers sign captions with their name: "... to e2 r Kong" -> KONG).
    # Recover the explicit "to X" destination from the raw text before the
    # movement is recorded under a phantom location.
    known_locations = set(LOCATION_CACHE.get("alias_map") or {})
    unknown_tokens = ("UNKNOWN", "NONE", "NULL", "MISSING_ORIGIN", "MISSING_DEST")
    if known_locations and (ctx.dest_loc and ctx.dest_loc not in unknown_tokens
            and ctx.dest_loc not in known_locations):
        recovered_dest = cross_facility_destination(ctx.raw_text or "")
        if recovered_dest:
            logger.info(
                f"Driver #{did} parser returned invalid destination "
                f"{ctx.dest_loc!r}; raw text says {recovered_dest}."
            )
            ctx.dest_loc = recovered_dest
    if known_locations and (ctx.origin_loc and ctx.origin_loc not in unknown_tokens
            and ctx.origin_loc not in known_locations):
        logger.info(
            f"Driver #{did} parser returned invalid origin {ctx.origin_loc!r}; "
            f"clearing for origin inference."
        )
        ctx.origin_loc = "UNKNOWN"

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
    if parsed_load_status is not None:
        parsed_load_status = str(parsed_load_status).strip().upper()
        if parsed_load_status in ("", "UNKNOWN", "NULL", "NONE"):
            parsed_load_status = None
        elif parsed_load_status not in ("EMPTY", "LOADED", "BOBTAIL"):
            # The parser occasionally returns a word ("DROP", "PICKUP") or a
            # phrase instead of the enum. shuttle_legs.load_status is
            # ENUM('EMPTY','LOADED'), so letting it through aborts the INSERT
            # with "Data truncated" and loses the whole movement. Fall through
            # to the text/previous-leg inference instead.
            logger.warning(
                f"Driver #{did} parser returned invalid load_status "
                f"{parsed_load_status!r}; inferring from text."
            )
            parsed_load_status = None
    if ctx.is_bobtail_flag or parsed_load_status == "BOBTAIL":
        ctx.load_status_val = "EMPTY"
        ctx.is_bobtail_flag = 1
    elif parsed_load_status in ("EMPTY", "LOADED"):
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

            # 0a. THIRD-PERSON DIRECTION GUARD. Drivers sometimes relay what
            # ANOTHER driver is supposed to do ("공영택 사장 께서도 엠티 E1 yard
            # 에 ... 픽업 합니다"). That is a direction to someone else, not the
            # sender's movement. When another registered driver is named and the
            # sender is not, treat it as no-op instead of inventing sender legs.
            raw_upper = " ".join((ctx.raw_text or "").upper().split())

            def name_in_text(name):
                if not name:
                    return False
                normalized = " ".join(str(name).upper().split())
                return bool(normalized) and normalized in raw_upper

            async def driver_names(uid):
                await cur.execute(
                    f"SELECT name_eng, name_kor, short_name "
                    f"  FROM {TABLE_DRIVERS} WHERE user_id = %s;",
                    (uid,)
                )
                return await cur.fetchone() or (None, None, None)

            own_names = await driver_names(did)
            await cur.execute(
                f"SELECT name_eng, name_kor, short_name "
                f"  FROM {TABLE_DRIVERS} WHERE user_id <> %s;",
                (did,)
            )
            other_rows = await cur.fetchall()
            other_mentioned = any(
                name_in_text(name)
                for row in other_rows for name in row
            )
            self_mentioned = any(name_in_text(name) for name in own_names)
            if other_mentioned and not self_mentioned:
                logger.info(
                    f"Driver #{did} message names another driver and not "
                    f"themselves; treating as a direction/no-op."
                )
                return {"is_clean": True, "leg_id": None, "card_text": None}

            # 0a-bis. FORWARD-LOOKING PLAN GUARD. Korean text that plans the
            # next move ("엠티 E1에 드랍하고 ... 합니다", "... 하기로 했습니다")
            # is not a movement that has happened; recording it creates a
            # phantom leg and steals the arrival time from the real report that
            # follows. Only text-only messages with a Korean future/sequence
            # marker and no English movement cue are ignored; photos and
            # English captions still pass through.
            if (not ctx.primary_image_blob
                    and KOREAN_PLAN_PATTERN.search(ctx.raw_text or "")
                    and not ENGLISH_MOVEMENT_PATTERN.search(ctx.raw_text or "")):
                logger.info(
                    f"Driver #{did} message is a Korean plan/intention, not a "
                    f"movement; treating as no-op."
                )
                return {"is_clean": True, "leg_id": None, "card_text": None}

            # 0b. A PAIR OF FACILITIES IS A DEPARTURE EVEN WITHOUT THE WORD "TO"
            # "Unloading finished / Empty 200 sds" is an EMPTY 200 -> sds trip.
            # A load pickup the parser filed as an arrival ("Load pick up D 020
            # sds to 200") is likewise a departure; only a hook that names no
            # destination should reach the CASE 2 no-destination card.
            # CASE 2 "arrival" messages that also contain an onward movement
            # ("Empty drop e1 yard, Bobtail to SDs") are an arrival AND the next
            # departure -- promote them so the departure is not lost; CASE 1
            # completes the open arrival leg before opening the new leg.
            recoverable = ctx.case_type in (
                "CASE_WORK_FINISHED", "NONE_WORK_RELATED",
                "CASE_2_DESTINATION_ARRIVAL", "CASE_3_INTRA_FACILITY_MOVE")
            if recoverable and ctx.raw_text:
                pair_origin, pair_dest = two_facilities_in_order(ctx.raw_text)

                # The regex needs a bare alias in the code list ("e2 to sds"
                # does not resolve because E2 is a site, not a location code),
                # but the LLM sometimes already returns the canonical pair.
                # When it did, that is a real departure -- use it as the
                # fallback so a finish-unload + move is not silently dropped.
                if (not pair_origin
                        and ctx.case_type == "CASE_WORK_FINISHED"):
                    llm_origin = ctx.origin_loc
                    llm_dest = ctx.dest_loc
                    if (llm_origin and llm_dest
                            and llm_origin not in (
                                "UNKNOWN", "NONE", "NULL", "MISSING_ORIGIN")
                            and llm_dest not in (
                                "UNKNOWN", "NONE", "NULL", "MISSING_DEST")
                            and site_of(llm_origin) != site_of(llm_dest)):
                        pair_origin, pair_dest = llm_origin, llm_dest
                    elif (UNLOAD_PATTERN.search(ctx.raw_text or "")
                          and llm_origin
                          and llm_origin not in (
                              "UNKNOWN", "NONE", "NULL", "MISSING_ORIGIN")
                          and llm_origin in (LOCATION_CACHE.get("alias_map") or {})):
                        # "Live unloading finished eg2 to e 1" arrives as
                        # CASE_WORK_FINISHED with no destination at all. The
                        # "to <facility>" phrase is the empty reposition's
                        # destination; keep the LLM origin and let the
                        # work-finished origin correction fix front/rear.
                        recovered_dest = cross_facility_destination(
                            ctx.raw_text or "")
                        if recovered_dest:
                            pair_origin, pair_dest = llm_origin, recovered_dest

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

            # 1c. THE REVERSE ARRIVAL: A DROP AT A THIRD FACILITY IS AN ARRIVAL
            # WHEN A CROSS-FACILITY TRIP IS OPEN. "drop empty E1 yard" parses as
            # an E1->E1 yard move, but if the driver left 200 for SDS and is
            # dropping the empty at E1, the drop is the trip's arrival. A drop
            # back at the ORIGIN yard is still a yard move (test:
            # a_drop_somewhere_else_is_not_the_arrival).
            if ctx.case_type == "CASE_3_INTRA_FACILITY_MOVE" \
                    and DROP_PATTERN.search(ctx.raw_text or ""):
                drop_at = normalize_location(ctx.origin_loc or ctx.dest_loc)
                if drop_at not in (
                        "UNKNOWN", "NONE", "NULL", "MISSING_ORIGIN", "MISSING_DEST"):
                    await cur.execute(
                        f"""SELECT id, origin_location, destination_location
                              FROM {TABLE_SHUTTLE_LEGS}
                             WHERE user_id = %s
                               AND leg_status IN
                                   ('IN_TRANSIT', 'ARRIVED', 'UNLOADING', 'LOADING')
                          ORDER BY id DESC
                             LIMIT 1;""",
                        (did,)
                    )
                    open_leg = await cur.fetchone()
                    if open_leg and open_leg[1] and open_leg[2]:
                        booked_origin = normalize_location(open_leg[1])
                        booked_dest = normalize_location(open_leg[2])
                        if (site_of(booked_dest) != site_of(drop_at)
                                and site_of(booked_origin) != site_of(drop_at)):
                            logger.info(
                                f"Driver #{did} filed a drop at {drop_at} as a yard "
                                f"move, but Leg #{open_leg[0]} is still open from "
                                f"{booked_origin} to {booked_dest}; recording the arrival."
                            )
                            ctx.case_type = "CASE_2_DESTINATION_ARRIVAL"
                            ctx.dest_loc = drop_at

            # 1d. A FINISHED-UNLOAD "to <facility>" MISFILED AS A YARD MOVE.
            # "finished live unloading Empty to E1 yard" never names where the
            # unload happened; the last delivery leg is that site, so use it as
            # the origin instead of recording an E1->E1 yard move.
            if (ctx.case_type == "CASE_3_INTRA_FACILITY_MOVE"
                    and ctx.intent.get("work_finished")
                    and UNLOAD_PATTERN.search(ctx.raw_text or "")):
                to_loc = cross_facility_destination(ctx.raw_text or "")
                if to_loc:
                    await cur.execute(
                        f"""SELECT destination_location
                              FROM {TABLE_SHUTTLE_LEGS}
                             WHERE user_id = %s
                               AND is_positioning_leg = 0
                               AND destination_location NOT IN
                                   ('UNKNOWN', 'NONE', 'NULL', 'MISSING_DEST', '')
                          ORDER BY id DESC
                             LIMIT 1;""",
                        (did,)
                    )
                    last_row = await cur.fetchone()
                    if last_row and last_row[0]:
                        last_dest = normalize_location(last_row[0])
                        if site_of(last_dest) != site_of(to_loc):
                            logger.info(
                                f"Driver #{did} finished unloading at {last_dest} "
                                f"and says empty to {to_loc}; recording the departure."
                            )
                            ctx.case_type = "CASE_1_ORIGIN_DEPARTURE"
                            ctx.origin_loc = last_dest
                            ctx.dest_loc = to_loc

            # Lunch is recorded regardless of the case: drivers routinely
            # report lunch in the same breath as a departure or a yard move,
            # and the work must not be lost to the lunch or the other way.
            lunch_boundary = ctx.intent.get("lunch")
            if lunch_boundary in ("START", "END"):
                await record_lunch(cur, did, msg_timestamp, lunch_boundary)
                await conn.commit()

            handler = CASE_HANDLERS.get(ctx.case_type, handle_default)
            return await handler(ctx)

