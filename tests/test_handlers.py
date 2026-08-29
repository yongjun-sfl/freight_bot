"""Timestamp derivation. No Telegram connection: messages are stand-ins.

These values become departure_time and arrival_time on records handed to the
client's accounting team, so they must track the driver rather than the server.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import handlers


def msg(date=None, forward_date=None, forward_origin_date=None):
    origin = SimpleNamespace(date=forward_origin_date) if forward_origin_date else None
    return SimpleNamespace(date=date, forward_date=forward_date, forward_origin=origin)


UTC_NOON = datetime(2026, 7, 15, 16, 30, 0, tzinfo=timezone.utc)   # summer -> EDT (-4)
UTC_WINTER = datetime(2026, 1, 15, 16, 30, 0, tzinfo=timezone.utc)  # winter -> EST (-5)


def test_uses_telegram_send_time_not_processing_time():
    assert handlers.message_timestamp(msg(date=UTC_NOON)) == "2026-07-15 12:30:00"


def test_converts_using_eastern_daylight_saving():
    assert handlers.message_timestamp(msg(date=UTC_NOON)) == "2026-07-15 12:30:00"    # EDT, -4
    assert handlers.message_timestamp(msg(date=UTC_WINTER)) == "2026-01-15 11:30:00"  # EST, -5


def test_naive_datetime_is_treated_as_utc():
    naive = UTC_NOON.replace(tzinfo=None)
    assert handlers.message_timestamp(msg(date=naive)) == "2026-07-15 12:30:00"


def test_forwarded_message_uses_the_original_send_time():
    """Relaying a driver's earlier report must not restamp it to now."""
    original = UTC_NOON
    forwarded_later = UTC_NOON + timedelta(hours=6)
    m = msg(date=forwarded_later, forward_origin_date=original)
    assert handlers.message_timestamp(m) == "2026-07-15 12:30:00"


def test_legacy_forward_date_is_honoured():
    """Bot API < 7.0 shape: forward_origin absent, forward_date present."""
    m = msg(date=UTC_NOON + timedelta(hours=6), forward_date=UTC_NOON)
    assert handlers.message_timestamp(m) == "2026-07-15 12:30:00"


def test_forward_origin_wins_over_legacy_forward_date():
    m = msg(date=UTC_NOON + timedelta(hours=6),
            forward_date=UTC_NOON + timedelta(hours=3),
            forward_origin_date=UTC_NOON)
    assert handlers.message_timestamp(m) == "2026-07-15 12:30:00"


def test_non_forwarded_message_ignores_forward_fields():
    assert handlers.message_timestamp(msg(date=UTC_NOON)) == "2026-07-15 12:30:00"


def test_missing_date_falls_back_without_raising():
    out = handlers.message_timestamp(msg())
    datetime.strptime(out, "%Y-%m-%d %H:%M:%S")   # parses, so a usable value


def test_album_takes_the_earliest_send_time():
    """The buffer flushes 1.2s later and images can arrive out of order."""
    stamps = [handlers.message_timestamp(msg(date=UTC_NOON + timedelta(seconds=s)))
              for s in (4, 0, 2)]
    assert min(stamps) == "2026-07-15 12:30:00"
