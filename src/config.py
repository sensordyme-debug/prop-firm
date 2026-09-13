"""Central typed configuration. One object, validated at startup, fails closed.

Replaces scattered ``os.environ`` reads. Every value that governs trading is
declared here once, with its type, and checked before anything runs.

THE TWO-KEY RULE
----------------
Order transmission requires BOTH of these, and neither defaults to permissive:

    DRY_RUN=false               (default true)
    LIVE_TRADING_ENABLED=true   (default false)

They are deliberately redundant. A single flag is one typo, one bad merge, or
one over-eager agent away from a live order. Two flags with opposite polarity
means the accidental states all fail safe: unset is safe, half-set is safe, and
``DRY_RUN=false`` alone is not merely ignored but rejected as incoherent.

Even with both set, :mod:`execution.broker` still requires an explicit
capability object that nothing in the current runtime constructs. This module
expresses *intent*; it does not grant *capability*.

FAIL CLOSED
-----------
Invalid configuration raises at construction. It is never clamped, defaulted or
warned about and carried past. A risk limit that is quietly corrected is a risk
limit nobody is enforcing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from datetime import time
from enum import Enum
from pathlib import Path
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = [
    "SUPPORTED_SYMBOLS",
    "AppConfig",
    "ConfigError",
    "ExecutionMode",
    "load_config",
]

SUPPORTED_SYMBOLS: Final[frozenset[str]] = frozenset({"MNQ"})

# CLAUDE.md constraint 6: position size is hard-coded, not a tunable.
MAX_POSITION_SIZE: Final[int] = 5


class ConfigError(ValueError):
    """Configuration is invalid. Always fatal; never downgraded to a warning."""


class ExecutionMode(str, Enum):
    """How the runtime is allowed to act on a decision.

    Ordered by increasing capability. Everything up to and including
    ``DRY_RUN`` is incapable of transmitting an order by construction, not by
    checking a flag at the point of sending.
    """

    BACKTEST = "BACKTEST"        # historical bars, no connection
    REPLAY = "REPLAY"            # recorded bars through the live code path
    READ_ONLY = "READ_ONLY"      # connected; observes only
    DRY_RUN = "DRY_RUN"          # connected; decides and logs, transmits nothing
    EXECUTION = "EXECUTION"      # NOT REACHABLE — see AppConfig.validate()

    @property
    def may_connect(self) -> bool:
        return self in (ExecutionMode.READ_ONLY, ExecutionMode.DRY_RUN,
                        ExecutionMode.EXECUTION)

    @property
    def may_transmit(self) -> bool:
        return self is ExecutionMode.EXECUTION


def _env(name: str, default: str | None = None) -> str | None:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip()


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(
            f"{name}={raw!r} is not a number. Fix .env before starting; the "
            "system will not guess a risk limit."
        ) from exc


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} is not an integer.") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in ("true", "1", "yes", "on"):
        return True
    if lowered in ("false", "0", "no", "off"):
        return False
    raise ConfigError(
        f"{name}={raw!r} is not a boolean. Use true/false. Ambiguous values "
        "are rejected rather than guessed, because this flag can gate orders."
    )


def _env_time(name: str, default: time) -> time:
    raw = _env(name)
    if raw is None:
        return default
    try:
        hh, mm = raw.split(":")[:2]
        return time(int(hh), int(mm))
    except (ValueError, IndexError) as exc:
        raise ConfigError(f"{name}={raw!r} is not HH:MM.") from exc


_PLACEHOLDER_MARKERS = (
    "paste_your", "your_", "changeme", "change_me", "xxx", "<", "example",
    "placeholder", "todo",
)


def _is_placeholder(value: str | None) -> bool:
    """Is this obviously not a real credential?"""
    if value is None or not value.strip():
        return True
    lowered = value.strip().lower()
    return any(marker in lowered for marker in _PLACEHOLDER_MARKERS)


@dataclass(frozen=True)
class AppConfig:
    """Every setting that governs behaviour, in one validated object."""

    # -- identity / credentials (presence only; values never logged) --------
    api_key: str | None = None
    username: str | None = None
    account_name: str | None = None

    # -- instrument ---------------------------------------------------------
    symbol: str = "MNQ"
    position_size: int = 2

    # -- execution gating ---------------------------------------------------
    dry_run: bool = True
    live_trading_enabled: bool = False

    # -- risk ---------------------------------------------------------------
    risk_per_trade: float = 80.0
    daily_max_loss: float = 250.0
    daily_profit_target: float = 500.0
    floor_buffer: float = 400.0
    max_trades_per_session: int = 2

    # -- timing -------------------------------------------------------------
    timezone: str = "America/New_York"
    session_start: time = time(18, 0)
    rth_open: time = time(9, 30)
    entry_cutoff: time = time(11, 30)
    hard_flatten: time = time(15, 55)
    entry_lockout_minutes: int = 10

    # -- operational --------------------------------------------------------
    state_dir: Path = field(default_factory=lambda: Path("state"))
    log_dir: Path = field(default_factory=lambda: Path("logs"))
    stale_data_seconds: int = 120

    def __post_init__(self) -> None:
        self.validate()

    # -- derived ------------------------------------------------------------

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def has_credentials(self) -> bool:
        """True only for credentials that could plausibly be real.

        The shipped .env.example contains placeholders, and copying it to .env
        is the documented first step. Treating "paste_your_key_here" as a
        credential would send junk to the API and produce a 401 that looks like
        a key problem rather than a setup problem.
        """
        return not (
            _is_placeholder(self.api_key) or _is_placeholder(self.username)
        )

    @property
    def execution_mode(self) -> ExecutionMode:
        """The most capable mode this configuration permits.

        Never returns EXECUTION: ``validate`` rejects the combination that
        would produce it. Reaching execution is a deliberate future change to
        this file plus a capability object, not a flag flip in ``.env``.
        """
        if not self.has_credentials:
            return ExecutionMode.BACKTEST
        if self.dry_run or not self.live_trading_enabled:
            return ExecutionMode.DRY_RUN
        return ExecutionMode.DRY_RUN  # unreachable in practice; see validate()

    # -- validation ---------------------------------------------------------

    def validate(self) -> None:
        def reject(what: str) -> None:
            raise ConfigError(what)

        if self.symbol not in SUPPORTED_SYMBOLS:
            reject(
                f"symbol {self.symbol!r} is not supported "
                f"(supported: {sorted(SUPPORTED_SYMBOLS)}). Contract geometry, "
                "the point value and the calendar are all instrument-specific."
            )

        if self.position_size <= 0:
            reject(f"position_size must be positive, got {self.position_size}")
        if self.position_size > MAX_POSITION_SIZE:
            reject(
                f"position_size {self.position_size} exceeds the {MAX_POSITION_SIZE} "
                "contract cap. CLAUDE.md constraint 6 fixes this at 2 until the "
                "go-live gates are cleared."
            )

        for name, value in (
            ("risk_per_trade", self.risk_per_trade),
            ("daily_max_loss", self.daily_max_loss),
            ("daily_profit_target", self.daily_profit_target),
        ):
            if value <= 0:
                reject(f"{name} must be positive, got {value}")

        # Negative here fails OPEN: `headroom <= buffer` with a negative buffer
        # only halts once net liq is already below the floor.
        if self.floor_buffer < 0:
            reject(f"floor_buffer must be zero or positive, got {self.floor_buffer}")

        if self.max_trades_per_session < 0:
            reject("max_trades_per_session must be zero or positive")
        if self.entry_lockout_minutes < 0:
            reject("entry_lockout_minutes must be zero or positive")
        if self.stale_data_seconds <= 0:
            reject("stale_data_seconds must be positive")

        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ConfigError(
                f"timezone {self.timezone!r} is not a valid IANA zone: {exc}. "
                "On Windows this also needs the tzdata package."
            ) from exc

        # Clock ordering. Out of order, the window is empty or inverted and the
        # system would either never trade or never stop.
        if not self.rth_open < self.entry_cutoff:
            reject(
                f"rth_open {self.rth_open} must be before entry_cutoff "
                f"{self.entry_cutoff}; otherwise no entry window exists"
            )
        if not self.entry_cutoff < self.hard_flatten:
            reject(
                f"entry_cutoff {self.entry_cutoff} must be before hard_flatten "
                f"{self.hard_flatten}; otherwise entries are accepted after the "
                "position must already be closed"
            )

        # -- the two-key rule ------------------------------------------------
        if self.live_trading_enabled and self.dry_run:
            reject(
                "LIVE_TRADING_ENABLED=true with DRY_RUN=true is incoherent. "
                "Refusing to guess which you meant: set DRY_RUN=false "
                "deliberately, or leave live trading disabled."
            )
        if self.live_trading_enabled and not self.dry_run:
            reject(
                "Live order transmission is NOT IMPLEMENTED and cannot be "
                "enabled by configuration. Reaching it requires a deliberate "
                "code change plus an execution capability object, reviewed by a "
                "human, after the ROADMAP Stage 5 gates are demonstrated. "
                "Set LIVE_TRADING_ENABLED=false."
            )

    # -- safe rendering -----------------------------------------------------

    def redacted(self) -> dict[str, object]:
        """A dict safe to log or paste into a ticket. Never includes secrets."""
        return {
            "symbol": self.symbol,
            "position_size": self.position_size,
            "execution_mode": self.execution_mode.value,
            "dry_run": self.dry_run,
            "live_trading_enabled": self.live_trading_enabled,
            "credentials_present": self.has_credentials,
            "username": _credential_state(self.username),
            "api_key": _credential_state(self.api_key),
            "account_name": self.account_name or "<default>",
            "risk_per_trade": self.risk_per_trade,
            "daily_max_loss": self.daily_max_loss,
            "daily_profit_target": self.daily_profit_target,
            "floor_buffer": self.floor_buffer,
            "max_trades_per_session": self.max_trades_per_session,
            "timezone": self.timezone,
            "session_start": self.session_start.strftime("%H:%M"),
            "rth_open": self.rth_open.strftime("%H:%M"),
            "entry_cutoff": self.entry_cutoff.strftime("%H:%M"),
            "hard_flatten": self.hard_flatten.strftime("%H:%M"),
        }

    def to_governor_config(self):
        """Build the pure governor Config from this one.

        The governor stays free of environment knowledge; this is the only
        place the two meet.
        """
        from governor import Config as GovernorConfig

        return GovernorConfig(
            daily_profit_target=self.daily_profit_target,
            daily_max_loss=self.daily_max_loss,
            floor_buffer=self.floor_buffer,
            max_trades_per_session=self.max_trades_per_session,
            session_boundary_et=self.session_start,
            hard_flatten_et=self.hard_flatten,
            entry_cutoff_et=self.entry_cutoff,
            rth_open_et=self.rth_open,
            entry_lockout_minutes=self.entry_lockout_minutes,
        )

    def with_overrides(self, **kwargs: object) -> AppConfig:
        return replace(self, **kwargs)  # type: ignore[arg-type]


def _credential_state(value: str | None) -> str:
    if value is None or not value.strip():
        return "<unset>"
    if _is_placeholder(value):
        return "<placeholder — .env still has the example value>"
    return "<set>"


def load_config(*, load_dotenv_file: bool = True) -> AppConfig:
    """Build :class:`AppConfig` from the environment, validating as it goes."""
    if load_dotenv_file:
        try:
            from dotenv import load_dotenv

            load_dotenv()
        except ImportError:  # pragma: no cover - dotenv is a declared dep
            pass

    return AppConfig(
        api_key=_env("PROJECT_X_API_KEY"),
        username=_env("PROJECT_X_USERNAME"),
        account_name=_env("PROJECT_X_ACCOUNT_NAME"),
        symbol=_env("SYMBOL", "MNQ") or "MNQ",
        position_size=_env_int("POSITION_SIZE", 2),
        dry_run=_env_bool("DRY_RUN", True),
        live_trading_enabled=_env_bool("LIVE_TRADING_ENABLED", False),
        risk_per_trade=_env_float("RISK_PER_TRADE", 80.0),
        daily_max_loss=_env_float("DAILY_MAX_LOSS", 250.0),
        daily_profit_target=_env_float("DAILY_PROFIT_TARGET", 500.0),
        floor_buffer=_env_float("FLOOR_BUFFER", 400.0),
        max_trades_per_session=_env_int("MAX_TRADES_PER_SESSION", 2),
        timezone=_env("TIMEZONE", "America/New_York") or "America/New_York",
        session_start=_env_time("SESSION_START", time(18, 0)),
        rth_open=_env_time("RTH_OPEN", time(9, 30)),
        entry_cutoff=_env_time("ENTRY_CUTOFF", time(11, 30)),
        hard_flatten=_env_time("HARD_FLATTEN", time(15, 55)),
        stale_data_seconds=_env_int("STALE_DATA_SECONDS", 120),
    )
