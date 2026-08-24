import logging
import asyncio
import os
from telegram import Update
from telegram.ext import ContextTypes

from ai_engine import (
    prepare_text_intent,
    prepare_image_intent,
    refresh_location_cache
)
from state_machine import commit_trip_leg

logger = logging.getLogger(__name__)

DISPATCH_ALERT_CHANNEL_ID = os.getenv("DISPATCH_ALERT_CHANNEL_ID")

# Buffer for grouping multi-photo album uploads (Media Groups)
MEDIA_GROUP_BUFFER = {}
MEDIA_GROUP_LOCK = asyncio.Lock()


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processes plain text operational messages (e.g., '200 to E2F', 'live loading at #47')."""
    if not update.message or not update.message.text:
        return

    # Ignore command messages starting with '/'
    if update.message.text.startswith("/"):
        return

    user = update.effective_user
    chat = update.effective_chat
    driver_id = user.id
    user_name = user.full_name or user.username or f"Driver_{driver_id}"
    group_title = chat.title or "Private Chat"
    msg_timestamp = update.message.date.strftime("%Y-%m-%d %H:%M:%S")
    raw_text = update.message.text.strip()

    pool = context.bot_data["db_pool"]
    loop = asyncio.get_running_loop()

    try:
        # Parse intent via Gemini LLM
        intent = await prepare_text_intent(raw_text)

        # Execute state machine transition
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

        # EXCEPTION-ONLY ALERT: Send Telegram card ONLY if manual intervention is required
        if res.get("card_text"):
            await context.bot.send_message(
                chat_id=DISPATCH_ALERT_CHANNEL_ID,
                text=res["card_text"],
                reply_to_message_id=update.message.message_id,
                parse_mode="Markdown"
            )

    except Exception as e:
        logger.error(f"Error processing text message from {driver_id}: {e}", exc_info=True)


async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processes single photos or buffers albums (media groups) for batch OCR processing."""
    if not update.message or not update.message.photo:
        return

    msg = update.message
    media_group_id = msg.media_group_id

    # If message is part of an album / media group, buffer it
    if media_group_id:
        async with MEDIA_GROUP_LOCK:
            if media_group_id not in MEDIA_GROUP_BUFFER:
                MEDIA_GROUP_BUFFER[media_group_id] = {
                    "messages": [],
                    "task": None
                }

            MEDIA_GROUP_BUFFER[media_group_id]["messages"].append(msg)

            # Schedule batch processing after a short delay to collect all media items
            if MEDIA_GROUP_BUFFER[media_group_id]["task"] is None:
                MEDIA_GROUP_BUFFER[media_group_id]["task"] = asyncio.create_task(
                    _process_media_group_delayed(media_group_id, context)
                )
        return

    # Process single image upload immediately
    await _process_single_image_event(msg, context)


async def _process_single_image_event(msg, context: ContextTypes.DEFAULT_TYPE):
    """Downloads photo, runs vision intent parser, and executes state transition."""
    user = msg.from_user
    chat = msg.chat
    driver_id = user.id
    user_name = user.full_name or user.username or f"Driver_{driver_id}"
    group_title = chat.title or "Private Chat"
    msg_timestamp = msg.date.strftime("%Y-%m-%d %H:%M:%S")
    caption = msg.caption.strip() if msg.caption else ""

    pool = context.bot_data["db_pool"]
    loop = asyncio.get_running_loop()

    try:
        # Get highest resolution photo variant
        photo_file = await context.bot.get_file(msg.photo[-1].file_id)
        photo_bytes = bytes(await photo_file.download_as_bytearray())

        # Vision OCR & Intent Extraction
        intent = await prepare_image_intent(
            images=[photo_bytes],
            caption_text=caption,
            loop=loop
        )

        # Execute state machine transition
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

        # EXCEPTION-ONLY ALERT
        if res.get("card_text"):
            await context.bot.send_message(
                chat_id=DISPATCH_ALERT_CHANNEL_ID,
                text=res["card_text"],
                reply_to_message_id=msg.message_id,
                parse_mode="Markdown"
            )

    except Exception as e:
        logger.error(f"Error processing photo from {driver_id}: {e}", exc_info=True)


async def _process_media_group_delayed(media_group_id: str, context: ContextTypes.DEFAULT_TYPE):
    """Waits 1.2s to collect all photos in a media album before processing batch vision OCR."""
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
    msg_timestamp = primary_msg.date.strftime("%Y-%m-%d %H:%M:%S")

    # Aggregate caption if driver typed it on any image in the album
    caption = ""
    for m in messages:
        if m.caption and m.caption.strip():
            caption = m.caption.strip()
            break

    pool = context.bot_data["db_pool"]
    loop = asyncio.get_running_loop()

    try:
        # Download all photo bytes in album
        image_bytes_list = []
        for m in messages:
            photo_file = await context.bot.get_file(m.photo[-1].file_id)
            img_b = bytes(await photo_file.download_as_bytearray())
            image_bytes_list.append(img_b)

        # Batch Vision OCR with Early Exit
        intent = await prepare_image_intent(
            images=image_bytes_list,
            caption_text=caption,
            loop=loop
        )

        # Execute state machine transition
        res = await commit_trip_leg(
            p=pool,
            did=driver_id,
            user_name=user_name,
            group_title=group_title,
            orig_chat_id=chat.id,
            orig_msg_id=primary_msg.message_id,
            msg_timestamp=msg_timestamp,
            intent=intent
        )

        # EXCEPTION-ONLY ALERT
        if res.get("card_text"):
            await context.bot.send_message(
                chat_id=DISPATCH_ALERT_CHANNEL_ID,
                text=res["card_text"],
                reply_to_message_id=primary_msg.message_id,
                parse_mode="Markdown"
            )

    except Exception as e:
        logger.error(f"Error processing media group {media_group_id} for {driver_id}: {e}", exc_info=True)


async def handle_document_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Processes document/PDF uploads."""
    if not update.message or not update.message.document:
        return

    msg = update.message
    doc = msg.document
    
    # Restrict document scanning to PDFs or standard image mime types
    if not (doc.mime_type == "application/pdf" or doc.mime_type.startswith("image/")):
        return

    user = msg.from_user
    chat = msg.chat
    driver_id = user.id
    user_name = user.full_name or user.username or f"Driver_{driver_id}"
    group_title = chat.title or "Private Chat"
    msg_timestamp = msg.date.strftime("%Y-%m-%d %H:%M:%S")
    caption = msg.caption.strip() if msg.caption else ""

    pool = context.bot_data["db_pool"]
    loop = asyncio.get_running_loop()

    try:
        doc_file = await context.bot.get_file(doc.file_id)
        doc_bytes = bytes(await doc_file.download_as_bytearray())

        intent = await prepare_image_intent(
            images=[doc_bytes],
            caption_text=caption,
            loop=loop
        )

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
            await context.bot.send_message(
                chat_id=DISPATCH_ALERT_CHANNEL_ID,
                text=res["card_text"],
                reply_to_message_id=msg.message_id,
                parse_mode="Markdown"
            )

    except Exception as e:
        logger.error(f"Error processing document from {driver_id}: {e}", exc_info=True)


async def refresh_locations_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command (/refresh_locations) to hot-reload location codes from MySQL."""
    pool = context.bot_data["db_pool"]
    try:
        await refresh_location_cache(pool)
        await update.message.reply_text("✅ Location cache refreshed successfully from MySQL.")
    except Exception as e:
        logger.error(f"Failed to refresh location cache: {e}")
        await update.message.reply_text(f"❌ Failed to refresh location cache: {e}")