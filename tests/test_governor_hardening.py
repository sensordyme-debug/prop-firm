"""Tests for the state-integrity, config, calendar and drift guards.

Kept separate from test_governor.py, which covers the four mandated trip-wires
and the session clock. These cover the ways the governor can be fed bad state
by a caller that is working correctly in every other respect.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime

import pytest

from governor import (
    ET,
    AccountSnapshot,
    Action,
    Config,
    GovernorState,
    Reason,
    effective_trade_count,
    evaluate,
    hard_flatten_at,
    roll_session,
    session_start_for,
    session_trading_date,
)

START_BALANCE = 50_000.0
MLL_FLOOR = 48_000.0
WED_1000 = datetime(2026, 9, 16, 10, 0, tzinfo=ET)


def cfg(**overrides) -> Config:
    return Config(**overrides)


def snap(
    *,
    net_liq: float = START_BALANCE,
    balance: float | None = None,
    mll_floor: float | None = MLL_FLOOR,
    open_position_size: int = 0,
    session_start_balance: float = START_BALANCE,
    now: datetime = WED_1000,
    kill_switch_active: bool = False,
    observed_trades: int | None = None,
) -> AccountSnapshot:
    return AccountSnapshot(
        net_liq=net_liq,
        balance=net_liq if balance is None else balance,
        mll_floor=mll_floor,
        open_position_size=open_position_size,
        session_start_balance=session_start_balance,
        now=now,
        kill_switch_active=kill_switch_active,
        observed_trades=observed_trades,
    )


def state(
    *,
    session_start_balance: float = START_BALANCE,
    trades_today: int = 0,
    halted: bool = False,
    reconciled: bool = True,
    session_start: datetime | None = None,
) -> GovernorState:
    return GovernorState(
        session_start_balance=session_start_balance,
        trades_today=trades_today,
        halted=halted,
        last_reconcile=datetime(2026, 9, 16, 9, 25, tzinfo=ET) if reconciled else None,
        session_start=session_start,
    )


def decide(snapshot=None, st=None, config=None):
    """evaluate() with the anchor a correct caller would have set."""
    s = snapshot or snap()
    c = config or cfg()
    st = st or state()
    if st.session_start is None:
        st = replace(st, session_start=session_start_for(s.now, c))
    return evaluate(s, st, c)


# ===========================================================================
# Stale session anchor -- the bug that disables the loss limit
# ===========================================================================


def test_stale_anchor_halts_even_though_the_stale_pnl_looks_healthy():
    """The exact reported scenario, end to end.

    Session N anchors at 50,000 and closes at net liq 50,400. Session N+1
    opens but roll_session is never called, so the anchor stays 50,000. Net
    liq falls to 50,150 -- a TRUE session loss of 250, the halt point.

    Measured from the stale anchor it reads as +150, and every trip-wire stays
    quiet. The anchor check must catch it before any of them run.
    """
    session_n = datetime(2026, 9, 15, 18, 0, tzinfo=ET)   # yesterday's anchor
    now = datetime(2026, 9, 16, 19, 30, tzinfo=ET)        # session N+1 has opened
    stale = state(session_start_balance=50_000.0, session_start=session_n)
    s = snap(net_liq=50_150.0, session_start_balance=50_000.0, now=now)

    assert s.net_liq - stale.session_start_balance == pytest.approx(150.0), (
        "the stale reading looks like a profit"
    )

    d = evaluate(s, stale, cfg())
    assert d.action is Action.FLATTEN_AND_HALT
    assert d.code == Reason.STALE_SESSION_ANCHOR
    assert "roll_session" in d.reason


def test_stale_anchor_halts_where_a_true_500_loss_reads_as_minus_100():
    """The deeper case: a true -500 reads as -100 and would still CONTINUE."""
    session_n = datetime(2026, 9, 15, 18, 0, tzinfo=ET)
    now = datetime(2026, 9, 16, 19, 30, tzinfo=ET)
    stale = state(session_start_balance=50_000.0, session_start=session_n)
    s = snap(net_liq=49_900.0, session_start_balance=50_000.0, now=now)

    assert s.net_liq - stale.session_start_balance == pytest.approx(-100.0)
    d = evaluate(s, stale, cfg())
    assert d.action is Action.FLATTEN_AND_HALT
    assert d.code == Reason.STALE_SESSION_ANCHOR


def test_missing_anchor_halts():
    d = evaluate(snap(), state(session_start=None), cfg())
    assert d.action is Action.FLATTEN_AND_HALT
    assert d.code == Reason.STALE_SESSION_ANCHOR
    assert "no session anchor" in d.reason


def test_anchor_check_runs_before_every_tripwire():
    """A stale anchor must not be masked by a trip-wire computed from it."""
    stale = state(session_start=datetime(2026, 9, 14, 18, 0, tzinfo=ET))
    d = evaluate(snap(net_liq=50_600.0), stale, cfg())  # would fire the target
    assert d.code == Reason.STALE_SESSION_ANCHOR


def test_anchor_mismatch_cannot_cover_the_stale_case():
    """Two stale anchors agree with each other, so the 3d check never fires."""
    session_n = datetime(2026, 9, 15, 18, 0, tzinfo=ET)
    now = datetime(2026, 9, 16, 19, 30, tzinfo=ET)
    stale = state(session_start_balance=50_000.0, session_start=session_n)
    s = snap(net_liq=50_150.0, session_start_balance=50_000.0, now=now)
    assert stale.session_start_balance == s.session_start_balance
    assert evaluate(s, stale, cfg()).code == Reason.STALE_SESSION_ANCHOR


def test_rolling_the_session_clears_the_stale_halt():
    """The documented fix: call roll_session and the same snapshot is fine."""
    session_n = datetime(2026, 9, 15, 18, 0, tzinfo=ET)
    now = datetime(2026, 9, 16, 19, 30, tzinfo=ET)
    stale = state(session_start_balance=50_000.0, session_start=session_n)
    s = snap(net_liq=50_150.0, session_start_balance=50_150.0, now=now)

    assert evaluate(s, stale, cfg()).code == Reason.STALE_SESSION_ANCHOR
    rolled = roll_session(s, stale, cfg())
    after = evaluate(s, rolled, cfg())
    assert after.code != Reason.STALE_SESSION_ANCHOR
    assert after.session_pnl == 0.0, "a rolled session starts flat"


# ===========================================================================
# MLL state unavailable
# ===========================================================================


def test_unknown_mll_floor_halts():
    d = decide(snap(mll_floor=None))
    assert d.action is Action.FLATTEN_AND_HALT
    assert d.code == Reason.MLL_STATE_UNAVAILABLE


def test_unknown_mll_floor_halts_even_when_everything_else_is_healthy():
    assert decide(snap(mll_floor=None, net_liq=50_100.0)).code == (
        Reason.MLL_STATE_UNAVAILABLE
    )


def test_unknown_mll_floor_is_checked_before_the_floor_buffer():
    """There is no floor to compare against, so the buffer check cannot run."""
    assert decide(snap(mll_floor=None, net_liq=40_000.0)).code == (
        Reason.MLL_STATE_UNAVAILABLE
    )


# ===========================================================================
# Trade count drift
# ===========================================================================


def test_drift_refuses_when_record_trade_was_missed():
    d = decide(snap(observed_trades=1), state(trades_today=0))
    assert d.action is Action.REFUSE_ENTRY
    assert d.code == Reason.TRADE_COUNT_DRIFT
    assert "record_trade was missed" in d.reason


def test_agreeing_counts_do_not_report_drift():
    assert decide(snap(observed_trades=1), state(trades_today=1)).action is (
        Action.CONTINUE
    )


def test_absent_observation_falls_back_to_the_tracked_count():
    assert decide(snap(observed_trades=None), state(trades_today=1)).action is (
        Action.CONTINUE
    )


def test_effective_count_takes_the_higher_of_the_two():
    assert effective_trade_count(snap(observed_trades=2), state(trades_today=0)) == 2
    assert effective_trade_count(snap(observed_trades=0), state(trades_today=2)) == 2
    assert effective_trade_count(snap(observed_trades=None), state(trades_today=1)) == 1


def test_budget_binds_on_the_observed_count_when_state_lost_it():
    """Two real entries but a zeroed counter must still stop a third."""
    assert effective_trade_count(snap(observed_trades=2), state(trades_today=0)) == 2
    assert decide(snap(observed_trades=2), state(trades_today=0)).action is (
        Action.REFUSE_ENTRY
    )


def test_tracked_count_higher_than_observed_still_exhausts_the_budget():
    """An incomplete fill history must not re-open a spent budget."""
    d = decide(snap(observed_trades=0), state(trades_today=2))
    assert d.action is Action.REFUSE_ENTRY
    assert d.code == Reason.TRADE_COUNT_EXHAUSTED


# ===========================================================================
# Config validation
# ===========================================================================


def test_negative_floor_buffer_is_rejected():
    """The fail-OPEN case: a negative buffer halts only after the account is gone."""
    with pytest.raises(ValueError, match="floor_buffer"):
        cfg(floor_buffer=-100.0)


def test_zero_floor_buffer_is_allowed():
    assert cfg(floor_buffer=0.0).floor_buffer == 0.0


@pytest.mark.parametrize(
    ("kwargs", "field_name"),
    [
        ({"daily_max_loss": 0.0}, "daily_max_loss"),
        ({"daily_max_loss": -250.0}, "daily_max_loss"),
        ({"daily_profit_target": 0.0}, "daily_profit_target"),
        ({"daily_profit_target": -500.0}, "daily_profit_target"),
        ({"max_trades_per_session": -1}, "max_trades_per_session"),
        ({"entry_lockout_minutes": -5}, "entry_lockout_minutes"),
        ({"anchor_tolerance": -0.01}, "anchor_tolerance"),
    ],
)
def test_config_rejects_out_of_range_values(kwargs, field_name):
    with pytest.raises(ValueError, match=field_name):
        cfg(**kwargs)


def test_from_env_names_the_variable_on_an_unparseable_value(monkeypatch):
    monkeypatch.setenv("DAILY_MAX_LOSS", "two hundred and fifty")
    with pytest.raises(ValueError, match="DAILY_MAX_LOSS"):
        Config.from_env()


def test_from_env_rejects_a_negative_buffer_from_the_environment(monkeypatch):
    monkeypatch.setenv("FLOOR_BUFFER", "-400")
    with pytest.raises(ValueError, match="floor_buffer"):
        Config.from_env()


# ===========================================================================
# Market calendar
# ===========================================================================


def test_saturday_morning_refuses_entry():
    """session_start_for(Sat 10:00) is Fri 18:00, whose trading date is Saturday."""
    sat = datetime(2026, 9, 19, 10, 0, tzinfo=ET)
    assert session_trading_date(sat) == date(2026, 9, 19)
    d = decide(snap(now=sat))
    assert d.action is Action.REFUSE_ENTRY
    assert d.code == Reason.MARKET_CLOSED
    assert "Saturday" in d.reason


def test_sunday_before_the_reopen_refuses_entry():
    d = decide(snap(now=datetime(2026, 9, 20, 10, 0, tzinfo=ET)))
    assert d.code == Reason.MARKET_CLOSED
    assert "Sunday" in d.reason


def test_sunday_evening_belongs_to_monday_and_is_not_market_closed():
    assert decide(snap(now=datetime(2026, 9, 20, 18, 30, tzinfo=ET))).code != (
        Reason.MARKET_CLOSED
    )


def test_full_holiday_refuses_entry():
    d = decide(snap(now=datetime(2026, 11, 26, 10, 0, tzinfo=ET)))
    assert d.code == Reason.MARKET_CLOSED
    assert "Thanksgiving" in d.reason


def test_normal_weekday_is_not_market_closed():
    assert decide(snap(now=datetime(2026, 9, 16, 10, 0, tzinfo=ET))).action is (
        Action.CONTINUE
    )


def test_holiday_half_day_refuses_entry_entirely():
    """DECISIONS.md: we stand aside on every holiday date, half-days included.

    Sources disagree on the close time and Topstep announces its own by
    Discord, so there is no trustworthy number to compute a deadline from.
    Refusing the whole date removes the need for one.
    """
    d = decide(snap(now=datetime(2026, 11, 27, 10, 0, tzinfo=ET)))
    assert d.action is Action.REFUSE_ENTRY
    assert d.code == Reason.MARKET_CLOSED
    assert "half-day" in d.reason


def test_no_early_close_arithmetic_survives_anywhere():
    """The flatten is 15:55 on a half-day too, because we never trade one."""
    assert hard_flatten_at(datetime(2026, 11, 27, 10, 0, tzinfo=ET)) == datetime(
        2026, 11, 27, 15, 55, tzinfo=ET
    )


def test_half_day_is_refused_at_every_hour_of_its_session():
    for hour in (9, 10, 11, 12, 13, 14):
        d = decide(snap(now=datetime(2026, 11, 27, hour, 30, tzinfo=ET)))
        assert d.code in (Reason.MARKET_CLOSED, Reason.HARD_FLATTEN_TIME,
                          Reason.AFTER_ENTRY_CUTOFF), f"hour {hour} -> {d.code}"
        assert d.action is not Action.CONTINUE


def test_regular_day_keeps_the_1555_flatten():
    assert hard_flatten_at(datetime(2026, 9, 16, 10, 0, tzinfo=ET)) == datetime(
        2026, 9, 16, 15, 55, tzinfo=ET
    )


def test_dates_outside_calendar_coverage_fail_closed():
    d = decide(snap(now=datetime(2028, 3, 15, 10, 0, tzinfo=ET)))
    assert d.action is Action.REFUSE_ENTRY
    assert d.code == Reason.MARKET_CLOSED
    assert "coverage" in d.reason


def test_calendar_guard_can_be_disabled():
    assert decide(
        snap(now=datetime(2026, 9, 19, 10, 0, tzinfo=ET)),
        config=cfg(enforce_market_calendar=False),
    ).code != Reason.MARKET_CLOSED


# ===========================================================================
# Consistency
# ===========================================================================


def test_roll_session_compares_anchors_absolutely_not_by_wall_clock():
    """Same instant, different zone: this is the SAME session, not a new one."""
    utc_anchor = datetime(2026, 9, 15, 22, 0, tzinfo=UTC)  # 18:00 ET
    assert utc_anchor == datetime(2026, 9, 15, 18, 0, tzinfo=ET)

    st = state(session_start=utc_anchor, trades_today=1)
    rolled = roll_session(snap(now=WED_1000), st, cfg())
    assert rolled is st, "a zone difference is not a new session"
    assert rolled.trades_today == 1, "the trade count must survive"


def test_evaluate_accepts_an_anchor_expressed_in_another_zone():
    st = state(session_start=datetime(2026, 9, 15, 22, 0, tzinfo=UTC))
    assert evaluate(snap(), st, cfg()).code != Reason.STALE_SESSION_ANCHOR


def test_bare_equality_would_confuse_the_two_halves_of_a_dst_fold():
    """Why roll_session compares through _utc rather than with bare ``==``.

    On the fall-back date 01:30 ET happens twice, an hour apart. Python's
    ``==`` on two datetimes sharing a tzinfo object compares WALL CLOCKS, so it
    calls those two distinct instants equal. Normalising to UTC does not.

    At the default 18:00 boundary the two forms always agree, which is why this
    is a latent trap rather than a live bug -- but ``session_boundary_et`` is
    configurable, and a boundary inside the fold makes it reachable.
    """
    edt = datetime(2026, 11, 1, 1, 30, tzinfo=ET, fold=0)  # 05:30 UTC
    est = datetime(2026, 11, 1, 1, 30, tzinfo=ET, fold=1)  # 06:30 UTC
    assert edt == est, "bare equality cannot tell them apart"
    assert edt.astimezone(UTC) != est.astimezone(UTC)

    fold_cfg = cfg(session_boundary_et=edt.time(), enforce_market_calendar=False)
    now = datetime(2026, 11, 1, 1, 40, tzinfo=ET, fold=0)
    assert session_start_for(now, fold_cfg) == edt

    # The anchor is the OTHER half of the fold: a genuinely different session.
    st = state(session_start=est, trades_today=2)
    rolled = roll_session(snap(now=now), st, fold_cfg)
    assert rolled is not st, "an hour-apart anchor must roll, not be reused"
    assert rolled.trades_today == 0


def test_a_daily_target_that_could_breach_combine_consistency_is_rejected():
    """The failure mode risk checks are blind to: failing by WINNING.

    A daily cap above 55% of the $3,000 profit target lets a single winning
    session breach the Combine consistency rule. Nothing downward-looking
    catches that, so it is enforced at config time.
    """
    with pytest.raises(ValueError, match="fail by winning"):
        cfg(daily_profit_target=1_700.0)


def test_the_consistency_ceiling_itself_is_allowed():
    assert cfg(daily_profit_target=1_650.0).daily_profit_target == 1_650.0


def test_the_default_daily_target_is_far_inside_the_ceiling():
    assert cfg().daily_profit_target == 500.0


def test_the_consistency_ceiling_guard_can_be_disabled():
    assert cfg(daily_profit_target=2_000.0,
               enforce_consistency_ceiling=False).daily_profit_target == 2_000.0


# ===========================================================================
# Contract expiry
# ===========================================================================


def test_quarterly_expiry_day_refuses_entry():
    """MNQ settles to the OPENING quote, so the deciding session is already
    over before our 09:30 window begins."""
    d = decide(snap(now=datetime(2026, 9, 18, 10, 0, tzinfo=ET)))
    assert d.action is Action.REFUSE_ENTRY
    assert d.code == Reason.CONTRACT_EXPIRY
    assert "MNQZ26" in d.reason, "and it names the contract that now leads"


def test_the_day_before_expiry_trades_normally():
    assert decide(snap(now=datetime(2026, 9, 17, 10, 0, tzinfo=ET))).action is (
        Action.CONTINUE
    )


def test_roll_day_itself_is_tradable():
    """Rolling is about WHICH contract, not whether to trade at all."""
    assert decide(snap(now=datetime(2026, 9, 14, 10, 0, tzinfo=ET))).action is (
        Action.CONTINUE
    )


def test_every_2026_quarterly_expiry_is_refused():
    for day in (date(2026, 3, 20), date(2026, 6, 19), date(2026, 9, 18),
                date(2026, 12, 18)):
        d = decide(snap(now=datetime(day.year, day.month, day.day, 10, 0, tzinfo=ET)))
        assert d.action is not Action.CONTINUE, f"{day} must not trade"


def test_the_expiry_guard_can_be_disabled():
    d = decide(snap(now=datetime(2026, 9, 18, 10, 0, tzinfo=ET)),
               config=cfg(enforce_contract_expiry=False))
    assert d.code != Reason.CONTRACT_EXPIRY
