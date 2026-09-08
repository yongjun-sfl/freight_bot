"""One handler per trip case type.

Each ``handle_*`` function replaces one ``case ``X``:`` arm of the
old ``commit_trip_leg`` match block. Bodies are moved verbatim; the
only changes are the message-scoped locals (now read off ``ctx``,
the ``LegContext``) and the shared helpers (now ``ctx.<method>``
calls).
"""

import logging

from config import TABLE_SHUTTLE_LEGS
from ai_engine import (
    LOCATION_CACHE,
    facility_for_dock,
    normalize_location,
    site_of,
)
from manifests import store_manifest
from rm_manifest import close_rm_load, record_rm_load
from shifts import (
    format_worked,
    record_clock_in,
    record_clock_out,
    record_lunch,
)

from leg_helpers import REPEAT_MOVE_MINUTES, UNLOAD_PATTERN

logger = logging.getLogger(__name__)


async def handle_lunch_start(ctx):
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


    return {
        "is_clean": True, "leg_id": None, "card_text": None,
        "reply_text": "🍽 Lunch started",
    }



async def handle_lunch_end(ctx):
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


async def handle_clock_in(ctx):
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



async def handle_clock_out(ctx):
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


async def handle_work_finished(ctx):
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


    finished_leg = await ctx.stamp_finished()
    await conn.commit()
    if not finished_leg:
        return {"is_clean": True, "leg_id": None, "card_text": None}
    logger.info(f"🏁 Driver #{did} finished work on Leg #{finished_leg}.")
    return {"is_clean": True, "leg_id": finished_leg, "card_text": None}

# =========================================================
# CASE 1: INTER-FACILITY DEPARTURE
# =========================================================


async def handle_case_1_departure(ctx):
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
    duplicate_of = await ctx.find_duplicate_bol_leg()

    # RECEIVER-SIGNED REPOST = POD backfill. When the SAME BOL is reposted
    # carrying a receiver stamp/signature -- proof the goods reached the
    # consignee -- it is not stale paperwork. It is the delivery receipt for
    # the ORIGINAL leg, so backfill that leg's BOL image and mark it
    # receiver-signed. Whether the leg is COMPLETED or still in transit does
    # not matter: the POD belongs to it either way. (A shipper-signed
    # duplicate is still treated as stale below; only a receiver-signed copy
    # is a completion signal.)
    if duplicate_of and receiver_signed:
        await cur.execute(
            f"""UPDATE {TABLE_SHUTTLE_LEGS} 
                   SET bol_image = COALESCE(%s, bol_image),
                       document_type = IF(%s != 'UNKNOWN', %s, document_type),
                       paperwork_time = COALESCE(paperwork_time, %s),
                       receiver_signed = 1
                 WHERE id = %s;""",
            (primary_image_blob, document_type, document_type,
             msg_timestamp if primary_image_blob else None, duplicate_of)
        )
        await conn.commit()
        logger.info(
            f"📦 Driver #{did} reposted BOL '{bol_number}' receiver-signed; "
            f"POD backfilled to Leg #{duplicate_of}."
        )

        # Movement always wins over a document. If the caption is ONLY the
        # POD for an already-recorded leg (same route, or no route at all),
        # no new leg is opened. But a caption like "finish live unloading
        # empty 7634 to 200" is ALSO the next departure: backfill the POD on
        # the delivered leg above, then keep recording the empty reposition.
        await cur.execute(
            f"SELECT origin_location, destination_location "
            f"FROM {TABLE_SHUTTLE_LEGS} WHERE id = %s;",
            (duplicate_of,)
        )
        dup_route = await cur.fetchone()
        unknown_orig = origin_loc in ("UNKNOWN", "NONE", "NULL", "MISSING_ORIGIN")
        unknown_dest = dest_loc in ("UNKNOWN", "NONE", "NULL", "MISSING_DEST")
        same_route = bool(
            dup_route and not unknown_orig and not unknown_dest
            and site_of(origin_loc) == site_of(dup_route[0])
            and site_of(dest_loc) == site_of(dup_route[1])
        )
        if same_route or (unknown_orig and unknown_dest):
            return {"is_clean": True, "leg_id": duplicate_of, "card_text": None}

        # Onward movement: the duplicate document belongs to the delivered
        # leg (POD-backfilled above). Keep the route, discard the stale BOL
        # fields so they do not ride along, and do not card a repost.
        pod_bol = bol_number
        bol_number = ctx.bol_number = None
        document_type = ctx.document_type = "UNKNOWN"
        primary_image_blob = ctx.primary_image_blob = None
        shipper_signed = ctx.shipper_signed = False
        receiver_signed = ctx.receiver_signed = False
        duplicate_of = None
        logger.info(
            f"Driver #{did} POD for BOL '{pod_bol}' backfilled; "
            f"recording onward movement without that paperwork."
        )

    stale_bol = None
    if duplicate_of:
        stale_bol = bol_number
        bol_number = ctx.bol_number = None
        document_type = ctx.document_type = "UNKNOWN"
        primary_image_blob = ctx.primary_image_blob = None
        shipper_signed = ctx.shipper_signed = False
        logger.warning(
            f"Driver #{did} attached BOL '{stale_bol}' already recorded on "
            f"Leg #{duplicate_of}; saving movement without paperwork."
        )

    # WORK-FINISHED ORIGIN CORRECTION: after a live unload the driver is at
    # the facility they just delivered, even when the empty-reposition caption
    # contains a stray facility name (Young Teak Kong 09:44 wrote "Finish Live
    # unloading empty 200 to sds" after unloading at E2F -- the trip is
    # E2F -> SDS, not 200F -> SDS). Trust the leg that was just unloaded.
    if (intent.get("work_finished") and UNLOAD_PATTERN.search(raw_text)
            and load_status_val == "EMPTY"
            and origin_loc not in ("UNKNOWN", "NONE", "NULL", "MISSING_ORIGIN")):
        await cur.execute(
            f"""SELECT destination_location
                  FROM {TABLE_SHUTTLE_LEGS}
                 WHERE user_id = %s
                   AND is_positioning_leg = 0
              ORDER BY id DESC
                 LIMIT 1;""",
            (did,)
        )
        unloaded_at_row = await cur.fetchone()
        if (unloaded_at_row and unloaded_at_row[0]
                and site_of(unloaded_at_row[0]) != site_of(origin_loc)):
            logger.info(
                f"Driver #{did} finished unloading at {unloaded_at_row[0]} but "
                f"reported empty origin {origin_loc}; using the unload site."
            )
            origin_loc = ctx.origin_loc = normalize_location(unloaded_at_row[0])

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

    # Still nowhere to go, but the load is carrying paperwork
    # that says where it is consigned. Only reached when the
    # driver named no destination at all, and skipped for a
    # duplicate BOL, whose ship-to belongs to the previous load.
    if (dest_loc == "UNKNOWN" and not duplicate_of
            and intent.get("bol_destination")
            and site_of(intent["bol_destination"]) != site_of(origin_loc)):
        dest_loc = intent["bol_destination"]
        logger.info(
            f"Driver #{did} named no destination; BOL ship-to "
            f"gives {dest_loc}."
        )

    if not display_trailer or display_trailer == "UNKNOWN":
        display_trailer = last_leg[1] if (last_leg and last_leg[1]) else "UNKNOWN"

    # A merged "live loading finished load 200 to E2F" ends the
    # previous trip and starts the next in one message.
    if intent.get("work_finished"):
        await ctx.stamp_finished()

    # PRECEDING EMPTY/BOBTAIL INFERENCE: a departure from a site the driver's
    # last completed trip did not end at means they must have deadheaded there
    # empty first. Drivers do not always type that leg (John Shim wrote only
    # "Lunch off" at E1, then 13:35 "Empty pickup SDs do21 to e1" -- the manual
    # log carries the E1 -> SDS bobtail between them). Infer it when no trip is
    # open and no such reposition is already recorded.
    if (not duplicate_of
            and origin_loc not in ("UNKNOWN", "NONE", "NULL", "MISSING_ORIGIN")
            and dest_loc not in ("UNKNOWN", "NONE", "NULL", "MISSING_DEST")
            and site_of(origin_loc) != site_of(dest_loc)):
        await cur.execute(
            f"""SELECT id, destination_location
                  FROM {TABLE_SHUTTLE_LEGS}
                 WHERE user_id = %s
                   AND is_positioning_leg = 0
                   AND leg_status IN ('IN_TRANSIT', 'ARRIVED', 'UNLOADING', 'LOADING')
              ORDER BY id DESC
                 LIMIT 1;""",
            (did,)
        )
        open_leg = await cur.fetchone()
        if not open_leg:
            await cur.execute(
                f"""SELECT destination_location,
                           trailer_number,
                           COALESCE(finished_time, arrival_time, departure_time) AS ended_at
                      FROM {TABLE_SHUTTLE_LEGS}
                     WHERE user_id = %s
                       AND is_positioning_leg = 0
                       AND leg_status = 'COMPLETED'
                  ORDER BY id DESC
                     LIMIT 1;""",
                (did,)
            )
            last_done = await cur.fetchone()
            if (last_done and last_done[0]
                    and last_done[0] not in ("UNKNOWN", "NONE", "NULL",
                                             "MISSING_DEST", "")
                    and last_done[2]
                    and site_of(last_done[0]) != site_of(origin_loc)
                    and str(last_done[2])[:10] == str(msg_timestamp)[:10]):
                await cur.execute(
                    f"""SELECT origin_location, destination_location
                          FROM {TABLE_SHUTTLE_LEGS}
                         WHERE user_id = %s
                           AND is_positioning_leg = 0
                           AND departure_time >= %s
                           AND departure_time <= %s
                      ORDER BY id;""",
                    (did, last_done[2], msg_timestamp)
                )
                already_moved = any(
                    site_of(row[0]) == site_of(last_done[0])
                    and site_of(row[1]) == site_of(origin_loc)
                    for row in await cur.fetchall()
                )
                if not already_moved:
                    inferred_id = await ctx.open_departure_leg(
                        normalize_location(last_done[0]), origin_loc,
                        None, departure_time=msg_timestamp,
                        load_status="EMPTY")
                    await cur.execute(
                        f"""UPDATE {TABLE_SHUTTLE_LEGS}
                               SET arrival_time = COALESCE(arrival_time, %s),
                                   arrival_action = 'BOBTAIL_ARRIVE',
                                   leg_status = 'COMPLETED'
                             WHERE id = %s;""",
                        (msg_timestamp, inferred_id)
                    )
                    await conn.commit()
                    logger.info(
                        f"Driver #{did} departed from {origin_loc} but last trip "
                        f"ended at {last_done[0]}; inferred the empty reposition "
                        f"as Leg #{inferred_id}."
                    )

    # Complete prior active legs upon new departure
    await cur.execute(
        f"""UPDATE {TABLE_SHUTTLE_LEGS} 
               SET leg_status = 'COMPLETED', 
                   arrival_time = COALESCE(arrival_time, %s)
             WHERE user_id = %s 
               AND leg_status IN ('IN_TRANSIT', 'ARRIVED', 'UNLOADING', 'LOADING');""",
        (msg_timestamp, did)
    )

    leg_id = await ctx.open_departure_leg(
        origin_loc, dest_loc, display_trailer)

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
    # A bobtail carries no trailer by definition, and an EMPTY
    # reposition rarely names one either -- so a departure must
    # never be rejected for a missing trailer when the trailer
    # is empty. It is only mandatory when the trailer is LOADED
    # (the rigor matters there). Origin and destination are
    # still required: without them there is no route to record.
    # This is what let terse "bobtail to 210" reports (John
    # Shim 10:28) and "Empty 200 sds" (Matthew Cho 09:47 on
    # 08/28) vanish while the manual log keeps them as rows.
    missing_trailer_ok = (
        is_bobtail_flag
        or (load_status_val or "") == "EMPTY"
    )
    if (origin_loc == "UNKNOWN" or dest_loc == "UNKNOWN"
            or (display_trailer == "UNKNOWN"
                and not missing_trailer_ok)):
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


async def handle_case_2_arrival(ctx):
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

        # The same message announced an arrival and the next
        # hook. "Load trailer pickup sds dock28 #77209" is filed
        # as an arrival -- a facility, a door, no travel named --
        # so the load leaving again was recorded nowhere at all,
        # not even as a card, and the whole SDS -> 200 run
        # vanished from the day.
        if is_load_pickup:
            hooked_at = (dest_loc if dest_loc != "UNKNOWN"
                         else booked_destination)
            if hooked_at:
                return await ctx.start_hooked_load(
                    hooked_at, door_num,
                    text_trailer or ocr_trailer, leg_id)

        if wrong_destination:
            logger.warning(
                f"Driver #{did} was routed to {booked_destination} "
                f"but reports arriving at {dest_loc}."
            )
            # The arrival names a real facility and the trip is still open, so
            # the arrival is stronger evidence of where the driver actually
            # went than the departure caption was. Correct the record and let
            # the dispatcher confirm (the card stays visible).
            await cur.execute(
                f"UPDATE {TABLE_SHUTTLE_LEGS} "
                f"   SET destination_location = %s "
                f" WHERE id = %s;",
                (dest_loc, leg_id),
            )
            await conn.commit()
            return {
                "is_clean": False,
                "leg_id": leg_id,
                "card_text": (
                    f"⚠️ **MANUAL RECONCILE: Wrong Destination Corrected**\n"
                    f"\U0001f464 Driver: {user_name}\n"
                    f"\U0001f4cd Routed to `{booked_destination}` "
                    f"but arrived at `{dest_loc}`.\n"
                    f"\U0001f69b Leg `{leg_id}` now records "
                    f"`{booked_destination}` -> `{dest_loc}`.\n"
                    f"\U0001f449 Confirm with the driver while they are still on site."
                )
            }

        return {"is_clean": True, "leg_id": leg_id, "card_text": None}

    # No open trip. A plain "arrived" with nothing running is
    # nothing to record, but a LOADED pickup is a departure the
    # driver forgot to announce.
    #
    # The single facility a pickup names is where they hooked,
    # whichever field the parser put it in: "pickup sds dock28"
    # reads as travel TO sds, so it comes back as the
    # destination, and testing origin alone left the message
    # silently dropped.
    hooked_at = origin_loc if origin_loc != "UNKNOWN" else dest_loc
    if is_load_pickup and hooked_at != "UNKNOWN":
        return await ctx.start_hooked_load(
            hooked_at, door_num, text_trailer or ocr_trailer)

    # EMPTY-DROP INFERENCE: the driver dropped an empty at a facility
    # with no trip open to it. The departure was never announced, but the
    # drop itself is first-hand evidence they moved there empty from
    # wherever their last completed trip ended. Young Teak Kong 15:19 on
    # 08/28 ("Empty drop yard sds D 012" after finishing unload at 200F
    # at 14:37) is the manual log's 14:40 200F -> SDS empty row.
    if (not is_load_pickup and load_status_val == "EMPTY"
            and dest_loc not in ("UNKNOWN", "NONE", "NULL", "MISSING_DEST")):
        await cur.execute(
            f"""SELECT destination_location,
                       trailer_number,
                       COALESCE(finished_time, arrival_time, departure_time) AS ended_at
                  FROM {TABLE_SHUTTLE_LEGS}
                 WHERE user_id = %s
                   AND is_positioning_leg = 0
              ORDER BY id DESC
                 LIMIT 1;""",
            (did,)
        )
        delivered = await cur.fetchone()
        if (delivered and delivered[0]
                and delivered[0] not in ("UNKNOWN", "NONE", "NULL",
                                          "MISSING_DEST", "")
                and delivered[2]
                and site_of(delivered[0]) != site_of(dest_loc)
                and str(delivered[2])[:10] == str(msg_timestamp)[:10]):
            await cur.execute(
                f"""SELECT origin_location, destination_location
                      FROM {TABLE_SHUTTLE_LEGS}
                     WHERE user_id = %s
                       AND is_positioning_leg = 0
                       AND departure_time >= %s
                       AND departure_time <= %s
                  ORDER BY id;""",
                (did, delivered[2], msg_timestamp)
            )
            already_recorded = any(
                site_of(row[0]) == site_of(delivered[0])
                and site_of(row[1]) == site_of(dest_loc)
                for row in await cur.fetchall()
            )
            if not already_recorded:
                reported_trailer = text_trailer or ocr_trailer
                if reported_trailer and reported_trailer != "UNKNOWN":
                    empty_trailer = reported_trailer
                else:
                    empty_trailer = delivered[1]
                leg_id = await ctx.open_departure_leg(
                    normalize_location(delivered[0]), dest_loc,
                    empty_trailer, departure_time=delivered[2])
                await cur.execute(
                    f"""UPDATE {TABLE_SHUTTLE_LEGS}
                           SET arrival_time = COALESCE(arrival_time, %s),
                               arrival_action = 'DROP_YARD',
                               arrival_at_dock = %s,
                               dock_number = COALESCE(%s, dock_number),
                               trailer_number = COALESCE(%s, trailer_number),
                               leg_status = 'COMPLETED'
                         WHERE id = %s;""",
                    (msg_timestamp, 1 if door_num else 0, door_num,
                     empty_trailer, leg_id)
                )
                await conn.commit()
                logger.info(
                    f"Driver #{did} dropped empty at {dest_loc} with no trip "
                    f"open; inferred the empty reposition from "
                    f"{normalize_location(delivered[0])} as Leg #{leg_id}."
                )
                return {"is_clean": True, "leg_id": leg_id, "card_text": None}

    return {"is_clean": True, "leg_id": None, "card_text": None}

# =========================================================
# CASE 3: HISTORICAL BOL DOCUMENT PATCH
# =========================================================


async def handle_bol_update(ctx):
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


async def handle_auto_resolve(ctx):
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


async def handle_case_3_intra_move(ctx):
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

    # A work-finished "<= X, empty to X" where the parser read
    # the same facility for both ends usually means the driver
    # typo'd the ORIGIN ("Finished live unloading e2r g, Empty
    # to e2r" -- John Shim 10:17 on 08/28 meant E2F -> E2R).
    # The trailer was actually at the last trip leg's
    # destination (E2F, where the first half unloaded) and is
    # being moved to the text's facility (E2R). When the whole
    # message says the job finished, that last trip's
    # destination is where the driver really is -- use it as
    # the origin so the move records E2F -> E2R instead of a
    # same-code E2R -> E2R shuffle.
    if (origin_loc != "UNKNOWN" and origin_loc == dest_loc
            and site_of(origin_loc) == "E2"
            and intent.get("work_finished")):
        await cur.execute(
            f"""SELECT destination_location 
                  FROM {TABLE_SHUTTLE_LEGS} 
                 WHERE user_id = %s 
                   AND is_positioning_leg = 0 
                   AND leg_status = 'COMPLETED'
              ORDER BY id DESC 
                 LIMIT 1;""",
            (did,)
        )
        completed_row = await cur.fetchone()
        if completed_row and completed_row[0]:
            completed_dest = completed_row[0].strip().upper()
            if (completed_dest not in ("UNKNOWN", "NONE", "NULL")
                    and site_of(completed_dest) == site_of(origin_loc)
                    and completed_dest != origin_loc):
                logger.info(
                    f"Driver #{did} work-finished move origin "
                    f"'{origin_loc}' looks like a typo; using "
                    f"last trip destination '{completed_dest}'."
                )
                # Destination stays the text's facility (E2R); only the ORIGIN
                # inherits from the last trip. `facility` is the
                # shared fallback for from/to, so it must stay
                # the destination (E2R) -- the corrected origin
                # feeds the from-side below.
                dest_loc = origin_loc
                origin_loc = completed_dest

    # Which half of the site each door belongs to. At 200 the
    # FG inbound doors are 3-21 (200F) and the RM outbound doors
    # 47-66 (200R), so "finish live unloading at pactra #3 move
    # to Dock 47" is a trailer coming off an inbound door and
    # being staged on a vacant outbound one -- the driver saving
    # himself, or whoever takes the next RM round, a hook.
    site = site_of(facility)
    # For a typo-corrected E2F->E2R move, `facility` is the
    # destination (E2R) and `origin_loc` is E2F -- so the
    # from-side falls back to the corrected origin while the
    # to-side keeps the facility. For ordinary same-site moves
    # (200F->200R) origin IS the facility, so both sides fold
    # to one code as before.
    from_facility = facility_for_dock(
        from_dock, site) or (
        origin_loc if origin_loc != "UNKNOWN" else facility
    )
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
    # A hooked load is a departure, not a yard move: the
    # driver just left the destination out.
    loaded_pickup = bool(
        is_load_pickup and from_dock is None
        and not named_two_places
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
                await ctx.stamp_finished(trip_id)
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
                return await ctx.start_hooked_load(
                    facility, to_dock or door_num,
                    display_trailer, trip_id)
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
            await ctx.stamp_finished(finished_candidate[0])
            await conn.commit()
        else:
            logger.warning(
                f"Driver #{did} reported work finished at "
                f"{facility}, but their last recorded trip did "
                f"not end there; completion not stamped."
            )

    # Recorded if the paperwork says where it is consigned,
    # carded otherwise -- the dispatcher then reads the
    # destination off the arrival that follows and only needs to
    # know the message happened. Either way no leg to UNKNOWN is
    # written; that would enter a round and a route as if real.
    if loaded_pickup and facility != "UNKNOWN":
        return await ctx.start_hooked_load(
            facility, to_dock or door_num, display_trailer)

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
            await ctx.current_round_number()
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


async def handle_manifest(ctx):
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


async def handle_parse_failed(ctx):
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



async def handle_default(ctx):
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


    return {"is_clean": False, "card_text": None}
