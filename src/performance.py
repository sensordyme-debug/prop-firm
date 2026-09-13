"""Performance analytics over a completed backtest. Pure arithmetic.

WHAT "UNAVAILABLE" MEANS HERE
-----------------------------
Several statistics are undefined on small or degenerate samples: a Sharpe ratio
from four daily returns, a Calmar with no drawdown, a profit factor with no
losses. Every one of those returns an explicit :class:`Unavailable` with a
reason rather than a number.

That is the whole design point. A fabricated 4.2 Sharpe from six trades is not
a harmless placeholder -- it is the number someone quotes back later as
evidence. ``None`` cannot be quoted.

NET, NOT GROSS
--------------
Every return feeding Sharpe, Sortino and Calmar is **net of commission and
modelled slippage**, because that is what the account would actually have
experienced. Computing them from gross P&L inflates every risk-adjusted figure
by exactly the amount the costs would have hurt.

ANNUALISATION
-------------
Daily returns are annualised with 252 trading days. The assumption is stated in
the report rather than buried, because it is the difference between a Sharpe of
1.0 and 2.0 depending on what someone assumed.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

__all__ = [
    "TRADING_DAYS_PER_YEAR",
    "ExcursionStats",
    "PerformanceReport",
    "PortfolioStats",
    "RiskAdjustedStats",
    "TradeStats",
    "Unavailable",
    "analyse",
    "excursion_stats",
]

TRADING_DAYS_PER_YEAR = 252

# Below this many observations, a risk-adjusted ratio is noise wearing a number.
MIN_RETURNS_FOR_RATIO = 20
MIN_DAYS_FOR_ANNUALISATION = 60


@dataclass(frozen=True)
class Unavailable:
    """A statistic that cannot honestly be computed, and why."""

    reason: str

    def __bool__(self) -> bool:
        return False

    def __str__(self) -> str:
        return f"n/a ({self.reason})"


Maybe = float | Unavailable


def _fmt(value: Maybe, spec: str = ",.2f", prefix: str = "") -> str:
    if isinstance(value, Unavailable):
        return str(value)
    return f"{prefix}{value:{spec}}"


# ---------------------------------------------------------------------------
# Trade statistics
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExcursionStats:
    """MAE / MFE aggregates.

    Reported, never auto-optimised. Fitting a stop to the observed adverse
    excursions of one sample is fitting to that sample: the stop that would
    have survived every past loser is the stop that survives exactly those
    losers. These are here to be READ by a human deciding whether the stop
    methodology is sane, which is a different activity from tuning.
    """

    count: int = 0
    average_mae: Maybe = field(default_factory=lambda: Unavailable("no trades"))
    median_mae: Maybe = field(default_factory=lambda: Unavailable("no trades"))
    max_mae: Maybe = field(default_factory=lambda: Unavailable("no trades"))
    average_mfe: Maybe = field(default_factory=lambda: Unavailable("no trades"))
    median_mfe: Maybe = field(default_factory=lambda: Unavailable("no trades"))
    max_mfe: Maybe = field(default_factory=lambda: Unavailable("no trades"))
    edge_ratio: Maybe = field(default_factory=lambda: Unavailable("no trades"))
    mae_percentiles: tuple[tuple[int, float], ...] = ()
    mfe_percentiles: tuple[tuple[int, float], ...] = ()


def _percentiles(values: Sequence[float]) -> tuple[tuple[int, float], ...]:
    if not values:
        return ()
    ordered = sorted(values)
    out = []
    for pct in (25, 50, 75, 90, 95):
        k = max(0, min(len(ordered) - 1,
                       round((pct / 100.0) * (len(ordered) - 1))))
        out.append((pct, ordered[k]))
    return tuple(out)


def excursion_stats(trades: Sequence[Any]) -> ExcursionStats:
    usable = [t for t in trades if hasattr(t, "mae")]
    if not usable:
        return ExcursionStats()
    maes = [t.mae for t in usable]
    mfes = [t.mfe for t in usable]
    total_mae = sum(maes)
    return ExcursionStats(
        count=len(usable),
        average_mae=statistics.fmean(maes),
        median_mae=statistics.median(maes),
        max_mae=max(maes),
        average_mfe=statistics.fmean(mfes),
        median_mfe=statistics.median(mfes),
        max_mfe=max(mfes),
        edge_ratio=(sum(mfes) / total_mae if total_mae > 0
                    else Unavailable("no adverse excursion recorded")),
        mae_percentiles=_percentiles(maes),
        mfe_percentiles=_percentiles(mfes),
    )


@dataclass(frozen=True)
class TradeStats:
    total: int = 0
    winners: int = 0
    losers: int = 0
    scratches: int = 0

    win_rate: Maybe = field(default_factory=lambda: Unavailable("no trades"))
    average_trade: Maybe = field(default_factory=lambda: Unavailable("no trades"))
    expectancy: Maybe = field(default_factory=lambda: Unavailable("no trades"))

    average_winner: Maybe = field(default_factory=lambda: Unavailable("no winners"))
    average_loser: Maybe = field(default_factory=lambda: Unavailable("no losers"))
    median_winner: Maybe = field(default_factory=lambda: Unavailable("no winners"))
    median_loser: Maybe = field(default_factory=lambda: Unavailable("no losers"))
    largest_winner: Maybe = field(default_factory=lambda: Unavailable("no winners"))
    largest_loser: Maybe = field(default_factory=lambda: Unavailable("no losers"))

    reward_risk: Maybe = field(default_factory=lambda: Unavailable("insufficient data"))
    profit_factor: Maybe = field(default_factory=lambda: Unavailable("no losses"))
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    net_profit: float = 0.0
    commission: float = 0.0
    slippage_modelled: float = 0.0

    average_duration: Maybe = field(default_factory=lambda: Unavailable("no trades"))
    median_duration: Maybe = field(default_factory=lambda: Unavailable("no trades"))
    longest_duration: Maybe = field(default_factory=lambda: Unavailable("no trades"))
    shortest_duration: Maybe = field(default_factory=lambda: Unavailable("no trades"))

    max_consecutive_losses: int = 0
    max_consecutive_wins: int = 0


def _trade_stats(trades: Sequence[Any], slippage_cost: float = 0.0) -> TradeStats:
    if not trades:
        return TradeStats()

    nets = [t.net for t in trades]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n < 0]
    scratches = len(nets) - len(wins) - len(losses)

    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))

    durations = [
        (t.exit_time - t.entry_time).total_seconds() / 60.0
        for t in trades
        if getattr(t, "exit_time", None) and getattr(t, "entry_time", None)
    ]

    streak_l = worst_l = streak_w = best_w = 0
    for n in nets:
        if n < 0:
            streak_l += 1
            streak_w = 0
            worst_l = max(worst_l, streak_l)
        elif n > 0:
            streak_w += 1
            streak_l = 0
            best_w = max(best_w, streak_w)
        else:
            streak_l = streak_w = 0

    avg_win = statistics.fmean(wins) if wins else None
    avg_loss = statistics.fmean(losses) if losses else None

    return TradeStats(
        total=len(trades),
        winners=len(wins),
        losers=len(losses),
        scratches=scratches,
        win_rate=len(wins) / len(nets),
        average_trade=statistics.fmean(nets),
        expectancy=statistics.fmean(nets),
        average_winner=avg_win if avg_win is not None else Unavailable("no winners"),
        average_loser=avg_loss if avg_loss is not None else Unavailable("no losers"),
        median_winner=statistics.median(wins) if wins else Unavailable("no winners"),
        median_loser=statistics.median(losses) if losses else Unavailable("no losers"),
        largest_winner=max(wins) if wins else Unavailable("no winners"),
        largest_loser=min(losses) if losses else Unavailable("no losers"),
        reward_risk=(
            abs(avg_win / avg_loss) if avg_win is not None and avg_loss
            else Unavailable("need both a winner and a loser")
        ),
        profit_factor=(
            gross_profit / gross_loss if gross_loss > 0
            else Unavailable("no losing trades; ratio is undefined, not infinite")
        ),
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        net_profit=sum(nets),
        commission=sum(t.commission for t in trades),
        slippage_modelled=slippage_cost,
        average_duration=statistics.fmean(durations) if durations
        else Unavailable("no trades"),
        median_duration=statistics.median(durations) if durations
        else Unavailable("no trades"),
        longest_duration=max(durations) if durations else Unavailable("no trades"),
        shortest_duration=min(durations) if durations else Unavailable("no trades"),
        max_consecutive_losses=worst_l,
        max_consecutive_wins=best_w,
    )


# ---------------------------------------------------------------------------
# Portfolio statistics
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PortfolioStats:
    starting_balance: float
    ending_balance: float
    peak_equity: float
    total_return: float                 # fraction, not percent
    max_drawdown: float                 # dollars, from the running peak
    max_drawdown_pct: float
    average_drawdown: float
    max_drawdown_duration_days: int
    recovery_days: Maybe
    daily_returns: tuple[float, ...] = ()
    weekly_returns: tuple[float, ...] = ()
    monthly_returns: tuple[float, ...] = ()
    trading_days: int = 0
    max_consecutive_losing_days: int = 0


def _bucket(daily: Sequence[tuple[date, float]], key) -> list[float]:
    out: dict[Any, float] = {}
    for day, pnl in daily:
        out[key(day)] = out.get(key(day), 0.0) + pnl
    return [out[k] for k in sorted(out)]


def _portfolio_stats(
    equity_curve: Sequence[tuple[datetime, float]],
    daily_pnl: Sequence[tuple[date, float]],
    starting_balance: float,
) -> PortfolioStats:
    equities = [e for _, e in equity_curve] or [starting_balance]
    peak = starting_balance
    worst = 0.0
    drawdowns: list[float] = []
    dd_start: datetime | None = None
    longest_dd = timedelta(0)
    recovery: float | Unavailable = Unavailable("never drew down")

    for when, equity in equity_curve:
        if equity >= peak:
            if dd_start is not None:
                longest_dd = max(longest_dd, when - dd_start)
                if isinstance(recovery, Unavailable):
                    recovery = (when - dd_start).total_seconds() / 86400.0
                dd_start = None
            peak = equity
        else:
            if dd_start is None:
                dd_start = when
            drawdowns.append(peak - equity)
        worst = max(worst, peak - equity)

    if dd_start is not None and equity_curve:
        longest_dd = max(longest_dd, equity_curve[-1][0] - dd_start)
        recovery = Unavailable("still in drawdown at the end of the sample")

    ending = equities[-1]
    streak = worst_days = 0
    for _, pnl in daily_pnl:
        if pnl < 0:
            streak += 1
            worst_days = max(worst_days, streak)
        else:
            streak = 0

    return PortfolioStats(
        starting_balance=starting_balance,
        ending_balance=ending,
        peak_equity=max([*equities, starting_balance]),
        total_return=(ending - starting_balance) / starting_balance,
        max_drawdown=worst,
        max_drawdown_pct=worst / starting_balance if starting_balance else 0.0,
        average_drawdown=statistics.fmean(drawdowns) if drawdowns else 0.0,
        max_drawdown_duration_days=longest_dd.days,
        recovery_days=recovery,
        daily_returns=tuple(p / starting_balance for _, p in daily_pnl),
        weekly_returns=tuple(
            p / starting_balance for p in _bucket(daily_pnl, lambda d: d.isocalendar()[:2])
        ),
        monthly_returns=tuple(
            p / starting_balance for p in _bucket(daily_pnl, lambda d: (d.year, d.month))
        ),
        trading_days=len(daily_pnl),
        max_consecutive_losing_days=worst_days,
    )


# ---------------------------------------------------------------------------
# Risk-adjusted
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RiskAdjustedStats:
    sharpe: Maybe
    sortino: Maybe
    calmar: Maybe
    annualised_return: Maybe
    return_volatility: Maybe
    downside_deviation: Maybe
    basis: str = (
        f"daily net returns, annualised at {TRADING_DAYS_PER_YEAR} trading days, "
        "risk-free rate 0%"
    )


def _risk_adjusted(portfolio: PortfolioStats) -> RiskAdjustedStats:
    returns = list(portfolio.daily_returns)
    n = len(returns)

    if n < MIN_RETURNS_FOR_RATIO:
        why = Unavailable(
            f"only {n} daily returns; need {MIN_RETURNS_FOR_RATIO}+ for a "
            "ratio that means anything"
        )
        return RiskAdjustedStats(why, why, why, why, why, why)

    mean = statistics.fmean(returns)
    stdev = statistics.stdev(returns) if n > 1 else 0.0
    downside = [r for r in returns if r < 0]
    dd_dev = (
        math.sqrt(statistics.fmean([r * r for r in downside])) if downside else 0.0
    )

    annual_factor = math.sqrt(TRADING_DAYS_PER_YEAR)
    sharpe: Maybe = (
        (mean / stdev) * annual_factor if stdev > 0
        else Unavailable("zero return volatility")
    )
    sortino: Maybe = (
        (mean / dd_dev) * annual_factor if dd_dev > 0
        else Unavailable("no negative daily returns; downside deviation is zero")
    )

    annualised: Maybe
    if portfolio.trading_days < MIN_DAYS_FOR_ANNUALISATION:
        annualised = Unavailable(
            f"only {portfolio.trading_days} trading days; annualising a sample "
            f"this short would extrapolate noise (need {MIN_DAYS_FOR_ANNUALISATION}+)"
        )
    else:
        years = portfolio.trading_days / TRADING_DAYS_PER_YEAR
        growth = 1.0 + portfolio.total_return
        annualised = (growth ** (1 / years)) - 1.0 if growth > 0 else -1.0

    calmar: Maybe
    if isinstance(annualised, Unavailable):
        calmar = Unavailable("annualised return unavailable")
    elif portfolio.max_drawdown_pct <= 0:
        calmar = Unavailable("no drawdown; ratio is undefined, not infinite")
    else:
        calmar = annualised / portfolio.max_drawdown_pct

    return RiskAdjustedStats(
        sharpe=sharpe,
        sortino=sortino,
        calmar=calmar,
        annualised_return=annualised,
        return_volatility=stdev * annual_factor,
        downside_deviation=dd_dev * annual_factor if dd_dev > 0
        else Unavailable("no negative returns"),
    )


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PerformanceReport:
    strategy: str
    symbol: str
    timeframe: str
    period_start: str
    period_end: str
    trades: TradeStats
    longs: TradeStats
    shorts: TradeStats
    portfolio: PortfolioStats
    risk: RiskAdjustedStats
    excursions: ExcursionStats = field(default_factory=ExcursionStats)
    excursions_winners: ExcursionStats = field(default_factory=ExcursionStats)
    excursions_losers: ExcursionStats = field(default_factory=ExcursionStats)
    excursions_long: ExcursionStats = field(default_factory=ExcursionStats)
    excursions_short: ExcursionStats = field(default_factory=ExcursionStats)
    gates: dict[str, Any] = field(default_factory=dict)
    assumptions: tuple[str, ...] = ()
    governor_halts: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Machine-readable. ``Unavailable`` serialises as null plus a reason.

        Walks the structure by hand rather than using ``dataclasses.asdict``:
        asdict recurses into nested dataclasses first, so an ``Unavailable``
        would arrive already flattened to ``{"reason": ...}`` and become
        indistinguishable from a real value. Checking the type BEFORE
        recursing is what keeps "unavailable" from silently looking available.
        """
        import dataclasses

        def clean(value: Any) -> Any:
            if isinstance(value, Unavailable):
                return {"value": None, "unavailable_because": value.reason}
            if dataclasses.is_dataclass(value) and not isinstance(value, type):
                return {
                    f.name: clean(getattr(value, f.name))
                    for f in dataclasses.fields(value)
                }
            if isinstance(value, dict):
                return {k: clean(v) for k, v in value.items()}
            if isinstance(value, (list, tuple)):
                return [clean(v) for v in value]
            return value

        return clean(self)

    def to_json(self, indent: int = 2) -> str:
        import json

        return json.dumps(self.to_dict(), indent=indent, default=str)

    def render(self) -> str:
        t, p, r = self.trades, self.portfolio, self.risk
        L: list[str] = []
        add = L.append

        add("=" * 60)
        add("BACKTEST PERFORMANCE REPORT")
        add("=" * 60)
        add("")
        add(f"Period:     {self.period_start} -> {self.period_end}")
        add(f"Strategy:   {self.strategy}")
        add(f"Symbol:     {self.symbol}")
        add(f"Timeframe:  {self.timeframe}")

        add("")
        add("ASSUMPTIONS")
        add("-" * 13)
        for line in self.assumptions:
            add(f"  {line}")

        add("")
        add("TRADES")
        add("-" * 6)
        add(f"  Trades:          {t.total}  ({t.winners}W / {t.losers}L)")
        add(f"  Win rate:        {_fmt(t.win_rate, '.1%')}")
        add(f"  Average trade:   {_fmt(t.average_trade, ',.2f', '$')}")
        add(f"  Expectancy:      {_fmt(t.expectancy, ',.2f', '$')}")
        add(f"  Profit factor:   {_fmt(t.profit_factor, '.2f')}")
        add(f"  Reward/risk:     {_fmt(t.reward_risk, '.2f')}")
        add(f"  Avg winner:      {_fmt(t.average_winner, ',.2f', '$')}"
            f"   median {_fmt(t.median_winner, ',.2f', '$')}")
        add(f"  Avg loser:       {_fmt(t.average_loser, ',.2f', '$')}"
            f"   median {_fmt(t.median_loser, ',.2f', '$')}")
        add(f"  Largest winner:  {_fmt(t.largest_winner, ',.2f', '$')}")
        add(f"  Largest loser:   {_fmt(t.largest_loser, ',.2f', '$')}")
        add(f"  Max consec W/L:  {t.max_consecutive_wins} / {t.max_consecutive_losses}")

        add("")
        add("RETURNS")
        add("-" * 7)
        add(f"  Net profit:        {t.net_profit:>12,.2f}")
        add(f"  Gross profit:      {t.gross_profit:>12,.2f}")
        add(f"  Gross loss:        {-t.gross_loss:>12,.2f}")
        add(f"  Total return:      {p.total_return:>12.2%}")
        add(f"  Annualised return: {_fmt(r.annualised_return, '.2%'):>12}")
        add(f"  Start -> end:      {p.starting_balance:,.2f} -> {p.ending_balance:,.2f}")

        add("")
        add("RISK")
        add("-" * 4)
        add(f"  Max drawdown:            ${p.max_drawdown:,.2f} "
            f"({p.max_drawdown_pct:.2%})")
        add(f"  Average drawdown:        ${p.average_drawdown:,.2f}")
        add(f"  Max DD duration:         {p.max_drawdown_duration_days} days")
        add(f"  Recovery:                {_fmt(p.recovery_days, '.1f')} days")
        add(f"  Largest loss:            {_fmt(t.largest_loser, ',.2f', '$')}")
        add(f"  Consecutive losing days: {p.max_consecutive_losing_days}")

        add("")
        add("RISK ADJUSTED")
        add("-" * 13)
        add(f"  Sharpe:   {_fmt(r.sharpe, '.2f')}")
        add(f"  Sortino:  {_fmt(r.sortino, '.2f')}")
        add(f"  Calmar:   {_fmt(r.calmar, '.2f')}")
        add(f"  Basis:    {r.basis}")

        add("")
        add("EXECUTION")
        add("-" * 9)
        add(f"  Commission:       ${t.commission:,.2f}")
        add(f"  Slippage modelled: ${t.slippage_modelled:,.2f}")
        add(f"  Average duration: {_fmt(t.average_duration, '.1f')} min"
            f"   median {_fmt(t.median_duration, '.1f')} min")

        e = self.excursions
        add("")
        add("EXCURSIONS (MAE / MFE)")
        add("-" * 22)
        add(f"  Average MAE:  {_fmt(e.average_mae, ',.2f', '$'):>12}"
            f"   median {_fmt(e.median_mae, ',.2f', '$')}")
        add(f"  Average MFE:  {_fmt(e.average_mfe, ',.2f', '$'):>12}"
            f"   median {_fmt(e.median_mfe, ',.2f', '$')}")
        add(f"  Worst MAE:    {_fmt(e.max_mae, ',.2f', '$'):>12}")
        add(f"  Edge ratio:   {_fmt(e.edge_ratio, '.2f'):>12}  (total MFE / total MAE)")
        for name, grp in (("winners", self.excursions_winners),
                          ("losers", self.excursions_losers),
                          ("long", self.excursions_long),
                          ("short", self.excursions_short)):
            add(f"  {name:<9s} n={grp.count:<4d} "
                f"MAE {_fmt(grp.average_mae, ',.2f', '$')}  "
                f"MFE {_fmt(grp.average_mfe, ',.2f', '$')}")
        add("  Read, not optimised: fitting stops to observed excursions fits "
            "this sample.")

        for label, side in (("LONG", self.longs), ("SHORT", self.shorts)):
            add("")
            add(label)
            add("-" * len(label))
            add(f"  Trades:     {side.total}")
            add(f"  Win rate:   {_fmt(side.win_rate, '.1%')}")
            add(f"  Expectancy: {_fmt(side.expectancy, ',.2f', '$')}")

        add("")
        add("GOVERNOR")
        add("-" * 8)
        if not self.governor_halts:
            add("  No halts.")
        for halt in self.governor_halts:
            add(f"  {halt}")

        add("")
        add("GATES")
        add("-" * 5)
        for name, value in self.gates.items():
            add(f"  {name}: {value}")

        add("")
        add("=" * 60)
        return "\n".join(L)


def analyse(
    result: Any,
    *,
    strategy: str = "unknown",
    symbol: str = "MNQ",
    timeframe: str = "5m",
) -> PerformanceReport:
    """Build a full report from a :class:`backtest.BacktestResult`."""
    trades = list(result.trades)
    costs = result.costs

    # What the modelled slippage cost across the run: adverse on entry, and
    # again on any stop exit. Targets fill at the limit, so they cost nothing.
    slip_per_side = costs.slippage_points * costs.point_value
    stop_exits = sum(1 for t in trades if t.exit_kind == "STOP")
    slippage_cost = slip_per_side * sum(t.size for t in trades) + (
        slip_per_side * sum(t.size for t in trades if t.exit_kind == "STOP")
        if stop_exits else 0.0
    )

    portfolio = _portfolio_stats(
        result.equity_curve, result.daily_pnl, result.starting_balance
    )
    gates = result.passes_gates()

    period_start = (
        result.equity_curve[0][0].strftime("%Y-%m-%d") if result.equity_curve else "-"
    )
    period_end = (
        result.equity_curve[-1][0].strftime("%Y-%m-%d") if result.equity_curve else "-"
    )

    return PerformanceReport(
        strategy=strategy,
        symbol=symbol,
        timeframe=timeframe,
        period_start=period_start,
        period_end=period_end,
        trades=_trade_stats(trades, slippage_cost),
        longs=_trade_stats([t for t in trades if t.side == 1]),
        shorts=_trade_stats([t for t in trades if t.side == -1]),
        portfolio=portfolio,
        risk=_risk_adjusted(portfolio),
        excursions=excursion_stats(trades),
        excursions_winners=excursion_stats([t for t in trades if t.net > 0]),
        excursions_losers=excursion_stats([t for t in trades if t.net < 0]),
        excursions_long=excursion_stats([t for t in trades if t.side == 1]),
        excursions_short=excursion_stats([t for t in trades if t.side == -1]),
        gates={
            "max drawdown": gates.detail.get("drawdown", ""),
            "consecutive losing days": gates.detail.get("consecutive_losing_days", ""),
            "expectancy / trade count": gates.detail.get("expectancy", ""),
            "strategy-caused halts": gates.detail.get("strategy_halts", ""),
            "FINAL VERDICT": "PASS" if gates.passed else "FAIL",
        },
        assumptions=(
            *costs.describe(),
            "entry fill      next bar's OPEN, never the signal bar's close",
            "both touched    STOP assumed first, ALWAYS",
            f"bar timestamps  {result.timestamp_convention.value}-labelled",
            "ratios          computed on NET returns, after costs",
        ),
        governor_halts=tuple(
            f"{h.trading_date}  {h.code}" for h in result.halts
        ),
    )
