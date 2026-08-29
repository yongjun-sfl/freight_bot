import logging
import re
from config import TABLE_DRIVERS, TABLE_SHUTTLE_LEGS
from ai_engine import normalize_location

logger = logging.getLogger(__name__)

# Word-boundary matched: a bare `"bt" in text` substring test fires on ordinary
# words like "doubt", "debt" and "subtotal", which forced load_status to EMPTY
# and silently skipped the outbound BOL compliance guard.
BOBTAIL_PATTERN = re.compile(
    r"\b(?:bobtail|bob\s*tail|bt|b/t|no\s+trailer|single\s+tractor|tractor\s+only)\b",
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
    text_trailer = intent.get("trailer_number") or intent.get("text_trailer")
    ocr_trailer = intent.get("ocr_trailer")
    bol_number = intent.get("bol_number")
    document_type = intent.get("document_type", "UNKNOWN")
    action_type = intent.get("action")
    door_num = intent.get("door_number")
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
                logger.warning(f"Unauthorized update attempt by Telegram User ID #{did} ({user_name}).")
                return {"is_clean": False, "leg_id": None, "card_text": None}

            # 1. FILTER IN-FACILITY MOVEMENTS
            if origin_loc != "UNKNOWN" and dest_loc != "UNKNOWN" and origin_loc == dest_loc:
                logger.info(f"Ignored in-facility dock move for Driver #{did} at {origin_loc}.")
                return {"is_clean": True, "leg_id": None, "card_text": None}

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

            match case_type:

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
                                       trailer_number = IF(%s != 'UNKNOWN', %s, trailer_number)
                                 WHERE id = %s;""",
                            (bol_number, document_type, document_type, primary_image_blob, display_trailer, display_trailer, leg_id)
                        )
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

                    # Complete prior active legs upon new departure
                    await cur.execute(
                        f"""UPDATE {TABLE_SHUTTLE_LEGS} 
                               SET leg_status = 'COMPLETED', 
                                   arrival_time = COALESCE(arrival_time, %s)
                             WHERE user_id = %s 
                               AND leg_status IN ('IN_TRANSIT', 'ARRIVED', 'UNLOADING', 'LOADING');""",
                        (msg_timestamp, did)
                    )

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
                               load_status
                           ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'IN_TRANSIT', %s);""",
                        (
                            did, display_trailer, bol_number, document_type, origin_loc, dest_loc,
                            msg_timestamp, action_type, primary_image_blob, door_num,
                            1 if shipper_signed else 0, is_bobtail_flag, load_status_val
                        )
                    )
                    await conn.commit()
                    leg_id = cur.lastrowid

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
                                   is_bobtail 
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
                                       dock_number = COALESCE(%s, dock_number),
                                       receiver_signed = COALESCE(%s, receiver_signed),
                                       leg_status = %s
                                 WHERE id = %s;""",
                            (
                                msg_timestamp, 
                                resolved_action, 
                                door_num, 
                                # None, not 0: COALESCE must fall through to the
                                # stored value when no signature was detected,
                                # otherwise an arrival wipes a POD captured earlier.
                                1 if receiver_signed else None, 
                                target_status, 
                                leg_id
                            )
                        )
                        await conn.commit()
                        logger.info(f"✅ Driver #{did} arrived at {dest_loc}. Leg #{leg_id} updated to {target_status}.")
                        return {"is_clean": True, "leg_id": leg_id, "card_text": None}

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
                                       shipper_signed = COALESCE(%s, shipper_signed),
                                       receiver_signed = COALESCE(%s, receiver_signed)
                                 WHERE id = %s;""",
                            (
                                primary_image_blob,
                                document_type, document_type,
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
                                       shipper_signed = COALESCE(%s, shipper_signed),
                                       receiver_signed = COALESCE(%s, receiver_signed)
                                 WHERE id = %s;""",
                            (
                                bol_number,
                                document_type, document_type,
                                primary_image_blob,
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