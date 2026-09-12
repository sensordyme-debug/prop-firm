"""Risk governor: pure decision logic for the Topstep 50K Combine bot.

The governor is authoritative (CLAUDE.md, design constraint 2): the strategy
proposes, the governor decides, and no order is transmitted without its approval.

PURITY CONTRACT
---------------
Everything in this module is a pure function of its arguments:

  * no network calls
  * no SDK imports
  * no filesystem access
  * no clock reads -- ``now`` always arrives on the snapshot

That is what makes every trip-wire unit-testable at its exact boundary without
credentials or a live account. All I/O -- reading the account, stat-ing the kill
file, reading the clock -- belongs to ``governor_adapter.py``.

ACCOUNTING RULES (CLAUDE.md, design constraints 3 and 4)
--------------------------------------------------------
  * Session P&L is measured against **net liquidation**, never realised P&L.
    The firm enforces the Max Loss Limit in real time on net liq including
    open positions, so any check against realised P&L is wrong by definition.
  * The session boundary is 18:00 America/New_York, never midnight. All daily
    accounting resets there. See ``session_start_for``.

All datetimes are timezone-aware. Naive datetimes are rejected, not coerced.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from datetime import datetime, time, timedelta, timezone
from enum import Enum
from typing import Final
from zoneinfo import ZoneInfo

ET: Final[ZoneInfo] = ZoneInfo("America/New_York")

__all__ = [
    "ET",
    "Action",
    "Reason",
    "Config",
    "AccountSnapshot",
    "GovernorState",
    "GovernorDecision",
    "session_start_for",
    "hard_flatten_at",
    "entry_cutoff_at",
    "rth_open_at",
    "compute_session_pnl",
    "evaluate",
    "roll_session",
    "record_trade",
    "update_high_balance",
    "apply_decision",
]


class Action(str, Enum):
    """What the caller must do. The governor never places orders itself."""

    CONTINUE = "CONTINUE"
    FLATTEN_AND_HALT = "FLATTEN_AND_HALT"
    REFUSE_ENTRY = "REFUSE_ENTRY"


class Reason:
    """Stable reason codes.

    Every decision string starts with one of these followed by ``": "`` and a
    human-readable detail. Tests assert on the code so that rewording a detail
    can never silently weaken a test.
    """

    # -- trip-wires: flatten and halt for the session ----------------------
    MLL_FLOOR_BUFFER = "MLL_FLOOR_BUFFER"
    DAILY_MAX_LOSS = "DAILY_MAX_LOSS"
    HARD_FLATTEN_TIME = "HARD_FLATTEN_TIME"
    DAILY_PROFIT_TARGET = "DAILY_PROFIT_TARGET"
    HALTED_POSITION_OPEN = "HALTED_POSITION_OPEN"

    # -- refusals: no new entry, but the session continues ------------------
    SESSION_HALTED = "SESSION_HALTED"
    KILL_SWITCH = "KILL_SWITCH"
    UNRECONCILED = "UNRECONCILED"
    SESSION_ANCHOR_MISMATCH = "SESSION_ANCHOR_MISMATCH"
    POSITION_ALREADY_OPEN = "POSITION_ALREADY_OPEN"
    TRADE_COUNT_EXHAUSTED = "TRADE_COUNT_EXHAUSTED"
    NEAR_HARD_FLATTEN = "NEAR_HARD_FLATTEN"
    AFTER_ENTRY_CUTOFF = "AFTER_ENTRY_CUTOFF"
    BEFORE_RTH_OPEN = "BEFORE_RTH_OPEN"

    # -- all clear ----------------------------------------------------------
    OK = "OK"


@dataclass(frozen=True)
class Config:
    """Risk envelope. Defaults mirror ``.env.example`` and CLAUDE.md.

    Deliberately far inside Topstep's own limits: the firm's Daily Loss Limit
    is $1,000 and the MLL is $2,000; we stop at $250 and a $400 floor buffer.
    """

    # Dollar limits.
    daily_profit_target: float = 500.0
    daily_max_loss: float = 250.0  # positive magnitude; compared against -value
    floor_buffer: float = 400.0

    # Trade budget (CLAUDE.md strategy spec: max 2 trades per session).
    max_trades_per_session: int = 2

    # Clock policy, all wall-clock America/New_York.
    #
    # Topstep requires every position closed by 16:10 ET (15:10 CT), and their
    # risk managers BEGIN FLATTENING at 16:08 ET. Flattening at their deadline
    # is already too late, so we flatten at 15:55 -- 13 minutes before their
    # desk acts, leaving room for a slow fill or a reconnect.
    #
    # With entry_lockout_minutes = 10, 15:55 also puts the last possible entry
    # at 15:45 ET. entry_cutoff_et (11:30) is the strategy's own, stricter
    # cutoff and binds first in normal operation; the lockout is the backstop
    # that survives anyone relaxing it.
    session_boundary_et: time = time(18, 0)
    hard_flatten_et: time = time(15, 55)
    entry_cutoff_et: time = time(11, 30)
    rth_open_et: time = time(9, 30)
    entry_lockout_minutes: int = 10

    # Optional guards. Each can be switched off independently without
    # touching the four mandated trip-wires.
    enforce_rth_open: bool = True
    enforce_anchor_consistency: bool = True
    anchor_tolerance: float = 0.01

    tz: ZoneInfo = field(default_factory=lambda: ET)

    @classmethod
    def from_env(cls, **overrides: object) -> Config:
        """Build from environment variables. I/O at the edge, not in decisions."""

        def _f(name: str, default: float) -> float:
            raw = os.environ.get(name)
            return default if raw is None or raw.strip() == "" else float(raw)

        return cls(
            daily_profit_target=_f("DAILY_PROFIT_TARGET", 500.0),
            daily_max_loss=_f("DAILY_MAX_LOSS", 250.0),
            floor_buffer=_f("FLOOR_BUFFER", 400.0),
            **overrides,  # type: ignore[arg-type]
        )


def _require_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(
            f"{label} must be timezone-aware; naive datetimes are rejected, "
            "not coerced (CLAUDE.md failure mode: session-boundary bug)"
        )


def _utc(value: datetime) -> datetime:
    """Normalise to UTC before comparing or subtracting.

    Python compares and subtracts two aware datetimes that share a ``tzinfo``
    *object* in wall-clock terms, ignoring the zone entirely. ``ZoneInfo``
    instances are cached and shared, so two ET datetimes almost always hit
    that path: across the spring-forward, ``18:00 Sat`` to ``10:00 Sun`` reads
    as 16 hours when only 15 hours have elapsed.

    Every comparison and duration in this module goes through here so that
    "is it past the flatten time" and "how long until the flatten" are always
    absolute, never wall-clock.
    """
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class AccountSnapshot:
    """An immutable observation of the world at one instant.

    Built only by ``governor_adapter``. ``net_liq`` must already include open
    position P&L -- the adapter refuses to build a snapshot it cannot compute
    honestly rather than falling back to bare balance.
    """

    net_liq: float
    balance: float
    mll_floor: float
    open_position_size: int
    session_start_balance: float
    now: datetime
    kill_switch_active: bool = False

    def __post_init__(self) -> None:
        _require_aware(self.now, "AccountSnapshot.now")


@dataclass
class GovernorState:
    """The governor's own memory across evaluations.

    ``session_start_balance`` is the authoritative anchor for session P&L; the
    snapshot's copy is the adapter's observation and is cross-checked against
    this one.
    """

    session_start_balance: float
    session_high_balance: float
    trades_today: int = 0
    halted: bool = False
    halt_reason: str | None = None
    last_reconcile: datetime | None = None
    session_start: datetime | None = None

    def __post_init__(self) -> None:
        if self.last_reconcile is not None:
            _require_aware(self.last_reconcile, "GovernorState.last_reconcile")
        if self.session_start is not None:
            _require_aware(self.session_start, "GovernorState.session_start")


@dataclass(frozen=True)
class GovernorDecision:
    action: Action
    reason: str
    session_pnl: float

    @property
    def code(self) -> str:
        """The stable reason code, without the human-readable detail."""
        return self.reason.split(":", 1)[0]

    @property
    def may_enter(self) -> bool:
        return self.action is Action.CONTINUE


# ---------------------------------------------------------------------------
# Session clock
# ---------------------------------------------------------------------------


def session_start_for(now: datetime, config: Config | None = None) -> datetime:
    """Return the 18:00 ET boundary that opened the session containing ``now``.

    This is the highest-risk function in the file. A bug here silently corrupts
    every daily limit, so it is written to be obviously correct:

      * ``now`` is converted to ET first, so callers may pass any zone. Without
        this, a UTC input would be bucketed against the wrong calendar date.
      * The boundary is built with ``datetime.combine(..., tzinfo=ET)``, which
        pins a *wall-clock* 18:00 on that calendar date.
      * The "previous day" branch steps back one **calendar date**. Note that
        ``aware_dt - timedelta(days=1)`` would give the same answer here --
        timedelta arithmetic on an aware datetime is wall-clock preserving, so
        it does not drift across a DST change. The calendar form is used
        because it states the intent directly, not because the other is buggy.
      * The comparison goes through :func:`_utc`. *That* is where the real DST
        hazard lives: comparing two datetimes that share a tzinfo object is
        wall-clock, not absolute.

    18:00 ET is never ambiguous or non-existent: US transitions occur at 02:00
    local.

    The boundary is inclusive -- at exactly 18:00:00 the new session has begun.

    A naive ``now`` is rejected rather than coerced: ``astimezone`` would
    silently read it as the host's local time, which on a machine that is not
    set to ET would shift every session boundary without any error.
    """
    _require_aware(now, "session_start_for(now)")
    cfg = config or Config()
    boundary = cfg.session_boundary_et
    et_now = now.astimezone(cfg.tz)

    today_boundary = datetime.combine(et_now.date(), boundary, tzinfo=cfg.tz)
    if _utc(et_now) >= _utc(today_boundary):
        return today_boundary

    previous_date = et_now.date() - timedelta(days=1)  # calendar step, not 24h
    return datetime.combine(previous_date, boundary, tzinfo=cfg.tz)


def _session_clock_time(now: datetime, at: time, config: Config) -> datetime:
    """Resolve a wall-clock ET time within the session that contains ``now``.

    A session opens at 18:00 ET on day D and runs to 18:00 ET on day D+1, so
    every intraday time (09:30, 11:30, 15:55) falls on D+1.
    """
    session_start = session_start_for(now, config)
    target_date = session_start.date() + timedelta(days=1)
    return datetime.combine(target_date, at, tzinfo=config.tz)


def hard_flatten_at(now: datetime, config: Config | None = None) -> datetime:
    """The 15:55 ET hard flatten for the session containing ``now``."""
    cfg = config or Config()
    return _session_clock_time(now, cfg.hard_flatten_et, cfg)


def entry_cutoff_at(now: datetime, config: Config | None = None) -> datetime:
    """The 11:30 ET no-new-entries cutoff for the session containing ``now``."""
    cfg = config or Config()
    return _session_clock_time(now, cfg.entry_cutoff_et, cfg)


def rth_open_at(now: datetime, config: Config | None = None) -> datetime:
    """The 09:30 ET regular-session open for the session containing ``now``."""
    cfg = config or Config()
    return _session_clock_time(now, cfg.rth_open_et, cfg)


# ---------------------------------------------------------------------------
# Accounting
# ---------------------------------------------------------------------------


def compute_session_pnl(snapshot: AccountSnapshot, state: GovernorState) -> float:
    """Session P&L from **net liquidation**, per CLAUDE.md constraint 3.

    Using realised P&L here would miss an open losing position -- exactly the
    exposure the Max Loss Limit is enforced against.
    """
    return snapshot.net_liq - state.session_start_balance


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------


def evaluate(
    snapshot: AccountSnapshot,
    state: GovernorState,
    config: Config,
) -> GovernorDecision:
    """Decide what the caller may do. Pure: no I/O, no clock, no SDK.

    Precedence is deliberate and ordered by severity:

      1. Trip-wires -> FLATTEN_AND_HALT (most account-ending first).
      2. An already-halted session -> re-assert the flatten if anything is
         open, otherwise refuse.
      3. Entry refusals -> REFUSE_ENTRY (the session continues).
      4. CONTINUE.
    """
    _require_aware(snapshot.now, "AccountSnapshot.now")
    session_pnl = compute_session_pnl(snapshot, state)

    def decide(action: Action, code: str, detail: str) -> GovernorDecision:
        return GovernorDecision(
            action=action, reason=f"{code}: {detail}", session_pnl=session_pnl
        )

    def halt(code: str, detail: str) -> GovernorDecision:
        return decide(Action.FLATTEN_AND_HALT, code, detail)

    def refuse(code: str, detail: str) -> GovernorDecision:
        return decide(Action.REFUSE_ENTRY, code, detail)

    # --- 1. Trip-wires -----------------------------------------------------

    # 1a. Trailing MLL floor. The only permanent-failure rule, so it is checked
    #     first and against net liq including open P&L.
    headroom = snapshot.net_liq - snapshot.mll_floor
    if headroom <= config.floor_buffer:
        return halt(
            Reason.MLL_FLOOR_BUFFER,
            f"net_liq {snapshot.net_liq:,.2f} is {headroom:,.2f} above trailing "
            f"MLL floor {snapshot.mll_floor:,.2f}; buffer is "
            f"{config.floor_buffer:,.2f}",
        )

    # 1b. Session loss limit.
    if session_pnl <= -config.daily_max_loss:
        return halt(
            Reason.DAILY_MAX_LOSS,
            f"session P&L {session_pnl:,.2f} reached the "
            f"{-config.daily_max_loss:,.2f} session loss limit",
        )

    # 1c. Hard flatten clock.
    flatten_at = hard_flatten_at(snapshot.now, config)
    if _utc(snapshot.now) >= _utc(flatten_at):
        return halt(
            Reason.HARD_FLATTEN_TIME,
            f"{snapshot.now.astimezone(config.tz):%Y-%m-%d %H:%M:%S %Z} reached "
            f"the {config.hard_flatten_et:%H:%M} ET hard flatten "
            f"({flatten_at:%Y-%m-%d %H:%M:%S %Z})",
        )

    # 1d. Session profit target.
    if session_pnl >= config.daily_profit_target:
        return halt(
            Reason.DAILY_PROFIT_TARGET,
            f"session P&L {session_pnl:,.2f} reached the "
            f"{config.daily_profit_target:,.2f} session profit target",
        )

    # --- 2. Already halted -------------------------------------------------
    if state.halted:
        if snapshot.open_position_size != 0:
            return halt(
                Reason.HALTED_POSITION_OPEN,
                f"session is halted ({state.halt_reason or 'no reason recorded'}) "
                f"but position size is {snapshot.open_position_size}; "
                "flattening again",
            )
        return refuse(
            Reason.SESSION_HALTED,
            f"session is halted: {state.halt_reason or 'no reason recorded'}",
        )

    # --- 3. Entry refusals -------------------------------------------------

    # 3a. Operator kill switch. Highest-priority explicit intent.
    if snapshot.kill_switch_active:
        return refuse(Reason.KILL_SWITCH, "kill switch file is present")

    # 3b. State unknown after a restart. Never assume flat (constraint 5).
    if state.last_reconcile is None:
        return refuse(
            Reason.UNRECONCILED,
            "no reconcile recorded; positions and working orders are unknown",
        )

    # 3c. The governor's session anchor disagrees with the adapter's. Every
    #     daily limit is measured from that anchor, so a mismatch means the
    #     limits cannot be trusted.
    if config.enforce_anchor_consistency:
        drift = abs(state.session_start_balance - snapshot.session_start_balance)
        if drift > config.anchor_tolerance:
            return refuse(
                Reason.SESSION_ANCHOR_MISMATCH,
                f"governor anchor {state.session_start_balance:,.2f} disagrees "
                f"with snapshot anchor {snapshot.session_start_balance:,.2f} "
                f"by {drift:,.2f}",
            )

    # 3d. Already in the market.
    if snapshot.open_position_size != 0:
        return refuse(
            Reason.POSITION_ALREADY_OPEN,
            f"position size is {snapshot.open_position_size}, not flat",
        )

    # 3e. Trade budget spent.
    if state.trades_today >= config.max_trades_per_session:
        return refuse(
            Reason.TRADE_COUNT_EXHAUSTED,
            f"{state.trades_today} trades taken; limit is "
            f"{config.max_trades_per_session} per session",
        )

    # 3f. Too close to the hard flatten to open anything.
    remaining = _utc(flatten_at) - _utc(snapshot.now)
    lockout = timedelta(minutes=config.entry_lockout_minutes)
    if remaining < lockout:
        return refuse(
            Reason.NEAR_HARD_FLATTEN,
            f"{remaining} remains before the {config.hard_flatten_et:%H:%M} ET "
            f"hard flatten; lockout is {config.entry_lockout_minutes} minutes",
        )

    # 3g. Past the strategy's entry cutoff. Inclusive: at exactly 11:30:00 the
    #     cutoff is already in force. Entering on the cutoff instant is the
    #     riskier reading, so it is refused.
    cutoff = entry_cutoff_at(snapshot.now, config)
    if _utc(snapshot.now) >= _utc(cutoff):
        return refuse(
            Reason.AFTER_ENTRY_CUTOFF,
            f"{snapshot.now.astimezone(config.tz):%H:%M:%S %Z} is at or past the "
            f"{config.entry_cutoff_et:%H:%M} ET entry cutoff",
        )

    # 3h. Before the regular session opens. Keeps entries inside 09:30-11:30 ET
    #     and makes an overnight position structurally impossible.
    if config.enforce_rth_open:
        opens = rth_open_at(snapshot.now, config)
        if _utc(snapshot.now) < _utc(opens):
            return refuse(
                Reason.BEFORE_RTH_OPEN,
                f"{snapshot.now.astimezone(config.tz):%H:%M:%S %Z} is before the "
                f"{config.rth_open_et:%H:%M} ET regular session open",
            )

    # --- 4. All clear ------------------------------------------------------
    return decide(
        Action.CONTINUE,
        Reason.OK,
        f"within all limits; session P&L {session_pnl:,.2f}, "
        f"{headroom:,.2f} above the MLL floor",
    )


# ---------------------------------------------------------------------------
# Pure state transitions
# ---------------------------------------------------------------------------


def roll_session(
    snapshot: AccountSnapshot,
    state: GovernorState,
    config: Config,
) -> GovernorState:
    """Return state re-anchored to the session containing ``snapshot.now``.

    Pure -- returns a new state, never mutates. Apply before ``evaluate`` so a
    process that runs across the 18:00 ET boundary resets its trade count and
    clears a previous session's halt.

    The new anchor is **net liq**, not balance: anchoring to balance while
    measuring P&L against net liq would book an open position's unrealised P&L
    as phantom session P&L the moment the session rolled.
    """
    current = session_start_for(snapshot.now, config)
    if state.session_start == current:
        return state
    return GovernorState(
        session_start_balance=snapshot.net_liq,
        session_high_balance=snapshot.net_liq,
        trades_today=0,
        halted=False,
        halt_reason=None,
        last_reconcile=state.last_reconcile,  # reconcile is process-scoped
        session_start=current,
    )


def record_trade(state: GovernorState) -> GovernorState:
    """Return state with the session trade counter incremented."""
    return replace(state, trades_today=state.trades_today + 1)


def update_high_balance(
    snapshot: AccountSnapshot, state: GovernorState
) -> GovernorState:
    """Track the session high-water mark on net liq."""
    if snapshot.net_liq <= state.session_high_balance:
        return state
    return replace(state, session_high_balance=snapshot.net_liq)


def apply_decision(
    state: GovernorState, decision: GovernorDecision
) -> GovernorState:
    """Return state with a halt recorded if the decision demands one."""
    if decision.action is not Action.FLATTEN_AND_HALT:
        return state
    return replace(state, halted=True, halt_reason=decision.reason)
