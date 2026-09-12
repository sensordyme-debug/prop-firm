# Roadmap — from here to payouts

Stages are gated. **Do not start a stage until the previous gate is met.** The
gates exist because each one is a documented way these accounts die.

Current state: governor, market calendar, MLL tracker, connection test and the
backtest harness are built and unit-tested. No code has ever touched a live
account, and no strategy exists yet.

---

## Stage 0 — Account and key · BLOCKING EVERYTHING

Buy the Topstep Combine 50K ($49/mo, Responsible Trading Advantage ON — it's free
and doubles the later payout cap). Then, in this exact order: create a ProjectX
account, subscribe to API Access ($29/mo, $14.50 with code `topstep`), link it to
the TopstepX profile, generate the key in TopstepX → Settings. Generating before
linking does not work.

**Gate:** `.env` holds a real key.

---

## Stage 1 — Prove the connection

Run `python src/connection_test.py`.

**Gate:** it prints a real balance and real MNQ bars. Nothing downstream is
meaningful until this passes — every later stage assumes authenticated data.

Record from its output, into CLAUDE.md: whether `t` labels the bar's open or
close, whether times are exchange-local or UTC, and what the account object
actually returns. Guesses here shift every opening range by one bar.

---

## Stage 2 — Data

Run `python -m src.data --probe MNQ`.

**Gate:** you know the earliest available bar and the total 5-minute history.

- Years of depth → no external data is ever needed.
- ~90 days → source CME history from a vendor (Databento, FirstRate) as CSV into
  the same harness. Still not a second engine, just a second data file.

Then seed the MLL tracker from the figure TopstepX actually displays:
`python -m src.mll_tracker --seed --mll 48000`, and verify it daily.

---

## Stage 3 — Harness validation

Prove the backtester on synthetic bars with a hand-calculable case before trusting
it on real ones.

**Gate:** the harness reproduces a hand-computed P&L exactly, refuses to look
ahead, and can report which days the governor would have halted.

---

## Stage 4 — Strategy development · the honest stage

This is where the timeline stops being predictable.

Everything before this is engineering with a known answer. This stage is research
with an uncertain one. Realistic duration is **weeks to months**, and a material
fraction of candidate strategies never clear the gates at all. That is a normal
outcome, not a failure of execution.

Start from opening-range breakout on MNQ (range 09:30–09:45 ET, entry on a
5-minute close beyond it, stop at range midpoint or 1×ATR(14) capped at $80,
target 1.5R, max 2 trades, no entries after 11:30 ET).

**Acceptance gates — judge on drawdown, not profit:**
- Max dollar drawdown under **$1,200**
- Fewer than **5** consecutive losing days
- Positive expectancy over **200+** trades
- Zero governor halts caused by the strategy's own behaviour

**Reject anything failing these regardless of net return.** A strategy making
$8,000 a year that draws down $1,900 will kill this account before it pays out.

Tune **at most two parameters**. More is curve-fitting a dataset, not finding an
edge. If nothing clears the gates, the correct action is to try a different
strategy family — not to loosen the gates.

**Gate:** a strategy meeting all four criteria on out-of-sample data.

---

## Stage 5 — Dress rehearsal

`DRY_RUN=true` across a full Globex session. The system computes every decision
and logs the exact order it would have sent, transmitting nothing.

**Gate:** a clean overnight log — connected throughout, sane levels, no attempt to
trade outside its window, correct session roll at 18:00 ET.

Also required before going live:
- Windows Task Scheduler task survives a deliberate reboot
- A killed process restarts and reconciles rather than assuming flat
- The kill switch works
- Network pulled mid-session behaves correctly

---

## Stage 6 — Live on the Combine, 2 MNQ

`DRY_RUN=false`. Position size 2, hard-coded.

Each day: pre-open check, then hands off. No parameter changes mid-session, no
manual overrides, no "helping" it.

**Gate to increase anything:** ten consecutive sessions with zero manual
intervention, zero governor failures, one survived disconnect, and live results
within one standard deviation of backtest expectancy. If live is materially worse
than backtest, the backtest was fiction — go back to Stage 4.

---

## Stage 7 — Pass the Combine

$3,000 profit, best day ≤55% of target, MLL never touched. No minimum days.

Then choose a payout path. **Consistency** has both a higher cap ($6,000 vs
$4,000) and fewer required days (3 vs 5); it only adds a 40% best-day rule, which
suits a strategy built around a steady daily target.

**Gate:** Express Funded Account active.

---

## Stage 8 — First payout, taken early

Take it as soon as eligible even if small. Every payout resets the MLL to $0
permanently — the floor locks at the starting balance and can never fall below it
again. That structural change is worth more than the cash.

**Gate:** payout received, MLL locked.

---

## Stage 9 — Scale, by seats not size

Add capacity as additional accounts at proven size, not more contracts on one.
Drawdown per account stays constant while capacity grows.

Later seats belong at **Tradeify Select Flex and Bulenox** — both permit full
automation *and* permit VPS hosting, which Topstep withholds. Different firms also
means one firm's rule change or payout dispute cannot take the whole operation
offline.

Understand what this is: copying one algorithm across N accounts is **leverage,
not diversification**. Correlation is exactly 1.0. The chance of losing all of
them is not the chance of losing one raised to the Nth power — it is simply the
chance the strategy has a $2,000 drawdown, which every strategy eventually does.

Step size back down immediately on any three-day losing streak.

---

## Status log

Append one line per gate cleared, with the date and the evidence.

- [ ] Stage 0 — key in .env
- [ ] Stage 1 — connection test passed
- [ ] Stage 2 — history depth known, MLL seeded
- [x] **Stage 3 — harness validated · 2026-09-12**
      Evidence: `src/backtest.py` with 40 tests in `tests/test_backtest.py`.
      - Hand-computed fixture reproduced to the cent: two trades, +$78.18 and
        -$41.82, total **+$36.36** (`test_total_net_profit_to_the_cent`).
      - No-lookahead is structural: a strategy reaching for `bars[index + 1]`
        raises `LookaheadError` and fails the run
        (`test_a_peeking_strategy_fails_the_backtest`).
      - The real governor runs in the replay path; halts are reported with
        their reason codes (`test_governor_halts_are_recorded_with_reasons`,
        `test_report_lists_halts_with_reasons`).
      - Stop wins every both-touched bar (`test_a_bar_touching_both_stop_and_
        target_fills_the_stop`).
      CAVEAT: validated on **synthetic** bars only. Running it on real data is
      still gated on Stage 1, because bar timestamp semantics are UNVERIFIED —
      `BarSeries` refuses `BarTimestamp.UNKNOWN` rather than assuming one.
- [ ] Stage 4 — strategy meets all four gates
- [ ] Stage 5 — clean dry run, reboot survived
- [ ] Stage 6 — ten clean live sessions
- [ ] Stage 7 — Combine passed
- [ ] Stage 8 — first payout, MLL locked
- [ ] Stage 9 — second seat added
