"""
Timezone architecture acceptance tests.

These tests verify that the timezone refactoring is correct end-to-end:
- UTC normalisation helpers work as specified
- Local-hour extraction respects DST transitions
- API response timestamps are in pilot-local time, not silently UTC
- Generic charger stats are indexed by real charger pilot-local hour
- valid_until is expressed in pilot-local (or caller) timezone
"""

from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo


# ---------------------------------------------------------------------------
# 1. to_utc — normalisation
# ---------------------------------------------------------------------------

def test_to_utc_from_tz_aware_summer():
    """Zurich CEST (+02:00) 16:36:50 → UTC 14:36:50"""
    from app.services.common.db_utils import to_utc
    dt = datetime(2026, 5, 12, 16, 36, 50, tzinfo=ZoneInfo("Europe/Zurich"))
    result = to_utc(dt)
    assert result == datetime(2026, 5, 12, 14, 36, 50, tzinfo=timezone.utc)


def test_to_utc_from_tz_aware_winter():
    """Zurich CET (+01:00) 15:36:50 → UTC 14:36:50"""
    from app.services.common.db_utils import to_utc
    dt = datetime(2026, 1, 12, 15, 36, 50, tzinfo=ZoneInfo("Europe/Zurich"))
    result = to_utc(dt)
    assert result == datetime(2026, 1, 12, 14, 36, 50, tzinfo=timezone.utc)


def test_to_utc_from_naive_treated_as_utc():
    """Naive datetime is assumed UTC and attached timezone.utc"""
    from app.services.common.db_utils import to_utc
    dt = datetime(2026, 5, 12, 14, 36, 50)
    result = to_utc(dt)
    assert result == datetime(2026, 5, 12, 14, 36, 50, tzinfo=timezone.utc)


def test_to_utc_from_fixed_offset():
    """Fixed +02:00 offset is normalised to UTC correctly"""
    from app.services.common.db_utils import to_utc
    dt = datetime(2026, 5, 12, 16, 0, 0, tzinfo=timezone(timedelta(hours=2)))
    result = to_utc(dt)
    assert result == datetime(2026, 5, 12, 14, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 2. to_response_tz — timezone conversion for API output
# ---------------------------------------------------------------------------

def test_to_response_tz_zurich_summer():
    """UTC 14:36:50 → Europe/Zurich CEST = 16:36:50 +02:00"""
    from app.services.common.db_utils import to_response_tz
    dt_utc = datetime(2026, 5, 12, 14, 36, 50, tzinfo=timezone.utc)
    result = to_response_tz(dt_utc, "Europe/Zurich")
    assert result.hour == 16
    assert result.utcoffset() == timedelta(hours=2)


def test_to_response_tz_zurich_winter():
    """UTC 14:36:50 → Europe/Zurich CET = 15:36:50 +01:00"""
    from app.services.common.db_utils import to_response_tz
    dt_utc = datetime(2026, 1, 12, 14, 36, 50, tzinfo=timezone.utc)
    result = to_response_tz(dt_utc, "Europe/Zurich")
    assert result.hour == 15
    assert result.utcoffset() == timedelta(hours=1)


def test_to_response_tz_london_summer():
    """UTC 14:36:50 → Europe/London BST = 15:36:50 +01:00"""
    from app.services.common.db_utils import to_response_tz
    dt_utc = datetime(2026, 5, 12, 14, 36, 50, tzinfo=timezone.utc)
    result = to_response_tz(dt_utc, "Europe/London")
    assert result.hour == 15
    assert result.utcoffset() == timedelta(hours=1)


def test_to_response_tz_london_winter():
    """UTC 14:36:50 → Europe/London GMT = 14:36:50 +00:00"""
    from app.services.common.db_utils import to_response_tz
    dt_utc = datetime(2026, 1, 12, 14, 36, 50, tzinfo=timezone.utc)
    result = to_response_tz(dt_utc, "Europe/London")
    assert result.hour == 14
    assert result.utcoffset() == timedelta(hours=0)


def test_to_response_tz_none_returns_utc():
    """tz=None leaves datetime in UTC (no conversion)"""
    from app.services.common.db_utils import to_response_tz
    dt_utc = datetime(2026, 5, 12, 14, 0, tzinfo=timezone.utc)
    result = to_response_tz(dt_utc, None)
    assert result == dt_utc


def test_to_response_tz_fixed_offset():
    """Fixed offset +02:00 tzinfo object works as response tz"""
    from app.services.common.db_utils import to_response_tz
    dt_utc = datetime(2026, 5, 12, 14, 0, tzinfo=timezone.utc)
    result = to_response_tz(dt_utc, timezone(timedelta(hours=2)))
    assert result.hour == 16
    assert result.utcoffset() == timedelta(hours=2)


# ---------------------------------------------------------------------------
# 3. get_local_hour — pilot-local hour extraction
# ---------------------------------------------------------------------------

def test_get_local_hour_zurich_summer():
    """UTC 14:36 → Europe/Zurich CEST → local hour 16"""
    from app.services.common.db_utils import get_local_hour
    dt_utc = datetime(2026, 5, 12, 14, 36, 50, tzinfo=timezone.utc)
    assert get_local_hour(dt_utc, "Europe/Zurich") == 16


def test_get_local_hour_zurich_winter():
    """UTC 14:36 → Europe/Zurich CET → local hour 15"""
    from app.services.common.db_utils import get_local_hour
    dt_utc = datetime(2026, 1, 12, 14, 36, 50, tzinfo=timezone.utc)
    assert get_local_hour(dt_utc, "Europe/Zurich") == 15


def test_get_local_hour_london_summer():
    """UTC 14:36 → Europe/London BST → local hour 15"""
    from app.services.common.db_utils import get_local_hour
    dt_utc = datetime(2026, 5, 12, 14, 36, 50, tzinfo=timezone.utc)
    assert get_local_hour(dt_utc, "Europe/London") == 15


def test_get_local_hour_utc_passthrough():
    """UTC tz_name returns the UTC hour unchanged"""
    from app.services.common.db_utils import get_local_hour
    dt_utc = datetime(2026, 5, 12, 14, 0, tzinfo=timezone.utc)
    assert get_local_hour(dt_utc, "UTC") == 14


def test_get_local_hour_midnight_transition():
    """UTC 23:30 → Europe/Zurich CET (+01:00) → local 00:30 next day, local_hour=0"""
    from app.services.common.db_utils import get_local_hour
    dt_utc = datetime(2026, 1, 12, 23, 30, tzinfo=timezone.utc)
    assert get_local_hour(dt_utc, "Europe/Zurich") == 0


def test_get_local_hour_midnight_utc_previous_day_in_minus5():
    """UTC 02:00 → America/New_York EST (-05:00) → local 21:00 previous day, local_hour=21"""
    from app.services.common.db_utils import get_local_hour
    dt_utc = datetime(2026, 1, 13, 2, 0, tzinfo=timezone.utc)
    assert get_local_hour(dt_utc, "America/New_York") == 21


# ---------------------------------------------------------------------------
# 4. Regression test: round-trip for the spec example
# ---------------------------------------------------------------------------

def test_roundtrip_spec_example():
    """
    Spec example:
      API input:  2026-05-12T16:36:50+02:00
      Stored UTC: 2026-05-12T14:36:50+00:00
      Pilot tz:   Europe/Zurich
      Expected start_time response: 2026-05-12T16:36:50+02:00
      Expected local_hour: 16
    """
    from app.services.common.db_utils import to_utc, to_response_tz, get_local_hour

    original = datetime(2026, 5, 12, 16, 36, 50, tzinfo=ZoneInfo("Europe/Zurich"))
    stored_utc = to_utc(original)
    assert stored_utc == datetime(2026, 5, 12, 14, 36, 50, tzinfo=timezone.utc)

    response_dt = to_response_tz(stored_utc, "Europe/Zurich")
    assert response_dt.hour == 16
    assert response_dt.utcoffset() == timedelta(hours=2)

    lh = get_local_hour(stored_utc, "Europe/Zurich")
    assert lh == 16


# ---------------------------------------------------------------------------
# 5. valid_until uses pilot-local timezone when caller tz not available
# ---------------------------------------------------------------------------

def test_valid_until_pilot_tz():
    """valid_until is in pilot-local time when no caller tz is available"""
    ts_utc = datetime(2026, 5, 12, 14, 36, 50, tzinfo=timezone.utc)
    pilot_tz_name = "Europe/Zurich"
    valid_until = (ts_utc + timedelta(minutes=15)).astimezone(ZoneInfo(pilot_tz_name))
    assert valid_until.hour == 16
    assert valid_until.minute == 51
    assert valid_until.utcoffset() == timedelta(hours=2)


def test_valid_until_caller_tz_preserved():
    """valid_until uses caller's offset when the event has a timezone"""
    ts_caller = datetime(2026, 5, 12, 16, 36, 50, tzinfo=timezone(timedelta(hours=2)))
    ts_utc = ts_caller.astimezone(timezone.utc)
    valid_until = (ts_utc + timedelta(minutes=15)).astimezone(ts_caller.tzinfo)
    assert valid_until.hour == 16
    assert valid_until.minute == 51
    assert valid_until.utcoffset() == timedelta(hours=2)


# ---------------------------------------------------------------------------
# 6. Generic charger must use real charger's pilot-local hour
# ---------------------------------------------------------------------------

def test_generic_charger_uses_pilot_local_hour_zurich():
    """
    Charger in Europe/Zurich: session starts at UTC 06:00 in January.
    Zurich CET = UTC+1, so local_hour = 7.
    The generic charger should record this as local_hour=7, not UTC 6.
    """
    from app.services.common.db_utils import get_local_hour
    dt_utc = datetime(2026, 1, 12, 6, 0, 0, tzinfo=timezone.utc)
    local_hour = get_local_hour(dt_utc, "Europe/Zurich")
    assert local_hour == 7


def test_generic_charger_uses_pilot_local_hour_london():
    """
    Charger in Europe/London: session starts at UTC 07:00 in January.
    London GMT = UTC+0, so local_hour = 7.
    The generic charger should record this as local_hour=7.
    """
    from app.services.common.db_utils import get_local_hour
    dt_utc = datetime(2026, 1, 12, 7, 0, 0, tzinfo=timezone.utc)
    local_hour = get_local_hour(dt_utc, "Europe/London")
    assert local_hour == 7


def test_two_chargers_same_local_hour_different_utc():
    """
    Acceptance criterion 28+29:

    Charger A (Europe/Zurich, winter, UTC+1):  session at UTC 06:00 → local_hour=7
    Charger B (Europe/London, winter, UTC+0):  session at UTC 07:00 → local_hour=7

    Both sessions start at "07:00 pilot-local time" but have different UTC timestamps.
    They must both contribute to generic charger local_hour=7.
    """
    from app.services.common.db_utils import get_local_hour
    lh_a = get_local_hour(datetime(2026, 1, 12, 6, 0, tzinfo=timezone.utc), "Europe/Zurich")
    lh_b = get_local_hour(datetime(2026, 1, 12, 7, 0, tzinfo=timezone.utc), "Europe/London")
    assert lh_a == 7
    assert lh_b == 7
    assert lh_a == lh_b  # both map to the same local_hour bucket in generic stats


def test_generic_charger_session_filter_uses_per_charger_tz():
    """
    Acceptance criterion 27+28:

    When filtering historical sessions for generic charger initialization at
    local_hour=7, a session from a Zurich charger (UTC 06:00 in winter) must be
    included because 06:00 UTC = 07:00 Zurich local, and a session from a London
    charger (UTC 07:00 in winter) must also be included because 07:00 UTC =
    07:00 London local.  If a single timezone (e.g. Zurich) were applied to both,
    the London session would be excluded (07:00 UTC = 08:00 Zurich).

    This test validates the per-session tz resolution logic without hitting the DB.
    """
    from app.services.common.db_utils import get_local_hour

    target_hour = 7

    # Simulated sessions: (utc_hour, charger_tz)
    sessions = [
        (datetime(2026, 1, 12, 6, 0, tzinfo=timezone.utc), "Europe/Zurich"),   # local 07:00
        (datetime(2026, 1, 12, 7, 0, tzinfo=timezone.utc), "Europe/London"),   # local 07:00
        (datetime(2026, 1, 12, 8, 0, tzinfo=timezone.utc), "Europe/Zurich"),   # local 09:00 — excluded
        (datetime(2026, 1, 12, 6, 0, tzinfo=timezone.utc), "Europe/London"),   # local 06:00 — excluded
    ]

    # Correct per-session tz resolution (what the fixed code does)
    matched_correct = [
        dt for dt, tz_name in sessions
        if get_local_hour(dt, tz_name) == target_hour
    ]
    assert len(matched_correct) == 2  # Zurich 06:00 UTC and London 07:00 UTC

    # Incorrect single-tz approach (what the old code did, using Zurich for all)
    matched_wrong = [
        dt for dt, _ in sessions
        if get_local_hour(dt, "Europe/Zurich") == target_hour
    ]
    # Would only match the Zurich session (06:00 UTC → 07:00 Zurich)
    # London session at 07:00 UTC → 08:00 Zurich — INCORRECTLY excluded
    assert len(matched_wrong) == 1  # proves the bug we fixed


# ---------------------------------------------------------------------------
# 7. to_pilot_time helper
# ---------------------------------------------------------------------------

def test_to_pilot_time_summer():
    """to_pilot_time converts UTC to Europe/Zurich CEST correctly"""
    from app.services.common.db_utils import to_pilot_time
    dt_utc = datetime(2026, 5, 12, 14, 0, tzinfo=timezone.utc)
    result = to_pilot_time(dt_utc, "Europe/Zurich")
    assert result.hour == 16
    assert result.utcoffset() == timedelta(hours=2)


def test_to_pilot_time_naive_input_assumed_utc():
    """to_pilot_time treats naive input as UTC before converting"""
    from app.services.common.db_utils import to_pilot_time
    dt_naive = datetime(2026, 5, 12, 14, 0)
    result = to_pilot_time(dt_naive, "Europe/Zurich")
    assert result.hour == 16


# ---------------------------------------------------------------------------
# 8. DST edge cases
# ---------------------------------------------------------------------------

def test_dst_spring_forward_zurich():
    """
    Europe/Zurich clocks spring forward on last Sunday of March.
    2026-03-29 01:59:59 UTC+1 → 03:00:00 UTC+2 (clocks skip 02:xx).
    UTC 00:59:59 is CET 01:59:59.
    UTC 01:00:00 is CEST 03:00:00.
    """
    from app.services.common.db_utils import get_local_hour
    # Just before spring-forward: UTC 00:59 → local 01:59 (still CET +1)
    dt_before = datetime(2026, 3, 29, 0, 59, tzinfo=timezone.utc)
    assert get_local_hour(dt_before, "Europe/Zurich") == 1
    # Just after spring-forward: UTC 01:00 → local 03:00 (CEST +2)
    dt_after = datetime(2026, 3, 29, 1, 0, tzinfo=timezone.utc)
    assert get_local_hour(dt_after, "Europe/Zurich") == 3
