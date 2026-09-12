# Topstep Algo — project context

Read this before writing any code. It carries the decisions already made so they
don't get re-litigated or accidentally violated.

## What this is

An automated futures trading bot for a **Topstep 50K Trading Combine** account,
written in Python against the **ProjectX Gateway API** via the `project-x-py` SDK.

Owner: Monish. Sole author — the strategy must remain solely owned and must not be
shared, sold, or run at another prop firm. Keep git history clean and attributable;
it is the evidence of sole ownership if Topstep ever asks.

## Hard rules that end the account

These are firm rules, not preferences. Violating any of them is not a bug, it is a
blown account.

| Rule | Value | Consequence |
|---|---|---|
| Max Loss Limit (the "One Rule") | $2,000, EOD trailing | **Permanent failure.** Enforced in real time on net liquidation, including open P&L. |
| Daily Loss Limit | $1,000 (Responsible Trading Advantage) | Locks the account for that session only. |
| Consistency target (Combine) | 55% | Best single day must stay within 55% of the $3,000 profit target. |
| Profit target (Combine) | $3,000 | Pass condition. No minimum trading days. |
| Max contracts | 5 mini / 50 micro | Micros count 10:1. |
| Flat by | 15:10 CT / 16:10 ET | Positions must be closed. Day trading only, no overnight. |
| HFT | Prohibited | No latency arbitrage, no sub-second churn. |
| Hosting | Personal device only | **No VPS, no VPN, no remote server may transmit orders.** A server may log, backtest and serve read-only dashboards. |

## Non-negotiable design constraints

1. **Every entry is a native gateway bracket order.** Stop and target rest on
   Topstep's side. Never a bare market order with the stop living in this process —
   if Python dies, the position must still be protected.
2. **The risk governor is authoritative.** The strategy proposes; the governor
   decides. No order is transmitted without governor approval.
3. **Net liquidation, not realised P&L.** The MLL is enforced against net liq
   including open positions. Any check that uses realised P&L is wrong.
4. **Session boundary is 18:00 ET, not midnight.** All daily accounting resets there.
5. **Reconcile on every startup.** Query actual positions and working orders and
   adopt reality. Never assume flat.
6. **Position size is 2 MNQ, hard-coded.** Not a tunable parameter. Raising it
   requires clearing the go-live gates first.
7. **No secrets in code.** `.env` only, and `.env` is gitignored from commit one.

## Risk governor spec

Flatten and disable for the session on any of:
- Daily profit target reached (+$500)
- Daily loss reached (-$250) — far inside the firm's $1,000 DLL
- Clock reaches 16:30 ET
- Net liquidation comes within $400 of the trailing MLL floor

Refuse to open a new position when:
- Fewer than 10 minutes remain before the hard flatten
- A position is already open
- State is unreconciled after a reconnect
- The kill switch file exists

Also required: a one-action kill switch that flattens everything and stops.

## Strategy v0.1 spec — opening range breakout, MNQ

- Opening range = 09:30–09:45 ET high and low
- Entry on a 5-minute **close** beyond the range, one trade per direction per day
- Stop = range midpoint or 1×ATR(14) on 5m, whichever is tighter, capped at $80 risk
- Target = 1.5R
- Max 2 trades per session, no new entries after 11:30 ET
- Minimum hold time is naturally minutes, which keeps well clear of any
  microscalping concern

This is a **hypothesis, not a validated edge.** Judge backtests on drawdown, not
profit: reject any parameter set with max drawdown over $1,200, 5+ consecutive
losing days, or fewer than 200 trades of evidence. Tune at most two parameters —
more than that is curve-fitting.

## SDK notes

`project-x-py` v4.x is **async throughout**; the synchronous API was removed.

```python
from project_x_py import ProjectX, TradingSuite

async with ProjectX.from_env() as client:
    await client.authenticate()
    acct = client.get_account_info()
    bars = await client.get_bars("MNQ", days=5, interval=5)

suite = await TradingSuite.create("MNQ")
await suite.orders.place_bracket_order(
    contract_id=suite.instrument_id, side=0, size=2,
    entry_price=..., stop_loss_price=..., take_profit_price=...,
)
positions = await suite.positions.get_all_positions()
```

Env vars are `PROJECT_X_API_KEY`, `PROJECT_X_USERNAME`, optional
`PROJECT_X_ACCOUNT_NAME`.

Rate limits: 200 requests / 60s general, 50 requests / 30s on `retrieveBars`.
Nowhere near binding for this strategy — but back off on HTTP 429 rather than retrying hot.

There is **no sandbox**. API orders hit the live account path. The Combine account
is simulated, so it is the test environment.

## Build order — do not reorder

1. `src/connection_test.py` — must print real balance and real bars. **Gate.**
2. Risk governor + its tests. Deliberately try to break it.
3. Strategy v0.1.
4. Watchdog: heartbeat, push alerts, one-line-per-decision logging.
5. Windows Task Scheduler registration, restart-on-failure, reboot test.
6. `DRY_RUN=true` overnight rehearsal on Globex before ever transmitting.

## Failure modes seen in this domain — design against these

- Naked position after the process or network dies → broker-side brackets
- Double entry after a reconnect → reconcile before deciding
- Timezone or session-boundary bug trading at 03:00 → explicit ET handling, tested
- A Windows update reboot mid-position → host hardening already applied
- Gaming on the host starving the process → don't; Game Mode is disabled
- Silent throttling from an overclock unstable over 23h uptime → OC disabled