"""Load type per leg, derived from the lane it runs.

The dispatcher's report sheet counts loads by type, per driver, per day. That
classification is a property of the lane rather than anything a driver says:
200 -> E2 is RM, E2 -> 200 is RM Inbound, SDS -> 200 is FG STO.

Only LOADED legs carry a type. An empty repositioning leg is not a load, so
counting it would overstate the day's work -- one round of E1 -> 210 -> E1
delivers one FG E1 load, not two. The exception is the RM circuit, where both
directions are loaded, giving one round with two loads.

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

    Matched on sites, so 200F and 200R both count as 200, and E2F and E2R
    both as E2 -- the lane is the same regardless of which door was used.
    """
    if load_status != "LOADED":
        return None
    if not origin or not destination:
        return None

    lane = (site_of(origin), site_of(destination))
    known = LANE_CACHE.get(lane)
    if known:
        return known

    # A loaded leg on no defined lane is spot work, which is its own category
    # in the dispatcher's sheet.
    if route_code == "SPOT":
        return SPOT_LOAD_TYPE
    return None
