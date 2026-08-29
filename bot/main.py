import os
import sys
import logging
import aiomysql
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters
)

from schema_ddl import apply_schema
from ai_engine import refresh_location_cache
from handlers import (
    handle_text_message,
    handle_photo_message,
    handle_document_message,
    handle_callback_query,
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

    logger.info("Database pool initialized successfully in Eastern Time.")
    return pool


async def on_startup(application: Application):
    db_pool = await init_db_pool()
    application.bot_data["db_pool"] = db_pool
    await refresh_location_cache(db_pool)
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
    app.add_handler(CallbackQueryHandler(handle_callback_query))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo_message))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document_message))

    logger.info("Starting Telegram Bot long-polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()