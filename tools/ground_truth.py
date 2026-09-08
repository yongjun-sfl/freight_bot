"""Read the dispatcher's hand-entered log for one day.

The workbook has a sheet per date. Below the summary block, legs are laid out
in per-team sections: one row per leg, with Trip Seq marking the rounds.
"""

import re

COL = {
    "driver": 0, "trip_seq": 1, "origin": 2, "trailer": 3, "bol": 4,
    "transaction": 5, "load_type": 6, "rm_seq": 7, "planned_depart": 8,
    "depart": 9, "remark": 10, "destination": 11, "arrival": 16,
    "finished": 17, "dock": 18, "do_number": 19, "time_taken": 21,
}


def _text(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clock(value):
    """HH:MM from a time cell, or None."""
    text = _text(value)
    if not text:
        return None
    match = re.search(r"(\d{1,2}):(\d{2})", text)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour == 0 and minute == 0:
        return None            # the sheet uses 00:00:00 for blank
    # The September manual sheets were typed with a 12-hour clock but no AM/PM,
    # so an afternoon "01:29" / "02:34" is stored as 1/2 AM. Shuttle work never
    # starts between 1 and 6 AM; interpret those as PM.
    if 1 <= hour < 7:
        hour += 12
    return f"{hour:02d}:{minute:02d}"


def load_day(path, sheet):
    """Legs the dispatcher recorded for that sheet, in order."""
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb[sheet]

    legs = []
    driver = None
    for row in ws.iter_rows(min_row=20, values_only=True):
        cells = list(row) + [None] * (max(COL.values()) + 1 - len(row))

        name = _text(cells[COL["driver"]])
        if name and name.upper() not in ("DRIVER", "TOTAL"):
            driver = name.upper()

        origin = _text(cells[COL["origin"]])
        destination = _text(cells[COL["destination"]])
        depart = _clock(cells[COL["depart"]])
        if not (driver and origin and destination and depart):
            continue
        if origin.upper() in ("LOCATION", "ORIGIN"):
            continue

        legs.append({
            "driver": driver,
            "trip_seq": _text(cells[COL["trip_seq"]]),
            "origin": origin.upper(),
            "destination": destination.upper(),
            "trailer": _text(cells[COL["trailer"]]),
            "bol": _text(cells[COL["bol"]]),
            "transaction": _text(cells[COL["transaction"]]),
            "load_type": _text(cells[COL["load_type"]]),
            "rm_seq": _text(cells[COL["rm_seq"]]),
            "depart": depart,
            "arrival": _clock(cells[COL["arrival"]]),
            "finished": _clock(cells[COL["finished"]]),
            "dock": _text(cells[COL["dock"]]),
            "do_number": _text(cells[COL["do_number"]]),
        })
    return legs
