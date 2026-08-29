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
    INDEX_UNIQUE_BOL,
)

DDL_DRIVERS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_DRIVERS} (
    user_id BIGINT PRIMARY KEY,
    driver_name VARCHAR(128) NOT NULL,
    home_yard ENUM('YARD_200', 'SDS_WH') DEFAULT 'YARD_200',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

DDL_LOCATION_CODES = f"""
CREATE TABLE IF NOT EXISTS {TABLE_LOCATION_CODES} (
    id INT AUTO_INCREMENT PRIMARY KEY,
    canonical_code VARCHAR(32) NOT NULL UNIQUE,
    aliases VARCHAR(255) DEFAULT NULL,
    site_code VARCHAR(32) DEFAULT NULL,
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

ALL_TABLES = (DDL_DRIVERS, DDL_LOCATION_CODES, DDL_SHUTTLE_LEGS,
              DDL_ROUTES, DDL_ROUTE_MEMBERS, DDL_DISTANCES)

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
)

# Same shape, for location_codes.
ADDITIVE_LOCATION_COLUMNS = (
    ("site_code",
     f"ALTER TABLE {TABLE_LOCATION_CODES} "
     "ADD COLUMN site_code VARCHAR(32) DEFAULT NULL AFTER aliases"),
)

# Tables the test harness is allowed to wipe between cases, child-first.
TRUNCATABLE = (TABLE_SHUTTLE_LEGS, TABLE_LOCATION_CODES, TABLE_DRIVERS)


async def apply_schema(cur, db_name: str, logger=None):
    """Create every table, add late columns, ensure the unique BOL index."""
    for ddl in ALL_TABLES:
        await cur.execute(ddl)

    for table, columns in ((TABLE_SHUTTLE_LEGS, ADDITIVE_COLUMNS),
                           (TABLE_LOCATION_CODES, ADDITIVE_LOCATION_COLUMNS)):
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
