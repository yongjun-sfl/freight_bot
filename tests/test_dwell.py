"""Facility dwell -- the transaction overhead that actually differentiates.

Transit times are met by everyone; time inside a facility is not. These pin
the escalation behaviour and the arrival-reporting caveat that any report
built on this data has to carry.
"""

from datetime import timedelta

import pytest

import dwell
from conftest import DRIVER_ID, commit, eastern_now, get_leg, insert_leg, intent, ts


def test_thresholds_escalate():
    assert dwell.level_for(0) is None
    assert dwell.level_for(19) is None
    assert dwell.level_for(20)[0] == 20
    assert dwell.level_for(29)[0] == 20
    assert dwell.level_for(30)[0] == 30
    assert dwell.level_for(39)[0] == 30
    assert dwell.level_for(40)[0] == 40
    assert dwell.level_for(120)[0] == 40


def _collector():
    """(list, async callable) -- sweep_dwells awaits the card sender."""
    cards = []

    async def send(text):
        cards.append(text)

    return cards, send


async def _sit_at_facility(pool, minutes_ago, **kw):
    arrived = eastern_now() - timedelta(minutes=minutes_ago)
    return await insert_leg(
        pool, origin_location="SDS", destination_location="200F",
        departure_time=ts(arrived - timedelta(minutes=30)),
        arrival_time=ts(arrived), leg_status="COMPLETED", **kw,
    )


async def test_driver_under_the_threshold_is_not_reported(pool):
    await _sit_at_facility(pool, 12)
    cards, send = _collector()
    assert await dwell.sweep_dwells(pool, send) == 0
    assert cards == []


async def test_card_raised_once_per_level(pool):
    leg = await _sit_at_facility(pool, 22)
    cards, send = _collector()
    assert await dwell.sweep_dwells(pool, send) == 1
    assert "WATCH" in cards[0] and "22 min" in cards[0]

    # a second sweep at the same level must stay quiet
    assert await dwell.sweep_dwells(pool, send) == 0
    assert (await get_leg(pool, leg))["dwell_alert_level"] == 20


async def test_escalation_reports_again(pool):
    leg = await _sit_at_facility(pool, 22)
    cards, send = _collector()
    await dwell.sweep_dwells(pool, send)

    # the driver is still there half an hour later
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE shuttle_legs SET arrival_time = %s WHERE id = %s;",
                (ts(eastern_now() - timedelta(minutes=42)), leg),
            )
    assert await dwell.sweep_dwells(pool, send) == 1
    assert "CRITICAL" in cards[1]
    assert (await get_leg(pool, leg))["dwell_alert_level"] == 40


async def test_departure_ends_the_dwell(pool):
    await _sit_at_facility(pool, 35)
    await commit(
        pool,
        intent(case_type="CASE_1_ORIGIN_DEPARTURE", origin_location="200F",
               destination_location="SDS", text_trailer="77344",
               load_status="EMPTY"),
    )
    cards, send = _collector()
    assert await dwell.sweep_dwells(pool, send) == 0, "driver has left"


async def test_dock_move_does_not_end_a_dwell(pool):
    """An empty move to enable a hook happens DURING the transaction."""
    await _sit_at_facility(pool, 25)
    await commit(
        pool,
        intent(case_type="CASE_3_INTRA_FACILITY_MOVE",
               origin_dock="13", destination_dock="47", load_status="EMPTY"),
    )
    cards, send = _collector()
    assert await dwell.sweep_dwells(pool, send) == 1


async def test_card_distinguishes_waiting_from_finished(pool):
    await _sit_at_facility(pool, 25)
    cards, send = _collector()
    await dwell.sweep_dwells(pool, send)
    assert "No signed paperwork yet" in cards[0]


async def test_card_notes_paperwork_already_signed(pool):
    await _sit_at_facility(
        pool, 25, paperwork_time=ts(eastern_now() - timedelta(minutes=5)))
    cards, send = _collector()
    await dwell.sweep_dwells(pool, send)
    assert "Paperwork signed" in cards[0]


async def test_card_warns_when_arrival_was_reported_at_a_door(pool):
    """Reporting arrival after docking understates dwell. The card has to say
    so, or the number reads as better performance than it is."""
    await _sit_at_facility(pool, 25, arrival_at_dock=1)
    cards, send = _collector()
    await dwell.sweep_dwells(pool, send)
    assert "likely longer" in cards[0]


async def test_stale_arrivals_are_ignored(pool):
    """A driver who never reported departing yesterday is not still on site."""
    await _sit_at_facility(pool, 60 * 20)
    cards, send = _collector()
    assert await dwell.sweep_dwells(pool, send) == 0
