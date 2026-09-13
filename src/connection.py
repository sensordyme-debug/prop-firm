"""Connection state machine. Nothing may act while the broker state is unknown.

THE RULE
--------
Order submission is legal from exactly one state: READY. Reaching READY
requires having just completed the full sequence -- authenticate, fetch
account, fetch positions, fetch working orders, reconcile, verify data
freshness, verify the governor. Every other state, including the ones that
feel harmless, refuses.

WHY A STATE MACHINE AND NOT A BOOLEAN
-------------------------------------
``if connected:`` is true immediately after a socket opens and long before the
system knows what it owns. The dangerous window is exactly there: connected,
authenticated, and completely ignorant of the position it may be holding. A
state machine makes that window a named state (``AUTHENTICATED``) from which
trading is illegal, rather than an unnamed moment where a boolean says yes.

AFTER A RECONNECT
-----------------
Reconnection does not restore knowledge, only connectivity. The market moved
while we were away, and the broker may have filled, stopped or liquidated
something. So a reconnect returns to ``CONNECTING`` and walks the whole
sequence again -- there is no fast path back to READY.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum

__all__ = [
    "READY_SEQUENCE",
    "ConnectionEvent",
    "ConnectionMachine",
    "ConnectionState",
    "IllegalTransition",
]


class IllegalTransition(RuntimeError):
    """An attempted state change that the machine does not permit."""


class ConnectionState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    AUTHENTICATED = "AUTHENTICATED"   # connected, but we own no knowledge yet
    RECONCILING = "RECONCILING"
    READY = "READY"                   # the ONLY state that may transmit
    DEGRADED = "DEGRADED"             # connected but data is stale or partial
    HALTED = "HALTED"                 # a human must look at this

    @property
    def may_transmit(self) -> bool:
        return self is ConnectionState.READY

    @property
    def may_read(self) -> bool:
        return self in (
            ConnectionState.AUTHENTICATED,
            ConnectionState.RECONCILING,
            ConnectionState.READY,
            ConnectionState.DEGRADED,
        )

    @property
    def is_terminal(self) -> bool:
        return self is ConnectionState.HALTED


# The full path to READY. No step may be skipped; see advance().
READY_SEQUENCE: tuple[str, ...] = (
    "authenticated",
    "account_fetched",
    "positions_fetched",
    "orders_fetched",
    "reconciled",
    "market_data_fresh",
    "governor_healthy",
)


_LEGAL: dict[ConnectionState, frozenset[ConnectionState]] = {
    ConnectionState.DISCONNECTED: frozenset(
        {ConnectionState.CONNECTING, ConnectionState.HALTED}),
    ConnectionState.CONNECTING: frozenset(
        {ConnectionState.AUTHENTICATED, ConnectionState.DISCONNECTED,
         ConnectionState.HALTED}),
    ConnectionState.AUTHENTICATED: frozenset(
        {ConnectionState.RECONCILING, ConnectionState.DISCONNECTED,
         ConnectionState.HALTED}),
    ConnectionState.RECONCILING: frozenset(
        {ConnectionState.READY, ConnectionState.DEGRADED,
         ConnectionState.DISCONNECTED, ConnectionState.HALTED}),
    ConnectionState.READY: frozenset(
        {ConnectionState.DEGRADED, ConnectionState.DISCONNECTED,
         ConnectionState.RECONCILING, ConnectionState.HALTED}),
    ConnectionState.DEGRADED: frozenset(
        {ConnectionState.RECONCILING, ConnectionState.DISCONNECTED,
         ConnectionState.HALTED}),
    # Terminal on purpose. Recovering from HALTED is a human decision.
    ConnectionState.HALTED: frozenset(),
}


@dataclass(frozen=True)
class ConnectionEvent:
    at: datetime
    frm: ConnectionState
    to: ConnectionState
    reason: str


@dataclass
class ConnectionMachine:
    """Tracks connection state and what has actually been verified.

    ``checks`` records which steps of :data:`READY_SEQUENCE` are satisfied.
    READY is unreachable until every one is, so "we reconnected and carried on"
    cannot happen by forgetting a step.
    """

    state: ConnectionState = ConnectionState.DISCONNECTED
    checks: dict[str, bool] = field(default_factory=dict)
    history: list[ConnectionEvent] = field(default_factory=list)
    last_data_at: datetime | None = None
    stale_after_seconds: int = 120

    # -- transitions --------------------------------------------------------

    def transition(
        self, to: ConnectionState, reason: str, *, now: datetime | None = None
    ) -> None:
        if to not in _LEGAL.get(self.state, frozenset()):
            raise IllegalTransition(
                f"{self.state.value} -> {to.value} is not permitted "
                f"({reason}). HALTED is terminal; READY is reachable only "
                "through RECONCILING."
            )
        event = ConnectionEvent(
            at=now or datetime.now(UTC),
            frm=self.state, to=to, reason=reason,
        )
        self.history.append(event)
        self.state = to
        if to in (ConnectionState.DISCONNECTED, ConnectionState.HALTED):
            # Knowledge does not survive a disconnect. Anything we verified
            # before the gap describes a world we can no longer see.
            self.checks.clear()

    def mark(self, check: str, ok: bool = True) -> None:
        if check not in READY_SEQUENCE:
            raise ValueError(
                f"unknown readiness check {check!r}; expected one of "
                f"{READY_SEQUENCE}"
            )
        self.checks[check] = ok

    def observe_data(self, at: datetime) -> None:
        self.last_data_at = at

    # -- queries ------------------------------------------------------------

    def missing_checks(self) -> tuple[str, ...]:
        return tuple(c for c in READY_SEQUENCE if not self.checks.get(c, False))

    def data_is_stale(self, now: datetime) -> bool:
        """No data is stale data. Absence is not freshness."""
        if self.last_data_at is None:
            return True
        return (now - self.last_data_at) > timedelta(seconds=self.stale_after_seconds)

    def may_transmit(self, now: datetime | None = None) -> tuple[bool, str]:
        """The single question the execution layer asks. Fails closed.

        Returns (allowed, reason). Every negative path is explicit -- there is
        no branch that falls through to True.
        """
        moment = now or datetime.now(UTC)
        if not self.state.may_transmit:
            return False, f"connection state is {self.state.value}, not READY"
        missing = self.missing_checks()
        if missing:
            return False, f"readiness checks not satisfied: {', '.join(missing)}"
        if self.data_is_stale(moment):
            age = (
                "never received"
                if self.last_data_at is None
                else f"{(moment - self.last_data_at).total_seconds():.0f}s old"
            )
            return False, f"market data is stale ({age})"
        return True, "READY, all checks satisfied, data fresh"

    def advance_to_ready(self, *, now: datetime | None = None) -> tuple[bool, str]:
        """Attempt RECONCILING -> READY. Refuses unless every check passed."""
        if self.state is not ConnectionState.RECONCILING:
            return False, f"must be RECONCILING to reach READY, not {self.state.value}"
        missing = self.missing_checks()
        if missing:
            self.transition(
                ConnectionState.DEGRADED,
                f"incomplete readiness: {', '.join(missing)}", now=now,
            )
            return False, f"missing: {', '.join(missing)}"
        self.transition(ConnectionState.READY, "all readiness checks passed", now=now)
        return True, "READY"

    def on_connection_lost(self, reason: str, *, now: datetime | None = None) -> None:
        """Drop to DISCONNECTED and forget everything we thought we knew."""
        if self.state is ConnectionState.HALTED:
            return
        self.transition(ConnectionState.DISCONNECTED, reason, now=now)

    def halt(self, reason: str, *, now: datetime | None = None) -> None:
        if self.state is ConnectionState.HALTED:
            return
        self.transition(ConnectionState.HALTED, reason, now=now)

    def render(self) -> str:
        lines = [f"CONNECTION: {self.state.value}"]
        for check in READY_SEQUENCE:
            mark = "ok " if self.checks.get(check) else "-- "
            lines.append(f"  [{mark}] {check}")
        allowed, why = self.may_transmit()
        lines.append(f"  transmit permitted: {allowed}  ({why})")
        return "\n".join(lines)
