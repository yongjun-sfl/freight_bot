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

from config import TABLE_DRIVERS, TABLE_LOCATION_CODES, TABLE_SHUTTLE_LEGS, INDEX_UNIQUE_BOL
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
            # 1. Ensure driver_profiles table exists
            await cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {TABLE_DRIVERS} (
                    user_id BIGINT PRIMARY KEY,
                    driver_name VARCHAR(128) NOT NULL,
                    home_yard ENUM('YARD_200', 'SDS_WH') DEFAULT 'YARD_200',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """)

            # 2. Ensure location_codes table exists
            await cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {TABLE_LOCATION_CODES} (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    canonical_code VARCHAR(32) NOT NULL UNIQUE,
                    aliases VARCHAR(255) DEFAULT NULL,
                    official_name TEXT DEFAULT NULL,
                    is_active TINYINT(1) DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """)

            # 3. Ensure shuttle_legs table exists matching exact GUI schema
            await cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {TABLE_SHUTTLE_LEGS} (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    trailer_number VARCHAR(64) DEFAULT NULL,
                    bol_number VARCHAR(64) DEFAULT NULL,
                    document_type ENUM('FG', 'RM', 'UNKNOWN') DEFAULT 'UNKNOWN',
                    bol_image MEDIUMBLOB DEFAULT NULL,
                    load_status ENUM('EMPTY', 'LOADED') DEFAULT 'LOADED',
                    origin_location VARCHAR(128) DEFAULT 'Origin',
                    destination_location VARCHAR(128) DEFAULT 'Destination',
                    departure_time DATETIME DEFAULT NULL,
                    arrival_time DATETIME DEFAULT NULL, 
                    arrival_action VARCHAR(64) DEFAULT NULL,
                    dock_number VARCHAR(32) DEFAULT NULL,
                    shipper_signed TINYINT(1) DEFAULT 0,
                    receiver_signed TINYINT(1) DEFAULT 0,
                    is_positioning_leg TINYINT(1) DEFAULT 0,
                    is_bobtail TINYINT(1) DEFAULT 0,
                    leg_status ENUM('IN_TRANSIT', 'ARRIVED', 'UNLOADING', 'LOADING', 'COMPLETED') DEFAULT 'IN_TRANSIT',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    dispatch_msg_id BIGINT DEFAULT NULL,
                    INDEX idx_user_status (user_id, leg_status),
                    INDEX idx_dep_time (departure_time)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """)

            # 4. Global unique index check on bol_number
            await cur.execute(
                f"""SELECT COUNT(*) 
                      FROM information_schema.statistics 
                     WHERE table_schema = %s 
                       AND table_name = '{TABLE_SHUTTLE_LEGS}' 
                       AND index_name = '{INDEX_UNIQUE_BOL}';""",
                (MYSQL_DB,)
            )
            (index_exists,) = await cur.fetchone()

            if not index_exists:
                try:
                    await cur.execute(
                        f"""ALTER TABLE {TABLE_SHUTTLE_LEGS} 
                               ADD UNIQUE INDEX {INDEX_UNIQUE_BOL} (bol_number);"""
                    )
                    logger.info(f"Created unique index '{INDEX_UNIQUE_BOL}'.")
                except Exception as e:
                    logger.warning(f"Failed to create unique index on bol_number: {e}")

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