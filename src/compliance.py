"""Combine and payout compliance. Pure arithmetic over a history of sessions.

WHY THIS EXISTS
---------------
Hitting the profit target is not the same as passing, and being profitable is
not the same as being payable. Topstep gates both on a **consistency ratio**:

  * **Combine:** your best single day should stay below 55% of the profit
    target, "to avoid increasing your Consistency Target".
  * **Payout, Consistency path:** your largest single day must be at or below
    40% of total net profit.

Both are verified from Topstep's own help centre (see FIRM_RULES.md). Neither
is enforced by the governor, because neither is a risk limit — you cannot
breach them by losing. You breach them by winning too much on one day, which
is exactly the failure a risk system is blind to.

THE NUMBER THAT MATTERS
-----------------------
Rearranging the payout rule: with a best day of ``B``, you cannot be paid out
until total net profit reaches ``B / 0.40``, i.e. **2.5x your best day**. One
outsized session does not just fail to help, it raises the bar for every
session after it.

That is the whole argument for a daily profit cap. Stopping at $500 is not
leaving money on the table; it is keeping the ratio reachable. A $1,500 day
needs $3,750 of total profit before a single dollar can be withdrawn.

:func:`profit_needed_for_consistency` computes that gap, and
:func:`Config`-level validation refuses a daily target that could breach the
Combine rule in a single session.

PURE
----
No I/O, no clock, no SDK. A session's net P&L arrives as data; where it came
from is the caller's problem.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Final, Sequence

__all__ = [
    "COMBINE_PROFIT_TARGET",
    "COMBINE_CONSISTENCY_PCT",
    "PAYOUT_CONSISTENCY_PCT",
    "WINNING_DAY_MINIMUM",
    "STANDARD_PATH_WINNING_DAYS",
    "CONSISTENCY_PATH_TRADING_DAYS",
    "PAYOUT_CAP_STANDARD",
    "PAYOUT_CAP_CONSISTENCY",
    "MINIMUM_PAYOUT_REQUEST",
    "SessionResult",
    "CombineStatus",
    "PayoutStatus",
    "consistency_ratio",
    "profit_needed_for_consistency",
    "max_safe_daily_profit",
    "combine_status",
    "payout_status",
    "validate_daily_target",
]

# --- FIRM_RULES.md, all VERIFIED from Topstep's help centre ----------------
COMBINE_PROFIT_TARGET: Final[float] = 3_000.0
COMBINE_CONSISTENCY_PCT: Final[float] = 0.55
PAYOUT_CONSISTENCY_PCT: Final[float] = 0.40
WINNING_DAY_MINIMUM: Final[float] = 150.0
STANDARD_PATH_WINNING_DAYS: Final[int] = 5
CONSISTENCY_PATH_TRADING_DAYS: Final[int] = 3

# 50K account with the Responsible Trading Advantage (Daily Loss Limit) on,
# which doubles both caps. Per request; there is no lifetime cap.
PAYOUT_CAP_STANDARD: Final[float] = 4_000.0
PAYOUT_CAP_CONSISTENCY: Final[float] = 6_000.0
MINIMUM_PAYOUT_REQUEST: Final[float] = 125.0


@dataclass(frozen=True)
class SessionResult:
    """One completed session's net P&L, as Topstep would score it.

    ``net_pnl`` is net of commissions, matching the "$150+ Net P&L" wording
    of the winning-day rule.
    """

    trading_date: date
    net_pnl: float
    trades: int = 0

    @property
    def is_winning_day(self) -> bool:
        return self.net_pnl >= WINNING_DAY_MINIMUM

    @property
    def is_trading_day(self) -> bool:
        """A day with at least one trade, which the Consistency path counts."""
        return self.trades > 0


# ---------------------------------------------------------------------------
# The consistency arithmetic
# ---------------------------------------------------------------------------


def consistency_ratio(sessions: Sequence[SessionResult]) -> float | None:
    """Best single day as a fraction of total net profit.

    Returns None when there is no profit to take a ratio of: a ratio against
    zero or negative total is undefined, and reporting 0.0 there would read
    as "perfectly consistent" when the truth is "not yet applicable".

    Only PROFITABLE days can be the "largest single day" for this purpose;
    losing days reduce the total, which raises the ratio.
    """
    total = sum(s.net_pnl for s in sessions)
    if total <= 0:
        return None
    best = max((s.net_pnl for s in sessions), default=0.0)
    if best <= 0:
        return None
    return best / total


def profit_needed_for_consistency(
    sessions: Sequence[SessionResult],
    *,
    limit_pct: float = PAYOUT_CONSISTENCY_PCT,
) -> float:
    """Additional net profit required before the ratio comes into line.

    The actionable number. With a best day of B and a limit of 40%, total
    profit must reach B / 0.40 = 2.5B. Returns 0.0 when already compliant.

    Note what this implies and why the daily cap exists: the requirement is
    driven by the LARGEST day, so one outsized session permanently raises the
    bar. Trading more afterwards is the only cure, and every further day risks
    drawdown against an MLL that does not move down.
    """
    if not sessions:
        return 0.0
    total = sum(s.net_pnl for s in sessions)
    best = max((s.net_pnl for s in sessions), default=0.0)
    if best <= 0:
        return 0.0
    required_total = best / limit_pct
    return max(0.0, required_total - total)


def max_safe_daily_profit(
    profit_target: float = COMBINE_PROFIT_TARGET,
    *,
    limit_pct: float = COMBINE_CONSISTENCY_PCT,
) -> float:
    """The largest single-day profit that cannot breach the Combine rule."""
    return profit_target * limit_pct


def validate_daily_target(
    daily_profit_target: float,
    *,
    profit_target: float = COMBINE_PROFIT_TARGET,
    limit_pct: float = COMBINE_CONSISTENCY_PCT,
) -> None:
    """Raise if a daily profit cap could breach consistency in one session.

    The governor's daily target and the Combine consistency rule are not
    independent settings, and nothing else ties them together. Raising the
    daily target past 55% of the profit target makes it possible to fail the
    Combine on a WINNING day, which is not a failure mode anyone expects to
    have to think about.
    """
    ceiling = max_safe_daily_profit(profit_target, limit_pct=limit_pct)
    if daily_profit_target > ceiling:
        raise ValueError(
            f"daily profit target {daily_profit_target:,.2f} exceeds "
            f"{limit_pct:.0%} of the {profit_target:,.2f} profit target "
            f"({ceiling:,.2f}). A single day at this size would breach the "
            "Combine consistency rule -- you would fail by winning."
        )


# ---------------------------------------------------------------------------
# Status reports
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CombineStatus:
    total_profit: float
    best_day: float
    profit_target: float
    consistency_ratio: float | None
    consistency_limit: float
    trading_days: int
    target_met: bool
    consistency_ok: bool

    @property
    def passed(self) -> bool:
        return self.target_met and self.consistency_ok

    @property
    def remaining_to_target(self) -> float:
        return max(0.0, self.profit_target - self.total_profit)

    def report(self) -> list[str]:
        ratio = (
            f"{self.consistency_ratio:.1%}" if self.consistency_ratio is not None
            else "n/a (no net profit yet)"
        )
        return [
            f"  profit            ${self.total_profit:,.2f} of "
            f"${self.profit_target:,.2f}  "
            f"({'MET' if self.target_met else f'${self.remaining_to_target:,.2f} to go'})",
            f"  best day          ${self.best_day:,.2f}",
            f"  consistency       {ratio} of total "
            f"(best day must stay under {self.consistency_limit:.0%} of target = "
            f"${self.profit_target * self.consistency_limit:,.2f})",
            f"  trading days      {self.trading_days}",
            f"  COMBINE           {'PASSED' if self.passed else 'not yet'}",
        ]


def combine_status(
    sessions: Sequence[SessionResult],
    *,
    profit_target: float = COMBINE_PROFIT_TARGET,
    limit_pct: float = COMBINE_CONSISTENCY_PCT,
) -> CombineStatus:
    """Progress toward passing the Combine.

    Consistency here is measured against the PROFIT TARGET, not against total
    profit -- that is what Topstep's Combine wording says, and it differs from
    the payout rule, which is measured against total. Conflating the two gives
    the wrong answer at both stages.
    """
    total = sum(s.net_pnl for s in sessions)
    best = max((s.net_pnl for s in sessions), default=0.0)
    ceiling = profit_target * limit_pct
    return CombineStatus(
        total_profit=total,
        best_day=best,
        profit_target=profit_target,
        consistency_ratio=consistency_ratio(sessions),
        consistency_limit=limit_pct,
        trading_days=sum(1 for s in sessions if s.is_trading_day),
        target_met=total >= profit_target,
        consistency_ok=best <= ceiling,
    )


@dataclass(frozen=True)
class PayoutStatus:
    total_profit: float
    best_day: float
    winning_days: int
    trading_days: int
    ratio: float | None

    standard_eligible: bool
    standard_days_needed: int
    standard_cap: float

    consistency_eligible: bool
    consistency_days_needed: int
    consistency_profit_needed: float
    consistency_cap: float

    @property
    def any_path_eligible(self) -> bool:
        return self.standard_eligible or self.consistency_eligible

    @property
    def best_available_cap(self) -> float:
        """The larger cap among paths currently eligible, else 0."""
        caps = [
            cap for eligible, cap in (
                (self.consistency_eligible, self.consistency_cap),
                (self.standard_eligible, self.standard_cap),
            ) if eligible
        ]
        return max(caps, default=0.0)

    def withdrawable(self, balance: float, starting_balance: float) -> float:
        """What could actually be requested right now.

        Bounded by three separate limits, all of which apply at once: the
        path cap, half the account balance, and the profit actually earned.
        Returns 0.0 below the minimum request.
        """
        if not self.any_path_eligible:
            return 0.0
        amount = min(
            self.best_available_cap,
            balance * 0.5,
            max(0.0, balance - starting_balance),
        )
        return amount if amount >= MINIMUM_PAYOUT_REQUEST else 0.0

    def report(self) -> list[str]:
        ratio = f"{self.ratio:.1%}" if self.ratio is not None else "n/a"
        lines = [
            f"  net profit        ${self.total_profit:,.2f}",
            f"  best day          ${self.best_day:,.2f}  "
            f"({ratio} of total; consistency path allows "
            f"{PAYOUT_CONSISTENCY_PCT:.0%})",
            f"  winning days      {self.winning_days} "
            f"(${WINNING_DAY_MINIMUM:,.0f}+ net)",
            f"  trading days      {self.trading_days}",
            "",
            f"  STANDARD path     "
            f"{'ELIGIBLE' if self.standard_eligible else f'{self.standard_days_needed} more winning day(s)'}"
            f"   cap ${self.standard_cap:,.0f}",
            f"  CONSISTENCY path  "
            f"{'ELIGIBLE' if self.consistency_eligible else 'not yet'}"
            f"   cap ${self.consistency_cap:,.0f}",
        ]
        if not self.consistency_eligible:
            if self.consistency_days_needed > 0:
                lines.append(
                    f"      needs {self.consistency_days_needed} more trading day(s)"
                )
            if self.consistency_profit_needed > 0:
                lines.append(
                    f"      needs ${self.consistency_profit_needed:,.2f} more profit "
                    f"to bring the best day under "
                    f"{PAYOUT_CONSISTENCY_PCT:.0%} of total"
                )
        return lines


def payout_status(
    sessions: Sequence[SessionResult],
    *,
    standard_cap: float = PAYOUT_CAP_STANDARD,
    consistency_cap: float = PAYOUT_CAP_CONSISTENCY,
) -> PayoutStatus:
    """Eligibility under both payout paths, and what each still needs.

    Both are reported rather than one being chosen, because which is better
    depends on the shape of the history: the Consistency path pays more
    ($6,000 vs $4,000) and needs fewer days (3 vs 5), but one outsized session
    can put it out of reach for a long time.
    """
    total = sum(s.net_pnl for s in sessions)
    best = max((s.net_pnl for s in sessions), default=0.0)
    winning = sum(1 for s in sessions if s.is_winning_day)
    trading = sum(1 for s in sessions if s.is_trading_day)

    ratio = consistency_ratio(sessions)
    profit_gap = profit_needed_for_consistency(sessions)

    consistency_days_short = max(0, CONSISTENCY_PATH_TRADING_DAYS - trading)
    consistency_ok = (
        trading >= CONSISTENCY_PATH_TRADING_DAYS
        and total > 0
        and profit_gap <= 0
    )

    return PayoutStatus(
        total_profit=total,
        best_day=best,
        winning_days=winning,
        trading_days=trading,
        ratio=ratio,
        standard_eligible=winning >= STANDARD_PATH_WINNING_DAYS and total > 0,
        standard_days_needed=max(0, STANDARD_PATH_WINNING_DAYS - winning),
        standard_cap=standard_cap,
        consistency_eligible=consistency_ok,
        consistency_days_needed=consistency_days_short,
        consistency_profit_needed=profit_gap,
        consistency_cap=consistency_cap,
    )


def report(
    sessions: Sequence[SessionResult],
    *,
    balance: float | None = None,
    starting_balance: float = 50_000.0,
) -> str:
    """A combined Combine-and-payout status report."""
    combine = combine_status(sessions)
    payout = payout_status(sessions)

    lines = ["=" * 70, "TOPSTEP COMPLIANCE", "=" * 70, "", "COMBINE"]
    lines += combine.report()
    lines += ["", "PAYOUT (Express Funded, 50K, Responsible Trading Advantage on)"]
    lines += payout.report()

    if balance is not None:
        amount = payout.withdrawable(balance, starting_balance)
        lines += [
            "",
            f"  withdrawable now  "
            f"{'$' + format(amount, ',.2f') if amount else 'nothing yet'}",
        ]
    lines += [
        "",
        "  Every payout resets the MLL to $0 permanently, which is worth more",
        "  than the cash: the floor stops trailing and locks at the starting",
        "  balance. Take the first one as soon as eligible.",
        "=" * 70,
    ]
    return "\n".join(lines)
