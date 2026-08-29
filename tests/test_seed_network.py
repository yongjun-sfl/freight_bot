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
