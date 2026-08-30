"""Behavioural coverage for state_machine.commit_trip_leg.

Tests marked ``xfail(strict=True)`` assert the behaviour the code is *supposed*
to have. They fail today because of a known defect, and the strict marker means
that once the defect is fixed the test turns into an error until the marker is
removed -- so a fix can never land silently.
"""

from datetime import timedelta

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
    seed_network,
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


async def test_same_site_departure_becomes_a_within_facility_move(pool):
    """"200F to 200R" is front to rear in one yard. Seen repeatedly in real
    traffic; recording it beats asking the dispatcher to fix it by hand."""
    await seed_network(pool)
    res = await commit(
        pool,
        intent(case_type="CASE_1_ORIGIN_DEPARTURE", raw_text="200F to 200R",
               origin_location="200F", destination_location="200R",
               load_status="EMPTY"),
    )
    assert res["is_clean"] is True
    leg = await get_leg(pool, res["leg_id"])
    assert leg["is_positioning_leg"] == 1
    assert leg["origin_location"] == leg["destination_location"]


async def test_stale_bol_records_movement_without_paperwork(pool):
    """A duplicate BOL means the driver attached the PREVIOUS load's paperwork.

    The document is wrong, but the trip is real: record the movement, discard
    every field derived from that document, and tell dispatch to chase a repost.
    """
    existing = await insert_leg(pool, bol_number="B100", leg_status="COMPLETED")

    res = await commit(
        pool,
        intent(case_type="CASE_1_ORIGIN_DEPARTURE", bol_number="B100",
               origin_location="200", destination_location="E2F",
               text_trailer="77344", load_status="LOADED",
               primary_image_blob=IMG, shipper_signed=True, document_type="FG"),
    )
    assert res["is_clean"] is False
    assert res["leg_id"] is not None and res["leg_id"] != existing

    leg = await get_leg(pool, res["leg_id"])
    assert leg["origin_location"] == "200"      # movement preserved
    assert leg["destination_location"] == "E2F"
    assert leg["trailer_number"] == "77344"
    assert leg["leg_status"] == "IN_TRANSIT"
    assert leg["bol_number"] is None            # stale paperwork discarded
    assert leg["bol_image"] is None
    assert leg["document_type"] == "UNKNOWN"
    assert leg["shipper_signed"] == 0

    assert "Stale BOL" in res["card_text"]
    assert "B100" in res["card_text"]
    assert str(existing) in res["card_text"]
    assert "repost" in res["card_text"].lower()


async def test_no_force_save_override_is_offered(pool):
    """The override button was removed: a stale BOL is a driver error to correct."""
    await insert_leg(pool, bol_number="B100", leg_status="COMPLETED")
    res = await commit(
        pool,
        intent(case_type="CASE_1_ORIGIN_DEPARTURE", bol_number="B100",
               origin_location="200", destination_location="E2F",
               text_trailer="77344"),
    )
    assert res.get("action_type") is None
    assert "force" not in res["card_text"].lower()


async def test_reposted_bol_heals_the_stale_bol_leg(pool):
    """End to end: stale BOL -> movement recorded -> repost completes the leg."""
    await insert_leg(pool, bol_number="B100", leg_status="COMPLETED")

    first = await commit(
        pool,
        intent(case_type="CASE_1_ORIGIN_DEPARTURE", bol_number="B100",
               origin_location="200", destination_location="E2F",
               text_trailer="77344", load_status="LOADED", primary_image_blob=IMG),
    )
    open_leg = first["leg_id"]

    # driver reposts the correct paperwork, no caption
    second = await commit(
        pool,
        intent(case_type="CASE_AUTO_RESOLVE", bol_number="B200",
               primary_image_blob=IMG, shipper_signed=True, document_type="FG"),
    )
    assert second["is_clean"] is True
    assert second["leg_id"] == open_leg

    leg = await get_leg(pool, open_leg)
    assert leg["bol_number"] == "B200"
    assert leg["bol_image"] == IMG
    assert leg["document_type"] == "FG"
    assert leg["shipper_signed"] == 1
    assert len(await all_legs(pool)) == 2   # the old leg plus this one, no extras


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


@pytest.mark.parametrize("phrase", [
    "heading to 200 bobtail",
    "heading to 200 bob tail",
    "200 bt",
    "b/t to 200",
    "no trailer, heading to 200",
    "tractor only today",
    "single tractor to 200",
])
async def test_bobtail_slang_is_still_detected(pool, phrase):
    """Guards against the word-boundary fix over-correcting and missing real slang."""
    res = await commit(
        pool,
        intent(case_type="CASE_1_ORIGIN_DEPARTURE", raw_text=phrase,
               origin_location="E2F", destination_location="200",
               text_trailer="77344"),
    )
    leg = await get_leg(pool, res["leg_id"])
    assert leg["is_bobtail"] == 1, f"{phrase!r} should flag bobtail"
    assert leg["load_status"] == "EMPTY"


@pytest.mark.parametrize("phrase", [
    "leaving 200 to E2F, no doubt running late",
    "200 to E2F, subtotal on the paperwork looks off",
    "heading to E2F, paying off a debt",
])
async def test_ordinary_words_containing_bt_are_not_bobtail(pool, phrase):
    res = await commit(
        pool,
        intent(case_type="CASE_1_ORIGIN_DEPARTURE", raw_text=phrase,
               origin_location="200", destination_location="E2F",
               text_trailer="77344", bol_number=f"B-{abs(hash(phrase)) % 99999}",
               primary_image_blob=IMG, load_status="LOADED"),
    )
    leg = await get_leg(pool, res["leg_id"])
    assert leg["is_bobtail"] == 0, f"{phrase!r} must not flag bobtail"
    assert leg["load_status"] == "LOADED"


# ==========================================================================
# Parser availability
# ==========================================================================

async def test_parse_failure_raises_manual_card(pool):
    """A parser outage must never look like casual chat."""
    res = await commit(
        pool,
        intent(case_type="PARSE_FAILED", raw_text="200 to e2f loaded 77344",
               parse_error="503 UNAVAILABLE"),
    )
    assert res["is_clean"] is False
    assert "Could Not Be Parsed" in res["card_text"]
    # the driver's original words must survive into the card for manual entry
    assert "200 to e2f loaded 77344" in res["card_text"]
    assert await all_legs(pool) == []


async def test_parse_failure_on_captionless_document_still_cards(pool):
    res = await commit(pool, intent(case_type="PARSE_FAILED", raw_text=""))
    assert res["is_clean"] is False
    assert "attached document only" in res["card_text"]


async def test_parse_failure_from_unknown_driver_stays_silent(pool):
    """Non-drivers must not be able to generate dispatch noise."""
    res = await commit(
        pool, intent(case_type="PARSE_FAILED", raw_text="hello"), did=999999,
    )
    assert res["card_text"] is None


async def test_none_work_related_still_silent(pool):
    """Genuine chatter must stay silent -- PARSE_FAILED is the only loud path."""
    res = await commit(pool, intent(case_type="NONE_WORK_RELATED", raw_text="lunch?"))
    assert res["card_text"] is None
    assert await all_legs(pool) == []


# ==========================================================================
# CASE 3 -- within-facility repositioning
# ==========================================================================

async def test_internal_move_is_its_own_leg(pool):
    """Every within-facility move is recorded separately; the trip leg it
    happened during is left alone."""
    trip = await insert_leg(
        pool, origin_location="SDS", destination_location="200",
        trailer_number="77344", dock_number="13",
        leg_status="COMPLETED", arrival_action="DROP",
    )

    res = await commit(
        pool,
        intent(case_type="CASE_3_INTRA_FACILITY_MOVE", raw_text="empty move #13 to #47",
               origin_dock="13", destination_dock="47",
               ocr_trailer="77344", load_status="EMPTY"),
    )
    assert res["is_clean"] is True
    assert res["leg_id"] != trip

    leg = await get_leg(pool, res["leg_id"])
    assert leg["is_positioning_leg"] == 1
    assert leg["origin_location"] == "200" and leg["destination_location"] == "200"
    assert leg["origin_dock"] == "13" and leg["destination_dock"] == "47"
    assert leg["trailer_number"] == "77344"
    assert leg["load_status"] == "EMPTY"
    assert leg["arrival_action"] == "DOCK_MOVE"
    assert leg["leg_status"] == "COMPLETED"
    assert leg["departure_time"] is not None and leg["arrival_time"] is not None

    trip_row = await get_leg(pool, trip)
    assert trip_row["origin_dock"] is None
    assert trip_row["destination_dock"] is None
    assert trip_row["dock_number"] == "13"


async def test_several_internal_moves_each_get_a_row(pool):
    """The case that folding into the trip leg would have destroyed: three
    moves between trips must produce three rows, not one overwritten pair."""
    await insert_leg(
        pool, origin_location="SDS", destination_location="200",
        trailer_number="77344", leg_status="COMPLETED",
    )

    moves = [("13", "47", "77344"), ("4", "13", "53012"), ("7", "YARD", "61002")]
    for frm, to, trailer in moves:
        res = await commit(
            pool,
            intent(case_type="CASE_3_INTRA_FACILITY_MOVE", origin_dock=frm,
                   destination_dock=to, ocr_trailer=trailer, load_status="EMPTY"),
        )
        assert res["is_clean"] is True

    legs = [l for l in await all_legs(pool) if l["is_positioning_leg"] == 1]
    assert len(legs) == 3
    assert [(l["origin_dock"], l["destination_dock"], l["trailer_number"]) for l in legs] == moves
    # facility inferred from the trip leg, then carried by each move
    assert all(l["origin_location"] == "200" for l in legs)


async def test_drop_to_yard_is_recorded(pool):
    await insert_leg(pool, destination_location="200", leg_status="COMPLETED")
    res = await commit(
        pool,
        intent(case_type="CASE_3_INTRA_FACILITY_MOVE",
               raw_text="empty dropped yard", destination_dock="YARD",
               ocr_trailer="53012", load_status="EMPTY"),
    )
    leg = await get_leg(pool, res["leg_id"])
    assert leg["destination_dock"] == "YARD"
    assert leg["arrival_action"] == "YARD_DROP"
    assert leg["is_positioning_leg"] == 1


# --------------------------------------------------------------------------
# A drop at the destination is the arrival, not a yard move
# --------------------------------------------------------------------------

async def test_drop_at_the_destination_completes_the_open_trip(pool):
    """From live data: "Pickup empty trailer pactra yard to sds" at 07:32, then
    "Drop empty trailer at sds yard D021 #25773" at 08:09.

    The second message ends the trip, but it reads exactly like an internal
    move -- one facility, a position, no travel -- so the bot opened a second
    SDS -> SDS leg and left the real one running until the next departure
    closed it, 11 minutes after the driver actually got there.
    """
    await seed_network(pool)
    departed = eastern_now() - timedelta(minutes=37)
    trip = await insert_leg(
        pool, origin_location="200F", destination_location="SDS",
        trailer_number="UNKNOWN", load_status="EMPTY",
        leg_status="IN_TRANSIT", departure_time=ts(departed),
    )

    dropped = eastern_now()
    res = await commit(
        pool,
        intent(case_type="CASE_3_INTRA_FACILITY_MOVE",
               raw_text="Drop empty trailer at sds yard D021 #25773",
               origin_location="SDS", destination_location="SDS",
               destination_dock="YARD", do_number="21",
               text_trailer="25773", load_status="EMPTY"),
        when=ts(dropped),
    )
    assert res["is_clean"] is True
    assert res["leg_id"] == trip, "the drop belongs to the trip already running"

    leg = await get_leg(pool, trip)
    assert leg["leg_status"] == "COMPLETED"
    assert leg["arrival_time"].strftime("%H:%M") == dropped.strftime("%H:%M")
    assert leg["arrival_action"] == "DROP_YARD"
    assert leg["destination_dock"] == "YARD"
    assert leg["do_number"] == "21"
    assert leg["trailer_number"] == "25773"

    assert [l for l in await all_legs(pool) if l["is_positioning_leg"] == 1] == [], \
        "no phantom SDS -> SDS leg"


async def test_a_move_between_two_positions_is_never_the_arrival(pool):
    """"door 7 to yard" names somewhere the driver moved off, so it is a real
    move whatever trip happens to be open."""
    await seed_network(pool)
    trip = await insert_leg(
        pool, origin_location="SDS", destination_location="200F",
        trailer_number="77344", leg_status="IN_TRANSIT",
    )
    res = await commit(
        pool,
        intent(case_type="CASE_3_INTRA_FACILITY_MOVE",
               raw_text="moved 44821 door 7 to yard", origin_dock="7",
               destination_dock="YARD", ocr_trailer="44821",
               load_status="EMPTY"),
    )
    assert res["leg_id"] != trip
    assert (await get_leg(pool, res["leg_id"]))["is_positioning_leg"] == 1
    assert (await get_leg(pool, trip))["leg_status"] == "IN_TRANSIT"


async def test_a_drop_somewhere_else_is_not_the_arrival(pool):
    """Dropping in the 200 yard says nothing about a trip running to SDS."""
    await seed_network(pool)
    trip = await insert_leg(
        pool, origin_location="200F", destination_location="SDS",
        leg_status="IN_TRANSIT",
    )
    res = await commit(
        pool,
        intent(case_type="CASE_3_INTRA_FACILITY_MOVE",
               raw_text="drop empty 200 r yard", origin_location="200R",
               destination_location="200R", destination_dock="YARD",
               load_status="EMPTY"),
    )
    assert (await get_leg(pool, res["leg_id"]))["is_positioning_leg"] == 1
    assert (await get_leg(pool, trip))["leg_status"] == "IN_TRANSIT"


async def test_the_trailer_named_at_the_drop_corrects_the_leg(pool):
    """From live data: Sokhwan Yun's 11:18 SDS run was recorded with trailer
    25773, inherited by the CASE 1 fallback from three legs earlier. He had
    swapped trailers at 08:20 and was hauling 77155 -- which is what he named
    on dropping it, and what the dispatcher logged. A first-hand report from
    the destination beats a value carried forward, so it must not be read as
    "different trailer, therefore a yard move"."""
    await seed_network(pool)
    trip = await insert_leg(
        pool, origin_location="200F", destination_location="SDS",
        trailer_number="25773", load_status="EMPTY", leg_status="IN_TRANSIT",
    )
    res = await commit(
        pool,
        intent(case_type="CASE_3_INTRA_FACILITY_MOVE",
               raw_text="Drop empty trailer at sds yard D026 #77155",
               origin_location="SDS", destination_location="SDS",
               destination_dock="YARD", do_number="26",
               text_trailer="77155", load_status="EMPTY"),
    )
    assert res["leg_id"] == trip

    leg = await get_leg(pool, trip)
    assert leg["leg_status"] == "COMPLETED"
    assert leg["trailer_number"] == "77155", "the trailer actually dropped"
    assert leg["do_number"] == "26"
    assert [l for l in await all_legs(pool) if l["is_positioning_leg"] == 1] == []


async def test_front_to_rear_while_a_trip_is_open_stays_a_move(pool):
    """"200F to 200R" is two named places -- a reposition, not a drop on
    arrival -- even when a leg to 200 is still marked in transit."""
    await seed_network(pool)
    trip = await insert_leg(
        pool, origin_location="SDS", destination_location="200F",
        leg_status="IN_TRANSIT",
    )
    res = await commit(
        pool,
        intent(case_type="CASE_1_ORIGIN_DEPARTURE", raw_text="200F to 200R",
               origin_location="200F", destination_location="200R",
               load_status="EMPTY"),
    )
    move = await get_leg(pool, res["leg_id"])
    assert move["is_positioning_leg"] == 1
    assert move["id"] != trip


async def test_drop_after_the_arrival_was_already_reported_is_a_move(pool):
    """Once the trip is closed the next drop is a yard move again."""
    await seed_network(pool)
    trip = await insert_leg(
        pool, origin_location="200F", destination_location="SDS",
        leg_status="IN_TRANSIT",
    )
    first = await commit(
        pool,
        intent(case_type="CASE_3_INTRA_FACILITY_MOVE",
               origin_location="SDS", destination_location="SDS",
               destination_dock="YARD", text_trailer="25773",
               load_status="EMPTY"),
    )
    assert first["leg_id"] == trip

    second = await commit(
        pool,
        intent(case_type="CASE_3_INTRA_FACILITY_MOVE",
               origin_location="SDS", destination_location="SDS",
               destination_dock="YARD", text_trailer="61002",
               load_status="EMPTY"),
    )
    assert second["leg_id"] != trip
    assert (await get_leg(pool, second["leg_id"]))["is_positioning_leg"] == 1


# --------------------------------------------------------------------------
# Doors say which building, and finish the job they end
# --------------------------------------------------------------------------

async def test_dock_move_across_the_site_records_both_buildings(pool):
    """From live data: "Finish live unloading at pactra #3 move to Dock 47".

    Door 3 is FG inbound at 200F, door 47 is RM outbound at 200R, so this is
    the trailer coming off the inbound door and being staged on a vacant
    outbound one -- for himself or whoever takes the next RM round. The bot
    read "move to Dock 47" as travel and recorded a leg to UNKNOWN.
    """
    await seed_network(pool)
    delivery = await insert_leg(
        pool, origin_location="SDS", destination_location="200F",
        trailer_number="77209", load_status="LOADED", leg_status="ARRIVED",
        arrival_time=ts(eastern_now() - timedelta(minutes=26)),
    )

    res = await commit(
        pool,
        intent(case_type="CASE_3_INTRA_FACILITY_MOVE",
               raw_text="Finish live unloading at pactra #3  move to Dock 47",
               origin_location="200F", destination_location="200F",
               origin_dock="3", destination_dock="47",
               work_finished=True, load_status="EMPTY"),
    )
    assert res["is_clean"] is True

    move = await get_leg(pool, res["leg_id"])
    assert move["is_positioning_leg"] == 1
    assert move["origin_location"] == "200F", "door 3 is the FG inbound side"
    assert move["destination_location"] == "200R", "door 47 is RM outbound"
    assert move["origin_dock"] == "3" and move["destination_dock"] == "47"
    assert move["arrival_action"] == "DOCK_MOVE"
    assert move["load_status"] == "EMPTY"

    # the unload it ends belongs to the trip that brought the load in
    assert (await get_leg(pool, delivery))["finished_time"] is not None


async def test_a_completion_is_not_stamped_on_a_trip_that_ended_elsewhere(pool):
    """When the departure was never recorded -- the driver did not say where
    he was taking the load -- the newest leg is an earlier run to somewhere
    else. Stamping it would file this unload against the wrong trip."""
    await seed_network(pool)
    elsewhere = await insert_leg(
        pool, origin_location="200F", destination_location="SDS",
        leg_status="COMPLETED",
        arrival_time=ts(eastern_now() - timedelta(minutes=90)),
    )
    res = await commit(
        pool,
        intent(case_type="CASE_3_INTRA_FACILITY_MOVE",
               raw_text="Finish live unloading at pactra #3 move to Dock 47",
               origin_location="200F", destination_location="200F",
               origin_dock="3", destination_dock="47",
               work_finished=True, load_status="EMPTY"),
    )
    assert res["is_clean"] is True
    assert (await get_leg(pool, elsewhere))["finished_time"] is None
    assert (await get_leg(pool, res["leg_id"]))["destination_location"] == "200R"


async def test_a_move_within_one_building_stays_there(pool):
    """3 and 13 are both FG inbound doors: one building, no crossing."""
    await seed_network(pool)
    await insert_leg(pool, destination_location="200F", leg_status="COMPLETED")
    res = await commit(
        pool,
        intent(case_type="CASE_3_INTRA_FACILITY_MOVE",
               raw_text="empty move #3 to #13", origin_dock="3",
               destination_dock="13", load_status="EMPTY"),
    )
    move = await get_leg(pool, res["leg_id"])
    assert move["origin_location"] == "200F"
    assert move["destination_location"] == "200F"


async def test_an_unmapped_door_keeps_the_facility_it_had(pool):
    """Door 46 sits between two bands and the upper bounds are approximate, so
    an unmapped door must not drag the move to another building."""
    await seed_network(pool)
    await insert_leg(pool, destination_location="200R", leg_status="COMPLETED")
    res = await commit(
        pool,
        intent(case_type="CASE_3_INTRA_FACILITY_MOVE",
               raw_text="empty move #46 to yard", origin_dock="46",
               destination_dock="YARD", load_status="EMPTY"),
    )
    move = await get_leg(pool, res["leg_id"])
    assert move["origin_location"] == "200R"
    assert move["destination_location"] == "200R"


async def test_doors_at_another_site_are_not_read_as_200_doors(pool):
    """Only 200 has bands recorded. A door move at Eagle 2 must stay at E2F."""
    await seed_network(pool)
    await insert_leg(pool, destination_location="E2F", leg_status="COMPLETED")
    res = await commit(
        pool,
        intent(case_type="CASE_3_INTRA_FACILITY_MOVE",
               raw_text="empty move #3 to #47", origin_dock="3",
               destination_dock="47", load_status="EMPTY"),
    )
    move = await get_leg(pool, res["leg_id"])
    assert move["origin_location"] == "E2F"
    assert move["destination_location"] == "E2F"


# --------------------------------------------------------------------------
# A load hooked with no destination named
# --------------------------------------------------------------------------

async def test_loaded_pickup_with_no_destination_raises_a_card(pool):
    """From live data: "Load trailer pickup sds dock28 #77209" at 14:24. The
    driver forgot to say where he was taking it, so the whole SDS -> 200F run
    went missing from the day -- silently, because the message reads as a yard
    move. Nothing can be recorded without a destination, but dispatch has to
    know the message happened."""
    await seed_network(pool)
    await insert_leg(pool, destination_location="SDS", leg_status="COMPLETED")

    res = await commit(
        pool,
        intent(case_type="CASE_3_INTRA_FACILITY_MOVE",
               raw_text="Load trailer pickup sds dock28 #77209",
               origin_location="SDS", destination_location="SDS",
               destination_dock="28", text_trailer="77209",
               load_status="LOADED"),
    )
    assert res["is_clean"] is False
    assert res["leg_id"] is None, "a leg to UNKNOWN would enter a round as a real trip"
    assert "Load Picked Up With No Destination" in res["card_text"]
    assert "SDS" in res["card_text"] and "77209" in res["card_text"]
    assert "28" in res["card_text"]

    assert [l for l in await all_legs(pool) if l["is_positioning_leg"] == 1] == []


async def test_pickup_at_the_destination_stamps_arrival_and_still_cards(pool):
    """Hooking the next load on arrival: the trip that just ended is safe, the
    one starting is not."""
    await seed_network(pool)
    trip = await insert_leg(
        pool, origin_location="200F", destination_location="SDS",
        load_status="EMPTY", leg_status="IN_TRANSIT",
    )
    res = await commit(
        pool,
        intent(case_type="CASE_3_INTRA_FACILITY_MOVE",
               raw_text="Load trailer pickup sds dock28 #77209",
               origin_location="SDS", destination_location="SDS",
               destination_dock="28", text_trailer="77209",
               load_status="LOADED"),
    )
    assert res["leg_id"] == trip
    assert res["is_clean"] is False
    assert "Load Picked Up With No Destination" in res["card_text"]

    leg = await get_leg(pool, trip)
    assert leg["leg_status"] == "COMPLETED", "the inbound run still ends here"
    assert leg["arrival_time"] is not None


async def test_orphan_loaded_pickup_read_as_an_arrival_also_cards(pool):
    """The same message classified CASE 2 instead of CASE 3 must not go quiet."""
    await seed_network(pool)
    res = await commit(
        pool,
        intent(case_type="CASE_2_DESTINATION_ARRIVAL",
               raw_text="Load trailer pickup sds dock28 #77209",
               origin_location="SDS", door_number="28",
               text_trailer="77209", load_status="LOADED"),
    )
    assert res["is_clean"] is False
    assert res["leg_id"] is None
    assert "Load Picked Up With No Destination" in res["card_text"]


async def test_a_plain_arrival_with_no_open_trip_stays_quiet(pool):
    """Only a pickup is a lost departure; a bare "arrived" is nothing."""
    res = await commit(
        pool,
        intent(case_type="CASE_2_DESTINATION_ARRIVAL", raw_text="arrived sds",
               origin_location="SDS"),
    )
    assert res["is_clean"] is True
    assert res["card_text"] is None


async def test_a_loaded_yard_move_is_not_a_lost_pickup(pool):
    """"loaded move #4 to #13" names where it moved off. Still a yard move."""
    await seed_network(pool)
    await insert_leg(pool, destination_location="200F", leg_status="COMPLETED")
    res = await commit(
        pool,
        intent(case_type="CASE_3_INTRA_FACILITY_MOVE",
               raw_text="loaded move #4 to #13", origin_dock="4",
               destination_dock="13", load_status="LOADED"),
    )
    assert res["is_clean"] is True
    assert res["card_text"] is None
    assert (await get_leg(pool, res["leg_id"]))["is_positioning_leg"] == 1


async def test_a_pickup_that_names_its_destination_is_a_departure(pool):
    """The control: "pickup load sds yard D033 to pactra" is a normal trip."""
    await seed_network(pool)
    res = await commit(
        pool,
        intent(case_type="CASE_1_ORIGIN_DEPARTURE",
               raw_text="Load trailer pickup sds yard D033 to pactra",
               origin_location="SDS", destination_location="200F",
               do_number="33", text_trailer="77209", load_status="LOADED",
               bol_number="B1", primary_image_blob=IMG, document_type="FG"),
    )
    assert res["is_clean"] is True
    leg = await get_leg(pool, res["leg_id"])
    assert leg["origin_location"] == "SDS"
    assert leg["destination_location"] == "200F"
    assert leg["leg_status"] == "IN_TRANSIT"


async def test_internal_move_with_no_known_facility_raises_card(pool):
    """Nothing to infer the facility from -- record it, but flag it, since a
    row with an unknown facility is useless to accounting."""
    res = await commit(
        pool,
        intent(case_type="CASE_3_INTRA_FACILITY_MOVE", raw_text="empty move #13 to #47",
               origin_dock="13", destination_dock="47"),
    )
    assert res["is_clean"] is False
    assert "Incomplete Internal Move" in res["card_text"]
    assert (await get_leg(pool, res["leg_id"]))["is_positioning_leg"] == 1


async def test_positioning_leg_does_not_trigger_pod_guard(pool):
    """A cleanup leg must not be read as the prior loaded trip needing a POD."""
    await insert_leg(
        pool, is_positioning_leg=1, load_status="LOADED", document_type="FG",
        receiver_signed=0, origin_location="200", destination_location="200",
        leg_status="COMPLETED",
    )
    res = await commit(
        pool,
        intent(case_type="CASE_1_ORIGIN_DEPARTURE", origin_location="200",
               destination_location="E2F", text_trailer="77344", load_status="EMPTY"),
    )
    assert res["is_clean"] is True
    assert res["card_text"] is None


async def test_positioning_leg_is_not_claimed_by_a_later_arrival(pool):
    """Recorded COMPLETED, so CASE 2 cannot mistake it for an open trip."""
    cleanup = await commit(
        pool,
        intent(case_type="CASE_3_INTRA_FACILITY_MOVE",
               origin_dock="13", destination_dock="47", origin_location="200"),
    )
    res = await commit(pool, intent(case_type="CASE_2_DESTINATION_ARRIVAL"))
    assert res["leg_id"] is None, "arrival must not attach to a positioning leg"
    assert (await get_leg(pool, cleanup["leg_id"]))["arrival_action"] == "DOCK_MOVE"


async def test_departure_after_dock_work_completes_the_trip_leg(pool):
    """The full 200 flow: arrive from SDS, several internal moves, then
    'live loading finished load 200 to E2F' closes leg A and opens leg B."""
    leg_a = await insert_leg(
        pool, origin_location="SDS", destination_location="200",
        trailer_number="77344", bol_number="B-IN", bol_image=IMG,
        leg_status="ARRIVED", dock_number="13",
    )
    for frm, to in (("13", "47"), ("4", "13")):
        await commit(
            pool,
            intent(case_type="CASE_3_INTRA_FACILITY_MOVE",
                   origin_dock=frm, destination_dock=to, load_status="EMPTY"),
        )

    res = await commit(
        pool,
        intent(case_type="CASE_1_ORIGIN_DEPARTURE",
               raw_text="live loading finished load 200 to E2F",
               origin_location="200", destination_location="E2F",
               text_trailer="77344", bol_number="B-OUT",
               primary_image_blob=IMG, load_status="LOADED", document_type="FG"),
    )
    assert res["is_clean"] is True
    assert (await get_leg(pool, leg_a))["leg_status"] == "COMPLETED"

    leg_b = await get_leg(pool, res["leg_id"])
    assert leg_b["origin_location"] == "200" and leg_b["destination_location"] == "E2F"
    assert leg_b["leg_status"] == "IN_TRANSIT"

    # the two internal moves survive as their own completed rows
    positioning = [l for l in await all_legs(pool) if l["is_positioning_leg"] == 1]
    assert len(positioning) == 2
    assert all(l["leg_status"] == "COMPLETED" for l in positioning)


# ==========================================================================
# Round grouping -- driven by the seven defined routes
# ==========================================================================

async def _depart(pool, origin, dest, **kw):
    res = await commit(
        pool,
        intent(case_type="CASE_1_ORIGIN_DEPARTURE", origin_location=origin,
               destination_location=dest, text_trailer="77344", **kw),
    )
    return res["leg_id"]


async def _arrive(pool):
    await commit(pool, intent(case_type="CASE_2_DESTINATION_ARRIVAL"))


async def _run(pool, stops, **kw):
    """Drive a circuit, returning (leg_id, round_number, route_code) per leg."""
    legs = []
    for origin, dest in zip(stops, stops[1:]):
        legs.append(await _depart(pool, origin, dest, **kw))
        await _arrive(pool)
    out = []
    for leg_id in legs:
        row = await get_leg(pool, leg_id)
        out.append((row["round_number"], row["route_code"]))
    return out


async def test_route_1_split_rm_is_one_round(pool):
    """200 -> E2F -> E2R -> SDS -> 200. RM can be split across E2F and E2R,
    and the whole circuit is a single traversal of route 1."""
    await seed_network(pool)
    result = await _run(pool, ["200F", "E2F", "E2R", "SDS", "200F"], load_status="EMPTY")
    assert [r for r, _ in result] == [1, 1, 1, 1]
    assert result[-1][1] == "R1"


async def test_spot_stop_splits_the_round(pool):
    """1380 is on no route from 200, so it is a spot delivery of its own."""
    await seed_network(pool)
    result = await _run(pool, ["200F", "E2F", "E2R", "1380", "200F"], load_status="EMPTY")
    rounds = [r for r, _ in result]
    assert rounds == [1, 1, 2, 2], rounds
    assert [c for _, c in result][2:] == ["SPOT", "SPOT"]


async def test_revisit_does_not_split_but_spot_does(pool):
    """200 -> E2F -> E2R -> E2F -> 100 -> 200 is two rounds, not three:
    revisiting E2F stays on route 1; only 100 breaks out."""
    await seed_network(pool)
    result = await _run(pool, ["200F", "E2F", "E2R", "E2F", "100", "200F"],
                        load_status="EMPTY")
    rounds = [r for r, _ in result]
    assert rounds == [1, 1, 1, 2, 2], rounds


async def test_reassignment_needs_no_intervention(pool):
    """Driver switches from the 200 circuit to shuttling E1 <-> 210. Route 5 is
    anchored at E1, so each cycle closes on its own with nothing typed."""
    await seed_network(pool)
    result = await _run(
        pool, ["200F", "E1", "210", "E1", "210", "E1", "200F"], load_status="EMPTY")
    rounds = [r for r, _ in result]
    codes = [c for _, c in result]

    assert rounds[0] == 1 and codes[0] == "SPOT", "200 -> E1 is off-route transit"
    assert rounds[1] == rounds[2] == 2, "first E1 <-> 210 cycle"
    assert codes[1] == codes[2] == "R5"
    assert rounds[3] == rounds[4] == 3, "second cycle is its own round"
    assert rounds[5] == 4 and codes[5] == "SPOT", "E1 -> 200 is off-route transit"


@pytest.mark.parametrize("stops,code", [
    (["200F", "SDS", "200F"], "R2"),
    (["SDS", "200F", "SDS"], "R3"),
    (["SDS", "7634", "SDS"], "R4"),
    (["E1", "210", "E1"], "R5"),
    (["3551", "E1", "3551"], "R6"),
    (["3551", "E2F", "3551"], "R6"),
    (["200F", "7634", "200F"], "R7"),
])
async def test_each_defined_route_is_one_round(pool, stops, code):
    await seed_network(pool)
    result = await _run(pool, stops, load_status="EMPTY")
    assert [r for r, _ in result] == [1] * len(result), f"{stops} split"
    assert result[-1][1] == code


async def test_front_and_rear_are_one_site_for_closing(pool):
    """Leaving 200R and returning to 200F closes the round: same yard."""
    await seed_network(pool)
    result = await _run(pool, ["200R", "E2F", "200F"], load_status="EMPTY")
    assert [r for r, _ in result] == [1, 1]

    nxt = await _depart(pool, "200F", "SDS", load_status="EMPTY")
    assert (await get_leg(pool, nxt))["round_number"] == 2


async def test_bare_200_normalises_to_front(pool):
    await seed_network(pool)
    leg = await _depart(pool, "200", "SDS", load_status="EMPTY")
    assert (await get_leg(pool, leg))["origin_location"] == "200F"


async def test_dock_moves_join_the_current_round(pool):
    await seed_network(pool)
    leg = await _depart(pool, "200F", "E2F", load_status="EMPTY")
    await _arrive(pool)
    res = await commit(
        pool,
        intent(case_type="CASE_3_INTRA_FACILITY_MOVE",
               origin_dock="13", destination_dock="47", load_status="EMPTY"),
    )
    move = await get_leg(pool, res["leg_id"])
    assert move["is_positioning_leg"] == 1
    assert move["round_number"] == (await get_leg(pool, leg))["round_number"]


async def test_unregistered_sender_is_recorded_for_the_roster(pool):
    """A driver missing from driver_profiles is ignored forever and silently.
    Their Telegram id exists nowhere else, so capture it when they message."""
    for _ in range(3):
        res = await commit(
            pool,
            intent(case_type="CASE_1_ORIGIN_DEPARTURE", origin_location="200F",
                   destination_location="E2F", raw_text="200 to e2f"),
            did=888777,
        )
        assert res["is_clean"] is False
        assert res["card_text"] is None, "must stay silent to the driver"

    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT user_id, display_name, message_count "
                "FROM unknown_senders WHERE user_id = 888777;"
            )
            row = await cur.fetchone()
    assert row is not None, "unregistered sender should be recorded"
    assert row[2] == 3, "repeat messages should count, not duplicate"
    assert await all_legs(pool) == []


async def test_registered_driver_is_not_recorded_as_unknown(pool):
    await commit(
        pool,
        intent(case_type="CASE_1_ORIGIN_DEPARTURE", origin_location="200F",
               destination_location="E2F", text_trailer="77344",
               load_status="EMPTY"),
    )
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT COUNT(*) FROM unknown_senders;")
            (n,) = await cur.fetchone()
    assert n == 0


# ==========================================================================
# Wrong destination
# ==========================================================================

async def test_arriving_off_route_raises_a_card(pool):
    """Departure said E2F, arrival says SDS. The leg records what was reported,
    and dispatch is told while the driver is still on site."""
    await seed_network(pool)
    leg = await insert_leg(
        pool, origin_location="200F", destination_location="E2F",
        trailer_number="77344", leg_status="IN_TRANSIT",
    )
    res = await commit(
        pool,
        intent(case_type="CASE_2_DESTINATION_ARRIVAL", destination_location="SDS"),
    )
    assert res["is_clean"] is False
    assert "Wrong Destination" in res["card_text"]
    assert "E2F" in res["card_text"] and "SDS" in res["card_text"]
    assert res["leg_id"] == leg
    assert (await get_leg(pool, leg))["arrival_time"] is not None


async def test_eagle_2_front_and_rear_is_not_a_wrong_destination(pool):
    """Routed to E2F, arrives at E2R. Same plant at 310 Nexus Dr, same site --
    the door used is not a routing mistake."""
    await seed_network(pool)
    await insert_leg(pool, origin_location="200F", destination_location="E2F",
                     leg_status="IN_TRANSIT")
    res = await commit(
        pool,
        intent(case_type="CASE_2_DESTINATION_ARRIVAL", destination_location="E2R"),
    )
    assert res["is_clean"] is True
    assert res["card_text"] is None


async def test_arriving_where_expected_is_silent(pool):
    await seed_network(pool)
    await insert_leg(pool, origin_location="200F", destination_location="E2F",
                     leg_status="IN_TRANSIT")
    res = await commit(
        pool,
        intent(case_type="CASE_2_DESTINATION_ARRIVAL", destination_location="E2F"),
    )
    assert res["is_clean"] is True
    assert res["card_text"] is None


async def test_front_and_rear_is_not_a_wrong_destination(pool):
    """Routed to 200F, arrives at 200R. Same yard, not a mistake."""
    await seed_network(pool)
    await insert_leg(pool, origin_location="SDS", destination_location="200F",
                     leg_status="IN_TRANSIT")
    res = await commit(
        pool,
        intent(case_type="CASE_2_DESTINATION_ARRIVAL", destination_location="200R"),
    )
    assert res["is_clean"] is True
    assert res["card_text"] is None


async def test_arrival_without_a_named_facility_is_silent(pool):
    """Most arrivals are just 'arrived' or 'at door 45' with no facility, which
    must not be read as a mismatch."""
    await seed_network(pool)
    await insert_leg(pool, origin_location="200F", destination_location="E2F",
                     leg_status="IN_TRANSIT")
    res = await commit(
        pool, intent(case_type="CASE_2_DESTINATION_ARRIVAL", door_number="45"),
    )
    assert res["is_clean"] is True
    assert res["card_text"] is None


async def test_named_facility_is_not_stored_as_a_dock(pool):
    """From a real message: "Hong il pyo drop empty 200 r yard".

    The driver named the site, and the parser put 200R into origin_dock. A
    facility is never a dock, so it is rejected there and used as the
    location instead."""
    await seed_network(pool)
    res = await commit(
        pool,
        intent(case_type="CASE_3_INTRA_FACILITY_MOVE",
               raw_text="Hong il pyo drop empty 200 r yard",
               origin_dock="200R", destination_dock="YARD",
               ocr_trailer="77208", load_status="EMPTY"),
    )
    leg = await get_leg(pool, res["leg_id"])
    assert leg["origin_dock"] is None, "200R is a facility, not a dock"
    assert leg["destination_dock"] == "YARD"
    assert leg["origin_location"] == "200R"
    assert leg["destination_location"] == "200R"
    assert leg["trailer_number"] == "77208"
    assert leg["arrival_action"] == "YARD_DROP"


async def test_parser_naming_the_facility_properly_also_works(pool):
    """The same message once the prompt puts the facility where it belongs."""
    await seed_network(pool)
    res = await commit(
        pool,
        intent(case_type="CASE_3_INTRA_FACILITY_MOVE",
               origin_location="200R", destination_location="200R",
               destination_dock="YARD", ocr_trailer="77208",
               load_status="EMPTY"),
    )
    leg = await get_leg(pool, res["leg_id"])
    assert leg["origin_location"] == "200R"
    assert leg["destination_dock"] == "YARD"
    assert leg["origin_dock"] is None


async def test_real_dock_numbers_are_still_kept(pool):
    """The guard must not reject genuine positions."""
    await seed_network(pool)
    await insert_leg(pool, destination_location="200F", leg_status="COMPLETED")
    res = await commit(
        pool,
        intent(case_type="CASE_3_INTRA_FACILITY_MOVE",
               origin_dock="74", destination_dock="47", load_status="EMPTY"),
    )
    leg = await get_leg(pool, res["leg_id"])
    assert leg["origin_dock"] == "74"
    assert leg["destination_dock"] == "47"


async def test_bare_200_in_a_dock_field_is_also_rejected(pool):
    """200 is an alias for 200F, so it must be caught as a facility too."""
    await seed_network(pool)
    res = await commit(
        pool,
        intent(case_type="CASE_3_INTRA_FACILITY_MOVE",
               origin_dock="200", destination_dock="YARD", load_status="EMPTY"),
    )
    leg = await get_leg(pool, res["leg_id"])
    assert leg["origin_dock"] is None
    assert leg["origin_location"] == "200F"


async def test_the_same_drop_reported_twice_is_one_leg(pool):
    """From live data: Ilpyo Hong reported dropping trailer 77208 in the 200
    yard twice, eight minutes apart -- once with the signed BOL, once with a
    photo of the parked trailer. One event, one leg.

    Note the captions resolved to 200F and 200R, so the match is on site."""
    await seed_network(pool)
    # the delivery he had just completed, still awaiting its signed paperwork
    delivery = await insert_leg(
        pool, origin_location="SDS", destination_location="7634",
        trailer_number="77208", bol_number=None, bol_image=None,
        leg_status="COMPLETED",
    )
    first = await commit(
        pool,
        intent(case_type="CASE_3_INTRA_FACILITY_MOVE",
               raw_text="Hong il pyo drop empty 200 yard",
               origin_location="200", destination_location="200",
               destination_dock="YARD", load_status="EMPTY",
               bol_number="0872726355", primary_image_blob=IMG,
               receiver_signed=True),
    )
    second = await commit(
        pool,
        intent(case_type="CASE_3_INTRA_FACILITY_MOVE",
               raw_text="Hong il pyo drop empty 200 r yard",
               origin_location="200R", destination_location="200R",
               destination_dock="YARD", ocr_trailer="77208",
               load_status="EMPTY"),
    )
    assert second["leg_id"] == first["leg_id"], "one drop, one leg"

    positioning = [l for l in await all_legs(pool) if l["is_positioning_leg"] == 1]
    assert len(positioning) == 1

    leg = positioning[0]
    assert leg["trailer_number"] == "77208", "trailer from the second report"
    assert leg["destination_dock"] == "YARD"
    assert leg["bol_number"] is None, "a yard drop has no paperwork of its own"

    # the signed BOL belongs to the delivery he had just finished
    delivered = await get_leg(pool, delivery)
    assert delivered["bol_number"] == "0872726355"
    assert delivered["bol_image"] == IMG
    assert delivered["receiver_signed"] == 1
    assert delivered["paperwork_time"] is not None


async def test_a_genuinely_different_move_still_gets_its_own_leg(pool):
    """Two drops to different positions are two events."""
    await seed_network(pool)
    first = await commit(
        pool,
        intent(case_type="CASE_3_INTRA_FACILITY_MOVE", origin_location="200F",
               destination_location="200F", origin_dock="13",
               destination_dock="47", load_status="EMPTY"),
    )
    second = await commit(
        pool,
        intent(case_type="CASE_3_INTRA_FACILITY_MOVE", origin_location="200F",
               destination_location="200F", origin_dock="4",
               destination_dock="13", load_status="EMPTY"),
    )
    assert second["leg_id"] != first["leg_id"]
    assert len([l for l in await all_legs(pool) if l["is_positioning_leg"]]) == 2


async def test_a_different_trailer_is_a_different_move(pool):
    await seed_network(pool)
    first = await commit(
        pool,
        intent(case_type="CASE_3_INTRA_FACILITY_MOVE", origin_location="200F",
               destination_location="200F", destination_dock="YARD",
               ocr_trailer="77208", load_status="EMPTY"),
    )
    second = await commit(
        pool,
        intent(case_type="CASE_3_INTRA_FACILITY_MOVE", origin_location="200F",
               destination_location="200F", destination_dock="YARD",
               ocr_trailer="53012", load_status="EMPTY"),
    )
    assert second["leg_id"] != first["leg_id"]


async def test_finished_plus_departure_records_the_trip(pool):
    """From live data, and the worst kind of miss: "7634 unloading finished
    Empty to 200R" was classified as a completion, so the departure was never
    recorded at all. A completion stamps a time; a departure records a trip.
    Losing the trip loses the movement entirely."""
    await seed_network(pool)
    previous = await insert_leg(
        pool, origin_location="SDS", destination_location="7634",
        trailer_number="77208", leg_status="UNLOADING",
    )
    res = await commit(
        pool,
        intent(case_type="CASE_1_ORIGIN_DEPARTURE", work_finished=True,
               raw_text="7634 unloading finished Empty to 200R",
               origin_location="7634", destination_location="200R",
               load_status="EMPTY"),
    )
    assert res["leg_id"] is not None, "the departure must be recorded"

    leg = await get_leg(pool, res["leg_id"])
    assert leg["origin_location"] == "7634"
    assert leg["destination_location"] == "200R"
    assert leg["load_status"] == "EMPTY"
    assert (await get_leg(pool, previous))["finished_time"] is not None


async def test_a_facility_code_is_never_stored_as_a_trailer(pool):
    """7634 read as a trailer number lost the origin and would have corrupted
    that trailer's history."""
    await seed_network(pool)
    res = await commit(
        pool,
        intent(case_type="CASE_1_ORIGIN_DEPARTURE",
               raw_text="7634 unloading finished Empty to 200R",
               origin_location="7634", destination_location="200R",
               text_trailer="7634", load_status="EMPTY"),
    )
    leg = await get_leg(pool, res["leg_id"])
    assert leg["trailer_number"] != "7634"
    assert leg["origin_location"] == "7634"


async def test_a_real_trailer_number_is_still_kept(pool):
    await seed_network(pool)
    res = await commit(
        pool,
        intent(case_type="CASE_1_ORIGIN_DEPARTURE", origin_location="7634",
               destination_location="200R", text_trailer="77208",
               load_status="EMPTY"),
    )
    assert (await get_leg(pool, res["leg_id"]))["trailer_number"] == "77208"
