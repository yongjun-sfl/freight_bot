import os
import sys
import asyncio
import logging
import aiomysql
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes
)

from ai_engine import refresh_location_cache
from handlers import (
    handle_text_message,
    handle_photo_message,
    handle_document_message,
    refresh_locations_command
)

# -------------------------------------------------------------------
# Logging Configuration
# -------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("dispatch_bot")

# SILENCE HTTPX POLLING LOGS
logging.getLogger("httpx").setLevel(logging.WARNING)

# -------------------------------------------------------------------
# Environment Variables
# -------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MYSQL_HOST = os.getenv("MYSQL_HOST", "mysql")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", 3306))
MYSQL_USER = os.getenv("MYSQL_USER", "dispatch_user")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "dispatch_pass")
MYSQL_DB = os.getenv("MYSQL_DATABASE", "logistics_db")

if not TELEGRAM_BOT_TOKEN:
    logger.critical("TELEGRAM_BOT_TOKEN environment variable is missing! Exiting...")
    sys.exit(1)


# -------------------------------------------------------------------
# Database Initialization & Schema Auto-Setup
# -------------------------------------------------------------------
async def init_db_pool():
    """Establishes aiomysql pool and enforces table schemas and unique constraints."""
    logger.info(f"Connecting to MySQL database '{MYSQL_DB}' at {MYSQL_HOST}:{MYSQL_PORT}...")
    pool = await aiomysql.create_pool(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        db=MYSQL_DB,
        autocommit=True,
        minsize=2,
        maxsize=20
    )

    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            # 1. Ensure shuttle_legs table exists
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS shuttle_legs (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    trailer_number VARCHAR(64) DEFAULT NULL,
                    bol_number VARCHAR(128) DEFAULT NULL,
                    origin_location VARCHAR(64) DEFAULT NULL,
                    destination_location VARCHAR(64) DEFAULT NULL,
                    departure_time DATETIME DEFAULT NULL,
                    arrival_time DATETIME DEFAULT NULL,
                    arrival_action VARCHAR(64) DEFAULT NULL,
                    dock_number VARCHAR(32) DEFAULT NULL,
                    load_status VARCHAR(32) DEFAULT 'UNKNOWN',
                    leg_status VARCHAR(32) DEFAULT 'IN_TRANSIT',
                    shipper_signed BOOLEAN DEFAULT FALSE,
                    receiver_signed BOOLEAN DEFAULT FALSE,
                    bol_image LONGBLOB DEFAULT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_user_status (user_id, leg_status),
                    INDEX idx_dep_time (departure_time)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """)

            # 2. Ensure location_codes lookup table exists
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS location_codes (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    canonical_code VARCHAR(32) NOT NULL UNIQUE,
                    aliases TEXT DEFAULT NULL,
                    is_active BOOLEAN DEFAULT TRUE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """)

            # 3. Add global unique index on bol_number if missing
            await cur.execute("""
                SELECT COUNT(*) FROM information_schema.statistics 
                WHERE table_schema = %s 
                  AND table_name = 'shuttle_legs' 
                  AND index_name = 'idx_unique_bol_number';
            """, (MYSQL_DB,))
            (index_exists,) = await cur.fetchone()

            if not index_exists:
                try:
                    await cur.execute("""
                        ALTER TABLE shuttle_legs 
                        ADD UNIQUE INDEX idx_unique_bol_number (bol_number);
                    """)
                    logger.info("Created unique index 'idx_unique_bol_number' on shuttle_legs.")
                except Exception as e:
                    logger.warning(f"Failed to create unique index on bol_number (data duplicates may exist): {e}")

    logger.info("Database pool initialized successfully.")
    return pool


# -------------------------------------------------------------------
# Startup / Shutdown Hooks
# -------------------------------------------------------------------
async def on_startup(application: Application):
    """Executes pre-flight checks and pre-warms location cache on application launch."""
    db_pool = await init_db_pool()
    application.bot_data["db_pool"] = db_pool

    # Pre-warm location code alias cache from MySQL
    await refresh_location_cache(db_pool)
    logger.info("🚀 AI Dispatch Engine is live and listening for messages.")


async def on_shutdown(application: Application):
    """Cleanly closes database pool connection on container shutdown."""
    db_pool = application.bot_data.get("db_pool")
    if db_pool:
        db_pool.close()
        await db_pool.wait_closed()
        logger.info("MySQL connection pool gracefully closed.")


# -------------------------------------------------------------------
# Main Application Entry Point
# -------------------------------------------------------------------
def main():
    """Builds and runs the Telegram bot Application."""
    builder = Application.builder().token(TELEGRAM_BOT_TOKEN)
    
    # Register lifecycle hooks
    builder.post_init(on_startup)
    builder.post_shutdown(on_shutdown)

    app = builder.build()

    # Admin Commands
    app.add_handler(CommandHandler("refresh_locations", refresh_locations_command))

    # Core Dispatch Handlers
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo_message))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document_message))

    # Start long-polling
    logger.info("Starting Telegram Bot long-polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()