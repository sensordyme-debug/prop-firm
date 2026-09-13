"""Backtest harness: replay bars through the REAL governor.

WHY OUR OWN HARNESS
-------------------
DECISIONS.md rejected LEAN twice, decisively on this ground: its bars come from
a different vendor with its own tick aggregation, bar labelling and session
filtering. For a strategy whose signal is the high and low of the first three
5-minute bars, a one-bar labelling difference moves the range and changes every
trade. A backtest measuring a subtly different thing is worse than no backtest,
because it produces confidence instead of doubt.

The other half of the reason is this module's design centre: **the governor runs
in the replay path**. Same ``evaluate()``, same trip-wires, same 18:00 ET session
roll, same market calendar, same MLL reconstruction. "Would the governor have
halted me that day?" is therefore an answerable question, and a strategy that
only looks profitable because it ignored its own risk limits cannot hide here.

WHAT THIS HARNESS ASSUMES, AND WHY IT IS PESSIMISTIC
----------------------------------------------------
Fills are what backtests lie about most, so every ambiguity resolves against us:

  * **Entry fills at the NEXT bar's open**, not the signal bar's close. You
    cannot trade at a price you have only just finished observing.
  * **Slippage is adverse on entry and on stops**, never favourable.
  * **Targets fill at the limit price exactly** — a limit order never improves.
  * **If a bar's range touched both the stop and the target, the STOP filled
    first. Always.** At bar resolution the true order is unknowable, and this
    is both the honest reading and the conservative one. Assuming otherwise
    manufactures winners out of ignorance.

Every one of these is printed in the report, because an assumption nobody reads
is an assumption nobody checks.

STRATEGY PURITY
---------------
A strategy is ``(bars, state, config) -> Signal | None`` — pure, exactly like
the governor. No I/O, no clock, no SDK. It sees a :class:`BarWindow` that
physically cannot reach past the current bar: peeking raises
:class:`LookaheadError`. One implementation then drives both this replay loop
and the live loop later, so what is tested is what trades.

BAR TIMESTAMP SEMANTICS -- FAIL CLOSED
--------------------------------------
FIRM_RULES.md lists this as UNVERIFIED: we do not yet know whether ProjectX
labels a bar with its opening or closing instant, nor whether times are
exchange-local or UTC. It decides which bars form an opening range, so a wrong
guess shifts every signal by one bar.

:class:`BarSeries` therefore REQUIRES an explicit convention and refuses to run
with ``BarTimestamp.UNKNOWN``. Confirm it from real data in Stage 1 before any
result from this harness means anything.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Final, Protocol

from governor import (
    AccountSnapshot,
    Action,
    Config,
    GovernorDecision,
    GovernorState,
    Reason,
    apply_decision,
    evaluate,
    record_trade,
    roll_session,
    session_start_for,
    session_trading_date,
)
from mll_tracker import MllState, floor_for, record_eod, seed_state

__all__ = [
    "BacktestResult",
    "Bar",
    "BarConventionError",
    "BarSeries",
    "BarTimestamp",
    "BarWindow",
    "CostModel",
    "GateReport",
    "LookaheadError",
    "Signal",
    "Strategy",
    "StrategyState",
    "Trade",
    "run_backtest",
]

# FIRM_RULES.md, contract specs.
MNQ_POINT_VALUE: Final[float] = 2.00
MNQ_TICK_SIZE: Final[float] = 0.25
MNQ_COMMISSION_ROUND_TURN: Final[float] = 1.82

# ROADMAP.md Stage 4 acceptance gates.
GATE_MAX_DRAWDOWN: Final[float] = 1_200.0
GATE_MAX_CONSECUTIVE_LOSING_DAYS: Final[int] = 5
GATE_MIN_TRADES: Final[int] = 200

# Halts the STRATEGY caused by its own behaviour, as opposed to the clock or
# the calendar. Hitting the profit target is the strategy succeeding, not
# failing, so it is deliberately not in this set.
STRATEGY_CAUSED_HALTS: Final[frozenset[str]] = frozenset(
    {Reason.MLL_FLOOR_BUFFER, Reason.DAILY_MAX_LOSS}
)

# If these fire, the HARNESS is broken, not the strategy. They must never be
# silently folded into the strategy's score.
HARNESS_FAULT_HALTS: Final[frozenset[str]] = frozenset(
    {Reason.STALE_SESSION_ANCHOR, Reason.MLL_STATE_UNAVAILABLE}
)


class LookaheadError(IndexError):
    """Raised when a strategy reaches past the current bar.

    Subclasses ``IndexError`` so ordinary iteration still terminates cleanly,
    while a deliberate peek is still identifiable and loud.
    """


class BarConventionError(RuntimeError):
    """Raised when bar timestamp semantics are unknown. See the module docstring."""


class BarTimestamp(str, Enum):
    """What a bar's timestamp labels."""

    OPEN = "OPEN"      # ts is the instant the bar opened
    CLOSE = "CLOSE"    # ts is the instant the bar closed
    UNKNOWN = "UNKNOWN"  # not yet confirmed -- refuses to run


@dataclass(frozen=True)
class Bar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int = 0

    def __post_init__(self) -> None:
        if self.ts.tzinfo is None or self.ts.utcoffset() is None:
            raise ValueError("Bar.ts must be timezone-aware")
        if self.high < self.low:
            raise ValueError(f"Bar at {self.ts}: high {self.high} < low {self.low}")
        for name, price in (("open", self.open), ("close", self.close)):
            if not self.low <= price <= self.high:
                raise ValueError(
                    f"Bar at {self.ts}: {name} {price} outside range "
                    f"[{self.low}, {self.high}]"
                )


@dataclass(frozen=True)
class BarSeries:
    """Bars plus the two facts needed to interpret them."""

    bars: tuple[Bar, ...]
    interval_minutes: int
    timestamp_convention: BarTimestamp

    def __post_init__(self) -> None:
        if self.timestamp_convention is BarTimestamp.UNKNOWN:
            raise BarConventionError(
                "Bar timestamp semantics are UNVERIFIED (FIRM_RULES.md). Whether "
                "`t` labels the bar's open or its close decides which bars form "
                "an opening range, so a wrong guess shifts every signal by one "
                "bar. Confirm it from real data (ROADMAP Stage 1) and pass "
                "BarTimestamp.OPEN or BarTimestamp.CLOSE explicitly."
            )
        if self.interval_minutes <= 0:
            raise ValueError("interval_minutes must be positive")
        for earlier, later in zip(self.bars, self.bars[1:]):
            if later.ts <= earlier.ts:
                raise ValueError(
                    f"bars must be strictly ordered: {later.ts} follows {earlier.ts}"
                )

    def close_instant(self, index: int) -> datetime:
        """The instant bar ``index`` finished -- the earliest you could act on it."""
        bar = self.bars[index]
        if self.timestamp_convention is BarTimestamp.CLOSE:
            return bar.ts
        return bar.ts + timedelta(minutes=self.interval_minutes)

    def __len__(self) -> int:
        return len(self.bars)


class BarWindow(Sequence[Bar]):
    """A read-only view of bars up to and including ``upto``.

    The no-lookahead guarantee is structural rather than advisory: the strategy
    is handed this object, never the full series, and any index past ``upto``
    raises :class:`LookaheadError`.
    """

    __slots__ = ("_bars", "_upto")

    def __init__(self, bars: tuple[Bar, ...], upto: int) -> None:
        self._bars = bars
        self._upto = upto

    def __len__(self) -> int:
        return self._upto + 1

    def __getitem__(self, index):  # type: ignore[override]
        if isinstance(index, slice):
            return list(self)[index]
        resolved = index if index >= 0 else len(self) + index
        if resolved > self._upto:
            raise LookaheadError(
                f"bar {index} is in the future: this window ends at index "
                f"{self._upto} ({self._bars[self._upto].ts}). A strategy that "
                "can see the next bar is not a strategy, it is a time machine."
            )
        if resolved < 0:
            raise IndexError(index)
        return self._bars[resolved]

    def __iter__(self) -> Iterator[Bar]:
        for i in range(len(self)):
            yield self._bars[i]

    @property
    def current(self) -> Bar:
        """The bar that has just closed."""
        return self._bars[self._upto]

    @property
    def current_index(self) -> int:
        """Position of the current bar.

        Deliberately NOT called ``index``: this subclasses ``Sequence``, whose
        ``index(value)`` searches for a value. Shadowing it would silently
        change the meaning of a standard method.
        """
        return self._upto


@dataclass(frozen=True)
class Signal:
    """A proposal. The governor still decides whether it is allowed."""

    side: int          # +1 long, -1 short
    size: int
    stop: float        # absolute price
    target: float      # absolute price
    reason: str = ""

    def __post_init__(self) -> None:
        if self.side not in (1, -1):
            raise ValueError(f"Signal.side must be +1 or -1, got {self.side}")
        if self.size <= 0:
            raise ValueError(f"Signal.size must be positive, got {self.size}")
        if self.side == 1 and not self.stop < self.target:
            raise ValueError(
                f"long signal needs stop {self.stop} below target {self.target}"
            )
        if self.side == -1 and not self.target < self.stop:
            raise ValueError(
                f"short signal needs target {self.target} below stop {self.stop}"
            )


@dataclass(frozen=True)
class StrategyState:
    """Driver-maintained facts a strategy may READ but never mutate.

    Anything derivable from the bars (an opening range, an ATR) is recomputed
    by the strategy from its window; that is what keeps it a pure function of
    its arguments rather than an object with hidden history.
    """

    session_start: datetime
    trading_date: date
    entries_this_session: int = 0
    position_side: int = 0          # 0 flat, +1 long, -1 short
    long_taken: bool = False
    short_taken: bool = False


class Strategy(Protocol):
    def __call__(
        self, bars: BarWindow, state: StrategyState, config: object
    ) -> Signal | None: ...


@dataclass(frozen=True)
class CostModel:
    """Commission and slippage. Defaults are deliberately pessimistic."""

    commission_per_round_turn: float = MNQ_COMMISSION_ROUND_TURN
    slippage_ticks: float = 2.0
    tick_size: float = MNQ_TICK_SIZE
    point_value: float = MNQ_POINT_VALUE

    def __post_init__(self) -> None:
        if self.commission_per_round_turn < 0:
            raise ValueError("commission cannot be negative")
        if self.slippage_ticks < 0:
            raise ValueError("slippage cannot be negative (it is never a gift)")
        if self.tick_size <= 0 or self.point_value <= 0:
            raise ValueError("tick_size and point_value must be positive")

    @property
    def slippage_points(self) -> float:
        return self.slippage_ticks * self.tick_size

    def dollars(self, points: float, size: int) -> float:
        return points * size * self.point_value

    def describe(self) -> list[str]:
        slip_dollars = self.slippage_points * self.point_value
        return [
            f"commission      ${self.commission_per_round_turn:.2f} per round turn",
            f"slippage        {self.slippage_ticks:g} ticks "
            f"({self.slippage_points:g} pts, ${slip_dollars:.2f}/contract) "
            f"adverse on entry and stop, never favourable",
            "targets         fill at the limit exactly, never improved",
            f"point value     ${self.point_value:.2f} per point",
        ]


@dataclass(frozen=True)
class Trade:
    entry_time: datetime
    exit_time: datetime
    trading_date: date
    side: int
    size: int
    entry_price: float
    exit_price: float
    exit_kind: str          # TARGET | STOP | FLATTEN | END_OF_DATA
    gross: float
    commission: float
    mae: float = 0.0        # Maximum Adverse Excursion, dollars, >= 0
    mfe: float = 0.0        # Maximum Favourable Excursion, dollars, >= 0

    @property
    def net(self) -> float:
        return self.gross - self.commission

    @property
    def edge_ratio(self) -> float | None:
        """MFE / MAE. High means the trade went our way before it went wrong.

        Useful for judging whether stops are too tight or targets too far,
        WITHOUT re-running anything. Deliberately not used to auto-tune: fitting
        stops to observed excursions is fitting to one sample.
        """
        return None if self.mae <= 0 else self.mfe / self.mae

    @property
    def is_win(self) -> bool:
        return self.net > 0


@dataclass(frozen=True)
class Halt:
    when: datetime
    trading_date: date
    code: str
    reason: str


@dataclass(frozen=True)
class GateReport:
    """Mechanical pass/fail against ROADMAP Stage 4. No judgement calls."""

    drawdown_ok: bool
    consecutive_losses_ok: bool
    expectancy_ok: bool
    no_strategy_halts_ok: bool
    detail: dict[str, str] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return all(
            (
                self.drawdown_ok,
                self.consecutive_losses_ok,
                self.expectancy_ok,
                self.no_strategy_halts_ok,
            )
        )


@dataclass(frozen=True)
class BacktestResult:
    trades: tuple[Trade, ...]
    halts: tuple[Halt, ...]
    equity_curve: tuple[tuple[datetime, float], ...]
    daily_pnl: tuple[tuple[date, float], ...]
    starting_balance: float
    costs: CostModel
    timestamp_convention: BarTimestamp
    bars_seen: int

    # -- risk first -------------------------------------------------------

    @property
    def max_drawdown(self) -> float:
        """Peak-to-trough on NET LIQ, including open positions.

        Marked to market rather than measured on closed trades, because that is
        how the firm enforces the MLL: an open position that goes $900 against
        us has already happened, whether or not we later close it green.
        """
        peak = self.starting_balance
        worst = 0.0
        for _, equity in self.equity_curve:
            peak = max(peak, equity)
            worst = max(worst, peak - equity)
        return worst

    @property
    def max_consecutive_losing_days(self) -> int:
        run = worst = 0
        for _, pnl in self.daily_pnl:
            if pnl < 0:
                run += 1
                worst = max(worst, run)
            else:
                run = 0
        return worst

    @property
    def expectancy(self) -> float:
        if not self.trades:
            return 0.0
        return sum(t.net for t in self.trades) / len(self.trades)

    @property
    def strategy_caused_halts(self) -> tuple[Halt, ...]:
        return tuple(h for h in self.halts if h.code in STRATEGY_CAUSED_HALTS)

    @property
    def harness_faults(self) -> tuple[Halt, ...]:
        return tuple(h for h in self.halts if h.code in HARNESS_FAULT_HALTS)

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        return sum(1 for t in self.trades if t.is_win) / len(self.trades)

    # -- profit, last -----------------------------------------------------

    @property
    def net_profit(self) -> float:
        return sum(t.net for t in self.trades)

    def passes_gates(self) -> GateReport:
        """Hard pass/fail against the four Stage 4 criteria.

        Mechanical on purpose. The failure mode this guards against is reading
        a profit number first and then deciding how strict to be about
        drawdown.
        """
        dd = self.max_drawdown
        losing = self.max_consecutive_losing_days
        n = len(self.trades)
        exp = self.expectancy
        strategy_halts = self.strategy_caused_halts

        return GateReport(
            drawdown_ok=dd < GATE_MAX_DRAWDOWN,
            consecutive_losses_ok=losing < GATE_MAX_CONSECUTIVE_LOSING_DAYS,
            expectancy_ok=exp > 0 and n >= GATE_MIN_TRADES,
            no_strategy_halts_ok=not strategy_halts,
            detail={
                "drawdown": f"${dd:,.2f} (limit ${GATE_MAX_DRAWDOWN:,.0f})",
                "consecutive_losing_days": (
                    f"{losing} (limit {GATE_MAX_CONSECUTIVE_LOSING_DAYS})"
                ),
                "expectancy": (
                    f"${exp:,.2f} over {n} trades "
                    f"(need > $0 over {GATE_MIN_TRADES}+)"
                ),
                "strategy_halts": (
                    f"{len(strategy_halts)} "
                    f"({', '.join(h.code for h in strategy_halts) or 'none'})"
                ),
            },
        )

    def report(self) -> str:
        """Drawdown-first report. Net profit is printed LAST, deliberately."""
        gates = self.passes_gates()
        lines: list[str] = []
        add = lines.append

        add("=" * 70)
        add("BACKTEST REPORT")
        add("=" * 70)

        add("")
        add("ASSUMPTIONS  (fills are what backtests lie about most)")
        for line in self.costs.describe():
            add(f"  {line}")
        add("  entry fill      next bar's OPEN, never the signal bar's close")
        add("  both touched    STOP assumed first, ALWAYS")
        add(f"  bar timestamps  {self.timestamp_convention.value}-labelled")
        add(f"  bars replayed   {self.bars_seen}")

        add("")
        add("RISK  (judged first)")
        add(f"  max drawdown                ${self.max_drawdown:,.2f}")
        add(f"  max consecutive losing days {self.max_consecutive_losing_days}")
        add(f"  trades                      {len(self.trades)}")
        add(f"  win rate                    {self.win_rate * 100:.1f}%")
        add(f"  expectancy per trade        ${self.expectancy:,.2f}")

        add("")
        add(f"GOVERNOR HALTS  ({len(self.halts)})")
        if not self.halts:
            add("  none")
        for halt in self.halts:
            add(f"  {halt.trading_date}  {halt.code}")
            add(f"      {halt.reason}")
        if self.harness_faults:
            add("")
            add("  *** HARNESS FAULTS -- these indicate a broken harness, not a")
            add("      broken strategy. The run is not trustworthy. ***")
            for halt in self.harness_faults:
                add(f"      {halt.code}: {halt.reason}")

        add("")
        add("STAGE 4 GATES  (ROADMAP.md)")
        for name, ok in (
            ("max drawdown", gates.drawdown_ok),
            ("consecutive losing days", gates.consecutive_losses_ok),
            ("expectancy over 200+ trades", gates.expectancy_ok),
            ("zero strategy-caused halts", gates.no_strategy_halts_ok),
        ):
            key = name.split()[0] if name != "consecutive losing days" else "consecutive_losing_days"
            key = {
                "max": "drawdown",
                "consecutive_losing_days": "consecutive_losing_days",
                "expectancy": "expectancy",
                "zero": "strategy_halts",
            }.get(key, key)
            add(f"  [{'PASS' if ok else 'FAIL'}]  {name:<30s} {gates.detail.get(key, '')}")
        add("")
        add(f"  OVERALL: {'PASS' if gates.passed else 'FAIL'}")
        if not gates.passed:
            add("  Reject regardless of net return. A strategy making $8,000 a year")
            add("  that draws down $1,900 will kill this account before it pays out.")

        add("")
        add("-" * 70)
        add(f"Net profit: ${self.net_profit:,.2f}")
        add("-" * 70)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# The replay loop
# ---------------------------------------------------------------------------


@dataclass
class _Position:
    side: int
    size: int
    entry_price: float
    entry_time: datetime
    stop: float
    target: float
    trading_date: date
    worst_price: float = 0.0   # furthest against us while open
    best_price: float = 0.0    # furthest in our favour while open

    def observe(self, bar: Bar) -> None:
        """Track excursion extremes, including the entry bar itself."""
        if self.side == 1:
            self.worst_price = min(self.worst_price, bar.low)
            self.best_price = max(self.best_price, bar.high)
        else:
            self.worst_price = max(self.worst_price, bar.high)
            self.best_price = min(self.best_price, bar.low)


def _unrealised(position: _Position | None, mark: float, costs: CostModel) -> float:
    if position is None:
        return 0.0
    return costs.dollars((mark - position.entry_price) * position.side, position.size)


def run_backtest(
    series: BarSeries,
    strategy: Strategy,
    *,
    config: Config | None = None,
    strategy_config: object = None,
    costs: CostModel | None = None,
    starting_balance: float = 50_000.0,
    mll_state: MllState | None = None,
) -> BacktestResult:
    """Replay ``series`` through ``strategy`` with the real governor in the path.

    Ordering within each bar is what keeps the run honest:

      A. fill any entry signalled on the PREVIOUS bar, at this bar's open
      B. resolve bracket exits against this bar's range (stop wins ties)
      C. mark to market at the close and extend the equity curve
      D. roll the session if the 18:00 ET boundary was crossed
      E. ask the governor, at this bar's closing instant
      F. flatten if it halted
      G. otherwise offer the strategy a window ending at THIS bar
    """
    cfg = config or Config()
    cost_model = costs or CostModel()
    mll = mll_state or seed_state(starting_balance=starting_balance)

    balance = starting_balance
    position: _Position | None = None
    pending: Signal | None = None

    trades: list[Trade] = []
    halts: list[Halt] = []
    equity_curve: list[tuple[datetime, float]] = []
    daily: dict[date, float] = {}

    gov_state: GovernorState | None = None
    strat_state: StrategyState | None = None

    def close_position(
        at_price: float, when: datetime, kind: str
    ) -> None:
        nonlocal position, balance
        assert position is not None
        gross = cost_model.dollars(
            (at_price - position.entry_price) * position.side, position.size
        )
        adverse = (position.entry_price - position.worst_price) * position.side
        favourable = (position.best_price - position.entry_price) * position.side
        trade = Trade(
            entry_time=position.entry_time,
            exit_time=when,
            trading_date=position.trading_date,
            side=position.side,
            size=position.size,
            entry_price=position.entry_price,
            exit_price=at_price,
            exit_kind=kind,
            gross=gross,
            commission=cost_model.commission_per_round_turn,
            mae=max(0.0, cost_model.dollars(adverse, position.size)),
            mfe=max(0.0, cost_model.dollars(favourable, position.size)),
        )
        trades.append(trade)
        balance += trade.net
        daily[trade.trading_date] = daily.get(trade.trading_date, 0.0) + trade.net
        position = None

    for i in range(len(series)):
        bar = series.bars[i]
        t_close = series.close_instant(i)
        trading_date = session_trading_date(t_close, cfg)

        # --- A. fill a pending entry at this bar's open --------------------
        if pending is not None and position is None:
            slip = cost_model.slippage_points * pending.side  # adverse
            fill = bar.open + slip
            position = _Position(
                side=pending.side,
                size=pending.size,
                entry_price=fill,
                entry_time=t_close,
                stop=pending.stop,
                target=pending.target,
                trading_date=trading_date,
                worst_price=fill,
                best_price=fill,
            )
            if gov_state is not None:
                gov_state = record_trade(gov_state)
            if strat_state is not None:
                strat_state = replace(
                    strat_state,
                    entries_this_session=strat_state.entries_this_session + 1,
                    position_side=pending.side,
                    long_taken=strat_state.long_taken or pending.side == 1,
                    short_taken=strat_state.short_taken or pending.side == -1,
                )
        pending = None

        # --- B. bracket exits, stop first on a tie -------------------------
        # Excursions are observed BEFORE the exit test, so the bar that stops
        # us out still contributes its adverse move.
        if position is not None:
            position.observe(bar)
            if position.side == 1:
                hit_stop = bar.low <= position.stop
                hit_target = bar.high >= position.target
            else:
                hit_stop = bar.high >= position.stop
                hit_target = bar.low <= position.target

            if hit_stop:
                # Stop-market: fills at or WORSE than the stop.
                fill = position.stop - cost_model.slippage_points * position.side
                close_position(fill, t_close, "STOP")
            elif hit_target:
                # Limit: fills at the limit exactly, never improved.
                close_position(position.target, t_close, "TARGET")

        # --- C. mark to market ---------------------------------------------
        net_liq = balance + _unrealised(position, bar.close, cost_model)
        equity_curve.append((t_close, net_liq))

        # --- D. session roll ------------------------------------------------
        current_session = session_start_for(t_close, cfg)
        if gov_state is None:
            gov_state = GovernorState(
                session_start_balance=net_liq,
                last_reconcile=t_close,
                session_start=current_session,
            )
        elif gov_state.session_start != current_session:
            # The previous session closed: record its EOD for the MLL floor.
            previous_anchor = gov_state.session_start
            if previous_anchor is None:
                # Cannot identify which session just closed, so the floor is
                # left alone rather than moved on a guess.
                previous_date = None
            else:
                previous_date = session_trading_date(
                    previous_anchor + timedelta(minutes=1), cfg
                )
            # Out-of-order or duplicate dates leave the floor alone by design.
            if previous_date is not None:
                with contextlib.suppress(ValueError):
                    mll = record_eod(mll, previous_date, balance)

        snapshot = AccountSnapshot(
            net_liq=net_liq,
            balance=balance,
            mll_floor=floor_for(mll),
            open_position_size=(position.side * position.size) if position else 0,
            session_start_balance=(
                gov_state.session_start_balance
                if gov_state.session_start == current_session
                else net_liq
            ),
            now=t_close,
            kill_switch_active=False,
            observed_trades=gov_state.trades_today,
        )
        gov_state = roll_session(snapshot, gov_state, cfg)
        snapshot = replace(
            snapshot,
            session_start_balance=gov_state.session_start_balance,
            observed_trades=gov_state.trades_today,
        )

        # --- E. the governor decides ----------------------------------------
        decision: GovernorDecision = evaluate(snapshot, gov_state, cfg)
        was_halted = gov_state.halted
        gov_state = apply_decision(gov_state, decision)

        if decision.action is Action.FLATTEN_AND_HALT and not was_halted:
            halts.append(
                Halt(
                    when=t_close,
                    trading_date=trading_date,
                    code=decision.code,
                    reason=decision.reason,
                )
            )

        # --- F. flatten on a halt -------------------------------------------
        if decision.action is Action.FLATTEN_AND_HALT and position is not None:
            close_position(bar.close, t_close, "FLATTEN")
            net_liq = balance
            equity_curve[-1] = (t_close, net_liq)

        # --- G. offer the strategy a window ending at THIS bar ---------------
        if strat_state is None or strat_state.session_start != current_session:
            strat_state = StrategyState(
                session_start=current_session, trading_date=trading_date
            )
        strat_state = replace(
            strat_state,
            position_side=(position.side if position else 0),
            trading_date=trading_date,
        )

        if decision.action is Action.CONTINUE and position is None:
            window = BarWindow(series.bars, i)
            proposal = strategy(window, strat_state, strategy_config)
            if proposal is not None:
                pending = proposal

    # Any position still open when the data ends is closed at the last close,
    # so it cannot be quietly excluded from the result.
    if position is not None:
        last = series.bars[-1]
        close_position(last.close, series.close_instant(len(series) - 1), "END_OF_DATA")
        equity_curve.append((series.close_instant(len(series) - 1), balance))

    return BacktestResult(
        trades=tuple(trades),
        halts=tuple(halts),
        equity_curve=tuple(equity_curve),
        daily_pnl=tuple(sorted(daily.items())),
        starting_balance=starting_balance,
        costs=cost_model,
        timestamp_convention=series.timestamp_convention,
        bars_seen=len(series),
    )
