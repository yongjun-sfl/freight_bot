"""RM consignments, shaped to the dispatcher's RM Delivery Summary.

The receiving departments at E2F and E2R report on what arrived, not merely
that a trailer moved: material, quantity, and the batch number their own
manager issued. That detail is on the paperwork rather than in anything a
driver types, so it comes from the BOL image.

Keyed on delivery_date + rm_seq. The sequence is hand-written on each RM BOL
("08/28-7" is load 7 of 28 August) and restarts daily, and one trip can carry
several reservations, so the reservation number is not a key.
"""

import logging

from ai_engine import LOCATION_CACHE
from config import TABLE_DISTANCES, TABLE_RM_LOADS, TABLE_RM_LOAD_ITEMS

logger = logging.getLogger(__name__)


def _seq(value):
    """The numeric part of an RM sequence mark, or None."""
    if value is None:
        return None
    digits = "".join(c for c in str(value) if c.isdigit())
    return int(digits) if digits else None


async def _eta(cur, origin, destination, departure_time):
    """Departure plus the weighted drive time, matching the summary's ETA.

    Weighted rather than raw drive time because that is the allowance the
    dispatcher already plans against in the distance sheet.
    """
    if not (origin and destination and departure_time):
        return None
    await cur.execute(
        f"""SELECT COALESCE(weighted_minutes, drive_minutes)
              FROM {TABLE_DISTANCES}
             WHERE origin_code = %s AND destination_code = %s;""",
        (origin, destination),
    )
    row = await cur.fetchone()
    if not row or row[0] is None:
        return None
    await cur.execute("SELECT %s + INTERVAL %s MINUTE;",
                      (departure_time, int(row[0])))
    return (await cur.fetchone())[0]


def _pod(destination):
    """Destination as the client names it, e.g. EAGLE 2 FRONT."""
    return LOCATION_CACHE.get("official_name", {}).get(destination) or destination


async def record_rm_load(cur, leg_id, intent, origin, destination,
                         trailer, departure_time, driver_id=None,
                         driver_name=None):
    """Write an RM consignment and its line items. Returns the id, or None.

    Without a sequence mark there is no key, so the load is skipped rather
    than filed under a guess -- a wrong seq would collide with a real load.
    """
    if intent.get("document_type") != "RM":
        return None
    seq = _seq(intent.get("rm_seq"))
    if seq is None or not departure_time:
        if intent.get("bol_number"):
            logger.warning(
                f"RM paperwork {intent.get('bol_number')} has no readable "
                f"sequence mark; not recorded on the delivery summary."
            )
        return None

    delivery_date = str(departure_time)[:10]
    eta = await _eta(cur, origin, destination, departure_time)

    await cur.execute(
        f"""INSERT INTO {TABLE_RM_LOADS}
                (delivery_date, rm_seq, leg_id, driver_id, driver_name,
                 reservation_no, trailer_number, dock_number, origin_location,
                 destination_location, pod, departure_time, eta)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                leg_id = VALUES(leg_id),
                driver_id = COALESCE(VALUES(driver_id), driver_id),
                driver_name = COALESCE(VALUES(driver_name), driver_name),
                reservation_no = COALESCE(VALUES(reservation_no), reservation_no),
                trailer_number = COALESCE(VALUES(trailer_number), trailer_number),
                dock_number = COALESCE(VALUES(dock_number), dock_number),
                destination_location = VALUES(destination_location),
                pod = VALUES(pod),
                departure_time = COALESCE(VALUES(departure_time), departure_time),
                eta = COALESCE(VALUES(eta), eta);""",
        (delivery_date, seq, leg_id, driver_id, driver_name,
         intent.get("bol_number"), trailer,
         intent.get("dock_number") or intent.get("door_number"),
         origin, destination, _pod(destination), departure_time, eta),
    )

    await cur.execute(
        f"SELECT id FROM {TABLE_RM_LOADS} WHERE delivery_date = %s AND rm_seq = %s;",
        (delivery_date, seq),
    )
    row = await cur.fetchone()
    if not row:
        return None
    rm_load_id = row[0]

    materials = intent.get("materials") or []
    if materials:
        # Replaced rather than merged: a corrected photo must not leave the
        # superseded lines sitting beside the new ones.
        await cur.execute(
            f"DELETE FROM {TABLE_RM_LOAD_ITEMS} WHERE rm_load_id = %s;", (rm_load_id,)
        )
        for item in materials:
            await cur.execute(
                f"""INSERT INTO {TABLE_RM_LOAD_ITEMS}
                        (rm_load_id, material_code, description, qty, weight,
                         cont_no, batch_no, remark)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s);""",
                (rm_load_id, item.get("material_code"), item.get("description"),
                 item.get("qty"), item.get("weight"), item.get("cont_no"),
                 item.get("batch_no"), item.get("remark")),
            )

    logger.info(
        f"RM load {delivery_date} seq {seq} recorded with {len(materials)} "
        f"line(s) for Leg #{leg_id}."
    )
    return rm_load_id


async def close_rm_load(cur, leg_id, arrival_time):
    """Stamp arrival on any RM load carried by this leg."""
    await cur.execute(
        f"""UPDATE {TABLE_RM_LOADS}
               SET arrival_time = COALESCE(arrival_time, %s),
                   transit_minutes = TIMESTAMPDIFF(
                       MINUTE, departure_time, COALESCE(arrival_time, %s))
             WHERE leg_id = %s;""",
        (arrival_time, arrival_time, leg_id),
    )


async def finish_rm_load(cur, leg_id, finished_time):
    """Stamp unload completion and compute Time Taken.

    Time Taken is Finished minus Arrival -- the unload itself -- not the drive.
    """
    await cur.execute(
        f"""UPDATE {TABLE_RM_LOADS}
               SET finished_time = COALESCE(finished_time, %s),
                   time_taken_minutes = TIMESTAMPDIFF(
                       MINUTE, arrival_time, COALESCE(finished_time, %s))
             WHERE leg_id = %s AND arrival_time IS NOT NULL;""",
        (finished_time, finished_time, leg_id),
    )
