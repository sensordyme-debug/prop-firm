"""Tests for config, normalisation, reconciliation, performance, research and ORB.

All offline. Where a broker is needed, it is a fake — no test may require
credentials, and none may reach a network.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest

import research
from backtest import (
    Bar,
    BarSeries,
    BarTimestamp,
    BarWindow,
    CostModel,
    StrategyState,
    run_backtest,
)
from config import AppConfig, ConfigError, load_config
from execution.broker import ContractSpec
from execution.models import (
    AuthenticationError,
    BrokerUnavailableError,
    MarketBar,
    OrderState,
    OrderType,
    Position,
    RateLimitError,
    Side,
    WorkingOrder,
)
from marketdata import ET, infer_timestamp_convention, normalise, to_bar_series
from performance import Unavailable, analyse
from reconciliation import reconcile
from research import monte_carlo, split, walk_forward_windows
from strategies.orb import OpeningRangeBreakout, OrbConfig, compute_atr, opening_range

UTC = UTC


# ===========================================================================
# Configuration
# ===========================================================================


def test_config_round_trips_into_the_governor():
    cfg = AppConfig(daily_max_loss=200.0, floor_buffer=350.0)
    gov = cfg.to_governor_config()
    assert gov.daily_max_loss == 200.0
    assert gov.floor_buffer == 350.0
    assert gov.hard_flatten_et == time(15, 55)


def test_config_rejects_an_invalid_timezone():
    with pytest.raises(ConfigError, match="not a valid IANA zone"):
        AppConfig(timezone="Mars/Olympus")


def test_config_rejects_an_inverted_trading_window():
    with pytest.raises(ConfigError, match="before entry_cutoff"):
        AppConfig(rth_open=time(12, 0), entry_cutoff=time(11, 30))


def test_config_rejects_oversized_position():
    with pytest.raises(ConfigError, match="exceeds"):
        AppConfig(position_size=50)


def test_load_config_reads_the_environment(monkeypatch):
    monkeypatch.setenv("DAILY_MAX_LOSS", "175")
    monkeypatch.setenv("POSITION_SIZE", "3")
    monkeypatch.setenv("HARD_FLATTEN", "15:30")
    cfg = load_config(load_dotenv_file=False)
    assert cfg.daily_max_loss == 175.0
    assert cfg.position_size == 3
    assert cfg.hard_flatten == time(15, 30)


def test_load_config_names_an_unparseable_risk_value(monkeypatch):
    monkeypatch.setenv("FLOOR_BUFFER", "four hundred")
    with pytest.raises(ConfigError, match="FLOOR_BUFFER"):
        load_config(load_dotenv_file=False)


# ===========================================================================
# Market data normalisation
# ===========================================================================


def mbar(ts: datetime, price: float = 100.0) -> MarketBar:
    return MarketBar(timestamp=ts, open=price, high=price + 1, low=price - 1,
                     close=price, volume=10, symbol="MNQ")


def test_normalise_sorts_and_deduplicates():
    base = datetime(2026, 9, 16, 13, 35, tzinfo=UTC)
    bars = [mbar(base + timedelta(minutes=10)), mbar(base), mbar(base)]
    out = normalise(bars)
    assert len(out) == 2
    assert out[0].timestamp < out[1].timestamp


def test_normalise_refuses_naive_timestamps_without_a_declared_zone():
    with pytest.raises(ValueError, match="naive"):
        normalise([mbar(datetime(2026, 9, 16, 9, 35))])


def test_normalise_localises_with_an_explicit_zone():
    out = normalise([mbar(datetime(2026, 9, 16, 9, 35))], assume_timezone=ET)
    assert out[0].timestamp.tzinfo is not None
    assert out[0].timestamp.astimezone(ET).hour == 9


def test_to_bar_series_requires_a_convention():
    bars = [mbar(datetime(2026, 9, 16, 13, 35, tzinfo=UTC))]
    with pytest.raises(TypeError):
        to_bar_series(bars)  # type: ignore[call-arg]


def test_to_bar_series_still_refuses_unknown():
    """The guard the whole normalisation layer exists to protect."""
    from backtest import BarConventionError

    bars = [mbar(datetime(2026, 9, 16, 13, 35, tzinfo=UTC))]
    with pytest.raises(BarConventionError, match="UNVERIFIED"):
        to_bar_series(bars, convention=BarTimestamp.UNKNOWN)


def test_infer_suggests_open_labelled_when_a_bar_sits_on_the_open():
    bars = [
        mbar(datetime(2026, 9, 16, 9, 30, tzinfo=ET)),
        mbar(datetime(2026, 9, 16, 9, 35, tzinfo=ET)),
        mbar(datetime(2026, 9, 16, 9, 40, tzinfo=ET)),
    ]
    finding = infer_timestamp_convention(bars)
    assert finding.suggested is BarTimestamp.OPEN
    assert finding.confident


def test_infer_returns_unknown_for_naive_timestamps():
    """Evidence, not a decision — and the honest answer here is 'unknown'."""
    finding = infer_timestamp_convention([mbar(datetime(2026, 9, 16, 9, 35))])
    assert finding.suggested is BarTimestamp.UNKNOWN
    assert not finding.confident
    assert any("NAIVE" in r for r in finding.reasoning)


# ===========================================================================
# Reconciliation
# ===========================================================================


def pos(size: int) -> Position:
    return Position(contract_id="CON.F.US.MNQ.Z26", size=size, average_price=20_000.0)


def wo(order_id: str) -> WorkingOrder:
    return WorkingOrder(order_id=order_id, contract_id="CON.F.US.MNQ.Z26",
                        side=Side.BUY, order_type=OrderType.STOP, size=2,
                        state=OrderState.UNKNOWN)


def test_flat_broker_and_flat_local_reconciles():
    result = reconcile(broker_positions=[], broker_orders=[])
    assert result.passed
    assert result.may_trade


def test_an_unexpected_position_fails_closed():
    """Local state said flat; the broker disagrees. This must block trading."""
    result = reconcile(broker_positions=[pos(2)], broker_orders=[])
    assert not result.passed
    assert not result.may_trade
    assert any(d.kind == "POSITION_MISMATCH" for d in result.discrepancies)
    assert "FLAT" in str(result.discrepancies[0])


def test_an_unexpected_working_order_fails_closed():
    result = reconcile(broker_positions=[], broker_orders=[wo("abc")])
    assert not result.passed
    assert any(d.kind == "UNEXPECTED_ORDER" for d in result.discrepancies)


def test_a_missing_expected_order_is_a_discrepancy():
    result = reconcile(broker_positions=[], broker_orders=[],
                       expected_order_ids=["gone"])
    assert not result.passed
    assert any(d.kind == "MISSING_ORDER" for d in result.discrepancies)


def test_a_matching_position_reconciles():
    result = reconcile(broker_positions=[pos(2)], broker_orders=[],
                       expected_position_size=2)
    assert result.passed


def test_reconciliation_never_proposes_a_corrective_order():
    """It reports; it does not fix. The rendered output says so explicitly."""
    result = reconcile(broker_positions=[pos(-2)], broker_orders=[])
    rendered = result.render()
    assert "BLOCKED" in rendered
    assert "human" in rendered.lower()


# ===========================================================================
# Broker error classification
# ===========================================================================


def test_broker_errors_separate_rejection_from_uncertainty():
    from brokers.projectx import classify_broker_error

    assert isinstance(classify_broker_error(Exception("HTTP 401")), AuthenticationError)
    assert isinstance(classify_broker_error(Exception("429 rate")), RateLimitError)
    assert isinstance(classify_broker_error(TimeoutError("timed out")),
                      BrokerUnavailableError)
    # Anything unrecognised is UNCERTAIN, never assumed to have failed safely.
    assert isinstance(classify_broker_error(Exception("weird")),
                      BrokerUnavailableError)


def test_contract_spec_point_value_and_conservative_tick_rounding():
    spec = ContractSpec(contract_id="C", symbol="MNQ", tick_size=0.25, tick_value=0.50)
    assert spec.point_value == 2.00
    # 10 points at 0.25 = 40 ticks exactly.
    assert spec.ticks_between(20_000.0, 20_010.0) == 40
    # A fractional distance rounds UP, never to a tighter stop.
    assert spec.ticks_between(20_000.0, 20_000.30) == 2


# ===========================================================================
# Performance
# ===========================================================================


def tiny_backtest():
    def never(bars, state, config):
        return None

    bars = tuple(
        Bar(ts=datetime(2026, 9, 16, 13, 35, tzinfo=UTC) + timedelta(minutes=5 * i),
            open=20_000, high=20_002, low=19_998, close=20_000)
        for i in range(6)
    )
    series = BarSeries(bars=bars, interval_minutes=5,
                       timestamp_convention=BarTimestamp.CLOSE)
    return run_backtest(series, never)


def test_performance_report_renders_and_serialises():
    report = analyse(tiny_backtest(), strategy="none")
    text = report.render()
    assert "BACKTEST PERFORMANCE REPORT" in text
    assert "RISK ADJUSTED" in text
    assert "FINAL VERDICT" in text
    payload = report.to_dict()
    assert payload["trades"]["total"] == 0


def test_ratios_are_unavailable_rather_than_invented_on_a_tiny_sample():
    """A Sharpe from six bars is noise wearing a number."""
    report = analyse(tiny_backtest())
    assert isinstance(report.risk.sharpe, Unavailable)
    assert isinstance(report.risk.sortino, Unavailable)
    assert "need" in report.risk.sharpe.reason


def test_unavailable_is_falsey_and_serialises_with_its_reason():
    u = Unavailable("not enough data")
    assert not u
    assert "not enough data" in str(u)
    report = analyse(tiny_backtest())
    sharpe = report.to_dict()["risk"]["sharpe"]
    assert sharpe["value"] is None
    assert "unavailable_because" in sharpe


def test_profit_factor_is_unavailable_with_no_losses_not_infinite():
    from performance import _trade_stats

    class T:
        def __init__(self, net):
            self.net = net
            self.commission = 0.0
            self.entry_time = datetime(2026, 9, 16, 13, 35, tzinfo=UTC)
            self.exit_time = datetime(2026, 9, 16, 13, 40, tzinfo=UTC)
            self.size = 2
            self.exit_kind = "TARGET"

    stats = _trade_stats([T(10.0), T(20.0)])
    assert isinstance(stats.profit_factor, Unavailable)
    assert "undefined, not infinite" in stats.profit_factor.reason


def test_the_report_states_its_annualisation_basis():
    assert "252" in analyse(tiny_backtest()).risk.basis


# ===========================================================================
# Research utilities
# ===========================================================================


def test_split_is_chronological_and_never_shuffles():
    items = list(range(100))
    parts = split(items)
    assert parts.train == tuple(range(50))
    assert parts.validation == tuple(range(50, 75))
    assert parts.test == tuple(range(75, 100))
    assert list(parts.train) + list(parts.validation) + list(parts.test) == items


def test_split_rejects_impossible_fractions():
    with pytest.raises(ValueError):
        split(list(range(10)), train=0.9, validation=0.2)


def test_walk_forward_test_windows_always_follow_their_training_window():
    windows = walk_forward_windows(list(range(100)), train_size=40, test_size=20)
    assert windows
    for w in windows:
        assert max(w.train) < min(w.test), "the test window must be in the future"


def test_walk_forward_windows_roll_forward():
    windows = walk_forward_windows(list(range(100)), train_size=40, test_size=20)
    assert [w.index for w in windows] == list(range(len(windows)))
    assert windows[1].train[0] > windows[0].train[0]


def test_monte_carlo_is_deterministic_for_a_given_seed():
    pnls = [50.0, -30.0, 80.0, -41.82, 12.0, -5.0]
    a = monte_carlo(pnls, simulations=200, seed=7)
    b = monte_carlo(pnls, simulations=200, seed=7)
    assert a.returns == b.returns
    assert a.drawdowns == b.drawdowns


def test_shuffle_preserves_total_profit_and_only_varies_the_path():
    pnls = [50.0, -30.0, 80.0, -40.0]
    result = monte_carlo(pnls, simulations=100, method="shuffle", seed=1)
    assert all(abs(r - sum(pnls)) < 1e-9 for r in result.returns)
    assert len(set(result.drawdowns)) > 1, "the PATH must vary"


def test_bootstrap_varies_the_total_as_well():
    pnls = [50.0, -30.0, 80.0, -40.0]
    result = monte_carlo(pnls, simulations=200, method="bootstrap", seed=1)
    assert len(set(result.returns)) > 1


def test_monte_carlo_reports_drawdown_exceedance_probability():
    result = monte_carlo([100.0, -100.0] * 20, simulations=300, seed=3)
    p = result.probability_drawdown_exceeds(50.0)
    assert 0.0 <= p <= 1.0
    assert "P(max DD >" in result.render()


def test_monte_carlo_handles_an_empty_trade_list():
    assert monte_carlo([], simulations=10).simulations == 0


def test_sensitivity_table_does_not_rank_by_profit():
    rows = [
        research.ParameterResult({"atr": 10}, net_profit=5_000, max_drawdown=900,
                                 trades=210, passed_gates=False),
        research.ParameterResult({"atr": 14}, net_profit=1_000, max_drawdown=400,
                                 trades=230, passed_gates=True),
    ]
    table = research.sensitivity_table(rows)
    assert table.index("10") < table.index("14"), "sorted by parameter, not profit"
    assert "STABLE REGION" in table


# ===========================================================================
# ORB strategy
# ===========================================================================


def et_bar(h: int, m: int, o: float, hi: float, lo: float, c: float) -> Bar:
    return Bar(ts=datetime(2026, 9, 16, h, m, tzinfo=ET), open=o, high=hi, low=lo, close=c)


def orb_session() -> list[Bar]:
    """09:35-09:45 forms the range; later bars break out."""
    return [
        et_bar(9, 35, 20_000, 20_010, 19_990, 20_005),
        et_bar(9, 40, 20_005, 20_012, 19_995, 20_008),
        et_bar(9, 45, 20_008, 20_015, 20_000, 20_010),
        et_bar(9, 50, 20_010, 20_014, 20_005, 20_012),
        et_bar(9, 55, 20_012, 20_016, 20_008, 20_014),
        et_bar(10, 0, 20_014, 20_030, 20_012, 20_028),   # breaks above 20,015
    ]


def test_atr_needs_enough_history_and_returns_none_otherwise():
    bars = orb_session()
    assert compute_atr(bars, period=14) is None
    assert compute_atr(bars, period=3) is not None


def test_no_signal_before_the_opening_range_completes():
    bars = orb_session()
    strat = OpeningRangeBreakout(OrbConfig(atr_period=3))
    window = BarWindow(tuple(bars), upto=1)  # still inside 09:30-09:45
    assert strat.on_bar(window, StrategyState(
        session_start=datetime(2026, 9, 15, 18, 0, tzinfo=ET),
        trading_date=date(2026, 9, 16))) is None


def test_the_opening_range_is_the_window_high_and_low():
    window = BarWindow(tuple(orb_session()), upto=5)
    rng = opening_range(window, OrbConfig())
    assert rng is not None
    assert rng.high == 20_015.0
    assert rng.low == 19_990.0
    assert rng.midpoint == pytest.approx(20_002.5)


def test_a_close_beyond_the_range_produces_a_long_signal():
    window = BarWindow(tuple(orb_session()), upto=5)
    strat = OpeningRangeBreakout(OrbConfig(atr_period=3, max_risk_dollars=200.0))
    signal = strat.on_bar(window, StrategyState(
        session_start=datetime(2026, 9, 15, 18, 0, tzinfo=ET),
        trading_date=date(2026, 9, 16)))
    assert signal is not None
    assert signal.side == 1
    assert signal.stop < 20_028 < signal.target
    assert signal.size == 2


def test_a_touch_without_a_close_beyond_the_range_is_not_a_signal():
    """A touch is noise that happened to reach a price."""
    bars = orb_session()
    bars[-1] = et_bar(10, 0, 20_014, 20_030, 20_012, 20_014)  # wick only
    window = BarWindow(tuple(bars), upto=5)
    strat = OpeningRangeBreakout(OrbConfig(atr_period=3, max_risk_dollars=200.0))
    assert strat.on_bar(window, StrategyState(
        session_start=datetime(2026, 9, 15, 18, 0, tzinfo=ET),
        trading_date=date(2026, 9, 16))) is None


def test_no_signal_when_the_direction_was_already_taken():
    window = BarWindow(tuple(orb_session()), upto=5)
    strat = OpeningRangeBreakout(OrbConfig(atr_period=3, max_risk_dollars=200.0))
    state = StrategyState(
        session_start=datetime(2026, 9, 15, 18, 0, tzinfo=ET),
        trading_date=date(2026, 9, 16), long_taken=True)
    assert strat.on_bar(window, state) is None


def test_no_signal_while_a_position_is_open():
    window = BarWindow(tuple(orb_session()), upto=5)
    strat = OpeningRangeBreakout(OrbConfig(atr_period=3, max_risk_dollars=200.0))
    state = StrategyState(
        session_start=datetime(2026, 9, 15, 18, 0, tzinfo=ET),
        trading_date=date(2026, 9, 16), position_side=1)
    assert strat.on_bar(window, state) is None


def test_a_trade_whose_risk_exceeds_the_cap_is_refused_not_tightened():
    """Tightening the stop would raise the chance of a noise stop-out."""
    window = BarWindow(tuple(orb_session()), upto=5)
    strat = OpeningRangeBreakout(OrbConfig(atr_period=3, max_risk_dollars=1.0))
    assert strat.on_bar(window, StrategyState(
        session_start=datetime(2026, 9, 15, 18, 0, tzinfo=ET),
        trading_date=date(2026, 9, 16))) is None


def test_orb_config_rejects_nonsense():
    for kwargs in ({"atr_period": 1}, {"reward_multiple": 0}, {"size": 0},
                   {"max_risk_dollars": -1}):
        with pytest.raises(ValueError):
            OrbConfig(**kwargs)


def test_the_strategy_is_pure_and_callable_by_the_backtester():
    """Same object drives both paths; no backtest/live divergence."""
    strat = OpeningRangeBreakout(OrbConfig(atr_period=3, max_risk_dollars=200.0))
    window = BarWindow(tuple(orb_session()), upto=5)
    state = StrategyState(session_start=datetime(2026, 9, 15, 18, 0, tzinfo=ET),
                          trading_date=date(2026, 9, 16))
    assert strat(window, state, None) == strat.on_bar(window, state)


def test_orb_runs_end_to_end_through_the_real_backtester():
    series = BarSeries(bars=tuple(orb_session()), interval_minutes=5,
                       timestamp_convention=BarTimestamp.CLOSE)
    result = run_backtest(
        series,
        OpeningRangeBreakout(OrbConfig(atr_period=3, max_risk_dollars=200.0)),
        costs=CostModel(slippage_ticks=0.0),
    )
    assert result.bars_seen == 6
    assert result.harness_faults == ()


def test_infer_suggests_close_labelled_when_the_session_starts_one_bar_late():
    """CLOSE-labelled: the first bar covering 09:30-09:35 is stamped 09:35."""
    bars = [
        mbar(datetime(2026, 9, 16, 9, 35, tzinfo=ET)),
        mbar(datetime(2026, 9, 16, 9, 40, tzinfo=ET)),
        mbar(datetime(2026, 9, 16, 9, 45, tzinfo=ET)),
    ]
    finding = infer_timestamp_convention(bars)
    assert finding.suggested is BarTimestamp.CLOSE
    assert finding.confident


def test_infer_is_unknown_when_sessions_disagree():
    """Honest 'cannot tell' beats a confident wrong answer."""
    bars = [
        mbar(datetime(2026, 9, 16, 9, 30, tzinfo=ET)),
        mbar(datetime(2026, 9, 16, 9, 35, tzinfo=ET)),
        mbar(datetime(2026, 9, 17, 9, 35, tzinfo=ET)),
        mbar(datetime(2026, 9, 17, 9, 40, tzinfo=ET)),
    ]
    assert infer_timestamp_convention(bars).suggested is BarTimestamp.UNKNOWN


def test_inference_is_evidence_and_never_reaches_the_backtester_by_itself():
    """Nothing consumes `suggested` automatically; a human records the answer."""
    import inspect

    import marketdata

    source = inspect.getsource(marketdata.to_bar_series)
    assert "infer_timestamp_convention" not in source
    assert "suggested" not in source
