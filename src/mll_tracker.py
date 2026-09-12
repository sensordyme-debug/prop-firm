"""Reconstruct the trailing Max Loss Limit floor that the API does not expose.

WHY THIS EXISTS
---------------
The MLL is the only rule whose breach PERMANENTLY ends the account, and the
ProjectX gateway reports it nowhere. Verified against the installed SDK 4.3.0:
``Account`` returns only ``id, name, balance, canTrade, isVisible, simulated``;
no model, none of the 21 endpoints, and no websocket payload carries a trailing
drawdown, max-loss or floor field. So we keep our own books on it.

That is an uncomfortable position, and the design reflects it: this module
never estimates. Anything it cannot compute exactly becomes a halt.

MECHANICS (Topstep 50K Combine)
-------------------------------
  * The floor trails the END-OF-DAY closing balance. It only ever rises.
  * It is enforced in REAL TIME against net liq including unrealised P&L --
    intraday equity does not move the floor, but it is what gets measured.
  * It LOCKS PERMANENTLY once it reaches the starting balance.
  * 50K account: distance $2,000, so the initial floor is $48,000.

    mll_floor = min(max_eod_balance_ever_seen - mll_distance, starting_balance)

Worked through Topstep's own example, and pinned as tests:
  * start 50,000 -> floor 48,000
  * first EOD close 50,500 -> floor 48,500
  * a later EOD close of 50,000 -> floor STAYS 48,500 (it never falls)
  * once EOD reaches 52,000 -> floor 50,000 and locks there forever

FUNDED-ACCOUNT NOTE (not yet implemented)
-----------------------------------------
On a funded XFA account Topstep sets the MLL to $0 permanently after the first
payout, which is a different regime rather than a different number. When this
account is funded, that needs an explicit state transition here -- do not try
to express it by editing ``mll_distance``.

This module is pure arithmetic. All file I/O lives in ``governor_adapter``.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any, Final

STATE_FILENAME: Final[str] = "mll_state.json"
DEFAULT_STARTING_BALANCE: Final[float] = 50_000.0
DEFAULT_MLL_DISTANCE: Final[float] = 2_000.0

__all__ = [
    "STATE_FILENAME",
    "DEFAULT_STARTING_BALANCE",
    "DEFAULT_MLL_DISTANCE",
    "MllState",
    "MllStateError",
    "floor_for",
    "record_eod",
    "seed_state",
    "reconcile",
    "parse_state",
    "serialise_state",
    "is_stale",
]


class MllStateError(ValueError):
    """The persisted state is missing, unparseable, or inconsistent.

    Always fatal to trading. The caller converts this into a halt; it must
    never be swallowed into a default floor.
    """


@dataclass(frozen=True)
class MllState:
    starting_balance: float
    mll_distance: float
    max_eod_balance: float
    locked: bool = False
    last_eod_date: date | None = None
    last_eod_balance: float | None = None

    @property
    def floor(self) -> float:
        return floor_for(self)


def floor_for(state: MllState) -> float:
    """The trailing floor. Rises with EOD highs, never falls, locks at start.

    ``min`` is what implements the lock: once ``max_eod_balance`` is a full
    ``mll_distance`` above the starting balance, the trailing term overtakes
    the starting balance and the floor pins there permanently.
    """
    trailing = state.max_eod_balance - state.mll_distance
    return min(trailing, state.starting_balance)


def seed_state(
    *,
    starting_balance: float = DEFAULT_STARTING_BALANCE,
    mll_distance: float = DEFAULT_MLL_DISTANCE,
    observed_floor: float | None = None,
) -> MllState:
    """Build initial state, optionally anchored to what TopstepX displays.

    Seeding from the dashboard is the honest starting point: it makes our
    books agree with the firm's on day one instead of assuming they do.
    """
    if starting_balance <= 0:
        raise MllStateError(f"starting_balance must be positive, got {starting_balance}")
    if mll_distance <= 0:
        raise MllStateError(f"mll_distance must be positive, got {mll_distance}")

    max_eod = starting_balance
    if observed_floor is not None:
        if observed_floor > starting_balance:
            raise MllStateError(
                f"observed floor {observed_floor:,.2f} is above the starting balance "
                f"{starting_balance:,.2f}; the floor can never exceed it"
            )
        # Invert the formula: a displayed floor implies this EOD high.
        max_eod = max(starting_balance, observed_floor + mll_distance)

    state = MllState(
        starting_balance=float(starting_balance),
        mll_distance=float(mll_distance),
        max_eod_balance=float(max_eod),
    )
    return replace(state, locked=_is_locked(state))


def _is_locked(state: MllState) -> bool:
    return state.max_eod_balance - state.mll_distance >= state.starting_balance


def record_eod(state: MllState, eod_date: date, eod_balance: float) -> MllState:
    """Fold one end-of-day closing balance into the state.

    Called at the 18:00 ET roll, when the account is guaranteed flat and
    balance therefore equals net liq. Pure: returns new state.

    Recording the same date twice is rejected rather than reapplied -- a
    double-count would ratchet the floor up on a day that did not happen.
    """
    if eod_balance < 0:
        raise MllStateError(f"eod_balance must not be negative, got {eod_balance}")
    if state.last_eod_date is not None and eod_date < state.last_eod_date:
        raise MllStateError(
            f"refusing to record {eod_date} before the last recorded date "
            f"{state.last_eod_date}; the EOD series must move forward"
        )
    if state.last_eod_date == eod_date:
        if state.last_eod_balance != eod_balance:
            raise MllStateError(
                f"{eod_date} already recorded with balance "
                f"{state.last_eod_balance}, now given {eod_balance}"
            )
        return state

    # The high-water mark only ever rises. A losing day leaves the floor alone.
    new_max = max(state.max_eod_balance, float(eod_balance))
    if state.locked:
        new_max = state.max_eod_balance  # locked: further highs change nothing

    updated = replace(
        state,
        max_eod_balance=new_max,
        last_eod_date=eod_date,
        last_eod_balance=float(eod_balance),
    )
    return replace(updated, locked=_is_locked(updated))


def reconcile(state: MllState, dashboard_floor: float) -> tuple[MllState, str | None]:
    """Compare our floor against what TopstepX displays.

    On disagreement we always adopt the HIGHER floor -- less headroom, stops
    us sooner. Being wrong in the safe direction costs a missed trade; being
    wrong in the other direction costs the account.

    Returns (state, warning). A warning is not optional reading: it means our
    books and the firm's disagree and somebody must find out why.
    """
    ours = floor_for(state)
    if abs(ours - dashboard_floor) < 0.005:
        return state, None

    if dashboard_floor > ours:
        implied_max_eod = dashboard_floor + state.mll_distance
        adopted = replace(state, max_eod_balance=max(state.max_eod_balance, implied_max_eod))
        adopted = replace(adopted, locked=_is_locked(adopted))
        return adopted, (
            f"MLL DISAGREEMENT: ours {ours:,.2f}, TopstepX {dashboard_floor:,.2f}. "
            f"Adopted the higher (TopstepX) figure. Our EOD history is probably "
            f"missing a day. Investigate before trading."
        )

    return state, (
        f"MLL DISAGREEMENT: ours {ours:,.2f}, TopstepX {dashboard_floor:,.2f}. "
        f"Keeping ours because it is higher (less headroom). TopstepX may not "
        f"have settled the last session yet. Investigate before trading."
    )


# ---------------------------------------------------------------------------
# Serialisation (pure: strings and dicts in, dataclasses out)
# ---------------------------------------------------------------------------


def serialise_state(state: MllState) -> str:
    payload: dict[str, Any] = asdict(state)
    payload["last_eod_date"] = (
        state.last_eod_date.isoformat() if state.last_eod_date else None
    )
    payload["derived_floor"] = floor_for(state)  # for humans; never read back
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


_REQUIRED = ("starting_balance", "mll_distance", "max_eod_balance")


def parse_state(raw: str) -> MllState:
    """Parse persisted state, refusing anything doubtful.

    Every failure path raises. There is deliberately no 'best effort' branch:
    a plausible-looking floor derived from a damaged file is worse than no
    floor at all, because it silently re-enables trading.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MllStateError(f"MLL state is not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise MllStateError(f"MLL state must be a JSON object, got {type(data).__name__}")

    missing = [k for k in _REQUIRED if k not in data]
    if missing:
        raise MllStateError(f"MLL state is missing required field(s): {', '.join(missing)}")

    try:
        starting = float(data["starting_balance"])
        distance = float(data["mll_distance"])
        max_eod = float(data["max_eod_balance"])
    except (TypeError, ValueError) as exc:
        raise MllStateError(f"MLL state has a non-numeric field: {exc}") from exc

    if starting <= 0 or distance <= 0:
        raise MllStateError(
            f"MLL state is nonsensical: starting_balance={starting}, "
            f"mll_distance={distance}; both must be positive"
        )
    if max_eod < starting:
        raise MllStateError(
            f"max_eod_balance {max_eod:,.2f} is below starting_balance "
            f"{starting:,.2f}; the high-water mark cannot start below the open"
        )

    last_date_raw = data.get("last_eod_date")
    last_date: date | None = None
    if last_date_raw is not None:
        try:
            last_date = date.fromisoformat(str(last_date_raw))
        except ValueError as exc:
            raise MllStateError(f"last_eod_date is not an ISO date: {exc}") from exc

    last_balance = data.get("last_eod_balance")
    state = MllState(
        starting_balance=starting,
        mll_distance=distance,
        max_eod_balance=max_eod,
        last_eod_date=last_date,
        last_eod_balance=None if last_balance is None else float(last_balance),
    )
    return replace(state, locked=_is_locked(state))


def is_stale(state: MllState, today: date, previous_trading_date: date) -> bool:
    """True when the EOD series has not been updated recently enough.

    Stale state means the floor may be lower than reality -- we would be
    trading against a floor from before the last session closed.
    """
    if state.last_eod_date is None:
        return True
    return state.last_eod_date < previous_trading_date and state.last_eod_date < today


# ---------------------------------------------------------------------------
# CLI -- the only part of this module that touches the filesystem
# ---------------------------------------------------------------------------


def _state_path(root: Path | None = None) -> Path:
    base = root or Path(__file__).resolve().parent.parent
    return base / STATE_FILENAME


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.mll_tracker",
        description="Seed or verify the locally tracked trailing MLL floor.",
    )
    parser.add_argument("--seed", action="store_true",
                        help="create mll_state.json from the displayed floor")
    parser.add_argument("--verify", action="store_true",
                        help="compare the displayed floor against ours; "
                             "exits non-zero on mismatch")
    parser.add_argument("--show", action="store_true", help="print current state")
    parser.add_argument("--mll", type=float, default=None,
                        help="the floor TopstepX currently displays")
    parser.add_argument("--starting-balance", type=float,
                        default=DEFAULT_STARTING_BALANCE)
    parser.add_argument("--distance", type=float, default=DEFAULT_MLL_DISTANCE)
    parser.add_argument("--root", type=Path, default=None)
    args = parser.parse_args(argv)

    path = _state_path(args.root)

    if args.seed:
        if args.mll is None:
            print("--seed requires --mll, the floor TopstepX displays.", file=sys.stderr)
            print("Read it from the dashboard; do not guess it.", file=sys.stderr)
            return 2
        if path.exists():
            print(f"REFUSING: {path} already exists. Delete it deliberately if you "
                  "really mean to reseed -- reseeding discards the EOD history "
                  "that the floor is built from.", file=sys.stderr)
            return 2
        try:
            state = seed_state(
                starting_balance=args.starting_balance,
                mll_distance=args.distance,
                observed_floor=args.mll,
            )
        except MllStateError as exc:
            print(f"REFUSING: {exc}", file=sys.stderr)
            return 2
        path.write_text(serialise_state(state), encoding="utf-8")
        print(f"Seeded {path}")
        print(f"  starting balance  ${state.starting_balance:,.2f}")
        print(f"  MLL distance      ${state.mll_distance:,.2f}")
        print(f"  max EOD balance   ${state.max_eod_balance:,.2f}")
        print(f"  floor             ${state.floor:,.2f}")
        print(f"  locked            {state.locked}")
        return 0

    if not path.exists():
        print(f"No MLL state at {path}. Seed it first:", file=sys.stderr)
        print("  python -m src.mll_tracker --seed --mll 48000", file=sys.stderr)
        return 2

    try:
        state = parse_state(path.read_text(encoding="utf-8"))
    except MllStateError as exc:
        print(f"MLL STATE UNUSABLE: {exc}", file=sys.stderr)
        print("Trading must stay halted until this is fixed.", file=sys.stderr)
        return 2

    if args.show or not args.verify:
        print(f"state file        {path}")
        print(f"  starting balance  ${state.starting_balance:,.2f}")
        print(f"  MLL distance      ${state.mll_distance:,.2f}")
        print(f"  max EOD balance   ${state.max_eod_balance:,.2f}")
        print(f"  last EOD          {state.last_eod_date} "
              f"({'' if state.last_eod_balance is None else f'${state.last_eod_balance:,.2f}'})")
        print(f"  FLOOR             ${state.floor:,.2f}")
        print(f"  locked            {state.locked}")
        if not args.verify:
            return 0

    if args.mll is None:
        print("--verify requires --mll, the floor TopstepX displays.", file=sys.stderr)
        return 2

    adopted, warning = reconcile(state, args.mll)
    if warning is None:
        print(f"MATCH: ours and TopstepX both report ${state.floor:,.2f}")
        return 0

    print(warning, file=sys.stderr)
    if adopted is not state:
        path.write_text(serialise_state(adopted), encoding="utf-8")
        print(f"Adopted the higher floor: ${adopted.floor:,.2f}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
