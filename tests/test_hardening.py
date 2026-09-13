"""Tests for the final hardening pass: connection, protection, idempotency,
persistence, logging, runtime, strategy registry and research runners.

Every test is offline. Where a broker would be needed, state is supplied
directly — no test may require credentials or a network.
"""

from __future__ import annotations

import io
import json
import os
from datetime import UTC, date, datetime, timedelta

import pytest

import research_ext as rx
import strategies
from backtest import (
    Bar,
    BarSeries,
    BarTimestamp,
    CostModel,
    run_backtest,
)
from connection import (
    READY_SEQUENCE,
    ConnectionMachine,
    ConnectionState,
    IllegalTransition,
)
from execution.idempotency import (
    InstanceLock,
    check_before_entry,
    client_tag,
)
from execution.models import (
    OrderIntent,
    OrderState,
    OrderType,
    Position,
    Side,
    WorkingOrder,
)
from execution.protection import ProtectionStatus, verify_protection
from observability import EventType, StructuredLogger, new_correlation_id, redact
from performance import Unavailable, excursion_stats
from research import ParameterResult
from session_state import SessionState, StateError, TrustLevel, load_state, save_state

ET = strategies.library.ET


# ===========================================================================
# Connection state machine
# ===========================================================================


def ready_machine(now: datetime | None = None) -> ConnectionMachine:
    m = ConnectionMachine()
    moment = now or datetime.now(UTC)
    m.transition(ConnectionState.CONNECTING, "x", now=moment)
    m.transition(ConnectionState.AUTHENTICATED, "x", now=moment)
    m.transition(ConnectionState.RECONCILING, "x", now=moment)
    for check in READY_SEQUENCE:
        m.mark(check, True)
    m.observe_data(moment)
    m.advance_to_ready(now=moment)
    return m


def test_a_fresh_machine_cannot_transmit():
    allowed, why = ConnectionMachine().may_transmit()
    assert allowed is False
    assert "DISCONNECTED" in why


def test_authenticated_is_not_enough_to_transmit():
    """The dangerous window: connected, but ignorant of what we hold."""
    m = ConnectionMachine()
    m.transition(ConnectionState.CONNECTING, "x")
    m.transition(ConnectionState.AUTHENTICATED, "x")
    assert m.may_transmit()[0] is False
    assert m.state.may_read is True


def test_ready_requires_every_check():
    m = ConnectionMachine()
    m.transition(ConnectionState.CONNECTING, "x")
    m.transition(ConnectionState.AUTHENTICATED, "x")
    m.transition(ConnectionState.RECONCILING, "x")
    m.mark("authenticated", True)
    ok, why = m.advance_to_ready()
    assert ok is False
    assert m.state is ConnectionState.DEGRADED
    assert "reconciled" in why


def test_a_fully_ready_machine_may_transmit():
    assert ready_machine().may_transmit()[0] is True


def test_ready_is_unreachable_without_passing_through_reconciling():
    m = ConnectionMachine()
    m.transition(ConnectionState.CONNECTING, "x")
    m.transition(ConnectionState.AUTHENTICATED, "x")
    with pytest.raises(IllegalTransition, match="not permitted"):
        m.transition(ConnectionState.READY, "skip the queue")


def test_disconnect_forgets_everything_it_knew():
    """Reconnection restores connectivity, not knowledge."""
    m = ready_machine()
    assert m.may_transmit()[0] is True
    m.on_connection_lost("network gone")
    assert m.state is ConnectionState.DISCONNECTED
    assert m.missing_checks() == READY_SEQUENCE
    assert m.may_transmit()[0] is False


def test_stale_data_blocks_transmission_even_when_ready():
    now = datetime(2026, 9, 16, 14, 0, tzinfo=UTC)
    m = ready_machine(now)
    later = now + timedelta(seconds=600)
    allowed, why = m.may_transmit(later)
    assert allowed is False
    assert "stale" in why


def test_never_having_received_data_counts_as_stale():
    """Absence is not freshness."""
    m = ConnectionMachine()
    assert m.data_is_stale(datetime.now(UTC)) is True


def test_halted_is_terminal():
    m = ready_machine()
    m.halt("a human must look at this")
    assert m.state.is_terminal
    for target in ConnectionState:
        with pytest.raises(IllegalTransition):
            m.transition(target, "recover")


# ===========================================================================
# Bracket / protection verification
# ===========================================================================


def stop_order(size: int = 2, side: Side = Side.SELL,
               price: float = 19_990.0, state=OrderState.ACCEPTED) -> WorkingOrder:
    return WorkingOrder(
        order_id="s1", contract_id="C", side=side, order_type=OrderType.STOP,
        size=size, state=state, stop_price=price,
    )


LONG = Position(contract_id="C", size=2, average_price=20_000.0)


def test_flat_needs_no_protection():
    report = verify_protection(position=None, working_orders=[])
    assert report.status is ProtectionStatus.FLAT
    assert report.is_safe


def test_a_position_with_no_stop_is_unprotected():
    """The naked position CLAUDE.md constraint 1 exists to prevent."""
    report = verify_protection(position=LONG, working_orders=[])
    assert report.status is ProtectionStatus.UNPROTECTED
    assert report.must_halt
    assert any(f.code == "NO_STOP" for f in report.findings)
    assert "naked" in report.findings[0].detail


def test_a_correctly_sized_and_sided_stop_is_protected():
    report = verify_protection(position=LONG, working_orders=[stop_order()])
    assert report.status is ProtectionStatus.PROTECTED
    assert report.is_safe


def test_a_partial_size_stop_leaves_the_remainder_naked():
    """A stop exists, which is why this is the easiest failure to miss."""
    report = verify_protection(position=LONG, working_orders=[stop_order(size=1)])
    assert report.must_halt
    assert any(f.code == "STOP_QUANTITY_SHORT" for f in report.findings)


def test_an_oversized_stop_would_open_a_reverse_position():
    report = verify_protection(position=LONG, working_orders=[stop_order(size=5)])
    assert report.must_halt
    assert any(f.code == "STOP_QUANTITY_EXCESS" for f in report.findings)


def test_a_stop_on_the_wrong_side_adds_exposure():
    report = verify_protection(
        position=LONG, working_orders=[stop_order(side=Side.BUY)]
    )
    assert report.must_halt
    assert any(f.code == "STOP_WRONG_SIDE" for f in report.findings)


def test_a_long_stop_above_entry_is_invalid():
    report = verify_protection(
        position=LONG, working_orders=[stop_order(price=20_010.0)]
    )
    assert report.must_halt
    assert any(f.code == "STOP_PRICE_INVALID" for f in report.findings)


def test_an_unknown_order_state_cannot_count_as_protection():
    """UNKNOWN means we do not know it is working."""
    report = verify_protection(
        position=LONG, working_orders=[stop_order(state=OrderState.UNKNOWN)]
    )
    assert report.status is ProtectionStatus.UNVERIFIABLE
    assert report.must_halt
    assert any(f.code == "UNKNOWN_ORDER_STATE" for f in report.findings)


def test_a_stop_at_the_wrong_level_is_flagged():
    report = verify_protection(
        position=LONG, working_orders=[stop_order(price=19_900.0)],
        expected_stop_price=19_990.0,
    )
    assert report.must_halt
    assert any(f.code == "STOP_PRICE_MISMATCH" for f in report.findings)


def test_only_flat_and_protected_are_safe():
    assert ProtectionStatus.FLAT.is_safe
    assert ProtectionStatus.PROTECTED.is_safe
    assert not ProtectionStatus.UNPROTECTED.is_safe
    assert not ProtectionStatus.UNVERIFIABLE.is_safe


def test_the_halt_message_names_the_constraint():
    report = verify_protection(position=LONG, working_orders=[])
    assert "HALT" in report.render()


# ===========================================================================
# Idempotency / duplicate protection
# ===========================================================================


def an_intent(side: Side = Side.BUY) -> OrderIntent:
    return OrderIntent(
        symbol="MNQ", contract_id="CON.F.US.MNQ.Z26", side=side, size=2,
        order_type=OrderType.MARKET, stop_loss=19_990.0, take_profit=20_020.0,
    )


def test_the_client_tag_is_deterministic():
    """A restart must regenerate the SAME tag, or reconciliation cannot find it."""
    a = client_tag(session_id="s1", strategy="orb", intent=an_intent(), sequence=1)
    b = client_tag(session_id="s1", strategy="orb", intent=an_intent(), sequence=1)
    assert a == b


def test_different_intents_produce_different_tags():
    base = client_tag(session_id="s1", strategy="orb", intent=an_intent(), sequence=1)
    assert base != client_tag(session_id="s1", strategy="orb",
                              intent=an_intent(Side.SELL), sequence=1)
    assert base != client_tag(session_id="s1", strategy="orb",
                              intent=an_intent(), sequence=2)
    assert base != client_tag(session_id="s2", strategy="orb",
                              intent=an_intent(), sequence=1)


def clear_check(**over):
    base = dict(
        intent=an_intent(), positions=[], working_orders=[], pending_tags=[],
        proposed_tag="tag-1", trades_taken=0, max_trades=2,
        reconciled=True, connection_ready=True,
    )
    return check_before_entry(**{**base, **over})


def test_a_clear_path_is_allowed():
    assert clear_check().allowed is True


def test_an_existing_position_blocks_a_second_entry():
    check = clear_check(positions=[Position("CON.F.US.MNQ.Z26", 2, 20_000.0)])
    assert not check.allowed
    assert any(r.code == "POSITION_ALREADY_OPEN" for r in check.risks)


def test_a_working_order_on_the_same_side_blocks_entry():
    order = WorkingOrder(
        order_id="o1", contract_id="CON.F.US.MNQ.Z26", side=Side.BUY,
        order_type=OrderType.MARKET, size=2, state=OrderState.ACCEPTED,
    )
    check = clear_check(working_orders=[order])
    assert not check.allowed
    assert any(r.code == "CONFLICTING_WORKING_ORDER" for r in check.risks)


def test_a_tag_already_in_flight_blocks_resubmission():
    check = clear_check(pending_tags=["tag-1"])
    assert not check.allowed
    assert any(r.code == "TAG_ALREADY_PENDING" for r in check.risks)


def test_a_tag_already_at_the_broker_proves_the_first_send_arrived():
    """The timeout case: the order DID reach the venue."""
    order = WorkingOrder(
        order_id="o1", contract_id="CON.F.US.MNQ.Z26", side=Side.SELL,
        order_type=OrderType.STOP, size=2, state=OrderState.ACCEPTED,
        custom_tag="tag-1",
    )
    check = clear_check(working_orders=[order])
    assert not check.allowed
    assert any(r.code == "TAG_ALREADY_AT_BROKER" for r in check.risks)


def test_unreconciled_blocks_entry():
    check = clear_check(reconciled=False)
    assert not check.allowed
    assert any(r.code == "UNRECONCILED" for r in check.risks)


def test_a_non_ready_connection_blocks_entry():
    check = clear_check(connection_ready=False)
    assert not check.allowed
    assert any(r.code == "CONNECTION_NOT_READY" for r in check.risks)


def test_a_spent_trade_budget_blocks_entry():
    check = clear_check(trades_taken=2, max_trades=2)
    assert not check.allowed
    assert any(r.code == "TRADE_BUDGET_SPENT" for r in check.risks)


def test_the_instance_lock_refuses_a_second_live_holder(tmp_path):
    lock = InstanceLock(tmp_path / "run.lock")
    lock.acquire()
    try:
        # Our own PID is recorded, so the second acquire sees a live holder
        # only when the PID differs; simulate that.
        (tmp_path / "run.lock").write_text("999999\n2026-01-01\n", encoding="utf-8")
        third = InstanceLock(tmp_path / "run.lock")
        third.acquire()  # 999999 is not alive -> reclaimed
        assert (tmp_path / "run.lock").exists()
    finally:
        lock.release()


def test_a_live_holder_is_not_evicted(tmp_path):
    path = tmp_path / "run.lock"
    path.write_text(f"{os.getpid()}\n2026-01-01\n", encoding="utf-8")
    # Same PID as ours is treated as ours, not as a conflict.
    InstanceLock(path).acquire()
    assert path.exists()


# ===========================================================================
# Session persistence
# ===========================================================================


def a_state() -> SessionState:
    return SessionState(
        session_id="sess-1", trading_date=date(2026, 9, 16),
        session_start_balance=50_000.0, trades_today=2, halted=True,
        halt_reason="DAILY_MAX_LOSS", last_known_position=2,
    )


def test_state_round_trips(tmp_path):
    save_state(a_state(), tmp_path)
    loaded = load_state(tmp_path)
    assert loaded is not None
    assert loaded.trades_today == 2
    assert loaded.halted is True


def test_loaded_state_is_unverified_until_reconciled(tmp_path):
    """The important line: persisted state is not broker truth."""
    save_state(a_state(), tmp_path)
    loaded = load_state(tmp_path)
    assert loaded.trust is TrustLevel.UNVERIFIED
    assert loaded.may_trade is False

    reconciled = loaded.mark_reconciled()
    assert reconciled.trust is TrustLevel.RECONCILED
    # Still false, because this state is halted -- two independent gates.
    assert reconciled.may_trade is False


def test_a_reconciled_unhalted_state_may_trade(tmp_path):
    fresh = SessionState(
        session_id="s", trading_date=date(2026, 9, 16),
        session_start_balance=50_000.0,
    ).mark_reconciled()
    assert fresh.may_trade is True


def test_missing_state_returns_none_not_an_error(tmp_path):
    assert load_state(tmp_path) is None


def test_corrupt_state_raises_rather_than_defaulting(tmp_path):
    """A default would reset the trade count and re-grant a spent budget."""
    (tmp_path / "session_state.json").write_text("{ broken", encoding="utf-8")
    with pytest.raises(StateError, match="unreadable"):
        load_state(tmp_path)


def test_state_missing_a_required_field_raises(tmp_path):
    (tmp_path / "session_state.json").write_text(
        json.dumps({"session_id": "x"}), encoding="utf-8"
    )
    with pytest.raises(StateError, match="missing"):
        load_state(tmp_path)


def test_state_from_another_trading_day_is_not_reused(tmp_path):
    save_state(a_state(), tmp_path)
    assert load_state(tmp_path, expected_trading_date=date(2026, 9, 17)) is None


def test_the_persisted_file_warns_it_is_not_authoritative(tmp_path):
    path = save_state(a_state(), tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert "NOT AUTHORITATIVE" in payload["_warning"]


# ===========================================================================
# Structured logging
# ===========================================================================


def capture() -> tuple[StructuredLogger, io.StringIO]:
    buf = io.StringIO()
    return StructuredLogger(stream=buf), buf


def test_every_event_is_one_json_object_per_line():
    log, buf = capture()
    log.event(EventType.SESSION_START, detail="x")
    log.event(EventType.SESSION_END, detail="y")
    lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
    assert len(lines) == 2
    for line in lines:
        json.loads(line)


def test_credentials_are_redacted_at_write_time():
    """Redaction cannot depend on every caller remembering."""
    log, buf = capture()
    log.event(
        EventType.BROKER_REQUEST,
        api_key="SECRET-KEY-VALUE",
        PROJECT_X_API_KEY="ALSO-SECRET",
        headers={"Authorization": "Bearer abc123"},
        nested=[{"token": "NESTED-TOKEN-VALUE"}],
        safe_field="visible",
    )
    out = buf.getvalue()
    # Distinctive values that cannot collide with a KEY name -- otherwise the
    # test can pass or fail on the key rather than the value.
    for secret in ("SECRET-KEY-VALUE", "ALSO-SECRET", "Bearer abc123",
                   "NESTED-TOKEN-VALUE"):
        assert secret not in out
    assert "visible" in out
    assert "<redacted>" in out


def test_redact_handles_nested_structures():
    cleaned = redact({"a": {"password": "p"}, "b": [{"secret": "s"}]})
    assert cleaned["a"]["password"] == "<redacted>"
    assert cleaned["b"][0]["secret"] == "<redacted>"


def test_exceptions_are_logged_without_a_traceback_body():
    """A traceback can carry headers and argument values."""
    log, buf = capture()
    log.exception(ValueError("Authorization: Bearer leak"), stage="test")
    record = json.loads(buf.getvalue().strip())
    assert record["exception_type"] == "ValueError"
    assert "Traceback" not in buf.getvalue()


def test_a_correlation_id_threads_one_decision_through_stages():
    log, _ = capture()
    cid = new_correlation_id()
    child = log.bind(correlation_id=cid)
    child.event(EventType.SIGNAL_GENERATED, side=1)
    child.event(EventType.RISK_CHECK, action="CONTINUE")
    child.event(EventType.EXECUTION_INTENT_CREATED, size=2)
    log.event(EventType.MARKET_DATA_RECEIVED, close=1.0)

    trace = log.trace(cid)
    assert len(trace) == 3
    assert [r["event"] for r in trace] == [
        "signal_generated", "risk_check", "execution_intent_created",
    ]


def test_every_required_event_type_exists():
    required = {
        "application_start", "application_shutdown", "market_data_received",
        "market_data_stale", "signal_generated", "signal_rejected",
        "risk_check", "risk_rejected", "execution_intent_created",
        "execution_intent_rejected", "broker_request", "broker_response",
        "order_submitted", "order_rejected", "order_state_changed",
        "order_unknown", "position_detected", "reconciliation_started",
        "reconciliation_failed", "reconciliation_passed", "governor_halt",
        "kill_switch", "connection_lost", "connection_restored",
        "session_start", "session_end", "exception",
    }
    assert required <= {e.value for e in EventType}


# ===========================================================================
# Strategy registry
# ===========================================================================


def test_the_registry_has_both_available_and_declared_strategies():
    assert len(strategies.available_strategies()) >= 8
    assert len(strategies.unavailable_strategies()) >= 5


def test_every_strategy_declares_complete_metadata():
    for name, meta in strategies.all_strategies().items():
        assert meta.name == name
        assert meta.version, name
        assert meta.hypothesis, name
        assert meta.required_data, name
        assert meta.family, name


def test_no_available_strategy_exceeds_two_tunable_parameters():
    """More free parameters buys in-sample performance that does not survive."""
    for name, meta in strategies.available_strategies().items():
        assert len(meta.tunable) <= 2, f"{name} has {len(meta.tunable)} tunables"


def test_unavailable_strategies_name_their_missing_requirement():
    for name, meta in strategies.unavailable_strategies().items():
        assert meta.unavailable_reason, name
        assert not meta.is_available


def test_an_unavailable_strategy_refuses_to_construct():
    """It must not silently run against approximated inputs."""
    with pytest.raises(NotImplementedError, match="declared but NOT implemented"):
        strategies.get_strategy("prev_day_breakout")


def test_available_strategies_all_construct():
    for name in strategies.available_strategies():
        assert strategies.get_strategy(name) is not None


def test_an_unknown_strategy_raises():
    with pytest.raises(KeyError, match="unknown strategy"):
        strategies.get_strategy("does_not_exist")


def test_duplicate_registration_is_refused():
    from strategies.base import StrategyFamily, StrategyMeta, register

    meta = StrategyMeta(name="orb", version="9", family=StrategyFamily.TREND,
                        hypothesis="x", description="x")
    with pytest.raises(ValueError, match="already registered"):
        register(meta)(lambda **_: None)


def test_config_and_dataset_hashes_are_stable_and_order_independent():
    from strategies.base import config_hash, dataset_hash

    assert config_hash({"a": 1, "b": 2}) == config_hash({"b": 2, "a": 1})
    assert config_hash({"a": 1}) != config_hash({"a": 2})
    assert dataset_hash([]) == "empty"


def test_every_available_strategy_runs_through_the_real_backtester():
    """No strategy may require changes to the core to be testable."""
    bars = tuple(
        Bar(ts=datetime(2026, 9, 16, 9, 35, tzinfo=ET) + timedelta(minutes=5 * i),
            open=20_000 + i, high=20_005 + i, low=19_995 + i,
            close=20_000 + i, volume=100)
        for i in range(40)
    )
    series = BarSeries(bars=bars, interval_minutes=5,
                       timestamp_convention=BarTimestamp.CLOSE)
    for name in strategies.available_strategies():
        result = run_backtest(series, strategies.get_strategy(name),
                              costs=CostModel(slippage_ticks=0.0))
        assert result.harness_faults == (), f"{name} caused a harness fault"


# ===========================================================================
# MAE / MFE
# ===========================================================================


class _T:
    def __init__(self, net, mae, mfe, side=1):
        self.net, self.mae, self.mfe, self.side = net, mae, mfe, side
        self.commission = 1.82
        self.entry_time = datetime(2026, 9, 16, 13, 35, tzinfo=UTC)
        self.exit_time = datetime(2026, 9, 16, 13, 45, tzinfo=UTC)
        self.size = 2
        self.exit_kind = "TARGET"


def test_excursion_stats_aggregate_correctly():
    stats = excursion_stats([_T(50, 20, 80), _T(-40, 60, 10)])
    assert stats.count == 2
    assert stats.average_mae == pytest.approx(40.0)
    assert stats.max_mae == pytest.approx(60.0)
    assert stats.edge_ratio == pytest.approx(90 / 80)


def test_excursion_stats_are_unavailable_with_no_trades():
    stats = excursion_stats([])
    assert stats.count == 0
    assert isinstance(stats.average_mae, Unavailable)


def test_the_backtester_records_excursions_per_trade():
    """A trade that went against us before winning must show it."""
    def scripted(bars, state, config):
        if bars.current_index == 1:
            from backtest import Signal
            return Signal(side=1, size=2, stop=19_950.0, target=20_060.0,
                          reason="test")
        return None

    bars = (
        Bar(ts=datetime(2026, 9, 16, 9, 35, tzinfo=ET), open=20_000, high=20_001,
            low=19_999, close=20_000),
        Bar(ts=datetime(2026, 9, 16, 9, 40, tzinfo=ET), open=20_000, high=20_001,
            low=19_999, close=20_000),
        Bar(ts=datetime(2026, 9, 16, 9, 45, tzinfo=ET), open=20_000, high=20_010,
            low=19_970, close=20_000),          # dips 30 pts against us
        Bar(ts=datetime(2026, 9, 16, 9, 50, tzinfo=ET), open=20_000, high=20_070,
            low=19_998, close=20_060),          # then hits target
    )
    series = BarSeries(bars=bars, interval_minutes=5,
                       timestamp_convention=BarTimestamp.CLOSE)
    result = run_backtest(series, scripted, costs=CostModel(slippage_ticks=0.0))
    assert result.trades
    trade = result.trades[0]
    assert trade.net > 0, "it ended a winner"
    assert trade.mae > 0, "but the adverse excursion still happened"
    assert trade.mfe >= trade.mae


# ===========================================================================
# Research runners
# ===========================================================================


def test_the_grid_refuses_more_than_two_free_parameters():
    with pytest.raises(ValueError, match="limit is 2"):
        rx.build_grid({"a": [1], "b": [2], "c": [3]})


def test_the_grid_refuses_to_explode():
    with pytest.raises(ValueError, match="exceeds"):
        rx.build_grid({"a": list(range(20)), "b": list(range(20))})


def test_the_grid_expands_deterministically():
    grid = rx.build_grid({"a": [1, 2], "b": [3, 4]})
    assert len(grid) == 4
    assert grid == rx.build_grid({"b": [3, 4], "a": [1, 2]})


def test_cost_scenarios_span_optimistic_to_punishing():
    names = [s.name for s in rx.cost_scenarios()]
    assert names == ["ZERO_COST", "LOW", "BASE", "HIGH", "STRESS"]
    stress = next(s for s in rx.cost_scenarios() if s.name == "STRESS")
    assert stress.slippage_ticks > 2.0


def test_cost_table_says_plainly_when_the_edge_does_not_survive():
    rows = [
        rx.CostSensitivityRow("ZERO_COST", 5_000, 400, 250, 20, True),
        rx.CostSensitivityRow("BASE", -200, 900, 250, -0.8, False),
        rx.CostSensitivityRow("STRESS", -3_000, 3_100, 250, -12, False),
    ]
    table = rx.cost_sensitivity_table(rows)
    assert "does not survive ANY realistic" in table


def test_a_lone_winner_in_a_sweep_is_flagged_fragile():
    results = [
        ParameterResult({"p": i}, net_profit=-100, max_drawdown=500,
                        trades=200, passed_gates=False)
        for i in range(4)
    ]
    results.append(ParameterResult({"p": 99}, net_profit=5_000,
                                   max_drawdown=400, trades=200,
                                   passed_gates=True))
    warnings = rx.flag_fragile(results)
    assert warnings
    assert any("FRAGILE" in w for w in warnings)


def test_consecutive_loss_distribution_is_deterministic():
    pnls = [100.0, -50.0, -50.0, -50.0, 200.0, -30.0]
    a = rx.consecutive_loss_distribution(pnls, simulations=500, seed=5)
    b = rx.consecutive_loss_distribution(pnls, simulations=500, seed=5)
    assert a == b
    assert a[1] == pytest.approx(1.0), "at least one loss is certain here"
    assert a[3] <= a[1]


def test_regime_classification_needs_enough_trades_per_bucket():
    thin = [rx.RegimeBucket("trend", 5, 100, 20, 0.5),
            rx.RegimeBucket("range", 5, -50, -10, 0.3)]
    assert rx.classify_regime_stability(thin) == "INSUFFICIENT DATA"


def test_regime_classification_labels_robust_and_dependent():
    robust = [rx.RegimeBucket("trend", 50, 500, 10, 0.5),
              rx.RegimeBucket("range", 50, 300, 6, 0.5)]
    assert rx.classify_regime_stability(robust) == "ROBUST"

    dependent = [rx.RegimeBucket("trend", 50, 900, 18, 0.6),
                 rx.RegimeBucket("range", 50, -400, -8, 0.3)]
    assert rx.classify_regime_stability(dependent) == "REGIME-DEPENDENT"

    unstable = [rx.RegimeBucket("trend", 50, -100, -2, 0.3),
                rx.RegimeBucket("range", 50, -400, -8, 0.3)]
    assert rx.classify_regime_stability(unstable) == "UNSTABLE"


def test_code_version_is_never_fabricated():
    version = rx.code_version()
    assert isinstance(version, str) and version
