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


async def test_same_facility_departure_is_surfaced_not_dropped(pool):
    """Within-facility work is billable now, so a same-facility CASE 1 is a
    misparse to be reported rather than silently discarded."""
    res = await commit(
        pool,
        intent(case_type="CASE_1_ORIGIN_DEPARTURE", raw_text="200 to 200",
               origin_location="200", destination_location="200"),
    )
    assert res["is_clean"] is False
    assert "Same-Facility Departure" in res["card_text"]
    assert await all_legs(pool) == []


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

async def test_midtrip_dock_move_folds_into_the_current_leg(pool):
    """Driver arrived from SDS, unloaded at an inbound dock, now moves to an
    RM outbound dock. That is part of the trip, not a leg of its own."""
    leg = await insert_leg(
        pool, origin_location="SDS", destination_location="200",
        trailer_number="77344", dock_number="13",
        leg_status="COMPLETED", arrival_action="DROP",   # a DROP closes the leg
    )

    res = await commit(
        pool,
        intent(case_type="CASE_3_INTRA_FACILITY_MOVE", raw_text="empty move #13 to #47",
               origin_dock="13", destination_dock="47", load_status="EMPTY"),
    )
    assert res["is_clean"] is True
    assert res["leg_id"] == leg
    assert len(await all_legs(pool)) == 1, "must not create a second leg"

    row = await get_leg(pool, leg)
    assert row["origin_dock"] == "13"
    assert row["destination_dock"] == "47"
    assert row["is_positioning_leg"] == 0


async def test_midtrip_move_never_overwrites_the_trailer(pool):
    """They may have dropped what they arrived with and hooked another; this
    leg must keep what it actually carried."""
    leg = await insert_leg(
        pool, origin_location="SDS", destination_location="200",
        trailer_number="77344", leg_status="COMPLETED",
    )
    await commit(
        pool,
        intent(case_type="CASE_3_INTRA_FACILITY_MOVE", origin_dock="13",
               destination_dock="47", ocr_trailer="99999"),
    )
    assert (await get_leg(pool, leg))["trailer_number"] == "77344"


async def test_cleanup_move_is_its_own_billable_leg(pool):
    """Dispatcher-assigned cleanup is the work being billed, so it stands alone
    even when the driver is mid-trip."""
    parent = await insert_leg(
        pool, origin_location="SDS", destination_location="200",
        trailer_number="77344", leg_status="COMPLETED",
    )

    res = await commit(
        pool,
        intent(case_type="CASE_3_INTRA_FACILITY_MOVE", is_cleanup=True,
               raw_text="cleanup empty move #13 to #47",
               origin_dock="13", destination_dock="47",
               ocr_trailer="53012", load_status="EMPTY"),
    )
    assert res["is_clean"] is True
    assert res["leg_id"] != parent
    assert len(await all_legs(pool)) == 2

    leg = await get_leg(pool, res["leg_id"])
    assert leg["is_positioning_leg"] == 1
    assert leg["origin_location"] == "200" and leg["destination_location"] == "200"
    assert leg["origin_dock"] == "13" and leg["destination_dock"] == "47"
    assert leg["trailer_number"] == "53012"
    assert leg["load_status"] == "EMPTY"
    assert leg["arrival_action"] == "DOCK_MOVE"
    assert leg["leg_status"] == "COMPLETED"
    assert leg["departure_time"] is not None and leg["arrival_time"] is not None

    # the trip leg it happened during must be untouched
    assert (await get_leg(pool, parent))["origin_dock"] is None


async def test_cleanup_drop_to_yard(pool):
    await insert_leg(pool, destination_location="200", leg_status="COMPLETED")
    res = await commit(
        pool,
        intent(case_type="CASE_3_INTRA_FACILITY_MOVE", is_cleanup=True,
               raw_text="cleanup empty dropped yard", destination_dock="YARD",
               ocr_trailer="53012", load_status="EMPTY"),
    )
    leg = await get_leg(pool, res["leg_id"])
    assert leg["destination_dock"] == "YARD"
    assert leg["arrival_action"] == "YARD_DROP"


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
        intent(case_type="CASE_3_INTRA_FACILITY_MOVE", is_cleanup=True,
               origin_dock="13", destination_dock="47", origin_location="200"),
    )
    res = await commit(pool, intent(case_type="CASE_2_DESTINATION_ARRIVAL"))
    assert res["leg_id"] is None, "arrival must not attach to a positioning leg"
    assert (await get_leg(pool, cleanup["leg_id"]))["arrival_action"] == "DOCK_MOVE"


async def test_departure_after_dock_work_completes_the_trip_leg(pool):
    """The full 200 flow: arrive from SDS, reposition inbound -> RM outbound,
    then 'live loading finished load 200 to E2F' closes it and opens the next."""
    leg_a = await insert_leg(
        pool, origin_location="SDS", destination_location="200",
        trailer_number="77344", bol_number="B-IN", bol_image=IMG,
        leg_status="ARRIVED", dock_number="13",
    )
    await commit(
        pool,
        intent(case_type="CASE_3_INTRA_FACILITY_MOVE",
               origin_dock="13", destination_dock="47", load_status="EMPTY"),
    )
    assert (await get_leg(pool, leg_a))["destination_dock"] == "47"

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
