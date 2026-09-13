"""Verify that an open position is actually protected. Never assume it.

THE FAILURE THIS PREVENTS
-------------------------
A bracket request was sent and returned without an exception, so the position
is protected. That reasoning is wrong in at least three ways on this venue:

1. **ProjectX brackets depend on an ACCOUNT SETTING.** In the default
   "Position Brackets" mode, submitted ``stopLossBracket`` /
   ``takeProfitBracket`` fields are REJECTED; only "Auto OCO Brackets" mode
   accepts them (docs/PROJECTX_API.md §4.1). A correct-looking request on a
   default account can leave a naked entry.

2. **A response is not a confirmation.** A timeout leaves the order in
   ``UNKNOWN``, which may still be live.

3. **A stop can exist and still not protect you** -- wrong quantity after a
   partial fill, wrong side, or a price on the wrong side of the market.

So protection is a *verified property of observed broker state*, not a
property of a request we made.

    POSITION OPEN + STOP UNKNOWN  ==  UNPROTECTED

There is no third category. Anything not positively verified is unprotected,
and an unprotected position halts the system rather than being traded around.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from execution.models import OrderState, OrderType, Position, Side, WorkingOrder

__all__ = [
    "ProtectionFinding",
    "ProtectionReport",
    "ProtectionStatus",
    "verify_protection",
]


class ProtectionStatus(str, Enum):
    FLAT = "FLAT"                    # nothing to protect
    PROTECTED = "PROTECTED"          # verified against observed broker state
    UNPROTECTED = "UNPROTECTED"      # position exists, protection does not
    UNVERIFIABLE = "UNVERIFIABLE"    # cannot tell -- treated as unprotected

    @property
    def is_safe(self) -> bool:
        """Only two states are safe, and 'probably fine' is not among them."""
        return self in (ProtectionStatus.FLAT, ProtectionStatus.PROTECTED)


@dataclass(frozen=True)
class ProtectionFinding:
    code: str
    detail: str

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


@dataclass(frozen=True)
class ProtectionReport:
    status: ProtectionStatus
    position_size: int
    stop_orders: tuple[WorkingOrder, ...] = ()
    target_orders: tuple[WorkingOrder, ...] = ()
    findings: tuple[ProtectionFinding, ...] = ()

    @property
    def is_safe(self) -> bool:
        return self.status.is_safe

    @property
    def must_halt(self) -> bool:
        return not self.status.is_safe

    def render(self) -> str:
        lines = [
            "PROTECTION",
            "-" * 10,
            f"  status        : {self.status.value}",
            f"  position size : {self.position_size:+d}",
            f"  stop orders   : {len(self.stop_orders)}",
            f"  target orders : {len(self.target_orders)}",
        ]
        for f in self.findings:
            lines.append(f"  {f}")
        if self.must_halt:
            lines += [
                "",
                "  HALT. A position without verified protection is the exact",
                "  exposure CLAUDE.md constraint 1 exists to prevent. Do not",
                "  open anything further; a human resolves this.",
            ]
        return "\n".join(lines)


def verify_protection(
    *,
    position: Position | None,
    working_orders: Sequence[WorkingOrder],
    expected_stop_price: float | None = None,
    tick_size: float = 0.25,
) -> ProtectionReport:
    """Decide whether an open position is verifiably protected. Pure.

    Every check is a reason to answer "no". There is deliberately no branch
    that concludes "protected" from an absence of evidence -- the only path to
    PROTECTED runs through observing a stop that matches the position.
    """
    findings: list[ProtectionFinding] = []

    if position is None or position.size == 0:
        return ProtectionReport(ProtectionStatus.FLAT, 0)

    size = position.size
    side = Side.BUY if size > 0 else Side.SELL
    required_stop_side = side.opposite()

    live = [o for o in working_orders if o.state.is_live]
    unknown = [o for o in live if o.state is OrderState.UNKNOWN]
    stops = [
        o for o in live
        if o.order_type in (OrderType.STOP, OrderType.TRAILING_STOP)
    ]
    targets = [o for o in live if o.order_type is OrderType.LIMIT]

    # 1. A stop must exist at all.
    if not stops:
        findings.append(ProtectionFinding(
            "NO_STOP",
            f"position of {size:+d} with no live stop order. This is a naked "
            "position.",
        ))
        return ProtectionReport(
            ProtectionStatus.UNPROTECTED, size, (), tuple(targets), tuple(findings)
        )

    # 2. Orders in UNKNOWN state cannot be counted as protection: we do not
    #    know whether they are working, filled or rejected.
    if unknown:
        findings.append(ProtectionFinding(
            "UNKNOWN_ORDER_STATE",
            f"{len(unknown)} order(s) in UNKNOWN state; their status must be "
            "established against the broker before protection can be claimed",
        ))

    # 3. Direction. A stop on the same side as the position adds exposure.
    wrong_side = [o for o in stops if o.side is not required_stop_side]
    if wrong_side:
        findings.append(ProtectionFinding(
            "STOP_WRONG_SIDE",
            f"stop side is {wrong_side[0].side.value}, expected "
            f"{required_stop_side.value} to close a {side.value} position",
        ))

    # 4. Quantity. A partial-size stop leaves the remainder naked -- the most
    #    easily missed failure, because a stop DOES exist.
    protected_qty = sum(
        o.remaining for o in stops if o.side is required_stop_side
    )
    if protected_qty < abs(size):
        findings.append(ProtectionFinding(
            "STOP_QUANTITY_SHORT",
            f"stops cover {protected_qty} of {abs(size)} contracts; "
            f"{abs(size) - protected_qty} would be left unprotected",
        ))
    elif protected_qty > abs(size):
        findings.append(ProtectionFinding(
            "STOP_QUANTITY_EXCESS",
            f"stops cover {protected_qty} against a position of {abs(size)}; "
            "the excess would open a NEW position in the opposite direction "
            "if it triggered",
        ))

    # 5. Price sanity. A long's stop must sit below entry, a short's above.
    for order in stops:
        if order.stop_price is None:
            findings.append(ProtectionFinding(
                "STOP_PRICE_MISSING",
                f"stop {order.order_id} reports no stop price",
            ))
            continue
        if side is Side.BUY and order.stop_price >= position.average_price:
            findings.append(ProtectionFinding(
                "STOP_PRICE_INVALID",
                f"long stop {order.stop_price} is at or above the average "
                f"entry {position.average_price}",
            ))
        if side is Side.SELL and order.stop_price <= position.average_price:
            findings.append(ProtectionFinding(
                "STOP_PRICE_INVALID",
                f"short stop {order.stop_price} is at or below the average "
                f"entry {position.average_price}",
            ))

    # 6. If we asked for a specific level, confirm we got roughly that one.
    if expected_stop_price is not None:
        matched = any(
            o.stop_price is not None
            and abs(o.stop_price - expected_stop_price) <= tick_size
            for o in stops
        )
        if not matched:
            findings.append(ProtectionFinding(
                "STOP_PRICE_MISMATCH",
                f"no live stop within one tick of the intended "
                f"{expected_stop_price}",
            ))

    if findings:
        status = (
            ProtectionStatus.UNVERIFIABLE if unknown
            else ProtectionStatus.UNPROTECTED
        )
        return ProtectionReport(
            status, size, tuple(stops), tuple(targets), tuple(findings)
        )

    return ProtectionReport(
        ProtectionStatus.PROTECTED,
        size,
        tuple(stops),
        tuple(targets),
        (ProtectionFinding(
            "VERIFIED",
            f"{protected_qty} contracts covered by a correctly-sided stop",
        ),),
    )
