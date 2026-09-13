"""Strategies we have NOT implemented, and exactly why.

WHY DECLARE WHAT WE CANNOT RUN
------------------------------
The brief lists ~26 candidate strategies. Roughly a third of them need data we
do not have and cannot currently obtain: overnight Globex bars, prior-session
daily OHLC, tick prints, order-book depth, or an economic calendar.

There are two honest options for those, and one dishonest one. The dishonest
one is to implement them anyway against approximated inputs -- deriving a
"previous day high" from whatever intraday bars happen to be in the window, or
treating the first bar of the sample as an overnight range. That produces
numbers, and numbers get quoted.

So they are registered here instead, UNAVAILABLE, with the missing requirement
named. The research runner skips them with a reason. Nothing fabricates their
inputs, and no report can accidentally include them.

When the data arrives (ROADMAP Stage 2 tells us what ProjectX actually
returns), implementing one is a small, well-specified job: the metadata below
already states the hypothesis, the entry, the stop and the requirement.
"""

from __future__ import annotations

from strategies.base import (
    DataRequirement,
    StrategyFamily,
    StrategyMeta,
    register,
)

__all__ = ["DECLARED_ONLY"]


def _declare(meta: StrategyMeta) -> StrategyMeta:
    """Register metadata with a factory that refuses to construct."""

    @register(meta)
    def _unavailable(**_kwargs):  # pragma: no cover - constructing is the error
        raise NotImplementedError(
            f"{meta.name} is declared but NOT implemented: "
            f"{meta.unavailable_reason}. Implementing it against approximated "
            "inputs would produce numbers nobody could trust."
        )

    return meta


_OVERNIGHT = "needs OVERNIGHT_SESSION bars; ProjectX session coverage is " \
             "unverified (FIRM_RULES.md) so the overnight range cannot be built"
_DAILY = "needs DAILY_BARS for the prior session; deriving them from whatever " \
         "intraday bars are in the window would silently change the level"
_TICK = "needs TICK_DATA; 5-minute bars cannot resolve this"
_DEPTH = "needs LEVEL2 order-book depth, which the REST API does not provide"
_CALENDAR = "needs an ECONOMIC_CALENDAR feed, which we do not have"


DECLARED_ONLY: tuple[StrategyMeta, ...] = (
    _declare(StrategyMeta(
        name="opening_drive",
        version="0.0.0",
        family=StrategyFamily.MOMENTUM_BREAKOUT,
        hypothesis="A one-directional open with no pullback continues all session.",
        description="Opening drive continuation.",
        entry_logic="sustained one-way movement from the open with no retrace",
        exit_logic="stop or target",
        stop_method="opening swing low/high",
        target_method="measured move",
        required_data=(DataRequirement.OHLCV_INTRADAY, DataRequirement.TICK_DATA),
        unavailable_reason=_TICK + " -- a 'drive' is defined by the absence of "
                                   "pullback at a finer granularity than 5m",
    )),
    _declare(StrategyMeta(
        name="prev_day_breakout",
        version="0.0.0",
        family=StrategyFamily.MOMENTUM_BREAKOUT,
        hypothesis="Breaking the prior session's high or low signals continuation.",
        description="Previous-day high/low breakout.",
        entry_logic="close beyond prior session high/low",
        exit_logic="stop or target",
        stop_method="1xATR",
        target_method="reward_multiple x risk",
        required_data=(DataRequirement.OHLCV_INTRADAY, DataRequirement.DAILY_BARS),
        unavailable_reason=_DAILY,
    )),
    _declare(StrategyMeta(
        name="overnight_range_breakout",
        version="0.0.0",
        family=StrategyFamily.MOMENTUM_BREAKOUT,
        hypothesis="The Globex overnight range frames the RTH session.",
        description="Overnight high/low breakout.",
        entry_logic="RTH close beyond the overnight range",
        exit_logic="stop or target",
        stop_method="overnight range midpoint",
        target_method="range width projection",
        required_data=(DataRequirement.OHLCV_INTRADAY,
                       DataRequirement.OVERNIGHT_SESSION),
        unavailable_reason=_OVERNIGHT,
    )),
    _declare(StrategyMeta(
        name="overnight_range_expansion",
        version="0.0.0",
        family=StrategyFamily.MARKET_STRUCTURE,
        hypothesis="A narrow overnight range precedes an expansion day.",
        description="Range expansion after overnight compression.",
        entry_logic="breakout after a below-average overnight range",
        exit_logic="stop or target",
        stop_method="1xATR",
        target_method="reward_multiple x risk",
        required_data=(DataRequirement.OHLCV_INTRADAY,
                       DataRequirement.OVERNIGHT_SESSION),
        unavailable_reason=_OVERNIGHT,
    )),
    _declare(StrategyMeta(
        name="gap_fill",
        version="0.0.0",
        family=StrategyFamily.MEAN_REVERSION,
        hypothesis="An opening gap versus the prior close tends to fill.",
        description="Gap fill / gap reversion.",
        entry_logic="fade the gap toward the prior session close",
        exit_logic="prior close, or stop",
        stop_method="beyond the opening extreme",
        target_method="prior session close",
        required_data=(DataRequirement.OHLCV_INTRADAY, DataRequirement.DAILY_BARS),
        unavailable_reason=_DAILY + "; the gap is defined against the prior "
                                    "SETTLEMENT, not the last intraday bar",
    )),
    _declare(StrategyMeta(
        name="prior_session_range_break",
        version="0.0.0",
        family=StrategyFamily.MARKET_STRUCTURE,
        hypothesis="The prior session's range acts as support and resistance.",
        description="Prior session range break.",
        entry_logic="close beyond the prior session range",
        exit_logic="stop or target",
        stop_method="range midpoint",
        target_method="range width projection",
        required_data=(DataRequirement.OHLCV_INTRADAY, DataRequirement.DAILY_BARS),
        unavailable_reason=_DAILY,
    )),
    _declare(StrategyMeta(
        name="liquidity_structure_breakout",
        version="0.0.0",
        family=StrategyFamily.MARKET_STRUCTURE,
        hypothesis="Breakouts through resting liquidity continue; those into it fail.",
        description="Order-book structure breakout.",
        entry_logic="breakout confirmed by book imbalance",
        exit_logic="stop or target",
        stop_method="structure low/high",
        target_method="next liquidity pocket",
        required_data=(DataRequirement.OHLCV_INTRADAY, DataRequirement.LEVEL2),
        unavailable_reason=_DEPTH,
    )),
    _declare(StrategyMeta(
        name="news_volatility",
        version="0.0.0",
        family=StrategyFamily.REGIME,
        hypothesis="Scheduled releases produce a tradable volatility regime.",
        description="Economic-release volatility strategy.",
        entry_logic="breakout in the window after a scheduled release",
        exit_logic="stop or target",
        stop_method="pre-release range",
        target_method="reward_multiple x risk",
        required_data=(DataRequirement.OHLCV_INTRADAY,
                       DataRequirement.ECONOMIC_CALENDAR),
        unavailable_reason=_CALENDAR + ". Note FIRM_RULES.md: trading MAXIMUM "
                           "position size into a scheduled news event is a "
                           "PROHIBITED strategy at Topstep. We trade 2 of 50 "
                           "permitted micros, so the rule would not bind -- but "
                           "any strategy in this family needs that checked first.",
    )),
    _declare(StrategyMeta(
        name="regime_adaptive",
        version="0.0.0",
        family=StrategyFamily.REGIME,
        hypothesis="Switching between trend and reversion logic by regime beats "
                   "either alone.",
        description="Regime-adaptive meta-strategy.",
        entry_logic="delegates to a sub-strategy chosen by the detected regime",
        exit_logic="delegated",
        stop_method="delegated",
        target_method="delegated",
        required_data=(DataRequirement.OHLCV_INTRADAY, DataRequirement.DAILY_BARS),
        unavailable_reason=(
            "deliberately deferred. A meta-strategy that selects among "
            "sub-strategies multiplies the effective free parameters, and "
            "selecting the regime rule on the same data that evaluates it is a "
            "textbook way to manufacture an edge that does not exist. Build it "
            "only after at least one component strategy survives walk-forward "
            "on its own."
        ),
    )),
)
