"""Load type per leg, derived from the lane it runs.

The dispatcher's report sheet counts loads by type, per driver, per day. That
classification is a property of the lane rather than anything a driver says:
200 -> E2 is RM, E2 -> 200 is RM Inbound, SDS -> 200 is FG STO.

Only LOADED legs carry a type. An empty repositioning leg is not a load, so
counting it would overstate the day's work -- one round of E1 -> 210 -> E1
delivers one FG E1 load, not two. The exception is the RM circuit, where both
directions are loaded, giving one round with two loads.

Lanes are keyed by exact facility pair (200F -> E2R, not 200 -> E2) because
the two halves of one site can carry different cargo: E2R -> 200R is RM
Inbound while E2R -> 200F is FG Inbound. The site pair is only a fallback,
and only when it resolves to a single type.

OQC Recall and IQC Recall are not derivable. They look identical to RM on the
wire (200 -> E2R/E2F) and the distinction lives with the dispatcher, who sets
it from a dropdown.
"""

import csv
import logging
import os

from ai_engine import site_of

logger = logging.getLogger(__name__)

CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "data", "load_types.csv")

SPOT_LOAD_TYPE = "Spot Delivery"

# (origin_site, destination_site) -> load type
LANE_CACHE = {}


def load_lane_map(path=CSV_PATH):
    """Read the lane map. Returns the number of lanes loaded."""
    LANE_CACHE.clear()
    if not os.path.exists(path):
        logger.warning(f"No load-type map at {path}; legs will be untyped.")
        return 0
    with open(path, newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            origin = (row.get("origin_site") or "").strip().upper()
            destination = (row.get("destination_site") or "").strip().upper()
            load_type = (row.get("load_type") or "").strip()
            if origin and destination and load_type:
                LANE_CACHE[(origin, destination)] = load_type
    logger.info(f"Load-type map: {len(LANE_CACHE)} lanes.")
    return len(LANE_CACHE)


def classify(origin: str, destination: str, load_status: str,
             route_code: str = None) -> str:
    """The load type for a leg, or None if it is not carrying a load.

    Exact lanes win: E2R -> 200R is RM Inbound while E2R -> 200F is FG
    Inbound, so the door-level lane cannot be collapsed. Only when a lane is
    NOT in the map does it fall back to the site pair (200F/200R -> 200), and
    then only if that site pair maps to a *single* load type -- (E2, 200) is
    both RM Inbound and FG Inbound, so it stays untyped rather than guessed.
    """
    if load_status != "LOADED":
        return None
    if not origin or not destination:
        return None
    origin_u = origin.strip().upper()
    destination_u = destination.strip().upper()

    known = LANE_CACHE.get((origin_u, destination_u))
    if known:
        return known

    # Site fallback, guarded against ambiguity. site_of() is read at call
    # time because the location cache is only guaranteed populated then.
    lane = (site_of(origin_u), site_of(destination_u))
    if lane != (origin_u, destination_u):
        types = {load_type for (origin_site, destination_site), load_type
                 in LANE_CACHE.items()
                 if (site_of(origin_site), site_of(destination_site)) == lane}
        if len(types) == 1:
            return types.pop()
        if len(types) > 1:
            return None   # ambiguous site pair: better untyped than mislabelled

    # A loaded leg on no defined lane is spot work, which is its own category
    # in the dispatcher's sheet.
    if route_code == "SPOT":
        return SPOT_LOAD_TYPE
    return None
