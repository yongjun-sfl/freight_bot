import os, io, logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ForceReply
from telegram.ext import ContextTypes
from ai_engine import clean_text_locally, extract_bol_locally
import asyncio

from datetime import datetime, timezone
# Optional: If you want to convert the timestamp to a specific local time zone (e.g., Eastern Time)
from zoneinfo import ZoneInfo 

logger = logging.getLogger(__name__)

ADMIN_IDS = {int(x.strip()) for x in os.getenv('ADMIN_IDS', '').split(',') if x.strip()}

PROCESSED_GROUPS = set()

def is_admin(uid: int) -> bool: 
    return uid in ADMIN_IDS

async def ensure_driver_exists(conn, user_id: int, driver_name: str):
    """Ensures driver is present in the drivers table prior to inserting message records."""
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO drivers (user_id, driver_name) VALUES (%s, %s) "
            "ON DUPLICATE KEY UPDATE driver_name=VALUES(driver_name)",
            (user_id, driver_name)
        )
    await conn.commit()


async def handle_incoming_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sid = update.message.from_user.id
    raw_text = update.message.text
    chat_type = update.message.chat.type

    if chat_type in ["group", "supergroup"]:
        # 1. Run local AI instantly on the group message background thread
        parsed = clean_text_locally(raw_text)
        p = context.application.bot_data['db_pool']
        
        # Determine if message implies an ARRIVAL or DEPARTURE
        text_lower = raw_text.lower()
        is_arrival = any(w in text_lower for w in ["arr", "arrive", "delivered", "done", "at yard", "empty"])

        if is_arrival:
            # AUTOMATIC ARRIVAL: Find open trip and close it
            async with p.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT id FROM messages WHERE user_id=%s AND status='active' ORDER BY id DESC LIMIT 1", (sid,))
                    row = await cur.fetchone()
                    if row:
                        rid = row[0]
                        await cur.execute("UPDATE messages SET arrival_time=%s, status='arrived' WHERE id=%s", (parsed.get("time_info") or raw_text, rid))
                        # Only notify admin privately of successful automatic stitch
                        for aid in ADMIN_IDS:
                            await context.bot.send_message(chat_id=aid, text=f"✅ **Auto-Linked Arrival** for Driver {sid} to Trip #{rid}")
                        return

        # AUTOMATIC DEPARTURE (Default fallback if not an explicit arrival)
        async with p.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO messages (user_id, original_text, origin, destination, departure_time, status) VALUES (%s,%s,%s,%s,%s,'active')", 
                    (sid, raw_text, parsed.get("origin"), parsed.get("destination"), parsed.get("time_info"))
                )
                rid = cur.lastrowid

        # 🚨 ONLY POP UP TO ADMIN IF AI FAILS CRITICAL FIELDS
        if not parsed.get("origin") or not parsed.get("destination"):
            for aid in ADMIN_IDS:
                kb = [[InlineKeyboardButton("✏️ Fix Route Manually", callback_data=f"fixdep_{rid}")]]
                await context.bot.send_message(
                    chat_id=aid, 
                    text=f"⚠️ **Auto-Logged Trip #{rid} (Driver {sid}) but AI missed fields!**\n💬 *\"{raw_text}\"*", 
                    reply_markup=InlineKeyboardMarkup(kb)
                )


from datetime import datetime, timezone
# Optional: If you want to convert the timestamp to a specific local time zone (e.g., Eastern Time)
from zoneinfo import ZoneInfo 

async def handle_pipeline_routing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id): return
    await q.answer()
    act, did = q.data.split("_")
    txt = context.bot_data.get(f"raw_text_{did}", "")
    parsed = clean_text_locally(txt)
    p = context.application.bot_data['db_pool']

    # ⏱️ 1. Extract the native Telegram message time (Defaults to UTC)
    # If using a group chat webhook forwarding flow, fall back to current query message date context
    msg_date = update.message.date if update.message else q.message.date
    
    # ⏱️ 2. Convert to your local fleet timezone (e.g., America/New_York for Eastern Time)
    local_msg_date = msg_date.astimezone(ZoneInfo("America/New_York"))
    telegram_time_str = local_msg_date.strftime("%m/%d %H:%M")

    if act == "dep":
        # Use the AI's extracted text time if found; otherwise, fall back to the Telegram message time string
        time_val = parsed.get("time_info") or telegram_time_str
        
        async with p.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO messages (user_id, original_text, origin, destination, departure_time, status) "
                    "VALUES (%s,%s,%s,%s,%s,'active')", 
                    (int(did), txt, parsed.get("origin"), parsed.get("destination"), time_val)
                )
                rid = cur.lastrowid
        kb = [[InlineKeyboardButton("✏️ Fix Dep", callback_data=f"fixdep_{rid}")]]
        await q.edit_message_text(f"✅ Dep Logged! (ID: {rid})", reply_markup=InlineKeyboardMarkup(kb))

    elif act == "arr":
        # ✅ Use the AI's extracted text time if found; otherwise, fall back to the Telegram message time string
        time_val = parsed.get("time_info") or telegram_time_str
        
        async with p.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id FROM messages WHERE user_id=%s AND status='active' ORDER BY id DESC LIMIT 1", 
                    (int(did),)
                )
                row = await cur.fetchone()
                if not row:
                    await q.edit_message_text("❌ Error: No active open trip records found for driver.")
                    return
                rid = row[0]
                # Save the clean message timestamp string into your arrival_time column
                await cur.execute("UPDATE messages SET arrival_time=%s, status='arrived' WHERE id=%s", (time_val, rid))
                
        kb = [[InlineKeyboardButton("✏️ Fix Arr", callback_data=f"fixarr_{rid}")]]
        await q.edit_message_text(f"✅ Arr Linked to Trip #{rid}!", reply_markup=InlineKeyboardMarkup(kb))


async def handle_photo_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    did = update.message.from_user.id
    driver_name = update.message.from_user.full_name or f"Driver {did}"
    p = context.application.bot_data['db_pool']
    
    # 1. ⚡ Send a quick status message immediately so Telegram knows the bot received the event
    msg = update.message
    if not msg or not msg.photo:
        return

    # Check if this photo is part of a multi-photo album upload
    media_group_id = msg.media_group_id

    if media_group_id:
        # If we have already started processing a photo from this album batch, skip the rest
        if media_group_id in PROCESSED_GROUPS:
            logger.info(f"Skipping duplicate photo in album group {media_group_id}")
            return
        
        PROCESSED_GROUPS.add(media_group_id)
        
        # Auto-clean set memory
        if len(PROCESSED_GROUPS) > 500:
            PROCESSED_GROUPS.clear()

    # Continue processing single photo or first photo of the album
    status_msg = await msg.reply_text("⏳ Processing shipping document...")

    logger.info(f"📸 Received photo from Driver {did}. Downloading...")
    
    file = await update.message.photo[-1].get_file()
    buf = io.BytesIO()
    await file.download_to_memory(buf)
    img = buf.getvalue()
    
    logger.info(f"⚡ Processing photo ({len(img)} bytes) via MiniCPM-V executor...")
    
    loop = asyncio.get_running_loop()
    ext = await loop.run_in_executor(None, extract_bol_locally, img)

    # 3. Clean up status message
    try:
        await status_msg.delete()
    except Exception:
        pass
    
    logger.info(f"🤖 Handler Received AI Output: {ext}")
    
    # 🛑 1. Check if filtered out
    if not ext.get("is_bol"):
        logger.info(f"⏭️ Photo from Driver {did} skipped because 'is_bol' is False.")
        return

    b_num = ext.get("bol_number")
    t_num = ext.get("trailer_number")
    
    ai_shipper = 1 if ext.get("shipper_signed") else 0
    ai_receiver = 1 if ext.get("receiver_signed") else 0
    
    row = None
    async with p.acquire() as conn:
        await ensure_driver_exists(conn, did, driver_name)
        async with conn.cursor() as cur:
            # First try matching active open trip
            if b_num:
                await cur.execute(
                    "SELECT id, status, shipper_signed, receiver_signed, bol_number FROM messages "
                    "WHERE user_id=%s AND bol_number=%s ORDER BY id DESC LIMIT 1", 
                    (did, b_num)
                )
                row = await cur.fetchone()
            
            if not row:
                await cur.execute(
                    "SELECT id, status, shipper_signed, receiver_signed, bol_number FROM messages "
                    "WHERE user_id=%s AND status != 'completed' ORDER BY id DESC LIMIT 1", 
                    (did,)
                )
                row = await cur.fetchone()

            # 🛠️ If no open trip exists, create a new record directly
            if not row:
                logger.info(f"📝 Creating brand new BOL trip entry for Driver {did}")
                status = 'completed' if (ai_shipper == 1 and ai_receiver == 1) else 'active'

                # Get the exact UTC timestamp when the driver sent the message in Telegram
                departure_time = update.message.date.strftime('%Y-%m-%d %H:%M:%S')

                await cur.execute(
                    "INSERT INTO messages (user_id, image_blob, bol_number, trailer_number, shipper_signed, receiver_signed, status, departure_time) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (did, img, b_num, t_num, ai_shipper, ai_receiver, status, departure_time)
                )
                await conn.commit()
                rid = cur.lastrowid
                final_shipper, final_receiver = ai_shipper, ai_receiver
                final_bol = b_num
                new_status = status
            else:
                rid, current_status, existing_shipper, existing_receiver, DB_bol_number = row
                final_bol = DB_bol_number if DB_bol_number else b_num
                final_shipper = 1 if (existing_shipper == 1 or ai_shipper == 1) else 0
                final_receiver = 1 if (existing_receiver == 1 or ai_receiver == 1) else 0
                new_status = 'completed' if (final_shipper == 1 and final_receiver == 1) else current_status

                await cur.execute(
                    "UPDATE messages SET image_blob=%s, bol_number=%s, "
                    "trailer_number=COALESCE(%s, trailer_number), shipper_signed=%s, "
                    "receiver_signed=%s, status=%s WHERE id=%s",
                    (img, final_bol, t_num, final_shipper, final_receiver, new_status, rid)
                )
                await conn.commit()

    logger.info(f"💾 Successfully saved record ID #{rid} to MySQL database!")

    # 4. Alert admins
    for aid in ADMIN_IDS:
        kb = [[InlineKeyboardButton("✏️ Manual Correction", callback_data=f"manbol_{rid}")]]
        ship_chk = "✅ Signed" if final_shipper else "❌ Missing"
        recv_chk = "✅ Signed" if final_receiver else "❌ Missing"
        
        report = (
            f"📋 BOL Processed (Record #{rid})\n"
            f"🔢 BOL #: {final_bol or 'Not Found'}\n"
            f"🚛 Trailer: {t_num or 'Not Found'}\n"
            f"✍️ Shipper: {ship_chk}\n"
            f"✍️ Receiver: {recv_chk}\n"
            f"Status: {new_status}"
        )
        try:
            await context.bot.send_message(chat_id=aid, text=report, reply_markup=InlineKeyboardMarkup(kb))
        except Exception as e:
            logger.error(f"Failed to report to admin {aid}: {e}")


async def trigger_manual_override(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id): return
    await q.answer()
    act, rid = q.data.split("_")
    context.user_data['target_row_id'] = int(rid)
    context.user_data['state'] = f"WAITING_{act.upper()}"
    p = {
        "fixdep": "Type: `Origin to Destination`", 
        "fixarr": "Type arrival time:", 
        "manbol": "Type exactly:\n`BOL Trailer ShipperSigned(true/false) ReceiverSigned(true/false)`\nExample: `BOL123 TR999 true true`"
    }
    await context.bot.send_message(chat_id=q.message.chat_id, text=p[act], reply_markup=ForceReply(selective=True))

async def process_manual_replies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = context.user_data.get('state')
    rid = context.user_data.get('target_row_id')
    if not st or not rid: return
    utxt = update.message.text
    p = context.application.bot_data['db_pool']

    async with p.acquire() as conn:
        async with conn.cursor() as cur:
            if st == "WAITING_FIXDEP":
                pts = utxt.split(" to ")
                await cur.execute("UPDATE messages SET origin=%s, destination=%s WHERE id=%s", (pts[0].strip(), pts[1].strip() if len(pts)>1 else "Unknown", rid))
            elif st == "WAITING_FIXARR":
                await cur.execute("UPDATE messages SET arrival_time=%s WHERE id=%s", (utxt, rid))
            elif st == "WAITING_MANBOL":
                pts = utxt.split()
                if len(pts) >= 4:
                    bol, trail = pts[0], pts[1]
                    s_sign = 1 if pts[2].lower() == 'true' else 0
                    r_sign = 1 if pts[3].lower() == 'true' else 0
                    await cur.execute("UPDATE messages SET bol_number=%s, trailer_number=%s, shipper_signed=%s, receiver_signed=%s, status='completed' WHERE id=%s", (bol, trail, s_sign, r_sign, rid))
    context.user_data.clear()
    await update.message.reply_text("✨ Parameters saved successfully!")
