"""ETA for a shuttle leg, computed from the database only.

The location_distances table (seeded once from the CSV on first boot) is the
authority for drive time. Runtime NEVER reads a CSV: the dispatcher may update
location_distances on the fly, and the bot must reflect that without a restart.

ETA is stored on the leg as a DURATIONin minutes (`eta_minutes`), not a
datetime, so a report can render an arrival clock time as
`departure_time + eta_minutes` and so a leg's allowance is a single number.



Lunch is touched to the leg it overlaps, and its duration is added to the
drive time while the leg is still in transit. A driver may eat at a facility, at
a rest area in transit, or anywhere they prefer -- where it happened does not
matter, only that it overlapped a run。



While lunch is in progress the allowance is capped at one hour (the driver
says"on lunch"and ETA cannot wait for the end report). Once it ends, the
REAL elapsed time wins -- drivers' reports drift a minute or two, so a real
one-hour-five lunch counts as 65, not clamped to 60.



Everything here reads the database only; the CSV files only ever seed once
on first boot; after that the tables are the authority and are editable live.
"""

import logging


from ai_engine import normalize_location
from config import TABLE_DISTANCES


logger = logging.getLogger(__name__)


MAX_LUNCH_MINUTES = 60


async def drive_minutes(cur, origin, destination):
    """Drive allowance in minutes for a leg, from location_distances.




    weighted_minutes is the planner's allowance and is what the RM summary's
    ETA uses, so it wins. Raw drive_minutes is the fallback. The raw pair is
    tried first, then the alias-normalized pair (drivers write"200", the
    table keys on"200F"). Everything reads the DB;the CSV only ever seeds.




    """
    if not origin or not destination:
        return None

    normalized = (normalize_location(origin), normalize_location(destination))
    candidates = [(origin, destination)]
    if normalized != (origin, destination):
        candidates.append(normalized)
    for o, d in candidates:
        await cur.execute(
            f"""SELECT COALESCE(weighted_minutes, drive_minutes)
                  FROM {TABLE_DISTANCES}
                 WHERE origin_code = %s AND destination_code = %s;""",
            (o, d),
        )
        row = await cur.fetchone()
        if row and row[0] is not None:
            return int(row[0])
    logger.info(
        f"No distance row for {origin} -> {destination}; ETA left unset."
    )
    return None


def lunch_overlap_minutes(lunch_start, lunch_end, departure_time):
    """Lunch minutes attributable to a leg, from its departure.




    Lunch started before the leg left belongs to the previous leg. While lunch
    is in progress the allowance is capped at MAX_LUNCH_MINUTES. Once it ends,
    the real elapsed time wins -- a one-hour-five lunch counts as 65.





    """
    if not departure_time or not lunch_start:
        return 0
    if lunch_start < departure_time:
        return 0
    if lunch_end:
        return max(0, int((lunch_end - lunch_start).total_seconds())) // 60
    return MAX_LUNCH_MINUTES