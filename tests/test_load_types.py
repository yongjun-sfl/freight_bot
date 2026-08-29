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
    assert leg_out["trip_seq"] == 1 and leg_back["trip_seq"] == 2


async def test_trip_seq_counts_legs_per_driver_per_day(pool):
    await seed_network(pool)
    seqs = []
    for origin, dest in (("200F", "E2F"), ("E2F", "SDS"), ("SDS", "200F")):
        res = await commit(
            pool,
            intent(case_type="CASE_1_ORIGIN_DEPARTURE", origin_location=origin,
                   destination_location=dest, text_trailer="77344",
                   load_status="EMPTY"),
        )
        await commit(pool, intent(case_type="CASE_2_DESTINATION_ARRIVAL"))
        seqs.append((await get_leg(pool, res["leg_id"]))["trip_seq"])
    assert seqs == [1, 2, 3]


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
