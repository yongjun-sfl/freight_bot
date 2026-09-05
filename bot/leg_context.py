"""Shared state for one commit_trip_leg call.

Split out of state_machine.py. The old giant function kept half a dozen
helper closures that each captured the same locals (cursor, driver id,
timestamp, parsed intent). This class bundles that per-message state so the
case handlers in leg_cases.py and the queries they share can live in
their own module. Each method is one of the former closures, with the
captured locals rewritten as self. reads.
"""

import logging

from config import TABLE_SHUTTLE_LEGS
from ai_engine import normalize_location, site_of
from eta import drive_minutes
from load_types import classify as classify_load
from rm_manifest import finish_rm_load, record_rm_load
from routes import SPOT_ROUTE_CODE, is_anchor, route_code_for, serves

from leg_helpers import not_a_facility

logger = logging.getLogger(__name__)


class LegContext:
    """Everything one commit_trip_leg call knows about a message."""

    def __init__(self, p, did, user_name, group_title,
                 orig_chat_id, orig_msg_id, msg_timestamp, intent):
        self.p = p
        self.did = did
        self.user_name = user_name
        self.group_title = group_title
        self.orig_chat_id = orig_chat_id
        self.orig_msg_id = orig_msg_id
        self.msg_timestamp = msg_timestamp
        self.intent = intent

        self.case_type = intent.get("case_type", "NONE_WORK_RELATED")
        self.conn = None
        self.cur = None

        # Parsed fields, as the old function unpacked them at the top.
        self.text_trailer = not_a_facility(
            intent.get("trailer_number") or intent.get("text_trailer"))
        self.ocr_trailer = not_a_facility(intent.get("ocr_trailer"))
        self.bol_number = intent.get("bol_number")
        self.document_type = intent.get("document_type", "UNKNOWN")
        self.action_type = intent.get("action")
        self.door_num = intent.get("door_number")
        self.do_num = intent.get("do_number")
        self.origin_dock = intent.get("origin_dock")
        self.destination_dock = intent.get("destination_dock")
        self.primary_image_blob = intent.get("primary_image_blob")
        self.shipper_signed = intent.get("shipper_signed", False)
        self.receiver_signed = intent.get("receiver_signed", False)
        self.raw_text = intent.get("raw_text") or ""
        self.raw_lower = self.raw_text.lower()

        raw_orig = intent.get("origin_location")
        raw_dest = intent.get("destination_location")
        self.raw_orig = raw_orig
        self.raw_dest = raw_dest
        self.origin_loc = normalize_location(raw_orig) if raw_orig else "UNKNOWN"
        self.dest_loc = normalize_location(raw_dest) if raw_dest else "UNKNOWN"

        # Filled in by the orchestrator after the load-status resolution.
        self.is_bobtail_flag = 0
        self.load_status_val = None
        self.is_load_pickup = False

    async def find_duplicate_bol_leg(self):
        """Id of an existing leg already carrying this BOL, else None."""
        if not self.bol_number:
            return None
        await self.cur.execute(
            f"""SELECT id
                  FROM {TABLE_SHUTTLE_LEGS}
                 WHERE bol_number = %s
                 LIMIT 1;""",
            (self.bol_number,)
        )
        existing_bol = await self.cur.fetchone()
        return existing_bol[0] if existing_bol else None

    async def resolve_round(self, origin, destination):
        """(round_number, route_code) for a new departure.

        A round is one traversal of a defined route: leave an anchor, work
        the stops belonging to a route anchored there, come back. A leg to
        anywhere else is a spot delivery and gets its own round.
        """
        await self.cur.execute(
            f"""SELECT round_number, route_code, destination_location
                  FROM {TABLE_SHUTTLE_LEGS}
                 WHERE user_id = %s
                   AND is_positioning_leg = 0
                   AND round_number IS NOT NULL
                   AND DATE(departure_time) = DATE(%s)
              ORDER BY id DESC
                 LIMIT 1;""",
            (self.did, self.msg_timestamp)
        )
        row = await self.cur.fetchone()

        async def open_new_round():
            await self.cur.execute(
                f"""SELECT COALESCE(MAX(round_number), 0)
                      FROM {TABLE_SHUTTLE_LEGS}
                     WHERE user_id = %s
                       AND DATE(departure_time) = DATE(%s);""",
                (self.did, self.msg_timestamp)
            )
            (highest_today,) = await self.cur.fetchone()
            return (highest_today or 0) + 1

        if not row:
            number = await open_new_round()
            if serves(origin, destination):
                return number, route_code_for(origin, [destination])
            return number, SPOT_ROUTE_CODE

        current, current_route, last_destination = row

        # Where the open round started, and everywhere it has been.
        await self.cur.execute(
            f"""SELECT origin_location, destination_location
                  FROM {TABLE_SHUTTLE_LEGS}
                 WHERE user_id = %s
                   AND round_number = %s
                   AND is_positioning_leg = 0
                   AND DATE(departure_time) = DATE(%s)
              ORDER BY id ASC;""",
            (self.did, current, self.msg_timestamp)
        )
        legs = await self.cur.fetchall()
        anchor = legs[0][0] if legs else None
        visited = [leg[1] for leg in legs]

        # Has the open round finished? A normal round ends back at its
        # anchor; a spot delivery ends wherever it rejoins a route.
        if current_route == SPOT_ROUTE_CODE:
            if not is_anchor(last_destination):
                return current, SPOT_ROUTE_CODE
        elif anchor and site_of(last_destination) != site_of(anchor):
            if serves(anchor, destination):
                return current, route_code_for(anchor, visited + [destination])
            return await open_new_round(), SPOT_ROUTE_CODE

        number = await open_new_round()
        if serves(origin, destination):
            return number, route_code_for(origin, [destination])
        return number, SPOT_ROUTE_CODE

    async def current_round_number(self):
        """The round a within-facility move happened during."""
        await self.cur.execute(
            f"""SELECT round_number
                  FROM {TABLE_SHUTTLE_LEGS}
                 WHERE user_id = %s
                   AND round_number IS NOT NULL
                   AND DATE(departure_time) = DATE(%s)
              ORDER BY id DESC
                 LIMIT 1;""",
            (self.did, self.msg_timestamp)
        )
        row = await self.cur.fetchone()
        return row[0] if row else None

    async def stamp_finished(self, target_leg=None):
        """Record that a live load or unload completed.

        Applies to the leg the driver is ending, which on a merged message
        ("live loading finished load 200 to E2F") is the leg BEFORE the
        departure being announced. The job is over, so the leg is marked
        COMPLETED here and now.
        """
        if target_leg is None:
            await self.cur.execute(
                f"""SELECT id FROM {TABLE_SHUTTLE_LEGS}
                     WHERE user_id = %s AND is_positioning_leg = 0
                  ORDER BY id DESC LIMIT 1;""",
                (self.did,)
            )
            row = await self.cur.fetchone()
            target_leg = row[0] if row else None
        if not target_leg:
            return None
        await self.cur.execute(
            f"""UPDATE {TABLE_SHUTTLE_LEGS}
                   SET finished_time = COALESCE(finished_time, %s),
                       leg_status = 'COMPLETED'
                 WHERE id = %s;""",
            (self.msg_timestamp, target_leg)
        )
        await finish_rm_load(self.cur, target_leg, self.msg_timestamp)
        return target_leg

    async def open_departure_leg(self, origin, destination, trailer):
        """Insert a new IN_TRANSIT leg for a trip that is starting.

        Shared by CASE 1 and by the paths that recognise a hooked load as a
        departure the driver never announced as one. Reads bol_number,
        document_type, primary_image_blob and shipper_signed at call time,
        so a stale BOL cleared by the caller is already gone by the time the
        row is written.
        """
        round_and_route = await self.resolve_round(origin, destination)
        # ETA is the drive allowance from location_distances (DB only -- the
        # CSVs seed once on first boot; the dispatcher may edit the table
        # live). Lunch is added later when it is reported against this leg
        # while it is still in transit.
        eta = await drive_minutes(self.cur, origin, destination)
        await self.cur.execute(
            f"""INSERT INTO {TABLE_SHUTTLE_LEGS} (
                   user_id,
                   trailer_number,
                   bol_number,
                   document_type,
                   origin_location,
                   destination_location,
                   departure_time,
                   arrival_action,
                   bol_image,
                   dock_number,
                   shipper_signed,
                   is_bobtail,
                   leg_status,
                   load_status,
                   round_number,
                   route_code,
                   load_type,
                   eta_minutes
               ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                         'IN_TRANSIT', %s, %s, %s, %s, %s);""",
            (
                self.did, trailer, self.bol_number, self.document_type,
                origin, destination, self.msg_timestamp, self.action_type,
                self.primary_image_blob, self.door_num,
                1 if self.shipper_signed else 0, self.is_bobtail_flag,
                self.load_status_val, *round_and_route,
                classify_load(origin, destination, self.load_status_val,
                              round_and_route[1]),
                eta,
            )
        )
        await self.conn.commit()
        leg_id = self.cur.lastrowid

        # RM consignments carry line items the receiving departments at E2F
        # and E2R report on, so they are kept in their own tables rather than
        # flattened into the leg.
        await record_rm_load(self.cur, leg_id, self.intent, origin, destination,
                             trailer, self.msg_timestamp,
                             driver_id=self.did, driver_name=self.user_name)
        await self.conn.commit()
        return leg_id

    async def start_hooked_load(self, where, position, trailer, arrival_leg=None):
        """Record a load hooked with no destination said aloud.

        The BOL's SHIP TO is the only other statement of where the load is
        going. Consulted ONLY when the driver named nowhere, and never when
        the paperwork is a duplicate (a BOL already on another leg is the
        previous load's). Falls back to a card when the paperwork cannot say.
        """
        paperwork_dest = self.intent.get("bol_destination")
        if paperwork_dest and await self.find_duplicate_bol_leg():
            logger.warning(
                f"BOL '{self.bol_number}' is already recorded on another leg; "
                f"not trusting its ship-to as a destination."
            )
            paperwork_dest = None
        # A consignee at the site they are standing on is not a trip.
        if paperwork_dest and site_of(paperwork_dest) == site_of(where):
            logger.info(
                f"BOL ship-to {paperwork_dest} is the site the load was "
                f"hooked at ({where}); not recording a leg."
            )
            paperwork_dest = None

        if not paperwork_dest:
            logger.warning(
                f"Driver #{self.did} picked up a load at {where} without "
                f"naming a destination; carding for dispatch."
            )
            return {
                "is_clean": False,
                "leg_id": arrival_leg,
                "card_text": self.pickup_with_no_destination(
                    where, position, trailer),
            }

        # Two legs open at once would leave the arrival half-recorded and skew
        # every dwell measured off it. Hooking a load means whatever they were
        # doing here is over.
        if arrival_leg:
            await self.cur.execute(
                f"""UPDATE {TABLE_SHUTTLE_LEGS}
                       SET leg_status = 'COMPLETED'
                     WHERE id = %s;""",
                (arrival_leg,)
            )
        leg_id = await self.open_departure_leg(where, paperwork_dest, trailer)
        logger.info(
            f"Driver #{self.did} hooked a load at {where} without naming a "
            f"destination; BOL ship-to gives {paperwork_dest}. "
            f"Recorded as Leg #{leg_id}."
        )
        return {"is_clean": True, "leg_id": leg_id, "card_text": None}

    def pickup_with_no_destination(self, where, position, trailer):
        """Card for a load hooked with nowhere recorded to take it.

        The trip is real and the dispatcher can work out where it went from
        the arrival that follows, but only if they are told the message
        happened. Nothing is written: a leg to UNKNOWN would enter a round
        and a route as though it were a real destination.
        """
        at = f"`{where}`"
        if position:
            at += f" (dock `{position}`)"
        return (
            f"\u26a0\ufe0f **MANUAL RECONCILE: Load Picked Up With No Destination**\n"
            f"\U0001f464 Driver: {self.user_name}\n"
            f"\U0001f4ac Message: `{self.raw_text}`\n"
            f"\U0001f69b Trailer: `{trailer or 'UNKNOWN'}`\n"
            f"\U0001f4cd Origin: {at}\n"
            f"\u2753 Issue: Load picked up with an origin but no destination "
            f"mentioned, so the departure was NOT recorded as a leg."
        )
