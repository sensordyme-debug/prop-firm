"""Opening Range Breakout, MNQ. **A HYPOTHESIS, NOT A VALIDATED EDGE.**

STATUS: UNVALIDATED
-------------------
Nothing here has been tested on real market data. It has never produced a
single real trade. It is written so that it CAN be evaluated, not because there
is any reason yet to believe it works. Most candidate strategies do not clear
the ROADMAP Stage 4 gates, and that is a normal outcome rather than a failure.

Do not read the existence of this file as evidence of anything.

THE RULES (CLAUDE.md strategy spec)
-----------------------------------
* Opening range = the high and low of 09:30-09:45 ET (three 5-minute bars).
* Entry on a 5-minute **close** beyond the range, never an intrabar touch --
  a touch is not a signal, it is noise that happened to reach a price.
* One trade per direction per session; at most 2 per session.
* No new entries after 11:30 ET.
* Stop = the tighter of the range midpoint and 1x ATR(14), capped so the dollar
  risk stays within ``risk_per_trade``.
* Target = 1.5R.

PURITY AND THE SINGLE IMPLEMENTATION
------------------------------------
``on_bar`` is a pure function of (bars, state, config). No I/O, no clock, no
SDK, no broker. The backtester and the live runner call **this same function** --
there is deliberately no ``backtest_orb`` and ``live_orb``, because two
implementations of one idea diverge and then the backtest validates code that
never trades.

It proposes. It cannot execute, and it cannot overrule the governor: it returns
a Signal or None, and the governor decides what happens next.

PARAMETERS
----------
Four, and that is deliberate. CLAUDE.md permits tuning at most two; the other
two are structural (the range window and the session cutoff) and come from the
spec rather than from fitting. Adding parameters to improve a backtest is
curve-fitting, not research.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from typing import Final, Sequence
from zoneinfo import ZoneInfo

from backtest import Bar, BarWindow, Signal, StrategyState

__all__ = ["OrbConfig", "OpeningRange", "OpeningRangeBreakout", "compute_atr"]

ET: Final[ZoneInfo] = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class OrbConfig:
    """Strategy parameters. Only the first two are considered tunable."""

    # -- tunable (CLAUDE.md allows at most two) -----------------------------
    atr_period: int = 14
    reward_multiple: float = 1.5

    # -- structural: from the spec, not from fitting ------------------------
    range_start_et: time = time(9, 30)
    range_end_et: time = time(9, 45)
    entry_cutoff_et: time = time(11, 30)

    # -- risk, supplied by AppConfig ----------------------------------------
    size: int = 2
    max_risk_dollars: float = 80.0
    point_value: float = 2.00
    tick_size: float = 0.25

    def __post_init__(self) -> None:
        if self.atr_period < 2:
            raise ValueError("atr_period must be at least 2")
        if self.reward_multiple <= 0:
            raise ValueError("reward_multiple must be positive")
        if self.size <= 0:
            raise ValueError("size must be positive")
        if self.max_risk_dollars <= 0:
            raise ValueError("max_risk_dollars must be positive")
        if not self.range_start_et < self.range_end_et:
            raise ValueError("range_start_et must be before range_end_et")


@dataclass(frozen=True)
class OpeningRange:
    high: float
    low: float
    bars: int

    @property
    def midpoint(self) -> float:
        return (self.high + self.low) / 2.0

    @property
    def width(self) -> float:
        return self.high - self.low


def compute_atr(bars: Sequence[Bar], period: int) -> float | None:
    """Wilder's true range, simple-averaged over ``period`` completed bars.

    Returns None when there is not enough history. None means "no signal",
    never "use zero" -- a zero ATR would collapse the stop onto the entry.
    """
    if len(bars) < period + 1:
        return None
    true_ranges: list[float] = []
    for previous, current in zip(bars[-(period + 1):-1], bars[-period:]):
        true_ranges.append(max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        ))
    if not true_ranges:
        return None
    return sum(true_ranges) / len(true_ranges)


def _et_time(bar: Bar) -> time:
    return bar.ts.astimezone(ET).timetz().replace(tzinfo=None)


def opening_range(bars: BarWindow, config: OrbConfig) -> OpeningRange | None:
    """The high/low of the bars inside the opening window, for TODAY only.

    Scans backwards from the current bar and stops at the session boundary, so
    yesterday's range can never leak into today's signal. Returns None until
    the window has actually completed -- a partial range is a different,
    tighter range, and trading it would be trading a different strategy.
    """
    current_date = bars.current.ts.astimezone(ET).date()
    highs: list[float] = []
    lows: list[float] = []
    for i in range(len(bars) - 1, -1, -1):
        bar = bars[i]
        et = bar.ts.astimezone(ET)
        if et.date() != current_date:
            break
        at = _et_time(bar)
        if config.range_start_et <= at <= config.range_end_et:
            highs.append(bar.high)
            lows.append(bar.low)
    if not highs:
        return None

    window_complete = _et_time(bars.current) > config.range_end_et
    if not window_complete:
        return None
    return OpeningRange(high=max(highs), low=min(lows), bars=len(highs))


@dataclass(frozen=True)
class OpeningRangeBreakout:
    """The strategy. Callable as ``(bars, state, config) -> Signal | None``."""

    config: OrbConfig = OrbConfig()

    def __call__(
        self, bars: BarWindow, state: StrategyState, _config: object = None
    ) -> Signal | None:
        return self.on_bar(bars, state)

    def on_bar(self, bars: BarWindow, state: StrategyState) -> Signal | None:
        """Propose a trade, or not. Pure.

        Every rejection below is a reason NOT to trade. The governor applies
        its own, stricter set afterwards; these are the strategy's own and are
        deliberately not a substitute for it.
        """
        cfg = self.config
        current = bars.current
        now_et = _et_time(current)

        if now_et > cfg.entry_cutoff_et:
            return None
        if state.position_side != 0:
            return None

        rng = opening_range(bars, cfg)
        if rng is None or rng.width <= 0:
            return None

        # A CLOSE beyond the range, not a touch.
        if current.close > rng.high and not state.long_taken:
            side = 1
        elif current.close < rng.low and not state.short_taken:
            side = -1
        else:
            return None

        atr = compute_atr(list(bars), cfg.atr_period)
        if atr is None or atr <= 0:
            return None  # no ATR yet: no trade, never a guessed stop

        entry = current.close
        # The tighter of midpoint and 1xATR. Tighter means less risk per trade,
        # which is the direction that keeps us inside the MLL.
        by_midpoint = abs(entry - rng.midpoint)
        stop_distance = min(by_midpoint, atr)

        # Cap by dollars. Reducing the distance would move the stop closer and
        # raise the chance of being stopped out on noise, so an over-large
        # distance is refused outright rather than silently tightened.
        risk = stop_distance * cfg.size * cfg.point_value
        if risk > cfg.max_risk_dollars:
            return None
        if stop_distance < cfg.tick_size:
            return None

        if side == 1:
            stop = entry - stop_distance
            target = entry + stop_distance * cfg.reward_multiple
        else:
            stop = entry + stop_distance
            target = entry - stop_distance * cfg.reward_multiple

        return Signal(
            side=side,
            size=cfg.size,
            stop=round(stop / cfg.tick_size) * cfg.tick_size,
            target=round(target / cfg.tick_size) * cfg.tick_size,
            reason=(
                f"ORB {'long' if side == 1 else 'short'}: close {entry:.2f} beyond "
                f"[{rng.low:.2f}, {rng.high:.2f}], stop {stop_distance:.2f}pt "
                f"(${risk:.2f}), {cfg.reward_multiple}R target"
            ),
        )
