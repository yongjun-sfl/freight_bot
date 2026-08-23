import os
import io
import time
import asyncio
import logging
import collections
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from ai_engine import extract_bol_locally, parse_text_with_llm

logger = logging.getLogger(__name__)

# State Buffers
ALBUM_BUFFERS = {}
ALBUM_LOCKS = set()
PROCESSED_GROUPS = set()

# Driver-Isolated FIFO Caches & Signal Events using User ID (did)
USER_TEXT_QUEUES = collections.defaultdict(collections.deque)
PENDING_IMAGE_QUEUE = {}

DISPATCH_CHANNEL_ID = os.getenv("DISPATCH_CHANNEL_ID")


async def delete_msg_after_delay(bot, chat_id: int, message_id: int, delay_seconds: int = 60):
    await asyncio.sleep(delay_seconds)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
        logger.info(f"Auto-deleted dispatch message {message_id} from chat {chat_id}.")
    except Exception as e:
        logger.warning(f"Failed to auto-delete message {message_id}: {e}")


def get_telegram_link(chat_id: int, message_id: int) -> str:
    str_chat_id = str(chat_id)
    if str_chat_id.startswith("-100"):
        clean_chat_id = str_chat_id[4:]
        return f"https://t.me/c/{clean_chat_id}/{message_id}"
    return ""


def get_manual_reconcile_keyboard(leg_id: int, orig_chat_id: int = None, orig_msg_id: int = None):
    keyboard = []
    
    if orig_chat_id and orig_msg_id:
        jump_url = get_telegram_link(orig_chat_id, orig_msg_id)
        if jump_url:
            keyboard.append([InlineKeyboardButton("🔗 View Original Message in Group", url=jump_url)])

    keyboard.extend([
        [
            InlineKeyboardButton("✅ Mark Closed", callback_data=f"rec_close_{leg_id}"),
            InlineKeyboardButton("🚛 Fix Trailer", callback_data=f"rec_trailer_{leg_id}")
        ],
        [
            InlineKeyboardButton("📍 Fix Route", callback_data=f"rec_route_{leg_id}"),
            InlineKeyboardButton("📄 Fix BOL", callback_data=f"rec_bol_{leg_id}")
        ]
    ])
    return InlineKeyboardMarkup(keyboard)


async def handle_reconcile_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    p = context.application.bot_data['db_pool']

    if not data or not data.startswith("rec_"):
        return

    parts = data.split("_")
    action = parts[1]
    leg_id = int(parts[2])

    async with p.acquire() as conn:
        async with conn.cursor() as cur:
            if action == "close":
                sql = "UPDATE shuttle_legs SET leg_status = 'COMPLETED', arrival_time = NOW() WHERE id = %s;"
                await cur.execute(sql, (leg_id,))
                await conn.commit()
                
                new_text = query.message.text + "\n\n🛠️ Manually Closed & Resolved by Dispatcher"
                await query.edit_message_text(text=new_text, parse_mode=None, reply_markup=None)

                asyncio.create_task(
                    delete_msg_after_delay(context.bot, query.message.chat_id, query.message.message_id, 60)
                )

            elif action in ["trailer", "route", "bol"]:
                prompt_text = (
                    f"⚠️ Manual Override Request (Leg #{leg_id})\n"
                    f"Reply in this channel to edit: {action.upper()}: <value>"
                )
                await context.bot.send_message(chat_id=query.message.chat_id, text=prompt_text, parse_mode=None)


# --- TEXT MESSAGE HANDLER ---
async def handle_shuttle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    did = update.message.from_user.id
    text = update.message.text
    orig_chat_id = update.effective_chat.id
    orig_msg_id = update.message.message_id
    now = time.time()

    llm_parsed = parse_text_with_llm(text)

    if llm_parsed.get("case_type") == "NONE_WORK_RELATED":
        logger.info(f"Ignored non-work message from driver {did}: '{text}'")
        return

    if did in PENDING_IMAGE_QUEUE:
        PENDING_IMAGE_QUEUE[did]["text"] = text
        PENDING_IMAGE_QUEUE[did]["orig_chat_id"] = orig_chat_id
        PENDING_IMAGE_QUEUE[did]["orig_msg_id"] = orig_msg_id
        PENDING_IMAGE_QUEUE[did]["event"].set()
        return

    USER_TEXT_QUEUES[did].append((text, now, orig_chat_id, orig_msg_id))
    user_name = update.message.from_user.full_name or f"Driver {did}"
    group_title = update.effective_chat.title or "Driver Group"
    msg_timestamp = update.message.date.strftime('%Y-%m-%d %H:%M:%S')
    p = context.application.bot_data['db_pool']

    output_text = ""
    leg_id = None
    is_completed = False

    async with p.acquire() as conn:
        async with conn.cursor() as cur:

            # DYNAMIC MESSAGE EDIT FOR OPEN PHOTO LEGS
            if llm_parsed.get("origin_location") != "INFER_FROM_HISTORY" or llm_parsed.get("destination_location") != "INFER_FROM_HISTORY":
                new_orig = llm_parsed.get("origin_location")
                new_dest = llm_parsed.get("destination_location")

                find_unresolved_sql = """
                    SELECT id, origin_location, destination_location, trailer_number, bol_number, dispatch_msg_id 
                    FROM shuttle_legs 
                    WHERE user_id = %s AND leg_status = 'IN_TRANSIT' 
                    ORDER BY id DESC LIMIT 1;
                """
                await cur.execute(find_unresolved_sql, (did,))
                unresolved_leg = await cur.fetchone()

                if unresolved_leg and (unresolved_leg[1] in ["Origin", "INFER_FROM_HISTORY", "UNKNOWN"] or unresolved_leg[2] in ["Destination", "INFER_FROM_HISTORY", "UNKNOWN"]):
                    leg_id = unresolved_leg[0]
                    curr_orig = unresolved_leg[1]
                    curr_dest = unresolved_leg[2]
                    curr_trailer = unresolved_leg[3]
                    curr_bol = unresolved_leg[4]
                    dispatch_msg_id = unresolved_leg[5]

                    final_orig = new_orig if new_orig and new_orig != "INFER_FROM_HISTORY" else curr_orig
                    final_dest = new_dest if new_dest and new_dest != "INFER_FROM_HISTORY" else curr_dest

                    update_backfill_sql = "UPDATE shuttle_legs SET origin_location = %s, destination_location = %s WHERE id = %s;"
                    await cur.execute(update_backfill_sql, (final_orig, final_dest, leg_id))
                    await conn.commit()

                    # Dynamically edit the existing Telegram message card in the dispatch channel
                    if dispatch_msg_id:
                        target_chat_id = DISPATCH_CHANNEL_ID if DISPATCH_CHANNEL_ID else update.effective_chat.id
                        updated_card_text = (
                            f"📸 **Case #1: Outbound Leg Logged**\n"
                            f"👤 Driver: {user_name}\n"
                            f"💬 Group: `{group_title}` (Msg ID: `{orig_msg_id}`)\n"
                            f"🚛 Trailer: `{curr_trailer}` | BOL: `{curr_bol or 'N/A'}`\n"
                            f"📍 Route: `{final_orig}` ➔ `{final_dest}`\n"
                            f"⏱️ Departure: `{msg_timestamp}`"
                        )
                        keyboard = get_manual_reconcile_keyboard(leg_id, orig_chat_id, orig_msg_id)
                        try:
                            await context.bot.edit_message_text(
                                chat_id=target_chat_id,
                                message_id=dispatch_msg_id,
                                text=updated_card_text,
                                parse_mode="Markdown",
                                reply_markup=keyboard
                            )
                            logger.info(f"Edited dispatch message {dispatch_msg_id} with updated route {final_orig} ➔ {final_dest}.")
                        except Exception as e:
                            logger.error(f"Failed to edit dispatch message {dispatch_msg_id}: {e}")
                    return

            # DESTINATION ARRIVAL EXECUTION
            elif llm_parsed.get("case_type") == "CASE_2_DESTINATION_ARRIVAL":
                target_trailer = llm_parsed.get("trailer_number")
                
                if not target_trailer or target_trailer in ["UNKNOWN", "None", "null", ""]:
                    await cur.execute(
                        "SELECT id, trailer_number FROM shuttle_legs WHERE user_id = %s AND leg_status = 'IN_TRANSIT' ORDER BY id DESC LIMIT 1;", 
                        (did,)
                    )
                    leg_row = await cur.fetchone()
                    if leg_row:
                        leg_id = leg_row[0]
                        target_trailer = leg_row[1]

                if leg_id:
                    sql = """
                        UPDATE shuttle_legs AS sl 
                        SET sl.arrival_time = %s, 
                            sl.arrival_action = COALESCE(NULLIF(%s, ''), sl.arrival_action, 'DROP'), 
                            sl.dock_number = COALESCE(NULLIF(%s, ''), sl.dock_number), 
                            sl.destination_location = COALESCE(NULLIF(%s, 'Destination'), sl.destination_location), 
                            sl.leg_status = 'COMPLETED'
                        WHERE sl.id = %s;
                    """
                    await cur.execute(sql, (msg_timestamp, llm_parsed.get("action"), llm_parsed.get("door_number"), llm_parsed.get("destination_location"), leg_id))
                else:
                    sql = """
                        UPDATE shuttle_legs AS sl 
                        SET sl.arrival_time = %s, 
                            sl.arrival_action = COALESCE(NULLIF(%s, ''), sl.arrival_action, 'DROP'), 
                            sl.dock_number = COALESCE(NULLIF(%s, ''), sl.dock_number), 
                            sl.destination_location = COALESCE(NULLIF(%s, 'Destination'), sl.destination_location), 
                            sl.leg_status = 'COMPLETED'
                        WHERE sl.user_id = %s AND sl.leg_status = 'IN_TRANSIT'
                        ORDER BY sl.id DESC LIMIT 1;
                    """
                    await cur.execute(sql, (msg_timestamp, llm_parsed.get("action"), llm_parsed.get("door_number"), llm_parsed.get("destination_location"), did))

                await conn.commit()
                is_completed = True
                door_str = f" at Door #{llm_parsed.get('door_number')}" if llm_parsed.get('door_number') else ""
                display_trailer = target_trailer if target_trailer else "Active Unit"
                
                output_text = (
                    f"📍 **Destination Arrival Logged**\n"
                    f"👤 Driver: {user_name}\n"
                    f"💬 Source Chat: `{group_title}` (Msg ID: `{orig_msg_id}`)\n"
                    f"🚛 Trailer: `{display_trailer}`{door_str}\n"
                    f"📍 Location: `{llm_parsed.get('destination_location') or 'Destination'}`\n"
                    f"⏱️ Time: `{msg_timestamp}`"
                )

    if output_text:
        target_chat_id = DISPATCH_CHANNEL_ID if DISPATCH_CHANNEL_ID else update.effective_chat.id
        try:
            keyboard = None if is_completed else get_manual_reconcile_keyboard(leg_id, orig_chat_id, orig_msg_id)
            
            sent_msg = await context.bot.send_message(
                chat_id=target_chat_id, 
                text=output_text, 
                parse_mode="Markdown",
                reply_markup=keyboard
            )

            if is_completed:
                asyncio.create_task(delete_msg_after_delay(context.bot, target_chat_id, sent_msg.message_id, 60))

        except Exception as e:
            logger.error(f"Failed to post text log: {e}")


# --- PHOTO & ALBUM HANDLER ---
async def handle_photo_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.photo:
        return

    media_group_id = msg.media_group_id

    if media_group_id and media_group_id in PROCESSED_GROUPS:
        return

    file = await msg.photo[-1].get_file()
    buf = io.BytesIO()
    await file.download_to_memory(buf)
    img_bytes = buf.getvalue()

    did = msg.from_user.id
    caption_text = msg.caption or ""
    orig_chat_id = update.effective_chat.id
    orig_msg_id = msg.message_id

    target_chat_id = DISPATCH_CHANNEL_ID if DISPATCH_CHANNEL_ID else update.effective_chat.id

    if not media_group_id:
        status_msg = await context.bot.send_message(chat_id=target_chat_id, text="⏳ Processing image...")
        await handle_image_completion(update, context, [img_bytes], caption_text, status_msg, orig_chat_id, orig_msg_id)
        return

    if media_group_id not in ALBUM_BUFFERS:
        ALBUM_BUFFERS[media_group_id] = {
            "images": [],
            "caption": "",
            "status_msg": None,
            "orig_chat_id": orig_chat_id,
            "orig_msg_id": orig_msg_id
        }

    ALBUM_BUFFERS[media_group_id]["images"].append(img_bytes)
    if caption_text and not ALBUM_BUFFERS[media_group_id]["caption"]:
        ALBUM_BUFFERS[media_group_id]["caption"] = caption_text

    if media_group_id in ALBUM_LOCKS:
        return

    ALBUM_LOCKS.add(media_group_id)
    status_msg = await context.bot.send_message(chat_id=target_chat_id, text="⏳ Processing photo album...")
    ALBUM_BUFFERS[media_group_id]["status_msg"] = status_msg

    await asyncio.sleep(2.5)

    PROCESSED_GROUPS.add(media_group_id)
    album_data = ALBUM_BUFFERS.pop(media_group_id, None)
    ALBUM_LOCKS.remove(media_group_id)

    if album_data:
        await handle_image_completion(
            update, 
            context, 
            album_data["images"], 
            album_data["caption"], 
            album_data["status_msg"],
            album_data["orig_chat_id"],
            album_data["orig_msg_id"]
        )

    if len(PROCESSED_GROUPS) > 1000:
        PROCESSED_GROUPS.clear()


# --- SYNCHRONIZED HOLD QUEUE ---
async def handle_image_completion(update: Update, context: ContextTypes.DEFAULT_TYPE, images: list[bytes], caption_text: str, status_msg, orig_chat_id: int, orig_msg_id: int):
    did = update.message.from_user.id

    if not caption_text and USER_TEXT_QUEUES[did]:
        while USER_TEXT_QUEUES[did]:
            item = USER_TEXT_QUEUES[did].popleft()
            cached_text, cached_time = item[0], item[1]
            if time.time() - cached_time < 180:
                caption_text = cached_text
                if len(item) >= 4:
                    orig_chat_id, orig_msg_id = item[2], item[3]
                break

    if not caption_text:
        event = asyncio.Event()
        PENDING_IMAGE_QUEUE[did] = {
            "event": event, 
            "text": "", 
            "orig_chat_id": orig_chat_id, 
            "orig_msg_id": orig_msg_id
        }
        
        try:
            await asyncio.wait_for(event.wait(), timeout=10.0)
            caption_text = PENDING_IMAGE_QUEUE[did].get("text", "")
            orig_chat_id = PENDING_IMAGE_QUEUE[did].get("orig_chat_id", orig_chat_id)
            orig_msg_id = PENDING_IMAGE_QUEUE[did].get("orig_msg_id", orig_msg_id)
        except asyncio.TimeoutError:
            logger.info(f"Driver {did} uploaded photos without caption within 10s window. Proceeding to fallback.")
        finally:
            PENDING_IMAGE_QUEUE.pop(did, None)

    await process_photo_batch(update, context, images, caption_text, status_msg, orig_chat_id, orig_msg_id)


# --- BATCH IMAGE & DATABASE ENGINE ---
async def process_photo_batch(update: Update, context: ContextTypes.DEFAULT_TYPE, images: list[bytes], caption_text: str, status_msg, orig_chat_id: int, orig_msg_id: int):
    did = update.message.from_user.id
    user_name = update.message.from_user.full_name or f"Driver {did}"
    group_title = update.effective_chat.title or "Driver Group"
    msg_timestamp = update.message.date.strftime('%Y-%m-%d %H:%M:%S')
    p = context.application.bot_data['db_pool']

    llm_text_data = parse_text_with_llm(caption_text) if caption_text else {}
    
    if llm_text_data.get("case_type") == "NONE_WORK_RELATED":
        if status_msg:
            try: await status_msg.delete()
            except Exception: pass
        return

    loop = asyncio.get_running_loop()
    ext = await loop.run_in_executor(None, extract_bol_locally, images)

    bol_num = ext.get("bol_number")
    trailer_num = llm_text_data.get("trailer_number") or ext.get("trailer_number") or "UNKNOWN"
    primary_image_blob = images[0] if images else None

    # HISTORICAL / DELAYED SIGNED BOL UPLOADS
    if llm_text_data.get("case_type") == "CASE_HISTORICAL_BOL_UPDATE":
        async with p.acquire() as conn:
            async with conn.cursor() as cur:
                leg_id = None
                
                if bol_num:
                    await cur.execute("SELECT id, origin_location, destination_location, trailer_number FROM shuttle_legs WHERE bol_number = %s ORDER BY id DESC LIMIT 1;", (bol_num,))
                    match = await cur.fetchone()
                    if match:
                        leg_id, origin_loc, dest_loc, trailer_num = match[0], match[1], match[2], match[3]

                if not leg_id and trailer_num != "UNKNOWN":
                    await cur.execute("SELECT id, origin_location, destination_location FROM shuttle_legs WHERE user_id = %s AND trailer_number = %s ORDER BY id DESC LIMIT 1;", (did, trailer_num))
                    match = await cur.fetchone()
                    if match:
                        leg_id, origin_loc, dest_loc = match[0], match[1], match[2]

                if leg_id:
                    is_signed = ext.get("receiver_signed") or True
                    update_sql = """
                        UPDATE shuttle_legs 
                        SET bol_image = COALESCE(%s, bol_image),
                            receiver_signed = %s,
                            leg_status = 'COMPLETED'
                        WHERE id = %s;
                    """
                    await cur.execute(update_sql, (primary_image_blob, is_signed, leg_id))
                    await conn.commit()

                    output_text = (
                        f"📑 **Historical BOL Reconciled (Past Leg #{leg_id})**\n"
                        f"👤 Driver: {user_name}\n"
                        f"💬 Group: `{group_title}` (Msg ID: `{orig_msg_id}`)\n"
                        f"🚛 Trailer: `{trailer_num}` | BOL: `{bol_num or 'Attached'}`\n"
                        f"📍 Route: `{origin_loc}` ➔ `{dest_loc}`\n"
                        f"✅ Status: Closed & Reconciled via Paper Upload"
                    )
                    
                    target_chat_id = DISPATCH_CHANNEL_ID if DISPATCH_CHANNEL_ID else update.effective_chat.id
                    sent_msg = await context.bot.send_message(chat_id=target_chat_id, text=output_text, parse_mode="Markdown")
                    asyncio.create_task(delete_msg_after_delay(context.bot, target_chat_id, sent_msg.message_id, 60))
                    
                    if status_msg:
                        try: await status_msg.delete()
                        except Exception: pass
                    return

    # ACTIVE MOVEMENT LOGIC
    raw_origin = (llm_text_data.get("origin_location") or "").strip()
    raw_dest = (llm_text_data.get("destination_location") or "").strip()

    origin_loc = raw_origin if raw_origin and raw_origin not in ["None", "null", "", "INFER_FROM_HISTORY"] else "INFER_FROM_HISTORY"
    dest_loc = raw_dest if raw_dest and raw_dest not in ["None", "null", "", "INFER_FROM_HISTORY"] else "INFER_FROM_HISTORY"
    load_status = llm_text_data.get("load_status") or "LOADED"

    output_text = ""
    leg_id = None
    is_closed = False

    async with p.acquire() as conn:
        async with conn.cursor() as cur:

            if origin_loc in ["INFER_FROM_HISTORY", "Origin", "UNKNOWN"]:
                completed_leg_sql = """
                    SELECT destination_location FROM shuttle_legs 
                    WHERE user_id = %s AND destination_location NOT IN ('Destination', 'UNKNOWN', 'INFER_FROM_HISTORY', '')
                    ORDER BY id DESC LIMIT 1;
                """
                await cur.execute(completed_leg_sql, (did,))
                last_completed = await cur.fetchone()
                origin_loc = last_completed[0] if (last_completed and last_completed[0]) else "Origin"

            if dest_loc in ["INFER_FROM_HISTORY", "Destination", "UNKNOWN"]:
                active_leg_sql = """
                    SELECT destination_location FROM shuttle_legs 
                    WHERE user_id = %s AND leg_status = 'IN_TRANSIT' AND destination_location NOT IN ('Destination', 'UNKNOWN', '')
                    ORDER BY id DESC LIMIT 1;
                """
                await cur.execute(active_leg_sql, (did,))
                active_leg = await cur.fetchone()

                if active_leg and active_leg[0] and active_leg[0] != origin_loc:
                    dest_loc = active_leg[0]
                else:
                    prev_origin_sql = """
                        SELECT origin_location FROM shuttle_legs 
                        WHERE user_id = %s AND origin_location NOT IN ('Origin', 'UNKNOWN', '', %s)
                        ORDER BY id DESC LIMIT 1;
                    """
                    await cur.execute(prev_origin_sql, (did, origin_loc))
                    prev_leg = await cur.fetchone()
                    dest_loc = prev_leg[0] if (prev_leg and prev_leg[0]) else "Destination"

            if origin_loc == dest_loc and origin_loc not in ["Origin", "Destination", "UNKNOWN"]:
                dest_loc = "Destination"
            
            # Match existing leg
            if bol_num:
                await cur.execute("SELECT id FROM shuttle_legs WHERE bol_number = %s ORDER BY id DESC LIMIT 1;", (bol_num,))
                match = await cur.fetchone()
                if match:
                    leg_id = match[0]

            if not leg_id and trailer_num != "UNKNOWN":
                await cur.execute(
                    "SELECT id FROM shuttle_legs WHERE user_id = %s AND trailer_number = %s AND leg_status = 'IN_TRANSIT' ORDER BY id DESC LIMIT 1;",
                    (did, trailer_num)
                )
                match = await cur.fetchone()
                if match:
                    leg_id = match[0]

            if not leg_id and trailer_num == "UNKNOWN":
                await cur.execute(
                    "SELECT id, trailer_number FROM shuttle_legs WHERE user_id = %s AND leg_status = 'IN_TRANSIT' ORDER BY id DESC LIMIT 1;",
                    (did,)
                )
                match = await cur.fetchone()
                if match:
                    leg_id = match[0]
                    if match[1] and match[1] != "UNKNOWN":
                        trailer_num = match[1]

            # RECONCILE EXISTING LEG
            if leg_id:
                is_receiver_signed = ext.get("receiver_signed") or False
                is_closed = is_receiver_signed
                
                update_sql = """
                    UPDATE shuttle_legs AS sl
                    SET sl.bol_number = COALESCE(%s, sl.bol_number),
                        sl.bol_image = COALESCE(%s, sl.bol_image),
                        sl.shipper_signed = COALESCE(%s, sl.shipper_signed),
                        sl.receiver_signed = COALESCE(%s, sl.receiver_signed),
                        sl.arrival_time = IF(%s = TRUE, %s, sl.arrival_time),
                        sl.leg_status = IF(%s = TRUE, 'COMPLETED', sl.leg_status)
                    WHERE sl.id = %s;
                """
                await cur.execute(update_sql, (
                    bol_num,
                    primary_image_blob,
                    ext.get("shipper_signed"),
                    is_receiver_signed,
                    is_receiver_signed,
                    msg_timestamp,
                    is_receiver_signed,
                    leg_id
                ))
                await conn.commit()

                status_label = "✅ Closed & Reconciled (Receiver Signed)" if is_receiver_signed else "⚠️ Action Required / Open Leg"
                output_text = (
                    f"📑 **{status_label}**\n"
                    f"👤 Driver: {user_name}\n"
                    f"💬 Group: `{group_title}` (Msg ID: `{orig_msg_id}`)\n"
                    f"🚛 Trailer: `{trailer_num}` | BOL: `{bol_num or 'Attached'}`\n"
                    f"📍 Route: `{origin_loc}` ➔ `{dest_loc}`\n"
                    f"⏱️ Timestamp: `{msg_timestamp}`"
                )

            # --- INSIDE process_photo_batch (bot/handlers.py) ---

            # NEW OUTBOUND DEPARTURE LEG
            else:
                insert_sql = """
                    INSERT INTO shuttle_legs (
                        user_id, trailer_number, bol_number, bol_image, shipper_signed, receiver_signed,
                        load_status, origin_location, destination_location, departure_time, leg_status
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'IN_TRANSIT');
                """
                await cur.execute(insert_sql, (
                    did, 
                    trailer_num, 
                    bol_num, 
                    primary_image_blob, 
                    ext.get("shipper_signed"), 
                    ext.get("receiver_signed"),
                    load_status,
                    origin_loc, 
                    dest_loc, 
                    msg_timestamp
                ))
                await conn.commit()
                leg_id = cur.lastrowid

                # CHECK IF DATA IS CLEAN AND COMPLETE
                has_valid_locations = (
                    origin_loc not in ["Origin", "UNKNOWN", "INFER_FROM_HISTORY", ""] and 
                    dest_loc not in ["Destination", "UNKNOWN", "INFER_FROM_HISTORY", ""]
                )
                has_valid_trailer = trailer_num not in ["UNKNOWN", "None", "null", ""]

                # Auto-close/auto-delete message if all movement data was captured cleanly
                is_closed = (has_valid_locations and has_valid_trailer)

                output_text = (
                    f"📸 **Case #1: Outbound Leg Logged**\n"
                    f"👤 Driver: {user_name}\n"
                    f"💬 Group: `{group_title}` (Msg ID: `{orig_msg_id}`)\n"
                    f"🚛 Trailer: `{trailer_num}` | BOL: `{bol_num or 'N/A'}`\n"
                    f"📍 Route: `{origin_loc}` ➔ `{dest_loc}`\n"
                    f"⏱️ Departure: `{msg_timestamp}`"
                )

    if status_msg:
        try: await status_msg.delete()
        except Exception: pass

    target_chat_id = DISPATCH_CHANNEL_ID if DISPATCH_CHANNEL_ID else update.effective_chat.id

    try:
        keyboard = None if is_closed else get_manual_reconcile_keyboard(leg_id, orig_chat_id, orig_msg_id)

        sent_msg = await context.bot.send_message(
            chat_id=target_chat_id, 
            text=output_text, 
            parse_mode="Markdown",
            reply_markup=keyboard
        )

        # Store dispatch_msg_id in database for dynamic live edits
        async with p.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("UPDATE shuttle_legs SET dispatch_msg_id = %s WHERE id = %s;", (sent_msg.message_id, leg_id))
                await conn.commit()

        if is_closed:
            asyncio.create_task(delete_msg_after_delay(context.bot, target_chat_id, sent_msg.message_id, 60))

    except Exception as e:
        logger.error(f"Failed to post log to target {target_chat_id}: {e}")