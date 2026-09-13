"""Normalise vendor bars into canonical form. The only place a convention is asserted.

THE GUARD THIS PROTECTS
-----------------------
``BarSeries`` refuses ``BarTimestamp.UNKNOWN``. That guard exists because two
properties of ProjectX bars are UNVERIFIED (FIRM_RULES.md): whether a timestamp
labels a bar's open or its close, and whether times are exchange-local or UTC.
Both decide *which bars form an opening range*, so a wrong guess shifts every
signal by one bar and every backtest number with it.

So this module never infers a convention. It reports the evidence, and a human
records the answer. :func:`infer_timestamp_convention` returns a finding with
its reasoning, not a decision.

WHY THE ADAPTER RETURNS MarketBar AND NOT Bar
---------------------------------------------
Handing the backtester's ``Bar`` straight out of the broker would smuggle an
assumed convention past the guard designed to catch it. The adapter produces
neutral :class:`MarketBar`; converting to a ``BarSeries`` requires stating the
convention explicitly, here, where it is visible.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from backtest import Bar, BarSeries, BarTimestamp
from execution.models import MarketBar

__all__ = [
    "ET",
    "TimestampFinding",
    "bars_from_projectx_frame",
    "infer_timestamp_convention",
    "normalise",
    "to_bar_series",
]

ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class TimestampFinding:
    """Evidence about bar timestamps. Explicitly NOT a decision.

    ``suggested`` is a hypothesis with reasoning attached, offered so a human
    can confirm it. Nothing consumes it automatically -- if it did, this module
    would be the guess it exists to prevent.
    """

    tz_aware: bool
    observed_offsets: tuple[str, ...]
    observed_spacings_minutes: tuple[int, ...]
    first_timestamp: str
    last_timestamp: str
    first_et_time_of_day: str
    suggested: BarTimestamp
    reasoning: tuple[str, ...]

    @property
    def confident(self) -> bool:
        return self.suggested is not BarTimestamp.UNKNOWN

    def report(self) -> str:
        lines = [
            "  timestamps carry an offset : " + ("yes" if self.tz_aware else "NO"),
            f"  observed UTC offsets       : {', '.join(self.observed_offsets) or '-'}",
            f"  observed spacings (minutes): {list(self.observed_spacings_minutes)}",
            f"  first bar                  : {self.first_timestamp}",
            f"  last bar                   : {self.last_timestamp}",
            f"  first bar in ET            : {self.first_et_time_of_day}",
            f"  SUGGESTED convention       : {self.suggested.value}",
        ]
        lines += [f"    - {r}" for r in self.reasoning]
        lines.append(
            "  This is EVIDENCE, not a decision. Record the confirmed answer in "
            "FIRM_RULES.md and pass it explicitly."
        )
        return "\n".join(lines)


def bars_from_projectx_frame(frame: Any, *, symbol: str) -> list[MarketBar]:
    """Convert the polars DataFrame ``get_bars`` returns into neutral bars.

    Column naming is matched case-insensitively because vendors are
    inconsistent about it; a missing price column raises rather than defaulting,
    since a silently-absent high or low would corrupt every range calculation.
    """
    columns = {str(c).lower(): str(c) for c in frame.columns}
    missing = [c for c in ("open", "high", "low", "close") if c not in columns]
    if missing:
        raise ValueError(
            f"ProjectX frame is missing {missing}; columns were {list(frame.columns)}"
        )
    ts_col = columns.get("timestamp") or columns.get("t") or columns.get("time")
    if ts_col is None:
        raise ValueError(f"no timestamp column in {list(frame.columns)}")

    out: list[MarketBar] = []
    for record in frame.iter_rows(named=True):
        raw_ts = record[ts_col]
        ts = raw_ts if isinstance(raw_ts, datetime) else _parse(str(raw_ts))
        out.append(
            MarketBar(
                timestamp=ts,
                open=float(record[columns["open"]]),
                high=float(record[columns["high"]]),
                low=float(record[columns["low"]]),
                close=float(record[columns["close"]]),
                volume=int(record.get(columns.get("volume", ""), 0) or 0),
                symbol=symbol,
            )
        )
    return out


def _parse(raw: str) -> datetime:
    text = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
    return datetime.fromisoformat(text)


def normalise(
    bars: Sequence[MarketBar],
    *,
    assume_timezone: ZoneInfo | None = None,
) -> list[MarketBar]:
    """Sort, deduplicate and make every timestamp aware.

    A naive timestamp is only accepted with an explicit ``assume_timezone``;
    otherwise it raises. ``astimezone`` on a naive value silently reads it as
    the host's local time, which on a machine that is not set to ET shifts
    every session boundary with no error anywhere.
    """
    fixed: list[MarketBar] = []
    seen: set[datetime] = set()
    for bar in bars:
        ts = bar.timestamp
        if ts.tzinfo is None:
            if assume_timezone is None:
                raise ValueError(
                    f"bar at {ts} is naive and no source timezone was given. "
                    "Refusing to assume: it would be read as the host's local "
                    "time and shift every session boundary."
                )
            ts = datetime(
                ts.year, ts.month, ts.day, ts.hour, ts.minute,
                ts.second, ts.microsecond, tzinfo=assume_timezone,
            )
        if ts in seen:
            continue
        seen.add(ts)
        fixed.append(MarketBar(
            timestamp=ts, open=bar.open, high=bar.high, low=bar.low,
            close=bar.close, volume=bar.volume, symbol=bar.symbol,
            contract_id=bar.contract_id,
        ))
    fixed.sort(key=lambda b: b.timestamp)
    return fixed


def to_bar_series(
    bars: Sequence[MarketBar],
    *,
    convention: BarTimestamp,
    interval_minutes: int = 5,
    assume_timezone: ZoneInfo | None = None,
) -> BarSeries:
    """Convert to the backtester's canonical series.

    ``convention`` is required and has no default. ``BarSeries`` will itself
    reject ``UNKNOWN``, so there is no way to reach the backtester without
    someone having stated what a timestamp means.
    """
    clean = normalise(bars, assume_timezone=assume_timezone)
    converted = [
        Bar(
            ts=b.timestamp, open=b.open, high=b.high,
            low=b.low, close=b.close, volume=b.volume,
        )
        for b in clean
    ]
    return BarSeries(
        bars=tuple(converted),
        interval_minutes=interval_minutes,
        timestamp_convention=convention,
    )


def infer_timestamp_convention(
    bars: Sequence[MarketBar],
    *,
    rth_open_et: str = "09:30",
) -> TimestampFinding:
    """Gather evidence about what bar timestamps mean. Suggests; never decides.

    The reasoning: if the exchange opens at 09:30 ET and the first bar of the
    session is stamped 09:30, the stamp marks the bar's OPEN. If it is stamped
    09:35 for a 5-minute bar, it marks the CLOSE. Anything else is UNKNOWN, and
    UNKNOWN is reported rather than resolved.
    """
    if not bars:
        return TimestampFinding(
            tz_aware=False, observed_offsets=(), observed_spacings_minutes=(),
            first_timestamp="-", last_timestamp="-", first_et_time_of_day="-",
            suggested=BarTimestamp.UNKNOWN,
            reasoning=("no bars were supplied",),
        )

    ordered = sorted(bars, key=lambda b: b.timestamp)
    aware = all(b.timestamp.tzinfo is not None for b in ordered)
    offsets = sorted({
        (b.timestamp.strftime("%z") or "naive") for b in ordered[:200]
    })
    spacings = sorted({
        int((b.timestamp - a.timestamp).total_seconds() // 60)
        for a, b in zip(ordered, ordered[1:])
        if (b.timestamp - a.timestamp).total_seconds() > 0
    })[:6]

    reasoning: list[str] = []
    suggested = BarTimestamp.UNKNOWN

    if not aware:
        reasoning.append(
            "timestamps are NAIVE: the source timezone must be established "
            "before any opening range can be computed"
        )
    else:
        hh, mm = (int(x) for x in rth_open_et.split(":"))
        open_minute = hh * 60 + mm
        interval = spacings[0] if spacings else 0

        # The discriminator is the FIRST bar of a session, not the presence of
        # a given stamp. Any 5-minute series covering the open contains both
        # 09:30 and 09:35, so "does an 09:30 bar exist" answers nothing.
        # OPEN-labelled sessions START at 09:30; CLOSE-labelled ones start at
        # 09:35, because that bar covers 09:30-09:35.
        first_of_session: dict[Any, int] = {}
        for bar in ordered:
            et = bar.timestamp.astimezone(ET)
            minute = et.hour * 60 + et.minute
            if minute < open_minute:
                continue  # pre-market; the RTH open is the reference point
            current = first_of_session.get(et.date())
            if current is None or minute < current:
                first_of_session[et.date()] = minute

        firsts = set(first_of_session.values())
        if firsts == {open_minute}:
            suggested = BarTimestamp.OPEN
            reasoning.append(
                f"every session's first bar at or after the open is stamped "
                f"exactly {rth_open_et} ET, consistent with OPEN-labelled"
            )
        elif interval and firsts == {open_minute + interval}:
            suggested = BarTimestamp.CLOSE
            reasoning.append(
                f"every session's first bar is stamped {rth_open_et}+{interval}m "
                f"and none at {rth_open_et}, consistent with CLOSE-labelled"
            )
        else:
            reasoning.append(
                f"session-opening stamps are inconsistent or absent "
                f"(observed first-bar minutes-after-midnight: {sorted(firsts)}); "
                "the convention cannot be established from this sample"
            )

    first_et = (
        ordered[0].timestamp.astimezone(ET).strftime("%Y-%m-%d %H:%M %Z")
        if aware else "n/a (naive)"
    )
    return TimestampFinding(
        tz_aware=aware,
        observed_offsets=tuple(offsets),
        observed_spacings_minutes=tuple(spacings),
        first_timestamp=str(ordered[0].timestamp),
        last_timestamp=str(ordered[-1].timestamp),
        first_et_time_of_day=first_et,
        suggested=suggested,
        reasoning=tuple(reasoning),
    )
