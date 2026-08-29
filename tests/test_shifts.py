"""Shift boundaries and live load/unload completion."""

from datetime import timedelta

import aiomysql
import pytest

import dwell
from conftest import (DRIVER_ID, commit, eastern_now, get_leg, insert_leg,
                      intent, seed_network, ts)


async def _shift(pool):
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("SELECT * FROM driver_shifts WHERE user_id = %s;",
                              (DRIVER_ID,))
            return await cur.fetchone()


# --------------------------------------------------------------------------
# clock in / out
# --------------------------------------------------------------------------

async def test_clock_in_is_recorded_and_acknowledged(pool):
    res = await commit(pool, intent(case_type="CASE_CLOCK_IN", raw_text="clock in"))
    assert res["is_clean"] is True
    assert res["reply_text"].startswith("✅ Clocked in —")
    assert (await _shift(pool))["reported_clock_in"] is not None


async def test_repeated_clock_in_does_not_move_the_start(pool):
    first = await commit(pool, intent(case_type="CASE_CLOCK_IN"),
                         when=ts(eastern_now() - timedelta(hours=2)))
    await commit(pool, intent(case_type="CASE_CLOCK_IN"))
    assert (await _shift(pool))["reported_clock_in"].strftime("%H:%M") in first["reply_text"]


async def test_clock_out_reports_hours_worked(pool):
    await commit(pool, intent(case_type="CASE_CLOCK_IN"),
                 when=ts(eastern_now() - timedelta(hours=9, minutes=36)))
    res = await commit(pool, intent(case_type="CASE_CLOCK_OUT"))
    assert "9h 36m" in res["reply_text"]
    assert (await _shift(pool))["worked_minutes"] == 576


async def test_clock_out_without_clock_in_is_flagged(pool):
    """Hours cannot be worked out, so it is surfaced rather than left blank."""
    res = await commit(pool, intent(case_type="CASE_CLOCK_OUT"))
    assert res["is_clean"] is False
    assert "No Clock-in" in res["card_text"]
    assert res["reply_text"].startswith("✅ Clocked out —")


async def test_expected_times_are_never_written_by_the_bot(pool):
    """The dispatcher owns expected; the bot owns reported. Merging them would
    make an inferred time indistinguishable from an observation."""
    await commit(pool, intent(case_type="CASE_CLOCK_IN"))
    row = await _shift(pool)
    assert row["expected_clock_in"] is None
    assert row["expected_clock_out"] is None


async def test_lateness_is_reported_against_an_expected_start(pool):
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO driver_shifts (user_id, shift_date, expected_clock_in) "
                "VALUES (%s, CURRENT_DATE(), %s);",
                (DRIVER_ID, ts(eastern_now() - timedelta(minutes=25))))
    res = await commit(pool, intent(case_type="CASE_CLOCK_IN"))
    assert "after expected" in res["reply_text"]


# --------------------------------------------------------------------------
# live load / unload finished
# --------------------------------------------------------------------------

async def test_standalone_finish_stamps_the_current_leg(pool):
    await seed_network(pool)
    leg = await insert_leg(pool, destination_location="E2F",
                           leg_status="UNLOADING")
    res = await commit(
        pool, intent(case_type="CASE_WORK_FINISHED",
                     raw_text="live unloading finished"))
    assert res["leg_id"] == leg
    assert (await get_leg(pool, leg))["finished_time"] is not None


async def test_merged_finish_and_departure_stamps_the_previous_leg(pool):
    """'live loading finished load 200 to E2F' ends one trip and starts the
    next. The completion belongs to the leg being closed, not the new one."""
    await seed_network(pool)
    previous = await insert_leg(pool, origin_location="SDS",
                                destination_location="200F",
                                leg_status="UNLOADING")
    res = await commit(
        pool,
        intent(case_type="CASE_1_ORIGIN_DEPARTURE", work_finished=True,
               raw_text="live loading finished load 200 to E2F",
               origin_location="200F", destination_location="E2F",
               text_trailer="77344", bol_number="B-OUT",
               primary_image_blob=b"x", load_status="LOADED"),
    )
    assert (await get_leg(pool, previous))["finished_time"] is not None
    assert (await get_leg(pool, res["leg_id"]))["finished_time"] is None


async def test_finish_computes_time_taken_on_an_rm_load(pool):
    """Time Taken on the RM summary is Finished minus Arrival."""
    await seed_network(pool)
    await commit(pool, intent(
        case_type="CASE_1_ORIGIN_DEPARTURE", origin_location="200R",
        destination_location="E2F", text_trailer="77168", bol_number="926761",
        document_type="RM", rm_seq="7", primary_image_blob=b"x",
        load_status="LOADED", materials=[{"material_code": "11800335"}]))
    await commit(pool, intent(case_type="CASE_2_DESTINATION_ARRIVAL",
                              destination_location="E2F"))
    await commit(pool, intent(case_type="CASE_WORK_FINISHED"))

    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("SELECT * FROM rm_loads;")
            load = await cur.fetchone()
    assert load["finished_time"] is not None
    assert load["time_taken_minutes"] is not None


# --------------------------------------------------------------------------
# dwell interaction
# --------------------------------------------------------------------------

async def test_dwell_stops_once_the_driver_clocks_out(pool):
    """Otherwise a driver who goes home mid-dwell climbs past every threshold
    overnight and is reported as critically delayed while asleep."""
    arrived = eastern_now() - timedelta(minutes=90)
    await insert_leg(pool, destination_location="200F",
                     departure_time=ts(arrived - timedelta(minutes=30)),
                     arrival_time=ts(arrived), leg_status="COMPLETED")

    cards = []

    async def send(text):
        cards.append(text)

    assert await dwell.sweep_dwells(pool, send) == 1, "still on site"

    cards.clear()
    await commit(pool, intent(case_type="CASE_CLOCK_OUT"))
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("UPDATE shuttle_legs SET dwell_alert_level = 0;")
    assert await dwell.sweep_dwells(pool, send) == 0, "gone home"
