import os
import logging
import asyncio
from zoneinfo import ZoneInfo
from datetime import datetime
from telegram import Update
from telegram.ext import ContextTypes

from ai_engine import prepare_text_intent, prepare_image_intent, refresh_location_cache
from state_machine import commit_trip_leg

logger = logging.getLogger(__name__)

DISPATCH_ALERT_CHANNEL_ID = os.getenv("DISPATCH_ALERT_CHANNEL_ID")
EASTERN_TZ = ZoneInfo("America/New_York")

MEDIA_GROUP_BUFFER = {}
MEDIA_GROUP_LOCK = asyncio.Lock()


def get_eastern_timestamp() -> str:
    return datetime.now(EASTERN_TZ).strftime("%Y-%m-%d %H:%M:%S")


async def _send_dispatch_card(context: ContextTypes.DEFAULT_TYPE,
                              fallback_chat_id: int,
                              card_text: str):
    """Post a manual-reconcile card, preferring the dispatch channel."""
    await context.bot.send_message(
        chat_id=DISPATCH_ALERT_CHANNEL_ID or fallback_chat_id,
        text=card_text,
        parse_mode="Markdown"
    )


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text or update.message.text.startswith("/"):
        return

    user = update.effective_user
    chat = update.effective_chat
    driver_id = user.id
    user_name = user.full_name or user.username or f"Driver_{driver_id}"
    group_title = chat.title or "Private Chat"
    msg_timestamp = get_eastern_timestamp()
    raw_text = update.message.text.strip()

    pool = context.bot_data["db_pool"]

    try:
        # Text Intent Extraction
        intent = await prepare_text_intent(text=raw_text)

        # Commit Leg to State Machine
        res = await commit_trip_leg(
            p=pool,
            did=driver_id,
            user_name=user_name,
            group_title=group_title,
            orig_chat_id=chat.id,
            orig_msg_id=update.message.message_id,
            msg_timestamp=msg_timestamp,
            intent=intent
        )

        if res.get("card_text"):
            await _send_dispatch_card(context, chat.id, res["card_text"])

    except Exception as e:
        logger.error(f"Error processing text from {driver_id}: {e}", exc_info=True)


async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.photo:
        return

    msg = update.message
    media_group_id = msg.media_group_id

    if media_group_id:
        async with MEDIA_GROUP_LOCK:
            if media_group_id not in MEDIA_GROUP_BUFFER:
                MEDIA_GROUP_BUFFER[media_group_id] = {
                    "messages": [],
                    "task": None
                }
            MEDIA_GROUP_BUFFER[media_group_id]["messages"].append(msg)
            if MEDIA_GROUP_BUFFER[media_group_id]["task"] is None:
                MEDIA_GROUP_BUFFER[media_group_id]["task"] = asyncio.create_task(
                    _process_media_group_delayed(media_group_id, context)
                )
        return

    await _process_single_image_event(msg, context)


async def _process_single_image_event(msg, context: ContextTypes.DEFAULT_TYPE):
    user = msg.from_user
    chat = msg.chat
    driver_id = user.id
    user_name = user.full_name or user.username or f"Driver_{driver_id}"
    group_title = chat.title or "Private Chat"
    msg_timestamp = get_eastern_timestamp()
    caption = msg.caption.strip() if msg.caption else ""

    pool = context.bot_data["db_pool"]
    loop = asyncio.get_running_loop()

    try:
        photo_file = await context.bot.get_file(msg.photo[-1].file_id)
        photo_bytes = bytes(await photo_file.download_as_bytearray())

        # Vision OCR & Intent Extraction
        intent = await prepare_image_intent(
            images=[photo_bytes],
            caption_text=caption,
            loop=loop
        )

        # Commit Leg to State Machine
        res = await commit_trip_leg(
            p=pool,
            did=driver_id,
            user_name=user_name,
            group_title=group_title,
            orig_chat_id=chat.id,
            orig_msg_id=msg.message_id,
            msg_timestamp=msg_timestamp,
            intent=intent
        )

        if res.get("card_text"):
            await _send_dispatch_card(context, chat.id, res["card_text"])

    except Exception as e:
        logger.error(f"Error processing photo from {driver_id}: {e}", exc_info=True)


async def _process_media_group_delayed(media_group_id: str, context: ContextTypes.DEFAULT_TYPE):
    await asyncio.sleep(1.2)
    async with MEDIA_GROUP_LOCK:
        group_data = MEDIA_GROUP_BUFFER.pop(media_group_id, None)

    if not group_data or not group_data["messages"]:
        return

    messages = group_data["messages"]
    primary_msg = messages[0]
    user = primary_msg.from_user
    chat = primary_msg.chat
    driver_id = user.id
    user_name = user.full_name or user.username or f"Driver_{driver_id}"
    group_title = chat.title or "Private Chat"
    msg_timestamp = get_eastern_timestamp()

    # Aggregate caption if driver typed it on any image in the album
    caption = ""
    for m in messages:
        if m.caption and m.caption.strip():
            caption = m.caption.strip()
            break

    pool = context.bot_data["db_pool"]
    loop = asyncio.get_running_loop()

    try:
        image_bytes_list = []
        for m in messages:
            photo_file = await context.bot.get_file(m.photo[-1].file_id)
            image_bytes_list.append(bytes(await photo_file.download_as_bytearray()))

        # Vision OCR & Intent Extraction
        intent = await prepare_image_intent(
            images=image_bytes_list,
            caption_text=caption,
            loop=loop
        )

        # Commit Leg to State Machine
        res = await commit_trip_leg(
            p=pool,
            did=driver_id,
            user_name=user_name,
            group_title=group_title,
            orig_chat_id=primary_msg.message_id,
            orig_msg_id=primary_msg.message_id,
            msg_timestamp=msg_timestamp,
            intent=intent
        )

        if res.get("card_text"):
            await _send_dispatch_card(context, chat.id, res["card_text"])

    except Exception as e:
        logger.error(f"Error processing media group from {driver_id}: {e}", exc_info=True)


async def handle_document_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.document:
        return

    msg = update.message
    doc = msg.document
    if not (doc.mime_type == "application/pdf" or doc.mime_type.startswith("image/")):
        return

    user = msg.from_user
    chat = msg.chat
    driver_id = user.id
    user_name = user.full_name or user.username or f"Driver_{driver_id}"
    group_title = chat.title or "Private Chat"
    msg_timestamp = get_eastern_timestamp()
    caption = msg.caption.strip() if msg.caption else ""

    pool = context.bot_data["db_pool"]
    loop = asyncio.get_running_loop()

    try:
        doc_file = await context.bot.get_file(doc.file_id)
        doc_bytes = bytes(await doc_file.download_as_bytearray())

        # Vision OCR & Intent Extraction
        intent = await prepare_image_intent(
            images=[doc_bytes],
            caption_text=caption,
            loop=loop
        )

        # Commit Leg to State Machine
        res = await commit_trip_leg(
            p=pool,
            did=driver_id,
            user_name=user_name,
            group_title=group_title,
            orig_chat_id=chat.id,
            orig_msg_id=msg.message_id,
            msg_timestamp=msg_timestamp,
            intent=intent
        )

        if res.get("card_text"):
            await _send_dispatch_card(context, chat.id, res["card_text"])

    except Exception as e:
        logger.error(f"Error processing document from {driver_id}: {e}", exc_info=True)


async def refresh_locations_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pool = context.bot_data["db_pool"]
    try:
        await refresh_location_cache(pool)
        await update.message.reply_text("✅ Location cache refreshed successfully.")
    except Exception as e:
        await update.message.reply_text(f"❌ Cache refresh failed: {e}")