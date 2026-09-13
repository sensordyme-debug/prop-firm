"""MNQ contract months, expiry and the quarterly roll. Pure arithmetic.

WHY THIS MATTERS IN TWO SEPARATE PLACES
---------------------------------------
**Live:** ``get_instrument("MNQ")`` resolves to *a* contract. After the roll
date, volume and open interest have moved to the next quarter, and the old
front month becomes thin. Trading the stale contract means wide spreads and
bad fills on an account where a few ticks of slippage is the whole edge.

**Backtest:** bar history splices contracts together. The next quarter trades
at a different price from the expiring one -- carry and dividends, not
sentiment -- so a naive continuous series shows a step change at every roll.
A breakout strategy reads that step as a signal. It is a phantom: nobody could
have traded it, because it is two different instruments printed side by side.
:mod:`data` flags a series that spans a roll for exactly this reason.

CONVENTION, AND ITS STATUS
--------------------------
  * Quarterly only: March (H), June (M), September (U), December (Z).
  * Expiry: the **third Friday** of the contract month, cash-settled to the
    index's Special Opening Quotation.
  * Roll: the **Monday prior to the third Friday**. After it, the second
    nearest month is the lead month.

Cross-checked against two independent sources; CME's own roll-dates page timed
out, so this carries the same status as the holiday table --
``ROLL_CONVENTION_CME_VERIFIED`` is False. Note the roll is NOT "eight days
before expiry" or "the second Thursday", both of which are commonly repeated
and both of which give the wrong date.

WHAT WE DO ABOUT IT
-------------------
We do not trade expiry day at all. Settlement is to the OPENING quote, so the
session that matters has already happened before our 09:30 window even starts,
and the remaining session is an artefact. Standing aside costs four sessions a
year and removes the need to reason about any of it -- the same trade made for
holiday dates.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Final

from market_calendar import market_is_open

__all__ = [
    "MONTH_CODES",
    "QUARTERLY_MONTHS",
    "ROLL_CONVENTION_CME_VERIFIED",
    "ContractMonth",
    "contract_symbol",
    "expiry_date",
    "front_month",
    "is_expiry_date",
    "is_roll_date",
    "next_roll_on_or_after",
    "roll_date",
    "rolls_between",
    "third_friday",
]

QUARTERLY_MONTHS: Final[tuple[int, ...]] = (3, 6, 9, 12)
MONTH_CODES: Final[dict[int, str]] = {3: "H", 6: "M", 9: "U", 12: "Z"}

# Cross-checked against two independent sources. CME's own roll-dates page was
# unreachable, so this is not verified at source -- same status as the holiday
# calendar, and treated with the same caution.
ROLL_CONVENTION_CME_VERIFIED: Final[bool] = False


@dataclass(frozen=True, order=True)
class ContractMonth:
    year: int
    month: int

    def __post_init__(self) -> None:
        if self.month not in QUARTERLY_MONTHS:
            raise ValueError(
                f"{self.month} is not a quarterly equity-index month; "
                f"MNQ lists only {sorted(QUARTERLY_MONTHS)}"
            )

    @property
    def code(self) -> str:
        """CME month code, e.g. 'Z' for December."""
        return MONTH_CODES[self.month]

    @property
    def expiry(self) -> date:
        return expiry_date(self.year, self.month)

    @property
    def roll(self) -> date:
        return roll_date(self.year, self.month)

    def symbol(self, root: str = "MNQ") -> str:
        return contract_symbol(self, root=root)

    def next_quarter(self) -> ContractMonth:
        index = QUARTERLY_MONTHS.index(self.month)
        if index == len(QUARTERLY_MONTHS) - 1:
            return ContractMonth(self.year + 1, QUARTERLY_MONTHS[0])
        return ContractMonth(self.year, QUARTERLY_MONTHS[index + 1])

    def __str__(self) -> str:
        return f"{self.year}-{self.month:02d} ({self.code})"


def third_friday(year: int, month: int) -> date:
    """The third Friday of a month.

    Counted forward from the first Friday rather than back from the end, so
    the arithmetic does not depend on month length.
    """
    first = date(year, month, 1)
    days_to_friday = (4 - first.weekday()) % 7  # Monday=0 ... Friday=4
    return first + timedelta(days=days_to_friday + 14)


def expiry_date(year: int, month: int) -> date:
    """Expiry: the third Friday, stepped back if the market is shut that day.

    Uses ``market_is_open``, not ``is_open``: the question is what the
    EXCHANGE does. A holiday half-day is still a trading day for settlement
    purposes even though we abstain from it.
    """
    candidate = third_friday(year, month)
    for _ in range(7):
        if market_is_open(candidate):
            return candidate
        candidate -= timedelta(days=1)
    return third_friday(year, month)  # outside calendar coverage: unadjusted


def roll_date(year: int, month: int) -> date:
    """The Monday prior to the third Friday -- CME's equity-index convention.

    Not "eight days before expiry" and not "the second Thursday". Both are
    widely repeated and both land on the wrong day.

    Deliberately computed from the unadjusted third Friday, so that a holiday
    shifting expiry does not silently shift the roll with it.
    """
    return third_friday(year, month) - timedelta(days=4)


def front_month(on: date) -> ContractMonth:
    """The lead contract on a given date -- the one carrying the volume.

    On and after the roll date, the lead month is the NEXT quarter, even
    though the near contract has not expired yet. That is the whole point of
    the roll: liquidity moves before expiry, and following it late means
    trading a thin book.
    """
    for year in (on.year, on.year + 1):
        for month in QUARTERLY_MONTHS:
            candidate = ContractMonth(year, month)
            if on < roll_date(year, month):
                return candidate
    raise ValueError(f"could not resolve a front month for {on}")


def contract_symbol(month: ContractMonth, root: str = "MNQ") -> str:
    """e.g. ContractMonth(2026, 12) -> 'MNQZ26'."""
    return f"{root}{month.code}{month.year % 100:02d}"


def is_expiry_date(on: date) -> bool:
    for year in (on.year - 1, on.year, on.year + 1):
        for month in QUARTERLY_MONTHS:
            if expiry_date(year, month) == on:
                return True
    return False


def is_roll_date(on: date) -> bool:
    for year in (on.year - 1, on.year, on.year + 1):
        for month in QUARTERLY_MONTHS:
            if roll_date(year, month) == on:
                return True
    return False


def next_roll_on_or_after(on: date) -> date:
    for year in (on.year, on.year + 1):
        for month in QUARTERLY_MONTHS:
            candidate = roll_date(year, month)
            if candidate >= on:
                return candidate
    raise ValueError(f"could not find a roll date on or after {on}")


def rolls_between(start: date, end: date) -> list[date]:
    """Roll dates within [start, end].

    Used by the data layer: a bar series spanning one of these may have
    spliced two contracts together, and the price step at the join is not a
    market move.
    """
    found: list[date] = []
    for year in range(start.year, end.year + 2):
        for month in QUARTERLY_MONTHS:
            candidate = roll_date(year, month)
            if start <= candidate <= end:
                found.append(candidate)
    return sorted(found)
