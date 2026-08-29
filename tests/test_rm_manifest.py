"""RM consignments, for the inbound departments at E2F and E2R.

They need what is on the trailer -- material, quantity, and the batch number
their own manager issued -- not merely that a trailer moved.
"""

import pytest

from conftest import commit, get_leg, intent, seed_network

BOL = b"\xff\xd8\xff\xe0rm-bol"

# as read from a real PACTRA RM pick-up instruction
MATERIALS = [{
    "material_code": "11800335",
    "description": "GLASS",
    "qty": "10 PLT",
    "weight": None,
    "cont_no": None,
    "batch_no": "0001836335",
    "remark": None,
}]


async def _rm_rows(pool, sql, args=()):
    import aiomysql
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(sql, args)
            return list(await cur.fetchall())


async def _ship_rm(pool, **overrides):
    payload = dict(
        case_type="CASE_1_ORIGIN_DEPARTURE", origin_location="200R",
        destination_location="E2F", text_trailer="77168",
        bol_number="926761", document_type="RM", rm_seq="7",
        dock_number="47", primary_image_blob=BOL, load_status="LOADED",
        materials=MATERIALS,
    )
    payload.update(overrides)
    return await commit(pool, intent(**payload))


async def test_rm_load_is_recorded_with_its_line_items(pool):
    await seed_network(pool)
    res = await _ship_rm(pool)

    loads = await _rm_rows(pool, "SELECT * FROM rm_loads;")
    assert len(loads) == 1
    load = loads[0]
    assert load["reservation_no"] == "926761"
    assert load["rm_seq"] == 7
    assert load["delivery_date"] is not None
    assert load["trailer_number"] == "77168"
    assert load["dock_number"] == "47"
    assert load["destination_location"] == "E2F"
    assert load["pod"] == "EAGLE 2 FRONT", "POD reads in the client's words"
    assert load["driver_name"]
    assert load["leg_id"] == res["leg_id"]
    assert load["departure_time"] is not None
    assert load["arrival_time"] is None

    items = await _rm_rows(pool, "SELECT * FROM rm_load_items;")
    assert len(items) == 1
    assert items[0]["material_code"] == "11800335"
    assert items[0]["description"] == "GLASS"
    assert items[0]["qty"] == "10 PLT"
    assert items[0]["batch_no"] == "0001836335", "the receiving manager's reference"


async def test_arrival_times_the_run(pool):
    await seed_network(pool)
    await _ship_rm(pool)
    await commit(pool, intent(case_type="CASE_2_DESTINATION_ARRIVAL",
                              destination_location="E2F"))

    load = (await _rm_rows(pool, "SELECT * FROM rm_loads;"))[0]
    assert load["arrival_time"] is not None
    assert load["transit_minutes"] is not None


async def test_eta_is_departure_plus_the_weighted_drive_time(pool):
    """The summary's ETA column: 8:35 depart + 35 min weighted = 9:10."""
    await seed_network(pool)
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO location_distances (origin_code, destination_code, "
                "miles, drive_minutes, weighted_minutes) VALUES "
                "('200R','E2F',21,25,35) ON DUPLICATE KEY UPDATE miles=miles;")
    await _ship_rm(pool)
    load = (await _rm_rows(pool, "SELECT * FROM rm_loads;"))[0]
    assert load["eta"] is not None
    gap = (load["eta"] - load["departure_time"]).total_seconds() / 60
    assert gap == 35, f"expected the weighted 35 min allowance, got {gap}"


async def test_one_trip_may_carry_several_reservations(pool):
    """Row 3 of the summary reads '926576, 926577' as a single delivery."""
    await seed_network(pool)
    await _ship_rm(pool, bol_number="926576, 926577", rm_seq="3")
    loads = await _rm_rows(pool, "SELECT * FROM rm_loads;")
    assert len(loads) == 1
    assert loads[0]["reservation_no"] == "926576, 926577"
    assert loads[0]["rm_seq"] == 3


async def test_paperwork_without_a_sequence_is_not_filed(pool):
    """date + seq is the key. Filing under a guessed sequence would collide
    with a real load, so it is skipped and logged instead."""
    await seed_network(pool)
    await _ship_rm(pool, rm_seq=None)
    assert await _rm_rows(pool, "SELECT * FROM rm_loads;") == []


async def test_resending_the_paperwork_does_not_duplicate(pool):
    """Drivers resend photos routinely; date + seq is the key."""
    await seed_network(pool)
    await _ship_rm(pool)
    await commit(pool, intent(case_type="CASE_2_DESTINATION_ARRIVAL"))
    await commit(pool, intent(
        case_type="CASE_AUTO_RESOLVE", bol_number="926761", document_type="RM",
        primary_image_blob=BOL, materials=MATERIALS, rm_seq="7"))

    assert len(await _rm_rows(pool, "SELECT * FROM rm_loads;")) == 1
    assert len(await _rm_rows(pool, "SELECT * FROM rm_load_items;")) == 1


async def test_corrected_paperwork_replaces_the_lines(pool):
    """A corrected photo must not leave superseded lines beside the new ones."""
    await seed_network(pool)
    await _ship_rm(pool)
    await _ship_rm(pool, materials=[
        {"material_code": "99999999", "description": "FILM", "qty": "4 PLT",
         "weight": None, "cont_no": None, "batch_no": "0009999999", "remark": None},
    ])
    items = await _rm_rows(pool, "SELECT * FROM rm_load_items;")
    assert len(items) == 1
    assert items[0]["material_code"] == "99999999"


async def test_fg_loads_are_not_recorded_as_rm(pool):
    await seed_network(pool)
    await commit(pool, intent(
        case_type="CASE_1_ORIGIN_DEPARTURE", origin_location="SDS",
        destination_location="200F", text_trailer="77344", bol_number="FG-1",
        document_type="FG", primary_image_blob=BOL, load_status="LOADED"))
    assert await _rm_rows(pool, "SELECT * FROM rm_loads;") == []


async def test_rm_without_line_items_still_records_the_load(pool):
    """An unreadable materials table must not lose the consignment itself."""
    await seed_network(pool)
    await _ship_rm(pool, materials=[])
    loads = await _rm_rows(pool, "SELECT * FROM rm_loads;")
    assert len(loads) == 1 and loads[0]["reservation_no"] == "926761"
    assert await _rm_rows(pool, "SELECT * FROM rm_load_items;") == []
