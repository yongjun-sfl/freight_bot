"""Test fixtures for the shuttle state machine.

Runs against a real MySQL database because the queries under test use
MySQL-specific syntax (``IF()``, ``INTERVAL``, ``CURRENT_DATE()``, ENUMs).
Point it at a throwaway schema -- the harness refuses to run unless the
database name ends in ``_test``.
"""

import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiomysql
import pytest
import pytest_asyncio

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bot"))

import ai_engine  # noqa: E402
from config import TABLE_DRIVERS, TABLE_LOCATION_CODES, TABLE_SHUTTLE_LEGS  # noqa: E402
from schema_ddl import TRUNCATABLE, apply_schema  # noqa: E402

EASTERN = ZoneInfo("America/New_York")

MYSQL_HOST = os.getenv("TEST_MYSQL_HOST", "mysql_db")
MYSQL_PORT = int(os.getenv("TEST_MYSQL_PORT", 3306))
MYSQL_USER = os.getenv("TEST_MYSQL_USER", "root")
MYSQL_PASSWORD = os.getenv("TEST_MYSQL_PASSWORD", "")
MYSQL_DB = os.getenv("TEST_MYSQL_DATABASE", "shuttle_db_test")

DRIVER_ID = 555001
DRIVER_NAME = "Test Driver"

if not MYSQL_DB.endswith("_test"):
    raise RuntimeError(
        f"Refusing to run: TEST_MYSQL_DATABASE is {MYSQL_DB!r}, which does not end "
        "in '_test'. This guard exists so the suite can never truncate production data."
    )


def eastern_now() -> datetime:
    """Wall-clock Eastern time, matching handlers.get_eastern_timestamp."""
    return datetime.now(EASTERN).replace(tzinfo=None)


def ts(dt: datetime = None) -> str:
    return (dt or eastern_now()).strftime("%Y-%m-%d %H:%M:%S")


@pytest_asyncio.fixture
async def pool():
    """A fresh aiomysql pool against an empty test schema.

    Session timezone is pinned to Eastern to match production, so the
    ``DATE(departure_time) = CURRENT_DATE()`` clauses agree with timestamps
    the tests generate.
    """
    bootstrap = await aiomysql.connect(
        host=MYSQL_HOST, port=MYSQL_PORT, user=MYSQL_USER,
        password=MYSQL_PASSWORD, autocommit=True,
    )
    async with bootstrap.cursor() as cur:
        await cur.execute(
            f"CREATE DATABASE IF NOT EXISTS {MYSQL_DB} "
            "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
        )
    bootstrap.close()

    p = await aiomysql.create_pool(
        host=MYSQL_HOST, port=MYSQL_PORT, user=MYSQL_USER,
        password=MYSQL_PASSWORD, db=MYSQL_DB, autocommit=True,
        init_command="SET time_zone = 'America/New_York';",
        minsize=1, maxsize=5,
    )

    async with p.acquire() as conn:
        async with conn.cursor() as cur:
            await apply_schema(cur, MYSQL_DB)
            await cur.execute("SET FOREIGN_KEY_CHECKS = 0;")
            for table in TRUNCATABLE:
                await cur.execute(f"TRUNCATE TABLE {table};")
            await cur.execute("SET FOREIGN_KEY_CHECKS = 1;")
            await cur.execute(
                f"INSERT INTO {TABLE_DRIVERS} (user_id, driver_name) VALUES (%s, %s);",
                (DRIVER_ID, DRIVER_NAME),
            )

    try:
        yield p
    finally:
        p.close()
        await p.wait_closed()


@pytest.fixture(autouse=True)
def clean_location_cache():
    """Reset the module-global location cache around every test.

    ai_engine.LOCATION_CACHE is mutated in place (state_machine holds a direct
    reference to normalize_location), so rebinding the name would not work.
    """
    original_codes = list(ai_engine.LOCATION_CACHE["codes"])
    original_aliases = dict(ai_engine.LOCATION_CACHE["alias_map"])
    ai_engine.LOCATION_CACHE["codes"] = []
    ai_engine.LOCATION_CACHE["alias_map"] = {}
    yield ai_engine.LOCATION_CACHE
    ai_engine.LOCATION_CACHE["codes"] = original_codes
    ai_engine.LOCATION_CACHE["alias_map"] = original_aliases


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

LEG_DEFAULTS = {
    "user_id": DRIVER_ID,
    "trailer_number": None,
    "bol_number": None,
    "document_type": "UNKNOWN",
    "bol_image": None,
    "load_status": "LOADED",
    "origin_location": "200",
    "destination_location": "E2F",
    "departure_time": None,
    "arrival_time": None,
    "arrival_action": None,
    "dock_number": None,
    "shipper_signed": 0,
    "receiver_signed": 0,
    "is_bobtail": 0,
    "leg_status": "IN_TRANSIT",
}


async def insert_leg(pool, **overrides) -> int:
    """Insert a leg row directly, bypassing the state machine. Returns its id."""
    row = {**LEG_DEFAULTS, **overrides}
    if row["departure_time"] is None:
        row["departure_time"] = ts()

    cols = ", ".join(row)
    marks = ", ".join(["%s"] * len(row))
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"INSERT INTO {TABLE_SHUTTLE_LEGS} ({cols}) VALUES ({marks});",
                tuple(row.values()),
            )
            return cur.lastrowid


async def get_leg(pool, leg_id: int) -> dict:
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                f"SELECT * FROM {TABLE_SHUTTLE_LEGS} WHERE id = %s;", (leg_id,)
            )
            return await cur.fetchone()


async def all_legs(pool) -> list:
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(f"SELECT * FROM {TABLE_SHUTTLE_LEGS} ORDER BY id;")
            return list(await cur.fetchall())


async def seed_locations(pool, rows):
    """rows: iterable of (canonical_code, aliases_csv). Also refreshes the cache."""
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            for code, aliases in rows:
                await cur.execute(
                    f"INSERT INTO {TABLE_LOCATION_CODES} (canonical_code, aliases) "
                    "VALUES (%s, %s);",
                    (code, aliases),
                )
    await ai_engine.refresh_location_cache(pool)


def intent(**overrides) -> dict:
    """Build an intent dict shaped like ai_engine.prepare_*_intent output."""
    base = {
        "case_type": "NONE_WORK_RELATED",
        "raw_text": "",
        "text_trailer": None,
        "ocr_trailer": None,
        "bol_number": None,
        "document_type": "UNKNOWN",
        "origin_location": None,
        "destination_location": None,
        "door_number": None,
        "action": None,
        "load_status": None,
        "shipper_signed": False,
        "receiver_signed": False,
        "primary_image_blob": None,
    }
    base.update(overrides)
    return base


async def commit(pool, intent_dict, when: str = None, did: int = DRIVER_ID) -> dict:
    """Invoke the state machine with the boilerplate args filled in."""
    from state_machine import commit_trip_leg

    return await commit_trip_leg(
        p=pool,
        did=did,
        user_name=DRIVER_NAME,
        group_title="Test Group",
        orig_chat_id=-100123,
        orig_msg_id=1,
        msg_timestamp=when or ts(),
        intent=intent_dict,
    )
