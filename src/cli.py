"""Research and operations CLI. Safe by default.

    python -m src.cli config
    python -m src.cli connection-test
    python -m src.cli data --validate FILE --convention CLOSE --tz UTC
    python -m src.cli data --probe MNQ
    python -m src.cli backtest --csv FILE --convention CLOSE --tz UTC
    python -m src.cli monte-carlo --csv FILE --convention CLOSE --tz UTC
    python -m src.cli mll --seed --mll 48000
    python -m src.cli compliance
    python -m src.cli dry-run

Every command that could touch the network is read-only, and none can transmit
an order -- the adapter has no method to do so. ``dry-run`` exists as an
explicit command rather than a flag on a trading command, so there is no
command whose default behaviour is "trade".
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

__all__ = ["main"]


def _load_series(args: argparse.Namespace):
    """Load bars from CSV, refusing to guess the two unverified properties."""
    from backtest import BarTimestamp
    from data import load_csv

    if not args.convention or not args.tz:
        print(
            "--csv requires --convention and --tz.\n"
            "Neither is guessable: whether a timestamp labels a bar's open or "
            "close, and whether times are exchange-local or UTC, both decide "
            "which bars form an opening range. FIRM_RULES.md lists both as "
            "UNVERIFIED; establish them in ROADMAP Stage 1.",
            file=sys.stderr,
        )
        return None

    result = load_csv(
        args.csv,
        convention=BarTimestamp(args.convention),
        source_timezone=ZoneInfo(args.tz),
        interval_minutes=args.interval,
    )
    print(result.report())
    if not result.ok:
        return None
    return result.series


def _build_strategy(args: argparse.Namespace):
    from strategies.orb import OpeningRangeBreakout, OrbConfig

    return OpeningRangeBreakout(OrbConfig(
        atr_period=args.atr_period,
        reward_multiple=args.reward,
        size=args.size,
        max_risk_dollars=args.risk,
    ))


def cmd_config(_args: argparse.Namespace) -> int:
    from config import load_config

    cfg = load_config()
    print("CONFIGURATION (secrets redacted)")
    print("=" * 40)
    for key, value in cfg.redacted().items():
        print(f"  {key:24s} {value}")
    print()
    print(f"  execution mode           {cfg.execution_mode.value}")
    print(f"  may transmit orders      {cfg.execution_mode.may_transmit}")
    if not cfg.has_credentials:
        print()
        print("  No credentials configured. Backtesting and research work; "
              "anything needing the API does not.")
    return 0


def cmd_config_check(_args: argparse.Namespace) -> int:
    """READ-ONLY. Reports presence, never values."""
    from config import ConfigError, load_config

    print("CONFIG CHECK  [READ-ONLY]")
    print("=" * 46)
    try:
        cfg = load_config()
    except ConfigError as exc:
        print("  CONFIGURATION INVALID")
        print(f"    {exc}")
        return 1

    red = cfg.redacted()
    print(f"  API KEY            {red['api_key']}")
    print(f"  USERNAME           {red['username']}")
    print(f"  ACCOUNT            {cfg.account_name or 'ABSENT (uses default)'}")
    print(f"  DRY RUN            {cfg.dry_run}")
    print(f"  LIVE TRADING       "
          f"{'ENABLED' if cfg.live_trading_enabled else 'DISABLED'}")
    print(f"  EXECUTION MODE     {cfg.execution_mode.value}")
    print(f"  MAY TRANSMIT       {cfg.execution_mode.may_transmit}")
    print()
    print(f"  SYMBOL             {cfg.symbol}")
    print(f"  POSITION SIZE      {cfg.position_size}")
    print(f"  RISK PER TRADE     ${cfg.risk_per_trade:,.2f}")
    print(f"  DAILY MAX LOSS     ${cfg.daily_max_loss:,.2f}")
    print(f"  DAILY TARGET       ${cfg.daily_profit_target:,.2f}")
    print(f"  FLOOR BUFFER       ${cfg.floor_buffer:,.2f}")
    print(f"  TIMEZONE           {cfg.timezone}")
    print()
    print("  The API key value is never printed, logged or returned.")
    if not cfg.has_credentials:
        print("  No usable credentials: research works, API commands do not.")
        return 0
    return 0


def cmd_readiness(args: argparse.Namespace) -> int:
    """READ-ONLY. Never reports ready merely because a key exists."""
    from readiness import assess

    report = assess(run_tests=not args.skip_tests)
    print(report.render())
    if args.json:
        import json as _json

        Path(args.json).write_text(_json.dumps(report.to_dict(), indent=2),
                                   encoding="utf-8")
        print(f"Machine-readable report written to {args.json}")
    return 0 if report.classification == "API-KEY-READY" else 1


def cmd_reconcile(_args: argparse.Namespace) -> int:
    """READ-ONLY. Compares broker truth against local state."""
    from config import load_config
    from session_state import load_state

    cfg = load_config()
    print("RECONCILE  [READ-ONLY]")
    print("=" * 46)
    local = load_state()
    if local is None:
        print("  local state : none on disk")
    else:
        print(local.render())
    print()
    if not cfg.has_credentials:
        print("  Broker state UNAVAILABLE: no credentials configured.")
        print("  Reconciliation compares LOCAL belief against BROKER truth, so")
        print("  it cannot run one-sided. Add credentials and re-run.")
        return 1
    print("  Live reconciliation needs a connection; run connection-test first.")
    print("  Nothing is assumed about positions in the meantime.")
    return 1


def cmd_research(args: argparse.Namespace) -> int:
    """READ-ONLY. Lists the strategy catalogue and the research pipeline."""
    import strategies

    print("RESEARCH CATALOGUE  [READ-ONLY]")
    print("=" * 66)
    available = strategies.available_strategies()
    declared = strategies.unavailable_strategies()

    print()
    print(f"TESTABLE ON 5m OHLCV ({len(available)})")
    for name, meta in sorted(available.items()):
        print()
        print(meta.describe())

    print()
    print(f"DECLARED, NOT IMPLEMENTED ({len(declared)})")
    print("These need data we do not have. They are NOT run against")
    print("approximated inputs, because that would produce numbers.")
    for name, meta in sorted(declared.items()):
        missing = ", ".join(r.value for r in meta.missing_requirements)
        print(f"  {name:<30s} needs {missing}")
        print(f"      {meta.unavailable_reason}")

    print()
    print("PIPELINE (each stage can reject a candidate)")
    for stage in ("DATA VALIDATION", "BASELINE BACKTEST", "COST STRESS",
                  "IN-SAMPLE / OUT-OF-SAMPLE", "WALK-FORWARD", "MONTE CARLO",
                  "PARAMETER ROBUSTNESS", "REGIME ANALYSIS",
                  "TOPSTEP CONSTRAINTS", "CROSS-STRATEGY COMPARISON"):
        print(f"  -> {stage}")
    print()
    print("No strategy has been run on real data. NONE IS VALIDATED.")
    print("Requires ROADMAP Stage 1 and 2 first.")
    return 0


def cmd_connection_test(_args: argparse.Namespace) -> int:
    import connection_test

    return connection_test.main()


def cmd_data(args: argparse.Namespace) -> int:
    import data

    argv: list[str] = []
    if args.probe:
        argv += ["--probe", args.probe]
    if args.validate:
        argv += ["--validate", args.validate]
    if args.convention:
        argv += ["--convention", args.convention]
    if args.tz:
        argv += ["--tz", args.tz]
    argv += ["--interval", str(args.interval)]
    return data.main(argv)


def cmd_backtest(args: argparse.Namespace) -> int:
    from backtest import CostModel, run_backtest
    from performance import analyse

    series = _load_series(args)
    if series is None:
        return 1

    result = run_backtest(
        series,
        _build_strategy(args),
        costs=CostModel(slippage_ticks=args.slippage),
        starting_balance=args.balance,
    )
    report = analyse(result, strategy="ORB v0.1", symbol=args.symbol,
                     timeframe=f"{args.interval}m")
    print()
    print(report.render())
    if args.json:
        Path(args.json).write_text(report.to_json(), encoding="utf-8")
        print(f"\nMachine-readable report written to {args.json}")
    print()
    print("ORB v0.1 is a HYPOTHESIS. A single backtest is one sample; see "
          "`monte-carlo` and ROADMAP Stage 4 before drawing any conclusion.")
    return 0 if report.gates.get("FINAL VERDICT") == "PASS" else 1


def cmd_monte_carlo(args: argparse.Namespace) -> int:
    from backtest import CostModel, run_backtest
    from research import monte_carlo

    series = _load_series(args)
    if series is None:
        return 1

    result = run_backtest(series, _build_strategy(args),
                          costs=CostModel(slippage_ticks=args.slippage),
                          starting_balance=args.balance)
    if not result.trades:
        print("\nNo trades were taken; there is nothing to resample.")
        return 1

    outcome = monte_carlo(
        [t.net for t in result.trades],
        simulations=args.simulations, method=args.method, seed=args.seed,
    )
    print()
    print(outcome.render())
    return 0


def cmd_mll(args: argparse.Namespace) -> int:
    import mll_tracker

    argv: list[str] = []
    if args.seed:
        argv.append("--seed")
    if args.verify:
        argv.append("--verify")
    if args.show:
        argv.append("--show")
    if args.mll is not None:
        argv += ["--mll", str(args.mll)]
    return mll_tracker.main(argv)


def cmd_compliance(_args: argparse.Namespace) -> int:
    from compliance import report

    print(report([]))
    print("\nNo session history is recorded yet; this shows the starting state.")
    return 0


def cmd_dry_run(_args: argparse.Namespace) -> int:
    from config import load_config

    cfg = load_config()
    print("DRY RUN")
    print("=" * 40)
    print(f"  execution mode      {cfg.execution_mode.value}")
    print(f"  may transmit orders {cfg.execution_mode.may_transmit}")
    print()
    if not cfg.has_credentials:
        print("  No credentials configured, so there is no market data to")
        print("  consume. Complete ROADMAP Stage 0 and Stage 1 first.")
        return 1
    print("  The runtime is implemented and CANNOT transmit: its broker has")
    print("  no order method at all. It needs a live market-data feed to")
    print("  consume, which requires credentials and ROADMAP Stage 1.")
    print()
    print("  Offline, `backtest` drives the identical pipeline (same strategy,")
    print("  same governor, same intent model) over historical bars.")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.cli",
        description="Topstep MNQ research and operations. Read-only; cannot "
                    "transmit orders.",
    )
    sub = parser.add_subparsers(dest="command")

    def add_data_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--csv", help="bar CSV to load")
        p.add_argument("--convention", choices=["OPEN", "CLOSE"],
                       help="what a timestamp labels; REQUIRED with --csv")
        p.add_argument("--tz", help="source timezone; REQUIRED with --csv")
        p.add_argument("--interval", type=int, default=5)
        p.add_argument("--symbol", default="MNQ")
        p.add_argument("--slippage", type=float, default=2.0,
                       help="adverse slippage in ticks (default pessimistic)")
        p.add_argument("--balance", type=float, default=50_000.0)
        p.add_argument("--atr-period", type=int, default=14, dest="atr_period")
        p.add_argument("--reward", type=float, default=1.5)
        p.add_argument("--size", type=int, default=2)
        p.add_argument("--risk", type=float, default=80.0)

    sub.add_parser("config", help="print the effective configuration")
    sub.add_parser("config-check", help="[READ-ONLY] credential presence only")
    sub.add_parser("reconcile", help="[READ-ONLY] compare local state to broker")
    sub.add_parser("research", help="[READ-ONLY] strategy catalogue and pipeline")

    p_ready = sub.add_parser("readiness", help="[READ-ONLY] final safety gate")
    p_ready.add_argument("--skip-tests", action="store_true",
                         help="do not run the suite (faster, weaker)")
    p_ready.add_argument("--json", help="also write the report here")
    sub.add_parser("connection-test", help="read-only API diagnostic")
    sub.add_parser("compliance", help="Combine and payout status")
    sub.add_parser("dry-run", help="decide against live data, transmit nothing")

    p_data = sub.add_parser("data", help="validate or probe market data")
    p_data.add_argument("--probe", nargs="?", const="MNQ", metavar="SYMBOL")
    p_data.add_argument("--validate", metavar="CSV")
    p_data.add_argument("--convention", choices=["OPEN", "CLOSE"])
    p_data.add_argument("--tz")
    p_data.add_argument("--interval", type=int, default=5)

    p_bt = sub.add_parser("backtest", help="run a backtest and report")
    add_data_args(p_bt)
    p_bt.add_argument("--json", help="also write a machine-readable report here")

    p_mc = sub.add_parser("monte-carlo", help="resample the trade sequence")
    add_data_args(p_mc)
    p_mc.add_argument("--simulations", type=int, default=10_000)
    p_mc.add_argument("--method", choices=["shuffle", "bootstrap"],
                      default="shuffle")
    p_mc.add_argument("--seed", type=int, default=20260912)

    p_mll = sub.add_parser("mll", help="seed or verify the tracked MLL floor")
    p_mll.add_argument("--seed", action="store_true")
    p_mll.add_argument("--verify", action="store_true")
    p_mll.add_argument("--show", action="store_true")
    p_mll.add_argument("--mll", type=float)

    args = parser.parse_args(argv)
    handlers = {
        "config": cmd_config,
        "config-check": cmd_config_check,
        "readiness": cmd_readiness,
        "reconcile": cmd_reconcile,
        "research": cmd_research,
        "connection-test": cmd_connection_test,
        "data": cmd_data,
        "backtest": cmd_backtest,
        "monte-carlo": cmd_monte_carlo,
        "mll": cmd_mll,
        "compliance": cmd_compliance,
        "dry-run": cmd_dry_run,
    }
    handler = handlers.get(args.command)
    if handler is None:
        parser.print_help()
        return 2
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
