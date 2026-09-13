# Topstep MNQ — quantitative futures trading platform

> ## ORDER TRANSMISSION IS DISABLED
> This system **cannot place an order.** Not "is configured not to" — the
> read-only broker adapter has no order method, and the only function that can
> grant execution capability raises unconditionally. Enabling execution is a
> reviewed code change after the Stage 5 gates are demonstrated, never a
> configuration edit.

Research and execution architecture for a **Topstep 50K Trading Combine** on
**MNQ**, against the **ProjectX / TopstepX** API.

---

## Four claims, deliberately kept separate

Conflating these is how people talk themselves into trading something untested.

| Claim | Status | Evidence |
|---|---|---|
| **Engine validated** | ✅ **YES** | 504 tests; hand-computed P&L reproduced to the cent; 65 injected mutations all fail the suite; ruff and mypy clean |
| **Strategy validated** | ❌ **NO** | 9 candidate strategies, all hypotheses. None run on real data. Zero real trades. |
| **Live execution validated** | ❌ **NO** | No order has ever been transmitted; the capability does not exist |
| **Profitable** | ❌ **UNKNOWN** | No claim is made, and none is supported |

---

## What this is

An event-driven platform built around the specific rules of a Topstep Combine,
not a generic trading bot. The rules are the architecture: the trailing Max
Loss Limit, the 18:00 ET session boundary, the consistency ratios, and the
flatten deadline all appear as code with tests, not as comments.

```
Historical / live data
        ↓
Normalisation + validation      timestamp convention must be DECLARED
        ↓
Backtester                      no lookahead, costs, adverse slippage
        ↓
Strategy (9 candidates)         pure; proposes only; knows no broker
        ↓
Risk Governor                   FINAL AUTHORITY; nothing overrides it
        ↓
Execution Intent                prices, validated, stop mandatory
        ↓
ExecutionBroker interface       requires a capability that cannot be obtained
        ↓
ProjectX adapter                READ-ONLY — no order method exists
```

---

## Architecture

| Module | Role |
|---|---|
| `config.py` | One typed config object, validated at startup, fails closed |
| `governor.py` | **Final authority.** Pure: no I/O, no SDK, no clock |
| `compliance.py` | Combine 55% and payout 40% consistency — the rules you break by *winning* |
| `mll_tracker.py` | Reconstructs the trailing MLL floor; the API does not expose it |
| `market_calendar.py` | Holiday dates; we stand aside on all of them |
| `contracts.py` | Quarterly roll, front month, expiry |
| `backtest.py` | Replay engine with the **real governor in the path** |
| `performance.py` | Full metric suite; unavailable stats say so rather than inventing |
| `research.py` | Walk-forward, splits, Monte Carlo, sensitivity |
| `data.py` / `marketdata.py` | Load, validate, normalise; never guesses a convention |
| `execution/` | Broker-neutral models, order state machine, capability boundary |
| `brokers/projectx.py` | The only module that speaks ProjectX |
| `reconciliation.py` | Broker truth wins; discrepancies fail closed |
| `strategies/` | Registry + 9 testable candidates + 9 declared pending data |
| `connection.py` | Connection state machine; only READY may transmit |
| `execution/protection.py` | Verifies a position is actually protected |
| `execution/idempotency.py` | Deterministic tags + single-instance lock |
| `session_state.py` | Restart survival; never authoritative over the broker |
| `observability.py` | Structured JSON logs, correlation IDs, redaction |
| `runtime.py` | Dry-run runtime; its broker has no order method |
| `readiness.py` | The final gate; can never emit LIVE-TRADING-READY |

---

## Install

```bash
py -V:3.13 -m venv .venv          # Python 3.13; the SDK requires 3.12+
.venv/Scripts/python -m pip install -r requirements.txt pytest
cp .env.example .env              # placeholders are detected as "no credentials"
```

## Run

```bash
python -m pytest                     # 504 tests, all offline
ruff check src tests && mypy         # static checks

python -m src.cli config-check       # [READ-ONLY] credential PRESENCE only
python -m src.cli readiness          # [READ-ONLY] the final safety gate
python -m src.cli research           # [READ-ONLY] strategy catalogue
python -m src.cli connection-test    # [READ-ONLY] needs credentials
python -m src.cli data --probe MNQ   # [READ-ONLY] how deep is the history?
python -m src.cli reconcile          # [READ-ONLY] local state vs broker
python -m src.cli backtest --csv bars.csv --convention CLOSE --tz UTC
python -m src.cli monte-carlo --csv bars.csv --convention CLOSE --tz UTC
python -m src.cli dry-run            # [DRY RUN] decides, transmits nothing
python -m src.cli mll --seed --mll 48000
```

Every command is READ-ONLY or DRY-RUN. **There is no LIVE command**, because
there is no code path that transmits.

### API-KEY-READY is not LIVE-TRADING-READY

`readiness` can emit exactly two classifications: `NOT READY` or
`API-KEY-READY`. `LIVE-TRADING-READY` is not a value it can return.

**API-KEY-READY** means credentials can be added safely and read-only
validation can begin. It does **not** mean the system may trade.

No test requires credentials. No command can transmit an order.

## Configure ProjectX

Order matters — a key generated before linking authenticates but resolves to no
tradable account:

1. Buy the Combine 50K with **Responsible Trading Advantage ON** (free; it
   *doubles* both payout caps)
2. Create the ProjectX account → subscribe to API Access
3. **Link it to TopstepX**
4. *Then* generate the key in TopstepX → Settings
5. Put it in `.env`. `PROJECT_X_USERNAME` is the **username**, not the email.

---

## Current status

**Engine validated. Nothing else.**

Cleared:
- Risk governor, MLL tracker, calendar, contracts, compliance — all tested
- Backtester proved against a hand-computed fixture, to the cent
- Read-only ProjectX adapter, reconciliation, order state machine
- Connection state machine, bracket verification, duplicate protection
- Dry-run runtime, structured logging, session persistence
- 504 tests, 65 mutations caught, ruff and mypy clean

Not cleared:
- **Stage 0** — no API key yet
- **Stage 1** — connection test has never run
- **Stage 2** — history depth unknown; we may need vendor data
- **Stage 4** — no strategy has seen real data
- **Stage 5** — dry-run runtime exists but has never consumed a live feed

---

## What is disabled, and what has not been verified

**Disabled by construction:**
- Order transmission — `grant_execution()` always raises; the adapter has no order method
- `LIVE_TRADING_ENABLED=true` — rejected by config validation

**Unverified, each with a fail-closed guard:**

| Unknown | Guard |
|---|---|
| Bar timestamps: open- or close-labelled? | `BarSeries` refuses `UNKNOWN`; loaders require it explicitly |
| Bar timezone: exchange-local or UTC? | Naive timestamps rejected without a declared zone |
| ProjectX history depth | Unknown until `--probe` runs |
| CME holiday dates | Cross-checked 2026 only; 2027 out of coverage and fails closed |
| Bracket account mode | **Default mode rejects submitted brackets** — see `docs/PROJECTX_API.md` §4.1 |
| `AccountUpdatePayload.equity` | Not read; net liq is derived |

---

## Documentation

| File | Contents |
|---|---|
| `FIRM_RULES.md` | **Authoritative on every number.** Tagged VERIFIED / DERIVED / UNVERIFIED |
| `DECISIONS.md` | Settled choices and what was rejected, with reasons |
| `ROADMAP.md` | Staged plan and gates |
| `CLAUDE.md` | Architecture and the caller contract |
| `docs/PROJECTX_API.md` | Verified API behaviour and failure modes |

---

## Research methodology

This is not an ORB bot. ORB is one hypothesis among nine implemented and nine
more declared. Every candidate runs the same pipeline, and each stage can
reject it:

```
DATA VALIDATION -> BASELINE BACKTEST -> COST STRESS -> IS/OOS
  -> WALK-FORWARD -> MONTE CARLO -> PARAMETER ROBUSTNESS
  -> REGIME ANALYSIS -> TOPSTEP CONSTRAINTS -> CROSS-STRATEGY COMPARISON
```

Guards that make the process honest rather than flattering:

- **Splits are chronological**, never shuffled — shuffling leaks the future.
- **Sweeps cap at 2 tunable parameters and 64 combinations**, and *raise*
  rather than truncate. Every extra free parameter buys in-sample performance
  that does not survive out of sample.
- **Sensitivity tables sort by parameter, not profit.** Sorting by outcome
  turns a sweep into an optimiser over one dataset.
- **A lone winner surrounded by failures is flagged FRAGILE.** A spike is not
  a region.
- **Statistics that cannot be computed honestly return `Unavailable` with a
  reason**, never a number.
- **Strategies needing data we lack are declared, not approximated.**

## What this repository does not claim

**BACKTEST PERFORMANCE DOES NOT GUARANTEE FUTURE PERFORMANCE.**

No strategy here has produced a single trade or has any measured edge. The
acceptance gates exist to *reject* candidates, and most do not clear such
gates — that is the normal outcome, not a failure.

**"No strategy currently validated" is a successful research result.**

Nothing here is ready for live trading, and this README will not say otherwise
until every gate has actually been demonstrated.
