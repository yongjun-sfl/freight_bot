import os
import logging
import aiomysql
from telegram.ext import (
    ApplicationBuilder, 
    MessageHandler, 
    CallbackQueryHandler, 
    ContextTypes, 
    filters
)
from handlers import handle_shuttle_text, handle_photo_upload, handle_reconcile_callback

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO
)
# Suppress verbose HTTP polling log spam
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Environment Configuration
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MYSQL_HOST = os.getenv("MYSQL_HOST", "db")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", 3306))
MYSQL_USER = os.getenv("MYSQL_USER", "root")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
MYSQL_DATABASE = os.getenv("MYSQL_DATABASE", "shuttle_db")


async def init_db_pool(app):
    """Initializes async MySQL connection pool."""
    logger.info("Initializing MySQL connection pool...")
    app.bot_data['db_pool'] = await aiomysql.create_pool(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        db=MYSQL_DATABASE,
        autocommit=True,
        maxsize=10
    )
    logger.info("MySQL connection pool created successfully.")


async def close_db_pool(app):
    """Gracefully closes async MySQL connection pool."""
    logger.info("Closing MySQL connection pool...")
    pool = app.bot_data.get('db_pool')
    if pool:
        pool.close()
        await pool.wait_closed()
    logger.info("MySQL pool closed.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Logs errors and prevents unhandled application crashes."""
    logger.error("Exception occurred while handling an update:", exc_info=context.error)


def main():
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN environment variable is missing.")

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(init_db_pool).post_shutdown(close_db_pool).build()

    # Register Handlers
    app.add_handler(CallbackQueryHandler(handle_reconcile_callback))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_shuttle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo_upload))

    # Register Global Error Handler
    app.add_error_handler(error_handler)

    logger.info("Starting Telegram Shuttle Dispatch Bot...")
    app.run_polling()


if __name__ == "__main__":
    main()