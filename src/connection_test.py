"""Build-order gate 1: prove the credentials, the account and the data feed.

Run this before anything else is trusted. It must print a real balance and real
bars (CLAUDE.md, build order item 1).

    python src/connection_test.py

WHY THIS CANNOT PLACE AN ORDER
------------------------------
It is built on ``ProjectX`` alone. Order placement in project-x-py 4.3 lives on
``OrderManager``/``TradingSuite``, which this module never imports and never
constructs. The client's entire public surface was checked against the
installed package: it exposes ``authenticate``, ``get_account_info``,
``list_accounts``, ``get_instrument``, ``get_bars``, ``get_positions``,
``search_open_positions`` and cache/session helpers -- and no place, submit,
modify or cancel method of any kind. The three order-ish names it does have
(``get_session_order_flow``, ``get_session_trades``, ``search_trades``) are
historical reads.

``_assert_no_order_surface`` re-checks that at startup, so the guarantee fails
loudly if a future SDK version adds order methods to the client.

WHAT IT REPORTS ABOUT NET LIQUIDATION
-------------------------------------
``Account`` exposes ``balance`` only -- there is no net-liq field -- so net liq
is derived as ``balance + unrealised P&L`` and the unrealised part is valued at
$2.00 per MNQ point, never the SDK's default of 1.0. See governor_adapter.

The mark used here is the close of the most recent 5-minute bar, not a live
quote: this script deliberately opens no realtime feed. Live trading must use
the realtime price via the adapter.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from project_x_py import ProjectX

sys.path.insert(0, str(Path(__file__).resolve().parent))

from governor_adapter import MNQ_POINT_VALUE  # noqa: E402

SYMBOL = "MNQ"
FORBIDDEN = ("place", "submit", "cancel", "modify", "buy", "sell", "flatten", "liquidate")


def _assert_no_order_surface() -> None:
    """Fail closed if the client ever gains an order-transmission method."""
    found = [
        name
        for name in dir(ProjectX)
        if not name.startswith("_")
        and any(name.lower().startswith(word) for word in FORBIDDEN)
    ]
    if found:
        raise SystemExit(
            "REFUSING TO RUN: the installed project_x_py exposes order methods "
            f"on ProjectX ({', '.join(found)}). This script is only safe while "
            "the client is read-only. Review before running again."
        )


def rule(title: str) -> None:
    print(f"\n{'-' * 68}\n{title}\n{'-' * 68}")


def diagnose(exc: BaseException) -> str:
    """Map a failure to the causes that actually produce it, in likelihood order."""
    text = f"{type(exc).__name__}: {exc}".lower()

    if any(k in text for k in ("401", "unauthor", "invalid", "authenticat", "credential",
                               "forbidden", "403", "token")):
        return (
            "Authentication was rejected. In rough order of likelihood:\n"
            "  1. PROJECT_X_USERNAME must be your TopstepX USERNAME, not the\n"
            "     email address you log in to the website with.\n"
            "  2. The API key was generated BEFORE the account was linked to\n"
            "     TopstepX. Re-generate it after linking -- an older key keeps\n"
            "     authenticating but resolves to no tradable account.\n"
            "  3. The ProjectX/TopstepX API add-on subscription is inactive or\n"
            "     lapsed. API access is a paid add-on separate from the Combine.\n"
            "  4. The key was copied with whitespace or a trailing newline.\n"
            "  5. A VPN or proxy is active. Topstep blocks VPN connections, and\n"
            "     CLAUDE.md forbids one on the trading host anyway."
        )
    if any(k in text for k in ("no account", "not found", "empty", "no tradable",
                               "account_name", "accounts")):
        return (
            "Authenticated, but no usable account came back:\n"
            "  1. The Combine account is not linked to TopstepX yet.\n"
            "  2. PROJECT_X_ACCOUNT_NAME is set and does not match any account\n"
            "     name exactly. Leave it unset to take the default.\n"
            "  3. The account is failed, expired, or awaiting reset, so the\n"
            "     gateway reports it as not tradable."
        )
    if any(k in text for k in ("timeout", "connection", "network", "dns", "ssl",
                               "unreachable", "resolve", "getaddrinfo")):
        return (
            "Could not reach the gateway:\n"
            "  1. A VPN or proxy is active -- disable it (Topstep blocks VPNs,\n"
            "     and CLAUDE.md forbids one on this host).\n"
            "  2. No internet connection, or DNS is failing.\n"
            "  3. A firewall or corporate network is blocking outbound HTTPS.\n"
            "  4. The gateway is down or in maintenance (weekend window)."
        )
    if "429" in text or "rate" in text:
        return (
            "Rate limited (200 requests/60s general, 50/30s on bars).\n"
            "Wait a minute and retry. Back off rather than retrying hot."
        )
    if any(k in text for k in ("instrument", "symbol", "contract")):
        return (
            "The instrument could not be resolved:\n"
            "  1. The front-month MNQ contract may have rolled.\n"
            "  2. Market data entitlements may not be active on the account."
        )
    return (
        "Unrecognised failure. Check, in order: VPN off, subscription active,\n"
        "account linked to TopstepX, key generated after linking, and that\n"
        "PROJECT_X_USERNAME is the username rather than the email address."
    )


async def run() -> int:
    rule("0. Environment")
    load_dotenv()
    missing = [k for k in ("PROJECT_X_API_KEY", "PROJECT_X_USERNAME") if not os.environ.get(k)]
    if missing:
        print(f"  MISSING: {', '.join(missing)}")
        print("  Copy .env.example to .env and fill it in. .env is gitignored.")
        return 1

    key = os.environ["PROJECT_X_API_KEY"]
    user = os.environ["PROJECT_X_USERNAME"]
    print(f"  PROJECT_X_USERNAME     {user}")
    print(f"  PROJECT_X_API_KEY      set, {len(key)} chars, "
          f"ends {key[-4:] if len(key) >= 4 else '??'}")
    if key.strip() != key:
        print("  WARNING: the key has leading/trailing whitespace. That alone "
              "causes 401s.")
    if "@" in user:
        print("  WARNING: PROJECT_X_USERNAME looks like an email address. "
              "TopstepX expects the USERNAME.")
    acct_name = os.environ.get("PROJECT_X_ACCOUNT_NAME")
    print(f"  PROJECT_X_ACCOUNT_NAME {acct_name or '(unset -- will use default)'}")

    async with ProjectX.from_env() as client:
        rule("1. Authenticate")
        await client.authenticate()
        print("  OK")

        rule("2. Account")
        account = client.get_account_info()
        if account is None:
            raise RuntimeError("get_account_info() returned no account")
        print(f"  name       {account.name}")
        print(f"  id         {account.id}")
        print(f"  balance    ${float(account.balance):,.2f}")
        print(f"  canTrade   {account.canTrade}")
        print(f"  simulated  {account.simulated}")

        try:
            everything = await client.list_accounts()
            print(f"  visible accounts: "
                  f"{', '.join(f'{a.name} (id {a.id})' for a in everything) or 'none'}")
        except Exception as exc:  # non-fatal
            print(f"  (could not list all accounts: {exc})")

        rule("3. Contract geometry")
        instrument = await client.get_instrument(SYMBOL)
        tick_size = float(instrument.tickSize)
        tick_value = float(instrument.tickValue)
        derived = tick_value / tick_size
        print(f"  contract   {instrument.name} (id {instrument.id})")
        print(f"  tickSize   {tick_size}")
        print(f"  tickValue  ${tick_value}")
        print(f"  point value = tickValue/tickSize = ${derived:,.2f}")
        if abs(derived - MNQ_POINT_VALUE) > 1e-9:
            print(f"  *** MISMATCH: expected ${MNQ_POINT_VALUE:,.2f}. Do not trade "
                  "until this is understood -- every P&L figure depends on it.")
        else:
            print(f"  matches MNQ_POINT_VALUE (${MNQ_POINT_VALUE:,.2f})  OK")

        rule("4. Bars: MNQ 5-minute")
        bars = await client.get_bars(SYMBOL, days=5, interval=5)
        if bars is None or len(bars) == 0:
            print("  NO BARS RETURNED. Market data entitlements may be inactive, "
                  "or the market has been closed for the whole window.")
            return 1
        print(f"  rows returned  {len(bars)}")
        print(f"  columns        {list(bars.columns)}")
        print("  last 3 rows:")
        for line in str(bars.tail(3)).splitlines():
            print(f"    {line}")

        close_col = next((c for c in bars.columns if c.lower() == "close"), None)
        if close_col is None:
            print("  WARNING: no 'close' column; cannot mark positions.")
            return 1
        mark = float(bars[close_col][-1])
        print(f"  mark (last 5m close)  {mark:,.2f}")

        rule("5. Positions and derived net liquidation")
        positions = await client.search_open_positions()
        balance = float(account.balance)
        if not positions:
            print("  open positions  none")
            unrealised = 0.0
        else:
            unrealised = 0.0
            for p in positions:
                pnl = float(p.unrealized_pnl(mark, MNQ_POINT_VALUE))
                unrealised += pnl
                print(f"  {p.contractId}  {p.direction}  size {p.size} "
                      f"(signed {p.signed_size})  avg {float(p.averagePrice):,.2f}  "
                      f"unrealised ${pnl:,.2f}")
            print("  (valued at the last 5m close, not a live quote)")

        net_liq = balance + unrealised
        print(f"\n  balance                ${balance:,.2f}")
        print(f"  unrealised P&L         ${unrealised:,.2f}")
        print(f"  NET LIQUIDATION        ${net_liq:,.2f}")
        print("  Net liq is DERIVED. Account exposes no net-liq field, so this "
              "is balance + unrealised P&L.")

        rule("6. Trailing Max Loss Limit floor")
        fields = sorted(
            getattr(type(account), "model_fields", None)
            or getattr(type(account), "__dataclass_fields__", {})
        )
        print(f"  Account fields returned by the gateway: {', '.join(fields)}")
        candidates = [
            f for f in fields
            if any(k in f.lower() for k in ("loss", "drawdown", "trail", "mll", "limit"))
        ]
        if candidates:
            print(f"  POSSIBLE MLL FIELDS FOUND: {', '.join(candidates)}")
            for f in candidates:
                print(f"    {f} = {getattr(account, f, '?')}")
            print("  Verify whether any of these is the trailing MLL floor.")
        else:
            print("  *** THE TRAILING MLL FLOOR IS NOT EXPOSED BY THIS API. ***")
            print("  No field on Account corresponds to it, and the SDK's")
            print("  max_loss_limit / RiskConfig values are client-side settings,")
            print("  not the firm's floor.")
            print()
            print("  The governor REQUIRES this value -- it is the only")
            print("  permanent-failure guard. It must therefore come from:")
            print("    (a) the TopstepX dashboard, entered as MLL_FLOOR in .env; or")
            print("    (b) tracked locally as (highest end-of-day balance - 2000),")
            print("        locking at 50,000 once the account reaches 52,000.")
            print("  Option (b) must be reconciled against the dashboard daily")
            print("  until it is proven to agree. A wrong floor silently disables")
            print("  the guard: too low and it never fires, too high and it fires")
            print("  constantly.")
            print()
            print(f"  MLL_FLOOR currently in .env: "
                  f"{os.environ.get('MLL_FLOOR') or '(unset)'}")

        rule("RESULT")
        print("  Gate 1 PASSED: authenticated, real balance, real bars.")
        print("  No order was placed and none could have been.")
        return 0


def main() -> int:
    _assert_no_order_surface()
    print("Topstep / ProjectX connection test -- READ ONLY, places no orders.")
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130
    except Exception as exc:
        print(f"\n{'=' * 68}\nFAILED: {type(exc).__name__}: {exc}\n{'=' * 68}")
        print(diagnose(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
