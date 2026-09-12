# Topstep firm rules — the authoritative reference

Every number here is tagged with how well it is known. **Never treat a DERIVED or
UNVERIFIED value as fact in code without a fail-closed guard.**

- `VERIFIED` — read from Topstep's or ProjectX's own documentation
- `DERIVED` — computed from a verified rule
- `UNVERIFIED` — from secondary sources, or assumed. Must fail closed.

Rules change without notice. Re-check anything load-bearing against the dashboard
and Topstep support before it governs real size.

## Account: Trading Combine 50K

| Item | Value | Status |
|---|---|---|
| Profit target | $3,000 | VERIFIED |
| Max Loss Limit ("the One Rule") | $2,000 | VERIFIED |
| Initial MLL floor | $48,000 | VERIFIED |
| Daily Loss Limit (Responsible Trading Advantage) | $1,000 | VERIFIED |
| Consistency target (Combine) | 55% | VERIFIED |
| Max contracts | 5 mini / 50 micro (10:1) | VERIFIED |
| Minimum trading days | none | VERIFIED |
| Platform required for 2026 Combines | TopstepX | VERIFIED |
| Cost | $49/month, $49 reset, monthly reset credit included | VERIFIED |
| Express Funded activation fee | $149 | VERIFIED |

## The Max Loss Limit — the only permanent-failure rule

VERIFIED from Topstep's help centre:

- Trails the **end-of-day closing balance**. Rises only, never falls.
- Monitored in **real time** against net liquidation **including unrealised P&L**.
- **Locks permanently** once it reaches the starting balance.
- Breach is immediate liquidation and permanent account failure. No appeal.

Worked example from Topstep, used as a test fixture:
- Day 1 closes $50,500 → MLL $48,500
- Day 2 closes back at $50,000 → MLL **stays** $48,500

DERIVED formula:

    mll_floor = min(max_eod_balance_ever_seen - 2000, 50000)

**The API does not expose this value.** Confirmed against the official ProjectX
reference and by inspecting the installed SDK: every REST endpoint, every model,
every websocket payload. We keep our own books. See `src/mll_tracker.py`, which
fails closed if its state is missing, stale or corrupt.

## Trading hours

| Item | Value | Status |
|---|---|---|
| All positions flat by | 15:10 CT / **16:10 ET** | VERIFIED |
| Topstep risk managers begin flattening | 15:08 CT / **16:08 ET** | VERIFIED |
| Working orders auto-cancel | 15:10 CT / 16:10 ET | VERIFIED |
| Session reopens | 17:00 CT / 18:00 ET | VERIFIED |
| **Our own hard flatten** | **15:55 ET** | DERIVED (13 min margin) |
| Our last entry | 15:45 ET | DERIVED (flatten − lockout) |
| Session accounting boundary | 18:00 ET, never midnight | VERIFIED |
| Overnight / weekend holds | prohibited at every stage | VERIFIED |

On holiday half-days Topstep moves the deadline to **15 minutes before the early
close**, and announces it in their Discord. Their announcement governs the
account, not CME's raw calendar.

## Automation policy

VERIFIED from Topstep's help centre:

> "Custom automated strategies and bots are allowed via the TopstepX / ProjectX
> API, subject to standard platform rules and our prohibition on high-frequency
> trading (HFT)."

> "All trading activity must originate from your personal device. The use of VPS,
> VPNs, and remote servers is prohibited."

> "Your server can watch and record, but it cannot trade."

There is **no chart-watching or monitoring requirement** — that claim came from a
third-party analysis and is not Topstep's rule. The binding constraint is where
the code runs.

A private server may hold historical data, run backtests, collect logs, serve
read-only dashboards, and receive copies of fills, positions and P&L. It may not
transmit, modify or cancel an order.

## Payouts — Express Funded Account, after passing

All caps are **per request**. There is no lifetime cap.

| Path | Requirement | Cap (50K, RTA on) |
|---|---|---|
| Standard | 5 winning days of $150+ net | $4,000 |
| Consistency | 3 trading days, best day ≤40% of total profit | $6,000 |

- Minimum request $125. Each request also limited to 50% of balance.
- Profit split 90/10.
- **After every payout the MLL resets to $0 permanently** — the floor locks at the
  starting balance and can never fall below it again. The winning-day counter
  restarts at the same moment.
- That reset is worth taking early even on a small payout: it converts the floor
  from trailing to fixed. `mll_tracker` will need a state transition for it.

## Prohibited

- High-frequency trading and latency arbitrage
- Hedging — opposing positions in the same or correlated instruments
- VPS, VPN, remote servers transmitting orders
- Running the identical strategy another trader also runs (correlated-P&L flags)

"Sole ownership" concerns other traders, not tools. Building with AI assistance is
normal and does not conflict with it. Keep the repo private, don't share or sell
the strategy, don't run it at another firm.

## UNVERIFIED — handle with care

- **CME holiday calendar.** Three secondary sources agree on the *dates* but
  disagree on the *times* (12:00 vs 12:15 CT, and whether some days are full
  closures). CME's own calendar page was unreachable. We therefore refuse to
  trade on any holiday date at all rather than depend on a contested time.
  `CALENDAR_VERIFIED = False` until someone checks against CME directly.
- **ProjectX history depth.** Undocumented. `--probe` answers it.
- **Bar timestamp semantics.** Whether `t` labels the bar's open or close, and
  whether times are exchange-local or UTC. Must be confirmed from real data
  before any opening range is computed.
- **`AccountUpdatePayload.equity`.** Declared optional in the websocket payload.
  If populated it is the broker's own net liquidation and is strictly better than
  our `balance + unrealised P&L` derivation. Unknown until the feed is live.

## Contract specs — MNQ

| Item | Value |
|---|---|
| Point value | **$2.00** (tickValue 0.50 ÷ tickSize 0.25) |
| Tick size | 0.25 |
| Tick value | $0.50 |
| Commission | $1.82 per round turn |

The SDK's `Position.unrealized_pnl(price, tick_value=1.0)` default is **wrong for
MNQ** and reports exactly half of every loss. Never call it without passing the
point value explicitly. An AST scan enforces this.
