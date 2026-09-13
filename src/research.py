"""Robustness testing. The tools that answer "is this real?" rather than "what did it make?"

A single backtest is one sample being read as a distribution. These utilities
exist because the failure that ends accounts is not a strategy that looks bad --
it is a strategy that looked good once.

WHAT EACH ANSWERS
-----------------
* **Split / walk-forward** -- does it survive data it was not chosen on?
  The test set must never influence parameter selection, which is why
  :func:`split` hands back three named windows and the walk-forward runner
  optimises only inside the training window.

* **Parameter sensitivity** -- is there a stable REGION, or a lucky point?
  A parameter set that is excellent while its neighbours are terrible is a
  coincidence with a good marketing department. :func:`sensitivity_table`
  reports the neighbourhood and never sorts by profit.

* **Slippage / commission sensitivity** -- does the edge survive worse fills?
  If two ticks of slippage destroys it, it was a fill assumption, not an edge.

* **Monte Carlo** -- was the drawdown typical or lucky? Reordering the same
  trades produces a distribution of outcomes the same strategy could have
  delivered. The observed path is one draw from it.

DETERMINISM
-----------
Every random routine takes an explicit seed and defaults to one, so results are
reproducible and tests are stable.
"""

from __future__ import annotations

import random
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "DataSplit",
    "MonteCarloResult",
    "ParameterResult",
    "WalkForwardWindow",
    "monte_carlo",
    "sensitivity_table",
    "split",
    "walk_forward_windows",
]


# ---------------------------------------------------------------------------
# In-sample / out-of-sample
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DataSplit:
    """Three windows. The test window is not to be looked at while choosing."""

    train: tuple[Any, ...]
    validation: tuple[Any, ...]
    test: tuple[Any, ...]

    @property
    def sizes(self) -> dict[str, int]:
        return {
            "train": len(self.train),
            "validation": len(self.validation),
            "test": len(self.test),
        }

    def describe(self) -> str:
        total = sum(self.sizes.values()) or 1
        return "  ".join(
            f"{name}={count} ({count / total:.0%})"
            for name, count in self.sizes.items()
        )


def split(
    items: Sequence[Any],
    *,
    train: float = 0.5,
    validation: float = 0.25,
) -> DataSplit:
    """Split chronologically. Never shuffled.

    Shuffling before splitting would leak the future into the training set --
    the single most common way a backtest flatters itself. Order is preserved
    absolutely.
    """
    if not 0 < train < 1 or not 0 <= validation < 1 or train + validation >= 1:
        raise ValueError(
            f"invalid split train={train} validation={validation}; "
            "the three windows must each be non-empty fractions summing to 1"
        )
    n = len(items)
    a = int(n * train)
    b = a + int(n * validation)
    return DataSplit(
        train=tuple(items[:a]),
        validation=tuple(items[a:b]),
        test=tuple(items[b:]),
    )


@dataclass(frozen=True)
class WalkForwardWindow:
    index: int
    train: tuple[Any, ...]
    test: tuple[Any, ...]


def walk_forward_windows(
    items: Sequence[Any],
    *,
    train_size: int,
    test_size: int,
    step: int | None = None,
) -> list[WalkForwardWindow]:
    """Rolling train/test windows.

    Each test window sits strictly AFTER its training window, so parameters
    chosen on the first are evaluated on data that did not exist when they were
    chosen. Aggregating the test windows gives out-of-sample performance across
    several regimes rather than one.
    """
    if train_size <= 0 or test_size <= 0:
        raise ValueError("train_size and test_size must be positive")
    stride = step or test_size
    windows: list[WalkForwardWindow] = []
    start = 0
    index = 0
    while start + train_size + test_size <= len(items):
        windows.append(WalkForwardWindow(
            index=index,
            train=tuple(items[start:start + train_size]),
            test=tuple(items[start + train_size:start + train_size + test_size]),
        ))
        start += stride
        index += 1
    return windows


# ---------------------------------------------------------------------------
# Parameter sensitivity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParameterResult:
    params: dict[str, Any]
    net_profit: float
    max_drawdown: float
    trades: int
    profit_factor: float | None = None
    sharpe: float | None = None
    passed_gates: bool = False


def sensitivity_table(results: Sequence[ParameterResult]) -> str:
    """Render a parameter sweep WITHOUT ranking by profit.

    Sorted by parameter value, not by outcome. Sorting by profit is how a
    sweep becomes an optimiser, and an optimiser over a single sample is a
    curve-fitter. What matters is whether good results cluster.
    """
    if not results:
        return "  (no results)"

    keys = sorted(results[0].params)
    header = "  " + "".join(f"{k:>14s}" for k in keys) + (
        f"{'Net':>12s}{'MaxDD':>12s}{'PF':>8s}{'Sharpe':>9s}{'Trades':>8s}{'Gates':>7s}"
    )
    lines = [header, "  " + "-" * (len(header) - 2)]

    for r in sorted(results, key=lambda x: tuple(str(x.params[k]) for k in keys)):
        row = "  " + "".join(f"{r.params[k]!s:>14s}" for k in keys)
        row += f"{r.net_profit:>12,.0f}{r.max_drawdown:>12,.0f}"
        row += f"{(f'{r.profit_factor:.2f}' if r.profit_factor else '-'):>8s}"
        row += f"{(f'{r.sharpe:.2f}' if r.sharpe else '-'):>9s}"
        row += f"{r.trades:>8d}{('PASS' if r.passed_gates else 'fail'):>7s}"
        lines.append(row)

    passing = [r for r in results if r.passed_gates]
    lines.append("")
    lines.append(f"  {len(passing)}/{len(results)} parameter sets clear the gates.")
    lines.append(
        "  Read this for a STABLE REGION, not a maximum. An isolated winner "
        "surrounded by failures is noise, not an edge."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Monte Carlo
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MonteCarloResult:
    simulations: int
    method: str
    seed: int
    returns: tuple[float, ...] = field(repr=False, default=())
    drawdowns: tuple[float, ...] = field(repr=False, default=())

    def percentile(self, values: Sequence[float], pct: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        k = max(0, min(len(ordered) - 1, round((pct / 100.0) * (len(ordered) - 1))))
        return ordered[k]

    @property
    def median_return(self) -> float:
        return statistics.median(self.returns) if self.returns else 0.0

    @property
    def median_drawdown(self) -> float:
        return statistics.median(self.drawdowns) if self.drawdowns else 0.0

    def probability_drawdown_exceeds(self, threshold: float) -> float:
        if not self.drawdowns:
            return 0.0
        return sum(1 for d in self.drawdowns if d > threshold) / len(self.drawdowns)

    @property
    def probability_negative(self) -> float:
        if not self.returns:
            return 0.0
        return sum(1 for r in self.returns if r < 0) / len(self.returns)

    def render(self, *, thresholds: Sequence[float] = (1_000.0, 1_200.0, 2_000.0)) -> str:
        L = ["MONTE CARLO", "-" * 11, ""]
        L.append(f"  Simulations: {self.simulations:,}   method: {self.method}"
                 f"   seed: {self.seed}")
        L.append("")
        L.append(f"  5th percentile return:   ${self.percentile(self.returns, 5):>12,.2f}")
        L.append(f"  Median return:           ${self.median_return:>12,.2f}")
        L.append(f"  95th percentile return:  ${self.percentile(self.returns, 95):>12,.2f}")
        L.append("")
        L.append(f"  5th percentile max DD:   ${self.percentile(self.drawdowns, 5):>12,.2f}")
        L.append(f"  Median max DD:           ${self.median_drawdown:>12,.2f}")
        L.append(f"  95th percentile max DD:  ${self.percentile(self.drawdowns, 95):>12,.2f}")
        L.append("")
        for threshold in thresholds:
            L.append(f"  P(max DD > ${threshold:,.0f}):      "
                     f"{self.probability_drawdown_exceeds(threshold):>8.1%}")
        L.append(f"  P(negative return):      {self.probability_negative:>8.1%}")
        L.append("")
        L.append("  The observed backtest is ONE draw from this distribution.")
        L.append("  Plan against the 95th percentile drawdown, not the median.")
        return "\n".join(L)


def monte_carlo(
    trade_pnls: Sequence[float],
    *,
    simulations: int = 10_000,
    method: str = "shuffle",
    seed: int = 20260912,
) -> MonteCarloResult:
    """Resample the trade sequence to get a distribution of outcomes.

    ``shuffle`` reorders the same trades: total profit is identical every time,
    so it isolates **path** risk -- the drawdown you might have suffered with
    the same results in a different order.

    ``bootstrap`` samples with replacement: totals vary too, which also
    captures the luck in *which* trades occurred.

    Both assume trades are independent. Real trades are not perfectly
    independent -- regimes cluster wins and losses -- so these understate tail
    risk rather than overstate it. That is the safe direction, but it is an
    assumption, not a fact.
    """
    if method not in ("shuffle", "bootstrap"):
        raise ValueError(f"unknown method {method!r}; use shuffle or bootstrap")
    if not trade_pnls:
        return MonteCarloResult(0, method, seed)

    rng = random.Random(seed)
    pnls = list(trade_pnls)
    returns: list[float] = []
    drawdowns: list[float] = []

    for _ in range(simulations):
        if method == "shuffle":
            path = pnls[:]
            rng.shuffle(path)
        else:
            path = [rng.choice(pnls) for _ in range(len(pnls))]

        equity = 0.0
        peak = 0.0
        worst = 0.0
        for pnl in path:
            equity += pnl
            peak = max(peak, equity)
            worst = max(worst, peak - equity)
        returns.append(equity)
        drawdowns.append(worst)

    return MonteCarloResult(
        simulations=simulations,
        method=method,
        seed=seed,
        returns=tuple(returns),
        drawdowns=tuple(drawdowns),
    )
