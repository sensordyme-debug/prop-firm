"""Broker interface and the execution capability boundary.

TWO INTERFACES, NOT ONE
-----------------------
:class:`MarketDataBroker` reads. :class:`ExecutionBroker` extends it with the
ability to transmit. They are separate types so that "this component cannot
place an order" is a fact about its type, not a promise in a docstring or a
flag checked at the last moment.

The read-only adapter implements only the first. There is no runtime path that
upgrades it.

WHY A CAPABILITY OBJECT AND NOT A FLAG
--------------------------------------
A boolean can be flipped by a typo, a bad merge, a careless agent, or an
``.env`` copied from somewhere else. :class:`ExecutionCapability` cannot be
constructed by accident: it requires several independent facts to be asserted
together, and its constructor refuses unless all of them hold.

More importantly, **nothing in the current runtime constructs one**, and
``grant_execution`` raises unconditionally. Enabling execution is therefore a
deliberate code change reviewed by a human, not a configuration change. That is
the intended shape: STEP 34 of the brief asks for resistance to accidental
activation, and configuration is not resistant.

The layering, top to bottom:

    strategy            proposes a Signal; knows nothing about any broker
    governor            approves or refuses; final authority
    OrderIntent         what we want, in prices
    ExecutionBroker     the interface (transmission requires a capability)
    ProjectX adapter    the only module that speaks the vendor's dialect
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from execution.models import (
    AccountState,
    BrokerError,
    MarketBar,
    OrderIntent,
    OrderResult,
    Position,
    WorkingOrder,
)

__all__ = [
    "ContractSpec",
    "ExecutionBroker",
    "ExecutionCapability",
    "ExecutionNotEnabled",
    "MarketDataBroker",
    "grant_execution",
]


class ExecutionNotEnabled(BrokerError):
    """Raised whenever transmission is attempted. Currently: always.

    Not a bug and not a placeholder to be removed when convenient. It is the
    boundary that keeps a research and dry-run system from becoming a live
    trading system by accident.
    """


@dataclass(frozen=True)
class ContractSpec:
    """Instrument geometry, vendor-neutral."""

    contract_id: str
    symbol: str
    tick_size: float
    tick_value: float

    @property
    def point_value(self) -> float:
        """Dollars per full point.

        The SDK's ``Position.unrealized_pnl`` defaults its multiplier to 1.0,
        which is wrong for MNQ and reports exactly half of every move. Nothing
        should ever use that default; this is the value to pass.
        """
        if self.tick_size <= 0:
            raise BrokerError(
                f"{self.symbol}: tick_size {self.tick_size} is unusable; "
                "cannot value P&L"
            )
        return self.tick_value / self.tick_size

    def ticks_between(self, a: float, b: float) -> int:
        """Distance in ticks, rounded so the result is never optimistic.

        ProjectX brackets are expressed in ticks, not prices. Rounding a stop
        distance DOWN would place the stop closer than intended; rounding up
        costs at most one tick and never produces a tighter stop than asked
        for. Erring the safe way is worth a tick.
        """
        import math

        return max(1, math.ceil(abs(a - b) / self.tick_size - 1e-9))


@runtime_checkable
class MarketDataBroker(Protocol):
    """Everything a read-only connection can do.

    Deliberately has no order method at all. A component typed against this
    protocol cannot transmit, and that is checkable rather than asserted.
    """

    async def authenticate(self) -> None: ...

    async def get_account(self) -> AccountState: ...

    async def get_positions(self) -> list[Position]: ...

    async def get_working_orders(self) -> list[WorkingOrder]: ...

    async def get_contract(self, symbol: str) -> ContractSpec: ...

    async def get_bars(
        self, symbol: str, *, days: int = 5, interval_minutes: int = 5
    ) -> list[MarketBar]: ...

    async def is_connected(self) -> bool: ...


@dataclass(frozen=True)
class ExecutionCapability:
    """Proof that every precondition for transmitting an order is satisfied.

    Constructing one requires asserting each condition independently. The point
    is not that these checks are hard to satisfy -- it is that they must be
    satisfied *explicitly and together*, so no single mistake is sufficient.
    """

    credentials_valid: bool
    account_identified: bool
    reconciliation_passed: bool
    market_data_fresh: bool
    governor_healthy: bool
    explicitly_enabled_by_human: bool
    granted_at: datetime

    def __post_init__(self) -> None:
        missing = [
            name
            for name, ok in (
                ("credentials_valid", self.credentials_valid),
                ("account_identified", self.account_identified),
                ("reconciliation_passed", self.reconciliation_passed),
                ("market_data_fresh", self.market_data_fresh),
                ("governor_healthy", self.governor_healthy),
                ("explicitly_enabled_by_human", self.explicitly_enabled_by_human),
            )
            if not ok
        ]
        if missing:
            raise ExecutionNotEnabled(
                "execution capability refused; unmet preconditions: "
                + ", ".join(missing)
            )


def grant_execution(*_args: object, **_kwargs: object) -> ExecutionCapability:
    """The only sanctioned way to obtain a capability. Currently always refuses.

    Order transmission is NOT IMPLEMENTED in this repository. This function
    exists so the shape of the eventual change is obvious and reviewable: when
    execution is genuinely wanted, it is enabled here, deliberately, after the
    ROADMAP Stage 5 gates have actually been demonstrated -- a clean dry-run
    session, a survived reboot, a working kill switch, and a network-loss test.

    Do not make this return a capability to "test the path". Test the path with
    a fake in the test suite, where it cannot reach a real account.
    """
    raise ExecutionNotEnabled(
        "Order transmission is not implemented and cannot be enabled at "
        "runtime. It requires a reviewed code change here, after ROADMAP "
        "Stage 5 is demonstrated. See docs/OPERATIONS.md."
    )


@runtime_checkable
class ExecutionBroker(MarketDataBroker, Protocol):
    """A broker that can also transmit. No adapter implements this today.

    Every method takes an :class:`ExecutionCapability`, so an order cannot be
    sent by a caller that never proved it was allowed to. The capability is not
    a decoration: implementations are expected to require it.
    """

    async def place_order(
        self, intent: OrderIntent, capability: ExecutionCapability
    ) -> OrderResult: ...

    async def place_bracket_order(
        self, intent: OrderIntent, capability: ExecutionCapability
    ) -> OrderResult: ...

    async def cancel_order(
        self, order_id: str, capability: ExecutionCapability
    ) -> OrderResult: ...

    async def flatten(
        self, contract_id: str, capability: ExecutionCapability
    ) -> OrderResult: ...
