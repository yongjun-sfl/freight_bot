import os, io, logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ForceReply
from telegram.ext import ContextTypes
from ai_engine import clean_text_locally, extract_bol_locally

ADMIN_IDS = {int(x.strip()) for x in os.getenv('ADMIN_IDS', '').split(',') if x.strip()}
def is_admin(uid: int) -> bool: return uid in ADMIN_IDS

async def handle_incoming_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sid = update.message.from_user.id
    if is_admin(sid): return
    context.bot_data[f"raw_text_{sid}"] = update.message.text
    for aid in ADMIN_IDS:
        kb = [[InlineKeyboardButton("🛫 Process Departure", callback_data=f"dep_{sid}")],
              [InlineKeyboardButton("🛬 Process Arrival", callback_data=f"arr_{sid}")]]
        await context.bot.send_message(chat_id=aid, text=f"🚛 Driver {sid}: {update.message.text}", reply_markup=InlineKeyboardMarkup(kb))

async def handle_pipeline_routing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id): return
    await q.answer()
    act, did = q.data.split("_")
    txt = context.bot_data.get(f"raw_text_{did}", "")
    parsed = clean_text_locally(txt)
    p = context.application.bot_data['db_pool']

    if act == "dep":
        async with p.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("INSERT INTO messages (user_id, original_text, origin, destination, departure_time, status) VALUES (%s,%s,%s,%s,%s,'active')", (int(did), txt, parsed["origin"], parsed["destination"], parsed["time_info"]))
                rid = cur.lastrowid
        kb = [[InlineKeyboardButton("✏️ Fix Dep", callback_data=f"fixdep_{rid}")]]
        await q.edit_message_text(f"✅ Dep Logged! (ID: {rid})", reply_markup=InlineKeyboardMarkup(kb))
    elif act == "arr":
        async with p.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT id FROM messages WHERE user_id=%s AND status='active' ORDER BY id DESC LIMIT 1", (int(did),))
                row = await cur.fetchone()
                if not row: return
                rid = row[0]
                await cur.execute("UPDATE messages SET arrival_time=%s, status='arrived' WHERE id=%s", (parsed["time_info"] or txt, rid))
        kb = [[InlineKeyboardButton("✏️ Fix Arr", callback_data=f"fixarr_{rid}")]]
        await q.edit_message_text(f"✅ Arr Linked to Trip #{rid}!", reply_markup=InlineKeyboardMarkup(kb))

async def handle_photo_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    did = update.message.from_user.id
    p = context.application.bot_data['db_pool']
    async with p.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT id FROM messages WHERE user_id=%s AND status!='completed' ORDER BY id DESC LIMIT 1", (did,))
            row = await cur.fetchone()
    if not row: return
    rid = row[0]
    
    file = await update.message.photo[-1].get_file()
    buf = io.BytesIO()
    await file.download_to_memory(buf)
    img = buf.getvalue()
    
    # Process locally
    ext = extract_bol_locally(img)
    b_num, t_num = ext.get("bol_number"), ext.get("trailer_number")
    s_sign = 1 if ext.get("shipper_signed") else 0
    r_sign = 1 if ext.get("receiver_signed") else 0

    async with p.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("UPDATE messages SET image_blob=%s, bol_number=%s, trailer_number=%s, shipper_signed=%s, receiver_signed=%s WHERE id=%s", (img, b_num, t_num, s_sign, r_sign, rid))
            if b_num and t_num and s_sign and r_sign:
                await cur.execute("UPDATE messages SET status='completed' WHERE id=%s", (rid,))
    
    for aid in ADMIN_IDS:
        kb = [[InlineKeyboardButton("✏️ Manual Override", callback_data=f"manbol_{rid}")]]
        ship_status = "✅ Signed" if s_sign else "❌ Missing"
        recv_status = "✅ Signed" if r_sign else "❌ Missing"
        status = "🎉 Complete!" if (b_num and t_num and s_sign and r_sign) else "⚠️ Incomplete."
        
        await context.bot.send_message(
            chat_id=aid, 
            text=f"📋 **BOL Upload Received (Trip #{rid})**\nBOL: {b_num or '❌'}\nTrailer: {t_num or '❌'}\nShipper: {ship_status}\nReceiver: {recv_status}\n\n**Status:** {status}", 
            reply_markup=InlineKeyboardMarkup(kb)
        )

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
        "manbol": "Type format:\n`BOL Trailer ShipperSigned(true/false) ReceiverSigned(true/false)`\nExample: `BOL123 TR999 true true`"
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
    await update.message.reply_text("✨ Saved manually!")
