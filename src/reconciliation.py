"""Startup and post-reconnect reconciliation. Broker truth wins, always.

THE RULE
--------
The system must NEVER assume it is flat because the process started with an
empty state. A restart tells you nothing about the market; it only tells you
about this process. CLAUDE.md constraint 5 exists because the opposite
assumption is how a forgotten position becomes a blown account.

FAIL CLOSED, AND NEVER "FIX"
----------------------------
An unexpected position or working order halts the system. It does **not**
trigger a corrective order. Submitting an offsetting trade to tidy up a
position you did not expect is trading on a state you demonstrably do not
understand, and if your understanding is wrong the correction doubles the
exposure instead of removing it. A human looks at it.

The output is deliberately blunt: reconciliation either passes or it does not,
and nothing downstream may run without a pass.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from execution.models import Position, WorkingOrder

__all__ = [
    "Discrepancy",
    "ReconciliationResult",
    "ReconciliationStatus",
    "reconcile",
]


class ReconciliationStatus(str, Enum):
    PASSED = "PASSED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class Discrepancy:
    kind: str
    detail: str

    def __str__(self) -> str:
        return f"{self.kind}: {self.detail}"


@dataclass(frozen=True)
class ReconciliationResult:
    status: ReconciliationStatus
    observed_positions: tuple[Position, ...] = ()
    observed_orders: tuple[WorkingOrder, ...] = ()
    discrepancies: tuple[Discrepancy, ...] = ()
    reconciled_at: datetime | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def passed(self) -> bool:
        return self.status is ReconciliationStatus.PASSED

    @property
    def may_trade(self) -> bool:
        """Reconciliation is a precondition for trading, never a suggestion."""
        return self.passed

    def render(self) -> str:
        L = ["RECONCILIATION", "-" * 14]
        L.append(f"  status     : {self.status.value}")
        L.append(f"  positions  : {len(self.observed_positions)}")
        for p in self.observed_positions:
            L.append(f"      {p.contract_id}  size {p.size:+d} @ {p.average_price}")
        L.append(f"  working orders: {len(self.observed_orders)}")
        for o in self.observed_orders:
            L.append(f"      {o.order_id}  {o.side.value} {o.size} "
                     f"[{o.state.value}]")
        if self.discrepancies:
            L.append("  DISCREPANCIES:")
            for d in self.discrepancies:
                L.append(f"      {d}")
            L.append("")
            L.append("  Trading is BLOCKED. These are not corrected automatically:")
            L.append("  an offsetting order placed against a state we do not")
            L.append("  understand can double the exposure instead of removing it.")
            L.append("  A human must resolve this.")
        for note in self.notes:
            L.append(f"  note: {note}")
        return "\n".join(L)


def reconcile(
    *,
    broker_positions: Sequence[Position],
    broker_orders: Sequence[WorkingOrder],
    expected_position_size: int = 0,
    expected_order_ids: Sequence[str] = (),
    now: datetime | None = None,
    symbol_filter: str | None = None,
) -> ReconciliationResult:
    """Compare broker truth against what we believed. Pure.

    ``expected_position_size`` is what LOCAL state claims. Anything the broker
    reports that local state did not expect is a discrepancy -- including the
    common and dangerous case of local state saying "flat" while the broker
    holds a position.
    """
    discrepancies: list[Discrepancy] = []
    notes: list[str] = []

    positions = [
        p for p in broker_positions
        if symbol_filter is None or symbol_filter in p.contract_id
    ]
    orders = [
        o for o in broker_orders
        if symbol_filter is None or symbol_filter in o.contract_id
    ]

    actual_size = sum(p.size for p in positions)

    if actual_size != expected_position_size:
        discrepancies.append(Discrepancy(
            "POSITION_MISMATCH",
            f"broker reports net {actual_size:+d}, local state expected "
            f"{expected_position_size:+d}"
            + (
                ". Local state believed it was FLAT: this is the case that "
                "must never be assumed away"
                if expected_position_size == 0 and actual_size != 0
                else ""
            ),
        ))

    expected_ids = set(expected_order_ids)
    for order in orders:
        if order.order_id not in expected_ids:
            discrepancies.append(Discrepancy(
                "UNEXPECTED_ORDER",
                f"working order {order.order_id} ({order.side.value} {order.size}) "
                "is live at the broker but unknown to local state",
            ))
    observed_ids = {o.order_id for o in orders}
    for missing in expected_ids - observed_ids:
        discrepancies.append(Discrepancy(
            "MISSING_ORDER",
            f"local state expected order {missing} but the broker does not "
            "report it; it may have filled or been cancelled",
        ))

    unknown_state = [o for o in orders if o.state.requires_reconciliation]
    if unknown_state:
        notes.append(
            f"{len(unknown_state)} order(s) reported in UNKNOWN state; their "
            "status must be established before trading"
        )

    status = (
        ReconciliationStatus.PASSED if not discrepancies
        else ReconciliationStatus.FAILED
    )
    if status is ReconciliationStatus.PASSED and not positions and not orders:
        notes.append("broker confirms flat with no working orders")

    return ReconciliationResult(
        status=status,
        observed_positions=tuple(positions),
        observed_orders=tuple(orders),
        discrepancies=tuple(discrepancies),
        reconciled_at=now,
        notes=tuple(notes),
    )
