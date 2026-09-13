"""Strategy framework: a common interface, a registry, and honest metadata.

WHY THIS EXISTS
---------------
This is not an ORB bot. ORB is one hypothesis among many, and the platform's
job is to find out whether ANY of them has a robust edge -- including
concluding that none does, which is a successful outcome, not a failure.

So nothing in the core may know a strategy's name. Adding a strategy must not
require touching the backtester, the governor, the execution layer or the
analytics. A strategy declares itself here and the research pipeline picks it
up.

WHAT A STRATEGY MAY AND MAY NOT DO
----------------------------------
It receives bars and its own driver-maintained state, and returns a Signal or
None. That is all.

  * It may NOT know about ProjectX, TopstepX, or any broker.
  * It may NOT submit an order, or reach any execution object.
  * It may NOT overrule the governor -- it proposes, the governor decides.
  * It may NOT read the clock, the network, or the filesystem.

A strategy that cannot reach the broker cannot bypass the risk layer by
accident, which is a stronger guarantee than a code review.

DATA HONESTY
------------
Every strategy declares what data it needs. A strategy whose requirements we
cannot currently satisfy is registered as UNAVAILABLE with the missing
requirement named. It is never quietly given fabricated inputs, and the
research runner skips it with a reason rather than producing a number.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "REGISTRY",
    "DataRequirement",
    "ParameterSpec",
    "Strategy",
    "StrategyFamily",
    "StrategyMeta",
    "all_strategies",
    "available_strategies",
    "config_hash",
    "dataset_hash",
    "get_strategy",
    "register",
    "unavailable_strategies",
]


class DataRequirement(str, Enum):
    """What a strategy needs. Anything beyond OHLCV_INTRADAY we do not have."""

    OHLCV_INTRADAY = "OHLCV_INTRADAY"        # 5-minute bars -- the baseline
    VOLUME = "VOLUME"                        # trustworthy per-bar volume
    OVERNIGHT_SESSION = "OVERNIGHT_SESSION"  # Globex bars outside RTH
    DAILY_BARS = "DAILY_BARS"                # prior-session daily OHLC
    TICK_DATA = "TICK_DATA"                  # per-trade prints
    LEVEL2 = "LEVEL2"                        # order book depth
    ECONOMIC_CALENDAR = "ECONOMIC_CALENDAR"  # scheduled release times


class StrategyFamily(str, Enum):
    MOMENTUM_BREAKOUT = "MOMENTUM_BREAKOUT"
    VWAP = "VWAP"
    TREND = "TREND"
    MEAN_REVERSION = "MEAN_REVERSION"
    MARKET_STRUCTURE = "MARKET_STRUCTURE"
    REGIME = "REGIME"


@dataclass(frozen=True)
class ParameterSpec:
    """One parameter, and whether it may be tuned.

    ``tunable`` is deliberately restrictive. Sweeping everything is not
    research, it is curve-fitting with extra steps: every additional free
    parameter buys in-sample performance that does not survive out of sample.
    """

    name: str
    default: Any
    tunable: bool = False
    sweep_values: tuple[Any, ...] = ()
    note: str = ""


@dataclass(frozen=True)
class StrategyMeta:
    """Everything a researcher needs to judge a strategy before running it."""

    name: str
    version: str
    family: StrategyFamily
    hypothesis: str
    description: str
    instrument: str = "MNQ"
    timeframe: str = "5m"
    session: str = "RTH 09:30-16:00 ET"
    required_data: tuple[DataRequirement, ...] = (DataRequirement.OHLCV_INTRADAY,)
    entry_logic: str = ""
    exit_logic: str = ""
    stop_method: str = ""
    target_method: str = ""
    max_trades_per_session: int = 2
    parameters: tuple[ParameterSpec, ...] = ()
    assumptions: tuple[str, ...] = ()
    unavailable_reason: str = ""

    @property
    def tunable(self) -> tuple[ParameterSpec, ...]:
        return tuple(p for p in self.parameters if p.tunable)

    @property
    def is_available(self) -> bool:
        """Can we actually test this with the data we can obtain?"""
        return not self.unavailable_reason

    @property
    def missing_requirements(self) -> tuple[DataRequirement, ...]:
        satisfiable = {DataRequirement.OHLCV_INTRADAY, DataRequirement.VOLUME}
        return tuple(r for r in self.required_data if r not in satisfiable)

    def identity(self) -> str:
        return f"{self.name}@{self.version}"

    def describe(self) -> str:
        lines = [
            f"{self.identity()}   [{self.family.value}]",
            f"  hypothesis : {self.hypothesis}",
            f"  instrument : {self.instrument} {self.timeframe}  ({self.session})",
            f"  entry      : {self.entry_logic}",
            f"  exit       : {self.exit_logic}",
            f"  stop       : {self.stop_method}",
            f"  target     : {self.target_method}",
            f"  data       : {', '.join(r.value for r in self.required_data)}",
            "  tunable    : "
            + (", ".join(p.name for p in self.tunable) or "none"),
        ]
        if not self.is_available:
            lines.append(f"  UNAVAILABLE: {self.unavailable_reason}")
        for a in self.assumptions:
            lines.append(f"  assumes    : {a}")
        return "\n".join(lines)


@runtime_checkable
class Strategy(Protocol):
    """The only interface the core knows about.

    The same object drives the backtester and the future live runtime. There is
    deliberately no separate backtest/live variant: two implementations of one
    idea diverge, and then the backtest validates code that never trades.
    """

    @property
    def meta(self) -> StrategyMeta:
        """Read-only on purpose: strategies are frozen dataclasses, and a
        mutable protocol attribute would exclude them."""
        ...

    def on_bar(self, bars: Any, state: Any) -> Any:
        """Return a Signal, or None. Pure: no I/O, no clock, no broker."""
        ...


REGISTRY: dict[str, Callable[..., Strategy]] = {}
_META: dict[str, StrategyMeta] = {}


def register(meta: StrategyMeta) -> Callable[[Any], Any]:
    """Register a strategy factory under its declared name."""

    def decorator(factory: Any) -> Any:
        if meta.name in REGISTRY:
            raise ValueError(
                f"strategy {meta.name!r} is already registered; names must be "
                "unique so research results cannot be silently attributed to "
                "the wrong code"
            )
        REGISTRY[meta.name] = factory
        _META[meta.name] = meta
        factory.meta = meta  # type: ignore[attr-defined]
        return factory

    return decorator


def get_strategy(name: str, **params: Any) -> Strategy:
    if name not in REGISTRY:
        raise KeyError(
            f"unknown strategy {name!r}. Registered: {sorted(REGISTRY)}"
        )
    return REGISTRY[name](**params)


def all_strategies() -> dict[str, StrategyMeta]:
    return dict(_META)


def available_strategies() -> dict[str, StrategyMeta]:
    """Strategies testable with data we can actually obtain."""
    return {n: m for n, m in _META.items() if m.is_available}


def unavailable_strategies() -> dict[str, StrategyMeta]:
    """Declared but not testable. Named honestly rather than omitted."""
    return {n: m for n, m in _META.items() if not m.is_available}


# ---------------------------------------------------------------------------
# Reproducibility
# ---------------------------------------------------------------------------


def config_hash(payload: Any) -> str:
    """Stable short hash of a parameter set, for run provenance.

    Sorted keys so the same configuration always hashes the same regardless of
    dict ordering -- otherwise two identical runs would look different and
    "reproducible" would mean nothing.
    """
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:12]


def dataset_hash(bars: Iterable[Any]) -> str:
    """Hash of the actual bars a result was produced from.

    Identifies the dataset so a result can never be silently compared against
    one produced from different data.
    """
    digest = hashlib.sha256()
    count = 0
    for bar in bars:
        digest.update(
            f"{getattr(bar, 'ts', '')}|{getattr(bar, 'open', '')}|"
            f"{getattr(bar, 'high', '')}|{getattr(bar, 'low', '')}|"
            f"{getattr(bar, 'close', '')}".encode()
        )
        count += 1
    if count == 0:
        return "empty"
    return f"{digest.hexdigest()[:12]}:{count}"
