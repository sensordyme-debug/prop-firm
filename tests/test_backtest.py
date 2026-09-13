"""Tests for the backtest harness.

The harness is the instrument every later decision is measured with, so it is
proved on synthetic bars whose correct P&L can be worked out by hand and
asserted to the cent. A harness nobody calibrated produces confident numbers,
which is worse than no numbers.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from backtest import (
    Bar,
    BarConventionError,
    BarSeries,
    BarTimestamp,
    BarWindow,
    CostModel,
    LookaheadError,
    Signal,
    StrategyState,
    run_backtest,
)
from governor import ET, Config, Reason

# 2026-09-16 is a Wednesday and a regular session in the calendar.
DAY = date(2026, 9, 16)
POINT_VALUE = 2.00
COMMISSION = 1.82

# Costs with slippage switched off, so the arithmetic below is exact.
NO_SLIP = CostModel(slippage_ticks=0.0)


def t(hour: int, minute: int) -> datetime:
    return datetime(2026, 9, 16, hour, minute, tzinfo=ET)


def bar(hour, minute, o, h, lo, c) -> Bar:
    return Bar(ts=t(hour, minute), open=o, high=h, low=lo, close=c)


def flat_bars(n: int, start_hour: int = 9, start_minute: int = 35,
              price: float = 20_000.0, spread: float = 2.0) -> list[Bar]:
    """n featureless 5-minute bars. Uses timedelta so minutes cannot overflow."""
    first = t(start_hour, start_minute)
    return [
        Bar(ts=first + timedelta(minutes=5 * i), open=price, high=price + spread,
            low=price - spread, close=price)
        for i in range(n)
    ]


def series(bars, convention=BarTimestamp.CLOSE) -> BarSeries:
    return BarSeries(
        bars=tuple(bars), interval_minutes=5, timestamp_convention=convention
    )


class ScriptedStrategy:
    """Signals at named bar timestamps. Deterministic, so P&L is hand-checkable.

    Stop and target are set relative to the SIGNAL bar's close, which is all a
    real strategy could know at that moment.
    """

    def __init__(self, triggers, side=1, size=2, stop_offset=10.0, target_offset=20.0):
        self.triggers = set(triggers)
        self.side = side
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.calls: list[datetime] = []

    def __call__(self, bars: BarWindow, state: StrategyState, config) -> Signal | None:
        current = bars.current
        self.calls.append(current.ts)
        if current.ts not in self.triggers:
            return None
        if self.side == 1:
            stop = current.close - self.stop_offset
            target = current.close + self.target_offset
        else:
            stop = current.close + self.stop_offset
            target = current.close - self.target_offset
        return Signal(side=self.side, size=self.size, stop=stop, target=target,
                      reason="scripted")


# ===========================================================================
# No-lookahead
# ===========================================================================


def test_window_refuses_to_reveal_the_next_bar():
    """The deliberate peek. This is the whole no-lookahead guarantee."""
    bars = tuple(flat_bars(4, price=100.0, spread=1.0))
    window = BarWindow(bars, upto=1)

    assert len(window) == 2
    assert window[0] is bars[0]
    assert window.current is bars[1]

    with pytest.raises(LookaheadError, match="in the future"):
        window[2]
    with pytest.raises(LookaheadError):
        window[3]


def test_lookahead_error_is_an_index_error_so_iteration_still_works():
    """Iteration must terminate cleanly while a deliberate peek stays loud."""
    bars = tuple(flat_bars(4, price=100.0, spread=1.0))
    window = BarWindow(bars, upto=1)
    assert len(list(window)) == 2
    assert issubclass(LookaheadError, IndexError)


def test_negative_indexing_reaches_backwards_only():
    bars = tuple(flat_bars(4, price=100.0, spread=1.0))
    window = BarWindow(bars, upto=2)
    assert window[-1] is bars[2]
    assert window[-3] is bars[0]
    with pytest.raises(IndexError):
        window[-4]


def test_a_peeking_strategy_fails_the_backtest():
    """A strategy that tries to cheat must break, not silently succeed."""

    def cheater(bars: BarWindow, state, config):
        return bars[bars.current_index + 1]  # the next bar, which it must not see

    bars = flat_bars(6, price=100.0, spread=1.0)
    with pytest.raises(LookaheadError):
        run_backtest(series(bars), cheater)


def test_strategy_only_ever_sees_bars_up_to_the_current_one():
    strat = ScriptedStrategy(triggers=[])
    bars = flat_bars(6)
    run_backtest(series(bars), strat)
    assert strat.calls == [b.ts for b in bars]


# ===========================================================================
# Bar timestamp semantics -- the UNVERIFIED value, fail closed
# ===========================================================================


def test_unknown_timestamp_convention_refuses_to_run():
    """FIRM_RULES.md lists this UNVERIFIED; it must not be assumed."""
    with pytest.raises(BarConventionError, match="UNVERIFIED"):
        BarSeries(
            bars=(bar(9, 35, 100, 101, 99, 100),),
            interval_minutes=5,
            timestamp_convention=BarTimestamp.UNKNOWN,
        )


def test_close_labelled_bars_act_at_their_timestamp():
    s = series([bar(9, 35, 100, 101, 99, 100)], BarTimestamp.CLOSE)
    assert s.close_instant(0) == t(9, 35)


def test_open_labelled_bars_act_one_interval_later():
    """A one-bar labelling difference moves every opening range."""
    s = series([bar(9, 35, 100, 101, 99, 100)], BarTimestamp.OPEN)
    assert s.close_instant(0) == t(9, 35) + timedelta(minutes=5)


def test_bars_must_be_ordered():
    with pytest.raises(ValueError, match="strictly ordered"):
        series([bar(9, 40, 100, 101, 99, 100), bar(9, 35, 100, 101, 99, 100)])


def test_impossible_bars_are_rejected():
    with pytest.raises(ValueError, match="outside range"):
        Bar(ts=t(9, 35), open=105, high=101, low=99, close=100)
    with pytest.raises(ValueError, match="high"):
        Bar(ts=t(9, 35), open=100, high=98, low=99, close=100)


def test_naive_bar_timestamps_are_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        Bar(ts=datetime(2026, 9, 16, 9, 35), open=100, high=101, low=99, close=100)


# ===========================================================================
# The hand-calculated fixture
# ===========================================================================
#
# Two trades, 2 MNQ, $2.00/point, $1.82 round turn, slippage OFF.
#
# Trade 1 -- TARGET
#   signal  09:45 close 20,000.00  -> stop 19,990.00, target 20,020.00
#   entry   09:50 open  20,000.00
#   bar     high 20,025 >= target;  low 19,995 > stop  -> target fills
#   gross   (20,020 - 20,000) x 2 x $2.00 = 20 x 4 = $80.00
#   net     $80.00 - $1.82 = +$78.18
#
# Trade 2 -- STOP
#   signal  09:55 close 20,020.00  -> stop 20,010.00, target 20,040.00
#   entry   10:00 open  20,020.00
#   bar     low 20,005 <= stop;  high 20,025 < target -> stop fills
#   gross   (20,010 - 20,020) x 2 x $2.00 = -10 x 4 = -$40.00
#   net     -$40.00 - $1.82 = -$41.82
#
# TOTAL     +$78.18 - $41.82 = +$36.36


def hand_calculated_bars() -> list[Bar]:
    return [
        bar(9, 35, 19_990, 19_995, 19_985, 19_990),
        bar(9, 40, 19_990, 19_999, 19_988, 19_998),
        bar(9, 45, 19_998, 20_002, 19_996, 20_000),   # signal 1
        bar(9, 50, 20_000, 20_025, 19_995, 20_020),   # entry 1 -> TARGET
        bar(9, 55, 20_020, 20_022, 20_018, 20_020),   # signal 2
        bar(10, 0, 20_020, 20_025, 20_005, 20_010),   # entry 2 -> STOP
        bar(10, 5, 20_010, 20_012, 20_008, 20_010),
    ]


def run_fixture(**kw):
    strat = ScriptedStrategy(triggers=[t(9, 45), t(9, 55)])
    return run_backtest(series(hand_calculated_bars()), strat, costs=NO_SLIP, **kw)


def test_fixture_takes_exactly_two_trades():
    result = run_fixture()
    assert len(result.trades) == 2
    assert [x.exit_kind for x in result.trades] == ["TARGET", "STOP"]


def test_first_trade_pnl_to_the_cent():
    first = run_fixture().trades[0]
    assert first.entry_price == 20_000.00
    assert first.exit_price == 20_020.00
    assert first.gross == pytest.approx(80.00)
    assert first.commission == pytest.approx(1.82)
    assert first.net == pytest.approx(78.18)


def test_second_trade_pnl_to_the_cent():
    second = run_fixture().trades[1]
    assert second.entry_price == 20_020.00
    assert second.exit_price == 20_010.00
    assert second.gross == pytest.approx(-40.00)
    assert second.net == pytest.approx(-41.82)


def test_total_net_profit_to_the_cent():
    assert run_fixture().net_profit == pytest.approx(36.36)


def test_expectancy_to_the_cent():
    assert run_fixture().expectancy == pytest.approx(36.36 / 2)


def test_commission_is_charged_once_per_round_turn():
    result = run_fixture()
    assert sum(x.commission for x in result.trades) == pytest.approx(2 * COMMISSION)


def test_entry_fills_at_the_next_bar_open_not_the_signal_close():
    """The signal bar closed at 20,000; entry is the NEXT bar's open."""
    result = run_fixture()
    assert result.trades[0].entry_price == 20_000.00  # 09:50 open
    assert result.trades[0].entry_time == t(9, 50)


# ===========================================================================
# Bracket semantics: the stop always wins a tie
# ===========================================================================


def test_a_bar_touching_both_stop_and_target_fills_the_stop():
    """The honest reading at bar resolution, and the conservative one."""
    bars = [
        bar(9, 35, 20_000, 20_001, 19_999, 20_000),
        bar(9, 45, 19_998, 20_002, 19_996, 20_000),      # signal
        bar(9, 50, 20_000, 20_030, 19_985, 20_000),      # BOTH touched
        bar(9, 55, 20_000, 20_001, 19_999, 20_000),
    ]
    strat = ScriptedStrategy(triggers=[t(9, 45)])
    result = run_backtest(series(bars), strat, costs=NO_SLIP)

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_kind == "STOP", "a tie must never be resolved in our favour"
    assert trade.exit_price == 19_990.00
    assert trade.net == pytest.approx(-41.82)


def test_the_same_bar_would_have_been_a_winner_under_a_dishonest_model():
    """Documents what the conservative choice costs, so it stays a choice."""
    optimistic = (20_020.0 - 20_000.0) * 2 * POINT_VALUE - COMMISSION
    assert optimistic == pytest.approx(78.18)
    honest = run_backtest(
        series([
            bar(9, 35, 20_000, 20_001, 19_999, 20_000),
            bar(9, 45, 19_998, 20_002, 19_996, 20_000),
            bar(9, 50, 20_000, 20_030, 19_985, 20_000),
            bar(9, 55, 20_000, 20_001, 19_999, 20_000),
        ]),
        ScriptedStrategy(triggers=[t(9, 45)]),
        costs=NO_SLIP,
    ).net_profit
    assert honest == pytest.approx(-41.82)
    assert honest < optimistic


def test_short_trades_price_correctly():
    """Short: profit when price falls. Target below entry, stop above."""
    bars = [
        bar(9, 35, 20_000, 20_001, 19_999, 20_000),
        bar(9, 45, 20_000, 20_002, 19_998, 20_000),      # signal short
        bar(9, 50, 20_000, 20_005, 19_975, 19_980),      # target 19,980 hit
        bar(9, 55, 19_980, 19_982, 19_978, 19_980),
    ]
    strat = ScriptedStrategy(triggers=[t(9, 45)], side=-1)
    result = run_backtest(series(bars), strat, costs=NO_SLIP)
    trade = result.trades[0]
    assert trade.side == -1
    assert trade.exit_kind == "TARGET"
    assert trade.exit_price == 19_980.00
    assert trade.gross == pytest.approx((20_000 - 19_980) * 2 * POINT_VALUE)
    assert trade.net == pytest.approx(80.00 - COMMISSION)


# ===========================================================================
# Slippage
# ===========================================================================


def test_slippage_is_adverse_on_entry_and_on_the_stop():
    """2 ticks = 0.5 pts. Long entry fills higher, stop fills lower."""
    bars = hand_calculated_bars()
    strat = ScriptedStrategy(triggers=[t(9, 45), t(9, 55)])
    result = run_backtest(series(bars), strat, costs=CostModel(slippage_ticks=2.0))

    first, second = result.trades
    assert first.entry_price == pytest.approx(20_000.50), "paid up to get in"
    assert first.exit_price == pytest.approx(20_020.00), "limit never improves"
    assert second.exit_price == pytest.approx(20_009.50), "stop slipped through"


def test_slippage_never_helps():
    with_slip = run_backtest(
        series(hand_calculated_bars()),
        ScriptedStrategy(triggers=[t(9, 45), t(9, 55)]),
        costs=CostModel(slippage_ticks=2.0),
    ).net_profit
    assert with_slip < run_fixture().net_profit


def test_negative_slippage_is_rejected():
    with pytest.raises(ValueError, match="never a gift"):
        CostModel(slippage_ticks=-1.0)


def test_default_cost_model_is_pessimistic_not_frictionless():
    costs = CostModel()
    assert costs.commission_per_round_turn == pytest.approx(1.82)
    assert costs.slippage_ticks > 0, "a frictionless default would flatter every run"
    assert costs.point_value == pytest.approx(2.00)


# ===========================================================================
# The governor really is in the path
# ===========================================================================


def test_governor_halts_are_recorded_with_reasons():
    """Drive net liq into the MLL buffer and the real trip-wire must fire."""
    bars = [
        bar(9, 35, 20_000, 20_001, 19_999, 20_000),
        bar(9, 45, 19_998, 20_002, 19_996, 20_000),
        bar(9, 50, 20_000, 20_001, 19_000, 19_100),   # catastrophic move
        bar(9, 55, 19_100, 19_101, 19_099, 19_100),
    ]
    strat = ScriptedStrategy(triggers=[t(9, 45)], stop_offset=5_000.0,
                             target_offset=9_000.0)
    result = run_backtest(series(bars), strat, costs=NO_SLIP)

    assert result.halts, "a 1,800-point adverse move must trip the governor"
    codes = {h.code for h in result.halts}
    assert codes & {Reason.MLL_FLOOR_BUFFER, Reason.DAILY_MAX_LOSS}


def test_governor_flattens_an_open_position_on_halt():
    bars = [
        bar(9, 35, 20_000, 20_001, 19_999, 20_000),
        bar(9, 45, 19_998, 20_002, 19_996, 20_000),
        bar(9, 50, 20_000, 20_001, 19_800, 19_850),   # -150 pts, -$600 on 2
        bar(9, 55, 19_850, 19_851, 19_849, 19_850),
    ]
    strat = ScriptedStrategy(triggers=[t(9, 45)], stop_offset=5_000.0,
                             target_offset=9_000.0)
    result = run_backtest(series(bars), strat, costs=NO_SLIP)
    assert any(x.exit_kind == "FLATTEN" for x in result.trades)


def test_trade_budget_is_enforced_by_the_real_governor():
    """A strategy signalling every bar still gets only max_trades_per_session."""
    bars = flat_bars(12)
    strat = ScriptedStrategy(triggers=[b.ts for b in bars],
                             stop_offset=500.0, target_offset=900.0)
    result = run_backtest(series(bars), strat)
    assert len(result.trades) <= Config().max_trades_per_session


def test_no_entry_is_taken_on_a_closed_day():
    """Saturday: the calendar refuses, so a signalling strategy takes nothing."""
    sat = datetime(2026, 9, 19, 10, 0, tzinfo=ET)
    bars = [
        Bar(ts=sat + timedelta(minutes=5 * i), open=20_000, high=20_002,
            low=19_998, close=20_000)
        for i in range(6)
    ]
    strat = ScriptedStrategy(triggers=[b.ts for b in bars])
    result = run_backtest(series(bars), strat)
    assert result.trades == ()


def test_no_entry_is_taken_on_a_holiday_half_day():
    """DECISIONS.md: we stand aside on holiday dates, half-days included."""
    half = datetime(2026, 11, 27, 10, 0, tzinfo=ET)
    bars = [
        Bar(ts=half + timedelta(minutes=5 * i), open=20_000, high=20_002,
            low=19_998, close=20_000)
        for i in range(6)
    ]
    strat = ScriptedStrategy(triggers=[b.ts for b in bars])
    result = run_backtest(series(bars), strat)
    assert result.trades == ()


def test_no_harness_faults_in_a_normal_run():
    """STALE_SESSION_ANCHOR or MLL_STATE_UNAVAILABLE would mean a broken harness."""
    assert run_fixture().harness_faults == ()


# ===========================================================================
# Metrics and gates
# ===========================================================================


def test_drawdown_is_measured_on_net_liq_including_open_positions():
    """An open position that went $400 against us already happened."""
    bars = [
        bar(9, 35, 20_000, 20_001, 19_999, 20_000),
        bar(9, 45, 19_998, 20_002, 19_996, 20_000),   # signal: stop 19990, tgt 20020
        bar(9, 50, 20_000, 20_005, 19_995, 19_996),   # open, marked -4 pts = -$16
        bar(9, 55, 19_996, 20_025, 19_995, 20_020),   # then the target fills
    ]
    strat = ScriptedStrategy(triggers=[t(9, 45)])
    result = run_backtest(series(bars), strat, costs=NO_SLIP)

    assert result.trades[0].exit_kind == "TARGET"
    assert result.net_profit == pytest.approx(78.18), "it ended a winner"
    # ... but the excursion while open still happened and must be visible.
    assert result.max_drawdown == pytest.approx(16.00)


def test_drawdown_is_measured_from_the_running_peak_not_the_opening_balance():
    """Giving back an unrealised gain is a drawdown, even if you end up ahead.

    The fixture wins $78.18 and then loses $41.82, finishing +$36.36 -- above
    where it started. Measuring from the opening balance would report zero
    drawdown and hide the giveback entirely. The MLL trails a high-water mark,
    so this is exactly the figure that matters.
    """
    result = run_fixture()
    assert result.net_profit == pytest.approx(36.36), "it finished ahead"
    assert result.max_drawdown == pytest.approx(41.82), (
        "peak 50,078.18 down to 50,036.36 -- measured from the peak, not the open"
    )


def test_daily_pnl_and_losing_day_counting():
    result = run_fixture()
    assert dict(result.daily_pnl)[DAY] == pytest.approx(36.36)
    assert result.max_consecutive_losing_days == 0


def test_gates_fail_on_too_few_trades_even_when_profitable():
    """Two profitable trades is not evidence; the gate must say so."""
    gates = run_fixture().passes_gates()
    assert gates.expectancy_ok is False, "200+ trades required"
    assert gates.passed is False
    assert "200" in gates.detail["expectancy"]


def test_gates_pass_each_criterion_independently():
    result = run_fixture()
    gates = result.passes_gates()
    assert gates.drawdown_ok is True
    assert gates.consecutive_losses_ok is True
    assert gates.no_strategy_halts_ok is True


def test_gates_are_mechanical_not_a_judgement_call():
    """passes_gates must not consult net profit at all."""
    gates = run_fixture().passes_gates()
    assert isinstance(gates.passed, bool)
    assert "profit" not in " ".join(gates.detail).lower()


# ===========================================================================
# The report
# ===========================================================================


def test_report_prints_assumptions_and_puts_profit_last():
    report = run_fixture().report()
    assert "ASSUMPTIONS" in report
    assert "1.82" in report
    assert "STOP assumed first, ALWAYS" in report
    assert "next bar's OPEN" in report
    assert "CLOSE-labelled" in report

    # Risk before profit, and profit last of all.
    assert report.index("max drawdown") < report.index("Net profit")
    assert report.strip().splitlines()[-2].startswith("Net profit:")


def test_report_states_the_gate_verdict():
    report = run_fixture().report()
    assert "STAGE 4 GATES" in report
    assert "OVERALL: FAIL" in report  # only two trades


def test_report_lists_halts_with_reasons():
    bars = [
        bar(9, 35, 20_000, 20_001, 19_999, 20_000),
        bar(9, 45, 19_998, 20_002, 19_996, 20_000),
        bar(9, 50, 20_000, 20_001, 19_000, 19_100),
        bar(9, 55, 19_100, 19_101, 19_099, 19_100),
    ]
    strat = ScriptedStrategy(triggers=[t(9, 45)], stop_offset=5_000.0,
                             target_offset=9_000.0)
    report = run_backtest(series(bars), strat, costs=NO_SLIP).report()
    assert "GOVERNOR HALTS" in report
    assert "MLL_FLOOR_BUFFER" in report or "DAILY_MAX_LOSS" in report


def test_empty_run_reports_cleanly():
    bars = flat_bars(4)
    result = run_backtest(series(bars), ScriptedStrategy(triggers=[]))
    assert result.net_profit == 0.0
    assert result.max_drawdown == 0.0
    assert result.expectancy == 0.0
    assert "Net profit: $0.00" in result.report()
