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

  A wrong early-close row is the dangerous kind: it means holding a position
  into a close we did not expect. A wrong holiday row is merely a missed day.
  Verify before trading real size, then flip the flag.

Dates outside ``COVERAGE`` are treated as CLOSED, not assumed open. Failing
closed on an unmaintained calendar costs a missed session; failing open costs
a position nobody is watching.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time, timedelta
from enum import Enum
from typing import Final

CALENDAR_VERIFIED: Final[bool] = False
CALENDAR_SOURCE: Final[str] = (
    "https://www.cmegroup.com/tools-information/holiday-calendar.html"
)

__all__ = [
    "CALENDAR_VERIFIED",
    "CALENDAR_SOURCE",
    "COVERAGE",
    "DayStatus",
    "SessionDay",
    "classify",
    "is_open",
    "early_close_et",
    "previous_trading_date",
]


class DayStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    EARLY_CLOSE = "EARLY_CLOSE"
    OUT_OF_COVERAGE = "OUT_OF_COVERAGE"


@dataclass(frozen=True)
class SessionDay:
    status: DayStatus
    label: str
    close_et: time | None = None

    @property
    def tradable(self) -> bool:
        return self.status in (DayStatus.OPEN, DayStatus.EARLY_CLOSE)


COVERAGE: Final[tuple[date, date]] = (date(2026, 1, 1), date(2027, 12, 31))

_EARLY = time(13, 0)  # 13:00 ET, the usual CME equity-index shortened close

# Full closures: no equity-index session at all.
HOLIDAYS: Final[dict[date, str]] = {
    date(2026, 1, 1): "New Year's Day",
    date(2026, 4, 3): "Good Friday",
    date(2026, 11, 26): "Thanksgiving Day",
    date(2026, 12, 25): "Christmas Day",
    date(2027, 1, 1): "New Year's Day",
    date(2027, 3, 26): "Good Friday",
    date(2027, 11, 25): "Thanksgiving Day",
    date(2027, 12, 24): "Christmas Day (observed, 25th is a Saturday)",
}

# Shortened sessions. Equity-index futures trade but close early; the US cash
# market is shut on most of these, so liquidity is thin well before the close.
EARLY_CLOSES: Final[dict[date, tuple[time, str]]] = {
    date(2026, 1, 19): (_EARLY, "Martin Luther King Jr. Day"),
    date(2026, 2, 16): (_EARLY, "Presidents' Day"),
    date(2026, 5, 25): (_EARLY, "Memorial Day"),
    date(2026, 6, 19): (_EARLY, "Juneteenth"),
    date(2026, 7, 3): (_EARLY, "Independence Day (observed, 4th is a Saturday)"),
    date(2026, 9, 7): (_EARLY, "Labor Day"),
    date(2026, 11, 27): (_EARLY, "Day after Thanksgiving"),
    date(2026, 12, 24): (_EARLY, "Christmas Eve"),
    date(2027, 1, 18): (_EARLY, "Martin Luther King Jr. Day"),
    date(2027, 2, 15): (_EARLY, "Presidents' Day"),
    date(2027, 5, 31): (_EARLY, "Memorial Day"),
    date(2027, 6, 18): (_EARLY, "Juneteenth (observed, 19th is a Saturday)"),
    date(2027, 7, 5): (_EARLY, "Independence Day (observed, 4th is a Sunday)"),
    date(2027, 9, 6): (_EARLY, "Labor Day"),
    date(2027, 11, 26): (_EARLY, "Day after Thanksgiving"),
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

    if trading_date in EARLY_CLOSES:
        close, label = EARLY_CLOSES[trading_date]
        return SessionDay(DayStatus.EARLY_CLOSE, label, close_et=close)

    return SessionDay(DayStatus.OPEN, "regular session")


def is_open(trading_date: date) -> bool:
    return classify(trading_date).tradable


def early_close_et(trading_date: date) -> time | None:
    """The early close for this date, or None on a regular/closed day."""
    return classify(trading_date).close_et


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
