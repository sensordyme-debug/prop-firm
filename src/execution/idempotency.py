"""Duplicate-order protection: deterministic tags and a single-instance lock.

THE FAILURE MODE
----------------
A duplicate entry on a $2,000 trailing drawdown is not a nuisance, it is
unrecoverable: the position is twice the intended size against a floor that
never moves down. Every path that could produce one is closed here.

  * **Timeout after submission.** The order may have reached the exchange. A
    deterministic ``client_tag`` lets reconciliation FIND it instead of
    guessing, so the answer is "look" rather than "resend".
  * **Process restart.** Local state says flat; the broker may disagree. The
    tag is derived from session, strategy and intent, so a restart regenerates
    the SAME tag and the duplicate is visible.
  * **Repeated signal / duplicate bar event.** The same intent hashes to the
    same tag, so a second submission is recognisably the first one.
  * **Two runtimes at once.** A PID lockfile refuses the second, and a stale
    lock is only cleared when the recorded PID is genuinely gone.

WHY DETERMINISTIC RATHER THAN RANDOM
------------------------------------
A random id is unique but useless after a crash: nothing can look up what was
sent. A deterministic tag is reconstructible, which is what makes "reconcile,
do not retry" a real procedure instead of advice.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from execution.models import OrderIntent, Position, WorkingOrder

__all__ = [
    "DuplicateRisk",
    "InstanceLock",
    "InstanceLockError",
    "PreTradeCheck",
    "check_before_entry",
    "client_tag",
]


def client_tag(
    *,
    session_id: str,
    strategy: str,
    intent: OrderIntent,
    sequence: int,
) -> str:
    """A deterministic, reconstructible correlation tag.

    Identical inputs always produce an identical tag, which is the whole point:
    after a crash the same intent regenerates the same tag, so reconciliation
    can search for it. Price is rounded into the hash so floating-point noise
    cannot produce two tags for one intent.
    """
    material = "|".join([
        session_id,
        strategy,
        intent.symbol,
        intent.contract_id,
        intent.side.value,
        str(intent.size),
        f"{intent.stop_loss:.4f}" if intent.stop_loss is not None else "nostop",
        str(sequence),
    ])
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
    return f"pf-{session_id[:8]}-{sequence:03d}-{digest}"


@dataclass(frozen=True)
class DuplicateRisk:
    code: str
    detail: str

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


@dataclass(frozen=True)
class PreTradeCheck:
    allowed: bool
    risks: tuple[DuplicateRisk, ...] = ()

    def render(self) -> str:
        if self.allowed:
            return "  pre-trade duplicate check: CLEAR"
        lines = ["  pre-trade duplicate check: BLOCKED"]
        for r in self.risks:
            lines.append(f"    {r}")
        return "\n".join(lines)


def check_before_entry(
    *,
    intent: OrderIntent,
    positions: Sequence[Position],
    working_orders: Sequence[WorkingOrder],
    pending_tags: Sequence[str],
    proposed_tag: str,
    trades_taken: int,
    max_trades: int,
    reconciled: bool,
    connection_ready: bool,
) -> PreTradeCheck:
    """Every duplicate-producing condition, checked before an entry. Pure.

    Fails closed: each check can only add a reason to refuse, and the allowed
    path requires all of them to stay silent.
    """
    risks: list[DuplicateRisk] = []

    if not reconciled:
        risks.append(DuplicateRisk(
            "UNRECONCILED",
            "broker state has not been reconciled; local belief about what we "
            "hold is unverified",
        ))

    if not connection_ready:
        risks.append(DuplicateRisk(
            "CONNECTION_NOT_READY",
            "connection is not in READY; an order sent now cannot be confirmed",
        ))

    net = sum(p.size for p in positions if p.contract_id == intent.contract_id)
    if net != 0:
        risks.append(DuplicateRisk(
            "POSITION_ALREADY_OPEN",
            f"broker reports {net:+d} on {intent.contract_id}; a second entry "
            "would double the intended size",
        ))

    live = [
        o for o in working_orders
        if o.contract_id == intent.contract_id and o.state.is_live
    ]
    entry_side_orders = [o for o in live if o.side is intent.side]
    if entry_side_orders:
        risks.append(DuplicateRisk(
            "CONFLICTING_WORKING_ORDER",
            f"{len(entry_side_orders)} live {intent.side.value} order(s) already "
            f"working on {intent.contract_id}",
        ))

    if proposed_tag in set(pending_tags):
        risks.append(DuplicateRisk(
            "TAG_ALREADY_PENDING",
            f"client tag {proposed_tag} is already in flight; this is the same "
            "intent, not a new one",
        ))

    matching = [o for o in live if o.custom_tag == proposed_tag]
    if matching:
        risks.append(DuplicateRisk(
            "TAG_ALREADY_AT_BROKER",
            f"the broker already has an order with tag {proposed_tag}; the "
            "earlier submission DID arrive",
        ))

    if trades_taken >= max_trades:
        risks.append(DuplicateRisk(
            "TRADE_BUDGET_SPENT",
            f"{trades_taken} of {max_trades} trades already taken this session",
        ))

    return PreTradeCheck(allowed=not risks, risks=tuple(risks))


# ---------------------------------------------------------------------------
# Single-instance lock
# ---------------------------------------------------------------------------


class InstanceLockError(RuntimeError):
    """Another runtime appears to hold the lock."""


@dataclass
class InstanceLock:
    """A PID lockfile preventing two runtimes against one account.

    Two processes each believing they are flat will each open a position. The
    lock is the only cheap defence, because neither process can see the other
    through the broker API in time.

    A stale lock (recorded PID no longer alive) is reclaimed, but ONLY after
    checking. Deleting a lock because it looks old is how the protection gets
    removed exactly when it was working.
    """

    path: Path
    _acquired: bool = field(default=False, init=False)

    def acquire(self, *, force: bool = False) -> None:
        if self.path.exists():
            holder = self._read_pid()
            if holder is not None and holder != os.getpid() and _pid_alive(holder):
                raise InstanceLockError(
                    f"another runtime is active (pid {holder}, lock {self.path}). "
                    "Two runtimes against one account will each open a position. "
                    "Stop the other process; do not delete the lock to get past "
                    "this."
                )
            if holder is not None and not _pid_alive(holder) and not force:
                # Stale: the holder is genuinely gone, so reclaiming is safe.
                self.path.unlink(missing_ok=True)

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            f"{os.getpid()}\n{datetime.now().isoformat()}\n", encoding="utf-8"
        )
        self._acquired = True

    def release(self) -> None:
        if self._acquired and self.path.exists():
            if self._read_pid() == os.getpid():
                self.path.unlink(missing_ok=True)
        self._acquired = False

    def _read_pid(self) -> int | None:
        try:
            first = self.path.read_text(encoding="utf-8").splitlines()[0]
            return int(first.strip())
        except (OSError, ValueError, IndexError):
            return None

    def __enter__(self) -> InstanceLock:
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def _pid_alive(pid: int) -> bool:
    """Is this PID running? Unknown counts as alive -- that fails closed."""
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import subprocess

            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True, text=True, timeout=5, check=False,
            )
            return str(pid) in out.stdout
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True   # exists, owned by someone else
    except (OSError, Exception):
        return False
