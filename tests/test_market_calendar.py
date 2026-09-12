"""Tests for the CME equity-index calendar.

The table itself is unverified data (see the module's provenance warning), so
these tests check the LOOKUP BEHAVIOUR and the fail-closed properties, plus a
few representative rows. They cannot certify that a given holiday is real --
only that a date in the table is treated correctly.
"""

from __future__ import annotations

from datetime import date

import market_calendar as mc


def test_a_regular_weekday_is_open():
    day = mc.classify(date(2026, 9, 16))  # Wednesday
    assert day.status is mc.DayStatus.OPEN
    assert day.tradable is True


def test_saturday_and_sunday_are_closed():
    for d, name in ((date(2026, 9, 19), "Saturday"), (date(2026, 9, 20), "Sunday")):
        day = mc.classify(d)
        assert day.status is mc.DayStatus.CLOSED
        assert day.tradable is False
        assert name in day.label


def test_a_full_holiday_is_closed():
    day = mc.classify(date(2026, 11, 26))
    assert day.status is mc.DayStatus.CLOSED
    assert day.tradable is False
    assert "Thanksgiving" in day.label


def test_a_holiday_half_day_is_not_tradable():
    """The market is open; we stand aside anyway (DECISIONS.md).

    The close time is disputed between sources and Topstep sets its own by
    announcement, so there is no number worth computing a deadline from.
    """
    day = mc.classify(date(2026, 11, 27))
    assert day.status is mc.DayStatus.HOLIDAY_HALF_DAY
    assert day.tradable is False
    assert "do not trade" in day.label


def test_no_close_time_is_exposed_for_computation():
    """The times are labels only, so nothing can quietly compute with them."""
    assert not hasattr(mc.classify(date(2026, 11, 27)), "close_et")
    assert not hasattr(mc, "early_close_et")


def test_dates_outside_coverage_fail_closed():
    """Unmaintained calendar must not be read as 'everything is open'."""
    for d in (date(2025, 6, 1), date(2028, 6, 1)):
        day = mc.classify(d)
        assert day.status is mc.DayStatus.OUT_OF_COVERAGE
        assert day.tradable is False
        assert "coverage" in day.label


def test_coverage_bounds_are_inclusive():
    low, high = mc.COVERAGE
    assert mc.classify(low).status is not mc.DayStatus.OUT_OF_COVERAGE
    assert mc.classify(high).status is not mc.DayStatus.OUT_OF_COVERAGE


def test_holidays_and_half_days_do_not_overlap():
    """A date cannot be both shut and shortened; classify would pick one."""
    assert not (set(mc.HOLIDAYS) & set(mc.HALF_DAYS))


def test_no_holiday_date_of_any_kind_is_tradable():
    """The single rule that replaces all early-close arithmetic."""
    for d in (*mc.HOLIDAYS, *mc.HALF_DAYS):
        assert mc.is_open(d) is False, f"{d} must not be tradable"


def test_no_calendar_entry_falls_on_a_weekend():
    """A weekend entry is a transcription error: weekends are already closed."""
    for d in (*mc.HOLIDAYS, *mc.HALF_DAYS):
        assert d.weekday() < 5, f"{d} is a {d:%A}; check the source calendar"


def test_every_calendar_entry_is_inside_coverage():
    low, high = mc.COVERAGE
    for d in (*mc.HOLIDAYS, *mc.HALF_DAYS):
        assert low <= d <= high, f"{d} is outside COVERAGE and will never be read"


def test_previous_trading_date_skips_a_weekend():
    assert mc.previous_trading_date(date(2026, 9, 21)) == date(2026, 9, 18)


def test_previous_trading_date_skips_a_holiday():
    """Thursday is Thanksgiving, so Friday's predecessor is Wednesday."""
    assert mc.previous_trading_date(date(2026, 11, 27)) == date(2026, 11, 25)


def test_previous_trading_date_on_a_normal_run_of_days():
    assert mc.previous_trading_date(date(2026, 9, 17)) == date(2026, 9, 16)


def test_previous_trading_date_returns_none_when_it_cannot_tell():
    """Outside coverage everything reads as closed, so there is no answer."""
    assert mc.previous_trading_date(date(2028, 6, 1)) is None


def test_calendar_is_flagged_unverified_until_someone_checks_it():
    """Guards the provenance warning.

    When the table has been checked against CME's published calendar, flip
    CALENDAR_VERIFIED to True and update this test. Failing here on purpose is
    better than a silent assumption that the dates were ever confirmed.
    """
    assert mc.CALENDAR_VERIFIED is False, (
        "If the calendar has now been verified against CME, update this test "
        "deliberately rather than letting it pass by accident."
    )
    assert "cmegroup.com" in mc.CALENDAR_SOURCE
