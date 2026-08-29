"""Parsing the dispatcher's distance export.

The file is a real export and has real quirks: a misspelled header, three
duplicated rows, and one pair with no reverse. The loader must absorb all of
that rather than require a clean re-export.
"""

import seed_network


def test_export_parses():
    codes, pairs = seed_network.read_distance_csv()
    assert codes, "distance export should be readable"
    assert len(codes) == 12
    assert set(codes) == {"100", "1380", "200F", "200R", "210", "300",
                          "3551", "7634", "E1", "E2F", "E2R", "SDS"}


def test_misspelled_destination_header_is_tolerated():
    """The export header reads 'Desitnation'."""
    _, pairs = seed_network.read_distance_csv()
    assert ("200F", "E2F") in pairs


def test_duplicate_rows_collapse():
    """SDS->200F, 210->SDS and SDS->210 each appear twice in the file."""
    _, pairs = seed_network.read_distance_csv()
    assert pairs[("SDS", "200F")] == (23.0, 27, 37.8)


def test_missing_reverse_is_mirrored():
    """E2F->SDS is in the file; SDS->E2F is not. Every other pair is symmetric."""
    _, pairs = seed_network.read_distance_csv()
    assert pairs[("E2F", "SDS")] == pairs[("SDS", "E2F")] == (3.0, 6, 8.4)


def test_every_pair_has_both_directions():
    _, pairs = seed_network.read_distance_csv()
    missing = [(a, b) for (a, b) in pairs if (b, a) not in pairs]
    assert missing == []


def test_spot_destinations_are_nearer_than_route_stops():
    """Why round grouping cannot be distance-based: the two spot-delivery
    facilities are closer to 200F than every routine shuttle destination."""
    _, pairs = seed_network.read_distance_csv()
    spot = max(pairs[("200F", code)][0] for code in ("100", "1380"))
    routine = min(pairs[("200F", code)][0]
                  for code in ("E2F", "E2R", "E1", "SDS", "3551", "210"))
    assert spot < routine, (
        f"furthest spot stop {spot} mi should be nearer than "
        f"closest routine stop {routine} mi"
    )


def test_missing_file_degrades_quietly():
    codes, pairs = seed_network.read_distance_csv("/nonexistent/nope.csv")
    assert codes == [] and pairs == {}


# --------------------------------------------------------------------------
# facility list
# --------------------------------------------------------------------------

def _locations():
    return {row["code"]: row for row in seed_network.read_locations_csv()}


def test_facility_list_parses():
    rows = _locations()
    assert rows, "locations export should be readable"
    active = [r for r in rows.values() if r["active"]]
    assert len(active) == 12


def test_official_names_are_the_client_vocabulary():
    """Reports for Qcell should read EAGLE 2 FRONT, not E2F."""
    rows = _locations()
    assert rows["E2F"]["official_name"] == "EAGLE 2 FRONT"
    assert rows["SDS"]["official_name"] == "SDS WH"
    assert rows["200F"]["official_name"] == "ADV SDS FRONT"


def test_front_and_rear_share_a_site():
    """What lets a round opened at 200R close on return to 200F."""
    rows = _locations()
    assert rows["200F"]["site"] == rows["200R"]["site"] == "200"
    assert rows["E2F"]["site"] == rows["E2R"]["site"] == "E2"


def test_eagle_1_has_no_front_or_rear():
    """The source export splits E1, but the dispatcher confirmed it does not.
    Both legacy rows are retained inactive rather than deleted."""
    rows = _locations()
    assert rows["E1"]["active"] is True
    assert rows["E1F"]["active"] is False
    assert rows["E1R"]["active"] is False


def test_unused_facilities_are_inactive():
    rows = _locations()
    assert rows["1001"]["active"] is False
    assert rows["1343"]["active"] is False


def test_bare_200_resolves_to_the_front():
    rows = _locations()
    assert "200" in (rows["200F"]["aliases"] or "")


def test_every_active_facility_has_a_site():
    for code, row in _locations().items():
        if row["active"]:
            assert row["site"], f"{code} has no site"


def test_every_route_facility_is_active():
    """A route stop that is inactive would drop out of the parser's code list."""
    import routes
    rows = _locations()
    stops = {c for _, _, anchor, members in routes.DEFAULT_ROUTES
             for c in (anchor, *members)}
    for code in stops:
        assert code in rows, f"route uses {code}, absent from the facility list"
        assert rows[code]["active"], f"route uses {code}, which is inactive"
