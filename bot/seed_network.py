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

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
CSV_PATH = os.path.join(DATA_DIR, "location_distance.csv")
LOCATIONS_PATH = os.path.join(DATA_DIR, "locations.csv")

def read_locations_csv(path=LOCATIONS_PATH):
    """Facility list with official names, addresses, sites and active flags.

    official_name is what the client calls the place ("EAGLE 2 FRONT"); the
    code is our shorthand. Reports aimed at their accounting team should use
    the former. site_code groups front/rear pairs that share an address, which
    is what lets a round opened at 200R close on return to 200F.
    """
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            code = (row.get("code") or "").strip().upper()
            if not code:
                continue
            aliases = (row.get("aliases") or "").replace("|", ",").strip(",")
            rows.append({
                "code": code,
                "official_name": (row.get("official_name") or "").strip() or None,
                "address": (row.get("address") or "").strip() or None,
                "type": (row.get("type") or "").strip() or None,
                "site": (row.get("site_code") or "").strip().upper() or code,
                "aliases": aliases or None,
                "active": (row.get("is_active") or "YES").strip().upper() == "YES",
            })
    return rows


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
        defined = read_locations_csv()
        seen = set()
        for row in defined:
            seen.add(row["code"])
            await cur.execute(
                f"""INSERT INTO {TABLE_LOCATION_CODES}
                        (canonical_code, aliases, site_code, address,
                         location_type, official_name, is_active)
                    VALUES (%s, %s, %s, %s, %s, %s, %s);""",
                (row["code"], row["aliases"], row["site"], row["address"],
                 row["type"], row["official_name"], row["active"]),
            )
        # Anything in the distance export but absent from the facility list
        # still needs a row, or normalize_location will not resolve it.
        for code in codes:
            if code in seen:
                continue
            await cur.execute(
                f"""INSERT INTO {TABLE_LOCATION_CODES}
                        (canonical_code, site_code) VALUES (%s, %s);""",
                (code, code),
            )
            logger.warning(f"{code} has distances but no facility record.")
        seeded_locations = len(seen) + len(set(codes) - seen)

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
