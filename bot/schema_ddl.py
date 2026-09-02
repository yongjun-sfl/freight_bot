"""Single source of truth for the shuttle database schema.

Both the bot startup path (``main.init_db_pool``) and the test harness apply the
schema from here, so a fresh test database can never drift from production.
"""

from config import (
    TABLE_DRIVERS,
    TABLE_LOCATION_CODES,
    TABLE_LOCATION_DOCKS,
    TABLE_SHUTTLE_LEGS,
    TABLE_ROUTES,
    TABLE_ROUTE_MEMBERS,
    TABLE_DISTANCES,
    TABLE_UNKNOWN_SENDERS,
    TABLE_RM_LOADS,
    TABLE_RM_LOAD_ITEMS,
    TABLE_SHIFTS,
    TABLE_MANIFESTS,
    TABLE_MANIFEST_ROWS,
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
    -- Who the work at this site is billed to. SDS contracts with FNS, and FNS
    -- sites may invoice to FNS rather than SDS. Recorded, not yet acted on:
    -- the invoicing arrangement is unsettled.
    owner VARCHAR(32) DEFAULT NULL,
    official_name TEXT DEFAULT NULL,
    is_active TINYINT(1) DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

DDL_LOCATION_DOCKS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_LOCATION_DOCKS} (
    id INT AUTO_INCREMENT PRIMARY KEY,
    facility_code VARCHAR(32) NOT NULL,
    -- Doors are numbered in bands, and the band says what the door is for.
    -- At 200: 3-21 FG inbound and 22-45 RM inbound at the front, 47-66 RM
    -- outbound and 67-99 FG outbound at the rear. That is how "#3 to #47"
    -- is known to cross from the front building to the rear one.
    first_dock INT NOT NULL,
    last_dock INT NOT NULL,
    dock_use VARCHAR(32) DEFAULT NULL,
    -- Who works the band. 200F 22-45 is Hanjin's RM inbound, not our traffic.
    operator VARCHAR(32) DEFAULT NULL,
    note VARCHAR(255) DEFAULT NULL,
    UNIQUE KEY uniq_dock_band (facility_code, first_dock, last_dock),
    INDEX idx_dock_facility (facility_code)
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
    -- When the driver reported the live load or unload complete. Distinct
    -- from arrival (on site) and from paperwork (clerk signed).
    finished_time DATETIME DEFAULT NULL,
    arrival_at_dock TINYINT(1) DEFAULT 0,
    dwell_alert_level INT DEFAULT 0,
    arrival_action VARCHAR(64) DEFAULT NULL,
    dock_number VARCHAR(32) DEFAULT NULL,
    origin_dock VARCHAR(32) DEFAULT NULL,
    destination_dock VARCHAR(32) DEFAULT NULL,
    -- SDS yard slot ("DO# 34"). A parking position, unrelated to the delivery
    -- order number that may appear on a BOL. SDS is the only site using it.
    do_number VARCHAR(32) DEFAULT NULL,
    -- Hand-marked on RM paperwork as "08/28-7": that load's place in the
    -- allocation the SDS manager sets for the day.
    rm_seq VARCHAR(16) DEFAULT NULL,
    shipper_signed TINYINT(1) DEFAULT 0,
    receiver_signed TINYINT(1) DEFAULT 0,
    is_positioning_leg TINYINT(1) DEFAULT 0,
    round_number INT DEFAULT NULL,
    route_code VARCHAR(32) DEFAULT NULL,
    load_type VARCHAR(32) DEFAULT NULL,
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

# Shaped to the dispatcher's RM Delivery Summary. Keyed on delivery_date +
# rm_seq, because the sequence restarts each day and one trip can carry more
# than one reservation ("926576, 926577" appears as a single row).
DDL_RM_LOADS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_RM_LOADS} (
    id INT AUTO_INCREMENT PRIMARY KEY,
    delivery_date DATE NOT NULL,
    rm_seq INT NOT NULL,
    leg_id INT DEFAULT NULL,
    driver_id BIGINT DEFAULT NULL,
    driver_name VARCHAR(128) DEFAULT NULL,
    -- may hold several, comma separated, when one trip carries multiple
    reservation_no VARCHAR(128) DEFAULT NULL,
    trailer_number VARCHAR(64) DEFAULT NULL,
    dock_number VARCHAR(32) DEFAULT NULL,
    origin_location VARCHAR(128) DEFAULT NULL,
    destination_location VARCHAR(128) DEFAULT NULL,
    -- destination in the client's own words, e.g. EAGLE 2 FRONT
    pod VARCHAR(128) DEFAULT NULL,
    departure_time DATETIME DEFAULT NULL,
    eta DATETIME DEFAULT NULL,
    arrival_time DATETIME DEFAULT NULL,
    finished_time DATETIME DEFAULT NULL,
    -- Finished minus Arrival: unload time at the receiving end, which is what
    -- the summary reports. Transit is arrival minus departure and is separate.
    time_taken_minutes INT DEFAULT NULL,
    transit_minutes INT DEFAULT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE INDEX idx_rm_date_seq (delivery_date, rm_seq),
    INDEX idx_rm_leg (leg_id),
    INDEX idx_rm_destination (destination_location, delivery_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

DDL_RM_LOAD_ITEMS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_RM_LOAD_ITEMS} (
    id INT AUTO_INCREMENT PRIMARY KEY,
    rm_load_id INT NOT NULL,
    material_code VARCHAR(64) DEFAULT NULL,
    description VARCHAR(255) DEFAULT NULL,
    qty VARCHAR(32) DEFAULT NULL,
    weight VARCHAR(32) DEFAULT NULL,
    cont_no VARCHAR(64) DEFAULT NULL,
    batch_no VARCHAR(64) DEFAULT NULL,
    remark VARCHAR(255) DEFAULT NULL,
    INDEX idx_item_load (rm_load_id),
    INDEX idx_item_batch (batch_no)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

# Expected columns are the dispatcher's, reported columns are the bot's.
# Never merged: an inferred time sitting in a reported column becomes
# indistinguishable from an observation, and these feed payroll.
DDL_SHIFTS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_SHIFTS} (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id BIGINT NOT NULL,
    shift_date DATE NOT NULL,
    expected_clock_in DATETIME DEFAULT NULL,
    reported_clock_in DATETIME DEFAULT NULL,
    expected_clock_out DATETIME DEFAULT NULL,
    reported_clock_out DATETIME DEFAULT NULL,
    -- One hour, taken whenever they like and reported at both ends.
    lunch_start DATETIME DEFAULT NULL,
    lunch_end DATETIME DEFAULT NULL,
    lunch_minutes INT DEFAULT NULL,
    worked_minutes INT DEFAULT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE INDEX idx_shift_driver_date (user_id, shift_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

# The photo is the record; Telegram is not an archive. Keyed on the image
# digest so a resent photo does not become a second manifest.
DDL_MANIFESTS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_MANIFESTS} (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id BIGINT NOT NULL,
    driver_name VARCHAR(128) DEFAULT NULL,
    manifest_date VARCHAR(32) DEFAULT NULL,
    team VARCHAR(32) DEFAULT NULL,
    image_sha256 CHAR(64) NOT NULL,
    image MEDIUMBLOB DEFAULT NULL,
    received_at DATETIME DEFAULT NULL,
    row_count INT DEFAULT 0,
    flagged_rows INT DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE INDEX idx_manifest_digest (image_sha256),
    INDEX idx_manifest_driver (user_id, received_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

# Rows read off the handwriting. A hint for reconciliation, never a source:
# on a real sheet the trailer column read "11" for eight of nine rows because
# the driver used ditto marks. `problems` records why a row is doubtful.
DDL_MANIFEST_ROWS = f"""
CREATE TABLE IF NOT EXISTS {TABLE_MANIFEST_ROWS} (
    id INT AUTO_INCREMENT PRIMARY KEY,
    manifest_id INT NOT NULL,
    row_no INT NOT NULL,
    origin VARCHAR(64) DEFAULT NULL,
    depart_time VARCHAR(32) DEFAULT NULL,
    destination VARCHAR(64) DEFAULT NULL,
    arrive_time VARCHAR(32) DEFAULT NULL,
    load_status VARCHAR(16) DEFAULT NULL,
    trailer_number VARCHAR(32) DEFAULT NULL,
    problems VARCHAR(255) DEFAULT NULL,
    matched_leg_id INT DEFAULT NULL,
    INDEX idx_row_manifest (manifest_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

ALL_TABLES = (DDL_DRIVERS, DDL_LOCATION_CODES, DDL_LOCATION_DOCKS,
              DDL_SHUTTLE_LEGS,
              DDL_ROUTES, DDL_ROUTE_MEMBERS, DDL_DISTANCES,
              DDL_UNKNOWN_SENDERS, DDL_RM_LOADS, DDL_RM_LOAD_ITEMS,
              DDL_SHIFTS, DDL_MANIFESTS, DDL_MANIFEST_ROWS)

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
    ("do_number",
     f"ALTER TABLE {TABLE_SHUTTLE_LEGS} "
     "ADD COLUMN do_number VARCHAR(32) DEFAULT NULL AFTER destination_dock"),
    ("rm_seq",
     f"ALTER TABLE {TABLE_SHUTTLE_LEGS} "
     "ADD COLUMN rm_seq VARCHAR(16) DEFAULT NULL AFTER do_number"),
    ("load_type",
     f"ALTER TABLE {TABLE_SHUTTLE_LEGS} "
     "ADD COLUMN load_type VARCHAR(32) DEFAULT NULL AFTER route_code"),
    ("finished_time",
     f"ALTER TABLE {TABLE_SHUTTLE_LEGS} "
     "ADD COLUMN finished_time DATETIME DEFAULT NULL AFTER arrival_time"),
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

# driver_shifts already shipped, so lunch has to reach it by ALTER.
ADDITIVE_SHIFT_COLUMNS = (
    ("lunch_start",
     f"ALTER TABLE {TABLE_SHIFTS} ADD COLUMN lunch_start DATETIME DEFAULT NULL"),
    ("lunch_end",
     f"ALTER TABLE {TABLE_SHIFTS} ADD COLUMN lunch_end DATETIME DEFAULT NULL"),
    ("lunch_minutes",
     f"ALTER TABLE {TABLE_SHIFTS} ADD COLUMN lunch_minutes INT DEFAULT NULL"),
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
    ("owner",
     f"ALTER TABLE {TABLE_LOCATION_CODES} "
     "ADD COLUMN owner VARCHAR(32) DEFAULT NULL AFTER location_type"),
)

# Tables the test harness is allowed to wipe between cases, child-first.
# Every table holding per-run state. Anything omitted leaks between tests and
# produces failures that look like logic bugs -- driver_shifts did exactly
# that, leaving one test's clock-in visible to the next.
TRUNCATABLE = (TABLE_MANIFEST_ROWS, TABLE_MANIFESTS,
               TABLE_RM_LOAD_ITEMS, TABLE_RM_LOADS, TABLE_SHIFTS,
               TABLE_SHUTTLE_LEGS, TABLE_LOCATION_CODES, TABLE_DRIVERS)


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
                           (TABLE_DRIVERS, ADDITIVE_DRIVER_COLUMNS),
                           (TABLE_SHIFTS, ADDITIVE_SHIFT_COLUMNS)):
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

    # `trip_seq` duplicated `round_number` and is gone. The additive list above
    # only ADDS columns, so a database that already has the stray column needs
    # its own idempotent migration to be rid of it.
    await cur.execute(
        """SELECT COUNT(*) FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s AND column_name = 'trip_seq';""",
        (db_name, TABLE_SHUTTLE_LEGS),
    )
    (trip_seq_exists,) = await cur.fetchone()
    if trip_seq_exists:
        await cur.execute(f"ALTER TABLE {TABLE_SHUTTLE_LEGS} DROP COLUMN trip_seq;")
        if logger:
            logger.info("Dropped redundant column 'trip_seq' from shuttle_legs.")

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
