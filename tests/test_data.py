"""Tests for market data loading, validation and caching.

No credentials: the SDK import inside :func:`probe` is lazy, so everything
below runs offline. The two things the loader must never guess — timestamp
convention and source timezone — are exercised as hard requirements.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import data as dm
from backtest import BarConventionError, BarSeries, BarTimestamp, run_backtest
from data import (
    CsvSchema,
    Severity,
    build_series,
    load_csv,
    parse_rows,
    read_cache,
    rows_from_projectx,
    validate,
    write_cache,
)

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
CT = ZoneInfo("America/Chicago")

HEADER = "timestamp,open,high,low,close,volume\n"


def write_csv(tmp_path: Path, body: str, name: str = "bars.csv") -> Path:
    path = tmp_path / name
    path.write_text(HEADER + body, encoding="utf-8")
    return path


def rows(*timestamps: str) -> list[dict[str, str]]:
    return [
        {"timestamp": ts, "open": "100", "high": "101", "low": "99",
         "close": "100", "volume": "10"}
        for ts in timestamps
    ]


# ===========================================================================
# Parsing
# ===========================================================================


def test_iso_timestamps_with_an_offset_are_used_as_given():
    bars, issues = parse_rows(rows("2026-09-16T09:35:00-04:00"), source_timezone=UTC)
    assert not issues
    assert bars[0].ts == datetime(2026, 9, 16, 9, 35, tzinfo=ET)


def test_trailing_z_is_understood_as_utc():
    bars, issues = parse_rows(rows("2026-09-16T13:35:00Z"), source_timezone=ET)
    assert not issues
    assert bars[0].ts == datetime(2026, 9, 16, 13, 35, tzinfo=UTC)


def test_naive_timestamps_are_localised_to_the_declared_zone():
    """The declared zone is used — not the host's, and not a guess."""
    bars, _ = parse_rows(rows("2026-09-16 08:35:00"), source_timezone=CT)
    assert bars[0].ts == datetime(2026, 9, 16, 8, 35, tzinfo=CT)
    assert bars[0].ts.astimezone(ET).hour == 9, "08:35 CT is 09:35 ET"


def test_the_declared_zone_actually_changes_the_instant():
    """Same wall clock, two zones, two different instants — this is the bug
    that a defaulted timezone would introduce silently."""
    as_ct, _ = parse_rows(rows("2026-09-16 09:35:00"), source_timezone=CT)
    as_et, _ = parse_rows(rows("2026-09-16 09:35:00"), source_timezone=ET)
    assert as_ct[0].ts != as_et[0].ts
    assert as_ct[0].ts - as_et[0].ts == timedelta(hours=1)


def test_epoch_seconds_and_milliseconds_are_both_accepted():
    moment = datetime(2026, 9, 16, 13, 35, tzinfo=UTC)
    secs = str(int(moment.timestamp()))
    millis = str(int(moment.timestamp() * 1000))
    from_secs, _ = parse_rows(rows(secs), source_timezone=UTC)
    from_millis, _ = parse_rows(rows(millis), source_timezone=UTC)
    assert from_secs[0].ts == moment
    assert from_millis[0].ts == moment


def test_a_bad_row_is_reported_and_skipped_without_losing_the_rest():
    raw = rows("2026-09-16T09:35:00Z", "not-a-date", "2026-09-16T09:45:00Z")
    bars, issues = parse_rows(raw, source_timezone=UTC)
    assert len(bars) == 2, "one malformed line must not discard the file"
    assert [i.code for i in issues] == ["ROW_UNPARSEABLE"]


def test_missing_columns_are_reported_per_row():
    raw = [{"timestamp": "2026-09-16T09:35:00Z", "open": "100"}]
    bars, issues = parse_rows(raw, source_timezone=UTC)
    assert bars == []
    assert issues[0].code == "ROW_MISSING_FIELDS"
    assert "high" in issues[0].message


def test_impossible_ohlc_is_rejected_by_the_bar_itself():
    raw = [{"timestamp": "2026-09-16T09:35:00Z", "open": "100", "high": "90",
            "low": "99", "close": "100", "volume": "1"}]
    bars, issues = parse_rows(raw, source_timezone=UTC)
    assert bars == []
    assert issues[0].code == "ROW_UNPARSEABLE"


def test_a_custom_vendor_schema_is_honoured():
    raw = [{"t": "2026-09-16T09:35:00Z", "o": "1", "h": "2", "l": "0.5", "c": "1.5"}]
    schema = CsvSchema(timestamp="t", open="o", high="h", low="l", close="c",
                       volume=None)
    bars, issues = parse_rows(raw, source_timezone=UTC, schema=schema)
    assert not issues
    assert bars[0].close == 1.5


# ===========================================================================
# Validation
# ===========================================================================


def utc_bars(*minutes: int):
    base = datetime(2026, 9, 16, 13, 30, tzinfo=UTC)
    got, _ = parse_rows(
        rows(*[(base + timedelta(minutes=m)).isoformat() for m in minutes]),
        source_timezone=UTC,
    )
    return got


def test_clean_five_minute_data_raises_nothing():
    assert validate(utc_bars(0, 5, 10, 15), interval_minutes=5) == []


def test_duplicate_timestamps_are_an_error():
    issues = validate(utc_bars(0, 5, 5, 10), interval_minutes=5)
    assert any(i.code == "DUPLICATE_TIMESTAMP" and i.severity is Severity.ERROR
               for i in issues)


def test_out_of_order_timestamps_are_an_error():
    bars = utc_bars(0, 10, 5)
    issues = validate(bars, interval_minutes=5)
    assert any(i.code == "OUT_OF_ORDER" for i in issues)


def test_empty_data_is_an_error():
    issues = validate([], interval_minutes=5)
    assert issues[0].code == "EMPTY"


def test_an_unexplained_intraday_gap_is_a_warning():
    """Four hours missing on an open Wednesday is missing data."""
    issues = validate(utc_bars(0, 5, 245, 250), interval_minutes=5)
    gaps = [i for i in issues if i.code == "UNEXPLAINED_GAP"]
    assert len(gaps) == 1
    assert gaps[0].severity is Severity.WARNING


def test_a_weekend_gap_is_not_flagged():
    """Friday evening to Monday morning is the market being shut, not a hole."""
    friday = datetime(2026, 9, 18, 20, 55, tzinfo=UTC)
    monday = datetime(2026, 9, 21, 13, 35, tzinfo=UTC)
    bars, _ = parse_rows(rows(friday.isoformat(), monday.isoformat()),
                         source_timezone=UTC)
    assert [i for i in validate(bars, interval_minutes=5)
            if i.code == "UNEXPLAINED_GAP"] == []


def test_a_holiday_gap_is_not_flagged():
    """Thanksgiving 2026 is a full closure in the calendar."""
    before = datetime(2026, 11, 25, 20, 55, tzinfo=UTC)
    after = datetime(2026, 11, 30, 14, 35, tzinfo=UTC)
    bars, _ = parse_rows(rows(before.isoformat(), after.isoformat()),
                         source_timezone=UTC)
    assert [i for i in validate(bars, interval_minutes=5)
            if i.code == "UNEXPLAINED_GAP"] == []


def test_the_nightly_maintenance_break_is_not_flagged():
    evening = datetime(2026, 9, 16, 20, 55, tzinfo=UTC)   # 16:55 ET
    reopen = datetime(2026, 9, 16, 22, 5, tzinfo=UTC)     # 18:05 ET
    bars, _ = parse_rows(rows(evening.isoformat(), reopen.isoformat()),
                         source_timezone=UTC)
    assert [i for i in validate(bars, interval_minutes=5)
            if i.code == "UNEXPLAINED_GAP"] == []


def test_an_ambiguous_dst_wall_clock_time_is_warned_about():
    """01:30 ET happens twice on 2026-11-01; the stamp cannot say which."""
    bars, _ = parse_rows(rows("2026-11-01 01:30:00"), source_timezone=ET)
    issues = validate(bars, interval_minutes=5)
    assert any(i.code == "AMBIGUOUS_DST_TIME" for i in issues)


def test_utc_source_data_is_never_ambiguous():
    bars, _ = parse_rows(rows("2026-11-01T05:30:00Z"), source_timezone=UTC)
    assert [i for i in validate(bars, interval_minutes=5)
            if i.code == "AMBIGUOUS_DST_TIME"] == []


# ===========================================================================
# build_series refuses bad data
# ===========================================================================


def test_build_series_refuses_when_any_error_is_present():
    bars = utc_bars(0, 5)
    issues = [dm.DataIssue(Severity.ERROR, "X", "boom")]
    assert build_series(bars, interval_minutes=5,
                        convention=BarTimestamp.CLOSE, issues=issues) is None


def test_build_series_proceeds_on_warnings_alone():
    bars = utc_bars(0, 5)
    issues = [dm.DataIssue(Severity.WARNING, "X", "just so you know")]
    series = build_series(bars, interval_minutes=5,
                          convention=BarTimestamp.CLOSE, issues=issues)
    assert isinstance(series, BarSeries)


def test_build_series_refuses_empty_input():
    assert build_series([], interval_minutes=5, convention=BarTimestamp.CLOSE) is None


# ===========================================================================
# load_csv
# ===========================================================================


def test_loading_a_clean_csv_produces_a_usable_series(tmp_path):
    body = "".join(
        f"2026-09-16T{13 + (m // 60):02d}:{m % 60:02d}:00Z,100,101,99,100,5\n"
        for m in range(30, 60, 5)
    )
    result = load_csv(write_csv(tmp_path, body), convention=BarTimestamp.CLOSE,
                      source_timezone=UTC, interval_minutes=5)
    assert result.ok
    assert result.series is not None
    assert len(result.series.bars) == 6
    assert result.series.timestamp_convention is BarTimestamp.CLOSE


def test_convention_and_timezone_are_required_arguments():
    """Not defaulted: both decide which bars form an opening range."""
    with pytest.raises(TypeError):
        load_csv("x.csv", source_timezone=UTC, interval_minutes=5)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        load_csv("x.csv", convention=BarTimestamp.CLOSE, interval_minutes=5)  # type: ignore[call-arg]


def test_an_unknown_convention_is_still_refused_downstream(tmp_path):
    body = "2026-09-16T13:35:00Z,100,101,99,100,5\n"
    with pytest.raises(BarConventionError, match="UNVERIFIED"):
        load_csv(write_csv(tmp_path, body), convention=BarTimestamp.UNKNOWN,
                 source_timezone=UTC, interval_minutes=5)


def test_a_missing_file_is_an_error_not_a_crash(tmp_path):
    result = load_csv(tmp_path / "nope.csv", convention=BarTimestamp.CLOSE,
                      source_timezone=UTC, interval_minutes=5)
    assert not result.ok
    assert result.errors[0].code == "NO_SUCH_FILE"


def test_a_reverse_ordered_file_is_sorted_not_rejected(tmp_path):
    body = ("2026-09-16T13:45:00Z,100,101,99,100,5\n"
            "2026-09-16T13:35:00Z,100,101,99,100,5\n"
            "2026-09-16T13:40:00Z,100,101,99,100,5\n")
    result = load_csv(write_csv(tmp_path, body), convention=BarTimestamp.CLOSE,
                      source_timezone=UTC, interval_minutes=5)
    assert result.ok
    stamps = [b.ts for b in result.series.bars]
    assert stamps == sorted(stamps)


def test_duplicates_in_a_file_block_the_series(tmp_path):
    body = ("2026-09-16T13:35:00Z,100,101,99,100,5\n"
            "2026-09-16T13:35:00Z,100,101,99,100,5\n")
    result = load_csv(write_csv(tmp_path, body), convention=BarTimestamp.CLOSE,
                      source_timezone=UTC, interval_minutes=5)
    assert not result.ok
    assert result.series is None


def test_report_says_plainly_whether_the_data_is_usable(tmp_path):
    body = "2026-09-16T13:35:00Z,100,101,99,100,5\n2026-09-16T13:40:00Z,1,2,0.5,1,5\n"
    good = load_csv(write_csv(tmp_path, body), convention=BarTimestamp.CLOSE,
                    source_timezone=UTC, interval_minutes=5).report()
    assert "USABLE FOR BACKTESTING" in good

    bad = load_csv(tmp_path / "missing.csv", convention=BarTimestamp.CLOSE,
                   source_timezone=UTC, interval_minutes=5).report()
    assert "NOT USABLE" in bad


# ===========================================================================
# Cache round trip
# ===========================================================================


def test_cache_round_trip_preserves_bars_and_convention(tmp_path):
    series = BarSeries(bars=tuple(utc_bars(0, 5, 10)), interval_minutes=5,
                       timestamp_convention=BarTimestamp.OPEN)
    target = write_cache(tmp_path / "mnq.csv", series, symbol="MNQ")

    restored = read_cache(target)
    assert restored.ok
    assert restored.series.timestamp_convention is BarTimestamp.OPEN
    assert [b.ts for b in restored.series.bars] == [b.ts for b in series.bars]
    assert [b.close for b in restored.series.bars] == [b.close for b in series.bars]


def test_cache_sidecar_records_what_is_needed_to_interpret_it(tmp_path):
    series = BarSeries(bars=tuple(utc_bars(0, 5)), interval_minutes=5,
                       timestamp_convention=BarTimestamp.CLOSE)
    target = write_cache(tmp_path / "mnq.csv", series, symbol="MNQ")
    meta = json.loads(Path(str(target) + dm.CACHE_META_SUFFIX).read_text())
    assert meta["symbol"] == "MNQ"
    assert meta["timestamp_convention"] == "CLOSE"
    assert meta["interval_minutes"] == 5
    assert meta["bars"] == 2


def test_a_cache_without_its_sidecar_is_refused(tmp_path):
    """Bars whose interpretation is unknown are numbers, not data."""
    series = BarSeries(bars=tuple(utc_bars(0, 5)), interval_minutes=5,
                       timestamp_convention=BarTimestamp.CLOSE)
    target = write_cache(tmp_path / "mnq.csv", series, symbol="MNQ")
    Path(str(target) + dm.CACHE_META_SUFFIX).unlink()

    result = read_cache(target)
    assert not result.ok
    assert result.errors[0].code == "NO_CACHE_METADATA"
    assert "must not be assumed" in result.errors[0].message


def test_a_corrupt_sidecar_is_refused(tmp_path):
    series = BarSeries(bars=tuple(utc_bars(0, 5)), interval_minutes=5,
                       timestamp_convention=BarTimestamp.CLOSE)
    target = write_cache(tmp_path / "mnq.csv", series, symbol="MNQ")
    Path(str(target) + dm.CACHE_META_SUFFIX).write_text("{ broken", encoding="utf-8")

    result = read_cache(target)
    assert not result.ok
    assert result.errors[0].code == "BAD_CACHE_METADATA"


# ===========================================================================
# ProjectX frame conversion
# ===========================================================================


def test_projectx_frame_converts_through_the_same_validation_path():
    """Live data and vendor CSVs share one parser, so they cannot drift apart."""
    import polars as pl

    frame = pl.DataFrame({
        "timestamp": ["2026-09-16T13:35:00Z", "2026-09-16T13:40:00Z"],
        "open": [100.0, 101.0], "high": [102.0, 103.0],
        "low": [99.0, 100.0], "close": [101.0, 102.0], "volume": [10, 20],
    })
    raw = rows_from_projectx(frame)
    bars, issues = parse_rows(raw, source_timezone=UTC)
    assert not issues
    assert len(bars) == 2
    assert bars[0].close == 101.0


def test_projectx_frame_with_a_t_column_is_understood():
    import polars as pl

    frame = pl.DataFrame({
        "t": ["2026-09-16T13:35:00Z"], "open": [1.0], "high": [2.0],
        "low": [0.5], "close": [1.5], "volume": [3],
    })
    assert rows_from_projectx(frame)[0]["timestamp"] == "2026-09-16T13:35:00Z"


def test_a_frame_missing_price_columns_is_rejected_loudly():
    import polars as pl

    with pytest.raises(ValueError, match="missing"):
        rows_from_projectx(pl.DataFrame({"timestamp": ["x"], "open": [1.0]}))


def test_a_frame_with_no_timestamp_column_is_rejected():
    import polars as pl

    with pytest.raises(ValueError, match="no timestamp"):
        rows_from_projectx(pl.DataFrame({
            "open": [1.0], "high": [2.0], "low": [0.5], "close": [1.5],
        }))


# ===========================================================================
# It actually feeds the harness
# ===========================================================================


def test_loaded_data_drives_the_backtest_harness(tmp_path):
    """The whole point: CSV in one end, governor-checked result out the other."""
    body = "".join(
        f"2026-09-16T{13 + ((30 + i * 5) // 60):02d}:{(30 + i * 5) % 60:02d}:00Z,"
        f"20000,20002,19998,20000,5\n"
        for i in range(12)
    )
    result = load_csv(write_csv(tmp_path, body), convention=BarTimestamp.CLOSE,
                      source_timezone=UTC, interval_minutes=5)
    assert result.ok

    def never_trade(bars, state, config):
        return None

    outcome = run_backtest(result.series, never_trade)
    assert outcome.bars_seen == 12
    assert outcome.net_profit == 0.0
    assert "Net profit" in outcome.report()


# ===========================================================================
# CLI
# ===========================================================================


def test_cli_refuses_to_validate_without_convention_and_timezone(tmp_path, capsys):
    path = write_csv(tmp_path, "2026-09-16T13:35:00Z,100,101,99,100,5\n")
    assert dm.main(["--validate", str(path)]) == 2
    assert "UNVERIFIED" in capsys.readouterr().err


def test_cli_validates_a_good_file(tmp_path, capsys):
    body = "2026-09-16T13:35:00Z,100,101,99,100,5\n2026-09-16T13:40:00Z,100,101,99,100,5\n"
    path = write_csv(tmp_path, body)
    code = dm.main(["--validate", str(path), "--convention", "CLOSE", "--tz", "UTC"])
    assert code == 0
    assert "USABLE FOR BACKTESTING" in capsys.readouterr().out


def test_cli_reports_failure_for_bad_data(tmp_path, capsys):
    body = "2026-09-16T13:35:00Z,100,101,99,100,5\n2026-09-16T13:35:00Z,100,101,99,100,5\n"
    path = write_csv(tmp_path, body)
    code = dm.main(["--validate", str(path), "--convention", "CLOSE", "--tz", "UTC"])
    assert code == 1
    assert "NOT USABLE" in capsys.readouterr().out


def test_cli_with_no_arguments_prints_help(capsys):
    assert dm.main([]) == 2
    assert "probe" in capsys.readouterr().out


def test_module_imports_without_the_sdk():
    """probe() imports the SDK lazily, so everything else works offline."""
    import ast

    tree = ast.parse((Path(__file__).parent.parent / "src" / "data.py")
                     .read_text(encoding="utf-8"))
    top_level = [
        n for n in tree.body
        if isinstance(n, (ast.Import, ast.ImportFrom))
    ]
    names = []
    for node in top_level:
        if isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
        elif isinstance(node, ast.Import):
            names += [a.name for a in node.names]
    assert not any("project_x_py" in n for n in names), (
        "the SDK import must stay inside probe() so this module loads offline"
    )


def test_a_short_hole_during_the_session_is_still_flagged():
    """A 35-minute hole at 10:00 ET is missing data, not the nightly break.

    Guards against a blanket tolerance wide enough to cover the maintenance
    window: that would also swallow holes of the same length mid-session.
    """
    issues = validate(utc_bars(0, 5, 10, 45, 50), interval_minutes=5)
    gaps = [i for i in issues if i.code == "UNEXPLAINED_GAP"]
    assert len(gaps) == 1
    assert "35m" in str(gaps[0]) or "0h 35m" in str(gaps[0])


def test_a_gap_of_break_length_at_the_wrong_time_of_day_is_flagged():
    """Same duration as the maintenance break, but at 10:00 ET."""
    issues = validate(utc_bars(0, 5, 70, 75), interval_minutes=5)
    assert [i for i in issues if i.code == "UNEXPLAINED_GAP"]


def test_one_missing_bar_is_flagged():
    issues = validate(utc_bars(0, 5, 15, 20), interval_minutes=5)
    assert [i for i in issues if i.code == "UNEXPLAINED_GAP"]


def test_a_series_spanning_a_quarterly_roll_is_flagged():
    """A spliced continuous series has a price step that is not a market move."""
    before = datetime(2026, 9, 11, 14, 0, tzinfo=UTC)
    after = datetime(2026, 9, 16, 14, 0, tzinfo=UTC)
    bars, _ = parse_rows(rows(before.isoformat(), after.isoformat()),
                         source_timezone=UTC)
    issues = validate(bars, interval_minutes=5)
    roll = [i for i in issues if i.code == "SPANS_CONTRACT_ROLL"]
    assert len(roll) == 1
    assert "2026-09-14" in roll[0].message
    assert "MNQZ26" in roll[0].message
    assert roll[0].severity is Severity.WARNING, "reportable, not fatal"


def test_a_series_inside_one_contract_is_not_flagged():
    quiet = [
        (datetime(2026, 9, 16, 14, 0, tzinfo=UTC) + timedelta(minutes=5 * i)).isoformat()
        for i in range(4)
    ]
    bars, _ = parse_rows(rows(*quiet), source_timezone=UTC)
    assert [i for i in validate(bars, interval_minutes=5)
            if i.code == "SPANS_CONTRACT_ROLL"] == []


def test_a_multi_quarter_series_flags_every_roll_it_crosses():
    bars, _ = parse_rows(
        rows(datetime(2026, 1, 5, 14, 0, tzinfo=UTC).isoformat(),
             datetime(2026, 12, 21, 14, 0, tzinfo=UTC).isoformat()),
        source_timezone=UTC,
    )
    issues = [i for i in validate(bars, interval_minutes=5)
              if i.code == "SPANS_CONTRACT_ROLL"]
    assert len(issues) == 4, "one per quarter"
