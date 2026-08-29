"""Defined shuttle routes and the round grouping that falls out of them.

A round is one traversal of a defined route: the driver leaves an anchor
facility, works the stops belonging to a route anchored there, and comes back.
A leg to anywhere else is a spot delivery and forms a round of its own.

This replaced a series of heuristics -- distance thresholds, zone maps, cycle
detection -- none of which could express the real rule. 100 and 1380 are not
separate rounds because they are far away; they are separate rounds because
they are on no route.
"""

import logging

from ai_engine import site_of
from config import TABLE_ROUTES, TABLE_ROUTE_MEMBERS

logger = logging.getLogger(__name__)

SPOT_ROUTE_CODE = "SPOT"

# anchors: every facility a route starts and ends at
# members: anchor -> set of facilities reachable on routes from that anchor
# routes:  anchor -> [(route_code, frozenset(members))], most specific first
ROUTE_CACHE = {
    "anchors": set(),
    "members": {},
    "routes": {},
}

# Seeded on first start. Operational data, so the tables win once populated.
DEFAULT_ROUTES = (
    ("R1", "200 RM out / FG back via SDS", "200F", ("E2F", "E2R", "SDS")),
    ("R2", "200 <-> SDS",                  "200F", ("SDS",)),
    ("R3", "SDS <-> 200",                  "SDS",  ("200F",)),
    ("R4", "SDS <-> 7634",                 "SDS",  ("7634",)),
    ("R5", "E1 <-> 210",                   "E1",   ("210",)),
    ("R6", "3551 <-> east cluster",        "3551", ("E1", "E2F", "E2R")),
    ("R7", "200 <-> 7634",                 "200F", ("7634",)),
)


async def seed_default_routes(cur):
    """Insert the defined routes once, if the table is empty."""
    await cur.execute(f"SELECT COUNT(*) FROM {TABLE_ROUTES};")
    (existing,) = await cur.fetchone()
    if existing:
        return 0

    for code, name, anchor, members in DEFAULT_ROUTES:
        await cur.execute(
            f"""INSERT INTO {TABLE_ROUTES} (route_code, route_name, anchor_location)
                VALUES (%s, %s, %s);""",
            (code, name, anchor),
        )
        for member in members:
            await cur.execute(
                f"""INSERT INTO {TABLE_ROUTE_MEMBERS} (route_code, location_code)
                    VALUES (%s, %s);""",
                (code, member),
            )
    return len(DEFAULT_ROUTES)


async def refresh_route_cache(pool):
    """Load routes and their stops from MySQL into memory."""
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""SELECT r.route_code, r.anchor_location, m.location_code
                      FROM {TABLE_ROUTES} r
                      LEFT JOIN {TABLE_ROUTE_MEMBERS} m
                             ON m.route_code = r.route_code
                     WHERE r.is_active = TRUE;"""
            )
            rows = await cur.fetchall()

    by_route = {}
    anchor_of = {}
    for route_code, anchor, member in rows:
        anchor = (anchor or "").strip().upper()
        anchor_of[route_code] = anchor
        if member:
            by_route.setdefault(route_code, set()).add(member.strip().upper())

    anchors = set(anchor_of.values()) - {""}
    members = {a: set() for a in anchors}
    routes = {a: [] for a in anchors}

    for route_code, anchor in anchor_of.items():
        if not anchor:
            continue
        stops = frozenset(by_route.get(route_code, ()))
        members[anchor] |= stops
        routes[anchor].append((route_code, stops))

    # Smallest first, so 200 -> SDS matches R2 rather than the broader R1.
    for anchor in routes:
        routes[anchor].sort(key=lambda pair: len(pair[1]))

    ROUTE_CACHE["anchors"] = anchors
    ROUTE_CACHE["members"] = members
    ROUTE_CACHE["routes"] = routes
    logger.info(
        f"Route cache refreshed: {len(anchor_of)} routes across "
        f"{len(anchors)} anchors ({', '.join(sorted(anchors)) or 'none'})."
    )


def is_anchor(location: str) -> bool:
    if not location:
        return False
    site = site_of(location)
    return any(site_of(a) == site for a in ROUTE_CACHE["anchors"])


def serves(anchor: str, destination: str) -> bool:
    """Is destination reachable on a route anchored at anchor?"""
    if not anchor or not destination:
        return False
    if site_of(destination) == site_of(anchor):
        return True          # the returning leg
    for known_anchor, stops in ROUTE_CACHE["members"].items():
        if site_of(known_anchor) == site_of(anchor) and destination in stops:
            return True
    return False


def route_code_for(anchor: str, visited) -> str:
    """The narrowest route from anchor covering every facility visited."""
    visited = {v for v in visited if v and site_of(v) != site_of(anchor)}
    if not visited:
        return None
    for known_anchor, entries in ROUTE_CACHE["routes"].items():
        if site_of(known_anchor) != site_of(anchor):
            continue
        for code, stops in entries:
            if visited <= stops:
                return code
    return None
