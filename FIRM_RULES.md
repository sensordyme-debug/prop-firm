# Topstep firm rules — the authoritative reference

Every number here is tagged with how well it is known. **Never treat a DERIVED or
UNVERIFIED value as fact in code without a fail-closed guard.**

- `VERIFIED` — read from Topstep's or ProjectX's own documentation
- `DERIVED` — computed from a verified rule
- `UNVERIFIED` — from secondary sources, or assumed. Must fail closed.

Rules change without notice. Re-check anything load-bearing against the dashboard
and Topstep support before it governs real size.

**Verification pass — 2026-09-12.** The account limits, MLL mechanics, payout
paths and caps below were checked against Topstep's own help centre and the
wording quoted verbatim. Everything that survived that check is marked
VERIFIED with its source. CME's own calendar page timed out again, so the
holiday table remains cross-checked against secondary sources only.

## Account: Trading Combine 50K

| Item | Value | Status |
|---|---|---|
| Profit target | $3,000 | VERIFIED |
| Max Loss Limit ("the One Rule") | $2,000 | VERIFIED |
| Initial MLL floor | $48,000 | VERIFIED |
| Daily Loss Limit (Responsible Trading Advantage) | $1,000 | VERIFIED |
| Consistency target (Combine) | 55% of the **profit target** | VERIFIED |
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

Verbatim from Topstep's *What is the Maximum Loss Limit* article:

> "You start a 50K Trading Combine. Balance: $50,000. MLL: $48,000. You make
> $500 on day 1, balance rises to $50,500, MLL trails up to $48,500."

> "Once it reaches your starting balance, it locks permanently."

Note the subject of the second quote: it is the **MLL** that reaches the
starting balance, not the account. So on a 50K the floor locks at **$50,000**,
which happens when the account reaches **$52,000**. (A summary of that page
paraphrased it as locking at $48,000; the verbatim text does not say that, and
the worked example above is inconsistent with it.)

Both lines are pinned as tests in `tests/test_mll_tracker.py`:
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

| Path | Requirement | Cap (50K, base) | Cap (50K, RTA on) |
|---|---|---|---|
| Standard | 5 winning days of $150+ net | $2,000 | **$4,000** |
| Consistency | 3 trading days with ≥1 trade each, best day ≤40% of total net profit | $3,000 | **$6,000** |

VERIFIED from the Topstep Payout Policy article. The Responsible Trading
Advantage (Daily Loss Limit) **doubles both caps** — it is free, so leave it on.
Secondary sources quoting a $5,000 standard cap are wrong.

- Minimum request $125. Each request also limited to 50% of balance.
- Profit split 90/10.
- A winning day is **$150+ Net P&L**, and days **lock in at 4:00 PM CT**
  (17:00 ET) — one hour before the 18:00 ET session boundary this repo uses for
  risk accounting. Harmless for us, since we are flat by 15:55 ET and take no
  entries after 11:30 ET, but the two boundaries are not the same instant.
- After the first payout, the Standard path also requires at least $0.01 of
  positive net profit since the last payout; the first payout is exempt.
- Live Funded Account payouts are **not capped**.
- **After every payout the MLL resets to $0 permanently** — the floor locks at the
  starting balance and can never fall below it again. The winning-day counter
  restarts at the same moment.
- That reset is worth taking early even on a small payout: it converts the floor
  from trailing to fixed. `mll_tracker` will need a state transition for it.

## Consistency — the rule you break by winning

Two different rules, measured against two different denominators. Conflating
them gives the wrong answer at both stages.

| Stage | Rule | Denominator | Ceiling on a 50K |
|---|---|---|---|
| Combine | best single day < 55% | the **$3,000 profit target** | $1,650 |
| Payout (Consistency path) | largest single day ≤ 40% | **total net profit** | 40% of whatever you have made |

Verbatim from the Trading Combine Parameters article:

> "Meet the Consistency Target — your best single day should stay below 55% of
> your Profit Target to avoid increasing your Consistency Target."

> "You can pass in as few as two days, but keep your best day below 55% of your
> Profit Target."

So there is no minimum-trading-days rule as such; two days is simply the fewest
the 55% rule permits.

**The arithmetic that should shape the strategy.** Rearranging the payout rule:
with a best day of `B`, total net profit must reach `B / 0.40` — **2.5× your
best day** — before a single dollar can be withdrawn. One outsized session does
not merely fail to help, it moves the finish line for every session after it:

| Best day | Total profit needed before any payout |
|---|---|
| $500 | $1,250 |
| $1,000 | $2,500 |
| $1,500 | $3,750 |

That is the real argument for a daily profit cap. Stopping at $500 is not
leaving money on the table; it is keeping the ratio reachable while the MLL —
which never moves down — is still trailing.

`src/compliance.py` implements both rules, reports the gap, and
`Config.__post_init__` refuses a daily target above the Combine ceiling.

## Prohibited

VERIFIED from *Prohibited Trading Strategies at Topstep* — the full list:

1. Account Stacking
2. Intentionally Depleting a Live Funded Account
3. Violating Topstep's Terms of Use
4. **Using Unfair Technology**
5. Trading Outside Real Market Behavior
6. Trading Outside the Best Bid or Offer
7. **Trading Maximum Position Size into Major News Events**

Plus, from *Prohibited Conduct*: coordinated trading with others, cross-account
hedging, and VPN / proxy / TOR / geo-obfuscation.

### "Using Unfair Technology" does NOT ban this bot

The clause is qualified, and the qualifier is the whole rule:

> "Using software, AI, ultra-high speed systems, or mass data entry **that
> manipulates, abuses, or provides an unfair advantage on the platform**."

The named examples are all simulator abuse: *"Running scalping algorithms
designed to exploit unrealistic SIM fills"* and *"Making hundreds of rapid
trades to take advantage of preferential queue position in SIM"*, with
*"average durations measured in seconds, not minutes."*

That is a ban on exploiting the SIM environment, not on automation — which
Topstep separately and explicitly permits via the ProjectX API (see Automation
policy above). Read the headline without the qualifier and you would conclude
this whole project is prohibited; it is not. But note how close the described
behaviour is to a naive scalper, and stay far from it:

| Their prohibited pattern | This system |
|---|---|
| exploits unrealistic SIM fills | backtest assumes the **stop** fills on any ambiguous bar |
| hundreds of rapid trades | max **2 entries per session** |
| durations in seconds | 5-minute bars, holds of minutes |
| max position size into news | **2 of 50** permitted micros — 4% of max |

### News trading

Not banned. What is banned is *"Purposefully trading your full Maximum Position
Size directly into a scheduled major news event."* At 2 contracts against a
50-micro cap we are nowhere near maximum size, so the rule does not bind — but
do not raise size into a release to "make it back".

### Inactivity

- Express Funded Account: closed after **30 days** without trading activity.
- Live Funded Account: permanently closed after **90 days**.

One trade inside the window keeps it alive. Relevant from Stage 8 onward, and
worth an alarm rather than a memory.

"Sole ownership" concerns other traders, not tools. Building with AI assistance is
normal and does not conflict with it. Keep the repo private, don't share or sell
the strategy, don't run it at another firm.

## UNVERIFIED — handle with care

- **CME holiday calendar.** Re-checked 2026-09-12 against three independent
  secondary sources. They agree on **which dates are special**; they still
  disagree on classification and times (12:00 vs 12:15 CT, and whether Memorial
  Day / Juneteenth / Labor Day / Thanksgiving are full closures or half-days).
  CME's own page timed out again. We refuse to trade on any holiday date at
  all, so the disagreement cannot reach a decision.
  - **Fixed: July 2 2026 was missing** — the day before the observed
    Independence Day holiday, confirmed as an early close by two sources. A
    missing date is the dangerous kind: we would have traded it.
  - **2027 has been removed from coverage entirely.** Those rows were never
    checked against anything. All 2027 dates now fail closed as
    OUT_OF_COVERAGE; the draft rows are kept in `DRAFT_2027_UNVERIFIED` as a
    starting point for that pass and are never read by `classify()`.
  - `CALENDAR_VERIFIED` stays `False` (not checked against CME directly);
    `CALENDAR_DATES_CROSS_CHECKED_2026` is `True`.
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
