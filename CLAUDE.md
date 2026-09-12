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
| Flat by (firm) | 15:10 CT / 16:10 ET | Positions must be closed. Day trading only, no overnight. Topstep risk managers **begin flattening at 16:08 ET** — their deadline is already too late to start acting. |
| Flat by (ours) | **15:55 ET** | Our own hard flatten, 13 minutes before Topstep's desk intervenes. This is the value the governor enforces. |
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
- Clock reaches **15:55 ET** (firm deadline is 16:10 ET; their risk
  managers start flattening at 16:08 ET, so 15:55 leaves 13 minutes)
- Net liquidation comes within $400 of the trailing MLL floor

Refuse to open a new position when:
- Fewer than 10 minutes remain before the hard flatten (so: no new entry after 15:45 ET)
- A position is already open
- State is unreconciled after a reconnect
- The kill switch file exists

Halt first, before any of the above, when the state itself cannot be trusted:
- `STALE_SESSION_ANCHOR` — the anchor does not belong to this session, so every
  limit derived from session P&L is measuring the wrong day
- `MLL_STATE_UNAVAILABLE` — the tracked floor is missing, stale or unparseable

Further refusals beyond the four above:
- `MARKET_CLOSED` — weekend, holiday, or outside the calendar's coverage
- `TRADE_COUNT_DRIFT` — observed fills disagree with the tracked trade count
- `SESSION_ANCHOR_MISMATCH` — governor and adapter anchors disagree
- `BEFORE_RTH_OPEN` — before 09:30 ET, which makes an overnight entry impossible

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

## Verified SDK behaviour

Checked by introspecting the **installed** `project-x-py 4.3.0`, not by reading
docs. The SDK notes above are accurate as far as they go; these are the things
that are not written down and that will quietly produce wrong numbers.

### 1. `Account` has no net-liquidation field

The model returns exactly: `id`, `name`, `balance`, `canTrade`, `isVisible`,
`simulated`. There is no net-liq field, and `Position` carries no unrealised
P&L field either — only `size`, `type`, `averagePrice`, `contractId`.

**Net liq must be derived: `balance + unrealised P&L`.**

This matters because the obvious implementation is wrong in the worst way.
Reading `balance` alone runs, returns a plausible number, and silently violates
design constraint 3: `balance` does not move while a position is open, so a
position running $1,500 against us reports zero session loss and the MLL guard
never fires. The check that looks correct is the one that blows the account.

Corollary: if the unrealised component cannot be computed — no current price,
stale feed — net liq is **unknown**, and unknown must stop trading. It must
never fall back to `balance`. `governor_adapter.value_positions` raises
`SnapshotUnavailable` instead.

### 2. `Position.unrealized_pnl` defaults to the wrong multiplier

Signature: `unrealized_pnl(current_price: float, tick_value: float = 1.0)`.

It multiplies a **point** difference by `tick_value`, and the default is `1.0`.
For MNQ the correct figure is:

    point value = tickValue / tickSize = 0.50 / 0.25 = $2.00 per index point

So accepting the default reports **exactly half** of every move. A real $80
stop-out reads as $40; the $250 session loss limit does not fire until the
account is actually down $500; the $400 MLL buffer is really $800 of exposure.
Every dollar figure in the system inherits this error, and nothing about the
call site looks wrong.

Defences, all three in place:
- `governor_adapter.MNQ_POINT_VALUE = 2.00`, with the derivation in a comment.
- `point_value()` derives it from the live contract and **refuses to value
  positions** if an MNQ contract ever reports geometry that disagrees.
- A test asserts 2 MNQ down 10 points is `-$40.00`, and an AST test fails the
  build if any call site omits the argument.

### 3. The trailing MLL floor is not exposed anywhere — confirmed

This was searched properly, because keeping our own books on the single rule
that permanently ends the account is not something to do on an assumption.
Swept the installed 4.3.0 for any field, endpoint or event carrying it:

- **Every model**: `Account` returns only `id, name, balance, canTrade,
  isVisible, simulated`. No other model has a loss/drawdown/limit field.
- **All 21 REST endpoints**: `/Account/search`, `/Auth/*`, `/Contract/*`,
  `/History/retrieveBars`, `/Order/*`, `/Position/*`, `/Status/ping`,
  `/Trade/search`. None returns account risk limits.
- **Websocket payloads**: `AccountUpdatePayload` carries `accountId`,
  `balance`, optional `equity`, optional `margin`, `timestamp`. No floor.
- `RiskConfig` and `stats_types.max_loss_limit` are **client-side settings we
  would be choosing ourselves**, not the firm's figure.

**Conclusion: absent. It is not an oversight that we track it ourselves —
there is nothing to read.** `src/mll_tracker.py` reconstructs it:

    mll_floor = min(max_eod_balance_ever_seen − mll_distance, starting_balance)

trailing the end-of-day close, rising only, locking permanently once it reaches
the starting balance. Seed it from the dashboard and reconcile daily:

    python -m src.mll_tracker --seed --mll 48000
    python -m src.mll_tracker --verify --mll <displayed floor>

On disagreement it always adopts the HIGHER floor — less headroom, stops us
sooner. Missing, stale or unparseable state yields `MLL_STATE_UNAVAILABLE` and
a halt. It never estimates and never defaults: a floor of zero would make the
buffer check pass unconditionally and disable the guard in silence.

**Funded-account note (not yet implemented).** On a funded XFA account Topstep
sets the MLL to $0 permanently after the first payout. That is a different
regime, not a different number — when this account is funded it needs an
explicit state transition in the tracker, not an edited `mll_distance`.

### 4. `equity` may exist on the realtime feed — worth checking

`AccountUpdatePayload` declares optional `equity` and `margin`. If the gateway
actually populates `equity` it is likely a true net liquidation, which would be
more authoritative than deriving it from balance plus unrealised P&L. Nothing
depends on this today, and the derivation stands until someone confirms it on a
live realtime connection. Worth checking when the feed is first wired up.

## Governor caller contract

The governor is pure, so it cannot maintain its own state. Every cycle the
caller MUST, in order:

1. build the snapshot (adapter supplies `now`, net liq, MLL floor, observed trades)
2. `state = roll_session(...)` — **miss this and the session anchor goes stale**
3. `decision = evaluate(...)`
4. `state = apply_decision(...)` — miss this and a halt is not recorded
5. on a fill, `state = record_trade(...)` — miss this and the 2-trade budget never binds
6. persist state; set `last_reconcile` only after really querying positions

Steps 2, 4 and 5 fail *silently* if forgotten, so each now has a detector:
`STALE_SESSION_ANCHOR` (halt), `SESSION_HALTED`, and `TRADE_COUNT_DRIFT`
(the adapter counts real entries from the fill history and the governor
budgets against the higher of the two). **Detection is a backstop, not a
licence to skip the call.**

Why the anchor check is a halt and not a refusal: a stale anchor measures
session P&L from the wrong day. A session truly down $250 can read as +$150,
so the loss limit, the profit target and everything else derived from
`session_pnl` all go quiet at once. There is no safe way to continue.

## Market calendar

`src/market_calendar.py` is a data table of CME equity-index holidays and
early closes for 2026–2027. Without it the governor would happily return
CONTINUE on a Saturday morning.

- Weekend or holiday → `MARKET_CLOSED`, entry refused.
- Early close (13:00 ET): Topstep moves the flat deadline to 15 minutes before
  the close and we take our usual 15 on top, so the flatten becomes **12:30**.
- Dates outside coverage fail **closed**, so an unmaintained calendar costs a
  missed session rather than an unwatched position.

**The table is an unverified best reconstruction.** `CALENDAR_VERIFIED` is
`False` and a test holds it there deliberately. Check every row against
CME's published calendar before trading real size, then flip it.

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