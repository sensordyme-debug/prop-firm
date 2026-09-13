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

THINGS THIS MODULE REFUSES TO GUESS
-----------------------------------
1. **Net liquidation.** ``Account`` in project-x-py 4.3 exposes ``balance``
   only -- there is no net-liq field. Net liq must be derived as
   ``balance + unrealised P&L``. If a position is open and the unrealised
   component cannot be computed honestly, this module raises
   :class:`SnapshotUnavailable` rather than falling back to bare balance.

2. **The trailing MLL floor.** The gateway API does not expose it anywhere.
   It is reconstructed by ``mll_tracker`` from persisted end-of-day balances.
   If that state is missing, stale or unparseable, ``mll_floor`` is ``None``
   and the governor halts -- it is never defaulted.

3. **The clock.** ``now_utc`` is the only sanctioned clock read in the system,
   and it reads UTC. See its docstring for why that matters more than it looks.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from math import isclose
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol
from zoneinfo import ZoneInfo

from governor import ET, AccountSnapshot
from market_calendar import previous_trading_date
from mll_tracker import STATE_FILENAME, MllStateError, floor_for, is_stale, parse_state

if TYPE_CHECKING:  # pragma: no cover - typing only
    from project_x_py.models import Account, Instrument, Position, Trade

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

# Trade.side, matching project_x_py.OrderSide: BUY = 0, SELL = 1.
SIDE_BUY: Final[int] = 0


class SnapshotUnavailable(RuntimeError):
    """Raised when an honest snapshot cannot be built.

    The caller must treat this as "do not trade" -- never as "assume flat" and
    never as "assume net liq equals balance".
    """


class _PriceSource(Protocol):
    async def get_current_price(self) -> float | None: ...


# ---------------------------------------------------------------------------
# Clock
# ---------------------------------------------------------------------------


def now_utc() -> datetime:
    """The current instant, in UTC. The only clock read in the system.

    It reads UTC rather than ET on purpose. The tempting alternative,
    ``datetime.now().replace(tzinfo=ET)``, produces a datetime that is aware,
    passes every ``_require_aware`` check, and is silently wrong by the host's
    UTC offset -- on a machine not set to ET, every session boundary shifts.
    ``datetime.now(timezone.utc)`` cannot express that bug.

    Convert to ET for display only; the governor converts internally for its
    own bucketing and comparisons.
    """
    return datetime.now(UTC)


def to_et(value: datetime, tz: ZoneInfo = ET) -> datetime:
    """Convert an aware datetime to ET. For display and date bucketing only."""
    if value.tzinfo is None:
        raise SnapshotUnavailable(
            "refusing to localise a naive datetime; it would be read as the "
            "host's local time. Use now_utc()."
        )
    return value.astimezone(tz)


# ---------------------------------------------------------------------------
# Filesystem
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


def load_mll_floor(
    today: date,
    root: Path | None = None,
) -> tuple[float | None, str]:
    """Read the tracked trailing MLL floor. Fails closed, without exceptions.

    Returns ``(floor, explanation)``. ``floor`` is ``None`` whenever the state
    is missing, unparseable or stale, and the governor turns that into
    ``MLL_STATE_UNAVAILABLE``. There is deliberately no default: a floor of
    zero would make the buffer check pass unconditionally and disable the only
    permanent-failure guard in silence.
    """
    path = (root or project_root()) / STATE_FILENAME
    if not path.is_file():
        return None, (
            f"no MLL state at {path}. Seed it from the TopstepX dashboard: "
            "python -m src.mll_tracker --seed --mll <displayed floor>"
        )
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"cannot read {path}: {exc}"

    try:
        state = parse_state(raw)
    except MllStateError as exc:
        return None, f"MLL state at {path} is unusable: {exc}"

    previous = previous_trading_date(today)
    if previous is None:
        return None, (
            f"cannot determine the previous trading date before {today}; "
            "the market calendar may need extending"
        )
    if is_stale(state, today, previous):
        return None, (
            f"MLL state is stale: last end-of-day recorded "
            f"{state.last_eod_date}, but {previous} has since closed. Record "
            "the session's closing balance before trading."
        )
    return floor_for(state), f"floor {floor_for(state):,.2f} (locked={state.locked})"


# ---------------------------------------------------------------------------
# Instrument arithmetic
# ---------------------------------------------------------------------------


def point_value(instrument: Instrument) -> float:
    """Dollars per one full point of price movement."""
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
# Counting entries from the fill history
# ---------------------------------------------------------------------------


def count_entries(signed_fills: Sequence[int]) -> int:
    """Count position-opening events from a time-ordered list of signed fills.

    Pure arithmetic, no SDK types, so it is testable with plain integers.

    A "trade" in the strategy's sense is an ENTRY, not a fill: a round trip
    produces two fills and must count as one. So we walk the running position
    and count each transition out of flat. A reversal (long straight to short
    without passing through flat) also counts, because it opens new exposure.
    """
    position = 0
    entries = 0
    for delta in signed_fills:
        new_position = position + delta
        if position == 0 and new_position != 0:
            entries += 1
        elif (
            position != 0
            and new_position != 0
            and (position > 0) != (new_position > 0)
        ):
            entries += 1  # reversal: new exposure without passing through flat
        position = new_position
    return entries


def signed_fills_from_trades(trades: Sequence[Trade]) -> list[int]:
    """Convert SDK trades to time-ordered signed size deltas.

    ``Trade.side`` follows ``project_x_py.OrderSide`` (BUY = 0, SELL = 1).
    Voided trades are skipped: they did not happen.
    """
    live = [t for t in trades if not getattr(t, "voided", False)]
    live.sort(key=lambda t: str(t.creationTimestamp))
    return [
        int(t.size) if int(t.side) == SIDE_BUY else -int(t.size)
        for t in live
    ]


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
    mll_floor: float | None,
    session_start_balance: float,
    now: datetime | None = None,
    root: Path | None = None,
    observed_trades: int | None = None,
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
        mll_floor=None if mll_floor is None else float(mll_floor),
        open_position_size=valuation.size,
        session_start_balance=float(session_start_balance),
        now=now or now_utc(),
        kill_switch_active=kill_switch_present(root),
        observed_trades=observed_trades,
    )


async def observed_entries_since(
    client: object,
    since: datetime,
) -> int | None:
    """Count real entries in the fill history since the session anchor.

    Returns ``None`` if the history cannot be read -- the governor treats that
    as "no observation", falling back to the tracked count, rather than as
    "zero trades", which would be an unsafe reading.
    """
    try:
        trades = await client.search_trades(start_date=since)  # type: ignore[attr-defined]
    except Exception:
        return None
    if trades is None:
        return None
    return count_entries(signed_fills_from_trades(trades))


async def snapshot_from_suite(
    suite: object,
    *,
    instrument: Instrument,
    mll_floor: float | None,
    session_start_balance: float,
    now: datetime | None = None,
    root: Path | None = None,
    observed_trades: int | None = None,
) -> AccountSnapshot:
    """Convenience wrapper over a live ``TradingSuite``.

    Kept separate from :func:`build_snapshot` so the latter stays trivially
    testable with fakes and needs no live connection.
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
        observed_trades=observed_trades,
    )


def mll_floor_from_env(default: float | None = None) -> float:
    """Read a manually pinned MLL floor from the environment.

    Prefer :func:`load_mll_floor`, which reconstructs the floor from tracked
    end-of-day balances. This exists for a pinned override during setup.

    Raises rather than defaulting to zero: a zero floor would make the buffer
    check pass unconditionally and silently disable the guard.
    """
    raw = os.environ.get("MLL_FLOOR")
    if raw is None or raw.strip() == "":
        if default is None:
            raise SnapshotUnavailable(
                "MLL_FLOOR is not set and no default was supplied; refusing to "
                "assume a floor (a wrong floor disables the MLL guard)"
            )
        return float(default)
    try:
        return float(raw)
    except ValueError as exc:
        raise SnapshotUnavailable(
            f"MLL_FLOOR={raw!r} is not a number; refusing to guess a floor"
        ) from exc
