"""Candidate strategies. Every one is a HYPOTHESIS with no measured edge.

NONE OF THESE HAS BEEN VALIDATED
--------------------------------
No strategy here has been run on real market data. There is no evidence any of
them works, and the expected outcome of the research pipeline is that most --
possibly all -- fail. "No strategy currently validated" is a successful result.

They exist so the data can decide, not because any is believed to be good.

WHAT IS IMPLEMENTED VERSUS DECLARED
-----------------------------------
Implemented here: strategies computable from 5-minute OHLCV within a single
RTH session, which is the only data we can be confident of obtaining.

Declared in :mod:`catalogue` but NOT implemented: anything needing overnight
bars, daily bars, tick data, depth, or an economic calendar. Those are
registered with their missing requirement named, so the research runner reports
"skipped, needs X" instead of silently producing a number from data we faked.

SHARED DISCIPLINE
-----------------
Every strategy below:
  * acts on a bar CLOSE, never an intrabar touch (a touch is noise that
    happened to reach a price);
  * refuses to trade when its stop cannot be computed, rather than guessing;
  * refuses when the implied risk exceeds the configured cap, rather than
    tightening the stop into the noise;
  * takes at most one trade per direction per session.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import time
from zoneinfo import ZoneInfo

from backtest import Bar, BarWindow, Signal, StrategyState
from strategies.base import (
    DataRequirement,
    ParameterSpec,
    StrategyFamily,
    StrategyMeta,
    register,
)

ET = ZoneInfo("America/New_York")

__all__ = [
    "SignalBuilder",
    "compute_atr",
    "ema",
    "session_bars",
    "vwap",
]


# ---------------------------------------------------------------------------
# Shared indicator helpers. Pure; return None rather than a guess.
# ---------------------------------------------------------------------------


def _et(bar: Bar) -> time:
    return bar.ts.astimezone(ET).timetz().replace(tzinfo=None)


def session_bars(bars: BarWindow) -> list[Bar]:
    """Bars belonging to the CURRENT calendar session only.

    Walks back and stops at the date change, so yesterday can never leak into
    today's indicator -- the quiet way an intraday strategy acquires lookahead.
    """
    today = bars.current.ts.astimezone(ET).date()
    out: list[Bar] = []
    for i in range(len(bars) - 1, -1, -1):
        bar = bars[i]
        if bar.ts.astimezone(ET).date() != today:
            break
        out.append(bar)
    out.reverse()
    return out


def compute_atr(bars: Sequence[Bar], period: int) -> float | None:
    """Wilder true range, simple-averaged. None when history is short."""
    if len(bars) < period + 1:
        return None
    trs = [
        max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close))
        for prev, cur in zip(bars[-(period + 1):-1], bars[-period:])
    ]
    return sum(trs) / len(trs) if trs else None


def vwap(bars: Sequence[Bar]) -> float | None:
    """Volume-weighted average price over the given bars.

    Returns None when volume is absent or zero. Falling back to a simple
    average would silently compute something that is NOT VWAP and label it
    VWAP, which is worse than refusing.
    """
    total_volume = sum(b.volume for b in bars)
    if total_volume <= 0:
        return None
    return sum(((b.high + b.low + b.close) / 3.0) * b.volume for b in bars) / total_volume


def ema(values: Sequence[float], period: int) -> float | None:
    if len(values) < period:
        return None
    k = 2.0 / (period + 1.0)
    out = values[0]
    for v in values[1:]:
        out = v * k + out * (1 - k)
    return out


def zscore(values: Sequence[float], period: int) -> float | None:
    if len(values) < period + 1:
        return None
    window = list(values[-period:])
    mean = statistics.fmean(window)
    sd = statistics.pstdev(window)
    if sd <= 0:
        return None
    return (values[-1] - mean) / sd


@dataclass(frozen=True)
class SignalBuilder:
    """Turns a direction and a stop distance into a risk-checked Signal.

    Shared so every strategy applies the same discipline: refuse rather than
    tighten, round to the tick, and never emit a stop on the wrong side.
    """

    size: int = 2
    max_risk_dollars: float = 80.0
    point_value: float = 2.00
    tick_size: float = 0.25
    reward_multiple: float = 1.5

    def build(
        self, side: int, entry: float, stop_distance: float, reason: str
    ) -> Signal | None:
        if stop_distance is None or stop_distance < self.tick_size:
            return None
        risk = stop_distance * self.size * self.point_value
        if risk > self.max_risk_dollars:
            # Refused, not tightened: moving the stop closer raises the chance
            # of a noise stop-out, which is a different (worse) strategy.
            return None

        if side == 1:
            stop = entry - stop_distance
            target = entry + stop_distance * self.reward_multiple
        else:
            stop = entry + stop_distance
            target = entry - stop_distance * self.reward_multiple

        return Signal(
            side=side,
            size=self.size,
            stop=round(stop / self.tick_size) * self.tick_size,
            target=round(target / self.tick_size) * self.tick_size,
            reason=f"{reason} | stop {stop_distance:.2f}pt (${risk:.2f})",
        )


@dataclass(frozen=True)
class _Base:
    """Common plumbing. Subclasses implement ``propose``."""

    builder: SignalBuilder = field(default_factory=SignalBuilder)
    entry_cutoff_et: time = time(11, 30)
    warmup_bars: int = 3

    def __call__(self, bars: BarWindow, state: StrategyState, _c=None) -> Signal | None:
        return self.on_bar(bars, state)

    def on_bar(self, bars: BarWindow, state: StrategyState) -> Signal | None:
        if state.position_side != 0:
            return None
        if _et(bars.current) > self.entry_cutoff_et:
            return None
        today = session_bars(bars)
        if len(today) < self.warmup_bars:
            return None
        return self.propose(bars, state, today)

    def propose(self, bars, state, today):  # pragma: no cover - interface
        raise NotImplementedError


# ===========================================================================
# MOMENTUM / BREAKOUT
# ===========================================================================


ORB_META = StrategyMeta(
    name="orb",
    version="0.1.0",
    family=StrategyFamily.MOMENTUM_BREAKOUT,
    hypothesis="Price closing beyond the first 15 minutes' range continues in "
               "that direction often enough to pay for the losers.",
    description="Opening Range Breakout on the 09:30-09:45 ET range.",
    entry_logic="5m close beyond the opening range high/low",
    exit_logic="stop or target; flat by the governor's hard flatten",
    stop_method="tighter of range midpoint distance and 1xATR",
    target_method="reward_multiple x risk",
    parameters=(
        ParameterSpec("atr_period", 14, tunable=True, sweep_values=(7, 10, 14, 20)),
        ParameterSpec("reward_multiple", 1.5, tunable=True,
                      sweep_values=(1.0, 1.5, 2.0, 2.5)),
        ParameterSpec("range_minutes", 15, note="structural, from the spec"),
    ),
    assumptions=(
        "the opening range is meaningful on MNQ",
        "a 5m close is a better signal than an intrabar touch",
    ),
)


@register(ORB_META)
@dataclass(frozen=True)
class OpeningRangeBreakout(_Base):
    atr_period: int = 14
    range_end_et: time = time(9, 45)
    meta: StrategyMeta = ORB_META

    def propose(self, bars, state, today):
        window = [b for b in today if _et(b) <= self.range_end_et]
        if not window or _et(bars.current) <= self.range_end_et:
            return None
        high, low = max(b.high for b in window), min(b.low for b in window)
        if high <= low:
            return None

        current = bars.current
        if current.close > high and not state.long_taken:
            side = 1
        elif current.close < low and not state.short_taken:
            side = -1
        else:
            return None

        atr = compute_atr(list(bars), self.atr_period)
        if atr is None:
            return None
        mid = (high + low) / 2.0
        return self.builder.build(
            side, current.close, min(abs(current.close - mid), atr),
            f"ORB {'long' if side == 1 else 'short'} beyond [{low:.2f},{high:.2f}]",
        )


DONCHIAN_META = StrategyMeta(
    name="donchian_breakout",
    version="0.1.0",
    family=StrategyFamily.MOMENTUM_BREAKOUT,
    hypothesis="Closing beyond an N-bar channel signals continuation.",
    description="Donchian channel breakout within the session.",
    entry_logic="close above the N-bar high / below the N-bar low",
    exit_logic="stop or target",
    stop_method="1xATR",
    target_method="reward_multiple x risk",
    parameters=(
        ParameterSpec("channel_bars", 12, tunable=True, sweep_values=(6, 12, 18, 24)),
        ParameterSpec("atr_period", 14, tunable=True, sweep_values=(7, 14, 21)),
    ),
)


@register(DONCHIAN_META)
@dataclass(frozen=True)
class DonchianBreakout(_Base):
    channel_bars: int = 12
    atr_period: int = 14
    warmup_bars: int = 13
    meta: StrategyMeta = DONCHIAN_META

    def propose(self, bars, state, today):
        if len(today) < self.channel_bars + 1:
            return None
        prior = today[-(self.channel_bars + 1):-1]
        high, low = max(b.high for b in prior), min(b.low for b in prior)
        current = bars.current

        if current.close > high and not state.long_taken:
            side = 1
        elif current.close < low and not state.short_taken:
            side = -1
        else:
            return None

        atr = compute_atr(list(bars), self.atr_period)
        if atr is None:
            return None
        return self.builder.build(
            side, current.close, atr,
            f"Donchian({self.channel_bars}) {'long' if side == 1 else 'short'}",
        )


ATR_BREAKOUT_META = StrategyMeta(
    name="atr_volatility_breakout",
    version="0.1.0",
    family=StrategyFamily.MOMENTUM_BREAKOUT,
    hypothesis="A move of k x ATR from the session open marks a directional day.",
    description="Volatility breakout measured from the session's first price.",
    entry_logic="close more than k x ATR from the session open",
    exit_logic="stop or target",
    stop_method="1xATR",
    target_method="reward_multiple x risk",
    parameters=(
        ParameterSpec("atr_multiple", 1.0, tunable=True,
                      sweep_values=(0.5, 0.75, 1.0, 1.5)),
        ParameterSpec("atr_period", 14, tunable=True, sweep_values=(7, 14, 21)),
    ),
)


@register(ATR_BREAKOUT_META)
@dataclass(frozen=True)
class AtrVolatilityBreakout(_Base):
    atr_multiple: float = 1.0
    atr_period: int = 14
    warmup_bars: int = 4
    meta: StrategyMeta = ATR_BREAKOUT_META

    def propose(self, bars, state, today):
        atr = compute_atr(list(bars), self.atr_period)
        if atr is None or atr <= 0:
            return None
        anchor = today[0].open
        current = bars.current
        move = current.close - anchor
        if move > self.atr_multiple * atr and not state.long_taken:
            side = 1
        elif move < -self.atr_multiple * atr and not state.short_taken:
            side = -1
        else:
            return None
        return self.builder.build(
            side, current.close, atr,
            f"ATR breakout {move:+.2f} vs {self.atr_multiple}xATR",
        )


# ===========================================================================
# VWAP
# ===========================================================================


VWAP_REVERSION_META = StrategyMeta(
    name="vwap_reversion",
    version="0.1.0",
    family=StrategyFamily.VWAP,
    hypothesis="Price stretched far from session VWAP reverts toward it.",
    description="Fade extension from session VWAP.",
    entry_logic="close more than k x ATR away from VWAP, trade back toward it",
    exit_logic="stop or target",
    stop_method="1xATR beyond the extreme",
    target_method="reward_multiple x risk",
    required_data=(DataRequirement.OHLCV_INTRADAY, DataRequirement.VOLUME),
    parameters=(
        ParameterSpec("deviation_atr", 1.5, tunable=True,
                      sweep_values=(1.0, 1.5, 2.0, 2.5)),
        ParameterSpec("atr_period", 14, tunable=True, sweep_values=(7, 14, 21)),
    ),
    assumptions=(
        "per-bar volume from ProjectX is trustworthy enough to weight by",
    ),
)


@register(VWAP_REVERSION_META)
@dataclass(frozen=True)
class VwapReversion(_Base):
    deviation_atr: float = 1.5
    atr_period: int = 14
    warmup_bars: int = 5
    meta: StrategyMeta = VWAP_REVERSION_META

    def propose(self, bars, state, today):
        anchor = vwap(today)
        if anchor is None:
            return None  # no volume: refuse rather than compute a fake VWAP
        atr = compute_atr(list(bars), self.atr_period)
        if atr is None or atr <= 0:
            return None

        current = bars.current
        deviation = current.close - anchor
        threshold = self.deviation_atr * atr
        if deviation < -threshold and not state.long_taken:
            side = 1
        elif deviation > threshold and not state.short_taken:
            side = -1
        else:
            return None
        return self.builder.build(
            side, current.close, atr,
            f"VWAP reversion {deviation:+.2f} vs {self.deviation_atr}xATR",
        )


VWAP_TREND_META = StrategyMeta(
    name="vwap_trend",
    version="0.1.0",
    family=StrategyFamily.VWAP,
    hypothesis="Price reclaiming VWAP with momentum continues in that direction.",
    description="Trade the first reclaim of session VWAP.",
    entry_logic="close crosses VWAP after being on the other side",
    exit_logic="stop or target",
    stop_method="1xATR",
    target_method="reward_multiple x risk",
    required_data=(DataRequirement.OHLCV_INTRADAY, DataRequirement.VOLUME),
    parameters=(
        ParameterSpec("atr_period", 14, tunable=True, sweep_values=(7, 14, 21)),
        ParameterSpec("reward_multiple", 1.5, tunable=True,
                      sweep_values=(1.0, 1.5, 2.0)),
    ),
)


@register(VWAP_TREND_META)
@dataclass(frozen=True)
class VwapReclaim(_Base):
    atr_period: int = 14
    warmup_bars: int = 5
    meta: StrategyMeta = VWAP_TREND_META

    def propose(self, bars, state, today):
        if len(today) < 2:
            return None
        anchor = vwap(today)
        previous_anchor = vwap(today[:-1])
        if anchor is None or previous_anchor is None:
            return None
        atr = compute_atr(list(bars), self.atr_period)
        if atr is None or atr <= 0:
            return None

        previous, current = today[-2], today[-1]
        crossed_up = previous.close <= previous_anchor and current.close > anchor
        crossed_down = previous.close >= previous_anchor and current.close < anchor

        if crossed_up and not state.long_taken:
            side = 1
        elif crossed_down and not state.short_taken:
            side = -1
        else:
            return None
        return self.builder.build(side, current.close, atr, "VWAP reclaim")


# ===========================================================================
# TREND
# ===========================================================================


EMA_TREND_META = StrategyMeta(
    name="ema_trend",
    version="0.1.0",
    family=StrategyFamily.TREND,
    hypothesis="A fast EMA crossing a slow one marks a tradable intraday trend.",
    description="EMA crossover within the session.",
    entry_logic="fast EMA crosses slow EMA on a bar close",
    exit_logic="stop or target",
    stop_method="1xATR",
    target_method="reward_multiple x risk",
    parameters=(
        ParameterSpec("fast", 9, tunable=True, sweep_values=(5, 9, 13)),
        ParameterSpec("slow", 21, tunable=True, sweep_values=(21, 34, 50)),
        ParameterSpec("atr_period", 14),
    ),
)


@register(EMA_TREND_META)
@dataclass(frozen=True)
class EmaTrend(_Base):
    fast: int = 9
    slow: int = 21
    atr_period: int = 14
    warmup_bars: int = 22
    meta: StrategyMeta = EMA_TREND_META

    def propose(self, bars, state, today):
        closes = [b.close for b in today]
        if len(closes) < self.slow + 1:
            return None
        fast_now, slow_now = ema(closes, self.fast), ema(closes, self.slow)
        fast_prev, slow_prev = ema(closes[:-1], self.fast), ema(closes[:-1], self.slow)
        if None in (fast_now, slow_now, fast_prev, slow_prev):
            return None
        atr = compute_atr(list(bars), self.atr_period)
        if atr is None or atr <= 0:
            return None

        if fast_prev <= slow_prev and fast_now > slow_now and not state.long_taken:
            side = 1
        elif fast_prev >= slow_prev and fast_now < slow_now and not state.short_taken:
            side = -1
        else:
            return None
        return self.builder.build(
            side, bars.current.close, atr, f"EMA {self.fast}/{self.slow} cross"
        )


# ===========================================================================
# MEAN REVERSION
# ===========================================================================


ZSCORE_META = StrategyMeta(
    name="zscore_reversion",
    version="0.1.0",
    family=StrategyFamily.MEAN_REVERSION,
    hypothesis="Intraday price extremes measured in standard deviations revert.",
    description="Z-score mean reversion on session closes.",
    entry_logic="|z| beyond a threshold, trade back toward the mean",
    exit_logic="stop or target",
    stop_method="1xATR",
    target_method="reward_multiple x risk",
    parameters=(
        ParameterSpec("lookback", 20, tunable=True, sweep_values=(10, 20, 30)),
        ParameterSpec("threshold", 2.0, tunable=True,
                      sweep_values=(1.5, 2.0, 2.5, 3.0)),
        ParameterSpec("atr_period", 14),
    ),
)


@register(ZSCORE_META)
@dataclass(frozen=True)
class ZScoreReversion(_Base):
    lookback: int = 20
    threshold: float = 2.0
    atr_period: int = 14
    warmup_bars: int = 21
    meta: StrategyMeta = ZSCORE_META

    def propose(self, bars, state, today):
        z = zscore([b.close for b in today], self.lookback)
        if z is None:
            return None
        atr = compute_atr(list(bars), self.atr_period)
        if atr is None or atr <= 0:
            return None

        if z < -self.threshold and not state.long_taken:
            side = 1
        elif z > self.threshold and not state.short_taken:
            side = -1
        else:
            return None
        return self.builder.build(
            side, bars.current.close, atr, f"z={z:+.2f} beyond {self.threshold}"
        )


BOLLINGER_META = StrategyMeta(
    name="bollinger_reversion",
    version="0.1.0",
    family=StrategyFamily.MEAN_REVERSION,
    hypothesis="Closes outside a volatility band revert toward the mean.",
    description="Bollinger band mean reversion.",
    entry_logic="close beyond the band, trade back toward the mean",
    exit_logic="stop or target",
    stop_method="1xATR",
    target_method="reward_multiple x risk",
    parameters=(
        ParameterSpec("period", 20, tunable=True, sweep_values=(10, 20, 30)),
        ParameterSpec("band_sigma", 2.0, tunable=True, sweep_values=(1.5, 2.0, 2.5)),
        ParameterSpec("atr_period", 14),
    ),
)


@register(BOLLINGER_META)
@dataclass(frozen=True)
class BollingerReversion(_Base):
    period: int = 20
    band_sigma: float = 2.0
    atr_period: int = 14
    warmup_bars: int = 21
    meta: StrategyMeta = BOLLINGER_META

    def propose(self, bars, state, today):
        closes = [b.close for b in today]
        if len(closes) < self.period:
            return None
        window = closes[-self.period:]
        mean = statistics.fmean(window)
        sd = statistics.pstdev(window)
        if sd <= 0:
            return None
        upper, lower = mean + self.band_sigma * sd, mean - self.band_sigma * sd
        atr = compute_atr(list(bars), self.atr_period)
        if atr is None or atr <= 0:
            return None

        current = bars.current
        if current.close < lower and not state.long_taken:
            side = 1
        elif current.close > upper and not state.short_taken:
            side = -1
        else:
            return None
        return self.builder.build(
            side, current.close, atr,
            f"Bollinger {self.band_sigma}sigma reversion",
        )


# ===========================================================================
# MARKET STRUCTURE
# ===========================================================================


FAILED_BREAKOUT_META = StrategyMeta(
    name="failed_breakout",
    version="0.1.0",
    family=StrategyFamily.MARKET_STRUCTURE,
    hypothesis="A breakout that immediately closes back inside the range traps "
               "traders and reverses.",
    description="Fade a failed opening-range breakout.",
    entry_logic="bar exceeds the range but CLOSES back inside; trade the other way",
    exit_logic="stop or target",
    stop_method="1xATR beyond the failed extreme",
    target_method="reward_multiple x risk",
    parameters=(
        ParameterSpec("atr_period", 14, tunable=True, sweep_values=(7, 14, 21)),
        ParameterSpec("reward_multiple", 1.5, tunable=True,
                      sweep_values=(1.0, 1.5, 2.0)),
    ),
)


@register(FAILED_BREAKOUT_META)
@dataclass(frozen=True)
class FailedBreakout(_Base):
    atr_period: int = 14
    range_end_et: time = time(9, 45)
    warmup_bars: int = 4
    meta: StrategyMeta = FAILED_BREAKOUT_META

    def propose(self, bars, state, today):
        window = [b for b in today if _et(b) <= self.range_end_et]
        if not window or _et(bars.current) <= self.range_end_et:
            return None
        high, low = max(b.high for b in window), min(b.low for b in window)
        if high <= low:
            return None

        current = bars.current
        poked_up = current.high > high and current.close < high
        poked_down = current.low < low and current.close > low

        if poked_down and not state.long_taken:
            side = 1
        elif poked_up and not state.short_taken:
            side = -1
        else:
            return None

        atr = compute_atr(list(bars), self.atr_period)
        if atr is None or atr <= 0:
            return None
        return self.builder.build(
            side, current.close, atr,
            f"failed breakout {'below' if side == 1 else 'above'} the range",
        )
