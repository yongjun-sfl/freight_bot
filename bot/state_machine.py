import logging
from ai_engine import normalize_location

logger = logging.getLogger(__name__)


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
    text_trailer = intent.get("trailer_number") or intent.get("text_trailer")
    ocr_trailer = intent.get("ocr_trailer")
    bol_number = intent.get("bol_number")
    action_type = intent.get("action")
    door_num = intent.get("door_number")
    primary_image_blob = intent.get("primary_image_blob")
    receiver_signed = intent.get("receiver_signed", False)
    raw_text = intent.get("raw_text") or ""

    # 1. Location Normalization
    raw_orig = intent.get("origin_location")
    raw_dest = intent.get("destination_location")

    origin_loc = normalize_location(raw_orig) if raw_orig else None
    dest_loc = normalize_location(raw_dest) if raw_dest else None

    # 2. Dynamic Load Status Resolution
    parsed_load_status = intent.get("load_status")
    if parsed_load_status and parsed_load_status not in ["UNKNOWN", "NULL", "NONE"]:
        load_status_val = parsed_load_status
    elif action_type in ["UNLOAD_COMPLETED", "FINISHED_UNLOAD"]:
        load_status_val = "EMPTY"
    elif action_type in ["LIVE_LOAD", "HOOK"]:
        load_status_val = "LOADED"
    elif "load pickup" in raw_text.lower() or "pick up" in raw_text.lower():
        load_status_val = "LOADED"
    elif "empty" in raw_text.lower():
        load_status_val = "EMPTY"
    else:
        async with p.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """SELECT load_status FROM shuttle_legs 
                       WHERE user_id = %s AND load_status IS NOT NULL 
                       ORDER BY id DESC LIMIT 1;""",
                    (did,)
                )
                last_status_row = await cur.fetchone()
                load_status_val = last_status_row[0] if (last_status_row and last_status_row[0]) else "UNKNOWN"

    # 3. Database Execution Loop
    async with p.acquire() as conn:
        async with conn.cursor() as cur:

            # GLOBAL DUPLICATE BOL GUARD
            if bol_number:
                await cur.execute("SELECT id FROM shuttle_legs WHERE bol_number = %s LIMIT 1;", (bol_number,))
                existing_bol = await cur.fetchone()
                if existing_bol:
                    return {
                        "is_clean": False,
                        "leg_id": None,
                        "card_text": (
                            f"⚠️ **MANUAL RECONCILE: Duplicate BOL Number**\n"
                            f"👤 Driver: {user_name}\n"
                            f"📄 BOL Number: `{bol_number}`\n"
                            f"❓ Issue: This BOL is already logged under Leg ID `#{existing_bol[0]}`. Re-upload rejected."
                        )
                    }

            match case_type:

                # =========================================================
                # CASE 1: OUTBOUND DEPARTURE (INTER-FACILITY MOVEMENT)
                # =========================================================
                case "CASE_1_ORIGIN_DEPARTURE":
                    display_trailer = text_trailer if text_trailer and text_trailer != "UNKNOWN" else ocr_trailer

                    # 1. AUTO-HEALING: Check if active departure exists in the last 12h missing a BOL
                    await cur.execute(
                        """SELECT id FROM shuttle_legs 
                           WHERE user_id = %s 
                             AND leg_status = 'IN_TRANSIT'
                             AND (bol_number IS NULL OR bol_image IS NULL)
                             AND departure_time >= NOW() - INTERVAL 12 HOUR
                           ORDER BY id DESC LIMIT 1;""",
                        (did,)
                    )
                    open_leg_missing_bol = await cur.fetchone()

                    if open_leg_missing_bol and (bol_number or primary_image_blob):
                        leg_id = open_leg_missing_bol[0]
                        await cur.execute(
                            """UPDATE shuttle_legs 
                               SET bol_number = COALESCE(%s, bol_number),
                                   bol_image = COALESCE(%s, bol_image),
                                   trailer_number = IF(%s != 'UNKNOWN', %s, trailer_number)
                               WHERE id = %s;""",
                            (bol_number, primary_image_blob, display_trailer, display_trailer, leg_id)
                        )
                        await conn.commit()
                        logger.info(f"⚡ CASE 1 AUTO-HEAL: Attached missing BOL '{bol_number}' to active Leg #{leg_id}")
                        return {"is_clean": True, "leg_id": leg_id, "card_text": None}

                    # 2. NORMAL DEPARTURE
                    await cur.execute(
                        """SELECT id, trailer_number, destination_location FROM shuttle_legs 
                           WHERE user_id = %s 
                             AND destination_location NOT IN ('UNKNOWN', 'MISSING_ORIGIN', 'MISSING_DEST', '')
                           ORDER BY id DESC LIMIT 1;""",
                        (did,)
                    )
                    last_leg = await cur.fetchone()

                    if not origin_loc or origin_loc in ["NONE", "NULL", "MISSING_ORIGIN"]:
                        origin_loc = last_leg[2] if (last_leg and last_leg[2]) else (raw_orig.strip().upper() if raw_orig else "UNKNOWN")

                    if not dest_loc or dest_loc in ["NONE", "NULL", "MISSING_DEST"]:
                        dest_loc = raw_dest.strip().upper() if raw_dest else "UNKNOWN"

                    if not display_trailer or display_trailer == "UNKNOWN":
                        display_trailer = last_leg[1] if (last_leg and last_leg[1]) else "UNKNOWN"

                    # Complete prior active legs
                    await cur.execute(
                        """UPDATE shuttle_legs 
                           SET leg_status = 'COMPLETED', arrival_time = COALESCE(arrival_time, %s)
                           WHERE user_id = %s AND leg_status IN ('IN_TRANSIT', 'ARRIVED', 'UNLOADING', 'LOADING');""",
                        (msg_timestamp, did)
                    )

                    # Insert new departure leg
                    await cur.execute(
                        """INSERT INTO shuttle_legs (
                               user_id, trailer_number, bol_number, origin_location, destination_location, 
                               departure_time, arrival_action, bol_image, leg_status, load_status
                           ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'IN_TRANSIT', %s);""",
                        (did, display_trailer, bol_number, origin_loc, dest_loc, msg_timestamp, action_type, primary_image_blob, load_status_val)
                    )
                    await conn.commit()
                    leg_id = cur.lastrowid

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

                    # ALERT GUARD 2: Loaded Departure Missing BOL
                    if load_status_val == "LOADED" and not bol_number:
                        return {
                            "is_clean": False,
                            "leg_id": leg_id,
                            "card_text": (
                                f"⚠️ **MANUAL RECONCILE: Missing BOL for Loaded Departure**\n"
                                f"👤 Driver: {user_name}\n"
                                f"🚛 Trailer: `{display_trailer}`\n"
                                f"📍 Route: `{origin_loc}` ➔ `{dest_loc}` ({load_status_val})\n"
                                f"❓ Issue: Trip marked as LOADED but no BOL number was attached or parsed."
                            )
                        }

                    return {"is_clean": True, "leg_id": leg_id, "card_text": None}

                # =========================================================
                # CASE 2: DESTINATION ARRIVAL / DOCK MOVES
                # =========================================================
                case "CASE_2_DESTINATION_ARRIVAL":
                    await cur.execute(
                        """SELECT id, trailer_number, origin_location, destination_location, leg_status 
                           FROM shuttle_legs 
                           WHERE user_id = %s AND leg_status IN ('IN_TRANSIT', 'ARRIVED', 'UNLOADING', 'LOADING') 
                           ORDER BY id DESC LIMIT 1;""",
                        (did,)
                    )
                    active_leg = await cur.fetchone()

                    if action_type in ["UNLOAD_COMPLETED", "FINISHED_UNLOAD", "DROP"]:
                        target_status = "COMPLETED"
                    elif action_type == "LIVE_LOAD":
                        target_status = "LOADING"
                    elif action_type == "LIVE_UNLOAD":
                        target_status = "UNLOADING"
                    else:
                        target_status = "COMPLETED"

                    if active_leg:
                        leg_id = active_leg[0]
                        display_trailer = text_trailer if text_trailer and text_trailer != "UNKNOWN" else active_leg[1]

                        await cur.execute(
                            """UPDATE shuttle_legs 
                               SET arrival_time = COALESCE(arrival_time, %s), 
                                   arrival_action = %s,
                                   dock_number = COALESCE(NULLIF(%s, ''), dock_number),
                                   bol_number = COALESCE(%s, bol_number),
                                   bol_image = COALESCE(%s, bol_image), 
                                   receiver_signed = COALESCE(%s, receiver_signed),
                                   leg_status = %s,
                                   load_status = COALESCE(NULLIF(%s, 'UNKNOWN'), load_status)
                               WHERE id = %s;""",
                            (msg_timestamp, action_type, door_num, bol_number, primary_image_blob, receiver_signed, target_status, load_status_val, leg_id)
                        )
                        await conn.commit()
                        return {"is_clean": True, "leg_id": leg_id, "card_text": None}

                    # Previous leg was already completed -> Silently acknowledge without creating junk records
                    return {"is_clean": True, "leg_id": None, "card_text": None}

                # =========================================================
                # CASE 3: HISTORICAL BOL DOCUMENT ATTACHMENT
                # =========================================================
                case "CASE_HISTORICAL_BOL_UPDATE":
                    if not bol_number:
                        return {
                            "is_clean": False,
                            "leg_id": None,
                            "card_text": (
                                f"⚠️ **MANUAL ATTENTION REQUIRED: Unreadable Historical Document**\n"
                                f"👤 Driver: {user_name}\n"
                                f"💬 Message: `{raw_text}`\n"
                                f"❓ Issue: Document uploaded as historical paperwork but no valid BOL number could be extracted."
                            )
                        }

                    await cur.execute(
                        """SELECT id FROM shuttle_legs 
                           WHERE user_id = %s 
                             AND bol_number = %s 
                             AND departure_time >= NOW() - INTERVAL 48 HOUR
                           ORDER BY id DESC LIMIT 1;""",
                        (did, bol_number)
                    )
                    target_leg = await cur.fetchone()

                    if target_leg:
                        leg_id = target_leg[0]
                        await cur.execute(
                            """UPDATE shuttle_legs 
                               SET bol_image = COALESCE(%s, bol_image),
                                   receiver_signed = COALESCE(%s, receiver_signed)
                               WHERE id = %s;""",
                            (primary_image_blob, receiver_signed, leg_id)
                        )
                        await conn.commit()
                        logger.info(f"✅ Strict Match: Historical proof attached to Leg #{leg_id} for BOL '{bol_number}'.")
                        return {"is_clean": True, "leg_id": leg_id, "card_text": None}

                    return {
                        "is_clean": False,
                        "leg_id": None,
                        "card_text": (
                            f"⚠️ **MANUAL ATTENTION REQUIRED: Unmatched BOL Number**\n"
                            f"👤 Driver: {user_name}\n"
                            f"📄 Parsed BOL #: `{bol_number}`\n"
                            f"❓ Issue: Document parsed successfully, but no matching leg with BOL `{bol_number}` was found in database."
                        )
                    }

                # =========================================================
                # CASE AUTO RESOLVE: UNCAPTIONED PHOTO UPLINK
                # =========================================================
                case "CASE_AUTO_RESOLVE":
                    if not bol_number:
                        return {
                            "is_clean": False,
                            "leg_id": None,
                            "card_text": (
                                f"⚠️ **MANUAL RECONCILE: Unreadable Image Upload**\n"
                                f"👤 Driver: {user_name}\n"
                                f"❓ Action Needed: Photo uploaded without text, and no clear BOL # could be detected."
                            )
                        }

                    # Target active leg (IN_TRANSIT, LOADING, ARRIVED within 12 hours)
                    await cur.execute(
                        """SELECT id FROM shuttle_legs 
                           WHERE user_id = %s 
                             AND leg_status IN ('IN_TRANSIT', 'LOADING', 'ARRIVED', 'UNLOADING')
                             AND departure_time >= NOW() - INTERVAL 12 HOUR
                           ORDER BY id DESC LIMIT 1;""",
                        (did,)
                    )
                    active_leg = await cur.fetchone()

                    if active_leg:
                        leg_id = active_leg[0]
                        await cur.execute(
                            """UPDATE shuttle_legs 
                               SET bol_number = %s,
                                   bol_image = COALESCE(%s, bol_image),
                                   receiver_signed = COALESCE(%s, receiver_signed)
                               WHERE id = %s;""",
                            (bol_number, primary_image_blob, receiver_signed, leg_id)
                        )
                        await conn.commit()
                        logger.info(f"⚡ Silent Auto-Link: Attached BOL '{bol_number}' to active Leg #{leg_id}")
                        return {"is_clean": True, "leg_id": leg_id, "card_text": None}  # SILENT SUCCESS

                    return {
                        "is_clean": False,
                        "leg_id": None,
                        "card_text": (
                            f"⚠️ **MANUAL RECONCILE: No Open Leg Found**\n"
                            f"👤 Driver: {user_name}\n"
                            f"📄 BOL #: `{bol_number}`\n"
                            f"❓ Action Needed: Valid BOL scanned, but no active trip leg was found for this driver in the last 12 hours."
                        )
                    }

                case _:
                    return {"is_clean": False, "card_text": None}