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

CALLER CONTRACT -- READ THIS BEFORE WIRING THE STRATEGY
-------------------------------------------------------
The governor decides; it cannot maintain its own state. Every function below is
pure, so the caller MUST perform these steps, in this order, on every cycle:

  1. ``snapshot = await adapter.build_snapshot(...)``
     Supplies ``now``, net liq, the MLL floor and the observed trade count.

  2. ``state = roll_session(snapshot, state, config)``
     MISS THIS and the session anchor goes stale after the 18:00 ET roll.
     Historically this was the worst bug available here: a stale anchor makes
     ``session_pnl`` measure from the wrong day, so a real -$250 session can
     read as +$150 and the loss limit never fires. It is now caught -- see
     ``STALE_SESSION_ANCHOR`` -- but the check exists to make the mistake
     loud, not to make skipping step 2 acceptable.

  3. ``decision = evaluate(snapshot, state, config)``

  4. ``state = apply_decision(state, decision)``
     MISS THIS and a halt is not recorded, so the next cycle re-evaluates from
     scratch and may resume trading after the session was supposed to be over.

  5. On a fill: ``state = record_trade(state)``
     MISS THIS and ``trades_today`` never increments, so the 2-trade budget
     never binds. Also now detectable: the adapter counts real entries and
     ``TRADE_COUNT_DRIFT`` refuses entry when the two disagree.

  6. Persist ``state`` across restarts, and set ``last_reconcile`` only after
     actually querying live positions and working orders (constraint 5).

Steps 2, 4 and 5 are the ones that fail silently if forgotten, which is why
each has a corresponding detection path. Detection is a backstop; it is not a
substitute for calling them.

ACCOUNTING RULES (CLAUDE.md, design constraints 3 and 4)
--------------------------------------------------------
  * Session P&L is measured against **net liquidation**, never realised P&L.
    The firm enforces the Max Loss Limit in real time on net liq including
    open positions, so any check against realised P&L is wrong by definition.
  * The session boundary is 18:00 America/New_York, never midnight. All daily
    accounting resets there. See ``session_start_for``.

All datetimes must be timezone-aware AND correctly localised. Those are not the
same requirement -- see ``_require_aware``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from typing import Final
from zoneinfo import ZoneInfo

from compliance import validate_daily_target
from contracts import front_month, is_expiry_date
from market_calendar import DayStatus, classify

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
    "session_trading_date",
    "hard_flatten_at",
    "entry_cutoff_at",
    "rth_open_at",
    "compute_session_pnl",
    "effective_trade_count",
    "evaluate",
    "roll_session",
    "record_trade",
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

    # -- state integrity: halt, because the numbers cannot be trusted --------
    STALE_SESSION_ANCHOR = "STALE_SESSION_ANCHOR"
    MLL_STATE_UNAVAILABLE = "MLL_STATE_UNAVAILABLE"

    # -- trip-wires: flatten and halt for the session ----------------------
    MLL_FLOOR_BUFFER = "MLL_FLOOR_BUFFER"
    DAILY_MAX_LOSS = "DAILY_MAX_LOSS"
    HARD_FLATTEN_TIME = "HARD_FLATTEN_TIME"
    DAILY_PROFIT_TARGET = "DAILY_PROFIT_TARGET"
    HALTED_POSITION_OPEN = "HALTED_POSITION_OPEN"

    # -- refusals: no new entry, but the session continues ------------------
    SESSION_HALTED = "SESSION_HALTED"
    KILL_SWITCH = "KILL_SWITCH"
    MARKET_CLOSED = "MARKET_CLOSED"
    CONTRACT_EXPIRY = "CONTRACT_EXPIRY"
    UNRECONCILED = "UNRECONCILED"
    SESSION_ANCHOR_MISMATCH = "SESSION_ANCHOR_MISMATCH"
    TRADE_COUNT_DRIFT = "TRADE_COUNT_DRIFT"
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

    Every field is validated in ``__post_init__``. A risk limit that is merely
    wrong fails closed (we stop too early); a *negative* one fails OPEN, which
    is why they are rejected outright rather than clamped.
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
    enforce_market_calendar: bool = True
    enforce_consistency_ceiling: bool = True
    enforce_contract_expiry: bool = True
    anchor_tolerance: float = 0.01

    tz: ZoneInfo = field(default_factory=lambda: ET)

    def __post_init__(self) -> None:
        def reject(field_name: str, value: object, requirement: str) -> None:
            raise ValueError(
                f"Config.{field_name} is {value!r} but must be {requirement}. "
                "A risk limit outside its valid range does not merely misbehave, "
                "it can disable the guard entirely."
            )

        if self.floor_buffer < 0:
            # The dangerous one. `headroom <= floor_buffer` with a negative
            # buffer only fires once net liq is ALREADY below the floor, i.e.
            # after the account is gone. Every other bad value fails closed.
            reject("floor_buffer", self.floor_buffer, "zero or positive")
        if self.daily_max_loss <= 0:
            reject("daily_max_loss", self.daily_max_loss, "a positive magnitude")
        if self.daily_profit_target <= 0:
            reject("daily_profit_target", self.daily_profit_target, "positive")
        if self.max_trades_per_session < 0:
            reject("max_trades_per_session", self.max_trades_per_session,
                   "zero or positive")
        if self.entry_lockout_minutes < 0:
            reject("entry_lockout_minutes", self.entry_lockout_minutes,
                   "zero or positive")
        if self.anchor_tolerance < 0:
            reject("anchor_tolerance", self.anchor_tolerance, "zero or positive")

        # The daily target and the Combine consistency rule are not independent
        # settings, and nothing else ties them together. A daily cap above 55%
        # of the profit target makes it possible to fail the Combine on a
        # WINNING day -- the one failure mode no risk check will ever catch,
        # because risk checks only look downward. See compliance.py.
        if self.enforce_consistency_ceiling:
            validate_daily_target(self.daily_profit_target)

    @classmethod
    def from_env(cls, **overrides: object) -> Config:
        """Build from environment variables. I/O at the edge, not in decisions.

        An unparseable value raises with the variable named. A bare ``float()``
        traceback here would be read as a config typo when it is actually a
        disabled risk limit.
        """

        def _f(name: str, default: float) -> float:
            raw = os.environ.get(name)
            if raw is None or raw.strip() == "":
                return default
            try:
                return float(raw)
            except ValueError as exc:
                raise ValueError(
                    f"environment variable {name}={raw!r} is not a number. "
                    "Fix .env before starting: the governor will not guess a "
                    "risk limit."
                ) from exc

        return cls(
            daily_profit_target=_f("DAILY_PROFIT_TARGET", 500.0),
            daily_max_loss=_f("DAILY_MAX_LOSS", 250.0),
            floor_buffer=_f("FLOOR_BUFFER", 400.0),
            **overrides,  # type: ignore[arg-type]
        )


def _require_aware(value: datetime, label: str) -> None:
    """Reject naive datetimes.

    Note what this CANNOT catch: ``datetime.now().replace(tzinfo=ET)`` is
    aware, passes this check, and is wrong by the host's UTC offset. Aware is
    necessary but not sufficient -- correctly *localised* is the real
    requirement, and it cannot be verified from the value alone.

    That is enforced upstream instead: ``governor_adapter.now_utc`` is the only
    sanctioned clock read, and a test fails the build if ``.replace(tzinfo=``
    appears anywhere in ``src/``.
    """
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

    ``mll_floor`` is ``None`` when the tracked floor is missing, stale or
    unparseable. That is a halt, never a default.

    ``observed_trades`` is the count of real entries the adapter found in the
    fill history since the session anchor, or ``None`` if it could not be
    determined. It exists to catch a caller that forgot ``record_trade``.
    """

    net_liq: float
    balance: float
    mll_floor: float | None
    open_position_size: int
    session_start_balance: float
    now: datetime
    kill_switch_active: bool = False
    observed_trades: int | None = None

    def __post_init__(self) -> None:
        _require_aware(self.now, "AccountSnapshot.now")


@dataclass
class GovernorState:
    """The governor's own memory across evaluations.

    ``session_start_balance`` is the authoritative anchor for session P&L; the
    snapshot's copy is the adapter's observation and is cross-checked against
    this one.

    ``session_start`` is what makes staleness detectable. It must equal the
    18:00 ET boundary of the session the snapshot belongs to.
    """

    session_start_balance: float
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


def session_trading_date(now: datetime, config: Config | None = None) -> date:
    """The calendar date a session belongs to.

    A session opens 18:00 ET on day D and runs to 18:00 ET on day D+1, so its
    trading date is D+1. Sunday 18:00 therefore belongs to Monday, and Friday
    18:00 would belong to Saturday -- which the calendar correctly reports as
    closed, because Globex is shut from Friday 17:00 to Sunday 18:00 ET.
    """
    return session_start_for(now, config).date() + timedelta(days=1)


def _session_clock_time(now: datetime, at: time, config: Config) -> datetime:
    """Resolve a wall-clock ET time within the session that contains ``now``."""
    return datetime.combine(session_trading_date(now, config), at, tzinfo=config.tz)


def hard_flatten_at(now: datetime, config: Config | None = None) -> datetime:
    """The 15:55 ET hard flatten for the session containing ``now``.

    Always 15:55. There is deliberately no early-close arithmetic: we do not
    trade holiday dates at all (DECISIONS.md), so a shortened session never
    needs a computed deadline. The half-day close times are disputed between
    sources and Topstep announces its own by Discord, so computing from them
    would be building on a contested number. Standing aside removes the need.

    If those times are ever verified, that does NOT by itself reopen trading on
    those dates -- that was a separate decision, made on its own grounds.
    """
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

    Meaningless unless ``state.session_start`` matches the snapshot's session.
    ``evaluate`` checks that first and halts if it does not.
    """
    return snapshot.net_liq - state.session_start_balance


def effective_trade_count(snapshot: AccountSnapshot, state: GovernorState) -> int:
    """The trade count to budget against: the HIGHER of tracked and observed.

    Taking the maximum is the conservative direction. If the caller forgot
    ``record_trade``, the observed count is higher and we stop sooner; if the
    fill history is incomplete, the tracked count is higher and we still stop
    sooner. Only a number that is too LOW can let a third trade through.
    """
    if snapshot.observed_trades is None:
        return state.trades_today
    return max(state.trades_today, snapshot.observed_trades)


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

      0. State integrity -> FLATTEN_AND_HALT. Checked FIRST, because a stale
         anchor or an unknown MLL floor makes every number below meaningless.
         A trip-wire computed from a bad anchor is not a safety check, it is a
         confident wrong answer.
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

    # --- 0. State integrity ------------------------------------------------

    # 0a. The session anchor must belong to the session we are evaluating.
    #     If it does not, session_pnl measures from the wrong day: a session
    #     that is really down $250 can read as up $150, and BOTH the loss limit
    #     and the profit target silently stop working. This must therefore be
    #     checked before any limit derived from session_pnl.
    expected_session = session_start_for(snapshot.now, config)
    if state.session_start is None:
        return halt(
            Reason.STALE_SESSION_ANCHOR,
            f"no session anchor recorded; session P&L cannot be trusted "
            f"(expected anchor {expected_session:%Y-%m-%d %H:%M %Z}). "
            "Call roll_session before evaluate.",
        )
    if _utc(state.session_start) != _utc(expected_session):
        return halt(
            Reason.STALE_SESSION_ANCHOR,
            f"anchor is {state.session_start:%Y-%m-%d %H:%M %Z} but this snapshot "
            f"belongs to the session starting {expected_session:%Y-%m-%d %H:%M %Z}; "
            f"the {session_pnl:,.2f} session P&L is measured from the wrong "
            "session. Call roll_session before evaluate.",
        )

    # 0b. The MLL floor is the only permanent-failure guard. Unknown is a halt.
    if snapshot.mll_floor is None:
        return halt(
            Reason.MLL_STATE_UNAVAILABLE,
            "the trailing MLL floor is unknown (state missing, stale or "
            "unparseable); refusing to trade without the one guard that "
            "protects against permanent failure",
        )

    # --- 1. Trip-wires -----------------------------------------------------

    # 1a. Trailing MLL floor, against net liq including open P&L.
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
            f"the hard flatten ({flatten_at:%Y-%m-%d %H:%M:%S %Z})",
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

    # 3b. Is there a session at all today?
    if config.enforce_market_calendar:
        trading_date = session_trading_date(snapshot.now, config)
        day = classify(trading_date)
        if not day.tradable:
            return refuse(
                Reason.MARKET_CLOSED,
                f"{trading_date} is not a trading day ({day.label})",
            )

    # 3c. Expiry day. MNQ settles to the index's OPENING quote on the third
    #     Friday, so the session that decides the contract is over before our
    #     09:30 window begins and what remains is an artefact. Four sessions a
    #     year, and it removes the need to reason about any of it.
    if config.enforce_contract_expiry:
        trading_date = session_trading_date(snapshot.now, config)
        if is_expiry_date(trading_date):
            return refuse(
                Reason.CONTRACT_EXPIRY,
                f"{trading_date} is a quarterly expiry; MNQ cash-settles to the "
                f"opening quote and the front month is now "
                f"{front_month(trading_date).symbol()}",
            )

    # 3d. State unknown after a restart. Never assume flat (constraint 5).
    if state.last_reconcile is None:
        return refuse(
            Reason.UNRECONCILED,
            "no reconcile recorded; positions and working orders are unknown",
        )

    # 3d. The governor's session anchor disagrees with the adapter's. Note this
    #     is a DIFFERENT failure from 0a: two stale anchors agree with each
    #     other, which is why 0a exists as well.
    if config.enforce_anchor_consistency:
        drift = abs(state.session_start_balance - snapshot.session_start_balance)
        if drift > config.anchor_tolerance:
            return refuse(
                Reason.SESSION_ANCHOR_MISMATCH,
                f"governor anchor {state.session_start_balance:,.2f} disagrees "
                f"with snapshot anchor {snapshot.session_start_balance:,.2f} "
                f"by {drift:,.2f}",
            )

    # 3e. The tracked trade count disagrees with observed fills, which means
    #     record_trade was missed. We budget against the higher number anyway
    #     (see effective_trade_count), but the disagreement is reported rather
    #     than quietly absorbed.
    if snapshot.observed_trades is not None and (
        snapshot.observed_trades > state.trades_today
    ):
        return refuse(
            Reason.TRADE_COUNT_DRIFT,
            f"{snapshot.observed_trades} entries observed in the fill history "
            f"but state records {state.trades_today}; record_trade was missed. "
            f"Budgeting against {snapshot.observed_trades}",
        )

    # 3f. Already in the market.
    if snapshot.open_position_size != 0:
        return refuse(
            Reason.POSITION_ALREADY_OPEN,
            f"position size is {snapshot.open_position_size}, not flat",
        )

    # 3g. Trade budget spent, measured conservatively.
    taken = effective_trade_count(snapshot, state)
    if taken >= config.max_trades_per_session:
        return refuse(
            Reason.TRADE_COUNT_EXHAUSTED,
            f"{taken} trades taken; limit is "
            f"{config.max_trades_per_session} per session",
        )

    # 3h. Too close to the hard flatten to open anything.
    remaining = _utc(flatten_at) - _utc(snapshot.now)
    lockout = timedelta(minutes=config.entry_lockout_minutes)
    if remaining < lockout:
        return refuse(
            Reason.NEAR_HARD_FLATTEN,
            f"{remaining} remains before the hard flatten at "
            f"{flatten_at:%H:%M} ET; lockout is "
            f"{config.entry_lockout_minutes} minutes",
        )

    # 3i. Past the strategy's entry cutoff. Inclusive: at exactly 11:30:00 the
    #     cutoff is already in force. Entering on the cutoff instant is the
    #     riskier reading, so it is refused.
    cutoff = entry_cutoff_at(snapshot.now, config)
    if _utc(snapshot.now) >= _utc(cutoff):
        return refuse(
            Reason.AFTER_ENTRY_CUTOFF,
            f"{snapshot.now.astimezone(config.tz):%H:%M:%S %Z} is at or past the "
            f"{config.entry_cutoff_et:%H:%M} ET entry cutoff",
        )

    # 3j. Before the regular session opens. Keeps entries inside 09:30-11:30 ET
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
    if state.session_start is not None and _utc(state.session_start) == _utc(current):
        return state
    return GovernorState(
        session_start_balance=snapshot.net_liq,
        trades_today=0,
        halted=False,
        halt_reason=None,
        last_reconcile=state.last_reconcile,  # reconcile is process-scoped
        session_start=current,
    )


def record_trade(state: GovernorState) -> GovernorState:
    """Return state with the session trade counter incremented."""
    return replace(state, trades_today=state.trades_today + 1)


def apply_decision(
    state: GovernorState, decision: GovernorDecision
) -> GovernorState:
    """Return state with a halt recorded if the decision demands one."""
    if decision.action is not Action.FLATTEN_AND_HALT:
        return state
    return replace(state, halted=True, halt_reason=decision.reason)
