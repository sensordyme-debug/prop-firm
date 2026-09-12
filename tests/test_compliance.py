"""Tests for Combine and payout compliance.

These encode rules verified from Topstep's own help centre (FIRM_RULES.md).
They matter because they are the rules you can break by WINNING, which no
risk check will ever catch for you.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from compliance import (
    COMBINE_CONSISTENCY_PCT,
    COMBINE_PROFIT_TARGET,
    CONSISTENCY_PATH_TRADING_DAYS,
    MINIMUM_PAYOUT_REQUEST,
    PAYOUT_CAP_CONSISTENCY,
    PAYOUT_CAP_STANDARD,
    PAYOUT_CONSISTENCY_PCT,
    STANDARD_PATH_WINNING_DAYS,
    WINNING_DAY_MINIMUM,
    SessionResult,
    combine_status,
    consistency_ratio,
    max_safe_daily_profit,
    payout_status,
    profit_needed_for_consistency,
    report,
    validate_daily_target,
)

DAY0 = date(2026, 9, 14)


def sessions(*pnls: float, trades: int = 1) -> list[SessionResult]:
    return [
        SessionResult(trading_date=DAY0 + timedelta(days=i), net_pnl=p, trades=trades)
        for i, p in enumerate(pnls)
    ]


# ===========================================================================
# The verified constants
# ===========================================================================


def test_constants_match_the_verified_firm_rules():
    assert COMBINE_PROFIT_TARGET == 3_000.0
    assert COMBINE_CONSISTENCY_PCT == 0.55
    assert PAYOUT_CONSISTENCY_PCT == 0.40
    assert WINNING_DAY_MINIMUM == 150.0
    assert STANDARD_PATH_WINNING_DAYS == 5
    assert CONSISTENCY_PATH_TRADING_DAYS == 3
    assert PAYOUT_CAP_STANDARD == 4_000.0      # 50K with RTA on
    assert PAYOUT_CAP_CONSISTENCY == 6_000.0   # 50K with RTA on
    assert MINIMUM_PAYOUT_REQUEST == 125.0


# ===========================================================================
# Consistency ratio
# ===========================================================================


def test_ratio_is_best_day_over_total():
    assert consistency_ratio(sessions(400.0, 300.0, 300.0)) == pytest.approx(0.4)


def test_ratio_is_undefined_without_profit():
    """Zero would read as 'perfectly consistent', which is the opposite."""
    assert consistency_ratio(sessions(-100.0, -200.0)) is None
    assert consistency_ratio(sessions(300.0, -300.0)) is None
    assert consistency_ratio([]) is None


def test_losing_days_make_the_ratio_worse_not_better():
    """A loss shrinks the total, so the best day looms larger."""
    clean = consistency_ratio(sessions(400.0, 300.0, 300.0))
    with_loss = consistency_ratio(sessions(400.0, 300.0, 300.0, -200.0))
    assert with_loss > clean


def test_a_single_day_is_one_hundred_percent_concentrated():
    assert consistency_ratio(sessions(500.0)) == pytest.approx(1.0)


# ===========================================================================
# The actionable number: 2.5x the best day
# ===========================================================================


def test_profit_needed_is_two_and_a_half_times_the_best_day():
    """40% limit means total must reach best/0.4 = 2.5 x best."""
    needed = profit_needed_for_consistency(sessions(500.0))
    assert needed == pytest.approx(1_250.0 - 500.0)  # total must reach 1,250


def test_no_profit_needed_when_already_compliant():
    assert profit_needed_for_consistency(sessions(400.0, 300.0, 300.0)) == 0.0


def test_an_outsized_day_raises_the_bar_for_everything_after():
    """The argument for a daily cap, as arithmetic.

    A $1,500 day needs $3,750 of total profit before a dollar can be
    withdrawn. A $500 day needs $1,250. The big day did not just fail to
    help -- it moved the finish line.
    """
    modest = profit_needed_for_consistency(sessions(500.0))
    outsized = profit_needed_for_consistency(sessions(1_500.0))
    assert modest == pytest.approx(750.0)
    assert outsized == pytest.approx(2_250.0)
    assert outsized > modest * 2


def test_profit_needed_shrinks_as_other_days_accumulate():
    assert profit_needed_for_consistency(sessions(500.0)) == pytest.approx(750.0)
    assert profit_needed_for_consistency(sessions(500.0, 300.0)) == pytest.approx(450.0)
    assert profit_needed_for_consistency(
        sessions(500.0, 300.0, 450.0)
    ) == pytest.approx(0.0)


def test_empty_history_needs_nothing():
    assert profit_needed_for_consistency([]) == 0.0


# ===========================================================================
# The daily-target guard
# ===========================================================================


def test_max_safe_daily_profit_is_55_percent_of_the_target():
    assert max_safe_daily_profit() == pytest.approx(1_650.0)


def test_the_default_daily_target_is_comfortably_safe():
    validate_daily_target(500.0)  # must not raise


def test_a_daily_target_above_the_consistency_ceiling_is_rejected():
    """You would fail the Combine by winning, which nobody expects."""
    with pytest.raises(ValueError, match="fail by winning"):
        validate_daily_target(1_700.0)


def test_the_ceiling_itself_is_allowed():
    validate_daily_target(1_650.0)


def test_one_cent_over_the_ceiling_is_rejected():
    with pytest.raises(ValueError):
        validate_daily_target(1_650.01)


# ===========================================================================
# Combine status
# ===========================================================================


def test_combine_not_passed_until_the_target_is_met():
    status = combine_status(sessions(500.0, 500.0, 500.0))
    assert status.total_profit == 1_500.0
    assert status.target_met is False
    assert status.passed is False
    assert status.remaining_to_target == pytest.approx(1_500.0)


def test_combine_passes_with_target_met_and_consistency_intact():
    status = combine_status(sessions(*[500.0] * 6))
    assert status.total_profit == 3_000.0
    assert status.target_met is True
    assert status.consistency_ok is True
    assert status.passed is True


def test_combine_target_met_but_consistency_broken_does_not_pass():
    """$1,700 in one day exceeds 55% of the $3,000 target."""
    status = combine_status(sessions(1_700.0, 700.0, 700.0))
    assert status.total_profit == 3_100.0
    assert status.target_met is True
    assert status.consistency_ok is False
    assert status.passed is False


def test_combine_consistency_is_measured_against_the_target_not_the_total():
    """This is where the Combine rule differs from the payout rule.

    $1,600 of $3,200 is 50% of TOTAL, which would pass a total-based test.
    The Combine measures against the $3,000 TARGET, where the ceiling is
    $1,650 -- so this one squeaks through, and $1,700 would not.
    """
    status = combine_status(sessions(1_600.0, 1_600.0))
    assert status.consistency_ratio == pytest.approx(0.5)
    assert status.consistency_ok is True

    worse = combine_status(sessions(1_700.0, 1_700.0))
    assert worse.consistency_ratio == pytest.approx(0.5), "same ratio of total"
    assert worse.consistency_ok is False, "but over 55% of the target"


def test_combine_can_be_passed_in_two_days():
    """Topstep: 'You can pass in as few as two days.'"""
    status = combine_status(sessions(1_500.0, 1_500.0))
    assert status.passed is True
    assert status.trading_days == 2


# ===========================================================================
# Payout status
# ===========================================================================


def test_standard_path_needs_five_winning_days():
    status = payout_status(sessions(200.0, 200.0, 200.0))
    assert status.winning_days == 3
    assert status.standard_eligible is False
    assert status.standard_days_needed == 2

    better = payout_status(sessions(*[200.0] * 5))
    assert better.standard_eligible is True
    assert better.standard_cap == 4_000.0


def test_a_day_under_150_is_not_a_winning_day():
    assert payout_status(sessions(149.99)).winning_days == 0
    assert payout_status(sessions(150.0)).winning_days == 1


def test_consistency_path_needs_three_trading_days_and_the_ratio():
    status = payout_status(sessions(400.0, 300.0, 300.0))
    assert status.trading_days == 3
    assert status.ratio == pytest.approx(0.4)
    assert status.consistency_eligible is True
    assert status.consistency_cap == 6_000.0


def test_consistency_path_blocked_by_too_few_days_even_when_the_ratio_is_fine():
    status = payout_status(sessions(500.0, 500.0))
    assert status.consistency_eligible is False
    assert status.consistency_days_needed == 1


def test_consistency_path_blocked_by_the_ratio_even_with_enough_days():
    status = payout_status(sessions(1_000.0, 100.0, 100.0))
    assert status.trading_days == 3
    assert status.consistency_eligible is False
    assert status.consistency_profit_needed == pytest.approx(2_500.0 - 1_200.0)


def test_a_day_with_no_trades_does_not_count_as_a_trading_day():
    mixed = [
        SessionResult(DAY0, 400.0, trades=1),
        SessionResult(DAY0 + timedelta(days=1), 0.0, trades=0),
        SessionResult(DAY0 + timedelta(days=2), 300.0, trades=1),
    ]
    assert payout_status(mixed).trading_days == 2


def test_the_consistency_path_pays_more_and_needs_fewer_days():
    """Why both paths are reported rather than one being chosen."""
    status = payout_status(sessions(400.0, 300.0, 300.0))
    assert status.consistency_cap > status.standard_cap
    assert CONSISTENCY_PATH_TRADING_DAYS < STANDARD_PATH_WINNING_DAYS
    assert status.consistency_eligible and not status.standard_eligible


def test_best_available_cap_prefers_the_larger_eligible_path():
    """Five $200 days satisfy both paths at once, so the bigger cap applies."""
    both = payout_status(sessions(*[200.0] * 5))
    assert both.standard_eligible is True
    assert both.consistency_eligible is True
    assert both.best_available_cap == PAYOUT_CAP_CONSISTENCY == 6_000.0


def test_best_available_cap_is_zero_when_no_path_is_open():
    assert payout_status(sessions(500.0)).best_available_cap == 0.0


def test_nothing_is_withdrawable_before_eligibility():
    status = payout_status(sessions(500.0))
    assert status.withdrawable(balance=50_500.0, starting_balance=50_000.0) == 0.0


def test_withdrawal_is_bounded_by_profit_not_just_the_cap():
    """Three limits apply at once: the cap, half the balance, and real profit."""
    status = payout_status(sessions(400.0, 300.0, 300.0))
    assert status.consistency_eligible
    amount = status.withdrawable(balance=51_000.0, starting_balance=50_000.0)
    assert amount == pytest.approx(1_000.0), "profit earned, not the 6,000 cap"


def test_withdrawal_respects_the_minimum_request():
    """$120 of profit is eligible on every other count but too small to request."""
    status = payout_status(sessions(40.0, 40.0, 40.0))
    assert status.trading_days == 3
    assert status.consistency_eligible is True
    amount = status.withdrawable(balance=50_120.0, starting_balance=50_000.0)
    assert amount == 0.0, "below the $125 minimum"


def test_just_over_the_minimum_is_withdrawable():
    status = payout_status(sessions(50.0, 50.0, 50.0))
    amount = status.withdrawable(balance=50_150.0, starting_balance=50_000.0)
    assert amount == pytest.approx(150.0)


def test_withdrawal_is_capped_at_half_the_balance():
    """Half the balance binds before the cap and before the profit earned."""
    status = payout_status(sessions(3_000.0, 3_000.0, 3_000.0))
    assert status.consistency_eligible is True
    amount = status.withdrawable(balance=1_000.0, starting_balance=0.0)
    assert amount == pytest.approx(500.0), "half of 1,000, not the 6,000 cap"


# ===========================================================================
# Report
# ===========================================================================


def test_report_states_both_paths_and_the_actionable_gap():
    text = report(sessions(1_000.0, 100.0, 100.0), balance=51_200.0)
    assert "COMBINE" in text
    assert "STANDARD path" in text
    assert "CONSISTENCY path" in text
    assert "more profit" in text
    assert "resets the MLL to $0" in text


def test_report_handles_an_empty_history():
    text = report([])
    assert "n/a" in text
    assert "COMBINE" in text
