"""Facility dwell time -- the transaction overhead that actually differentiates.

Transit times are met by everyone, so the number worth watching is how long a
driver sits inside a facility: live load/unload, drop and hook, the empty moves
that make a hook possible, and waiting on a warehouse clerk to sign the BOL.

Dwell runs from the arrival report to the next departure. It cannot be measured
by waiting for the departure -- by then the overhead has already happened -- so
a timer sweeps open dwells and raises a card as each threshold is crossed.

A caveat that belongs in any report built on this: drivers report arrival at
different moments. Some send it at the gate, some after dropping at a door,
which understates their dwell. arrival_at_dock records which kind an arrival
looked like, so like can be compared with like rather than silently averaged.
"""

import logging

from config import TABLE_DRIVERS, TABLE_SHIFTS, TABLE_SHUTTLE_LEGS  # noqa: F401

logger = logging.getLogger(__name__)

# Minutes inside a facility. Escalating, and each fires at most once per dwell.
WATCH_MINUTES = 20
DANGER_MINUTES = 30
CRITICAL_MINUTES = 40

LEVELS = (
    (CRITICAL_MINUTES, "🔴 CRITICAL"),
    (DANGER_MINUTES, "🟠 DANGER"),
    (WATCH_MINUTES, "🟡 WATCH"),
)


def level_for(minutes: int):
    """(threshold, label) for an elapsed dwell, or None if under the first."""
    for threshold, label in LEVELS:
        if minutes >= threshold:
            return threshold, label
    return None


async def open_dwells(pool):
    """Drivers currently sat at a facility, with minutes elapsed.

    A dwell is open when a driver's most recent trip leg has an arrival but no
    departure has followed it. Positioning legs are excluded from being the
    marker -- a dock move happens *during* a dwell, it does not end one.
    """
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""SELECT l.id,
                           l.user_id,
                           d.driver_name,
                           l.destination_location,
                           l.arrival_time,
                           l.paperwork_time,
                           l.arrival_at_dock,
                           l.dwell_alert_level,
                           TIMESTAMPDIFF(MINUTE, l.arrival_time, NOW()) AS minutes,
                           -- Lunch is reported at both ends, so it is
                           -- subtracted rather than guessed at. A driver
                           -- still on lunch is not delayed at all.
                           s.lunch_start,
                           s.lunch_end
                      FROM {TABLE_SHUTTLE_LEGS} l
                      JOIN {TABLE_DRIVERS} d ON d.user_id = l.user_id
                      LEFT JOIN {TABLE_SHIFTS} s
                             ON s.user_id = l.user_id
                            AND s.shift_date = DATE(l.arrival_time)
                     WHERE l.is_positioning_leg = 0
                       AND l.arrival_time IS NOT NULL
                       AND l.id = (
                           SELECT MAX(id) FROM {TABLE_SHUTTLE_LEGS}
                            WHERE user_id = l.user_id AND is_positioning_leg = 0
                       )
                       AND NOT EXISTS (
                           SELECT 1 FROM {TABLE_SHUTTLE_LEGS} nxt
                            WHERE nxt.user_id = l.user_id
                              AND nxt.id > l.id
                              AND nxt.is_positioning_leg = 0
                       )
                       AND l.arrival_time >= NOW() - INTERVAL 12 HOUR
                       -- A driver who has clocked out is not on site. Without
                       -- this they climb past every threshold overnight and
                       -- get reported as critically delayed while at home.
                       AND NOT EXISTS (
                           SELECT 1 FROM {TABLE_SHIFTS} s
                            WHERE s.user_id = l.user_id
                              AND s.shift_date = DATE(l.arrival_time)
                              AND s.reported_clock_out IS NOT NULL
                       );"""
            )
            return await cur.fetchall()


async def record_alert(pool, leg_id: int, threshold: int):
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""UPDATE {TABLE_SHUTTLE_LEGS}
                       SET dwell_alert_level = %s
                     WHERE id = %s;""",
                (threshold, leg_id),
            )


def dwell_card(driver_name, facility, minutes, label, paperwork_time,
               arrival_at_dock, maybe_lunch=False) -> str:
    if paperwork_time:
        detail = "📄 Paperwork signed — the delay is after the work finished."
    else:
        detail = "📄 No signed paperwork yet — still loading, unloading or waiting."

    caveat = ""
    if arrival_at_dock:
        caveat = ("\nℹ️ Arrival was reported at a door, so real time on site "
                  "is likely longer than this.")
    if maybe_lunch:
        caveat += "\n🍽 May include their lunch hour."

    return (
        f"{label}: **{minutes} min on site**\n"
        f"👤 Driver: {driver_name}\n"
        f"📍 Facility: `{facility}`\n"
        f"{detail}{caveat}"
    )


async def sweep_dwells(pool, send_card):
    """Check every open dwell and raise a card as each threshold is crossed.

    Returns the number of cards sent, so the caller can log it.
    """
    sent = 0
    for row in await open_dwells(pool):
        (leg_id, _user_id, driver_name, facility, arrival, paperwork_time,
         at_dock, alerted, minutes, lunch_start, lunch_end) = row

        # Still eating: not a delay.
        if lunch_start and not lunch_end:
            continue
        # Lunch taken during this stop does not count against it.
        if lunch_start and lunch_end and arrival and lunch_start >= arrival:
            minutes = (minutes or 0) - max(
                0, int((lunch_end - lunch_start).total_seconds() // 60))

        if minutes is None:
            continue
        current = level_for(int(minutes))
        if not current:
            continue

        threshold, label = current
        if threshold <= (alerted or 0):
            continue          # already reported at this level or worse

        await record_alert(pool, leg_id, threshold)
        await send_card(dwell_card(driver_name, facility, int(minutes), label,
                                   paperwork_time, at_dock,
                                   maybe_lunch=False))
        sent += 1
    return sent
