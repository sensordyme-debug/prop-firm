"""Tests for MNQ contract months, expiry and the quarterly roll.

Two failure modes are being guarded here, and they are unrelated to each other:
trading a thin contract after liquidity has moved, and reading a spliced
price step in backtest data as if it were a market move.
"""

from __future__ import annotations

from datetime import date

import pytest

import contracts as ct
from contracts import (
    MONTH_CODES,
    QUARTERLY_MONTHS,
    ContractMonth,
    contract_symbol,
    expiry_date,
    front_month,
    is_expiry_date,
    is_roll_date,
    next_roll_on_or_after,
    roll_date,
    rolls_between,
    third_friday,
)


# ===========================================================================
# Third Friday
# ===========================================================================


def test_third_friday_for_every_2026_quarter():
    """Checked by hand against a calendar."""
    assert third_friday(2026, 3) == date(2026, 3, 20)
    assert third_friday(2026, 6) == date(2026, 6, 19)
    assert third_friday(2026, 9) == date(2026, 9, 18)
    assert third_friday(2026, 12) == date(2026, 12, 18)


def test_third_friday_is_always_a_friday():
    for year in (2026, 2027, 2028):
        for month in range(1, 13):
            assert third_friday(year, month).weekday() == 4


def test_third_friday_when_the_first_of_the_month_is_itself_a_friday():
    """The edge case that off-by-one errors live in: 2026-05-01 is a Friday."""
    assert date(2026, 5, 1).weekday() == 4
    assert third_friday(2026, 5) == date(2026, 5, 15)


def test_third_friday_is_never_in_the_wrong_month():
    for year in (2026, 2027):
        for month in range(1, 13):
            assert third_friday(year, month).month == month


# ===========================================================================
# Roll date
# ===========================================================================


def test_roll_is_the_monday_before_the_third_friday():
    for month in QUARTERLY_MONTHS:
        roll = roll_date(2026, month)
        assert roll.weekday() == 0, "must be a Monday"
        assert third_friday(2026, month) - roll == (date(2026, 1, 5) - date(2026, 1, 1))


def test_roll_dates_for_2026():
    assert roll_date(2026, 3) == date(2026, 3, 16)
    assert roll_date(2026, 6) == date(2026, 6, 15)
    assert roll_date(2026, 9) == date(2026, 9, 14)
    assert roll_date(2026, 12) == date(2026, 12, 14)


def test_roll_is_not_the_widely_repeated_wrong_conventions():
    """Two convincing-sounding rules that give the wrong day.

    'Eight days before expiry' lands on a Thursday; 'the second Thursday'
    lands somewhere else again. Both are commonly repeated. Pinning the
    difference means nobody can 'correct' this back to a plausible mistake.
    """
    from datetime import timedelta

    for month in QUARTERLY_MONTHS:
        expiry = third_friday(2026, month)
        assert roll_date(2026, month) != expiry - timedelta(days=8)
        second_thursday = None
        first = date(2026, month, 1)
        thursdays = [
            first + timedelta(days=d)
            for d in range(31)
            if (first + timedelta(days=d)).month == month
            and (first + timedelta(days=d)).weekday() == 3
        ]
        second_thursday = thursdays[1]
        assert roll_date(2026, month) != second_thursday


# ===========================================================================
# Expiry, and the holiday interaction
# ===========================================================================


def test_expiry_is_normally_the_third_friday():
    assert expiry_date(2026, 9) == date(2026, 9, 18)
    assert expiry_date(2026, 12) == date(2026, 12, 18)


def test_expiry_does_not_move_for_a_day_we_merely_abstain_from():
    """June 19 2026 is both the third Friday AND Juneteenth.

    We do not trade Juneteenth, but the MARKET is open for a shortened
    session, so settlement still happens that day. Using our own
    'will we trade it' test here would have pushed expiry a day early.
    """
    import market_calendar as mc

    juneteenth = date(2026, 6, 19)
    assert mc.is_open(juneteenth) is False, "we abstain"
    assert mc.market_is_open(juneteenth) is True, "the exchange does not"
    assert expiry_date(2026, 6) == juneteenth


def test_expiry_steps_back_when_the_market_is_genuinely_shut(monkeypatch):
    """The mechanism, not a date.

    No 2026 quarterly third Friday happens to be a full closure, so within
    this calendar year 'market open' and 'is a weekday' agree and a date-based
    test cannot tell them apart. They do NOT agree in general: Good Friday
    ranges from 20 March to 23 April and the third Friday of March ranges 15-21
    March, so the two can collide (Good Friday WAS the third Friday in April
    2025). In such a year settlement must step back to the Thursday.

    So the calendar is stubbed to shut that one day, and expiry must move.
    """
    shut = third_friday(2026, 9)  # 2026-09-18
    monkeypatch.setattr(ct, "market_is_open", lambda d: d != shut)

    assert expiry_date(2026, 9) == date(2026, 9, 17), "steps back to the Thursday"
    assert expiry_date(2026, 9).weekday() == 3


def test_expiry_keeps_stepping_back_over_consecutive_closures(monkeypatch):
    friday = third_friday(2026, 9)
    thursday = date(2026, 9, 17)
    monkeypatch.setattr(ct, "market_is_open", lambda d: d not in (friday, thursday))
    assert expiry_date(2026, 9) == date(2026, 9, 16)


def test_expiry_consults_the_exchange_not_our_own_abstention(monkeypatch):
    """A weekday check would be indistinguishable here; it must not be one."""
    calls: list[date] = []

    def spy(d: date) -> bool:
        calls.append(d)
        return True

    monkeypatch.setattr(ct, "market_is_open", spy)
    expiry_date(2026, 9)
    assert calls, "expiry must ask the calendar, not just look at the weekday"


def test_roll_does_not_move_when_expiry_does():
    """Computed from the unadjusted third Friday on purpose.

    A holiday shifting settlement should not silently shift the day liquidity
    migrates; they are different events with different causes.
    """
    assert roll_date(2026, 6) == date(2026, 6, 15)
    assert roll_date(2026, 6).weekday() == 0


# ===========================================================================
# Front month
# ===========================================================================


def test_front_month_before_the_roll_is_the_near_quarter():
    assert front_month(date(2026, 9, 11)) == ContractMonth(2026, 9)
    assert front_month(date(2026, 9, 13)) == ContractMonth(2026, 9)


def test_front_month_flips_on_the_roll_date_itself():
    """Liquidity moves before expiry; following it late means a thin book."""
    assert front_month(date(2026, 9, 13)) == ContractMonth(2026, 9)
    assert front_month(date(2026, 9, 14)) == ContractMonth(2026, 12)


def test_front_month_is_already_the_next_quarter_during_expiry_week():
    """The near contract has not expired, but it is no longer the lead."""
    assert front_month(date(2026, 9, 18)) == ContractMonth(2026, 12)


def test_front_month_rolls_into_the_next_year_in_december():
    assert front_month(date(2026, 12, 13)) == ContractMonth(2026, 12)
    assert front_month(date(2026, 12, 14)) == ContractMonth(2027, 3)
    assert front_month(date(2026, 12, 31)) == ContractMonth(2027, 3)


def test_front_month_is_always_a_quarterly_month():
    from datetime import timedelta

    day = date(2026, 1, 1)
    while day < date(2027, 6, 1):
        assert front_month(day).month in QUARTERLY_MONTHS
        day += timedelta(days=1)


def test_front_month_never_goes_backwards_through_the_year():
    from datetime import timedelta

    day = date(2026, 1, 1)
    previous = front_month(day)
    while day < date(2027, 6, 1):
        current = front_month(day)
        assert current >= previous, f"front month regressed on {day}"
        previous = current
        day += timedelta(days=1)


# ===========================================================================
# Symbols
# ===========================================================================


def test_month_codes_are_the_cme_quarterly_set():
    assert MONTH_CODES == {3: "H", 6: "M", 9: "U", 12: "Z"}


def test_contract_symbols_for_2026():
    assert contract_symbol(ContractMonth(2026, 3)) == "MNQH26"
    assert contract_symbol(ContractMonth(2026, 6)) == "MNQM26"
    assert contract_symbol(ContractMonth(2026, 9)) == "MNQU26"
    assert contract_symbol(ContractMonth(2026, 12)) == "MNQZ26"


def test_symbol_root_is_configurable():
    assert ContractMonth(2027, 3).symbol(root="NQ") == "NQH27"


def test_a_non_quarterly_month_is_rejected():
    with pytest.raises(ValueError, match="not a quarterly"):
        ContractMonth(2026, 7)


def test_next_quarter_wraps_the_year():
    assert ContractMonth(2026, 12).next_quarter() == ContractMonth(2027, 3)
    assert ContractMonth(2026, 3).next_quarter() == ContractMonth(2026, 6)


# ===========================================================================
# Predicates and ranges
# ===========================================================================


def test_expiry_and_roll_predicates():
    assert is_expiry_date(date(2026, 9, 18)) is True
    assert is_expiry_date(date(2026, 9, 17)) is False
    assert is_roll_date(date(2026, 9, 14)) is True
    assert is_roll_date(date(2026, 9, 15)) is False


def test_next_roll_on_or_after_includes_the_day_itself():
    assert next_roll_on_or_after(date(2026, 9, 14)) == date(2026, 9, 14)
    assert next_roll_on_or_after(date(2026, 9, 15)) == date(2026, 12, 14)


def test_rolls_between_finds_every_quarter_in_a_year():
    found = rolls_between(date(2026, 1, 1), date(2026, 12, 31))
    assert found == [
        date(2026, 3, 16), date(2026, 6, 15),
        date(2026, 9, 14), date(2026, 12, 14),
    ]


def test_rolls_between_is_empty_for_a_quiet_window():
    assert rolls_between(date(2026, 9, 15), date(2026, 12, 13)) == []


def test_rolls_between_is_inclusive_at_both_ends():
    assert rolls_between(date(2026, 9, 14), date(2026, 9, 14)) == [date(2026, 9, 14)]


def test_the_convention_is_flagged_as_not_verified_at_source():
    """CME's own roll-dates page was unreachable; two sources agreed instead."""
    assert ct.ROLL_CONVENTION_CME_VERIFIED is False
