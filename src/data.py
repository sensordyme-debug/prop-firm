"""Market data: load, validate, cache, probe.

Feeds :mod:`backtest`. The harness is useless without this, and a backtest run
on data nobody checked is worse than no backtest: it produces confidence
instead of doubt (DECISIONS.md).

WHAT THIS REFUSES TO GUESS
--------------------------
FIRM_RULES.md tags two properties of ProjectX bars UNVERIFIED, and both change
which bars form an opening range:

  * whether ``t`` labels the bar's OPEN or its CLOSE
  * whether times are exchange-local or UTC

So :func:`load_csv` takes ``convention`` and ``source_timezone`` as **required
arguments with no defaults**. A loader that defaults them would be guessing at
the one thing Stage 1 exists to establish, and the guess would be invisible.

Cached data carries both in a sidecar file, so a cache can never be reloaded
under a different interpretation than it was written with.

ERRORS VERSUS WARNINGS
----------------------
Anything that makes the data *wrong* is an ERROR and refuses to build a
series: duplicate or out-of-order timestamps, impossible OHLC, naive times
with no zone given, unparseable rows.

Anything that makes it *incomplete* is a WARNING and is reported loudly but
does not block: unexplained gaps, suspiciously thin coverage, timestamps
inside a DST fold. Futures data has legitimate gaps — the nightly maintenance
break, weekends, holidays — so blocking on every gap would train people to
pass ``--force``, which is worse than reporting honestly.

NOTHING HERE DECIDES ANYTHING
-----------------------------
Pure parsing and validation, then I/O at the edges, same shape as the rest of
the repo. The SDK import is lazy and lives in one function, so this module
imports and its logic tests without credentials.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Final
from zoneinfo import ZoneInfo

_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:  # so `python -m src.data` finds its siblings
    sys.path.insert(0, str(_SRC))

from backtest import Bar, BarSeries, BarTimestamp
from contracts import front_month, rolls_between
from market_calendar import is_open

__all__ = [
    "CsvSchema",
    "DataIssue",
    "LoadResult",
    "ProbeResult",
    "Severity",
    "build_series",
    "load_csv",
    "parse_rows",
    "probe",
    "read_cache",
    "rows_from_projectx",
    "validate",
    "write_cache",
]

# CME equity index: the nightly maintenance break, 17:00-18:00 ET.
MAINTENANCE_BREAK_MINUTES: Final[int] = 60
CACHE_META_SUFFIX: Final[str] = ".meta.json"


class Severity(str, Enum):
    ERROR = "ERROR"      # the data is wrong; refuse to build a series
    WARNING = "WARNING"  # the data is incomplete; report loudly, proceed


@dataclass(frozen=True)
class DataIssue:
    severity: Severity
    code: str
    message: str
    at: datetime | None = None

    def __str__(self) -> str:
        where = f" [{self.at:%Y-%m-%d %H:%M %Z}]" if self.at else ""
        return f"{self.severity.value:7s} {self.code}{where}: {self.message}"


@dataclass(frozen=True)
class CsvSchema:
    """Column names. Vendors disagree, so the mapping is explicit."""

    timestamp: str = "timestamp"
    open: str = "open"
    high: str = "high"
    low: str = "low"
    close: str = "close"
    volume: str | None = "volume"

    def required(self) -> tuple[str, ...]:
        return (self.timestamp, self.open, self.high, self.low, self.close)


@dataclass(frozen=True)
class LoadResult:
    series: BarSeries | None
    issues: tuple[DataIssue, ...]
    source: str
    rows_read: int

    @property
    def errors(self) -> tuple[DataIssue, ...]:
        return tuple(i for i in self.issues if i.severity is Severity.ERROR)

    @property
    def warnings(self) -> tuple[DataIssue, ...]:
        return tuple(i for i in self.issues if i.severity is Severity.WARNING)

    @property
    def ok(self) -> bool:
        return self.series is not None and not self.errors

    def report(self) -> str:
        lines = [
            "=" * 70,
            "DATA QUALITY REPORT",
            "=" * 70,
            f"  source          {self.source}",
            f"  rows read       {self.rows_read}",
        ]
        if self.series is not None:
            bars = self.series.bars
            span_days = (bars[-1].ts - bars[0].ts).days if len(bars) > 1 else 0
            lines += [
                f"  bars accepted   {len(bars)}",
                f"  earliest        {bars[0].ts:%Y-%m-%d %H:%M %Z}",
                f"  latest          {bars[-1].ts:%Y-%m-%d %H:%M %Z}",
                f"  span            {span_days} days",
                f"  interval        {self.series.interval_minutes} min",
                f"  timestamps      {self.series.timestamp_convention.value}-labelled",
            ]
        lines.append("")
        lines.append(f"  ERRORS   {len(self.errors)}")
        for issue in self.errors[:25]:
            lines.append(f"    {issue}")
        if len(self.errors) > 25:
            lines.append(f"    ... and {len(self.errors) - 25} more")
        lines.append(f"  WARNINGS {len(self.warnings)}")
        for issue in self.warnings[:25]:
            lines.append(f"    {issue}")
        if len(self.warnings) > 25:
            lines.append(f"    ... and {len(self.warnings) - 25} more")
        lines.append("")
        lines.append(
            "  USABLE FOR BACKTESTING" if self.ok
            else "  NOT USABLE — fix the errors above before backtesting"
        )
        lines.append("=" * 70)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Pure: parsing
# ---------------------------------------------------------------------------


def _localise(naive: datetime, tz: ZoneInfo) -> datetime:
    """Attach a zone by CONSTRUCTION, never by ``.replace(tzinfo=...)``.

    Identical result, but ``.replace(tzinfo=...)`` is banned repo-wide by an
    AST test because it is indistinguishable from the mislocalisation bug it
    usually is. Building the datetime from its components keeps that ban
    meaningful instead of carving an exception into it.

    A wall-clock time inside a DST fold is genuinely ambiguous; :func:`validate`
    reports those separately rather than pretending otherwise.
    """
    return datetime(
        naive.year, naive.month, naive.day,
        naive.hour, naive.minute, naive.second, naive.microsecond,
        tzinfo=tz,
    )


def parse_rows(
    rows: Sequence[dict[str, str]],
    *,
    source_timezone: ZoneInfo,
    schema: CsvSchema | None = None,
) -> tuple[list[Bar], list[DataIssue]]:
    """Parse raw dict rows into bars, collecting per-row failures.

    A bad row is reported and skipped rather than aborting the load, so one
    malformed line in a year of data does not hide the other 99,999.
    """
    sch = schema or CsvSchema()
    bars: list[Bar] = []
    issues: list[DataIssue] = []

    for index, row in enumerate(rows):
        missing = [c for c in sch.required() if c not in row or row[c] in (None, "")]
        if missing:
            issues.append(DataIssue(
                Severity.ERROR, "ROW_MISSING_FIELDS",
                f"row {index}: missing {', '.join(missing)}",
            ))
            continue
        try:
            raw_ts = str(row[sch.timestamp]).strip()
            parsed = _parse_timestamp(raw_ts)
            ts = parsed if parsed.tzinfo is not None else _localise(parsed, source_timezone)
            volume = 0
            if sch.volume and row.get(sch.volume) not in (None, ""):
                volume = int(float(row[sch.volume]))
            bars.append(Bar(
                ts=ts,
                open=float(row[sch.open]),
                high=float(row[sch.high]),
                low=float(row[sch.low]),
                close=float(row[sch.close]),
                volume=volume,
            ))
        except ValueError as exc:
            issues.append(DataIssue(
                Severity.ERROR, "ROW_UNPARSEABLE", f"row {index}: {exc}",
            ))
    return bars, issues


def _parse_timestamp(raw: str) -> datetime:
    """Accept ISO 8601, with or without an offset, plus epoch seconds/millis."""
    text = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    try:
        number = float(raw)
    except ValueError as exc:
        raise ValueError(f"unrecognised timestamp {raw!r}") from exc
    # Heuristic only, and a narrow one: 1e11 separates seconds from millis
    # for every date this project will ever see.
    seconds = number / 1000.0 if number > 1e11 else number
    return datetime.fromtimestamp(seconds, tz=UTC)


# ---------------------------------------------------------------------------
# Pure: validation
# ---------------------------------------------------------------------------


def validate(
    bars: Sequence[Bar],
    *,
    interval_minutes: int,
    expect_calendar: bool = True,
) -> list[DataIssue]:
    """Check ordering, duplicates, gaps and fold ambiguity. Pure."""
    issues: list[DataIssue] = []
    if not bars:
        return [DataIssue(Severity.ERROR, "EMPTY", "no bars were parsed")]

    seen: dict[datetime, int] = {}
    for i, bar in enumerate(bars):
        if bar.ts in seen:
            issues.append(DataIssue(
                Severity.ERROR, "DUPLICATE_TIMESTAMP",
                f"rows {seen[bar.ts]} and {i} share a timestamp", bar.ts,
            ))
        seen[bar.ts] = i

    for earlier, later in zip(bars, bars[1:]):
        if later.ts < earlier.ts:
            issues.append(DataIssue(
                Severity.ERROR, "OUT_OF_ORDER",
                f"{later.ts:%Y-%m-%d %H:%M} follows {earlier.ts:%Y-%m-%d %H:%M}",
                later.ts,
            ))

    # Ambiguous wall-clock times: only possible for non-UTC sources.
    for bar in bars:
        if bar.ts.utcoffset() is None:
            continue
        alternate = bar.ts.replace(fold=1 - bar.ts.fold)
        if alternate.utcoffset() != bar.ts.utcoffset():
            issues.append(DataIssue(
                Severity.WARNING, "AMBIGUOUS_DST_TIME",
                "wall-clock time occurs twice on this date; which instant it "
                "means cannot be recovered from the timestamp alone",
                bar.ts,
            ))

    issues.extend(_gap_issues(bars, interval_minutes, expect_calendar))
    issues.extend(_roll_issues(bars))
    return issues


def _roll_issues(bars: Sequence[Bar]) -> list[DataIssue]:
    """Warn when a series spans a quarterly roll.

    Bar history for a root symbol splices contracts together. The next quarter
    trades at a different price from the expiring one -- carry and dividends,
    not sentiment -- so a continuous series shows a step change at the join.
    A breakout strategy reads that step as a signal, and it is a phantom:
    nobody could have traded it, because it is two instruments printed side by
    side.

    This cannot be fixed here, only reported. Back-adjusting a series is a
    decision with its own trade-offs, and making it silently inside a
    validator would be the wrong place for it.
    """
    if len(bars) < 2:
        return []
    first = bars[0].ts.astimezone(ZoneInfo("America/New_York")).date()
    last = bars[-1].ts.astimezone(ZoneInfo("America/New_York")).date()
    issues: list[DataIssue] = []
    for roll in rolls_between(first, last):
        issues.append(DataIssue(
            Severity.WARNING, "SPANS_CONTRACT_ROLL",
            f"the series crosses the {roll} quarterly roll "
            f"(front month becomes {front_month(roll).symbol()}). If these bars "
            "are a spliced continuous series, the price step at the join is not "
            "a market move and a breakout strategy will trade it.",
        ))
    return issues


def _gap_issues(
    bars: Sequence[Bar], interval_minutes: int, expect_calendar: bool
) -> list[DataIssue]:
    """Report gaps the session schedule does not explain.

    Legitimate gaps are the nightly maintenance break, weekends and holidays.
    Everything else is missing data, and a backtest over missing data quietly
    skips whatever happened there.
    """
    issues: list[DataIssue] = []
    step = timedelta(minutes=interval_minutes)
    # Allow half a bar of jitter, nothing more. A blanket tolerance wide enough
    # to cover the nightly break would also swallow an hour-long hole in the
    # middle of the session, which is exactly the thing worth knowing about.
    jitter = step + step / 2

    for earlier, later in zip(bars, bars[1:]):
        delta = later.ts - earlier.ts
        if delta <= jitter:
            continue
        if _spans_maintenance_break(earlier.ts, later.ts):
            continue
        if expect_calendar and _spans_a_closure(earlier.ts, later.ts):
            continue
        issues.append(DataIssue(
            Severity.WARNING, "UNEXPLAINED_GAP",
            f"{_humanise(delta)} with no bars, not explained by a weekend, "
            f"holiday or the nightly break (resumes {later.ts:%Y-%m-%d %H:%M})",
            earlier.ts,
        ))
    return issues


def _spans_maintenance_break(start: datetime, end: datetime) -> bool:
    """Does this gap actually cover the 17:00-18:00 ET maintenance window?

    Checked against the clock rather than allowed as a blanket tolerance, so a
    gap of the same LENGTH at 10:00 is still reported.
    """
    et = ZoneInfo("America/New_York")
    probe = start.astimezone(et).date()
    last = end.astimezone(et).date()
    while probe <= last:
        window_start = datetime(probe.year, probe.month, probe.day, 17, 0, tzinfo=et)
        window_end = window_start + timedelta(minutes=MAINTENANCE_BREAK_MINUTES)
        if start < window_end and end > window_start:
            return True
        probe += timedelta(days=1)
    return False


def _spans_a_closure(start: datetime, end: datetime) -> bool:
    probe = start.date()
    last = end.date()
    while probe <= last:
        if not is_open(probe):
            return True
        probe += timedelta(days=1)
    return False


def _humanise(delta: timedelta) -> str:
    hours, remainder = divmod(int(delta.total_seconds()), 3600)
    minutes = remainder // 60
    if hours >= 24:
        return f"{hours // 24}d {hours % 24}h"
    return f"{hours}h {minutes:02d}m"


def build_series(
    bars: Sequence[Bar],
    *,
    interval_minutes: int,
    convention: BarTimestamp,
    issues: Sequence[DataIssue] = (),
) -> BarSeries | None:
    """Build a BarSeries, or None if any ERROR issue was raised.

    Refusing on error is the point: a harness fed known-bad data reports
    numbers just as confidently as one fed good data.
    """
    if any(i.severity is Severity.ERROR for i in issues) or not bars:
        return None
    return BarSeries(
        bars=tuple(bars),
        interval_minutes=interval_minutes,
        timestamp_convention=convention,
    )


# ---------------------------------------------------------------------------
# I/O: CSV and cache
# ---------------------------------------------------------------------------


def load_csv(
    path: Path | str,
    *,
    convention: BarTimestamp,
    source_timezone: ZoneInfo,
    interval_minutes: int,
    schema: CsvSchema | None = None,
    expect_calendar: bool = True,
) -> LoadResult:
    """Load and validate a vendor CSV.

    ``convention`` and ``source_timezone`` are required. See the module
    docstring: defaulting them would guess at exactly what Stage 1 exists to
    establish, and the guess would be silent.
    """
    source = Path(path)
    if not source.is_file():
        return LoadResult(
            None,
            (DataIssue(Severity.ERROR, "NO_SUCH_FILE", f"{source} does not exist"),),
            str(source), 0,
        )

    with source.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))

    bars, issues = parse_rows(rows, source_timezone=source_timezone, schema=schema)
    bars.sort(key=lambda b: b.ts)
    issues += validate(
        bars, interval_minutes=interval_minutes, expect_calendar=expect_calendar
    )
    series = build_series(
        bars, interval_minutes=interval_minutes, convention=convention, issues=issues
    )
    return LoadResult(series, tuple(issues), str(source), len(rows))


def write_cache(path: Path | str, series: BarSeries, *, symbol: str) -> Path:
    """Write bars plus a sidecar recording how they must be interpreted.

    The sidecar is the point. Without it a cache is a pile of numbers whose
    timestamp convention someone will eventually have to guess.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for bar in series.bars:
            writer.writerow([
                bar.ts.isoformat(), bar.open, bar.high, bar.low, bar.close, bar.volume,
            ])

    meta = {
        "symbol": symbol,
        "interval_minutes": series.interval_minutes,
        "timestamp_convention": series.timestamp_convention.value,
        "bars": len(series.bars),
        "earliest": series.bars[0].ts.isoformat() if series.bars else None,
        "latest": series.bars[-1].ts.isoformat() if series.bars else None,
        "written_utc": datetime.now(UTC).isoformat(),
    }
    Path(str(target) + CACHE_META_SUFFIX).write_text(
        json.dumps(meta, indent=2) + "\n", encoding="utf-8"
    )
    return target


def read_cache(path: Path | str, *, expect_calendar: bool = True) -> LoadResult:
    """Read a cache written by :func:`write_cache`.

    The convention comes from the sidecar, never from an argument and never
    from a default. A missing or unreadable sidecar is an ERROR: bars whose
    interpretation is unknown are not data, they are numbers.
    """
    target = Path(path)
    meta_path = Path(str(target) + CACHE_META_SUFFIX)
    if not meta_path.is_file():
        return LoadResult(
            None,
            (DataIssue(
                Severity.ERROR, "NO_CACHE_METADATA",
                f"{meta_path} is missing; the timestamp convention of "
                f"{target} is unknown and must not be assumed",
            ),),
            str(target), 0,
        )
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        convention = BarTimestamp(meta["timestamp_convention"])
        interval = int(meta["interval_minutes"])
    except (ValueError, KeyError, TypeError) as exc:
        return LoadResult(
            None,
            (DataIssue(Severity.ERROR, "BAD_CACHE_METADATA", f"{meta_path}: {exc}"),),
            str(target), 0,
        )

    return load_csv(
        target,
        convention=convention,
        source_timezone=ZoneInfo("UTC"),  # cache timestamps always carry offsets
        interval_minutes=interval,
        expect_calendar=expect_calendar,
    )


# ---------------------------------------------------------------------------
# I/O: ProjectX
# ---------------------------------------------------------------------------


def rows_from_projectx(frame: object) -> list[dict[str, str]]:
    """Convert the polars DataFrame ``get_bars`` returns into raw rows.

    Kept as dicts so the same :func:`parse_rows` and :func:`validate` run over
    live data and vendor CSVs alike. One validation path, not two.
    """
    columns = {c.lower(): c for c in frame.columns}  # type: ignore[attr-defined]
    wanted = ("timestamp", "open", "high", "low", "close", "volume")
    missing = [w for w in ("open", "high", "low", "close") if w not in columns]
    if missing:
        raise ValueError(
            f"ProjectX frame is missing {missing}; columns were "
            f"{list(frame.columns)}"  # type: ignore[attr-defined]
        )
    ts_col = columns.get("timestamp") or columns.get("t") or columns.get("time")
    if ts_col is None:
        raise ValueError(
            f"no timestamp column in {list(frame.columns)}"  # type: ignore[attr-defined]
        )

    rows: list[dict[str, str]] = []
    for record in frame.iter_rows(named=True):  # type: ignore[attr-defined]
        row = {"timestamp": str(record[ts_col])}
        for name in wanted[1:]:
            source = columns.get(name)
            if source is not None:
                row[name] = str(record[source])
        rows.append(row)
    return rows


@dataclass(frozen=True)
class ProbeResult:
    """Answers ROADMAP Stage 2: how far back does the history actually go?"""

    symbol: str
    interval_minutes: int
    attempts: tuple[tuple[int, int, str | None], ...] = ()  # (days asked, bars, earliest)
    earliest: datetime | None = None
    latest: datetime | None = None
    total_bars: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)

    def report(self) -> str:
        lines = [
            "=" * 70,
            f"HISTORY PROBE — {self.symbol}, {self.interval_minutes}-minute bars",
            "=" * 70,
            "",
            f"  {'days asked':>12s}  {'bars returned':>14s}   earliest returned",
        ]
        for asked, count, earliest in self.attempts:
            lines.append(f"  {asked:>12d}  {count:>14d}   {earliest or '-'}")
        lines += [
            "",
            f"  earliest bar    {self.earliest or 'unknown'}",
            f"  latest bar      {self.latest or 'unknown'}",
            f"  total bars      {self.total_bars}",
        ]
        if self.earliest and self.latest:
            span = (self.latest - self.earliest).days
            lines.append(f"  span            {span} days (~{span / 365.25:.1f} years)")
            lines.append("")
            if span >= 700:
                lines.append("  VERDICT: deep enough. No external data needed.")
            else:
                lines.append(
                    "  VERDICT: shallow. ROADMAP Stage 2 says source CME history "
                    "from a vendor (Databento, FirstRate) as CSV into this same "
                    "harness — a second data file, not a second engine."
                )
        lines += ["", "  RECORD IN CLAUDE.md / FIRM_RULES.md:"]
        lines += [f"    - {n}" for n in self.notes] or ["    - (nothing observed)"]
        lines.append("=" * 70)
        return "\n".join(lines)


async def probe(
    symbol: str = "MNQ",
    *,
    interval_minutes: int = 5,
    windows: Sequence[int] = (30, 90, 180, 365, 730, 1825),
) -> ProbeResult:
    """Ask for progressively longer windows and record what comes back.

    Empirical because the limit is undocumented (FIRM_RULES.md, UNVERIFIED).
    Six requests, well inside the 50-per-30s bars limit.

    Also reports what the timestamps LOOK like — the Stage 1 question of
    whether they are exchange-local or UTC, and whether ``t`` labels the open
    or the close. It reports the evidence; a human still confirms it.
    """
    from project_x_py import ProjectX  # lazy: this module imports without a key

    attempts: list[tuple[int, int, str | None]] = []
    notes: list[str] = []
    earliest: datetime | None = None
    latest: datetime | None = None
    total = 0

    async with ProjectX.from_env() as client:
        await client.authenticate()
        for days in windows:
            try:
                frame = await client.get_bars(
                    symbol, days=days, interval=interval_minutes
                )
            except Exception as exc:
                attempts.append((days, 0, f"failed: {type(exc).__name__}"))
                notes.append(f"{days}-day request failed: {exc}")
                break

            if frame is None or len(frame) == 0:
                attempts.append((days, 0, None))
                continue

            rows = rows_from_projectx(frame)
            bars, _ = parse_rows(rows, source_timezone=ZoneInfo("UTC"))
            bars.sort(key=lambda b: b.ts)
            attempts.append((days, len(bars), bars[0].ts.isoformat() if bars else None))

            if bars:
                total = max(total, len(bars))
                earliest = bars[0].ts if earliest is None else min(earliest, bars[0].ts)
                latest = bars[-1].ts if latest is None else max(latest, bars[-1].ts)
                if len(notes) == 0:
                    notes.append(f"raw columns: {list(frame.columns)}")
                    notes.append(f"first raw timestamp: {rows[0]['timestamp']!r}")
                    notes.append(
                        "timestamps carry an offset"
                        if "+" in rows[0]["timestamp"] or "Z" in rows[0]["timestamp"]
                        else "timestamps are NAIVE — the zone must be established "
                             "before any opening range is computed"
                    )
                    spacing = {
                        int((b.ts - a.ts).total_seconds() // 60)
                        for a, b in zip(bars, bars[1:])
                    }
                    notes.append(f"observed bar spacings (minutes): {sorted(spacing)[:6]}")
                    first_of_day = bars[0].ts.astimezone(ZoneInfo("America/New_York"))
                    notes.append(
                        f"first bar in ET: {first_of_day:%Y-%m-%d %H:%M} — if the "
                        f"session opens 09:30 ET, a 09:30 stamp means OPEN-labelled "
                        f"and 09:35 means CLOSE-labelled"
                    )

    return ProbeResult(
        symbol=symbol,
        interval_minutes=interval_minutes,
        attempts=tuple(attempts),
        earliest=earliest,
        latest=latest,
        total_bars=total,
        notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.data",
        description="Load, validate and probe market data.",
    )
    parser.add_argument("--probe", metavar="SYMBOL", nargs="?", const="MNQ",
                        help="ask how far back the history goes (needs the API key)")
    parser.add_argument("--validate", metavar="CSV",
                        help="load and validate a vendor CSV")
    parser.add_argument("--cache", metavar="CSV",
                        help="read a cache written by write_cache")
    parser.add_argument("--convention", choices=["OPEN", "CLOSE"],
                        help="what a timestamp labels; REQUIRED for --validate")
    parser.add_argument("--tz", default=None,
                        help="source timezone for naive timestamps, e.g. "
                             "America/Chicago; REQUIRED for --validate")
    parser.add_argument("--interval", type=int, default=5, help="bar interval, minutes")
    args = parser.parse_args(argv)

    if args.cache:
        result = read_cache(args.cache)
        print(result.report())
        return 0 if result.ok else 1

    if args.validate:
        if not args.convention or not args.tz:
            print(
                "--validate requires --convention and --tz.\n"
                "Neither is guessable: whether `t` labels a bar's open or close, "
                "and whether times are exchange-local or UTC, both decide which "
                "bars form an opening range (FIRM_RULES.md lists both as "
                "UNVERIFIED). Establish them in ROADMAP Stage 1 and pass them.",
                file=sys.stderr,
            )
            return 2
        result = load_csv(
            args.validate,
            convention=BarTimestamp(args.convention),
            source_timezone=ZoneInfo(args.tz),
            interval_minutes=args.interval,
        )
        print(result.report())
        return 0 if result.ok else 1

    if args.probe:
        import asyncio

        try:
            outcome = asyncio.run(probe(args.probe, interval_minutes=args.interval))
        except Exception as exc:
            print(f"PROBE FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
            print(
                "This needs a working key. Run src/connection_test.py first "
                "(ROADMAP Stage 1) — it diagnoses credential problems properly.",
                file=sys.stderr,
            )
            return 1
        print(outcome.report())
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
