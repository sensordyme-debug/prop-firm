# ProjectX Gateway API — verified reference

Verified **2026-09-12** against the official documentation at
`gateway.docs.projectx.com`, plus introspection of the installed
`project-x-py 4.3.0` package. Anything not confirmed at one of those two
sources is marked UNVERIFIED and must fail closed in code.

**No credentials appear in this file, and none ever should.**

TopstepX is a commercial deployment of the ProjectX Gateway, so the base host
is `api.topstepx.com`.

---

## 1. Authentication

```
POST https://api.topstepx.com/api/Auth/loginKey
```

Request:

| Field | Type | Notes |
|---|---|---|
| `userName` | string | Platform **username** — not the email, not the account id. Case-insensitive. |
| `apiKey` | string | Generated in the trading platform's settings. |

Response:

| Field | Type | Notes |
|---|---|---|
| `token` | string | Session token, used as a **bearer** token thereafter |
| `success` | bool | |
| `errorCode` | int | `0` on success |
| `errorMessage` | string \| null | |

- Token is valid **24 hours**. One token serves all REST calls and realtime
  connections; there is no per-request login.
- After expiry, requests fail with **HTTP 401**.
- A "Validate Session" endpoint exists for refreshing before expiry.

### The failure mode that matters

> A failed login returns **HTTP 200** with `success: false`.

Only a malformed request (missing `userName`/`apiKey`) returns HTTP 400. So a
client that checks only the HTTP status will treat a **rejected login as a
success** and carry on with no token. Any adapter must check `success`, not
the status code.

Known error codes: `3` = invalid credentials, `7` = agreements not signed.

---

## 2. Rate limits

| Endpoint | Limit |
|---|---|
| `POST /api/History/retrieveBars` | **50 requests / 30 seconds** |
| All other endpoints | **200 requests / 60 seconds** |

Exceeding either returns **HTTP 429**. The documentation's guidance is to
reduce frequency and retry after a delay — back off, never retry hot.

These match the figures already recorded in CLAUDE.md.

---

## 3. Endpoints in the installed SDK

Extracted from `project_x_py 4.3.0` source — the complete set:

```
/Auth/loginKey        /Auth/logout          /Auth/validate
/Account/search
/Contract/available   /Contract/search      /Contract/searchById
/History/retrieveBars
/Order/place          /Order/modify         /Order/cancel
/Order/search         /Order/searchById     /Order/searchOpen
/Order/v2/query
/Position/searchOpen  /Position/closeContract
/Position/partialCloseContract
/Trade/search
/Status/ping
```

Nothing else exists. In particular there is **no endpoint of any kind that
returns the trailing Max Loss Limit** — see §7.

---

## 4. Order placement

```
POST https://api.topstepx.com/api/Order/place
```

| Field | Type | Notes |
|---|---|---|
| `accountId` | int | required |
| `contractId` | string | required |
| `type` | int | `1` Limit, `2` Market, `4` Stop, `5` TrailingStop, `6` JoinBid, `7` JoinAsk |
| `side` | int | `0` Bid (**buy**), `1` Ask (**sell**) |
| `size` | int | required |
| `limitPrice` | decimal | Limit orders |
| `stopPrice` | decimal | Stop orders |
| `trailPrice` | decimal | TrailingStop only; an absolute **price level**, not a distance |
| `customTag` | string | unique within the account |
| `stopLossBracket` | object | `{ticks, type}` |
| `takeProfitBracket` | object | `{ticks, type}` |

Response: `{orderId, success, errorCode, errorMessage}`.

Side encoding matches `project_x_py.OrderSide` (BUY=0, SELL=1), which is what
`governor_adapter.SIDE_BUY` already assumes.

### 4.1 Native brackets exist — but are gated by an ACCOUNT SETTING

This is the most consequential finding of the verification pass, because
CLAUDE.md design constraint 1 depends on it.

> "Each account has a bracket mode, set in the trading platform under
> **Settings → Risk Settings**. In **Position Brackets** mode (the default),
> brackets are managed by the platform and **cannot be submitted**; in
> **Auto OCO Brackets** mode, `stopLossBracket` and `takeProfitBracket` are
> accepted and attach to each order."

Two consequences:

1. **An operational prerequisite nobody had recorded.** On a default account,
   sending `stopLossBracket` will not give us a broker-side bracket. The
   account must be switched to **Auto OCO Brackets** before the architecture
   in CLAUDE.md constraint 1 is even possible. This must be verified on the
   live account before execution is ever enabled — it is on the Stage 5
   checklist for that reason.

2. **Brackets are expressed in TICKS, not prices.** The strategy reasons in
   prices (range midpoint, ATR). Conversion happens at the adapter boundary,
   and the rounding direction must be conservative: a stop rounded the wrong
   way is a wider loss than intended. `OrderIntent` therefore carries prices,
   and the adapter converts.

UNVERIFIED: whether a rejected bracket fails the whole order or silently
places a naked entry. **A naked entry is the exact failure CLAUDE.md
constraint 1 exists to prevent**, so until this is observed on a real account
the executor must verify protection exists after every entry rather than
assume it.

---

## 5. Market data

```
POST /api/History/retrieveBars
```

Exposed by the SDK as `ProjectX.get_bars(symbol, days, interval, unit, limit,
partial, start_time, end_time) -> polars.DataFrame`.

UNVERIFIED, and all blocking for strategy work (FIRM_RULES.md):

- whether a bar timestamp labels its **open or its close**
- whether times are **exchange-local or UTC**
- how far back history actually goes
- whether bars are RTH-only or include the full Globex session

`BarSeries` refuses `BarTimestamp.UNKNOWN`, and `data.load_csv` requires both
the convention and the source timezone as explicit arguments, so none of these
can be silently assumed. `connection_test.py` and `data --probe` report the
evidence for each.

---

## 6. Account and positions

`GET`/`POST` via the SDK:

- `client.get_account_info() -> Account` — **synchronous**, unusually
- `client.list_accounts()`, `client.search_open_positions()`,
  `client.get_positions()`, `client.search_trades(start_date, end_date)`
- `client.get_instrument(symbol) -> Instrument`

Model shapes (introspected):

```
Account     id, name, balance, canTrade, isVisible, simulated
Position    id, accountId, contractId, creationTimestamp, type, size,
            averagePrice, contractDisplayName
Instrument  id, name, description, tickSize, tickValue, activeContract, symbolId
Order       id, accountId, contractId, creationTimestamp, updateTimestamp,
            status, type, side, size, symbolId, fillVolume, limitPrice,
            stopPrice, filledPrice, customTag
Trade       id, accountId, contractId, creationTimestamp, price,
            profitAndLoss, fees, side, size, voided, orderId, commissions
```

`PositionType`: `0` undefined, `1` long, `2` short.

### Net liquidation is not exposed

`Account` carries `balance` only. Net liq must be derived as
`balance + unrealised P&L`. Reading `balance` alone runs, returns a plausible
number, and silently violates the rule the MLL is enforced against — see
CLAUDE.md §Verified SDK behaviour.

The realtime `AccountUpdatePayload` declares optional `equity` and `margin`.
If `equity` is populated it is likely a true net liquidation and would beat
our derivation. UNVERIFIED until the realtime feed is live.

---

## 7. The Max Loss Limit is not available from the API

Swept every model, all 21 endpoints, and the websocket payloads. Nothing
returns the trailing floor. `RiskConfig` and `stats_types.max_loss_limit` are
**client-side settings we would be choosing ourselves**, not the firm's
figure.

`src/mll_tracker.py` reconstructs it from persisted end-of-day balances and
fails closed when that state is missing, stale or unparseable. See
FIRM_RULES.md for the rule and Topstep's own worked example.

---

## 8. Known failure modes to design against

| Failure | Behaviour | Our response |
|---|---|---|
| Bad credentials | HTTP **200**, `success: false` | check `success`, never the status |
| Expired token (>24h) | HTTP 401 | re-authenticate, then reconcile before trading |
| Rate limit | HTTP 429 | back off; never retry hot |
| Timeout on `Order/place` | no response | order state **UNKNOWN** — never assume it failed, never resubmit |
| Bracket mode wrong | bracket not attached | verify protection after entry; never assume |
| Contract rolled | stale front month | `contracts.front_month()` resolves the lead |

The timeout row is the dangerous one. A client timeout on order placement does
**not** mean the order did not reach the exchange. Treating it as a failure and
resubmitting is how duplicate positions happen, which on a $2,000 trailing
drawdown is unrecoverable. The order state machine has an explicit `UNKNOWN`
state that requires reconciliation against the broker, never a retry.

---

## 9. What is NOT supported / NOT verified

- No backtesting, simulation or strategy framework — it is a broker API. The
  SDK does ship reusable `utils.portfolio_analytics` metrics and ~40
  indicators, which we use.
- No trailing MLL figure (§7).
- No net liquidation field on the REST account (§6).
- Bar timestamp semantics, history depth, session filtering (§5).
- Bracket rejection behaviour (§4.1).
- Whether `AccountUpdatePayload.equity` is populated (§6).

Every one of these has a fail-closed guard in code. None is assumed.
