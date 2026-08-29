"""Seed facility codes and the distance matrix from the dispatcher's export.

data/location_distance.csv is the sheet already used for ETA calculation. It
is the authoritative list of facility codes, so location_codes is seeded from
it too -- that table has been empty in production, which is why
normalize_location currently passes every code through untouched.

Everything here is idempotent and skipped once the tables hold rows, so the
database stays the source of truth after first start.
"""

import csv
import logging
import os

from config import TABLE_DISTANCES, TABLE_LOCATION_CODES

logger = logging.getLogger(__name__)

CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "data", "location_distance.csv")

# Codes sharing a physical site. 200F (front, FG/RM inbound) and 200R (rear,
# FG/RM outbound) are one yard for deciding whether a round has closed.
SITE_OVERRIDES = {"200F": "200", "200R": "200"}

# Spoken shorthand -> canonical code. A bare "200" means the front.
ALIAS_OVERRIDES = {"200F": "200,200 FRONT", "200R": "200 REAR"}


def read_distance_csv(path=CSV_PATH):
    """Parse the export into (codes, pairs), tolerating its quirks.

    The header misspells Destination, three rows are duplicated, and E2F->SDS
    has no reverse. Duplicates collapse; missing reverses are mirrored, since
    every other pair in the file is symmetric.
    """
    if not os.path.exists(path):
        logger.warning(f"No distance export at {path}; skipping network seed.")
        return [], {}

    pairs = {}
    with open(path, newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            values = {(k or "").strip().lower(): (v or "").strip()
                      for k, v in row.items()}
            origin = values.get("origin", "").upper()
            # the export spells it "Desitnation"
            destination = (values.get("destination")
                           or values.get("desitnation") or "").upper()
            if not origin or not destination:
                continue

            def number(key, cast):
                raw = values.get(key, "")
                try:
                    return cast(raw)
                except (TypeError, ValueError):
                    return None

            pairs[(origin, destination)] = (
                number("distance", float),
                number("driving time", int),
                number("weighted dt", float),
            )

    for (origin, destination), value in list(pairs.items()):
        pairs.setdefault((destination, origin), value)

    codes = sorted({code for pair in pairs for code in pair})
    return codes, pairs


async def seed_network(cur):
    """Populate location_codes and location_distances if empty."""
    codes, pairs = read_distance_csv()
    if not codes:
        return 0, 0

    await cur.execute(f"SELECT COUNT(*) FROM {TABLE_LOCATION_CODES};")
    (has_locations,) = await cur.fetchone()
    seeded_locations = 0
    if not has_locations:
        for code in codes:
            await cur.execute(
                f"""INSERT INTO {TABLE_LOCATION_CODES}
                        (canonical_code, aliases, site_code)
                    VALUES (%s, %s, %s);""",
                (code, ALIAS_OVERRIDES.get(code), SITE_OVERRIDES.get(code, code)),
            )
        seeded_locations = len(codes)

    await cur.execute(f"SELECT COUNT(*) FROM {TABLE_DISTANCES};")
    (has_distances,) = await cur.fetchone()
    seeded_pairs = 0
    if not has_distances:
        for (origin, destination), (miles, minutes, weighted) in pairs.items():
            await cur.execute(
                f"""INSERT INTO {TABLE_DISTANCES}
                        (origin_code, destination_code, miles,
                         drive_minutes, weighted_minutes)
                    VALUES (%s, %s, %s, %s, %s);""",
                (origin, destination, miles, minutes, weighted),
            )
        seeded_pairs = len(pairs)

    return seeded_locations, seeded_pairs
