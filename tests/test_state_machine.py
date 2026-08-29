"""Behavioural coverage for state_machine.commit_trip_leg.

Tests marked ``xfail(strict=True)`` assert the behaviour the code is *supposed*
to have. They fail today because of a known defect, and the strict marker means
that once the defect is fixed the test turns into an error until the marker is
removed -- so a fix can never land silently.
"""

import pytest

from conftest import (
    DRIVER_ID,
    all_legs,
    commit,
    eastern_now,
    get_leg,
    insert_leg,
    intent,
    seed_locations,
    ts,
)

IMG = b"\xff\xd8\xff\xe0fake-jpeg-bytes"


# ==========================================================================
# Pre-dispatch guards
# ==========================================================================

async def test_unknown_driver_is_rejected(pool):
    res = await commit(
        pool,
        intent(case_type="CASE_1_ORIGIN_DEPARTURE", origin_location="200",
               destination_location="E2F"),
        did=999999,  # not in driver_profiles
    )
    assert res["is_clean"] is False
    assert res["leg_id"] is None
    assert res["card_text"] is None
    assert await all_legs(pool) == []


async def test_in_facility_move_is_ignored(pool):
    res = await commit(
        pool,
        intent(case_type="CASE_1_ORIGIN_DEPARTURE",
               origin_location="200", destination_location="200"),
    )
    assert res["is_clean"] is True
    assert res["leg_id"] is None
    assert await all_legs(pool) == []


async def test_duplicate_bol_returns_override_card(pool):
    existing = await insert_leg(pool, bol_number="B100", leg_status="COMPLETED")

    res = await commit(
        pool,
        intent(case_type="CASE_1_ORIGIN_DEPARTURE", bol_number="B100",
               origin_location="200", destination_location="E2F"),
    )
    assert res["is_clean"] is False
    assert res["action_type"] == "DUPLICATE_BOL_OVERRIDE"
    assert res["existing_leg_id"] == existing
    assert "Duplicate BOL" in res["card_text"]
    assert len(await all_legs(pool)) == 1  # nothing new written


# ==========================================================================
# CASE 1 -- origin departure
# ==========================================================================

async def test_clean_loaded_departure_inserts_leg(pool):
    res = await commit(
        pool,
        intent(case_type="CASE_1_ORIGIN_DEPARTURE",
               raw_text="200 to E2F loaded",
               origin_location="200", destination_location="E2F",
               text_trailer="77344", bol_number="B1",
               primary_image_blob=IMG, shipper_signed=True,
               load_status="LOADED", document_type="FG"),
    )
    assert res["is_clean"] is True
    assert res["card_text"] is None

    leg = await get_leg(pool, res["leg_id"])
    assert leg["origin_location"] == "200"
    assert leg["destination_location"] == "E2F"
    assert leg["trailer_number"] == "77344"
    assert leg["bol_number"] == "B1"
    assert leg["document_type"] == "FG"
    assert leg["load_status"] == "LOADED"
    assert leg["leg_status"] == "IN_TRANSIT"
    assert leg["shipper_signed"] == 1
    assert leg["is_bobtail"] == 0
    assert leg["bol_image"] == IMG
    assert leg["departure_time"] is not None


async def test_loaded_departure_without_paperwork_raises_compliance_card(pool):
    res = await commit(
        pool,
        intent(case_type="CASE_1_ORIGIN_DEPARTURE",
               raw_text="200 to E2F loaded",
               origin_location="200", destination_location="E2F",
               text_trailer="77344", load_status="LOADED"),
    )
    assert res["is_clean"] is False
    assert "Loaded Departure Compliance Error" in res["card_text"]
    # the leg is still persisted -- the card is an alert, not a rejection
    assert res["leg_id"] is not None
    assert (await get_leg(pool, res["leg_id"]))["bol_number"] is None


async def test_bobtail_departure_is_empty_and_skips_paperwork_rule(pool):
    res = await commit(
        pool,
        intent(case_type="CASE_1_ORIGIN_DEPARTURE",
               raw_text="heading to 200 bobtail",
               origin_location="E2F", destination_location="200",
               text_trailer="77344", load_status="BOBTAIL"),
    )
    assert res["is_clean"] is True
    assert res["card_text"] is None

    leg = await get_leg(pool, res["leg_id"])
    assert leg["is_bobtail"] == 1
    assert leg["load_status"] == "EMPTY"


async def test_autoheal_attaches_bol_to_open_leg(pool):
    open_leg = await insert_leg(
        pool, leg_status="IN_TRANSIT", load_status="LOADED",
        trailer_number="77344", bol_number=None, bol_image=None,
    )

    res = await commit(
        pool,
        intent(case_type="CASE_1_ORIGIN_DEPARTURE", bol_number="B9",
               primary_image_blob=IMG, document_type="FG"),
    )
    assert res["is_clean"] is True
    assert res["leg_id"] == open_leg
    assert len(await all_legs(pool)) == 1  # healed, not duplicated

    leg = await get_leg(pool, open_leg)
    assert leg["bol_number"] == "B9"
    assert leg["bol_image"] == IMG
    assert leg["document_type"] == "FG"
    assert leg["trailer_number"] == "77344"  # preserved


async def test_origin_and_trailer_inferred_from_last_leg(pool):
    await insert_leg(
        pool, leg_status="COMPLETED", destination_location="E2F",
        trailer_number="77344", bol_number="BPRIOR", bol_image=IMG,
    )

    res = await commit(
        pool,
        intent(case_type="CASE_1_ORIGIN_DEPARTURE",
               origin_location=None, destination_location="200",
               bol_number="BNEW", primary_image_blob=IMG, load_status="LOADED"),
    )
    leg = await get_leg(pool, res["leg_id"])
    assert leg["origin_location"] == "E2F"   # inferred from prior destination
    assert leg["trailer_number"] == "77344"  # inherited


async def test_new_departure_completes_prior_active_legs(pool):
    prior = await insert_leg(
        pool, leg_status="IN_TRANSIT", load_status="LOADED",
        bol_number="BPRIOR", bol_image=IMG, trailer_number="77344",
    )

    res = await commit(
        pool,
        intent(case_type="CASE_1_ORIGIN_DEPARTURE",
               origin_location="E2F", destination_location="200",
               text_trailer="77344", bol_number="BNEW",
               primary_image_blob=IMG, load_status="LOADED"),
    )
    prior_row = await get_leg(pool, prior)
    assert prior_row["leg_status"] == "COMPLETED"
    assert prior_row["arrival_time"] is not None
    assert (await get_leg(pool, res["leg_id"]))["leg_status"] == "IN_TRANSIT"


async def test_incomplete_route_raises_card(pool):
    res = await commit(pool, intent(case_type="CASE_1_ORIGIN_DEPARTURE"))
    assert res["is_clean"] is False
    assert "Incomplete Departure" in res["card_text"]
    assert res["leg_id"] is not None


async def test_rm_load_bypasses_pod_check(pool):
    """Raw-material loads must never demand a receiver POD."""
    await insert_leg(
        pool, leg_status="IN_TRANSIT", load_status="LOADED",
        document_type="RM", receiver_signed=0,
        bol_number="BRM", bol_image=IMG, trailer_number="77344",
    )

    res = await commit(
        pool,
        intent(case_type="CASE_1_ORIGIN_DEPARTURE",
               origin_location="E2F", destination_location="200",
               text_trailer="77344", load_status="EMPTY"),
    )
    assert res["is_clean"] is True
    assert res["card_text"] is None


@pytest.mark.xfail(
    strict=True,
    reason="Mid-shift POD guard is dead code: the SELECT at state_machine.py:247 "
           "runs after the INSERT and has no id filter, so ORDER BY id DESC LIMIT 1 "
           "returns the EMPTY leg just created rather than the prior FG load. "
           "last_shift_leg[1] == 'LOADED' is therefore never true.",
)
async def test_midshift_pod_guard_blocks_after_unsigned_fg_leg(pool):
    await insert_leg(
        pool, leg_status="IN_TRANSIT", load_status="LOADED",
        document_type="FG", receiver_signed=0,
        bol_number="BFG", bol_image=IMG, trailer_number="77344",
    )

    res = await commit(
        pool,
        intent(case_type="CASE_1_ORIGIN_DEPARTURE",
               origin_location="E2F", destination_location="200",
               text_trailer="77344", load_status="EMPTY"),
    )
    assert res["is_clean"] is False
    assert "Missing Receiver POD" in res["card_text"]


@pytest.mark.xfail(
    strict=True,
    reason="The global duplicate-BOL guard at state_machine.py:92 runs before the "
           "match block, so re-sending a photo for a leg whose bol_number is already "
           "known can never reach the CASE 1 auto-heal that would attach the image.",
)
async def test_autoheal_attaches_image_when_bol_already_known(pool):
    open_leg = await insert_leg(
        pool, leg_status="IN_TRANSIT", load_status="LOADED",
        trailer_number="77344", bol_number="B7", bol_image=None,
    )

    res = await commit(
        pool,
        intent(case_type="CASE_1_ORIGIN_DEPARTURE", bol_number="B7",
               primary_image_blob=IMG),
    )
    assert res["is_clean"] is True
    assert res["leg_id"] == open_leg
    assert (await get_leg(pool, open_leg))["bol_image"] == IMG


# ==========================================================================
# CASE 2 -- destination arrival
# ==========================================================================

async def test_arrival_completes_active_leg(pool):
    leg_id = await insert_leg(pool, leg_status="IN_TRANSIT", load_status="LOADED")

    res = await commit(
        pool,
        intent(case_type="CASE_2_DESTINATION_ARRIVAL",
               destination_location="E2F", door_number="45"),
    )
    assert res["is_clean"] is True
    assert res["leg_id"] == leg_id

    leg = await get_leg(pool, leg_id)
    assert leg["leg_status"] == "COMPLETED"
    assert leg["arrival_action"] == "LIVE_UNLOAD"
    assert leg["arrival_time"] is not None
    assert leg["dock_number"] == "45"


async def test_live_unload_sets_unloading_status(pool):
    leg_id = await insert_leg(pool, leg_status="IN_TRANSIT", load_status="LOADED")

    await commit(
        pool,
        intent(case_type="CASE_2_DESTINATION_ARRIVAL", action="LIVE_UNLOAD"),
    )
    leg = await get_leg(pool, leg_id)
    assert leg["leg_status"] == "UNLOADING"
    assert leg["arrival_action"] == "LIVE_UNLOAD"


async def test_live_load_sets_loading_status(pool):
    leg_id = await insert_leg(pool, leg_status="IN_TRANSIT", load_status="EMPTY")

    await commit(
        pool,
        intent(case_type="CASE_2_DESTINATION_ARRIVAL", action="LIVE_LOAD"),
    )
    leg = await get_leg(pool, leg_id)
    assert leg["leg_status"] == "LOADING"
    assert leg["arrival_action"] == "LIVE_LOAD"


async def test_bobtail_arrival_completes_leg(pool):
    leg_id = await insert_leg(
        pool, leg_status="IN_TRANSIT", load_status="EMPTY", is_bobtail=1,
    )

    await commit(pool, intent(case_type="CASE_2_DESTINATION_ARRIVAL"))
    leg = await get_leg(pool, leg_id)
    assert leg["arrival_action"] == "BOBTAIL_ARRIVE"
    assert leg["leg_status"] == "COMPLETED"


async def test_arrival_without_active_leg_is_a_noop(pool):
    res = await commit(
        pool, intent(case_type="CASE_2_DESTINATION_ARRIVAL",
                     destination_location="E2F"),
    )
    assert res["is_clean"] is True
    assert res["leg_id"] is None
    assert await all_legs(pool) == []


@pytest.mark.xfail(
    strict=True,
    reason="state_machine.py:330 passes `1 if receiver_signed else 0` into "
           "COALESCE(%s, receiver_signed). 0 is not NULL, so COALESCE is defeated "
           "and an arrival with no detected signature wipes a POD captured earlier. "
           "Lines 381 and 444 pass None here and are correct.",
)
async def test_arrival_preserves_existing_receiver_signature(pool):
    leg_id = await insert_leg(
        pool, leg_status="IN_TRANSIT", load_status="LOADED", receiver_signed=1,
    )

    await commit(
        pool,
        intent(case_type="CASE_2_DESTINATION_ARRIVAL", receiver_signed=False),
    )
    assert (await get_leg(pool, leg_id))["receiver_signed"] == 1


# ==========================================================================
# CASE 3 -- historical BOL patch
# ==========================================================================

async def test_historical_update_without_bol_raises_card(pool):
    res = await commit(
        pool, intent(case_type="CASE_HISTORICAL_BOL_UPDATE",
                     primary_image_blob=IMG),
    )
    assert res["is_clean"] is False
    assert "Unreadable Historical Paperwork" in res["card_text"]


async def test_historical_update_with_no_match_raises_card(pool):
    res = await commit(
        pool, intent(case_type="CASE_HISTORICAL_BOL_UPDATE",
                     bol_number="NOTFOUND", primary_image_blob=IMG),
    )
    assert res["is_clean"] is False
    assert "Unmatched Historical BOL" in res["card_text"]


@pytest.mark.xfail(
    strict=True,
    reason="CASE_HISTORICAL_BOL_UPDATE is unreachable. It patches the leg whose "
           "bol_number matches, but the duplicate-BOL guard at state_machine.py:92 "
           "returns DUPLICATE_BOL_OVERRIDE before the match block whenever that "
           "bol_number already exists -- which is exactly when this case applies.",
)
async def test_historical_update_patches_matching_leg(pool):
    leg_id = await insert_leg(
        pool, bol_number="B22", leg_status="COMPLETED",
        bol_image=None, receiver_signed=0,
    )

    res = await commit(
        pool,
        intent(case_type="CASE_HISTORICAL_BOL_UPDATE", bol_number="B22",
               primary_image_blob=IMG, receiver_signed=True, document_type="FG"),
    )
    assert res["is_clean"] is True
    assert res["leg_id"] == leg_id

    leg = await get_leg(pool, leg_id)
    assert leg["bol_image"] == IMG
    assert leg["receiver_signed"] == 1
    assert leg["document_type"] == "FG"


# ==========================================================================
# CASE AUTO RESOLVE -- uncaptioned paperwork
# ==========================================================================

async def test_auto_resolve_without_bol_raises_card(pool):
    res = await commit(
        pool, intent(case_type="CASE_AUTO_RESOLVE", primary_image_blob=IMG),
    )
    assert res["is_clean"] is False
    assert "Uncaptioned Image Scan Failed" in res["card_text"]


async def test_auto_resolve_links_to_open_loaded_leg(pool):
    leg_id = await insert_leg(
        pool, load_status="LOADED", leg_status="IN_TRANSIT",
        bol_number=None, bol_image=None,
    )

    res = await commit(
        pool,
        intent(case_type="CASE_AUTO_RESOLVE", bol_number="B24",
               primary_image_blob=IMG, shipper_signed=True, document_type="FG"),
    )
    assert res["is_clean"] is True
    assert res["leg_id"] == leg_id

    leg = await get_leg(pool, leg_id)
    assert leg["bol_number"] == "B24"
    assert leg["bol_image"] == IMG
    assert leg["shipper_signed"] == 1


async def test_auto_resolve_without_open_leg_raises_card(pool):
    res = await commit(
        pool,
        intent(case_type="CASE_AUTO_RESOLVE", bol_number="B25",
               primary_image_blob=IMG),
    )
    assert res["is_clean"] is False
    assert "No Open Leg Found" in res["card_text"]


# ==========================================================================
# Slang parsing
# ==========================================================================

@pytest.mark.xfail(
    strict=True,
    reason="state_machine.py:39 does a bare substring test for 'bt', so any word "
           "containing those letters ('doubt', 'subtotal', 'debt') flags the trip as "
           "bobtail, forces load_status to EMPTY and skips the BOL compliance guard. "
           "Needs word-boundary matching.",
)
async def test_bt_inside_a_word_does_not_flag_bobtail(pool):
    res = await commit(
        pool,
        intent(case_type="CASE_1_ORIGIN_DEPARTURE",
               raw_text="leaving 200 to E2F, no doubt running late",
               origin_location="200", destination_location="E2F",
               text_trailer="77344", bol_number="B26",
               primary_image_blob=IMG, load_status="LOADED"),
    )
    leg = await get_leg(pool, res["leg_id"])
    assert leg["is_bobtail"] == 0
    assert leg["load_status"] == "LOADED"


# ==========================================================================
# Location normalisation
# ==========================================================================

async def test_location_aliases_are_normalised(pool):
    await seed_locations(pool, [("E2F", "E2,EAST2,E-2F"), ("200", "200F,YARD200")])

    res = await commit(
        pool,
        intent(case_type="CASE_1_ORIGIN_DEPARTURE",
               origin_location="yard200", destination_location="east2",
               text_trailer="77344", bol_number="B27",
               primary_image_blob=IMG, load_status="LOADED"),
    )
    leg = await get_leg(pool, res["leg_id"])
    assert leg["origin_location"] == "200"
    assert leg["destination_location"] == "E2F"

