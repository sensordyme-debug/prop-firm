"""Operational state that survives a restart. Never authoritative over the broker.

WHAT THIS IS FOR, AND WHAT IT IS NOT
------------------------------------
A restart loses everything the process knew: which session it was in, how many
trades it had taken, whether it was halted. Losing the trade count silently
re-grants the trade budget, which is a real way to take a third trade in a
two-trade session. So that much is persisted.

What is NOT persisted as truth is anything the broker owns -- positions, fills,
order status. Those are recorded only as "what we last believed", clearly
labelled, and every startup re-derives them from the broker.

    LOCAL STATE -> BROKER RECONCILIATION -> BROKER TRUTH WINS -> continue

:meth:`SessionState.trust_level` makes the distinction impossible to ignore:
loaded state is ``UNVERIFIED`` until reconciliation says otherwise, and the
runtime refuses to trade on unverified state.

FAIL CLOSED ON DAMAGE
---------------------
Missing, unparseable or stale state is not repaired and not defaulted. A
plausible-looking state file that is actually wrong is worse than none, because
it re-enables trading on a fiction.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, date, datetime
from enum import Enum
from pathlib import Path
from typing import Any

__all__ = [
    "STATE_FILENAME",
    "SessionState",
    "StateError",
    "TrustLevel",
    "load_state",
    "save_state",
]

STATE_FILENAME = "session_state.json"
STATE_VERSION = 1


class StateError(RuntimeError):
    """Persisted state is missing, damaged or inconsistent. Always fatal."""


class TrustLevel(str, Enum):
    FRESH = "FRESH"            # created this process, nothing loaded
    UNVERIFIED = "UNVERIFIED"  # loaded from disk, not yet reconciled
    RECONCILED = "RECONCILED"  # confirmed against the broker

    @property
    def may_trade(self) -> bool:
        """Only reconciled state may drive a trading decision."""
        return self is TrustLevel.RECONCILED


@dataclass(frozen=True)
class SessionState:
    """Everything worth surviving a restart, and nothing the broker owns."""

    session_id: str
    trading_date: date
    session_start_balance: float

    # Governor-scoped, and the reason this file exists at all.
    trades_today: int = 0
    halted: bool = False
    halt_reason: str | None = None
    kill_switch_seen: bool = False

    # What we LAST BELIEVED. Never trusted; re-derived on startup.
    last_known_position: int = 0
    last_known_order_ids: tuple[str, ...] = ()
    last_broker_sync: str | None = None

    # Strategy-scoped, so an opening range is not lost mid-session.
    strategy_name: str | None = None
    strategy_state: dict[str, Any] = field(default_factory=dict)

    saved_at: str | None = None
    trust: TrustLevel = TrustLevel.FRESH

    def mark_reconciled(self) -> SessionState:
        return replace(self, trust=TrustLevel.RECONCILED)

    @property
    def may_trade(self) -> bool:
        return self.trust.may_trade and not self.halted

    def to_json(self) -> str:
        payload = asdict(self)
        payload["trading_date"] = self.trading_date.isoformat()
        payload["trust"] = self.trust.value
        payload["last_known_order_ids"] = list(self.last_known_order_ids)
        payload["saved_at"] = datetime.now(UTC).isoformat()
        payload["_version"] = STATE_VERSION
        payload["_warning"] = (
            "NOT AUTHORITATIVE. Positions and orders here are what the process "
            "last believed. The broker is the source of truth; reconcile on "
            "startup before trading."
        )
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"

    def render(self) -> str:
        return "\n".join([
            "SESSION STATE",
            "-" * 13,
            f"  session      : {self.session_id}",
            f"  trading date : {self.trading_date}",
            f"  trust        : {self.trust.value}",
            f"  trades today : {self.trades_today}",
            f"  halted       : {self.halted}"
            + (f" ({self.halt_reason})" if self.halt_reason else ""),
            f"  believed pos : {self.last_known_position:+d}  "
            "(NOT authoritative)",
            f"  may trade    : {self.may_trade}",
        ])


def save_state(state: SessionState, root: Path | None = None) -> Path:
    target = (root or Path("state")) / STATE_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    # Write-then-replace: a crash mid-write must not leave a half file that
    # parses into a plausible but wrong state.
    temporary = target.with_suffix(".tmp")
    temporary.write_text(state.to_json(), encoding="utf-8")
    temporary.replace(target)
    return target


def load_state(
    root: Path | None = None,
    *,
    expected_trading_date: date | None = None,
) -> SessionState | None:
    """Load persisted state, or None if there is none.

    Raises :class:`StateError` on damage rather than returning a default. A
    default would silently reset the trade count, which re-grants a budget the
    session had already spent.
    """
    target = (root or Path("state")) / STATE_FILENAME
    if not target.is_file():
        return None

    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError(
            f"session state at {target} is unreadable: {exc}. Refusing to "
            "start with a default: that would reset the trade count and "
            "re-grant a budget this session may already have spent."
        ) from exc

    if not isinstance(payload, dict):
        raise StateError(f"session state at {target} is not a JSON object")

    for required in ("session_id", "trading_date", "session_start_balance"):
        if required not in payload:
            raise StateError(
                f"session state at {target} is missing {required!r}"
            )

    try:
        trading_date = date.fromisoformat(str(payload["trading_date"]))
    except ValueError as exc:
        raise StateError(f"trading_date is not an ISO date: {exc}") from exc

    if expected_trading_date is not None and trading_date != expected_trading_date:
        # A different day is not damage -- it is simply not this session's
        # state, and carrying its trade count forward would be wrong.
        return None

    return SessionState(
        session_id=str(payload["session_id"]),
        trading_date=trading_date,
        session_start_balance=float(payload["session_start_balance"]),
        trades_today=int(payload.get("trades_today", 0)),
        halted=bool(payload.get("halted", False)),
        halt_reason=payload.get("halt_reason"),
        kill_switch_seen=bool(payload.get("kill_switch_seen", False)),
        last_known_position=int(payload.get("last_known_position", 0)),
        last_known_order_ids=tuple(payload.get("last_known_order_ids", ())),
        last_broker_sync=payload.get("last_broker_sync"),
        strategy_name=payload.get("strategy_name"),
        strategy_state=dict(payload.get("strategy_state", {})),
        saved_at=payload.get("saved_at"),
        # The important line in this file.
        trust=TrustLevel.UNVERIFIED,
    )
