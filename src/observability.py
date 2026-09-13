"""Structured JSON logging with correlation IDs. Never logs a secret.

WHY JSON AND WHY CORRELATION IDS
--------------------------------
When something goes wrong at 09:41 on a live account, the question is never
"what happened" in general -- it is "what happened to THAT signal". A
correlation id threads one decision through every stage:

    signal_generated -> risk_check -> execution_intent_created
        -> broker_request -> order_submitted -> order_state_changed

so the whole life of a decision is one grep, and a decision that stopped
somewhere is visibly missing its later events rather than silently absent.

CREDENTIAL SAFETY
-----------------
Redaction happens at WRITE time, not at call sites. Relying on every caller to
remember is how a key ends up in a log: it only takes one. Any field whose name
looks credential-ish is replaced with ``<redacted>`` before serialisation,
recursively, whatever the caller passed.
"""

from __future__ import annotations

import json
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, TextIO

__all__ = [
    "SENSITIVE_KEYS",
    "EventType",
    "StructuredLogger",
    "new_correlation_id",
    "redact",
]


class EventType(str, Enum):
    """Every event the runtime may emit."""

    APPLICATION_START = "application_start"
    APPLICATION_SHUTDOWN = "application_shutdown"
    SESSION_START = "session_start"
    SESSION_END = "session_end"

    MARKET_DATA_RECEIVED = "market_data_received"
    MARKET_DATA_STALE = "market_data_stale"

    SIGNAL_GENERATED = "signal_generated"
    SIGNAL_REJECTED = "signal_rejected"

    RISK_CHECK = "risk_check"
    RISK_REJECTED = "risk_rejected"
    GOVERNOR_HALT = "governor_halt"

    EXECUTION_INTENT_CREATED = "execution_intent_created"
    EXECUTION_INTENT_REJECTED = "execution_intent_rejected"

    BROKER_REQUEST = "broker_request"
    BROKER_RESPONSE = "broker_response"

    ORDER_SUBMITTED = "order_submitted"
    ORDER_REJECTED = "order_rejected"
    ORDER_STATE_CHANGED = "order_state_changed"
    ORDER_UNKNOWN = "order_unknown"

    POSITION_DETECTED = "position_detected"
    PROTECTION_VERIFIED = "protection_verified"
    PROTECTION_MISSING = "protection_missing"

    RECONCILIATION_STARTED = "reconciliation_started"
    RECONCILIATION_PASSED = "reconciliation_passed"
    RECONCILIATION_FAILED = "reconciliation_failed"

    CONNECTION_LOST = "connection_lost"
    CONNECTION_RESTORED = "connection_restored"

    KILL_SWITCH = "kill_switch"
    SIMULATED_FILL = "simulated_fill"
    EXCEPTION = "exception"


SENSITIVE_KEYS: frozenset[str] = frozenset({
    "api_key", "apikey", "api-key", "project_x_api_key",
    "password", "passwd", "secret", "token", "session_token",
    "authorization", "auth", "bearer", "credential", "credentials",
    "access_token", "refresh_token", "private_key", "signature",
})


def new_correlation_id() -> str:
    return uuid.uuid4().hex[:12]


def redact(payload: Any) -> Any:
    """Strip anything credential-shaped, recursively.

    Matches on substring rather than exact key, so ``PROJECT_X_API_KEY``,
    ``apiKey`` and ``auth_header`` are all caught. Over-redacting a harmless
    field is a cosmetic problem; under-redacting one is a disclosed key.
    """
    if isinstance(payload, dict):
        clean: dict[str, Any] = {}
        for key, value in payload.items():
            lowered = str(key).lower().replace("-", "_")
            if any(marker in lowered for marker in SENSITIVE_KEYS):
                clean[key] = "<redacted>"
            else:
                clean[key] = redact(value)
        return clean
    if isinstance(payload, (list, tuple)):
        return [redact(v) for v in payload]
    return payload


@dataclass
class StructuredLogger:
    """One JSON object per line. Safe to ship anywhere.

    ``stream`` defaults to stdout; ``path`` additionally appends to a file.
    Both are optional so tests can capture without touching the filesystem.
    """

    component: str = "runtime"
    stream: TextIO | None = None
    path: Path | None = None
    correlation_id: str | None = None
    session_id: str | None = None
    _emitted: list[dict[str, Any]] = field(default_factory=list, repr=False)

    def bind(self, **context: Any) -> StructuredLogger:
        """A child logger carrying extra context, e.g. one signal's id."""
        child = StructuredLogger(
            component=self.component,
            stream=self.stream,
            path=self.path,
            correlation_id=context.get("correlation_id", self.correlation_id),
            session_id=context.get("session_id", self.session_id),
        )
        child._emitted = self._emitted
        return child

    def event(self, kind: EventType, **fields: Any) -> dict[str, Any]:
        record: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "event": kind.value,
            "component": self.component,
        }
        if self.session_id:
            record["session_id"] = self.session_id
        if self.correlation_id:
            record["correlation_id"] = self.correlation_id
        record.update(redact(fields))

        line = json.dumps(record, default=str, sort_keys=False)
        target = self.stream if self.stream is not None else sys.stdout
        print(line, file=target)
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        self._emitted.append(record)
        return record

    # -- convenience wrappers used by the runtime --------------------------

    def exception(self, exc: BaseException, **fields: Any) -> dict[str, Any]:
        """Log a failure WITHOUT its traceback body.

        A traceback can contain request headers and argument values, which is
        precisely where a bearer token ends up. The type and message are enough
        to act on; the rest belongs in a debugger, not a log file.
        """
        return self.event(
            EventType.EXCEPTION,
            exception_type=type(exc).__name__,
            message=str(exc)[:500],
            **fields,
        )

    @property
    def emitted(self) -> tuple[dict[str, Any], ...]:
        """Everything logged, for assertions in tests."""
        return tuple(self._emitted)

    def events_of(self, kind: EventType) -> tuple[dict[str, Any], ...]:
        return tuple(r for r in self._emitted if r["event"] == kind.value)

    def trace(self, correlation_id: str) -> tuple[dict[str, Any], ...]:
        """Every event belonging to one decision, in order."""
        return tuple(
            r for r in self._emitted if r.get("correlation_id") == correlation_id
        )
