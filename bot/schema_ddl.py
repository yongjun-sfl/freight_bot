"""Single source of truth for the shuttle database schema.

Both the bot startup path (``main.init_db_pool``) and the test harness apply the
schema from here, so a fresh test database can never drift from production.
"""

from config import (
    TABLE_DRIVERS,
    TABLE_LOCATION_CODES,
    TABLE_SHUTTLE_LEGS,
    TABLE_ROUTES,
    TABLE_ROUTE_MEMBERS,
    TABLE_DISTANCES,
    TABLE_UNKNOWN_SENDERS,
    INDEX_UNIQUE_BOL,
)

# user_id is nullable and merely unique, not the primary key: the roster is
# known long before the Telegram ids are, and a driver has to exist as a row
# before their schedule lines can be attached to anyone.
DDL_DRIVERS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_DRIVERS} (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id BIGINT DEFAULT NULL,
    driver_name VARCHAR(128) NOT NULL,
    name_kor VARCHAR(128) DEFAULT NULL,
    name_eng VARCHAR(128) DEFAULT NULL,
    short_name VARCHAR(128) DEFAULT NULL,
    truck_plate VARCHAR(32) DEFAULT NULL,
    home_repo VARCHAR(32) DEFAULT NULL,
    is_active TINYINT(1) DEFAULT 1,
    home_yard ENUM('YARD_200', 'SDS_WH') DEFAULT 'YARD_200',
    last_nudge_at DATETIME DEFAULT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE INDEX idx_driver_user_id (user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

DDL_LOCATION_CODES = f"""
CREATE TABLE IF NOT EXISTS {TABLE_LOCATION_CODES} (
    id INT AUTO_INCREMENT PRIMARY KEY,
    canonical_code VARCHAR(32) NOT NULL UNIQUE,
    aliases VARCHAR(255) DEFAULT NULL,
    site_code VARCHAR(32) DEFAULT NULL,
    address VARCHAR(255) DEFAULT NULL,
    location_type VARCHAR(32) DEFAULT NULL,
    official_name TEXT DEFAULT NULL,
    is_active TINYINT(1) DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

DDL_SHUTTLE_LEGS = f"""
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
    paperwork_time DATETIME DEFAULT NULL,
    arrival_at_dock TINYINT(1) DEFAULT 0,
    dwell_alert_level INT DEFAULT 0,
    arrival_action VARCHAR(64) DEFAULT NULL,
    dock_number VARCHAR(32) DEFAULT NULL,
    origin_dock VARCHAR(32) DEFAULT NULL,
    destination_dock VARCHAR(32) DEFAULT NULL,
    shipper_signed TINYINT(1) DEFAULT 0,
    receiver_signed TINYINT(1) DEFAULT 0,
    is_positioning_leg TINYINT(1) DEFAULT 0,
    round_number INT DEFAULT NULL,
    route_code VARCHAR(32) DEFAULT NULL,
    is_bobtail TINYINT(1) DEFAULT 0,
    leg_status ENUM('IN_TRANSIT', 'ARRIVED', 'UNLOADING', 'LOADING', 'COMPLETED') DEFAULT 'IN_TRANSIT',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    dispatch_msg_id BIGINT DEFAULT NULL,
    INDEX idx_user_status (user_id, leg_status),
    INDEX idx_dep_time (departure_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

DDL_ROUTES = f"""
CREATE TABLE IF NOT EXISTS {TABLE_ROUTES} (
    route_code VARCHAR(32) PRIMARY KEY,
    route_name VARCHAR(128) DEFAULT NULL,
    anchor_location VARCHAR(32) NOT NULL,
    is_active TINYINT(1) DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_anchor (anchor_location)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

DDL_ROUTE_MEMBERS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_ROUTE_MEMBERS} (
    id INT AUTO_INCREMENT PRIMARY KEY,
    route_code VARCHAR(32) NOT NULL,
    location_code VARCHAR(32) NOT NULL,
    UNIQUE INDEX idx_route_location (route_code, location_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

DDL_DISTANCES = f"""
CREATE TABLE IF NOT EXISTS {TABLE_DISTANCES} (
    origin_code VARCHAR(32) NOT NULL,
    destination_code VARCHAR(32) NOT NULL,
    miles DECIMAL(6,2) DEFAULT NULL,
    drive_minutes INT DEFAULT NULL,
    weighted_minutes DECIMAL(6,2) DEFAULT NULL,
    PRIMARY KEY (origin_code, destination_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

DDL_UNKNOWN_SENDERS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_UNKNOWN_SENDERS} (
    user_id BIGINT PRIMARY KEY,
    display_name VARCHAR(128) DEFAULT NULL,
    username VARCHAR(128) DEFAULT NULL,
    first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    message_count INT DEFAULT 0
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

ALL_TABLES = (DDL_DRIVERS, DDL_LOCATION_CODES, DDL_SHUTTLE_LEGS,
              DDL_ROUTES, DDL_ROUTE_MEMBERS, DDL_DISTANCES,
              DDL_UNKNOWN_SENDERS)

# Columns introduced after the table first shipped. CREATE TABLE IF NOT EXISTS
# is a no-op against an existing database, so these must be applied separately
# and idempotently or production will never receive them.
ADDITIVE_COLUMNS = (
    ("origin_dock",
     f"ALTER TABLE {TABLE_SHUTTLE_LEGS} "
     "ADD COLUMN origin_dock VARCHAR(32) DEFAULT NULL AFTER dock_number"),
    ("destination_dock",
     f"ALTER TABLE {TABLE_SHUTTLE_LEGS} "
     "ADD COLUMN destination_dock VARCHAR(32) DEFAULT NULL AFTER origin_dock"),
    ("round_number",
     f"ALTER TABLE {TABLE_SHUTTLE_LEGS} "
     "ADD COLUMN round_number INT DEFAULT NULL AFTER is_positioning_leg"),
    ("route_code",
     f"ALTER TABLE {TABLE_SHUTTLE_LEGS} "
     "ADD COLUMN route_code VARCHAR(32) DEFAULT NULL AFTER round_number"),
    ("paperwork_time",
     f"ALTER TABLE {TABLE_SHUTTLE_LEGS} "
     "ADD COLUMN paperwork_time DATETIME DEFAULT NULL AFTER arrival_time"),
    ("arrival_at_dock",
     f"ALTER TABLE {TABLE_SHUTTLE_LEGS} "
     "ADD COLUMN arrival_at_dock TINYINT(1) DEFAULT 0 AFTER paperwork_time"),
    ("dwell_alert_level",
     f"ALTER TABLE {TABLE_SHUTTLE_LEGS} "
     "ADD COLUMN dwell_alert_level INT DEFAULT 0 AFTER arrival_at_dock"),
)

# Same shape, for location_codes.
ADDITIVE_DRIVER_COLUMNS = (
    ("last_nudge_at",
     f"ALTER TABLE {TABLE_DRIVERS} "
     "ADD COLUMN last_nudge_at DATETIME DEFAULT NULL"),
    ("name_kor",
     f"ALTER TABLE {TABLE_DRIVERS} ADD COLUMN name_kor VARCHAR(128) DEFAULT NULL"),
    ("name_eng",
     f"ALTER TABLE {TABLE_DRIVERS} ADD COLUMN name_eng VARCHAR(128) DEFAULT NULL"),
    ("short_name",
     f"ALTER TABLE {TABLE_DRIVERS} ADD COLUMN short_name VARCHAR(128) DEFAULT NULL"),
    ("truck_plate",
     f"ALTER TABLE {TABLE_DRIVERS} ADD COLUMN truck_plate VARCHAR(32) DEFAULT NULL"),
    ("home_repo",
     f"ALTER TABLE {TABLE_DRIVERS} ADD COLUMN home_repo VARCHAR(32) DEFAULT NULL"),
    ("is_active",
     f"ALTER TABLE {TABLE_DRIVERS} ADD COLUMN is_active TINYINT(1) DEFAULT 1"),
)

ADDITIVE_LOCATION_COLUMNS = (
    ("site_code",
     f"ALTER TABLE {TABLE_LOCATION_CODES} "
     "ADD COLUMN site_code VARCHAR(32) DEFAULT NULL AFTER aliases"),
    ("address",
     f"ALTER TABLE {TABLE_LOCATION_CODES} "
     "ADD COLUMN address VARCHAR(255) DEFAULT NULL AFTER site_code"),
    ("location_type",
     f"ALTER TABLE {TABLE_LOCATION_CODES} "
     "ADD COLUMN location_type VARCHAR(32) DEFAULT NULL AFTER address"),
)

# Tables the test harness is allowed to wipe between cases, child-first.
TRUNCATABLE = (TABLE_SHUTTLE_LEGS, TABLE_LOCATION_CODES, TABLE_DRIVERS)


async def _relax_driver_primary_key(cur, db_name: str, logger=None):
    """Move driver_profiles off user_id as its primary key.

    The original table made the Telegram id the primary key, so a driver could
    not be recorded until that id was known -- yet the id appears in no export
    and is only discovered when the person first messages. The roster now loads
    first and ids attach later, so user_id becomes a nullable unique column.
    """
    await cur.execute(
        """SELECT COUNT(*) FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s AND column_name = 'id';""",
        (db_name, TABLE_DRIVERS),
    )
    (already_done,) = await cur.fetchone()
    if already_done:
        return
    try:
        await cur.execute(
            f"""ALTER TABLE {TABLE_DRIVERS}
                    DROP PRIMARY KEY,
                    MODIFY user_id BIGINT DEFAULT NULL,
                    ADD COLUMN id INT AUTO_INCREMENT PRIMARY KEY FIRST,
                    ADD UNIQUE INDEX idx_driver_user_id (user_id);"""
        )
        if logger:
            logger.info(f"Restructured {TABLE_DRIVERS}: user_id is now nullable.")
    except Exception as e:
        if logger:
            logger.warning(f"Could not restructure {TABLE_DRIVERS}: {e}")


async def apply_schema(cur, db_name: str, logger=None):
    """Create every table, add late columns, ensure the unique BOL index."""
    for ddl in ALL_TABLES:
        await cur.execute(ddl)

    await _relax_driver_primary_key(cur, db_name, logger)

    for table, columns in ((TABLE_SHUTTLE_LEGS, ADDITIVE_COLUMNS),
                           (TABLE_LOCATION_CODES, ADDITIVE_LOCATION_COLUMNS),
                           (TABLE_DRIVERS, ADDITIVE_DRIVER_COLUMNS)):
        for column, alter in columns:
            await cur.execute(
                """SELECT COUNT(*)
                      FROM information_schema.columns
                     WHERE table_schema = %s
                       AND table_name = %s
                       AND column_name = %s;""",
                (db_name, table, column),
            )
            (column_exists,) = await cur.fetchone()
            if not column_exists:
                await cur.execute(alter)
                if logger:
                    logger.info(f"Added column '{column}' to {table}.")

    await cur.execute(
        f"""SELECT COUNT(*)
              FROM information_schema.statistics
             WHERE table_schema = %s
               AND table_name = %s
               AND index_name = %s;""",
        (db_name, TABLE_SHUTTLE_LEGS, INDEX_UNIQUE_BOL),
    )
    (index_exists,) = await cur.fetchone()

    if not index_exists:
        try:
            await cur.execute(
                f"""ALTER TABLE {TABLE_SHUTTLE_LEGS}
                       ADD UNIQUE INDEX {INDEX_UNIQUE_BOL} (bol_number);"""
            )
            if logger:
                logger.info(f"Created unique index '{INDEX_UNIQUE_BOL}'.")
        except Exception as e:
            if logger:
                logger.warning(f"Failed to create unique index on bol_number: {e}")
