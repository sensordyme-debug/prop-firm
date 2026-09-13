"""Broker-neutral domain models.

Nothing above the adapter should ever see a ProjectX type. That is not tidiness
for its own sake: the moment a vendor object leaks into the strategy or the
governor, swapping or mocking the broker means touching decision code, and the
decision code is the part that must stay provable offline.

Everything here is a frozen dataclass. State transitions produce new objects, so
an execution record cannot be mutated after the fact by something holding a
stale reference.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import Any

__all__ = [
    "TERMINAL_STATES",
    "AccountState",
    "AuthenticationError",
    "BrokerError",
    "BrokerUnavailableError",
    "ExecutionEvent",
    "Fill",
    "MarketBar",
    "OrderIntent",
    "OrderResult",
    "OrderState",
    "OrderStateError",
    "OrderType",
    "Position",
    "RateLimitError",
    "Side",
    "WorkingOrder",
]


class Side(str, Enum):
    """Direction. Mapped to the vendor's integer encoding in the adapter only."""

    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1

    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    TRAILING_STOP = "TRAILING_STOP"


class OrderState(str, Enum):
    """Lifecycle of an order.

    ``UNKNOWN`` is the important one. A client timeout on submission does NOT
    mean the order failed to reach the exchange. Treating it as a failure and
    resubmitting is how a duplicate position appears, and on a $2,000 trailing
    drawdown a duplicate is not recoverable. UNKNOWN is resolved by asking the
    broker what happened -- never by retrying.
    """

    CREATED = "CREATED"
    VALIDATED = "VALIDATED"
    SUBMITTED = "SUBMITTED"
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_STATES

    @property
    def is_live(self) -> bool:
        """Could this order still fill? UNKNOWN counts, because it might."""
        return self in (
            OrderState.SUBMITTED,
            OrderState.ACCEPTED,
            OrderState.PARTIALLY_FILLED,
            OrderState.CANCEL_REQUESTED,
            OrderState.UNKNOWN,
        )

    @property
    def requires_reconciliation(self) -> bool:
        return self is OrderState.UNKNOWN


TERMINAL_STATES = frozenset(
    {OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED}
)


# ---------------------------------------------------------------------------
# Broker errors
# ---------------------------------------------------------------------------


class BrokerError(RuntimeError):
    """Base for anything the broker layer reports. Never silently swallowed."""


class AuthenticationError(BrokerError):
    """Credentials rejected, or the session token expired.

    ProjectX returns HTTP 200 with ``success: false`` for a bad login, so an
    adapter that checks only the status code will treat a rejection as a
    success. This exception exists to make that impossible to miss.
    """


class RateLimitError(BrokerError):
    """HTTP 429. Back off; never retry hot."""


class BrokerUnavailableError(BrokerError):
    """Timeout, network failure, or an unparseable response.

    Distinct from a rejection on purpose: we do not know what happened, and
    "do not know" must not collapse into "did not happen".
    """


class OrderStateError(BrokerError):
    """An illegal order state transition was attempted."""


# ---------------------------------------------------------------------------
# Account and market state
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AccountState:
    """A broker-neutral account snapshot.

    ``net_liquidation`` is optional because ProjectX does not report it: it has
    to be derived as balance plus unrealised P&L, and if the unrealised part
    cannot be computed honestly it must stay ``None`` rather than fall back to
    balance. ``None`` means unknown, and unknown stops trading.
    """

    account_id: int
    name: str
    balance: float
    can_trade: bool
    is_simulated: bool
    net_liquidation: float | None = None
    observed_at: datetime | None = None

    @property
    def net_liq_known(self) -> bool:
        return self.net_liquidation is not None

    def redacted(self) -> dict[str, Any]:
        """Account NAME is not logged; the id is enough to correlate."""
        return {
            "account_id": self.account_id,
            "balance": round(self.balance, 2),
            "net_liquidation": (
                None if self.net_liquidation is None
                else round(self.net_liquidation, 2)
            ),
            "can_trade": self.can_trade,
            "simulated": self.is_simulated,
        }


@dataclass(frozen=True)
class Position:
    contract_id: str
    size: int          # signed: negative is short
    average_price: float
    opened_at: datetime | None = None

    @property
    def is_flat(self) -> bool:
        return self.size == 0

    @property
    def side(self) -> Side | None:
        if self.size == 0:
            return None
        return Side.BUY if self.size > 0 else Side.SELL


@dataclass(frozen=True)
class WorkingOrder:
    order_id: str
    contract_id: str
    side: Side
    order_type: OrderType
    size: int
    state: OrderState
    limit_price: float | None = None
    stop_price: float | None = None
    filled_size: int = 0
    custom_tag: str | None = None

    @property
    def remaining(self) -> int:
        return max(0, self.size - self.filled_size)


@dataclass(frozen=True)
class MarketBar:
    """A normalised bar. See :mod:`marketdata` for the conversion."""

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    symbol: str
    contract_id: str | None = None


# ---------------------------------------------------------------------------
# Order intent and results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrderIntent:
    """What the system WANTS to do, after the governor has approved it.

    Deliberately not an order. It carries absolute PRICES, because that is how
    the strategy reasons; ProjectX wants bracket offsets in ticks, and that
    conversion belongs in the adapter where the tick size is known.

    An intent with no stop is rejected at validation. CLAUDE.md constraint 1
    says a naked position must never exist, and the cheapest place to enforce
    that is before anything is transmitted.
    """

    symbol: str
    contract_id: str
    side: Side
    size: int
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    reason: str = ""
    created_at: datetime | None = None
    client_tag: str | None = None

    def validate(self) -> None:
        """Reject anything that could produce an unsafe order. Fails closed."""
        if self.size <= 0:
            raise OrderStateError(f"size must be positive, got {self.size}")
        if not self.contract_id:
            raise OrderStateError("contract_id is required; refusing to guess it")

        if self.stop_loss is None:
            raise OrderStateError(
                "no stop_loss: an entry without protection is exactly the naked "
                "position CLAUDE.md constraint 1 forbids"
            )
        if self.order_type is OrderType.LIMIT and self.limit_price is None:
            raise OrderStateError("LIMIT order without a limit price")

        # Direction sanity. A stop on the wrong side of entry is not a stop.
        if self.limit_price is not None:
            reference = self.limit_price
            if self.side is Side.BUY and self.stop_loss >= reference:
                raise OrderStateError(
                    f"long stop {self.stop_loss} is at or above entry {reference}"
                )
            if self.side is Side.SELL and self.stop_loss <= reference:
                raise OrderStateError(
                    f"short stop {self.stop_loss} is at or below entry {reference}"
                )

        if self.take_profit is not None and self.stop_loss is not None:
            if self.side is Side.BUY and self.take_profit <= self.stop_loss:
                raise OrderStateError(
                    f"long target {self.take_profit} is not above stop {self.stop_loss}"
                )
            if self.side is Side.SELL and self.take_profit >= self.stop_loss:
                raise OrderStateError(
                    f"short target {self.take_profit} is not below stop {self.stop_loss}"
                )

    def redacted(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "contract_id": self.contract_id,
            "side": self.side.value,
            "size": self.size,
            "type": self.order_type.value,
            "limit": self.limit_price,
            "stop": self.stop_loss,
            "target": self.take_profit,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Fill:
    order_id: str
    contract_id: str
    side: Side
    size: int
    price: float
    filled_at: datetime
    commission: float = 0.0


@dataclass(frozen=True)
class OrderResult:
    """The outcome of attempting to place an order.

    ``state`` may legitimately be UNKNOWN. A result is not the same as a
    confirmation, and code downstream must not treat it as one.
    """

    intent: OrderIntent
    state: OrderState
    order_id: str | None = None
    fills: tuple[Fill, ...] = ()
    error: str | None = None
    raw_reference: str | None = None  # a correlation id, never a payload

    @property
    def filled_size(self) -> int:
        return sum(f.size for f in self.fills)

    @property
    def needs_reconciliation(self) -> bool:
        return self.state.requires_reconciliation

    def redacted(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "order_id": self.order_id,
            "filled": self.filled_size,
            "error": self.error,
            "intent": self.intent.redacted(),
        }


@dataclass(frozen=True)
class ExecutionEvent:
    """One line in the decision audit trail. Always safe to log."""

    at: datetime
    kind: str
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"at": self.at.isoformat(), "event": self.kind, **self.detail}


def transition(order: WorkingOrder, to: OrderState) -> WorkingOrder:
    """Move an order to a new state, rejecting illegal transitions.

    Guards the specific mistake that costs money: treating UNKNOWN as though it
    resolved itself. UNKNOWN can only be left by observing the broker, which is
    modelled as an explicit transition to a state the broker reported.
    """
    if not _LEGAL_TRANSITIONS.get(order.state, frozenset()) >= {to}:
        raise OrderStateError(
            f"illegal transition {order.state.value} -> {to.value} for order "
            f"{order.order_id}. Terminal and unknown states are not escapable "
            "by assumption."
        )
    return replace(order, state=to)


_LEGAL_TRANSITIONS: dict[OrderState, frozenset[OrderState]] = {
    OrderState.CREATED: frozenset({OrderState.VALIDATED, OrderState.REJECTED}),
    OrderState.VALIDATED: frozenset(
        {OrderState.SUBMITTED, OrderState.REJECTED, OrderState.UNKNOWN}
    ),
    OrderState.SUBMITTED: frozenset(
        {
            OrderState.ACCEPTED,
            OrderState.REJECTED,
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.UNKNOWN,
        }
    ),
    OrderState.ACCEPTED: frozenset(
        {
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCEL_REQUESTED,
            OrderState.CANCELLED,
            OrderState.REJECTED,
            OrderState.UNKNOWN,
        }
    ),
    OrderState.PARTIALLY_FILLED: frozenset(
        {
            OrderState.FILLED,
            OrderState.CANCEL_REQUESTED,
            OrderState.CANCELLED,
            OrderState.UNKNOWN,
        }
    ),
    OrderState.CANCEL_REQUESTED: frozenset(
        {OrderState.CANCELLED, OrderState.FILLED, OrderState.UNKNOWN}
    ),
    # UNKNOWN is escapable ONLY to a state the broker actually reported.
    OrderState.UNKNOWN: frozenset(
        {
            OrderState.ACCEPTED,
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.REJECTED,
        }
    ),
    OrderState.FILLED: frozenset(),
    OrderState.CANCELLED: frozenset(),
    OrderState.REJECTED: frozenset(),
}
