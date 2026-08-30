"""Driver shifts: expected against reported.

The dispatcher sets the expected times; the bot records what drivers report.
The two are never merged. An inferred start sitting in a reported column is
indistinguishable from an observation once anyone else reads it, and these
feed the hours the dispatcher works out overtime from.

A driver's first message of the day is NOT their clock-in. It comes after
they have arrived, hooked up and started moving -- easily half an hour later
-- so inferring from it would understate every shift.
"""

import logging

from config import TABLE_SHIFTS

logger = logging.getLogger(__name__)


async def record_clock_in(cur, user_id, when):
    """First report wins: a repeated clock-in must not move the start time."""
    await cur.execute(
        f"""INSERT INTO {TABLE_SHIFTS} (user_id, shift_date, reported_clock_in)
            VALUES (%s, DATE(%s), %s)
            ON DUPLICATE KEY UPDATE
                reported_clock_in = COALESCE(reported_clock_in, VALUES(reported_clock_in));""",
        (user_id, when, when),
    )
    await cur.execute(
        f"""SELECT reported_clock_in, expected_clock_in FROM {TABLE_SHIFTS}
             WHERE user_id = %s AND shift_date = DATE(%s);""",
        (user_id, when),
    )
    return await cur.fetchone()


async def record_clock_out(cur, user_id, when):
    """Last report wins: a driver correcting their finish should update it."""
    await cur.execute(
        f"""INSERT INTO {TABLE_SHIFTS} (user_id, shift_date, reported_clock_out)
            VALUES (%s, DATE(%s), %s)
            ON DUPLICATE KEY UPDATE
                reported_clock_out = VALUES(reported_clock_out);""",
        (user_id, when, when),
    )
    await cur.execute(
        f"""UPDATE {TABLE_SHIFTS}
               SET worked_minutes = TIMESTAMPDIFF(
                       MINUTE, reported_clock_in, reported_clock_out)
             WHERE user_id = %s AND shift_date = DATE(%s)
               AND reported_clock_in IS NOT NULL;""",
        (user_id, when),
    )
    await cur.execute(
        f"""SELECT reported_clock_in, reported_clock_out, worked_minutes
              FROM {TABLE_SHIFTS}
             WHERE user_id = %s AND shift_date = DATE(%s);""",
        (user_id, when),
    )
    return await cur.fetchone()


async def record_lunch(cur, user_id, when, boundary):
    """Record going on or coming off lunch. Returns (start, end, minutes)."""
    column = "lunch_start" if boundary == "START" else "lunch_end"
    await cur.execute(
        f"""INSERT INTO {TABLE_SHIFTS} (user_id, shift_date, {column})
            VALUES (%s, DATE(%s), %s)
            ON DUPLICATE KEY UPDATE {column} = VALUES({column});""",
        (user_id, when, when),
    )
    await cur.execute(
        f"""UPDATE {TABLE_SHIFTS}
               SET lunch_minutes = TIMESTAMPDIFF(MINUTE, lunch_start, lunch_end)
             WHERE user_id = %s AND shift_date = DATE(%s)
               AND lunch_start IS NOT NULL AND lunch_end IS NOT NULL;""",
        (user_id, when),
    )
    await cur.execute(
        f"""SELECT lunch_start, lunch_end, lunch_minutes FROM {TABLE_SHIFTS}
             WHERE user_id = %s AND shift_date = DATE(%s);""",
        (user_id, when),
    )
    return await cur.fetchone()


async def is_clocked_out(cur, user_id) -> bool:
    """Whether the driver has finished for the day.

    Dwell is measured from an arrival to the next departure, so a driver who
    goes home mid-dwell would otherwise climb past every threshold overnight
    and be reported as critically delayed while asleep.
    """
    await cur.execute(
        f"""SELECT reported_clock_out IS NOT NULL FROM {TABLE_SHIFTS}
             WHERE user_id = %s AND shift_date = CURRENT_DATE();""",
        (user_id,),
    )
    row = await cur.fetchone()
    return bool(row and row[0])


def format_worked(minutes) -> str:
    if not minutes or minutes < 0:
        return ""
    return f"{minutes // 60}h {minutes % 60:02d}m"
