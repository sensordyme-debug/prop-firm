"""Readiness gate. Reports API-KEY-READY or NOT READY, and never more.

THE ONE RULE
------------
This never reports ready because a key is present. A key proves someone pasted
a string into a file; it proves nothing about whether the account can be read,
whether the data is intelligible, or whether the risk layer works.

The maximum classification this module can EVER emit is ``API-KEY-READY``.
``LIVE-TRADING-READY`` is not a value it can return -- there is no code path
that produces it, because order transmission is not implemented.

Checks that require a live connection report ``NOT TESTED`` rather than passing
or failing. "Not tested" is the honest answer before credentials exist, and
silently treating it as a pass is exactly the failure this gate is for.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

__all__ = ["Check", "CheckStatus", "ReadinessReport", "assess"]


class CheckStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_TESTED = "NOT TESTED"
    DISABLED = "DISABLED"
    NOT_VALIDATED = "NOT VALIDATED"
    NOT_AVAILABLE = "NOT AVAILABLE"

    @property
    def blocks_readiness(self) -> bool:
        """Only an outright FAIL blocks. NOT TESTED is expected before a key."""
        return self is CheckStatus.FAIL


@dataclass(frozen=True)
class Check:
    name: str
    status: CheckStatus
    detail: str = ""


@dataclass(frozen=True)
class ReadinessReport:
    checks: tuple[Check, ...]

    @property
    def blocking(self) -> tuple[Check, ...]:
        return tuple(c for c in self.checks if c.status.blocks_readiness)

    @property
    def classification(self) -> str:
        """NOT READY or API-KEY-READY. Never LIVE-TRADING-READY."""
        return "NOT READY" if self.blocking else "API-KEY-READY"

    def render(self) -> str:
        width = max(len(c.name) for c in self.checks) + 2
        lines = ["=" * 66, "READINESS REPORT", "=" * 66, ""]
        for c in self.checks:
            lines.append(f"  {c.name:<{width}s} {c.status.value}")
            if c.detail:
                lines.append(f"  {'':<{width}s}   {c.detail}")
        lines += ["", "-" * 66]
        lines.append(f"  FINAL STATUS: {self.classification}")
        lines.append("-" * 66)
        if self.blocking:
            lines.append("")
            lines.append("  Blocking:")
            for c in self.blocking:
                lines.append(f"    {c.name}: {c.detail}")
        lines += [
            "",
            "  API-KEY-READY means: credentials can be added safely and",
            "  read-only validation can begin. It does NOT mean the system",
            "  may trade. Order transmission is not implemented, no strategy",
            "  has been validated on real data, and no gate beyond engine",
            "  validation has been demonstrated.",
        ]
        return "\n".join(lines)

    def to_dict(self) -> dict[str, object]:
        return {
            "checks": [
                {"name": c.name, "status": c.status.value, "detail": c.detail}
                for c in self.checks
            ],
            "classification": self.classification,
            "live_trading_ready": False,
        }


def _run_tests(root: Path) -> Check:
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
             "--tb=no"],
            cwd=str(root), capture_output=True, text=True, timeout=600, check=False,
        )
    except Exception as exc:
        return Check("TESTS", CheckStatus.FAIL, f"could not run pytest: {exc}")

    tail = [ln for ln in proc.stdout.strip().splitlines() if "passed" in ln
            or "failed" in ln]
    summary = tail[-1].strip() if tail else "no summary"
    if proc.returncode == 0:
        return Check("TESTS", CheckStatus.PASS, summary)
    return Check("TESTS", CheckStatus.FAIL, summary)


def assess(
    *,
    root: Path | None = None,
    run_tests: bool = True,
    config: object | None = None,
) -> ReadinessReport:
    """Build the readiness report. Live-dependent checks report NOT TESTED."""
    base = root or Path(__file__).resolve().parent.parent
    checks: list[Check] = []

    # --- repository -------------------------------------------------------
    required = [
        "src/governor.py", "src/backtest.py", "src/config.py",
        "src/execution/broker.py", "src/brokers/projectx.py",
        "src/strategies/base.py", "src/connection.py",
        "src/execution/protection.py", "src/reconciliation.py",
        "docs/PROJECTX_API.md", "FIRM_RULES.md",
    ]
    missing = [p for p in required if not (base / p).exists()]
    checks.append(Check(
        "REPOSITORY",
        CheckStatus.FAIL if missing else CheckStatus.PASS,
        f"missing: {missing}" if missing else f"{len(required)} core modules present",
    ))

    # --- tests ------------------------------------------------------------
    checks.append(
        _run_tests(base) if run_tests
        else Check("TESTS", CheckStatus.NOT_TESTED, "skipped by request")
    )

    # --- configuration ----------------------------------------------------
    try:
        from config import load_config

        cfg = config if config is not None else load_config()
        checks.append(Check(
            "CONFIGURATION", CheckStatus.PASS,
            f"validated; mode={cfg.execution_mode.value}",  # type: ignore[attr-defined]
        ))
        has_creds = bool(cfg.has_credentials)  # type: ignore[attr-defined]
        checks.append(Check(
            "PROJECTX CONFIGURATION",
            CheckStatus.PASS if has_creds else CheckStatus.NOT_TESTED,
            "credentials present (value never read here)" if has_creds
            else "no credentials yet -- expected at this stage",
        ))
        dry = bool(cfg.dry_run)          # type: ignore[attr-defined]
        live = bool(cfg.live_trading_enabled)  # type: ignore[attr-defined]
        checks.append(Check(
            "EXECUTION GATING",
            CheckStatus.PASS if (dry and not live) else CheckStatus.FAIL,
            f"DRY_RUN={dry}  LIVE_TRADING_ENABLED={live}",
        ))
    except Exception as exc:
        checks.append(Check("CONFIGURATION", CheckStatus.FAIL, str(exc)[:160]))
        checks.append(Check("PROJECTX CONFIGURATION", CheckStatus.FAIL,
                            "configuration invalid"))
        checks.append(Check("EXECUTION GATING", CheckStatus.FAIL,
                            "configuration invalid"))

    # --- live-dependent: honest NOT TESTED --------------------------------
    for name, detail in (
        ("PROJECTX READ ACCESS", "needs credentials; run connection-test"),
        ("MARKET DATA", "no validated dataset exists yet"),
        ("DATA VALIDATION", "needs real bars"),
        ("TIMESTAMP VALIDATION",
         "UNVERIFIED: open- vs close-labelled is unknown; BarSeries fails closed"),
        ("ACCOUNT RECONCILIATION", "needs a live account"),
    ):
        checks.append(Check(name, CheckStatus.NOT_TESTED, detail))

    # --- things we can assert offline -------------------------------------
    try:
        from execution.broker import ExecutionNotEnabled, grant_execution

        try:
            grant_execution()
            transmit = Check("LIVE ORDER TRANSMISSION", CheckStatus.FAIL,
                             "grant_execution() returned a capability")
        except ExecutionNotEnabled:
            transmit = Check("LIVE ORDER TRANSMISSION", CheckStatus.DISABLED,
                             "grant_execution() raises unconditionally")
    except Exception as exc:
        transmit = Check("LIVE ORDER TRANSMISSION", CheckStatus.FAIL, str(exc)[:120])
    checks.append(transmit)

    try:
        from governor import Config as GovernorConfig
        from governor import Reason

        GovernorConfig()
        codes = [a for a in dir(Reason) if a.isupper()]
        checks.append(Check("RISK GOVERNOR", CheckStatus.PASS,
                            f"{len(codes)} reason codes, config validates"))
    except Exception as exc:
        checks.append(Check("RISK GOVERNOR", CheckStatus.FAIL, str(exc)[:120]))

    try:
        import runtime  # noqa: F401
        from runtime import DryRunBroker

        broker = DryRunBroker()
        can_send = any(
            hasattr(broker, m) for m in
            ("place_order", "place_bracket_order", "submit", "cancel_order")
        )
        checks.append(Check(
            "DRY RUN",
            CheckStatus.FAIL if can_send else CheckStatus.PASS,
            "dry-run broker exposes an order method" if can_send
            else "runtime present; dry-run broker has no transmit path",
        ))
    except Exception as exc:
        checks.append(Check("DRY RUN", CheckStatus.FAIL, str(exc)[:120]))

    try:
        import strategies

        available = strategies.available_strategies()
        declared = strategies.unavailable_strategies()
        checks.append(Check(
            "MULTI-STRATEGY RESEARCH",
            CheckStatus.PASS if len(available) >= 3 else CheckStatus.FAIL,
            f"{len(available)} testable, {len(declared)} declared pending data",
        ))
    except Exception as exc:
        checks.append(Check("MULTI-STRATEGY RESEARCH", CheckStatus.FAIL,
                            str(exc)[:120]))

    checks.append(Check(
        "STRATEGY VALIDATION", CheckStatus.NOT_VALIDATED,
        "no strategy has been run on real data; this is expected",
    ))
    checks.append(Check(
        "BACKTEST DATA", CheckStatus.NOT_AVAILABLE,
        "zero validated bars in the repository",
    ))

    return ReadinessReport(tuple(checks))
