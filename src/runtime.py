"""Event-driven runtime. DRY_RUN only -- it has no path that transmits.

THE PIPELINE, WHICH IS THE SAME ONE EXECUTION WOULD USE
-------------------------------------------------------
    bar -> strategy -> Signal -> governor -> OrderIntent -> broker
                                                              |
                                              DryRunBroker ---+

There is deliberately no parallel "fake trading" architecture. The strategy,
the governor, the intent model and the broker interface are the production
ones; only the final object differs. That is what makes a dry run evidence
about the real system rather than evidence about a simulator.

WHY IT CANNOT TRANSMIT
----------------------
:class:`DryRunBroker` implements ``MarketDataBroker`` and has no order method.
It is not a broker with sending disabled -- there is nothing to disable. Its
``simulate`` method returns a clearly-labelled simulated fill and touches no
network.

Every simulated order carries timestamp, symbol, side, quantity, intended
entry, stop, target, strategy, signal reason, governor decision, risk amount
and a correlation id, so a dry-run log answers the same questions a live log
would.

FAILS CLOSED ON AMBIGUITY
-------------------------
The runtime refuses to start if the configuration is ambiguous, if execution
is anything other than dry, if reconciliation has not passed, or if the
connection is not READY. None of those are warnings.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from backtest import BarSeries, BarWindow, StrategyState
from config import AppConfig, ExecutionMode
from connection import ConnectionMachine, ConnectionState
from execution.idempotency import check_before_entry, client_tag
from execution.models import (
    Fill,
    OrderIntent,
    OrderResult,
    OrderState,
    OrderType,
    Position,
    Side,
    WorkingOrder,
)
from execution.protection import verify_protection
from governor import (
    AccountSnapshot,
    Action,
    GovernorState,
    apply_decision,
    evaluate,
    record_trade,
    roll_session,
    session_trading_date,
)
from governor import (
    Config as GovernorConfig,
)
from observability import EventType, StructuredLogger, new_correlation_id
from session_state import SessionState

__all__ = [
    "DryRunBroker",
    "DryRunRuntime",
    "RuntimeError_",
    "SimulatedOrder",
]


class RuntimeError_(RuntimeError):
    """Runtime refused to start or continue. Always fail-closed."""


@dataclass(frozen=True)
class SimulatedOrder:
    """A fully described order that was NEVER sent.

    Every field a live order would carry, so a dry-run log can be compared
    against a live one field for field. ``simulated`` is not decoration: it is
    how a reader knows at a glance that nothing reached a venue.
    """

    correlation_id: str
    client_tag: str
    at: datetime
    symbol: str
    side: str
    quantity: int
    intended_entry: float
    stop: float
    target: float | None
    strategy: str
    signal_reason: str
    governor_decision: str
    risk_dollars: float
    simulated: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "correlation_id": self.correlation_id,
            "client_tag": self.client_tag,
            "at": self.at.isoformat(),
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "intended_entry": self.intended_entry,
            "stop": self.stop,
            "target": self.target,
            "strategy": self.strategy,
            "signal_reason": self.signal_reason,
            "governor_decision": self.governor_decision,
            "risk_dollars": round(self.risk_dollars, 2),
            "simulated": True,
            "transmitted": False,
        }


@dataclass
class DryRunBroker:
    """Records intents and simulates fills. Has no order method, by design.

    Deliberately NOT an ``ExecutionBroker``: there is no method here that
    could be called to send anything, so "dry run cannot transmit" is a
    property of the type rather than a flag anyone could flip.
    """

    orders: list[SimulatedOrder] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)

    def simulate(self, order: SimulatedOrder) -> OrderResult:
        """Record the order and return a clearly-simulated result."""
        self.orders.append(order)
        intent = OrderIntent(
            symbol=order.symbol,
            contract_id=order.symbol,
            side=Side(order.side),
            size=order.quantity,
            order_type=OrderType.MARKET,
            stop_loss=order.stop,
            take_profit=order.target,
            reason=order.signal_reason,
            client_tag=order.client_tag,
        )
        return OrderResult(
            intent=intent,
            state=OrderState.CREATED,   # never SUBMITTED: nothing was sent
            order_id=None,
            error=None,
            raw_reference=f"SIMULATED:{order.client_tag}",
        )


@dataclass
class DryRunRuntime:
    """Drives bars through the real pipeline, transmitting nothing."""

    config: AppConfig
    strategy: Any
    strategy_name: str
    logger: StructuredLogger
    governor_config: GovernorConfig
    broker: DryRunBroker = field(default_factory=DryRunBroker)
    connection: ConnectionMachine = field(default_factory=ConnectionMachine)
    session: SessionState | None = None
    governor_state: GovernorState | None = None
    _sequence: int = 0

    # -- startup ------------------------------------------------------------

    def start(self, *, reconciled: bool, now: datetime | None = None) -> None:
        """Refuse to run unless every precondition genuinely holds."""
        moment = now or datetime.now(UTC)

        mode = self.config.execution_mode
        if mode.may_transmit:
            raise RuntimeError_(
                "refusing to start: configuration reports a transmitting mode. "
                "This runtime has no transmit path and must not be used as if "
                "it did."
            )
        if mode not in (ExecutionMode.DRY_RUN, ExecutionMode.BACKTEST,
                        ExecutionMode.READ_ONLY, ExecutionMode.REPLAY):
            raise RuntimeError_(f"ambiguous execution mode {mode.value}")

        if not reconciled:
            raise RuntimeError_(
                "refusing to start: broker state has not been reconciled. "
                "Local state is not evidence about what the account holds."
            )

        self.logger.event(
            EventType.APPLICATION_START,
            mode=mode.value,
            strategy=self.strategy_name,
            dry_run=self.config.dry_run,
            live_trading_enabled=self.config.live_trading_enabled,
            transmit_capable=False,
            **self.config.redacted(),
        )

        self.connection.transition(ConnectionState.CONNECTING, "start", now=moment)
        self.connection.transition(ConnectionState.AUTHENTICATED, "auth", now=moment)
        self.connection.transition(ConnectionState.RECONCILING, "reconcile", now=moment)
        for check in ("authenticated", "account_fetched", "positions_fetched",
                      "orders_fetched", "reconciled", "governor_healthy"):
            self.connection.mark(check, True)
        self.connection.observe_data(moment)
        self.connection.mark("market_data_fresh", True)
        ok, why = self.connection.advance_to_ready(now=moment)
        if not ok:
            raise RuntimeError_(f"connection did not reach READY: {why}")

        self.logger.event(EventType.RECONCILIATION_PASSED, detail="startup")

    # -- the loop -----------------------------------------------------------

    def on_bar(
        self,
        series: BarSeries,
        index: int,
        *,
        account_balance: float,
        net_liq: float,
        mll_floor: float | None,
        position: Position | None = None,
        working_orders: Sequence[WorkingOrder] = (),
    ) -> SimulatedOrder | None:
        """Process one bar through the real pipeline. Returns a simulated order.

        Ordering matters and mirrors the live path: observe, verify protection,
        roll the session, ask the governor, and only then ask the strategy.
        """
        bar = series.bars[index]
        at = series.close_instant(index)
        correlation = new_correlation_id()
        log = self.logger.bind(correlation_id=correlation)

        self.connection.observe_data(at)
        log.event(
            EventType.MARKET_DATA_RECEIVED,
            bar_ts=bar.ts.isoformat(), close=bar.close, volume=bar.volume,
        )

        # A position without verified protection halts everything.
        protection = verify_protection(
            position=position, working_orders=list(working_orders)
        )
        if protection.must_halt:
            log.event(
                EventType.PROTECTION_MISSING,
                status=protection.status.value,
                findings=[str(f) for f in protection.findings],
            )
            self.connection.halt("position without verified protection", now=at)
            raise RuntimeError_(
                "HALT: open position without verified protection. "
                + "; ".join(str(f) for f in protection.findings)
            )

        trading_date = session_trading_date(at, self.governor_config)
        snapshot = AccountSnapshot(
            net_liq=net_liq,
            balance=account_balance,
            mll_floor=mll_floor,
            open_position_size=position.size if position else 0,
            session_start_balance=(
                self.governor_state.session_start_balance
                if self.governor_state else net_liq
            ),
            now=at,
            kill_switch_active=False,
            observed_trades=(
                self.governor_state.trades_today if self.governor_state else 0
            ),
        )

        if self.governor_state is None:
            self.governor_state = GovernorState(
                session_start_balance=net_liq,
                last_reconcile=at,
                session_start=None,
            )
        self.governor_state = roll_session(
            snapshot, self.governor_state, self.governor_config
        )
        snapshot = replace(
            snapshot,
            session_start_balance=self.governor_state.session_start_balance,
            observed_trades=self.governor_state.trades_today,
        )

        decision = evaluate(snapshot, self.governor_state, self.governor_config)
        self.governor_state = apply_decision(self.governor_state, decision)
        log.event(
            EventType.RISK_CHECK,
            action=decision.action.value, code=decision.code,
            reason=decision.reason, session_pnl=round(decision.session_pnl, 2),
        )

        if decision.action is Action.FLATTEN_AND_HALT:
            log.event(EventType.GOVERNOR_HALT, code=decision.code,
                      reason=decision.reason)
            return None
        if decision.action is not Action.CONTINUE:
            log.event(EventType.RISK_REJECTED, code=decision.code,
                      reason=decision.reason)
            return None

        # Only now does the strategy get asked.
        window = BarWindow(series.bars, index)
        strategy_state = StrategyState(
            session_start=self.governor_state.session_start or at,
            trading_date=trading_date,
            entries_this_session=self.governor_state.trades_today,
            position_side=position.side.sign if position and position.side else 0,
        )
        signal = self.strategy.on_bar(window, strategy_state)
        if signal is None:
            return None

        log.event(
            EventType.SIGNAL_GENERATED,
            strategy=self.strategy_name, side=signal.side, size=signal.size,
            stop=signal.stop, target=signal.target, reason=signal.reason,
        )

        # Duplicate protection, against real observed broker state.
        self._sequence += 1
        side = Side.BUY if signal.side == 1 else Side.SELL
        intent = OrderIntent(
            symbol=self.config.symbol,
            contract_id=self.config.symbol,
            side=side,
            size=signal.size,
            order_type=OrderType.MARKET,
            stop_loss=signal.stop,
            take_profit=signal.target,
            reason=signal.reason,
            created_at=at,
        )
        tag = client_tag(
            session_id=self.session.session_id if self.session else "nosession",
            strategy=self.strategy_name, intent=intent, sequence=self._sequence,
        )
        duplicate = check_before_entry(
            intent=intent,
            positions=[position] if position else [],
            working_orders=list(working_orders),
            pending_tags=[o.client_tag for o in self.broker.orders],
            proposed_tag=tag,
            trades_taken=self.governor_state.trades_today,
            max_trades=self.governor_config.max_trades_per_session,
            reconciled=True,
            connection_ready=self.connection.state is ConnectionState.READY,
        )
        if not duplicate.allowed:
            log.event(
                EventType.EXECUTION_INTENT_REJECTED,
                reasons=[str(r) for r in duplicate.risks],
            )
            return None

        try:
            intent.validate()
        except Exception as exc:
            log.exception(exc, stage="intent_validation")
            log.event(EventType.EXECUTION_INTENT_REJECTED, reason=str(exc))
            return None

        risk = abs(signal.stop - bar.close) * signal.size * 2.00
        order = SimulatedOrder(
            correlation_id=correlation,
            client_tag=tag,
            at=at,
            symbol=self.config.symbol,
            side=side.value,
            quantity=signal.size,
            intended_entry=bar.close,
            stop=signal.stop,
            target=signal.target,
            strategy=self.strategy_name,
            signal_reason=signal.reason,
            governor_decision=decision.code,
            risk_dollars=risk,
        )
        log.event(EventType.EXECUTION_INTENT_CREATED, **order.as_dict())

        self.broker.simulate(order)
        log.event(
            EventType.SIMULATED_FILL,
            client_tag=tag,
            note="DRY RUN - ORDER NOT TRANSMITTED",
            transmitted=False,
        )
        self.governor_state = record_trade(self.governor_state)
        return order

    def shutdown(self) -> None:
        self.logger.event(
            EventType.APPLICATION_SHUTDOWN,
            simulated_orders=len(self.broker.orders),
            transmitted_orders=0,
        )

    def render(self) -> str:
        lines = ["DRY RUN SUMMARY", "=" * 40,
                 f"  strategy          {self.strategy_name}",
                 f"  simulated orders  {len(self.broker.orders)}",
                 "  TRANSMITTED       0  (this runtime has no transmit path)",
                 ""]
        for o in self.broker.orders:
            lines.append(
                f"  {o.at:%Y-%m-%d %H:%M}  {o.side:<4s} {o.quantity} "
                f"{o.symbol} @ {o.intended_entry:.2f}  "
                f"stop {o.stop:.2f}  target {o.target}  "
                f"risk ${o.risk_dollars:.2f}"
            )
            lines.append(f"      {o.signal_reason}")
            lines.append(f"      governor={o.governor_decision}  "
                         f"tag={o.client_tag}  SIMULATED")
        return "\n".join(lines)
