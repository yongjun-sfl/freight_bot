import os
import sys
import asyncio
import logging
import concurrent.futures

import aiomysql
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters
)

from schema_ddl import apply_schema
from ai_engine import refresh_location_cache
from routes import refresh_route_cache, seed_default_routes
from seed_network import seed_network
from dwell import sweep_dwells
from handlers import (
    handle_text_message,
    handle_photo_message,
    handle_document_message,
    refresh_locations_command
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("dispatch_bot")

# SILENCE HTTPX POLLING LOGS FOR CLEAN CONSOLE OUTPUT
logging.getLogger("httpx").setLevel(logging.WARNING)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MYSQL_HOST = os.getenv("MYSQL_HOST", "mysql")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", 3306))
MYSQL_USER = os.getenv("MYSQL_USER", "dispatch_user")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "dispatch_pass")
MYSQL_DB = os.getenv("MYSQL_DATABASE", "shuttle_db")

if not TELEGRAM_BOT_TOKEN:
    logger.critical("TELEGRAM_BOT_TOKEN environment variable is missing!")
    sys.exit(1)


async def init_db_pool():
    logger.info(f"Connecting to MySQL database '{MYSQL_DB}' at {MYSQL_HOST}:{MYSQL_PORT}...")
    pool = await aiomysql.create_pool(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        db=MYSQL_DB,
        autocommit=True,
        init_command="SET time_zone = 'America/New_York';",  # Enforce Eastern Time pool-wide
        minsize=2,
        maxsize=20
    )

    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await apply_schema(cur, MYSQL_DB, logger)
            seeded = await seed_default_routes(cur)
            if seeded:
                logger.info(f"Seeded {seeded} default shuttle routes.")
            locations, distances = await seed_network(cur)
            if locations or distances:
                logger.info(
                    f"Seeded {locations} location codes and {distances} distance pairs."
                )

    logger.info("Database pool initialized successfully in Eastern Time.")
    return pool


DWELL_SWEEP_SECONDS = 120

# Gemini calls are blocking and slow -- measured 1s to 90s for the same prompt,
# with no errors; it is Google's latency variance, not retries. They run in a
# thread pool so the event loop stays free, but Python's default pool is
# min(32, cpu+4), which is 8 on a t3.xlarge. With 13 drivers that queues.
# These threads only wait on network, so a larger pool costs nothing.
PARSE_POOL_WORKERS = int(os.getenv("PARSE_POOL_WORKERS", 32))


async def _dwell_job(context):
    """Raise a card as each driver crosses a dwell threshold, while it matters."""
    pool = context.bot_data.get("db_pool")
    if not pool:
        return
    channel = os.getenv("DISPATCH_ALERT_CHANNEL_ID")
    if not channel:
        return

    async def send_card(text):
        await context.bot.send_message(chat_id=channel, text=text,
                                       parse_mode="Markdown")

    try:
        sent = await sweep_dwells(pool, send_card)
        if sent:
            logger.info(f"Dwell sweep raised {sent} card(s).")
    except Exception as e:
        logger.error(f"Dwell sweep failed: {e}", exc_info=True)


async def on_startup(application: Application):
    asyncio.get_running_loop().set_default_executor(
        concurrent.futures.ThreadPoolExecutor(
            max_workers=PARSE_POOL_WORKERS, thread_name_prefix="parse"
        )
    )
    logger.info(f"Parse pool sized to {PARSE_POOL_WORKERS} workers.")

    db_pool = await init_db_pool()
    application.bot_data["db_pool"] = db_pool
    await refresh_location_cache(db_pool)
    await refresh_route_cache(db_pool)

    if application.job_queue:
        application.job_queue.run_repeating(
            _dwell_job, interval=DWELL_SWEEP_SECONDS, first=DWELL_SWEEP_SECONDS
        )
        logger.info(f"Dwell monitor running every {DWELL_SWEEP_SECONDS}s.")
    else:
        logger.warning(
            "JobQueue unavailable; dwell alerts disabled. "
            "Install python-telegram-bot[job-queue]."
        )

    logger.info("🚀 AI Dispatch Engine is live.")


async def on_shutdown(application: Application):
    db_pool = application.bot_data.get("db_pool")
    if db_pool:
        db_pool.close()
        await db_pool.wait_closed()
        logger.info("Database pool closed.")


def main():
    builder = Application.builder().token(TELEGRAM_BOT_TOKEN)
    builder.post_init(on_startup)
    builder.post_shutdown(on_shutdown)

    app = builder.build()

    app.add_handler(CommandHandler("refresh_locations", refresh_locations_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo_message))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document_message))

    logger.info("Starting Telegram Bot long-polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()