"""Unit tests for the risk governor.

No network, no credentials, no SDK. Every test drives pure functions with
explicit timezone-aware datetimes.

Tests assert on **reason codes**, not just actions: two different trip-wires
both returning FLATTEN_AND_HALT is not enough information to debug a halt at
09:41 on a live account.

Calendar anchors used below (verified weekdays):
  2026-09-12 Saturday   2026-09-13 Sunday   2026-09-15 Tuesday
  2026-09-16 Wednesday
US DST in 2026: begins Sunday 2026-03-08, ends Sunday 2026-11-01.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from governor import (
    ET,
    AccountSnapshot,
    Action,
    Config,
    GovernorState,
    Reason,
    apply_decision,
    compute_session_pnl,
    entry_cutoff_at,
    evaluate,
    hard_flatten_at,
    record_trade,
    roll_session,
    rth_open_at,
    session_start_for,
)

# ---------------------------------------------------------------------------
# Fixtures and builders
# ---------------------------------------------------------------------------

START_BALANCE = 50_000.0
MLL_FLOOR = 48_000.0  # headroom 2,000 -- far outside the 400 buffer

WED_1000 = datetime(2026, 9, 16, 10, 0, tzinfo=ET)  # mid-window, all clear


def cfg(**overrides) -> Config:
    return Config(**overrides)


def snap(
    *,
    net_liq: float = START_BALANCE,
    balance: float | None = None,
    mll_floor: float = MLL_FLOOR,
    open_position_size: int = 0,
    session_start_balance: float = START_BALANCE,
    now: datetime = WED_1000,
    kill_switch_active: bool = False,
) -> AccountSnapshot:
    return AccountSnapshot(
        net_liq=net_liq,
        balance=net_liq if balance is None else balance,
        mll_floor=mll_floor,
        open_position_size=open_position_size,
        session_start_balance=session_start_balance,
        now=now,
        kill_switch_active=kill_switch_active,
    )


def state(
    *,
    session_start_balance: float = START_BALANCE,
    trades_today: int = 0,
    halted: bool = False,
    halt_reason: str | None = None,
    reconciled: bool = True,
    session_start: datetime | None = None,
) -> GovernorState:
    return GovernorState(
        session_start_balance=session_start_balance,
        trades_today=trades_today,
        halted=halted,
        halt_reason=halt_reason,
        last_reconcile=datetime(2026, 9, 16, 9, 25, tzinfo=ET) if reconciled else None,
        session_start=session_start,
    )


def anchored(st: GovernorState, snapshot: AccountSnapshot, config=None) -> GovernorState:
    """Give state the anchor a correct caller would have set via roll_session.

    Most tests are about some OTHER rule, so they should not all have to
    restate the anchor. Tests that exercise staleness pass one explicitly, or
    call evaluate() directly with session_start=None.
    """
    if st.session_start is None:
        st = replace(st, session_start=session_start_for(snapshot.now, config or cfg()))
    return st


def decide(snapshot=None, st=None, config=None):
    s = snapshot or snap()
    c = config or cfg()
    return evaluate(s, anchored(st or state(), s, c), c)


def ev(snapshot, st=None, config=None):
    """evaluate() with the anchor aligned, for tests that call it directly."""
    c = config or cfg()
    return evaluate(snapshot, anchored(st or state(), snapshot, c), c)


def elapsed(later: datetime, earlier: datetime) -> timedelta:
    """Real elapsed time between two aware datetimes.

    Plain subtraction is NOT this. Python ignores the zone when both operands
    share a tzinfo object and subtracts wall-clock readings, so across a DST
    transition ``b - a`` is off by an hour. Normalising to UTC is what makes
    the duration absolute.
    """
    return later.astimezone(UTC) - earlier.astimezone(UTC)


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------


def test_clean_state_mid_window_allows_entry():
    d = decide()
    assert d.action is Action.CONTINUE
    assert d.code == Reason.OK
    assert d.may_enter is True
    assert d.session_pnl == 0.0


def test_session_pnl_uses_net_liq_not_balance():
    """CLAUDE.md constraint 3: the MLL is enforced on net liq including open P&L.

    A snapshot whose balance is unchanged but whose net liq is down $300 must
    report a $300 session loss, not zero.
    """
    s = snap(net_liq=49_700.0, balance=START_BALANCE, open_position_size=2)
    assert compute_session_pnl(s, state()) == pytest.approx(-300.0)
    assert ev(s).session_pnl == pytest.approx(-300.0)


def test_evaluate_is_deterministic_and_uses_snapshot_now_not_wall_clock():
    """A 2020 timestamp must be judged on its own terms, proving no clock read.

    The decision cites 2020, not today, which is only possible if ``now`` came
    from the snapshot. It also demonstrates the calendar failing closed outside
    its coverage rather than assuming an unknown date is tradable.
    """
    long_ago = datetime(2020, 5, 6, 10, 0, tzinfo=ET)
    s = snap(now=long_ago)
    first = ev(s)
    second = ev(s)
    assert first == second, "same inputs must give the same decision"
    assert "2020-05-06" in first.reason
    assert first.code == Reason.MARKET_CLOSED


def test_evaluate_is_deterministic_on_an_ordinary_day():
    s = snap(now=datetime(2026, 9, 16, 10, 0, tzinfo=ET))
    assert ev(s) == ev(s)
    assert ev(s).action is Action.CONTINUE


def test_naive_datetime_is_rejected_not_coerced():
    with pytest.raises(ValueError, match="timezone-aware"):
        snap(now=datetime(2026, 9, 16, 10, 0))


# ---------------------------------------------------------------------------
# Trip-wire 1: trailing MLL floor buffer
# ---------------------------------------------------------------------------


def test_floor_buffer_fires_exactly_at_the_buffer():
    """net_liq - mll_floor == 400 must fire (the spec is <=, not <)."""
    d = decide(snap(net_liq=START_BALANCE, mll_floor=49_600.0))
    assert d.action is Action.FLATTEN_AND_HALT
    assert d.code == Reason.MLL_FLOOR_BUFFER
    assert "49,600.00" in d.reason


def test_floor_buffer_does_not_fire_one_cent_outside():
    d = decide(snap(net_liq=START_BALANCE, mll_floor=49_599.99))
    assert d.action is Action.CONTINUE


def test_floor_buffer_fires_when_net_liq_is_below_the_floor():
    d = decide(snap(net_liq=47_900.0, mll_floor=48_000.0))
    assert d.action is Action.FLATTEN_AND_HALT
    assert d.code == Reason.MLL_FLOOR_BUFFER


def test_floor_buffer_uses_net_liq_including_open_loss():
    """Balance still 50k, open position down 1,900: the floor guard must fire."""
    d = decide(
        snap(
            net_liq=48_100.0,
            balance=START_BALANCE,
            mll_floor=48_000.0,
            open_position_size=2,
        )
    )
    assert d.action is Action.FLATTEN_AND_HALT
    assert d.code == Reason.MLL_FLOOR_BUFFER


# ---------------------------------------------------------------------------
# Trip-wire 2: session max loss
# ---------------------------------------------------------------------------


def test_daily_max_loss_fires_exactly_at_the_limit():
    d = decide(snap(net_liq=49_750.0))
    assert d.action is Action.FLATTEN_AND_HALT
    assert d.code == Reason.DAILY_MAX_LOSS
    assert "-250.00" in d.reason
    assert d.session_pnl == pytest.approx(-250.0)


def test_daily_max_loss_does_not_fire_one_cent_inside():
    d = decide(snap(net_liq=49_750.01))
    assert d.action is Action.CONTINUE


# ---------------------------------------------------------------------------
# Trip-wire 3: hard flatten clock
# ---------------------------------------------------------------------------


def test_hard_flatten_fires_exactly_at_1555_et():
    """Topstep flattens from 16:08 and requires flat by 16:10; we act 15:55."""
    d = decide(snap(now=datetime(2026, 9, 16, 15, 55, 0, tzinfo=ET)))
    assert d.action is Action.FLATTEN_AND_HALT
    assert d.code == Reason.HARD_FLATTEN_TIME
    assert "15:55" in d.reason


def test_hard_flatten_does_not_fire_one_second_before():
    d = decide(snap(now=datetime(2026, 9, 16, 15, 54, 59, tzinfo=ET)))
    assert d.action is not Action.FLATTEN_AND_HALT


def test_flatten_lands_safely_before_topstep_acts():
    """The firm requires flat by 16:10 ET and starts flattening at 16:08 ET.

    Pinning the relationship in a test means a future edit to hard_flatten_et
    cannot quietly drift past the point where Topstep's desk intervenes.
    """
    c = cfg()
    assert c.hard_flatten_et == time(15, 55)
    topstep_starts_flattening = time(16, 8)
    firm_deadline = time(16, 10)
    assert c.hard_flatten_et < topstep_starts_flattening < firm_deadline

    margin = datetime.combine(WED_1000.date(), topstep_starts_flattening) - datetime.combine(
        WED_1000.date(), c.hard_flatten_et
    )
    assert margin == timedelta(minutes=13)


def test_last_possible_entry_is_1545_et():
    """15:55 flatten minus the 10-minute lockout puts the final entry at 15:45.

    Checked with the strategy's own 11:30 cutoff lifted, so this exercises the
    governor's backstop rather than the stricter rule that normally binds.
    """
    assert LATE_CUTOFF.hard_flatten_et == time(15, 55)
    at_1545 = decide(snap(now=datetime(2026, 9, 16, 15, 45, 0, tzinfo=ET)), config=LATE_CUTOFF)
    assert at_1545.action is Action.CONTINUE

    after = decide(snap(now=datetime(2026, 9, 16, 15, 45, 1, tzinfo=ET)), config=LATE_CUTOFF)
    assert after.action is Action.REFUSE_ENTRY
    assert after.code == Reason.NEAR_HARD_FLATTEN


def test_no_entry_survives_into_topsteps_flattening_window():
    """Nothing may be opened at or after 16:08 ET under any configuration."""
    for cutoff in (time(11, 30), time(17, 0)):
        d = decide(
            snap(now=datetime(2026, 9, 16, 16, 8, 0, tzinfo=ET)),
            config=cfg(entry_cutoff_et=cutoff),
        )
        assert d.action is Action.FLATTEN_AND_HALT
        assert d.code == Reason.HARD_FLATTEN_TIME


def test_hard_flatten_fires_for_an_evening_timestamp_past_the_cutoff():
    """17:00 ET belongs to the session that opened at 18:00 the previous day,
    whose 15:55 flatten has already passed."""
    d = decide(snap(now=datetime(2026, 9, 16, 17, 0, tzinfo=ET)))
    assert d.action is Action.FLATTEN_AND_HALT
    assert d.code == Reason.HARD_FLATTEN_TIME


def test_hard_flatten_does_not_fire_just_after_the_session_reopens():
    """18:30 opens a NEW session; its 15:55 is tomorrow, so no halt."""
    d = decide(snap(now=datetime(2026, 9, 16, 18, 30, tzinfo=ET)))
    assert d.action is not Action.FLATTEN_AND_HALT


# ---------------------------------------------------------------------------
# Trip-wire 4: session profit target
# ---------------------------------------------------------------------------


def test_daily_profit_target_fires_exactly_at_the_target():
    d = decide(snap(net_liq=50_500.0))
    assert d.action is Action.FLATTEN_AND_HALT
    assert d.code == Reason.DAILY_PROFIT_TARGET
    assert "500.00" in d.reason
    assert d.session_pnl == pytest.approx(500.0)


def test_daily_profit_target_does_not_fire_one_cent_inside():
    d = decide(snap(net_liq=50_499.99))
    assert d.action is Action.CONTINUE


# ---------------------------------------------------------------------------
# Trip-wire distinctness and precedence
# ---------------------------------------------------------------------------


def test_every_tripwire_has_a_distinct_reason_code():
    codes = {
        decide(snap(net_liq=START_BALANCE, mll_floor=49_600.0)).code,
        decide(snap(net_liq=49_750.0)).code,
        decide(snap(now=datetime(2026, 9, 16, 15, 55, tzinfo=ET))).code,
        decide(snap(net_liq=50_500.0)).code,
    }
    assert codes == {
        Reason.MLL_FLOOR_BUFFER,
        Reason.DAILY_MAX_LOSS,
        Reason.HARD_FLATTEN_TIME,
        Reason.DAILY_PROFIT_TARGET,
    }


def test_mll_floor_outranks_daily_loss_when_both_fire():
    """The floor is the only permanent-failure rule, so it must be reported."""
    d = decide(snap(net_liq=48_200.0, mll_floor=48_000.0))
    assert d.code == Reason.MLL_FLOOR_BUFFER


def test_daily_loss_outranks_the_clock_when_both_fire():
    d = decide(snap(net_liq=49_700.0, now=datetime(2026, 9, 16, 16, 45, tzinfo=ET)))
    assert d.code == Reason.DAILY_MAX_LOSS


# ---------------------------------------------------------------------------
# Halted-session behaviour
# ---------------------------------------------------------------------------


def test_halted_and_flat_refuses_entry_and_echoes_the_halt_reason():
    d = decide(st=state(halted=True, halt_reason="DAILY_MAX_LOSS: ..."))
    assert d.action is Action.REFUSE_ENTRY
    assert d.code == Reason.SESSION_HALTED
    assert "DAILY_MAX_LOSS" in d.reason


def test_halted_with_an_open_position_re_asserts_the_flatten():
    """A halted session that is somehow not flat must keep demanding a flatten."""
    d = decide(
        snap(open_position_size=2),
        state(halted=True, halt_reason="KILL"),
    )
    assert d.action is Action.FLATTEN_AND_HALT
    assert d.code == Reason.HALTED_POSITION_OPEN
    assert "2" in d.reason


# ---------------------------------------------------------------------------
# Entry refusals
# ---------------------------------------------------------------------------


def test_refuses_when_kill_switch_file_is_present():
    d = decide(snap(kill_switch_active=True))
    assert d.action is Action.REFUSE_ENTRY
    assert d.code == Reason.KILL_SWITCH


def test_refuses_when_state_is_unreconciled_after_restart():
    d = decide(st=state(reconciled=False))
    assert d.action is Action.REFUSE_ENTRY
    assert d.code == Reason.UNRECONCILED
    assert "reconcile" in d.reason


def test_refuses_when_a_position_is_already_open():
    d = decide(snap(open_position_size=2))
    assert d.action is Action.REFUSE_ENTRY
    assert d.code == Reason.POSITION_ALREADY_OPEN


def test_refuses_when_a_short_position_is_open():
    d = decide(snap(open_position_size=-2))
    assert d.action is Action.REFUSE_ENTRY
    assert d.code == Reason.POSITION_ALREADY_OPEN
    assert "-2" in d.reason


def test_refuses_when_trade_budget_is_spent():
    d = decide(st=state(trades_today=2))
    assert d.action is Action.REFUSE_ENTRY
    assert d.code == Reason.TRADE_COUNT_EXHAUSTED


def test_allows_entry_on_the_second_trade_of_the_session():
    d = decide(st=state(trades_today=1))
    assert d.action is Action.CONTINUE


def test_refuses_when_the_governor_and_snapshot_anchors_disagree():
    """A wrong anchor silently corrupts every daily limit."""
    d = decide(snap(session_start_balance=49_000.0), state(session_start_balance=50_000.0))
    assert d.action is Action.REFUSE_ENTRY
    assert d.code == Reason.SESSION_ANCHOR_MISMATCH
    assert "1,000.00" in d.reason


def test_matching_anchors_within_rounding_tolerance_are_accepted():
    d = decide(snap(session_start_balance=50_000.004), state(session_start_balance=50_000.0))
    assert d.action is Action.CONTINUE


# -- 10-minute lockout before the hard flatten ------------------------------
# The 11:30 cutoff would mask this window, so these two use a later cutoff.
# With the flatten at 15:55, the lockout puts the last entry at 15:45:00.

LATE_CUTOFF = cfg(entry_cutoff_et=time(17, 0))


def test_refuses_inside_the_ten_minute_lockout():
    d = decide(snap(now=datetime(2026, 9, 16, 15, 45, 1, tzinfo=ET)), config=LATE_CUTOFF)
    assert d.action is Action.REFUSE_ENTRY
    assert d.code == Reason.NEAR_HARD_FLATTEN


def test_allows_entry_with_exactly_ten_minutes_remaining():
    """'Fewer than 10 minutes' -- at exactly 10:00 remaining, entry stands."""
    d = decide(snap(now=datetime(2026, 9, 16, 15, 45, 0, tzinfo=ET)), config=LATE_CUTOFF)
    assert d.action is Action.CONTINUE


# -- 11:30 entry cutoff ------------------------------------------------------


def test_refuses_at_exactly_the_1130_cutoff():
    """Inclusive by choice: entering on the cutoff instant is the riskier read."""
    d = decide(snap(now=datetime(2026, 9, 16, 11, 30, 0, tzinfo=ET)))
    assert d.action is Action.REFUSE_ENTRY
    assert d.code == Reason.AFTER_ENTRY_CUTOFF


def test_allows_entry_one_second_before_the_cutoff():
    d = decide(snap(now=datetime(2026, 9, 16, 11, 29, 59, tzinfo=ET)))
    assert d.action is Action.CONTINUE


def test_refuses_well_after_the_cutoff():
    d = decide(snap(now=datetime(2026, 9, 16, 14, 0, tzinfo=ET)))
    assert d.action is Action.REFUSE_ENTRY
    assert d.code == Reason.AFTER_ENTRY_CUTOFF


# -- 09:30 regular-session open ---------------------------------------------


def test_refuses_before_the_regular_session_opens():
    d = decide(snap(now=datetime(2026, 9, 16, 9, 29, 59, tzinfo=ET)))
    assert d.action is Action.REFUSE_ENTRY
    assert d.code == Reason.BEFORE_RTH_OPEN


def test_allows_entry_exactly_at_the_open():
    d = decide(snap(now=datetime(2026, 9, 16, 9, 30, 0, tzinfo=ET)))
    assert d.action is Action.CONTINUE


def test_refuses_an_overnight_entry_at_2000_et():
    """No overnight positions: 20:00 is inside a fresh session but pre-open."""
    d = decide(snap(now=datetime(2026, 9, 16, 20, 0, tzinfo=ET)))
    assert d.action is Action.REFUSE_ENTRY
    assert d.code == Reason.BEFORE_RTH_OPEN


def test_rth_open_guard_can_be_disabled_without_touching_the_tripwires():
    d = decide(
        snap(now=datetime(2026, 9, 16, 20, 0, tzinfo=ET)),
        config=cfg(enforce_rth_open=False),
    )
    assert d.action is Action.CONTINUE


def test_every_refusal_has_a_distinct_reason_code():
    codes = {
        decide(snap(kill_switch_active=True)).code,
        decide(st=state(reconciled=False)).code,
        decide(snap(open_position_size=2)).code,
        decide(st=state(trades_today=2)).code,
        decide(snap(now=datetime(2026, 9, 16, 15, 45, 1, tzinfo=ET)), config=LATE_CUTOFF).code,
        decide(snap(now=datetime(2026, 9, 16, 12, 0, tzinfo=ET))).code,
        decide(snap(now=datetime(2026, 9, 16, 9, 0, tzinfo=ET))).code,
        decide(snap(session_start_balance=49_000.0)).code,
        decide(st=state(halted=True, halt_reason="x")).code,
    }
    assert len(codes) == 9


def test_kill_switch_outranks_other_refusals():
    d = decide(snap(kill_switch_active=True, open_position_size=2), state(trades_today=2))
    assert d.code == Reason.KILL_SWITCH


def test_tripwires_outrank_the_kill_switch():
    """A halt is strictly stronger than a refusal and must not be masked."""
    d = decide(snap(net_liq=49_700.0, kill_switch_active=True))
    assert d.action is Action.FLATTEN_AND_HALT
    assert d.code == Reason.DAILY_MAX_LOSS


# ---------------------------------------------------------------------------
# session_start_for -- the highest-risk logic in the module
# ---------------------------------------------------------------------------


def test_session_start_weekday_afternoon():
    """Wed 14:00 belongs to the session that opened Tue 18:00."""
    assert session_start_for(datetime(2026, 9, 16, 14, 0, tzinfo=ET)) == datetime(
        2026, 9, 15, 18, 0, tzinfo=ET
    )


def test_session_start_at_1730_is_still_the_previous_session():
    assert session_start_for(datetime(2026, 9, 16, 17, 30, tzinfo=ET)) == datetime(
        2026, 9, 15, 18, 0, tzinfo=ET
    )


def test_session_start_at_1830_is_the_new_session():
    assert session_start_for(datetime(2026, 9, 16, 18, 30, tzinfo=ET)) == datetime(
        2026, 9, 16, 18, 0, tzinfo=ET
    )


def test_session_start_boundary_is_inclusive_at_exactly_1800():
    assert session_start_for(datetime(2026, 9, 16, 18, 0, 0, tzinfo=ET)) == datetime(
        2026, 9, 16, 18, 0, tzinfo=ET
    )


def test_session_start_one_second_before_1800_is_the_old_session():
    assert session_start_for(datetime(2026, 9, 16, 17, 59, 59, tzinfo=ET)) == datetime(
        2026, 9, 15, 18, 0, tzinfo=ET
    )


def test_session_start_at_midnight_belongs_to_the_previous_evening():
    """The bug this guards: resetting daily accounting at 00:00 instead of 18:00."""
    assert session_start_for(datetime(2026, 9, 16, 0, 30, tzinfo=ET)) == datetime(
        2026, 9, 15, 18, 0, tzinfo=ET
    )


def test_session_start_exactly_at_midnight():
    assert session_start_for(datetime(2026, 9, 16, 0, 0, 0, tzinfo=ET)) == datetime(
        2026, 9, 15, 18, 0, tzinfo=ET
    )


def test_session_start_sunday_evening_reopen():
    """Globex reopens Sunday 18:00 ET; that instant starts Monday's session."""
    assert session_start_for(datetime(2026, 9, 13, 18, 30, tzinfo=ET)) == datetime(
        2026, 9, 13, 18, 0, tzinfo=ET
    )


def test_session_start_sunday_afternoon_before_the_reopen():
    assert session_start_for(datetime(2026, 9, 13, 17, 30, tzinfo=ET)) == datetime(
        2026, 9, 12, 18, 0, tzinfo=ET
    )


def test_session_start_accepts_a_utc_input_and_resolves_in_et():
    """22:00 UTC on 2026-09-16 is 18:00 EDT -- exactly the boundary."""
    utc = datetime(2026, 9, 16, 22, 0, tzinfo=UTC)
    assert session_start_for(utc) == datetime(2026, 9, 16, 18, 0, tzinfo=ET)


def test_session_start_accepts_a_non_et_zone():
    tokyo = datetime(2026, 9, 17, 7, 0, tzinfo=ZoneInfo("Asia/Tokyo"))  # 18:00 ET
    assert session_start_for(tokyo) == datetime(2026, 9, 16, 18, 0, tzinfo=ET)


def test_session_start_converts_to_et_before_bucketing_by_date():
    """A foreign zone whose calendar date is ahead of ET's must still bucket by
    the ET date.

    04:00 Tokyo on the 18th is 15:00 ET on the 17th, which belongs to the
    session that opened 18:00 ET on the 16th. Skipping the ET conversion and
    bucketing on the Tokyo date yields the 17th -- a full session out, which
    would reset daily accounting a day early. Milder cross-zone inputs happen
    to survive that bug, so this case is the one that pins it.
    """
    tokyo = datetime(2026, 9, 18, 4, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
    assert tokyo.astimezone(ET) == datetime(2026, 9, 17, 15, 0, tzinfo=ET)
    assert session_start_for(tokyo) == datetime(2026, 9, 16, 18, 0, tzinfo=ET)


def test_session_start_agrees_across_zones_for_the_same_instant():
    """The same instant expressed in four zones must yield one session."""
    instant = datetime(2026, 9, 17, 19, 0, tzinfo=UTC)  # 15:00 ET
    expected = datetime(2026, 9, 16, 18, 0, tzinfo=ET)
    for zone in ("UTC", "America/New_York", "Asia/Tokyo", "Europe/London"):
        assert session_start_for(instant.astimezone(ZoneInfo(zone))) == expected


def test_session_start_rejects_naive_datetimes():
    """A naive input must raise, never be read as the host's local time.

    ``astimezone`` on a naive datetime silently assumes local time, so on any
    host not set to ET this would shift every session boundary with no error.
    """
    with pytest.raises(ValueError, match="timezone-aware"):
        session_start_for(datetime(2026, 9, 16, 10, 0))


def test_derived_session_times_also_reject_naive_datetimes():
    for fn in (hard_flatten_at, entry_cutoff_at, rth_open_at):
        with pytest.raises(ValueError, match="timezone-aware"):
            fn(datetime(2026, 9, 16, 10, 0))


# -- DST transitions --------------------------------------------------------
# The bug being guarded: computing the previous boundary as `now - 24h`
# instead of stepping back one calendar date. Across a transition that lands
# on 17:00 or 19:00 local and shifts the entire session window.


def test_session_start_across_spring_forward_keeps_wall_clock_1800():
    """DST begins Sun 2026-03-08. A 10:00 EDT reading anchors to Sat 18:00 EST."""
    now = datetime(2026, 3, 8, 10, 0, tzinfo=ET)
    start = session_start_for(now)
    assert start == datetime(2026, 3, 7, 18, 0, tzinfo=ET)
    assert start.hour == 18, "a 24h subtraction would give 17:00"
    assert start.utcoffset() == timedelta(hours=-5)  # EST
    assert now.utcoffset() == timedelta(hours=-4)  # EDT
    # The session reads as 16 wall-clock hours but only 15 have elapsed.
    assert elapsed(now, start) == timedelta(hours=15)
    assert now - start == timedelta(hours=16), "plain subtraction is wall-clock"


def test_session_start_inside_the_spring_forward_night():
    now = datetime(2026, 3, 8, 1, 30, tzinfo=ET)  # still EST
    start = session_start_for(now)
    assert start == datetime(2026, 3, 7, 18, 0, tzinfo=ET)
    assert now - start == timedelta(hours=7, minutes=30)


def test_session_start_across_fall_back_keeps_wall_clock_1800():
    """DST ends Sun 2026-11-01. A 10:00 EST reading anchors to Sat 18:00 EDT."""
    now = datetime(2026, 11, 1, 10, 0, tzinfo=ET)
    start = session_start_for(now)
    assert start == datetime(2026, 10, 31, 18, 0, tzinfo=ET)
    assert start.hour == 18, "a 24h subtraction would give 19:00"
    assert start.utcoffset() == timedelta(hours=-4)  # EDT
    assert now.utcoffset() == timedelta(hours=-5)  # EST
    # The session reads as 16 wall-clock hours but 17 have elapsed.
    assert elapsed(now, start) == timedelta(hours=17)
    assert now - start == timedelta(hours=16), "plain subtraction is wall-clock"


def test_session_start_on_the_evening_of_a_dst_change_day():
    now = datetime(2026, 11, 1, 19, 0, tzinfo=ET)
    assert session_start_for(now) == datetime(2026, 11, 1, 18, 0, tzinfo=ET)


# -- derived session clock times --------------------------------------------


def test_hard_flatten_is_the_1555_inside_the_current_session():
    assert hard_flatten_at(datetime(2026, 9, 16, 10, 0, tzinfo=ET)) == datetime(
        2026, 9, 16, 15, 55, tzinfo=ET
    )


def test_hard_flatten_for_an_evening_timestamp_is_the_next_day():
    """19:00 Wed opens Thursday's session, so the flatten is Thu 15:55."""
    assert hard_flatten_at(datetime(2026, 9, 16, 19, 0, tzinfo=ET)) == datetime(
        2026, 9, 17, 15, 55, tzinfo=ET
    )


def test_entry_cutoff_and_rth_open_follow_the_same_session():
    evening = datetime(2026, 9, 16, 20, 0, tzinfo=ET)
    assert entry_cutoff_at(evening) == datetime(2026, 9, 17, 11, 30, tzinfo=ET)
    assert rth_open_at(evening) == datetime(2026, 9, 17, 9, 30, tzinfo=ET)


def test_session_clock_times_survive_the_spring_forward():
    """Session opened Sat 18:00 EST; its 15:55 is Sunday, in EDT."""
    now = datetime(2026, 3, 8, 10, 0, tzinfo=ET)
    flatten = hard_flatten_at(now)
    assert flatten == datetime(2026, 3, 8, 15, 55, tzinfo=ET)
    assert flatten.utcoffset() == timedelta(hours=-4)


# ---------------------------------------------------------------------------
# Pure state transitions
# ---------------------------------------------------------------------------


def test_roll_session_resets_the_trade_count_and_clears_a_halt():
    old = state(trades_today=2, halted=True, halt_reason="DAILY_MAX_LOSS: ...")
    old.session_start = datetime(2026, 9, 15, 18, 0, tzinfo=ET)
    s = snap(net_liq=50_320.0, now=datetime(2026, 9, 16, 18, 30, tzinfo=ET))

    rolled = roll_session(s, old, cfg())

    assert rolled.session_start == datetime(2026, 9, 16, 18, 0, tzinfo=ET)
    assert rolled.trades_today == 0
    assert rolled.halted is False
    assert rolled.halt_reason is None
    assert rolled.session_start_balance == 50_320.0
    assert rolled.last_reconcile == old.last_reconcile, "reconcile is process-scoped"


def test_roll_session_is_a_no_op_inside_the_same_session():
    st = state(trades_today=1, session_start=datetime(2026, 9, 15, 18, 0, tzinfo=ET))
    assert roll_session(snap(now=WED_1000), st, cfg()) is st


def test_roll_session_anchors_to_net_liq_not_balance():
    """Anchoring to balance would book open unrealised P&L as phantom session P&L."""
    s = snap(
        net_liq=49_800.0,
        balance=50_000.0,
        open_position_size=2,
        now=datetime(2026, 9, 16, 18, 30, tzinfo=ET),
    )
    rolled = roll_session(s, state(), cfg())
    assert rolled.session_start_balance == 49_800.0
    assert compute_session_pnl(s, rolled) == 0.0


def test_record_trade_increments_without_mutating():
    original = state()
    assert record_trade(original).trades_today == 1
    assert original.trades_today == 0


def test_apply_decision_records_a_halt_only_for_flatten():
    halt = decide(snap(net_liq=49_750.0))
    halted = apply_decision(state(), halt)
    assert halted.halted is True
    assert halted.halt_reason.startswith(Reason.DAILY_MAX_LOSS)

    ok = decide()
    assert apply_decision(state(), ok).halted is False


def test_halt_then_re_evaluate_keeps_refusing_after_recovery():
    """Once halted, a recovered net liq must not silently re-enable trading."""
    halted = apply_decision(state(), decide(snap(net_liq=49_750.0)))
    later = ev(snap(net_liq=50_100.0), halted)
    assert later.action is Action.REFUSE_ENTRY
    assert later.code == Reason.SESSION_HALTED


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_config_defaults_match_the_documented_risk_envelope():
    c = cfg()
    assert (c.daily_profit_target, c.daily_max_loss, c.floor_buffer) == (500.0, 250.0, 400.0)
    assert c.max_trades_per_session == 2
    assert (c.session_boundary_et, c.hard_flatten_et, c.entry_cutoff_et) == (
        time(18, 0),
        time(15, 55),
        time(11, 30),
    )


def test_config_from_env_reads_the_risk_envelope(monkeypatch):
    monkeypatch.setenv("DAILY_PROFIT_TARGET", "750")
    monkeypatch.setenv("DAILY_MAX_LOSS", "300")
    monkeypatch.setenv("FLOOR_BUFFER", "500")
    c = Config.from_env()
    assert (c.daily_profit_target, c.daily_max_loss, c.floor_buffer) == (750.0, 300.0, 500.0)


def test_config_from_env_falls_back_to_defaults_when_unset(monkeypatch):
    for key in ("DAILY_PROFIT_TARGET", "DAILY_MAX_LOSS", "FLOOR_BUFFER"):
        monkeypatch.delenv(key, raising=False)
    assert Config.from_env().daily_max_loss == 250.0


def test_tightening_the_loss_limit_fires_earlier():
    d = decide(snap(net_liq=49_900.0), config=cfg(daily_max_loss=100.0))
    assert d.action is Action.FLATTEN_AND_HALT
    assert d.code == Reason.DAILY_MAX_LOSS
