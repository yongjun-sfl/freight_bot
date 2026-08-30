import logging
import re
from config import TABLE_DRIVERS, TABLE_SHUTTLE_LEGS, TABLE_UNKNOWN_SENDERS
from ai_engine import (LOCATION_CACHE, facility_for_dock,
                       normalize_location, site_of)
from load_types import classify as classify_load
from manifests import read_manifest, store_manifest
from rm_manifest import close_rm_load, finish_rm_load, record_rm_load
from shifts import (format_worked, record_clock_in, record_clock_out,
                    record_lunch)
from routes import SPOT_ROUTE_CODE, is_anchor, route_code_for, serves

logger = logging.getLogger(__name__)

# A within-facility move re-reported inside this window is treated as the same
# event rather than a second drop. Drivers routinely send the paperwork and a
# photo of the parked trailer minutes apart, both describing one drop.
REPEAT_MOVE_MINUTES = 30

# Word-boundary matched: a bare `"bt" in text` substring test fires on ordinary
# words like "doubt", "debt" and "subtotal", which forced load_status to EMPTY
# and silently skipped the outbound BOL compliance guard.
BOBTAIL_PATTERN = re.compile(
    r"\b(?:bobtail|bob\s*tail|bt|b/t|no\s+trailer|single\s+tractor|tractor\s+only)\b",
    re.IGNORECASE,
)

# Hooking a load starts a trip; it is never a within-yard reposition. When the
# driver names no destination there is nothing to record, so the message would
# otherwise vanish -- "Load trailer pickup sds dock28 #77209" left the whole
# SDS -> 200F run missing from the day.
PICKUP_PATTERN = re.compile(
    r"\b(?:pick\s*-?\s*up|pickup|picking\s+up|hook(?:ed|ing)?(?:\s+up)?)\b",
    re.IGNORECASE,
)


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
    case_type = intent.get("case_type", "NONE_WORK_RELATED")
    def _not_a_facility(value):
        """Drop a trailer number that is really a facility code.

        Bare numeric sites like 7634 read as trailers, and a facility stored
        as a trailer both loses the location and corrupts trailer history.
        """
        if not value:
            return value
        clean = str(value).strip().upper()
        known = set(LOCATION_CACHE.get("codes") or [])
        known |= set(LOCATION_CACHE.get("alias_map") or {})
        if clean in known or normalize_location(clean) in known:
            logger.info(f"Ignoring facility code {clean!r} given as a trailer number.")
            return None
        return value

    text_trailer = _not_a_facility(
        intent.get("trailer_number") or intent.get("text_trailer"))
    ocr_trailer = _not_a_facility(intent.get("ocr_trailer"))
    bol_number = intent.get("bol_number")
    document_type = intent.get("document_type", "UNKNOWN")
    action_type = intent.get("action")
    door_num = intent.get("door_number")
    do_num = intent.get("do_number")
    origin_dock = intent.get("origin_dock")
    destination_dock = intent.get("destination_dock")
    primary_image_blob = intent.get("primary_image_blob")
    shipper_signed = intent.get("shipper_signed", False)
    receiver_signed = intent.get("receiver_signed", False)
    raw_text = intent.get("raw_text") or ""

    raw_orig = intent.get("origin_location")
    raw_dest = intent.get("destination_location")
    origin_loc = normalize_location(raw_orig) if raw_orig else "UNKNOWN"
    dest_loc = normalize_location(raw_dest) if raw_dest else "UNKNOWN"

    parsed_load_status = intent.get("load_status")
    raw_lower = raw_text.lower()

    # Detect Bobtail Flag
    is_bobtail_flag = 1 if BOBTAIL_PATTERN.search(raw_text) else 0

    # Extended Fallback Logic for Load Status
    if is_bobtail_flag or parsed_load_status == "BOBTAIL":
        load_status_val = "EMPTY"
        is_bobtail_flag = 1
    elif parsed_load_status and parsed_load_status not in ["UNKNOWN", "NULL", "NONE"]:
        load_status_val = parsed_load_status
    elif action_type in ["UNLOAD_COMPLETED", "FINISHED_UNLOAD"]:
        load_status_val = "EMPTY"
    elif action_type in ["LIVE_LOAD", "HOOK"]:
        load_status_val = "LOADED"
    elif "load pickup" in raw_lower or "pick up" in raw_lower:
        load_status_val = "LOADED"
    elif "empty" in raw_lower:
        load_status_val = "EMPTY"
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
                load_status_val = last_status_row[0] if (last_status_row and last_status_row[0]) else "LOADED"

    async with p.acquire() as conn:
        async with conn.cursor() as cur:

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
                # Remember them rather than only logging. driver_profiles keys on
                # the Telegram user_id, which is not recorded anywhere else, so a
                # driver missing from the roster is silently ignored forever.
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

            # 1. A SAME-SITE DEPARTURE IS A WITHIN-FACILITY MOVE
            # "200F to 200R" is front to rear in one yard, not a trip. Compared
            # by site, so E2F to E2R counts too. Recorded as the move it is
            # rather than carded for the dispatcher to correct by hand.
            if (case_type == "CASE_1_ORIGIN_DEPARTURE"
                    and origin_loc != "UNKNOWN" and dest_loc != "UNKNOWN"
                    and site_of(origin_loc) == site_of(dest_loc)):
                logger.info(
                    f"Driver #{did} reported {origin_loc} to {dest_loc}: one site, "
                    f"recording as a within-facility move."
                )
                case_type = "CASE_3_INTRA_FACILITY_MOVE"

            # 2. DUPLICATE BOL LOOKUP
            # Deliberately NOT run ahead of the match block. The patch paths --
            # CASE_HISTORICAL_BOL_UPDATE, CASE_AUTO_RESOLVE, and the CASE 1
            # auto-heal -- all locate their target *by* an existing bol_number,
            # so a pre-match guard made every one of them unreachable. It now
            # runs only where a brand new leg would claim a BOL.
            async def find_duplicate_bol_leg():
                """Id of an existing leg already carrying this BOL, else None."""
                if not bol_number:
                    return None
                await cur.execute(
                    f"""SELECT id 
                          FROM {TABLE_SHUTTLE_LEGS} 
                         WHERE bol_number = %s 
                         LIMIT 1;""",
                    (bol_number,)
                )
                existing_bol = await cur.fetchone()
                return existing_bol[0] if existing_bol else None

            async def resolve_round(origin: str, destination: str):
                """(round_number, route_code) for a new departure.

                A round is one traversal of a defined route: leave an anchor,
                work the stops that belong to a route anchored there, come back.
                A leg to anywhere else is a spot delivery and gets its own round.

                This is deliberately not distance-based. 100 and 1380 form their
                own rounds because they sit on no route, not because they are far
                -- Cartersville is in fact nearer to 200 than Dalton is.
                """
                await cur.execute(
                    f"""SELECT round_number, route_code, destination_location 
                          FROM {TABLE_SHUTTLE_LEGS} 
                         WHERE user_id = %s 
                           AND is_positioning_leg = 0 
                           AND round_number IS NOT NULL 
                      ORDER BY id DESC 
                         LIMIT 1;""",
                    (did,)
                )
                row = await cur.fetchone()

                async def open_new_round():
                    await cur.execute(
                        f"""SELECT COALESCE(MAX(round_number), 0) 
                              FROM {TABLE_SHUTTLE_LEGS} 
                             WHERE user_id = %s 
                               AND DATE(departure_time) = CURRENT_DATE();""",
                        (did,)
                    )
                    (highest_today,) = await cur.fetchone()
                    return (highest_today or 0) + 1

                if not row:
                    number = await open_new_round()
                    if serves(origin, destination):
                        return number, route_code_for(origin, [destination])
                    return number, SPOT_ROUTE_CODE

                current, current_route, last_destination = row

                # Where the open round started, and everywhere it has been.
                await cur.execute(
                    f"""SELECT origin_location, destination_location 
                          FROM {TABLE_SHUTTLE_LEGS} 
                         WHERE user_id = %s 
                           AND round_number = %s 
                           AND is_positioning_leg = 0 
                      ORDER BY id ASC;""",
                    (did, current)
                )
                legs = await cur.fetchall()
                anchor = legs[0][0] if legs else None
                visited = [leg[1] for leg in legs]

                # Has the open round finished? A normal round ends back at its
                # anchor; a spot delivery ends wherever it rejoins a route.
                if current_route == SPOT_ROUTE_CODE:
                    if not is_anchor(last_destination):
                        # Still out on the spot run, including the leg home.
                        # Its origin is not a route anchor, so route membership
                        # cannot be consulted here.
                        return current, SPOT_ROUTE_CODE
                elif anchor and site_of(last_destination) != site_of(anchor):
                    if serves(anchor, destination):
                        return current, route_code_for(anchor, visited + [destination])
                    # Off-route: close this round, start a spot delivery.
                    return await open_new_round(), SPOT_ROUTE_CODE

                number = await open_new_round()
                if serves(origin, destination):
                    return number, route_code_for(origin, [destination])
                return number, SPOT_ROUTE_CODE

            async def next_trip_seq() -> int:
                """Sequential leg number for this driver today.

                Mirrors the Trip Seq column in the dispatcher's sheet, which
                exists so rounds can be worked out from the leg order.
                """
                await cur.execute(
                    f"""SELECT COALESCE(MAX(trip_seq), 0) 
                          FROM {TABLE_SHUTTLE_LEGS} 
                         WHERE user_id = %s 
                           AND DATE(departure_time) = CURRENT_DATE();""",
                    (did,)
                )
                (highest,) = await cur.fetchone()
                return (highest or 0) + 1

            async def current_round_number():
                """The round a within-facility move happened during."""
                await cur.execute(
                    f"""SELECT round_number 
                          FROM {TABLE_SHUTTLE_LEGS} 
                         WHERE user_id = %s 
                           AND round_number IS NOT NULL 
                      ORDER BY id DESC 
                         LIMIT 1;""",
                    (did,)
                )
                row = await cur.fetchone()
                return row[0] if row else None

            async def stamp_finished(target_leg=None):
                """Record that a live load or unload completed.

                Applies to the leg the driver is ending, which on a merged
                message ("live loading finished load 200 to E2F") is the leg
                BEFORE the departure being announced.
                """
                if target_leg is None:
                    await cur.execute(
                        f"""SELECT id FROM {TABLE_SHUTTLE_LEGS} 
                             WHERE user_id = %s AND is_positioning_leg = 0 
                          ORDER BY id DESC LIMIT 1;""",
                        (did,)
                    )
                    row = await cur.fetchone()
                    target_leg = row[0] if row else None
                if not target_leg:
                    return None
                await cur.execute(
                    f"""UPDATE {TABLE_SHUTTLE_LEGS} 
                           SET finished_time = COALESCE(finished_time, %s) 
                         WHERE id = %s;""",
                    (msg_timestamp, target_leg)
                )
                await finish_rm_load(cur, target_leg, msg_timestamp)
                return target_leg

            def pickup_with_no_destination(where, position, trailer):
                """Card for a load hooked with nowhere recorded to take it.

                The trip is real and the dispatcher can work out where it went
                from the arrival that follows, but only if they are told the
                message happened. Nothing is written: a leg to UNKNOWN would
                enter a round and a route as though it were a real destination.
                """
                at = f"`{where}`"
                if position:
                    at += f" (dock `{position}`)"
                return (
                    f"\u26a0\ufe0f **MANUAL RECONCILE: Load Picked Up With No Destination**\n"
                    f"\U0001f464 Driver: {user_name}\n"
                    f"\U0001f4ac Message: `{raw_text}`\n"
                    f"\U0001f69b Trailer: `{trailer or 'UNKNOWN'}`\n"
                    f"\U0001f4cd Origin: {at}\n"
                    f"\u2753 Issue: Load picked up with an origin but no destination "
                    f"mentioned, so the departure was NOT recorded as a leg."
                )

            lunch_boundary = intent.get("lunch")
            if lunch_boundary in ("START", "END"):
                # Recorded regardless of the case: drivers routinely report
                # lunch in the same breath as a departure or a yard move, and
                # the work must not be lost to the lunch or the other way round.
                await record_lunch(cur, did, msg_timestamp, lunch_boundary)
                await conn.commit()

            match case_type:

                # =========================================================
                # LUNCH (reported on its own)
                # =========================================================
                case "CASE_LUNCH_START":
                    return {
                        "is_clean": True, "leg_id": None, "card_text": None,
                        "reply_text": "🍽 Lunch started",
                    }

                case "CASE_LUNCH_END":
                    _, _, minutes = await record_lunch(cur, did, msg_timestamp, "END")
                    await conn.commit()
                    taken = f" · {minutes} min" if minutes else ""
                    return {
                        "is_clean": True, "leg_id": None, "card_text": None,
                        "reply_text": f"🍽 Lunch ended{taken}",
                    }

                # =========================================================
                # SHIFT BOUNDARIES
                # =========================================================
                case "CASE_CLOCK_IN":
                    reported, expected = await record_clock_in(cur, did, msg_timestamp)
                    await conn.commit()
                    logger.info(f"🕐 Driver #{did} clocked in at {reported}.")
                    late = ""
                    if expected and reported and reported > expected:
                        minutes = int((reported - expected).total_seconds() // 60)
                        late = f" ({minutes} min after expected)"
                    return {
                        "is_clean": True,
                        "leg_id": None,
                        "card_text": None,
                        "reply_text": f"✅ Clocked in — {reported:%H:%M}{late}",
                    }

                case "CASE_CLOCK_OUT":
                    started, ended, worked = await record_clock_out(cur, did, msg_timestamp)
                    await conn.commit()
                    logger.info(f"🕐 Driver #{did} clocked out at {ended}.")
                    if not started:
                        return {
                            "is_clean": False,
                            "leg_id": None,
                            "reply_text": f"✅ Clocked out — {ended:%H:%M}",
                            "card_text": (
                                f"⚠️ **MANUAL RECONCILE: Clock-out With No Clock-in**\n"
                                f"\U0001f464 Driver: {user_name}\n"
                                f"\U0001f550 Clocked out at `{ended:%H:%M}` but never "
                                f"reported starting, so hours cannot be worked out."
                            )
                        }
                    return {
                        "is_clean": True,
                        "leg_id": None,
                        "card_text": None,
                        "reply_text": (
                            f"✅ Clocked out — {ended:%H:%M} · {format_worked(worked)}"
                        ),
                    }

                # =========================================================
                # LIVE LOAD / UNLOAD COMPLETE
                # =========================================================
                case "CASE_WORK_FINISHED":
                    finished_leg = await stamp_finished()
                    await conn.commit()
                    if not finished_leg:
                        return {"is_clean": True, "leg_id": None, "card_text": None}
                    logger.info(f"🏁 Driver #{did} finished work on Leg #{finished_leg}.")
                    return {"is_clean": True, "leg_id": finished_leg, "card_text": None}

                # =========================================================
                # CASE 1: INTER-FACILITY DEPARTURE
                # =========================================================
                case "CASE_1_ORIGIN_DEPARTURE":
                    display_trailer = text_trailer if text_trailer and text_trailer != "UNKNOWN" else ocr_trailer

                    # 1. AUTO-HEALING: Check if active departure exists today missing a BOL/image
                    await cur.execute(
                        f"""SELECT id 
                              FROM {TABLE_SHUTTLE_LEGS} 
                             WHERE user_id = %s 
                               AND leg_status = 'IN_TRANSIT'
                               AND load_status = 'LOADED'
                               AND (bol_number IS NULL OR bol_image IS NULL)
                               AND DATE(departure_time) = CURRENT_DATE()
                          ORDER BY id DESC 
                             LIMIT 1;""",
                        (did,)
                    )
                    open_leg_missing_bol = await cur.fetchone()

                    if open_leg_missing_bol and (bol_number or primary_image_blob):
                        leg_id = open_leg_missing_bol[0]
                        await cur.execute(
                            f"""UPDATE {TABLE_SHUTTLE_LEGS} 
                                   SET bol_number = COALESCE(%s, bol_number),
                                       document_type = IF(%s != 'UNKNOWN', %s, document_type),
                                       bol_image = COALESCE(%s, bol_image),
                                       paperwork_time = COALESCE(paperwork_time, %s),
                                       trailer_number = IF(%s != 'UNKNOWN', %s, trailer_number)
                                 WHERE id = %s;""",
                            (bol_number, document_type, document_type, primary_image_blob,
                             msg_timestamp if primary_image_blob else None,
                             display_trailer, display_trailer, leg_id)
                        )
                        await record_rm_load(cur, leg_id, intent, origin_loc, dest_loc,
                                             display_trailer, msg_timestamp,
                                             driver_id=did, driver_name=user_name)
                        await conn.commit()
                        logger.info(f"⚡ CASE 1 AUTO-HEAL: Attached missing BOL '{bol_number}' to active Leg #{leg_id}")
                        return {"is_clean": True, "leg_id": leg_id, "card_text": None}

                    # A BOL already on another leg means the driver attached the
                    # PREVIOUS load's paperwork to this one. Every field derived
                    # from that document is therefore wrong and is discarded. The
                    # movement itself is real, so the leg is still recorded -- with
                    # no paperwork -- and the auto-heal above completes it once the
                    # driver reposts the correct BOL.
                    duplicate_of = await find_duplicate_bol_leg()
                    stale_bol = None
                    if duplicate_of:
                        stale_bol = bol_number
                        bol_number = None
                        document_type = "UNKNOWN"
                        primary_image_blob = None
                        shipper_signed = False
                        logger.warning(
                            f"Driver #{did} attached BOL '{stale_bol}' already recorded on "
                            f"Leg #{duplicate_of}; saving movement without paperwork."
                        )

                    # 2. ORIGIN INFERENCE & LAST LEG LOOKUP
                    await cur.execute(
                        f"""SELECT id, 
                                   trailer_number, 
                                   destination_location 
                              FROM {TABLE_SHUTTLE_LEGS} 
                             WHERE user_id = %s 
                               AND destination_location NOT IN ('UNKNOWN', 'MISSING_ORIGIN', 'MISSING_DEST', '')
                          ORDER BY id DESC 
                             LIMIT 1;""",
                        (did,)
                    )
                    last_leg = await cur.fetchone()

                    if not origin_loc or origin_loc in ["UNKNOWN", "NONE", "NULL", "MISSING_ORIGIN"]:
                        origin_loc = last_leg[2] if (last_leg and last_leg[2]) else (raw_orig.strip().upper() if raw_orig else "UNKNOWN")

                    if not dest_loc or dest_loc in ["UNKNOWN", "NONE", "NULL", "MISSING_DEST"]:
                        dest_loc = raw_dest.strip().upper() if raw_dest else "UNKNOWN"

                    if not display_trailer or display_trailer == "UNKNOWN":
                        display_trailer = last_leg[1] if (last_leg and last_leg[1]) else "UNKNOWN"

                    # A merged "live loading finished load 200 to E2F" ends the
                    # previous trip and starts the next in one message.
                    if intent.get("work_finished"):
                        await stamp_finished()

                    # Complete prior active legs upon new departure
                    await cur.execute(
                        f"""UPDATE {TABLE_SHUTTLE_LEGS} 
                               SET leg_status = 'COMPLETED', 
                                   arrival_time = COALESCE(arrival_time, %s)
                             WHERE user_id = %s 
                               AND leg_status IN ('IN_TRANSIT', 'ARRIVED', 'UNLOADING', 'LOADING');""",
                        (msg_timestamp, did)
                    )

                    round_and_route = await resolve_round(origin_loc, dest_loc)
                    rm_dock = intent.get("dock_number") or door_num

                    # Insert new departure leg
                    await cur.execute(
                        f"""INSERT INTO {TABLE_SHUTTLE_LEGS} (
                               user_id, 
                               trailer_number, 
                               bol_number, 
                               document_type,
                               origin_location, 
                               destination_location, 
                               departure_time, 
                               arrival_action, 
                               bol_image, 
                               dock_number, 
                               shipper_signed, 
                               is_bobtail, 
                               leg_status, 
                               load_status,
                               round_number,
                               route_code,
                               load_type,
                               trip_seq
                           ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'IN_TRANSIT', %s, %s, %s, %s, %s);""",
                        (
                            did, display_trailer, bol_number, document_type, origin_loc, dest_loc,
                            msg_timestamp, action_type, primary_image_blob, door_num,
                            1 if shipper_signed else 0, is_bobtail_flag, load_status_val,
                            *round_and_route,
                            classify_load(origin_loc, dest_loc, load_status_val,
                                          round_and_route[1]),
                            await next_trip_seq(),
                        )
                    )
                    await conn.commit()
                    leg_id = cur.lastrowid

                    # RM consignments carry line items the receiving departments
                    # at E2F and E2R report on, so they are kept in their own
                    # tables rather than flattened into the leg.
                    await record_rm_load(cur, leg_id, intent, origin_loc, dest_loc,
                                         display_trailer, msg_timestamp,
                                         driver_id=did, driver_name=user_name)
                    await conn.commit()

                    # ALERT GUARD 0: Stale BOL. Takes precedence over the paperwork
                    # guards below, which would otherwise fire on the fields just
                    # cleared and bury the actionable instruction.
                    if duplicate_of:
                        return {
                            "is_clean": False,
                            "leg_id": leg_id,
                            "card_text": (
                                f"⚠️ **MANUAL RECONCILE: Stale BOL Attached**\n"
                                f"👤 Driver: {user_name}\n"
                                f"🚛 Trailer: `{display_trailer}`\n"
                                f"📍 Route: `{origin_loc}` ➔ `{dest_loc}`\n"
                                f"📄 BOL `{stale_bol}` is already recorded on Leg `{duplicate_of}`.\n"
                                f"👉 Trip saved as Leg `{leg_id}` with no paperwork. "
                                f"Ask the driver to repost the correct BOL for this load."
                            )
                        }

                    # ALERT GUARD 1: Incomplete Route Data
                    if origin_loc == "UNKNOWN" or display_trailer == "UNKNOWN" or dest_loc == "UNKNOWN":
                        return {
                            "is_clean": False,
                            "leg_id": leg_id,
                            "card_text": (
                                f"⚠️ **MANUAL ATTENTION REQUIRED: Incomplete Departure**\n"
                                f"👤 Driver: {user_name}\n"
                                f"💬 Message: `{raw_text}`\n"
                                f"🚛 Trailer: `{display_trailer}`\n"
                                f"📍 Route: `{origin_loc}` ➔ `{dest_loc}`\n"
                                f"❓ Issue: Missing origin, destination, or trailer number."
                            )
                        }

                    # DEPARTURE RULE: LOADED Requires BOL # & Shipper Signed Image
                    if load_status_val == "LOADED" and not is_bobtail_flag and (not bol_number or not primary_image_blob):
                        return {
                            "is_clean": False,
                            "leg_id": leg_id,
                            "card_text": (
                                f"⚠️ **MANUAL RECONCILE: Loaded Departure Compliance Error**\n"
                                f"👤 Driver: {user_name}\n"
                                f"🚛 Trailer: `{display_trailer}`\n"
                                f"📍 Route: `{origin_loc}` ➔ `{dest_loc}`\n"
                                f"📄 BOL #: `{bol_number or 'MISSING'}` | Paper Photo: `{'ATTACHED' if primary_image_blob else 'MISSING'}`\n"
                                f"❓ Issue: Outbound LOADED trip requires a valid BOL # and shipper-signed photo."
                            )
                        }

                    # DEPARTURE RULE: EMPTY Mid-Shift POD Enforcement (FG Loads ONLY)
                    if load_status_val == "EMPTY" and not is_bobtail_flag:
                        # `id <> %s` excludes the leg inserted moments ago: without
                        # it ORDER BY id DESC always returned that new EMPTY row, so
                        # the `load_status == 'LOADED'` test below could never be true
                        # and this guard never fired for any driver.
                        await cur.execute(
                            f"""SELECT id, 
                                       load_status, 
                                       receiver_signed,
                                       document_type
                                  FROM {TABLE_SHUTTLE_LEGS} 
                                 WHERE user_id = %s 
                                   AND id <> %s
                                   AND is_positioning_leg = 0
                                   AND DATE(departure_time) = CURRENT_DATE()
                              ORDER BY id DESC 
                                 LIMIT 1;""",
                            (did, leg_id)
                        )
                        last_shift_leg = await cur.fetchone()

                        # Mid-Shift Check: Block ONLY if prior loaded trip was explicitly NOT Raw Materials ('RM')
                        if (
                            last_shift_leg 
                            and last_shift_leg[1] == "LOADED" 
                            and not last_shift_leg[2] 
                            and last_shift_leg[3] != "RM"
                        ):
                            return {
                                "is_clean": False,
                                "leg_id": leg_id,
                                "card_text": (
                                    f"⚠️ **MANUAL RECONCILE: Missing Receiver POD for Prior FG Load**\n"
                                    f"👤 Driver: {user_name}\n"
                                    f"🚛 Route: `{origin_loc}` ➔ `{dest_loc}` (EMPTY)\n"
                                    f"❓ Issue: Previous Finished Goods leg today (Leg #{last_shift_leg[0]}) is missing a receiver-signed POD."
                                )
                            }

                    return {"is_clean": True, "leg_id": leg_id, "card_text": None}

                # =========================================================
                # CASE 2: INTER-FACILITY ARRIVAL
                # =========================================================
                case "CASE_2_DESTINATION_ARRIVAL":
                    await cur.execute(
                        f"""SELECT id, 
                                   load_status, 
                                   is_bobtail,
                                   destination_location 
                              FROM {TABLE_SHUTTLE_LEGS} 
                             WHERE user_id = %s 
                               AND leg_status IN ('IN_TRANSIT', 'ARRIVED', 'UNLOADING', 'LOADING')
                          ORDER BY id DESC 
                             LIMIT 1;""",
                        (did,)
                    )
                    active_leg = await cur.fetchone()

                    if active_leg:
                        leg_id = active_leg[0]
                        dep_load_status = active_leg[1]
                        dep_is_bobtail = active_leg[2]
                        booked_destination = active_leg[3]

                        # Arriving somewhere other than where the departure said
                        # they were going. Compared by site, so 200F and 200R do
                        # not read as a mismatch, and only when the driver named
                        # a facility -- most arrivals just say "arrived".
                        wrong_destination = (
                            dest_loc != "UNKNOWN"
                            and booked_destination
                            and site_of(dest_loc) != site_of(booked_destination)
                        )

                        if dep_is_bobtail or is_bobtail_flag:
                            resolved_action = "BOBTAIL_ARRIVE"
                            target_status = "COMPLETED"
                        else:
                            resolved_action = action_type or ("LIVE_UNLOAD" if dep_load_status == "LOADED" else "DROP_YARD")
                            
                            if action_type in ["UNLOAD_COMPLETED", "FINISHED_UNLOAD", "DROP"]:
                                target_status = "COMPLETED"
                            elif action_type == "LIVE_LOAD":
                                target_status = "LOADING"
                            elif action_type == "LIVE_UNLOAD":
                                target_status = "UNLOADING"
                            else:
                                target_status = "COMPLETED"

                        await cur.execute(
                            f"""UPDATE {TABLE_SHUTTLE_LEGS} 
                                   SET arrival_time = COALESCE(arrival_time, %s),
                                       arrival_action = %s,
                                       -- A dock named on arrival means the driver
                                       -- had already pulled to a door, so real time
                                       -- on site is longer than the recorded dwell.
                                       arrival_at_dock = %s,
                                       dock_number = COALESCE(%s, dock_number),
                                       do_number = COALESCE(%s, do_number),
                                       receiver_signed = COALESCE(%s, receiver_signed),
                                       leg_status = %s
                                 WHERE id = %s;""",
                            (
                                msg_timestamp, 
                                resolved_action, 
                                1 if door_num else 0, 
                                door_num, 
                                do_num, 
                                # None, not 0: COALESCE must fall through to the
                                # stored value when no signature was detected,
                                # otherwise an arrival wipes a POD captured earlier.
                                1 if receiver_signed else None, 
                                target_status, 
                                leg_id
                            )
                        )
                        await close_rm_load(cur, leg_id, msg_timestamp)
                        await conn.commit()
                        logger.info(f"✅ Driver #{did} arrived at {dest_loc}. Leg #{leg_id} updated to {target_status}.")

                        if wrong_destination:
                            logger.warning(
                                f"Driver #{did} was routed to {booked_destination} "
                                f"but reports arriving at {dest_loc}."
                            )
                            return {
                                "is_clean": False,
                                "leg_id": leg_id,
                                "card_text": (
                                    f"⚠️ **MANUAL RECONCILE: Wrong Destination**\n"
                                    f"\U0001f464 Driver: {user_name}\n"
                                    f"\U0001f4cd Routed to `{booked_destination}` "
                                    f"but arrived at `{dest_loc}`.\n"
                                    f"\U0001f69b Leg `{leg_id}` records the arrival as reported.\n"
                                    f"\U0001f449 Confirm with the driver while they are still on site."
                                )
                            }

                        return {"is_clean": True, "leg_id": leg_id, "card_text": None}

                    # No open trip. A plain "arrived" with nothing running is
                    # nothing to record, but a LOADED pickup is a departure the
                    # driver forgot to announce.
                    if (load_status_val == "LOADED"
                            and PICKUP_PATTERN.search(raw_text)
                            and origin_loc != "UNKNOWN"):
                        logger.warning(
                            f"Driver #{did} picked up a load at {origin_loc} "
                            f"without naming a destination; carding for dispatch."
                        )
                        return {
                            "is_clean": False,
                            "leg_id": None,
                            "card_text": pickup_with_no_destination(
                                origin_loc, door_num,
                                text_trailer or ocr_trailer),
                        }

                    return {"is_clean": True, "leg_id": None, "card_text": None}

                # =========================================================
                # CASE 3: HISTORICAL BOL DOCUMENT PATCH
                # =========================================================
                case "CASE_HISTORICAL_BOL_UPDATE":
                    if not bol_number:
                        return {
                            "is_clean": False,
                            "leg_id": None,
                            "card_text": (
                                f"⚠️ **MANUAL RECONCILE: Unreadable Historical Paperwork**\n"
                                f"👤 Driver: {user_name}\n"
                                f"❓ Issue: Document uploaded but no readable BOL # could be parsed."
                            )
                        }

                    await cur.execute(
                        f"""SELECT id 
                              FROM {TABLE_SHUTTLE_LEGS} 
                             WHERE user_id = %s 
                               AND bol_number = %s 
                               AND departure_time >= NOW() - INTERVAL 48 HOUR
                          ORDER BY id DESC 
                             LIMIT 1;""",
                        (did, bol_number)
                    )
                    target_leg = await cur.fetchone()

                    if target_leg:
                        leg_id = target_leg[0]
                        await cur.execute(
                            f"""UPDATE {TABLE_SHUTTLE_LEGS} 
                                   SET bol_image = COALESCE(%s, bol_image),
                                       document_type = IF(%s != 'UNKNOWN', %s, document_type),
                                       paperwork_time = COALESCE(paperwork_time, %s),
                                       shipper_signed = COALESCE(%s, shipper_signed),
                                       receiver_signed = COALESCE(%s, receiver_signed)
                                 WHERE id = %s;""",
                            (
                                primary_image_blob,
                                document_type, document_type,
                                msg_timestamp if primary_image_blob else None,
                                1 if shipper_signed else None,
                                1 if receiver_signed else None,
                                leg_id
                            )
                        )
                        await conn.commit()
                        logger.info(f"✅ Historical proof patched to Leg #{leg_id} for BOL '{bol_number}'.")
                        return {"is_clean": True, "leg_id": leg_id, "card_text": None}

                    return {
                        "is_clean": False,
                        "leg_id": None,
                        "card_text": (
                            f"⚠️ **MANUAL RECONCILE: Unmatched Historical BOL**\n"
                            f"👤 Driver: {user_name}\n"
                            f"📄 BOL #: `{bol_number}`\n"
                            f"❓ Issue: Valid BOL parsed, but no matching leg for BOL `{bol_number}` was found in the past 48h."
                        )
                    }

                # =========================================================
                # CASE AUTO RESOLVE: UNCAPTIONED PAPERWORK AUTO-LINK
                # =========================================================
                case "CASE_AUTO_RESOLVE":
                    if not bol_number:
                        return {
                            "is_clean": False,
                            "leg_id": None,
                            "card_text": (
                                f"⚠️ **MANUAL RECONCILE: Uncaptioned Image Scan Failed**\n"
                                f"👤 Driver: {user_name}\n"
                                f"❓ Action Needed: Document photo uploaded without text, but no clear BOL # was detected."
                            )
                        }

                    # Target driver's recent LOADED leg today that is missing paperwork
                    await cur.execute(
                        f"""SELECT id 
                              FROM {TABLE_SHUTTLE_LEGS} 
                             WHERE user_id = %s 
                               AND DATE(departure_time) = CURRENT_DATE()
                               AND load_status = 'LOADED'
                               AND (bol_number IS NULL OR bol_image IS NULL)
                          ORDER BY id DESC 
                             LIMIT 1;""",
                        (did,)
                    )
                    active_leg = await cur.fetchone()

                    if active_leg:
                        leg_id = active_leg[0]
                        await cur.execute(
                            f"""UPDATE {TABLE_SHUTTLE_LEGS} 
                                   SET bol_number = COALESCE(bol_number, %s),
                                       document_type = IF(%s != 'UNKNOWN', %s, document_type),
                                       bol_image = COALESCE(%s, bol_image),
                                       paperwork_time = COALESCE(paperwork_time, %s),
                                       shipper_signed = COALESCE(%s, shipper_signed),
                                       receiver_signed = COALESCE(%s, receiver_signed)
                                 WHERE id = %s;""",
                            (
                                bol_number,
                                document_type, document_type,
                                primary_image_blob,
                                msg_timestamp if primary_image_blob else None,
                                1 if shipper_signed else None,
                                1 if receiver_signed else None,
                                leg_id
                            )
                        )
                        await conn.commit()
                        logger.info(f"⚡ Silent Auto-Link: Saved BOL '{bol_number}' to Leg #{leg_id}")
                        return {"is_clean": True, "leg_id": leg_id, "card_text": None}

                    return {
                        "is_clean": False,
                        "leg_id": None, 
                        "card_text": (
                            f"⚠️ **MANUAL RECONCILE: No Open Leg Found**\n"
                            f"👤 Driver: {user_name}\n"
                            f"📄 BOL #: `{bol_number}`\n"
                            f"❓ Action Needed: Valid BOL scanned, but no unlinked LOADED leg today was found for this driver."
                        )
                    }

                # =========================================================
                # CASE 3: WITHIN-FACILITY REPOSITIONING
                # =========================================================
                case "CASE_3_INTRA_FACILITY_MOVE":
                    display_trailer = text_trailer if text_trailer and text_trailer != "UNKNOWN" else ocr_trailer

                    # A signed BOL sent with a yard-move caption is proof for the
                    # trip the driver just finished, not for the move they are
                    # describing. Positioning legs carry no paperwork, so it is
                    # attached to the delivery instead of being discarded.
                    if primary_image_blob and bol_number:
                        await cur.execute(
                            f"""SELECT id 
                                  FROM {TABLE_SHUTTLE_LEGS} 
                                 WHERE user_id = %s 
                                   AND is_positioning_leg = 0 
                                   AND (bol_image IS NULL OR bol_number IS NULL) 
                                   AND DATE(departure_time) = CURRENT_DATE() 
                              ORDER BY id DESC 
                                 LIMIT 1;""",
                            (did,)
                        )
                        delivery = await cur.fetchone()
                        if delivery:
                            await cur.execute(
                                f"""UPDATE {TABLE_SHUTTLE_LEGS} 
                                       SET bol_number = COALESCE(bol_number, %s),
                                           bol_image = COALESCE(bol_image, %s),
                                           document_type = IF(%s != 'UNKNOWN', %s, document_type),
                                           paperwork_time = COALESCE(paperwork_time, %s),
                                           receiver_signed = COALESCE(%s, receiver_signed)
                                     WHERE id = %s;""",
                                (bol_number, primary_image_blob,
                                 document_type, document_type, msg_timestamp,
                                 1 if receiver_signed else None, delivery[0])
                            )
                            await conn.commit()
                            logger.info(
                                f"Paperwork {bol_number} sent with a yard-move "
                                f"caption; attached to delivery Leg #{delivery[0]}."
                            )

                    def as_dock(value):
                        """A dock position, or None if this is really a facility.

                        The parser occasionally puts a facility code in a dock
                        field when the driver names the site ("drop empty 200 r
                        yard"). A facility is never a dock, so it is rejected
                        here rather than stored as one.
                        """
                        clean = (value or "").strip().upper()
                        if not clean:
                            return None
                        if clean == "YARD":
                            return clean
                        if normalize_location(clean) in known_codes or clean in known_codes:
                            logger.info(
                                f"Ignoring facility code {clean!r} in a dock field."
                            )
                            return None
                        return clean

                    known_codes = set(LOCATION_CACHE.get("codes") or [])
                    known_codes |= set(LOCATION_CACHE.get("alias_map") or {})

                    misplaced = [c for c in (origin_dock, destination_dock)
                                 if c and as_dock(c) is None and c.strip().upper() != "YARD"]
                    from_dock = as_dock(origin_dock) or as_dock(door_num)
                    to_dock = as_dock(destination_dock)

                    # If a facility arrived in a dock field and none was given
                    # as a location, that is where the move happened.
                    if misplaced and origin_loc == "UNKNOWN" and dest_loc == "UNKNOWN":
                        origin_loc = dest_loc = normalize_location(misplaced[0])
                    # Usually no facility is named ("empty move #13 to #47"),
                    # so fall back to where the driver was last recorded.
                    facility = origin_loc if origin_loc != "UNKNOWN" else dest_loc
                    if facility == "UNKNOWN":
                        await cur.execute(
                            f"""SELECT destination_location 
                                  FROM {TABLE_SHUTTLE_LEGS} 
                                 WHERE user_id = %s 
                                   AND destination_location NOT IN ('UNKNOWN', 'MISSING_ORIGIN', 'MISSING_DEST', '')
                              ORDER BY id DESC 
                                 LIMIT 1;""",
                            (did,)
                        )
                        last_seen = await cur.fetchone()
                        facility = last_seen[0] if (last_seen and last_seen[0]) else "UNKNOWN"

                    # Which half of the site each door belongs to. At 200 the
                    # FG inbound doors are 3-21 (200F) and the RM outbound doors
                    # 47-66 (200R), so "finish live unloading at pactra #3 move
                    # to Dock 47" is a trailer coming off an inbound door and
                    # being staged on a vacant outbound one -- the driver saving
                    # himself, or whoever takes the next RM round, a hook.
                    site = site_of(facility)
                    from_facility = facility_for_dock(from_dock, site) or facility
                    to_facility = facility_for_dock(to_dock, site) or facility

                    move_desc = f"{from_dock or '?'} \u2794 {to_dock or '?'}"

                    # A drop at the facility a trip is still running to is that
                    # trip's ARRIVAL, not a yard shuffle. "Drop empty trailer at
                    # sds yard D021" reads exactly like an internal move -- one
                    # facility, a position, no travel -- and the parser cannot
                    # tell the difference, because only the open leg says the
                    # driver was on their way there. Recorded as a move it
                    # invented a second SDS -> SDS leg and left the real one
                    # open until the next departure closed it, stamping an
                    # arrival time tens of minutes late.
                    #
                    # Only a drop with no position moved FROM qualifies: "door 7
                    # to yard" and "200F to 200R" both name somewhere the driver
                    # moved off, so they stay genuine moves whatever else is open.
                    named_two_places = (
                        origin_loc != "UNKNOWN" and dest_loc != "UNKNOWN"
                        and origin_loc != dest_loc
                    )
                    # Hooking a load is the start of a trip, never a yard move:
                    # the driver just left the destination out.
                    loaded_pickup = bool(
                        load_status_val == "LOADED" and from_dock is None
                        and not named_two_places
                        and PICKUP_PATTERN.search(raw_text)
                    )
                    await cur.execute(
                        f"""SELECT id, destination_location
                              FROM {TABLE_SHUTTLE_LEGS}
                             WHERE user_id = %s
                               AND is_positioning_leg = 0
                               AND leg_status = 'IN_TRANSIT'
                          ORDER BY id DESC
                             LIMIT 1;""",
                        (did,)
                    )
                    open_trip = await cur.fetchone()
                    if (open_trip and from_dock is None and not named_two_places
                            and facility != "UNKNOWN"):
                        trip_id, booked_destination = open_trip
                        if site_of(booked_destination or "") == site_of(facility):
                            at_dock = bool(to_dock) and to_dock != "YARD"
                            # The trailer named at the drop wins over the one on
                            # the leg. A leg's trailer is often inherited from the
                            # driver's previous leg (CASE 1 fills it in when the
                            # message does not name one), so it is a guess that
                            # goes stale the moment they swap trailers -- Sokhwan
                            # Yun's 11:18 SDS run carried 25773 from three legs
                            # earlier while he was actually hauling 77155. The
                            # drop is a first-hand report from the destination,
                            # and it is the trailer the dispatcher logs.
                            dropped_trailer = (
                                display_trailer
                                if display_trailer and display_trailer != "UNKNOWN"
                                else None
                            )
                            await cur.execute(
                                f"""UPDATE {TABLE_SHUTTLE_LEGS}
                                       SET arrival_time = COALESCE(arrival_time, %s),
                                           arrival_action = %s,
                                           arrival_at_dock = %s,
                                           destination_dock = COALESCE(destination_dock, %s),
                                           do_number = COALESCE(do_number, %s),
                                           trailer_number = COALESCE(%s, trailer_number),
                                           receiver_signed = COALESCE(%s, receiver_signed),
                                           leg_status = 'COMPLETED'
                                     WHERE id = %s;""",
                                (msg_timestamp,
                                 action_type or ("DROP_DOCK" if at_dock
                                                 else "DROP_YARD"),
                                 1 if at_dock else 0,
                                 to_dock, do_num, dropped_trailer,
                                 1 if receiver_signed else None,
                                 trip_id)
                            )
                            await close_rm_load(cur, trip_id, msg_timestamp)
                            # Stamped after the arrival, never before: the unload
                            # is measured from arrival, and finish_rm_load skips
                            # any load that has not arrived yet.
                            if intent.get("work_finished"):
                                await stamp_finished(trip_id)
                            await conn.commit()
                            logger.info(
                                f"Driver #{did} dropped at {facility} while Leg "
                                f"#{trip_id} was still running there; recorded as "
                                f"that leg's arrival, not a positioning move."
                            )
                            # They hooked the next load in the same breath. The
                            # arrival is safe now; the onward trip is not, so it
                            # still has to be raised.
                            if loaded_pickup:
                                logger.warning(
                                    f"Driver #{did} picked up a load at {facility} "
                                    f"without naming a destination; carding."
                                )
                                return {
                                    "is_clean": False,
                                    "leg_id": trip_id,
                                    "card_text": pickup_with_no_destination(
                                        facility, to_dock or door_num,
                                        display_trailer),
                                }
                            return {"is_clean": True, "leg_id": trip_id,
                                    "card_text": None}

                    # "Finish live unloading at pactra #3 move to Dock 47" ends
                    # one job and describes a move in the same breath. CASE 1
                    # already handles that pairing on a departure; a yard move
                    # needs it too, or the unload time is lost.
                    #
                    # Only if the last trip actually ended here. When the
                    # departure was never recorded -- the driver did not say
                    # where he was taking the load -- the newest leg is some
                    # earlier run to somewhere else, and stamping it would put
                    # this unload against the wrong trip.
                    if intent.get("work_finished"):
                        await cur.execute(
                            f"""SELECT id, destination_location 
                                  FROM {TABLE_SHUTTLE_LEGS} 
                                 WHERE user_id = %s 
                                   AND is_positioning_leg = 0 
                              ORDER BY id DESC 
                                 LIMIT 1;""",
                            (did,)
                        )
                        finished_candidate = await cur.fetchone()
                        if (finished_candidate
                                and site_of(finished_candidate[1] or "") == site):
                            await stamp_finished(finished_candidate[0])
                            await conn.commit()
                        else:
                            logger.warning(
                                f"Driver #{did} reported work finished at "
                                f"{facility}, but their last recorded trip did "
                                f"not end there; completion not stamped."
                            )

                    # Carded rather than recorded: the dispatcher reads the
                    # destination off the arrival that follows, and only needs to
                    # know the message happened. A leg to UNKNOWN would instead
                    # enter a round and a route as if it were a real trip.
                    if loaded_pickup and facility != "UNKNOWN":
                        logger.warning(
                            f"Driver #{did} picked up a load at {facility} "
                            f"without naming a destination; carding for dispatch."
                        )
                        return {
                            "is_clean": False,
                            "leg_id": None,
                            "card_text": pickup_with_no_destination(
                                facility, to_dock or door_num, display_trailer),
                        }

                    # Drivers report one move more than once -- typically the
                    # signed paperwork first and a photo of the parked trailer a
                    # few minutes later, both captioned with the same drop. Those
                    # are one event, so a matching recent move is completed
                    # rather than duplicated.
                    await cur.execute(
                        f"""SELECT id, trailer_number, origin_dock, destination_dock 
                              FROM {TABLE_SHUTTLE_LEGS} 
                             WHERE user_id = %s 
                               AND is_positioning_leg = 1 
                               AND arrival_time >= %s - INTERVAL %s MINUTE 
                          ORDER BY id DESC 
                             LIMIT 5;""",
                        (did, msg_timestamp, REPEAT_MOVE_MINUTES)
                    )
                    for row in await cur.fetchall():
                        prior_id, prior_trailer, prior_from, prior_to = row
                        if to_dock and prior_to and to_dock != prior_to:
                            continue
                        if (display_trailer and prior_trailer
                                and display_trailer != prior_trailer):
                            continue
                        await cur.execute(
                            f"""SELECT origin_location FROM {TABLE_SHUTTLE_LEGS} 
                                 WHERE id = %s;""",
                            (prior_id,)
                        )
                        (prior_site,) = await cur.fetchone()
                        if site_of(prior_site or "") != site_of(facility):
                            continue

                        await cur.execute(
                            f"""UPDATE {TABLE_SHUTTLE_LEGS} 
                                   SET trailer_number = COALESCE(trailer_number, %s),
                                       origin_dock = COALESCE(origin_dock, %s),
                                       destination_dock = COALESCE(destination_dock, %s)
                                 WHERE id = %s;""",
                            (display_trailer, from_dock, to_dock, prior_id)
                        )
                        await conn.commit()
                        logger.info(
                            f"Driver #{did} re-reported the move {move_desc} at "
                            f"{facility}; folded into positioning Leg #{prior_id}."
                        )
                        return {"is_clean": True, "leg_id": prior_id, "card_text": None}

                    # Every internal move gets its own row. Drivers never label
                    # these, and billing is by shift rather than by move, so no
                    # cleanup-vs-reposition guess is needed or wanted. Folding a
                    # move into the trip leg would also silently overwrite earlier
                    # moves, since a leg holds only one dock pair.
                    #
                    # Recorded COMPLETED so a later arrival can never mistake one
                    # for an open trip.
                    await cur.execute(
                        f"""INSERT INTO {TABLE_SHUTTLE_LEGS} (
                               user_id, 
                               trailer_number, 
                               origin_location, 
                               destination_location, 
                               origin_dock, 
                               destination_dock, 
                               departure_time, 
                               arrival_time, 
                               arrival_action, 
                               is_positioning_leg, 
                               leg_status, 
                               load_status,
                               round_number
                           ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 1, 'COMPLETED', %s, %s);""",
                        (
                            did, display_trailer, from_facility, to_facility,
                            from_dock, to_dock, msg_timestamp, msg_timestamp,
                            "YARD_DROP" if to_dock == "YARD" else "DOCK_MOVE",
                            load_status_val,
                            await current_round_number()
                        )
                    )
                    await conn.commit()
                    leg_id = cur.lastrowid
                    logger.info(
                        f"\U0001f4e6 Driver #{did} moved {move_desc} at {facility}; "
                        f"recorded as positioning Leg #{leg_id}."
                    )

                    # A known facility is enough to record the move. "200F to
                    # 200R" says front to rear without naming a dock, and
                    # demanding one would card a perfectly clear report.
                    if facility == "UNKNOWN":
                        return {
                            "is_clean": False,
                            "leg_id": leg_id,
                            "card_text": (
                                f"⚠️ **MANUAL RECONCILE: Incomplete Internal Move**\n"
                                f"👤 Driver: {user_name}\n"
                                f"💬 Message: `{raw_text}`\n"
                                f"🚛 Trailer: `{display_trailer or 'UNKNOWN'}`\n"
                                f"📍 Facility: `{facility}` | Move: `{move_desc}`\n"
                                f"❓ Issue: Could not determine the facility or the destination "
                                f"position. Saved as Leg `{leg_id}` for correction."
                            )
                        }

                    return {"is_clean": True, "leg_id": leg_id, "card_text": None}

                # =========================================================
                # END-OF-SHIFT MANIFEST
                # =========================================================
                case "CASE_MANIFEST":
                    image = intent.get("manifest_image") or primary_image_blob
                    if not image:
                        return {"is_clean": True, "leg_id": None, "card_text": None}

                    parsed = intent.get("manifest_parsed") or {}
                    manifest_id, rows = await store_manifest(
                        cur, did, user_name, msg_timestamp, image, parsed)
                    await conn.commit()
                    if not manifest_id:
                        return {"is_clean": True, "leg_id": None, "card_text": None}

                    logger.info(
                        f"\U0001f4cb Manifest #{manifest_id} stored for Driver "
                        f"#{did} with {rows} row(s) read."
                    )
                    note = f" · {rows} trips read" if rows else " · not readable"
                    return {
                        "is_clean": True,
                        "leg_id": None,
                        "card_text": None,
                        "reply_text": f"\U0001f4cb Manifest received{note}",
                    }

                # =========================================================
                # PARSE FAILURE: surface, never swallow
                # =========================================================
                case "PARSE_FAILED":
                    logger.error(
                        f"Parser unavailable for Driver #{did} ({user_name}); "
                        f"raising manual card. Detail: {intent.get('parse_error')}"
                    )
                    return {
                        "is_clean": False,
                        "leg_id": None,
                        "card_text": (
                            f"⚠️ **MANUAL RECONCILE: Message Could Not Be Parsed**\n"
                            f"👤 Driver: {user_name}\n"
                            f"💬 Message: `{raw_text or '(no text - attached document only)'}`\n"
                            f"❓ Issue: The AI parser was unavailable after repeated retries, so this "
                            f"update was NOT recorded. Please enter it manually."
                        )
                    }

                case _:
                    return {"is_clean": False, "card_text": None}