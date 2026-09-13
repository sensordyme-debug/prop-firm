"""Research runners: provenance, cost stress, controlled sweeps, regimes.

Extends :mod:`research` (which holds the primitives) with the parts that
actually run an evaluation. Kept separate so the primitives stay small and
independently testable.

THE GOVERNING IDEA
------------------
Every function here is designed to make a strategy look WORSE than a naive
backtest would, or to refuse to produce a number at all. That is deliberate.
The failure mode this platform exists to prevent is not a strategy that looks
bad; it is a strategy that looked good once.
"""

from __future__ import annotations

import random
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from research import ParameterResult

__all__ = [
    "MAX_SWEEP_COMBINATIONS",
    "MAX_TUNABLE_PARAMETERS",
    "CostScenario",
    "CostSensitivityRow",
    "RegimeBucket",
    "RunProvenance",
    "build_grid",
    "classify_regime_stability",
    "code_version",
    "consecutive_loss_distribution",
    "cost_scenarios",
    "cost_sensitivity_table",
    "flag_fragile",
]


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunProvenance:
    """Everything needed to reproduce a result, or to refuse to compare two.

    Without this, numbers from different datasets, cost assumptions or code
    versions sit in the same table looking comparable. They are not, and by the
    time anyone notices the comparison has already been acted on.
    """

    strategy: str
    strategy_version: str
    parameters: dict[str, Any]
    config_hash: str
    dataset_hash: str
    code_version: str
    commission: float
    slippage_ticks: float
    seed: int | None = None
    created_at: str = ""

    def line(self) -> str:
        return (
            f"{self.strategy}@{self.strategy_version}  cfg={self.config_hash}  "
            f"data={self.dataset_hash}  code={self.code_version}  "
            f"comm={self.commission}  slip={self.slippage_ticks}t"
        )

    @staticmethod
    def now() -> str:
        return datetime.now(UTC).isoformat()


def code_version() -> str:
    """Short git commit, or ``unknown``. Never fabricated."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# Cost and slippage stress
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CostScenario:
    name: str
    commission_per_round_turn: float
    slippage_ticks: float
    note: str = ""


def cost_scenarios(base_commission: float = 1.82) -> tuple[CostScenario, ...]:
    """Scenarios spanning diagnostic-optimistic to punishing.

    BASE is already pessimistic at 2 ticks adverse. The others are not there to
    find a flattering number -- they are there to locate where the edge dies. A
    strategy that only works at ZERO_COST was never an edge, it was a fill
    assumption wearing one.
    """
    return (
        CostScenario("ZERO_COST", 0.0, 0.0, "diagnostic only, never realistic"),
        CostScenario("LOW", base_commission, 1.0, "optimistic fills"),
        CostScenario("BASE", base_commission, 2.0, "the standing assumption"),
        CostScenario("HIGH", base_commission, 3.0, "busy tape"),
        CostScenario("STRESS", base_commission * 2, 5.0,
                     "thin liquidity and doubled commission"),
    )


@dataclass(frozen=True)
class CostSensitivityRow:
    scenario: str
    net_profit: float
    max_drawdown: float
    trades: int
    expectancy: float
    passed_gates: bool


def cost_sensitivity_table(rows: Sequence[CostSensitivityRow]) -> str:
    if not rows:
        return "  (no scenarios run)"
    lines = [
        f"  {'SCENARIO':<12s}{'Net':>12s}{'MaxDD':>12s}{'Expectancy':>13s}"
        f"{'Trades':>8s}{'Gates':>7s}",
        "  " + "-" * 64,
    ]
    for r in rows:
        lines.append(
            f"  {r.scenario:<12s}{r.net_profit:>12,.0f}{r.max_drawdown:>12,.0f}"
            f"{r.expectancy:>13,.2f}{r.trades:>8d}"
            f"{('PASS' if r.passed_gates else 'fail'):>7s}"
        )
    realistic = [r for r in rows if r.scenario != "ZERO_COST"]
    survivors = [r for r in realistic if r.passed_gates]
    lines.append("")
    if not survivors:
        lines.append("  Edge does not survive ANY realistic cost assumption.")
    else:
        lines.append(
            f"  Survives {len(survivors)} of {len(realistic)} realistic "
            "scenarios. An edge that dies between BASE and HIGH is a fill "
            "assumption, not an edge."
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Controlled parameter sweeps
# ---------------------------------------------------------------------------


MAX_TUNABLE_PARAMETERS = 2
MAX_SWEEP_COMBINATIONS = 64


def build_grid(
    ranges: dict[str, Sequence[Any]],
    *,
    max_parameters: int = MAX_TUNABLE_PARAMETERS,
    max_combinations: int = MAX_SWEEP_COMBINATIONS,
) -> list[dict[str, Any]]:
    """Expand explicit ranges into combinations, refusing to explode.

    Two limits, both deliberate:

    * More than ``max_parameters`` free dimensions is curve-fitting with extra
      steps. Each additional parameter buys in-sample performance that does not
      survive out of sample.
    * An unbounded grid quietly becomes an optimiser over a single dataset,
      which is the exact failure this module exists to prevent.

    Both RAISE rather than truncate. Silently testing a subset would make the
    reported best depend on iteration order, which is worse than refusing.
    """
    if not ranges:
        return [{}]
    if len(ranges) > max_parameters:
        raise ValueError(
            f"{len(ranges)} tunable parameters requested ({sorted(ranges)}), "
            f"limit is {max_parameters}. Every extra free parameter buys "
            "in-sample performance that does not survive out of sample."
        )
    total = 1
    for values in ranges.values():
        total *= max(1, len(values))
    if total > max_combinations:
        raise ValueError(
            f"{total} combinations exceeds the {max_combinations} cap. Narrow "
            "the ranges deliberately rather than searching harder."
        )

    combos: list[dict[str, Any]] = [{}]
    for key in sorted(ranges):
        combos = [{**c, key: v} for c in combos for v in ranges[key]]
    return combos


def flag_fragile(results: Sequence[ParameterResult]) -> list[str]:
    """Name parameter sets that look good only in isolation.

    A single winner surrounded by failures is a spike, not a region. Spikes do
    not survive out of sample, because there is no reason the next period's
    optimum sits on the same point. Reported as a warning rather than filtered
    out, so a human sees what was flagged and why.
    """
    if len(results) < 3:
        return []
    passing = [r for r in results if r.passed_gates]
    warnings: list[str] = []

    if not passing:
        return []
    if len(passing) == 1 and len(results) >= 4:
        warnings.append(
            f"FRAGILE: exactly 1 of {len(results)} parameter sets clears the "
            f"gates ({passing[0].params}). An isolated winner is a spike, not "
            "a region -- check its neighbours before believing it."
        )

    profits = sorted(r.net_profit for r in results)
    median = profits[len(profits) // 2]
    if median > 0:
        for r in passing:
            if r.net_profit > median * 3:
                warnings.append(
                    f"FRAGILE: {r.params} returns {r.net_profit:,.0f} against a "
                    f"sweep median of {median:,.0f}. An outlier that large is "
                    "usually a coincidence, not a discovery."
                )
    return warnings


# ---------------------------------------------------------------------------
# Monte Carlo extension: streak risk
# ---------------------------------------------------------------------------


def consecutive_loss_distribution(
    trade_pnls: Sequence[float],
    *,
    simulations: int = 10_000,
    seed: int = 20260912,
) -> dict[int, float]:
    """P(longest losing streak >= k) under reshuffling.

    Answers the question that actually breaks people. Not "what is the
    drawdown" but "how many losers in a row should I expect to sit through
    without concluding the system is broken". A trader who abandons a working
    system during a normal streak has the same outcome as one with no system.
    """
    if not trade_pnls:
        return {}
    rng = random.Random(seed)
    pnls = list(trade_pnls)
    streaks: list[int] = []
    for _ in range(simulations):
        path = pnls[:]
        rng.shuffle(path)
        worst = run = 0
        for pnl in path:
            run = run + 1 if pnl < 0 else 0
            worst = max(worst, run)
        streaks.append(worst)

    longest = max(streaks) if streaks else 0
    return {
        k: sum(1 for s in streaks if s >= k) / len(streaks)
        for k in range(1, min(16, longest + 1))
    }


# ---------------------------------------------------------------------------
# Regime analysis
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RegimeBucket:
    name: str
    trades: int
    net_profit: float
    expectancy: float
    win_rate: float


MIN_TRADES_PER_REGIME = 20


def classify_regime_stability(buckets: Sequence[RegimeBucket]) -> str:
    """ROBUST / REGIME-DEPENDENT / UNSTABLE / INSUFFICIENT DATA.

    A strategy profitable in one regime and ruinous in another is not a
    strategy, it is a bet that the regime persists -- a different and much
    harder claim than the one its backtest appears to make.

    Buckets with too few trades are excluded rather than counted: a regime with
    four trades tells you nothing, and including it would let noise decide the
    classification.
    """
    usable = [b for b in buckets if b.trades >= MIN_TRADES_PER_REGIME]
    if len(usable) < 2:
        return "INSUFFICIENT DATA"
    positive = [b for b in usable if b.expectancy > 0]
    if len(positive) == len(usable):
        return "ROBUST"
    if not positive:
        return "UNSTABLE"
    return "REGIME-DEPENDENT"
