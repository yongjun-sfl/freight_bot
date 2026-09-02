"""Load type per leg, derived from the lane.

The dispatcher's report sheet counts loads by type per driver per day. These
pin the derivation so the Done side can be filled without hand-tallying.
"""

import pytest

import ai_engine
import load_types
from conftest import commit, get_leg, intent, seed_network


@pytest.fixture(autouse=True)
def lanes():
    load_types.load_lane_map()
    yield
    load_types.LANE_CACHE.clear()


def _sites(mapping):
    ai_engine.LOCATION_CACHE["site_map"] = mapping


def test_lane_map_loads():
    assert load_types.LANE_CACHE, "lane map should not be empty"


@pytest.mark.parametrize("origin,destination,expected", [
    ("200F", "E2R", "RM"),
    ("200R", "E2F", "RM"),
    ("E2R", "200R", "RM Inbound"),
    ("SDS", "200F", "FG STO"),
    ("E1", "210", "FG E1"),
    ("200F", "7634", "FG ES"),
    ("SDS", "7634", "FG DS"),
    ("3551", "E2F", "RM 3551"),
])
def test_lane_determines_load_type(origin, destination, expected):
    _sites({"200F": "200", "200R": "200", "E2F": "E2", "E2R": "E2"})
    assert load_types.classify(origin, destination, "LOADED") == expected


def test_front_and_rear_do_not_change_the_lane():
    """200F -> E2R and 200R -> E2F are the same lane; the door used is not
    part of the classification."""
    _sites({"200F": "200", "200R": "200", "E2F": "E2", "E2R": "E2"})
    assert (load_types.classify("200F", "E2F", "LOADED")
            == load_types.classify("200R", "E2R", "LOADED") == "RM")


def test_facility_lanes_stay_distinct_across_one_site_pair():
    """E2R -> 200R is RM Inbound while E2R -> 200F is FG Inbound. Both are
    (E2, 200) once collapsed to sites, so the exact lane must win or one of
    them is silently overwritten."""
    _sites({"200F": "200", "200R": "200", "E2F": "E2", "E2R": "E2"})
    assert load_types.classify("E2R", "200R", "LOADED") == "RM Inbound"
    assert load_types.classify("E2R", "200F", "LOADED") == "FG Inbound"
    assert load_types.classify("E2F", "200R", "LOADED") == "RM Inbound"


def test_ambiguous_site_pair_stays_untyped():
    """An unmapped door combination that collapses to a mixed site pair must
    stay None -- the map cannot say which of RM Inbound / FG Inbound it is."""
    _sites({"200F": "200", "200R": "200", "E2F": "E2", "E2R": "E2"})
    assert load_types.classify("E2F", "200F", "LOADED") is None


def test_7634_back_to_200_is_empty_only():
    """7634 -> 200 was deleted from the map: that move only ever runs empty,
    so a loaded leg on the lane is None, not FG ES."""
    _sites({"200F": "200", "200R": "200", "7634": "7634"})
    assert load_types.classify("7634", "200F", "LOADED", "R7") is None
    assert load_types.classify("7634", "200R", "LOADED", "R7") is None
    assert load_types.classify("7634", "200F", "EMPTY", "R7") is None


def test_210_back_to_e1_is_empty_only():
    """210 -> E1 was deleted from the map: the empty leg of the E1<->210 round
    never carries a load, so a loaded leg on the lane is None, not FG E1. The
    loaded direction (E1 -> 210) is untouched."""
    _sites({"E1": "E1", "210": "210"})
    assert load_types.classify("210", "E1", "LOADED", "R5") is None
    assert load_types.classify("210", "E1", "EMPTY", "R5") is None
    assert load_types.classify("E1", "210", "LOADED", "R5") == "FG E1"


def test_site_pair_fallback_is_unique_before_use():
    """A lane keyed at site granularity (how the old '7634,200' row read)
    must still be found by either 200 door, but only while it stays
    unambiguous."""
    _sites({"200F": "200", "200R": "200", "7634": "7634"})
    load_types.LANE_CACHE[("7634", "200")] = "FG ES"
    try:
        assert load_types.classify("7634", "200F", "LOADED") == "FG ES"
        assert load_types.classify("7634", "200R", "LOADED") == "FG ES"
    finally:
        load_types.LANE_CACHE.pop(("7634", "200"), None)


def test_empty_legs_carry_no_load_type():
    """An empty repositioning leg is not a load. Counting it would overstate
    the day: one E1 -> 210 -> E1 round delivers one FG E1, not two."""
    _sites({})
    assert load_types.classify("210", "E1", "EMPTY") is None


def test_unmapped_loaded_lane_off_route_is_spot():
    _sites({})
    assert load_types.classify("E2R", "1380", "LOADED", "SPOT") == "Spot Delivery"


def test_unmapped_loaded_lane_on_route_is_untyped():
    """Better untyped than mislabelled -- the dispatcher can set it."""
    _sites({})
    assert load_types.classify("E2R", "1380", "LOADED", "R1") is None


# --------------------------------------------------------------------------
# through the state machine
# --------------------------------------------------------------------------

async def test_rm_circuit_is_one_round_carrying_two_loads(pool):
    """200 -> E2R with RM, straight back to 200 with RM Inbound. One round,
    two loaded legs, two distinct load types."""
    await seed_network(pool)
    load_types.load_lane_map()

    out = await commit(
        pool,
        intent(case_type="CASE_1_ORIGIN_DEPARTURE", origin_location="200R",
               destination_location="E2R", text_trailer="77344",
               load_status="LOADED", bol_number="RM-1", primary_image_blob=b"x",
               document_type="RM"),
    )
    await commit(pool, intent(case_type="CASE_2_DESTINATION_ARRIVAL"))
    back = await commit(
        pool,
        intent(case_type="CASE_1_ORIGIN_DEPARTURE", origin_location="E2R",
               destination_location="200R", text_trailer="77344",
               load_status="LOADED", bol_number="RM-2", primary_image_blob=b"x",
               document_type="RM"),
    )

    leg_out = await get_leg(pool, out["leg_id"])
    leg_back = await get_leg(pool, back["leg_id"])
    assert leg_out["round_number"] == leg_back["round_number"], "one round"
    assert leg_out["load_type"] == "RM"
    assert leg_back["load_type"] == "RM Inbound"


async def test_rounds_increment_per_circuit_on_a_past_day(pool):
    """The replay drives previous days, so round counters must key off the
    departure's own day, not the database's CURRENT_DATE. Before this fix the
    per-day MAX() query never saw the older legs, and every leg of a replayed
    day was round 1. Two 200F -> SDS -> 200F circuits are two rounds."""
    await seed_network(pool)
    rounds = []
    for origin, dest in (("200F", "SDS"), ("SDS", "200F"),
                         ("200F", "SDS"), ("SDS", "200F")):
        res = await commit(
            pool,
            intent(case_type="CASE_1_ORIGIN_DEPARTURE", origin_location=origin,
                   destination_location=dest, text_trailer="77344",
                   load_status="EMPTY"),
            when="2026-08-28 07:00:00",
        )
        await commit(pool, intent(case_type="CASE_2_DESTINATION_ARRIVAL"),
                     when="2026-08-28 08:00:00")
        rounds.append((await get_leg(pool, res["leg_id"]))["round_number"])
    assert rounds == [1, 1, 2, 2], rounds


async def test_trip_seq_column_is_gone(pool):
    """round_number carries the round sequence; trip_seq was the duplicate
    column added by mistake and must not survive the schema migration."""
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE table_schema = DATABASE() AND table_name = 'shuttle_legs' "
                "AND column_name = 'trip_seq';")
            (n,) = await cur.fetchone()
    assert n == 0


async def test_sds_yard_slot_is_recorded(pool):
    """DO# at SDS is a parking position, not a delivery order number."""
    await seed_network(pool)
    leg = await commit(
        pool,
        intent(case_type="CASE_1_ORIGIN_DEPARTURE", origin_location="200F",
               destination_location="SDS", text_trailer="77344",
               load_status="EMPTY"),
    )
    await commit(
        pool,
        intent(case_type="CASE_2_DESTINATION_ARRIVAL",
               destination_location="SDS", do_number="34"),
    )
    assert (await get_leg(pool, leg["leg_id"]))["do_number"] == "34"


def test_loaded_200_to_sds_is_spot_work():
    """It runs on route R2, but the load is spot delivery. Route and load type
    are independent dimensions -- being on a defined route does not make the
    cargo scheduled work."""
    _sites({"200F": "200", "200R": "200"})
    assert load_types.classify("200F", "SDS", "LOADED", "R2") == "Spot Delivery"


def test_sds_to_200_loaded_is_still_fg_sto():
    """The reverse lane is scheduled work, so direction matters."""
    _sites({"200F": "200"})
    assert load_types.classify("SDS", "200F", "LOADED", "R2") == "FG STO"
