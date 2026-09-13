"""CME equity-index trading calendar. Pure data plus trivial lookups.

Used for MNQ (CME Globex equity index). The calendar is a TABLE, not logic:
holidays and early closes are irregular, so they are enumerated rather than
computed from rules that would drift.

  PROVENANCE WARNING -- READ BEFORE GO-LIVE
  =========================================
  These dates are a best reconstruction, NOT a verified transcription of
  CME's published calendar. ``CALENDAR_VERIFIED`` is False until somebody
  checks every row against:

      https://www.cmegroup.com/tools-information/holiday-calendar.html

  Verify before trading real size, then flip the flag.

WE DO NOT TRADE HOLIDAY DATES AT ALL -- including half-days (DECISIONS.md).
Sources agree on the dates but disagree on the *times* (12:00 vs 12:15 CT, and
whether some days are full closures), and on half-days Topstep sets its own
deadline by Discord announcement rather than by CME's calendar. Rather than
compute a flat time from a contested number, every holiday date is simply not
tradable. It costs ~12 of ~250 sessions on a strategy taking 1-2 trades a day
and removes an entire failure mode. Do not optimise this back in.

Consequently there is no early-close time arithmetic here, and no ``close_et``:
the times are recorded as labels only, so that nothing can compute with them.

Dates outside ``COVERAGE`` are treated as closed, not assumed open. Failing
closed on an unmaintained calendar costs a missed session; failing open costs
a position nobody is watching.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum
from typing import Final

CALENDAR_VERIFIED: Final[bool] = False  # not checked against CME directly
# The 2026 DATE LIST has been cross-checked against three independent
# secondary sources, which agree on which dates are special. They disagree
# on classification and close times -- which is precisely why we trade none
# of them, so the disagreement cannot reach a decision.
CALENDAR_DATES_CROSS_CHECKED_2026: Final[bool] = True
CALENDAR_SOURCE: Final[str] = (
    "https://www.cmegroup.com/tools-information/holiday-calendar.html"
)

__all__ = [
    "CALENDAR_DATES_CROSS_CHECKED_2026",
    "CALENDAR_SOURCE",
    "CALENDAR_VERIFIED",
    "COVERAGE",
    "DayStatus",
    "SessionDay",
    "classify",
    "is_open",
    "previous_trading_date",
]


class DayStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"                    # market shut
    HOLIDAY_HALF_DAY = "HOLIDAY_HALF_DAY"  # market open, but we stand aside
    OUT_OF_COVERAGE = "OUT_OF_COVERAGE"


@dataclass(frozen=True)
class SessionDay:
    status: DayStatus
    label: str

    @property
    def tradable(self) -> bool:
        """Only a full regular session is tradable.

        Half-days are deliberately excluded even though the market is open.
        """
        return self.status is DayStatus.OPEN


COVERAGE: Final[tuple[date, date]] = (date(2026, 1, 1), date(2026, 12, 31))

# Recorded as a label only. Nothing computes with these times -- see the
# module docstring. The figure is disputed between sources, which is the
# whole reason we stand aside on these dates.
_EARLY = "reported early close ~13:00 ET, time disputed"

# Full closures: no equity-index session at all.
HOLIDAYS: Final[dict[date, str]] = {
    date(2026, 1, 1): "New Year's Day",
    date(2026, 4, 3): "Good Friday",
    date(2026, 11, 26): "Thanksgiving Day",
    date(2026, 12, 25): "Christmas Day",
}

# Half-days. Equity-index futures trade a shortened session, but the close
# time is contested and Topstep announces its own deadline, so we stand aside.
HALF_DAYS: Final[dict[date, tuple[str, str]]] = {
    date(2026, 1, 19): (_EARLY, "Martin Luther King Jr. Day"),
    date(2026, 2, 16): (_EARLY, "Presidents' Day"),
    date(2026, 5, 25): (_EARLY, "Memorial Day"),
    date(2026, 6, 19): (_EARLY, "Juneteenth"),
    date(2026, 7, 3): (_EARLY, "Independence Day (observed, 4th is a Saturday)"),
    date(2026, 9, 7): (_EARLY, "Labor Day"),
    date(2026, 7, 2): (_EARLY, "Day before Independence Day (observed)"),
    date(2026, 11, 27): (_EARLY, "Day after Thanksgiving"),
    date(2026, 12, 24): (_EARLY, "Christmas Eve"),
}

# 2027 was never cross-checked against anything. It is deliberately OUTSIDE
# COVERAGE, so every 2027 date already refuses. These rows are a starting
# point for that verification pass, not a calendar -- classify() never reads
# them, and a test asserts it never will.
DRAFT_2027_UNVERIFIED: Final[dict[date, str]] = {
    date(2027, 1, 1): "New Year's Day",
    date(2027, 1, 18): "Martin Luther King Jr. Day",
    date(2027, 2, 15): "Presidents' Day",
    date(2027, 3, 26): "Good Friday",
    date(2027, 5, 31): "Memorial Day",
    date(2027, 6, 18): "Juneteenth (observed)",
    date(2027, 7, 5): "Independence Day (observed)",
    date(2027, 9, 6): "Labor Day",
    date(2027, 11, 25): "Thanksgiving Day",
    date(2027, 11, 26): "Day after Thanksgiving",
    date(2027, 12, 24): "Christmas Day (observed)",
}


def classify(trading_date: date) -> SessionDay:
    """Classify a session's TRADING date (the date the session closes on).

    A session opens 18:00 ET on day D and belongs to day D+1, so callers pass
    D+1. That makes the weekend arithmetic fall out correctly: the session
    "opening" Friday 18:00 has trading date Saturday and is closed, which is
    exactly right -- Globex is shut from Friday 17:00 to Sunday 18:00 ET.
    """
    low, high = COVERAGE
    if not low <= trading_date <= high:
        return SessionDay(
            DayStatus.OUT_OF_COVERAGE,
            f"{trading_date} is outside the calendar's coverage "
            f"({low} to {high}); treated as closed until the table is extended",
        )

    if trading_date.weekday() >= 5:  # 5 = Saturday, 6 = Sunday
        return SessionDay(DayStatus.CLOSED, f"{trading_date:%A}")

    if trading_date in HOLIDAYS:
        return SessionDay(DayStatus.CLOSED, HOLIDAYS[trading_date])

    if trading_date in HALF_DAYS:
        note, label = HALF_DAYS[trading_date]
        return SessionDay(
            DayStatus.HOLIDAY_HALF_DAY,
            f"{label} half-day ({note}); we do not trade holiday dates",
        )

    return SessionDay(DayStatus.OPEN, "regular session")


def is_open(trading_date: date) -> bool:
    """Will WE trade this date? Holiday half-days are excluded by policy."""
    return classify(trading_date).tradable


def market_is_open(trading_date: date) -> bool:
    """Is the MARKET open, regardless of whether we choose to trade?

    Deliberately different from :func:`is_open`. We stand aside on holiday
    half-days, but the exchange is still trading, and some questions depend on
    what the exchange does rather than on our policy -- contract expiry moving
    to the previous business day, for one. Using ``is_open`` there would push
    an expiry off a day the market was actually open.
    """
    return classify(trading_date).status in (
        DayStatus.OPEN,
        DayStatus.HOLIDAY_HALF_DAY,
    )


def previous_trading_date(trading_date: date, limit: int = 10) -> date | None:
    """The most recent tradable date strictly before ``trading_date``.

    Used to decide whether tracked end-of-day state is stale: over a weekend
    or a holiday, state from "two calendar days ago" is perfectly current.
    Returns None if none is found within ``limit`` days, which the caller must
    treat as "cannot determine" rather than "not stale".
    """
    probe = trading_date
    for _ in range(limit):
        probe = probe - timedelta(days=1)
        if is_open(probe):
            return probe
    return None
