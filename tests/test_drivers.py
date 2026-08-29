"""The driver roster, and the name forms a schedule post has to resolve."""

import seed_network


def _roster():
    return seed_network.read_drivers_csv()


def test_roster_parses():
    rows = _roster()
    assert len(rows) == 14
    assert sum(1 for r in rows if r["active"]) == 13


def test_active_flag_is_case_folded():
    """The export mixes Y and y."""
    by_name = {r["name_eng"]: r for r in _roster()}
    assert by_name["HYUNGBAE KIM"]["active"] is True
    assert by_name["Billy Jung"]["active"] is False


def test_telegram_ids_are_absent_and_that_is_expected():
    """The id is Telegram's own, in no export, learned when someone messages."""
    assert all(r["user_id"] is None for r in _roster())


def test_home_repos_are_location_codes():
    repos = {r["home_repo"] for r in _roster()}
    assert repos == {"200F", "SDS"}


def test_drivers_without_a_korean_name_still_load():
    by_name = {r["name_eng"]: r for r in _roster()}
    assert by_name["RODERICK MCBRIDE"]["name_kor"] is None
    assert by_name["RODERICK MCBRIDE"]["short_name"] == "Roderick"


def test_every_schedule_name_form_is_resolvable():
    """Names as they appear in a real schedule post must map to a driver."""
    forms = {}
    for driver in _roster():
        for form in seed_network.name_forms(driver):
            forms[form] = driver["name_eng"]

    # taken verbatim from the dispatcher's 08/28 posting
    for written, expected in [
        ("김영표", "YOUNGPYO KIM"),
        ("이윤근", "YUNGEUN LEE"),
        ("김형배", "HYUNGBAE KIM"),
        ("김정근", "JUNGGEUN KIM"),
        ("JOSEPH KIM", "JOSEPH KIM"),
        ("한기수", "KISOO HAN"),
        ("JOHN SHIM", "JOHN SHIM"),
        ("매튜조", "MATTHEW CHO"),
        ("윤석환", "SOKHWAN YUN"),
        ("RODERICK", "RODERICK MCBRIDE"),
        ("DOMINIQUE", "DOMINIQUE WALLS"),
        ("공영택", "YOUNG TEAK KONG"),
    ]:
        assert forms.get(written.upper()) == expected, f"{written} did not resolve"


def test_a_misspelled_name_does_not_resolve():
    """The 08/28 post wrote 공형택 for 공영택. Exact matching drops that
    driver's whole day silently, which is why the schedule parser needs fuzzy
    matching and must report unresolved names rather than skipping them."""
    forms = set()
    for driver in _roster():
        forms |= seed_network.name_forms(driver)
    assert "공형택" not in forms
    assert "공영택" in forms
