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
| **Engine validated** | ✅ **YES** | 434 tests; hand-computed P&L reproduced to the cent; 65 injected mutations all fail the suite |
| **Strategy validated** | ❌ **NO** | ORB v0.1 is a hypothesis. Never run on real data. Zero real trades. |
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
Strategy (ORB v0.1)             pure; proposes only; knows no broker
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
| `strategies/orb.py` | ORB v0.1 — **a hypothesis** |

---

## Install

```bash
py -V:3.13 -m venv .venv          # Python 3.13; the SDK requires 3.12+
.venv/Scripts/python -m pip install -r requirements.txt pytest
cp .env.example .env              # placeholders are detected as "no credentials"
```

## Run

```bash
python -m pytest                              # 434 tests, all offline
python -m src.cli config                      # effective config, secrets redacted
python -m src.cli connection-test             # read-only API diagnostic
python -m src.cli data --probe MNQ            # how deep is the history?
python -m src.cli backtest --csv bars.csv --convention CLOSE --tz UTC
python -m src.cli monte-carlo --csv bars.csv --convention CLOSE --tz UTC
python -m src.cli mll --seed --mll 48000
python -m src.cli compliance
```

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
- 434 tests, 65 mutations caught

Not cleared:
- **Stage 0** — no API key yet
- **Stage 1** — connection test has never run
- **Stage 2** — history depth unknown; we may need vendor data
- **Stage 4** — ORB has never seen real data
- **Stage 5** — no dry-run runtime yet

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

## A note on what this repository does not claim

ORB v0.1 has produced no trades and has no measured edge. The acceptance gates
in ROADMAP Stage 4 exist to reject it, and most candidate strategies do not
clear such gates — that is the normal outcome, not a failure.

Nothing here is ready for live trading, and this README will not say otherwise
until every gate has actually been demonstrated.
