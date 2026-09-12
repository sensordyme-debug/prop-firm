"""I/O boundary for the risk governor. The ONLY module that imports the SDK.

Thin by design: it observes the world and builds an :class:`AccountSnapshot`.
It makes no decisions -- every threshold, every comparison and every reason
string lives in ``governor.py``, which stays pure and unit-testable.

DELIBERATELY ABSENT: any order-transmission path. There is no ``place_*``,
no ``close_*`` and no flatten execution here yet. The governor can return
``FLATTEN_AND_HALT``, but nothing in this repo can act on it until the
executor is built under the CLAUDE.md build order (gate 1, the connection
test, is still open). ``project_x_py.PositionManager.close_all_positions`` is
the intended primitive when that time comes.

TWO PLACES THIS MODULE REFUSES TO GUESS
---------------------------------------
1. **Net liquidation.** ``Account`` in project-x-py 4.3 exposes ``balance``
   only -- there is no net-liq field. Net liq must be derived as
   ``balance + unrealised P&L``. If a position is open and the unrealised
   component cannot be computed honestly, this module raises
   :class:`SnapshotUnavailable` rather than falling back to bare balance.
   Falling back would under-report the drawdown on an open losing position,
   which is precisely the exposure the Max Loss Limit is enforced against
   (CLAUDE.md design constraint 3).

2. **The trailing MLL floor.** The gateway API does not expose it. It must be
   supplied by the caller from tracked state, so it is a required argument
   with no default.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from math import isclose
from typing import TYPE_CHECKING, Final, Protocol
from zoneinfo import ZoneInfo

from governor import ET, AccountSnapshot

if TYPE_CHECKING:  # pragma: no cover - typing only
    from project_x_py.models import Account, Instrument, Position

KILL_FILE_NAME = ".KILL"

# MNQ contract geometry, verified against the installed SDK's Instrument model:
#   tickSize  = 0.25 index points
#   tickValue = $0.50 per tick
#   point value = tickValue / tickSize = 0.50 / 0.25 = $2.00 per index point
#
# This constant exists because ``Position.unrealized_pnl(price, tick_value=1.0)``
# multiplies a POINT difference by ``tick_value`` and defaults it to 1.0.
# Accepting that default reports exactly HALF of every MNQ move -- a $80 loss
# reads as $40, and the governor's loss trip-wire fires at twice the intended
# drawdown, or not at all. Never call unrealized_pnl without passing this.
MNQ_POINT_VALUE: Final[float] = 2.00


class SnapshotUnavailable(RuntimeError):
    """Raised when an honest snapshot cannot be built.

    The caller must treat this as "do not trade" -- never as "assume flat" and
    never as "assume net liq equals balance".
    """


class _PriceSource(Protocol):
    async def get_current_price(self) -> float | None: ...


# ---------------------------------------------------------------------------
# Filesystem and clock -- the I/O the pure governor must never do itself
# ---------------------------------------------------------------------------


def project_root() -> Path:
    """Repository root: the parent of ``src/``."""
    return Path(__file__).resolve().parent.parent


def kill_switch_present(root: Path | None = None) -> bool:
    """True when the one-action kill switch file exists in the project root.

    Creating ``.KILL`` is the operator's single gesture to stop everything;
    it is gitignored so it can never be committed away.
    """
    return (root or project_root()).joinpath(KILL_FILE_NAME).is_file()


def now_et(tz: ZoneInfo = ET) -> datetime:
    """The current instant, timezone-aware. The only clock read in the system."""
    return datetime.now(tz)


# ---------------------------------------------------------------------------
# Instrument arithmetic
# ---------------------------------------------------------------------------


def point_value(instrument: Instrument) -> float:
    """Dollars per one full point of price movement.

    ``Position.unrealized_pnl(current_price, tick_value)`` multiplies a *point*
    difference by ``tick_value``, and its default is ``1.0``. For MNQ
    (tickSize 0.25, tickValue 0.50) the correct figure is 2.0, so accepting the
    default would report exactly half the true P&L -- a silent 2x under-
    statement of every loss. This module always passes it explicitly.
    """
    tick_size = float(instrument.tickSize)
    tick_value = float(instrument.tickValue)
    if tick_size <= 0 or tick_value <= 0:
        raise SnapshotUnavailable(
            f"instrument {instrument.name!r} has unusable tick geometry "
            f"(tickSize={tick_size}, tickValue={tick_value}); cannot value P&L"
        )
    derived = tick_value / tick_size

    # Cross-check the live contract against the value we reasoned about. If the
    # gateway ever reports different geometry for MNQ, every P&L figure and
    # therefore every trip-wire is wrong, so fail loudly instead of trading on it.
    if instrument.name.upper().startswith("MNQ") and not isclose(
        derived, MNQ_POINT_VALUE, rel_tol=1e-9
    ):
        raise SnapshotUnavailable(
            f"MNQ point value is {derived} but {MNQ_POINT_VALUE} was expected "
            f"(tickSize={tick_size}, tickValue={tick_value}); contract geometry "
            "changed, refusing to value positions until this is reviewed"
        )
    return derived


# ---------------------------------------------------------------------------
# Snapshot construction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PositionValuation:
    """Result of valuing the open book. ``size`` is signed: negative is short."""

    size: int
    unrealised_pnl: float


async def value_positions(
    positions: list[Position],
    price_source: _PriceSource,
    instrument: Instrument,
) -> PositionValuation:
    """Value open positions at the current market price.

    Raises :class:`SnapshotUnavailable` if a position is open but no current
    price is available. A stale or missing price on an open position means net
    liq is unknown, and an unknown net liq must stop trading, not be guessed.
    """
    if not positions:
        return PositionValuation(size=0, unrealised_pnl=0.0)

    price = await price_source.get_current_price()
    if price is None:
        raise SnapshotUnavailable(
            f"{len(positions)} position(s) open but no current price is "
            "available; net liquidation cannot be determined"
        )

    pv = point_value(instrument)
    signed = sum(int(p.signed_size) for p in positions)
    pnl = sum(float(p.unrealized_pnl(price, pv)) for p in positions)
    return PositionValuation(size=signed, unrealised_pnl=pnl)


async def build_snapshot(
    *,
    account: Account,
    positions: list[Position],
    price_source: _PriceSource,
    instrument: Instrument,
    mll_floor: float,
    session_start_balance: float,
    now: datetime | None = None,
    root: Path | None = None,
) -> AccountSnapshot:
    """Observe the account and return an :class:`AccountSnapshot`.

    No thresholds are applied and no decision is taken here. ``mll_floor`` and
    ``session_start_balance`` are required because neither is derivable from
    the gateway API; they come from the caller's tracked state.
    """
    valuation = await value_positions(positions, price_source, instrument)
    balance = float(account.balance)

    return AccountSnapshot(
        net_liq=balance + valuation.unrealised_pnl,
        balance=balance,
        mll_floor=float(mll_floor),
        open_position_size=valuation.size,
        session_start_balance=float(session_start_balance),
        now=now or now_et(),
        kill_switch_active=kill_switch_present(root),
    )


async def snapshot_from_suite(
    suite: object,
    *,
    instrument: Instrument,
    mll_floor: float,
    session_start_balance: float,
    now: datetime | None = None,
    root: Path | None = None,
) -> AccountSnapshot:
    """Convenience wrapper over a live ``TradingSuite``.

    Kept separate from :func:`build_snapshot` so the latter stays trivially
    testable with fakes and needs no live connection.

    Uses the verified project-x-py 4.3 surface:
    ``suite.positions.get_all_positions()`` and ``suite.data.get_current_price()``.
    """
    client = getattr(suite, "_client", None) or getattr(suite, "client", None)
    if client is None:
        raise SnapshotUnavailable("TradingSuite exposes no client for account info")

    account = client.get_account_info()  # sync in v4.3
    if account is None:
        raise SnapshotUnavailable("account info unavailable; cannot value the account")

    positions = await suite.positions.get_all_positions()  # type: ignore[attr-defined]
    return await build_snapshot(
        account=account,
        positions=positions,
        price_source=suite.data,  # type: ignore[attr-defined]
        instrument=instrument,
        mll_floor=mll_floor,
        session_start_balance=session_start_balance,
        now=now,
        root=root,
    )


def mll_floor_from_env(default: float | None = None) -> float:
    """Read the tracked trailing MLL floor from the environment.

    Not available from the API. Raises rather than defaulting to zero: a zero
    floor would make the buffer check pass unconditionally and silently
    disable the only permanent-failure guard in the system.
    """
    raw = os.environ.get("MLL_FLOOR")
    if raw is None or raw.strip() == "":
        if default is None:
            raise SnapshotUnavailable(
                "MLL_FLOOR is not set and no default was supplied; refusing to "
                "assume a floor (a wrong floor disables the MLL guard)"
            )
        return float(default)
    return float(raw)
