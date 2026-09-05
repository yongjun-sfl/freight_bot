"""Reusable text helpers for the trip state machine.

Everything here is a pure function of its arguments plus the location cache
(``LOCATION_CACHE`` from ``ai_engine``, mutated in place at startup and when
the dispatcher refreshes locations). No database access.

Split out of ``state_machine.py`` so the case handlers and the orchestrator
share one copy of the pattern matching.
"""

import re
import logging

from ai_engine import LOCATION_CACHE, normalize_location, site_of

logger = logging.getLogger(__name__)

# A within-facility move re-reported inside this window is treated as the same
# event rather than a second drop. Drivers routinely send the paperwork and a
# photo of the parked trailer minutes apart, both describing one drop.
REPEAT_MOVE_MINUTES = 30

# Word-boundary matched: a bare `"bt" in text` substring test fires on ordinary
# words like "doubt", "debt" and "subtotal", which forced load_status to EMPTY
# and silently skipped the outbound BOL compliance guard.
BOBTAIL_PATTERN = re.compile(
    r"\b(?:bobtail|bob\s*tail|bt|b/t|no\s+trailer|single\s+tractor|tractor\s+only)\b",
    re.IGNORECASE,
)

# Hooking a load starts a trip; it is never a within-yard reposition. When the
# driver names no destination there is nothing to record, so the message would
# otherwise vanish -- "Load trailer pickup sds dock28 #77209" left the whole
# SDS -> 200F run missing from the day.
PICKUP_PATTERN = re.compile(
    r"\b(?:pick\s*-?\s*up|pickup|picking\s+up|hook(?:ed|ing)?(?:\s+up)?)\b",
    re.IGNORECASE,
)

# Tells a finished unload from a finished load. `work_finished` says a live
# job completed but not which kind, and the two leave the trailer in opposite
# states. Matched on a word boundary so "unloading" is never read as loading.
UNLOAD_PATTERN = re.compile(r"\bunload(?:s|ed|ing)?\b", re.IGNORECASE)
LOAD_PATTERN = re.compile(r"\bload(?:s|ed|ing)?\b", re.IGNORECASE)

# A report the driver is actually moving a trailer between facilities. The
# two-facility recovery below exists for messages the parser filed as a plain
# completion ("Unloading finished / Empty 200 sds") -- the real ones carry a
# movement or load-status cue. Chatter that merely NAMES two known codes is
# not a trip: "7634 pactra 가 ..." is a service complaint about the 7634 depot
# (whose official name is EPC PACTRA, and recovering it invented a phantom
# 7634 -> 200F leg for Ilpyo Hong at  15:53 on  08/28.
MOVEMENT_CUE_PATTERN = re.compile(
    r"\b(?:to|heading|headed|leaving|leaves|left|going|returning|departed|"
    r"departing|departure|driv(?:e|es|ing)?|mov(?:e|es|ed|ing)|empty|"
    r"load(?:s|ed|ing)?|pick\s*-?\s*up|pickup|picking\s+up|"
    r"drop(?:s|ped|ping)?|unload(?:s|ed|ing)?|bobtail|bt|"
    r"hook(?:s|ed|ing)?|arriv(?:ed|es)?|finish(?:es|ed)?)\b|b/t",
    re.IGNORECASE,
)

# A "to <SOMETHING>" that names a real facility, used to tell a yard move from
# the trip hiding inside it. "Empty drop e1 yard #3, Bobtail to SDs" is a drop
# at E1 AND a bobtail to SDS: the departure phrase must not be swallowed by the
# yard phrase. Only a token that actually resolves to a known facility counts,
# so "move to Dock 47" (Dock is not a facility) stays a yard move.
DEPARTURE_TO_PATTERN = re.compile(
    r"\b(?:bobtail|bob\s*tail|bt|empty|heading|leaving|going|returning|"
    r"drive|move|pick\s*u?p?|pickup)\b[^.]*?\bto\s+([0-9A-Za-z]+)",
    re.IGNORECASE,
)
def cross_facility_destination(raw_text):
    """A facility the text says the driver is going to, i.e. a to X whose X
    resolves to a known facility. Returns the canonical code or None.

    This turns a phrase like empty-drop at a yard with a Bobtail to SDs into
    a trip: the to SDs is the real movement and must not be swallowed by the
    yard-move phrasing. Only known facilities count, so move to Dock 47
    stays a yard move (Dock is not a facility).
    """
    if not raw_text:
        return None
    for match in DEPARTURE_TO_PATTERN.finditer(raw_text):
        token = match.group(1).strip().upper()
        resolved = normalize_location(token)
        if resolved not in ("UNKNOWN", "NONE", "NULL", "MISSING_DEST") \
                and site_of(resolved) in LOCATION_CACHE.get("site_map", {}):
            return resolved
    return None
def two_facilities_in_order(raw_text):
    """(origin, destination) when the raw text names two DISTINCT facilities
    in that order, or (None, None).

    Drivers drop the word "to" constantly: "Empty 200 sds" means an EMPTY
    departure 200 -> sds. The parser only sometimes recovers that (LLM
    variance), so when a message was filed as a plain completion but names
    two valid codes, the state machine can recover the trip itself. Only two
    KNOWN facilities count; "200 yard", "sds do27" or a dock number is one
    location, not two.
    """
    if not raw_text:
        return None, None
    # Guard: an ARRIVAL is not the departure being recovered. "arrived X from
    # Y" names two facilities but the "from Y" is where the trip started, not
    # a return leg -- recovering it invented a phantom for every Younypyo Kim
    # stop. Only treat it as a departure when there is an onward cue (an
    # explicit "to", or a bare "empty X Y" without "arrived ... from").
    text_l = raw_text.lower()
    if (("arrived" in text_l or "arrive" in text_l)
            and " from " in text_l
            and " to " not in text_l):
        return None, None
    # See MOVEMENT_CUE_PATTERN: recover only real movement reports, never
    # chatter that happens to name two known codes.
    if not MOVEMENT_CUE_PATTERN.search(raw_text):
        return None, None
    known = set(LOCATION_CACHE.get("codes") or [])
    known |= set(LOCATION_CACHE.get("alias_map") or {})
    seen = []
    for token in re.findall(r"[0-9A-Za-z]+", raw_text):
        cleaned = token.upper()
        resolved = normalize_location(cleaned)
        if resolved in ("UNKNOWN", "NONE", "NULL", "MISSING_DEST", "YARD"):
            continue
        if cleaned in known or resolved in known:
            if not seen or seen[-1] != resolved:
                seen.append(resolved)
    if len(seen) >= 2:
        origin, destination = seen[0], seen[1]
        if origin != destination and site_of(origin) != site_of(destination):
            return origin, destination
    return None, None
def facility_dock_from_text(raw_text, origin_loc, door_number, trailer):
    """Recover a ``FACILITY #NN`` the parser mis-split.

    ``"pick up load 200 #47 to e2 f"`` reaches the state machine as origin
    UNKNOWN and trailer "47" --the model reads the wall number as equipment
    and drops the facility (Joseph Kim 08:35 on 08/28). A known facility
    immediately followed by a #-number is the facility AND a dock position:
    the origin is the facility, the number is the door, and the number never
    rides along as a trailer number.
    """
    if not raw_text:
        return origin_loc, door_number, trailer
    # Canonical codes PLUS their spoken aliases: "200" is an alias of 200F,
    # and drivers write "200 #47", not "200F #47". Only bare tokens count --
    # "200 FRONT" is a multi-word alias and cannot sit in <FACILITY> #NN.
    aliases = (LOCATION_CACHE.get("alias_map") or {})
    codes = sorted(
        {str(c) for c in (LOCATION_CACHE.get("codes") or []) if c}
        | {a for a in aliases if re.fullmatch(r"[A-Z0-9]+", a)},
        key=len, reverse=True,
    )
    if not codes:
        return origin_loc, door_number, trailer
    pattern = re.compile(
        r"\b(" + "|".join(re.escape(c) for c in codes) + r")\s*#\s*(\d{1,3})\b",
        re.IGNORECASE,
    )
    for match in pattern.finditer(raw_text):
        resolved = normalize_location(match.group(1))
        if resolved in ("UNKNOWN", "NONE", "NULL", "MISSING_DEST"):
            continue
        if (not origin_loc or
                origin_loc in ("UNKNOWN", "NONE", "NULL", "MISSING_ORIGIN")):
            origin_loc = resolved
        if not door_number:
            door_number = match.group(2)
        if trailer and trailer == match.group(2):
            logger.info(
                f"Door #'{trailer}' parsed as a trailer;it is the dock of "
                f"{resolved}, not equipment."
            )
            trailer = None
        break
    return origin_loc, door_number, trailer


def not_a_facility(value):
    """Drop a trailer number that is really a facility code.

    Bare numeric sites like 7634 read as trailers, and a facility stored
    as a trailer both loses the location and corrupts trailer history.
    """
    if not value:
        return value
    clean = str(value).strip().upper()
    known = set(LOCATION_CACHE.get("codes") or [])
    known |= set(LOCATION_CACHE.get("alias_map") or {})
    if clean in known or normalize_location(clean) in known:
        logger.info(f"Ignoring facility code {clean!r} given as a trailer number.")
        return None
    return value