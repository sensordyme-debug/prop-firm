# Decisions already made

Each of these was reasoned through and settled. They are not open questions.
If you believe one is wrong, say so and explain why — do not quietly work around it.

## Firm and platform

**Topstep, not Tradeify or Apex.** Screened 18 futures prop firms against one test:
can a bot enter and exit without a human in the loop? Apex, Take Profit Trader,
Alpha Futures, Phidias and Earn2Trade all prohibit it or allow semi-auto only.
Topstep is the only firm with an official documented REST API and written
permission to use bots on it.

Cost of that choice: no VPS. Orders must originate from a personal device, so the
bot runs on a dedicated always-on Windows laptop rather than a Chicago VPS.

**Python + ProjectX, not NinjaScript.** The API removes the need for C#, Windows-
only tooling and webhook bridges, and supports native gateway bracket orders — so
stops rest at the broker and survive the process dying.

**Combine first, not the funded account.** The Combine is simulated and costs $49.
A new strategy learns the same lessons there for a fraction of the downside.
(A separate Tradeify funded 50K exists and sits out until the strategy is proven.
It needs one manual trade per week or the account is lost to the idle rule.)

## Architecture

**Purity contract.** Decision logic is a pure function of its arguments — no
network, no SDK, no filesystem, no clock reads. `now` always arrives on the
snapshot. This is what makes every trip-wire testable at its exact boundary
without credentials. All I/O lives in adapters.

**The governor is authoritative.** The strategy proposes, the governor decides.
No order is transmitted without approval.

**Net liquidation, never realised P&L.** The firm enforces the MLL against net liq
including open positions, so any check against realised P&L is wrong by definition.

**Fail closed, always.** Missing state, stale state, unreconciled positions,
unknown session anchor — every one halts rather than estimating. The one rule
that permanently ends the account has its input reconstructed from our own
bookkeeping, so there is no safe default.

**Broker-side brackets on every entry.** Never a bare market order with the stop
living in the Python process.

## Rejected, with reasons

**QuantConnect LEAN.** Rejected twice, on different grounds.
First: it supports no ProjectX, TopstepX, Tradovate or Rithmic brokerage, so it
cannot execute on this account — meaning two implementations of one strategy and
a backtest that validates code we don't trade.
Second, and decisively even for research-only use: its bars come from a different
vendor with its own tick aggregation, bar timestamp convention and session
filtering. For a strategy whose signal is the high and low of the first three
5-minute bars, a one-bar labelling difference moves the range and changes every
trade. That part is not configurable. A backtest measuring a subtly different
thing is worse than no backtest, because it produces confidence instead of doubt.

**Any second backtesting engine.** One harness serves both jobs. Screening mode is
fast sweeps comparing candidates; validation mode puts the governor in the path
with pessimistic slippage and the acceptance gates. Same code, same data source.

**Trading on holiday half-days.** Three sources agree on the dates and disagree on
the times. Rather than depend on a contested number, refuse to trade on any
holiday date at all. Costs ~12 of ~250 trading days on a strategy taking 1–2
trades a day; removes an entire failure mode. Do not "optimise" this back in.

**Early-close time arithmetic.** Deleted for the same reason.

**`session_high_balance`.** Deleted. Nothing read it, and the MLL trails the EOD
close rather than an intraday high, so it had no future use. Dead state in a
safety file invites misplaced trust.

## Open questions, deliberately unresolved

- Whether ProjectX history goes back far enough for multi-year backtests.
- Whether `AccountUpdatePayload.equity` is populated.
- Bar timestamp semantics — open-labelled or close-labelled, exchange time or UTC.
- Whether the CME calendar rows are correct.

Each has a fail-closed guard until answered with real data.
