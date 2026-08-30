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

from config import (TABLE_DISTANCES, TABLE_DRIVERS, TABLE_LOCATION_CODES,
                    TABLE_LOCATION_DOCKS)

logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
CSV_PATH = os.path.join(DATA_DIR, "location_distance.csv")
LOCATIONS_PATH = os.path.join(DATA_DIR, "locations.csv")
DOCKS_PATH = os.path.join(DATA_DIR, "docks.csv")
DRIVERS_PATH = os.path.join(DATA_DIR, "drivers.csv")

def read_locations_csv(path=LOCATIONS_PATH):
    """Facility list with official names, addresses, sites and active flags.

    official_name is what the client calls the place ("EAGLE 2 FRONT"); the
    code is our shorthand. Reports aimed at their accounting team should use
    the former. site_code groups front/rear pairs that share an address, which
    is what lets a round opened at 200R close on return to 200F.
    Doors are not here: they live in docks.csv, because one facility has
    several bands with different uses.
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
                "owner": (row.get("owner") or "").strip() or None,
                "site": (row.get("site_code") or "").strip().upper() or code,
                "aliases": aliases or None,
                "active": (row.get("is_active") or "YES").strip().upper() == "YES",
            })
    return rows


def read_docks_csv(path=DOCKS_PATH):
    """Door bands per facility: which doors, what they are for, who works them.

    Doors are numbered in bands and the band carries the meaning. At 200 the
    front building runs 3-21 FG inbound and 22-45 RM inbound (Hanjin's, not
    ours); the rear runs 47-66 RM outbound and 67-99 FG outbound for the 7634
    lane. A driver naming only a door is therefore naming a building, which is
    what lets "#3 to #47" read as 200F -> 200R.

    Bands are the dispatcher's own numbers and some upper bounds are
    approximate, so a door outside every band is left unresolved rather than
    forced into the nearest one.
    """
    if not os.path.exists(path):
        return []
    bands = []
    with open(path, newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            facility = (row.get("facility") or "").strip().upper()
            first = (row.get("first_dock") or "").strip()
            last = (row.get("last_dock") or "").strip()
            if not (facility and first.isdigit() and last.isdigit()):
                if facility or first or last:
                    logger.warning(f"Skipping unreadable dock band {row!r}.")
                continue
            bands.append({
                "facility": facility,
                "first": int(first),
                "last": int(last),
                "use": (row.get("use") or "").strip().upper() or None,
                "operator": (row.get("operator") or "").strip().upper() or None,
                "note": (row.get("note") or "").strip() or None,
            })
    return bands


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


def read_drivers_csv(path=DRIVERS_PATH):
    """The driver roster: Korean name, English name, short name, plate, repo.

    telegram_user_id is usually blank. It appears in no export -- it is
    Telegram's own account id, learned only when someone messages -- so the
    roster loads without it and ids attach later via /roster.
    """
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            english = (row.get("name_eng") or "").strip()
            korean = (row.get("name_kor") or "").strip()
            if not (english or korean):
                continue
            raw_id = (row.get("telegram_user_id") or "").strip()
            rows.append({
                "user_id": int(raw_id) if raw_id.isdigit() else None,
                "name_kor": korean or None,
                "name_eng": english or None,
                "short_name": (row.get("short_name") or "").strip() or None,
                "truck_plate": (row.get("truck_plate") or "").strip() or None,
                "home_repo": (row.get("home_repo") or "").strip().upper() or None,
                # Case-folded: the export mixes Y and y.
                "active": (row.get("active") or "Y").strip().upper() == "Y",
                "display": english or korean,
            })
    return rows


def name_forms(driver) -> set:
    """Every way a driver might be written in a schedule post."""
    forms = set()
    for value in (driver.get("name_kor"), driver.get("name_eng"),
                  driver.get("short_name"), driver.get("display")):
        if value:
            forms.add(value.strip().upper())
    return forms


async def seed_drivers(cur):
    """Load the roster if driver_profiles is empty. Returns rows inserted."""
    drivers = read_drivers_csv()
    if not drivers:
        return 0
    await cur.execute(f"SELECT COUNT(*) FROM {TABLE_DRIVERS};")
    (existing,) = await cur.fetchone()
    if existing:
        return 0

    for d in drivers:
        await cur.execute(
            f"""INSERT INTO {TABLE_DRIVERS}
                    (user_id, driver_name, name_kor, name_eng, short_name,
                     truck_plate, home_repo, is_active)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s);""",
            (d["user_id"], d["display"], d["name_kor"], d["name_eng"],
             d["short_name"], d["truck_plate"], d["home_repo"], d["active"]),
        )
    missing = sum(1 for d in drivers if d["user_id"] is None)
    if missing:
        logger.warning(
            f"{missing} of {len(drivers)} drivers have no Telegram id and will be "
            f"ignored by the state machine until one is set. Use /roster."
        )
    return len(drivers)


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
                         location_type, owner, official_name, is_active)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s);""",
                (row["code"], row["aliases"], row["site"], row["address"],
                 row["type"], row["owner"], row["official_name"], row["active"]),
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

    await cur.execute(f"SELECT COUNT(*) FROM {TABLE_LOCATION_DOCKS};")
    (has_docks,) = await cur.fetchone()
    if not has_docks:
        known = {row["code"] for row in read_locations_csv()}
        for band in read_docks_csv():
            if band["facility"] not in known:
                logger.warning(
                    f"Dock band {band['first']}-{band['last']} names "
                    f"{band['facility']}, which is not a facility."
                )
                continue
            await cur.execute(
                f"""INSERT INTO {TABLE_LOCATION_DOCKS}
                        (facility_code, first_dock, last_dock, dock_use,
                         operator, note)
                    VALUES (%s, %s, %s, %s, %s, %s);""",
                (band["facility"], band["first"], band["last"],
                 band["use"], band["operator"], band["note"]),
            )

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
