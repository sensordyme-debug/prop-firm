"""Tests for the reconstructed trailing MLL floor.

This is the only guard against PERMANENT account failure, and the API does not
supply it, so the arithmetic is pinned against Topstep's own published
examples rather than against our own reading of the rule.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from mll_tracker import (
    DEFAULT_MLL_DISTANCE,
    DEFAULT_STARTING_BALANCE,
    MllState,
    MllStateError,
    floor_for,
    is_stale,
    parse_state,
    reconcile,
    record_eod,
    seed_state,
    serialise_state,
)

START = DEFAULT_STARTING_BALANCE  # 50,000
DIST = DEFAULT_MLL_DISTANCE       # 2,000


def fresh() -> MllState:
    return seed_state()


# ===========================================================================
# Topstep's own worked examples
# ===========================================================================


def test_initial_floor_is_48000_on_a_50k_account():
    assert floor_for(fresh()) == 48_000.0


def test_topstep_example_eod_50500_gives_a_48500_floor():
    """Their first worked example: the floor trails the EOD close up."""
    state = record_eod(fresh(), date(2026, 9, 16), 50_500.0)
    assert floor_for(state) == 48_500.0


def test_topstep_example_a_later_eod_of_50000_leaves_the_floor_at_48500():
    """Their second: the floor NEVER falls, even when the balance does."""
    state = record_eod(fresh(), date(2026, 9, 16), 50_500.0)
    assert floor_for(state) == 48_500.0

    state = record_eod(state, date(2026, 9, 17), 50_000.0)
    assert floor_for(state) == 48_500.0, "a losing day must not lower the floor"
    assert state.max_eod_balance == 50_500.0


def test_intraday_spikes_do_not_move_the_floor():
    """Only END-OF-DAY closes count; intraday equity is irrelevant to the floor."""
    state = record_eod(fresh(), date(2026, 9, 16), 50_100.0)
    assert floor_for(state) == 48_100.0  # not 48,900 from some intraday high


# ===========================================================================
# The permanent lock
# ===========================================================================


def test_floor_locks_at_the_starting_balance_and_never_exceeds_it():
    state = record_eod(fresh(), date(2026, 9, 16), 52_000.0)
    assert floor_for(state) == 50_000.0
    assert state.locked is True


def test_floor_does_not_lock_one_dollar_early():
    state = record_eod(fresh(), date(2026, 9, 16), 51_999.0)
    assert floor_for(state) == 49_999.0
    assert state.locked is False


def test_locked_floor_never_rises_again():
    state = record_eod(fresh(), date(2026, 9, 16), 52_000.0)
    assert state.locked is True

    state = record_eod(state, date(2026, 9, 17), 60_000.0)
    assert floor_for(state) == 50_000.0, "locked means locked"
    assert state.max_eod_balance == 52_000.0


def test_floor_is_capped_at_starting_balance_even_on_a_huge_day():
    state = record_eod(fresh(), date(2026, 9, 16), 80_000.0)
    assert floor_for(state) == 50_000.0


# ===========================================================================
# Recording discipline
# ===========================================================================


def test_recording_the_same_date_twice_is_a_no_op():
    state = record_eod(fresh(), date(2026, 9, 16), 50_500.0)
    again = record_eod(state, date(2026, 9, 16), 50_500.0)
    assert again == state, "a double-count would ratchet the floor up wrongly"


def test_recording_the_same_date_with_a_different_balance_is_rejected():
    state = record_eod(fresh(), date(2026, 9, 16), 50_500.0)
    with pytest.raises(MllStateError, match="already recorded"):
        record_eod(state, date(2026, 9, 16), 50_900.0)


def test_recording_out_of_order_is_rejected():
    state = record_eod(fresh(), date(2026, 9, 17), 50_500.0)
    with pytest.raises(MllStateError, match="before the last recorded date"):
        record_eod(state, date(2026, 9, 16), 50_200.0)


def test_negative_eod_balance_is_rejected():
    with pytest.raises(MllStateError, match="negative"):
        record_eod(fresh(), date(2026, 9, 16), -1.0)


# ===========================================================================
# Seeding from the dashboard
# ===========================================================================


def test_seeding_from_the_displayed_floor_reproduces_that_floor():
    state = seed_state(observed_floor=48_500.0)
    assert floor_for(state) == 48_500.0
    assert state.max_eod_balance == 50_500.0, "the floor implies this EOD high"


def test_seeding_at_the_locked_floor_marks_it_locked():
    state = seed_state(observed_floor=50_000.0)
    assert floor_for(state) == 50_000.0
    assert state.locked is True


def test_seeding_above_the_starting_balance_is_rejected():
    with pytest.raises(MllStateError, match="above the starting balance"):
        seed_state(observed_floor=50_500.0)


def test_seeding_rejects_nonsense_parameters():
    with pytest.raises(MllStateError, match="starting_balance"):
        seed_state(starting_balance=0.0)
    with pytest.raises(MllStateError, match="mll_distance"):
        seed_state(mll_distance=-2_000.0)


# ===========================================================================
# Fail closed: missing, corrupt, stale
# ===========================================================================


def test_unparseable_json_raises():
    with pytest.raises(MllStateError, match="not valid JSON"):
        parse_state("{not json")


def test_json_that_is_not_an_object_raises():
    with pytest.raises(MllStateError, match="must be a JSON object"):
        parse_state("[1, 2, 3]")


def test_missing_required_fields_raise():
    with pytest.raises(MllStateError, match="missing required field"):
        parse_state(json.dumps({"starting_balance": 50_000.0}))


def test_non_numeric_fields_raise():
    with pytest.raises(MllStateError, match="non-numeric"):
        parse_state(json.dumps({
            "starting_balance": "fifty thousand",
            "mll_distance": 2_000.0,
            "max_eod_balance": 50_000.0,
        }))


def test_nonsensical_values_raise():
    with pytest.raises(MllStateError, match="nonsensical"):
        parse_state(json.dumps({
            "starting_balance": -50_000.0,
            "mll_distance": 2_000.0,
            "max_eod_balance": 50_000.0,
        }))


def test_high_water_mark_below_the_open_raises():
    with pytest.raises(MllStateError, match="below starting_balance"):
        parse_state(json.dumps({
            "starting_balance": 50_000.0,
            "mll_distance": 2_000.0,
            "max_eod_balance": 49_000.0,
        }))


def test_bad_date_raises():
    with pytest.raises(MllStateError, match="ISO date"):
        parse_state(json.dumps({
            "starting_balance": 50_000.0,
            "mll_distance": 2_000.0,
            "max_eod_balance": 50_000.0,
            "last_eod_date": "the 16th",
        }))


def test_round_trip_through_serialisation_preserves_the_floor():
    original = record_eod(fresh(), date(2026, 9, 16), 50_500.0)
    restored = parse_state(serialise_state(original))
    assert restored == original
    assert floor_for(restored) == 48_500.0


def test_state_with_no_recorded_eod_is_stale():
    """Never recorded means we cannot know the floor is current."""
    assert is_stale(fresh(), date(2026, 9, 17), date(2026, 9, 16)) is True


def test_state_recorded_for_the_last_session_is_not_stale():
    state = record_eod(fresh(), date(2026, 9, 16), 50_500.0)
    assert is_stale(state, date(2026, 9, 17), date(2026, 9, 16)) is False


def test_state_older_than_the_last_session_is_stale():
    state = record_eod(fresh(), date(2026, 9, 14), 50_500.0)
    assert is_stale(state, date(2026, 9, 17), date(2026, 9, 16)) is True


def test_state_recorded_today_is_not_stale():
    state = record_eod(fresh(), date(2026, 9, 17), 50_500.0)
    assert is_stale(state, date(2026, 9, 17), date(2026, 9, 16)) is False


# ===========================================================================
# Reconciliation with the dashboard
# ===========================================================================


def test_matching_floors_produce_no_warning():
    state = record_eod(fresh(), date(2026, 9, 16), 50_500.0)
    adopted, warning = reconcile(state, 48_500.0)
    assert warning is None
    assert adopted is state


def test_a_higher_dashboard_floor_is_adopted():
    """Less headroom wins. Our EOD history is probably missing a day."""
    state = record_eod(fresh(), date(2026, 9, 16), 50_500.0)
    adopted, warning = reconcile(state, 49_000.0)
    assert floor_for(adopted) == 49_000.0
    assert warning is not None
    assert "DISAGREEMENT" in warning


def test_a_lower_dashboard_floor_does_not_lower_ours():
    """Keeping the higher floor is the safe direction even if theirs is newer."""
    state = record_eod(fresh(), date(2026, 9, 16), 50_500.0)
    adopted, warning = reconcile(state, 48_000.0)
    assert floor_for(adopted) == 48_500.0, "never adopt a lower floor"
    assert warning is not None
    assert "Keeping ours" in warning


def test_disagreement_always_resolves_to_the_higher_floor():
    state = record_eod(fresh(), date(2026, 9, 16), 50_500.0)
    for dashboard in (47_000.0, 48_000.0, 48_500.0, 49_000.0, 50_000.0):
        adopted, _ = reconcile(state, dashboard)
        assert floor_for(adopted) == max(48_500.0, dashboard)
