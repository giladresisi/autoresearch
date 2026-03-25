# System Upgrade Phases

Ordered implementation path from "baseline optimization complete" to "full live trading system".
Each phase is a deployable milestone — run it, validate it, then move to the next.

---

## Phase 0 — Research Completion (Exit Optimization)

**Goal:** Complete the optimization research cycle started in the global-mar24 worktree before
building any live system infrastructure. These steps produce a strategy with W/L ≥ 1.2 and
establish the enriched trade log that all subsequent diagnostic work depends on.

**Must be done:** after the global-mar24 worktree recommendations are finalized and before Phase 1.

**Source:** `../global-mar24/harness_upgrade.md` — iter21 conclusions and exit optimization plan.

---

### Phase 0.1 — Harness Prerequisites (no strategy logic changes)

These are blocking prerequisites for any exit experiment. They change what is measurable and
what the baseline even is. Captured in `.agents/plans/p0-price-fix-and-instrumentation.md`.

**P0-A: Fix `price_10am` → `price_1030am`**

`prepare.py` currently extracts the 9:30 AM Open (market open — worst possible intraday price
for a "wait for open volatility to settle" strategy). Switch to the Close of the 9:30 AM bar
(~10:30 AM, post-opening-volatility by definition). Rename the column throughout `prepare.py`,
`train.py`, and all test fixtures. Re-run iter21 config (`6ad6edd`) on corrected price first
to establish a new baseline — results will differ from all 27 prior iterations.

**P0-B: Add MFE and MAE to trades.tsv**

Track `max_price_seen` and `min_price_seen` during each position's hold period. At exit, compute
and write to trades.tsv:
- `mfe_atr = (max_price_seen − entry_price) / ATR14` — maximum favorable excursion in ATR units
- `mae_atr = (entry_price − min_price_seen) / ATR14` — maximum adverse excursion in ATR units

ATR units (not raw %) make MFE/MAE directly comparable to stop and trail thresholds.
Without this, it is impossible to distinguish "early false stops" from "true failures."

**P0-C: Add per-exit-type reporting to fold summary**

Add `exit_type` column to all trade records: `stop_hit`, `stall_exit`, `breakeven_stop`,
`trailing_stop`, `time_exit`, `partial`. Log count and avg P&L per exit type in the fold summary
so each run immediately shows which mechanism is responsible for avg win suppression.

**P0-D: Add R-multiple to trades.tsv**

`r_multiple = (exit_price − entry_price) / (entry_price − initial_stop)`. Log at exit.
A healthy momentum strategy shows winners clustering at +1.5–3.0R and losers at -1.0R.
If winners cluster near 0R, the breakeven trigger is the dominant suppressor. If losers
cluster at -0.3 to -0.6R, the stop is being hit too early.

**P0-E: Volume criterion redesign** ✅ Done (2026-03-25)

The backtested `vol_ratio` rule (`today_vol / vm30 >= 2.5`) is structurally wrong — `today_vol`
is unavailable pre-market (`= 0`) and only reaches a reliable reading well after the 10:30am
entry window. Replace with two prior-data-only checks:

| New rule | Condition | Rationale |
|---|---|---|
| **Rule 3a** — recent trend | `mean(vol[-5:]) / vm30 >= 1.0` | Confirms building institutional interest over the past week |
| **Rule 3b** — yesterday not dead | `vol[-2] / vm30 >= 0.8` | Rejects setups where the prior session itself was unusually quiet |

`today_vol` removed from entry logic entirely. Return dict gains `prev_vol_ratio` and
`vol_trend_ratio` (replacing `vol_ratio`). Applied before Phase 0.2 re-calibration so the
new volume rules are baked into the baseline.

---

### Phase 0.2 — Baseline Re-establishment After F1

**This step is required before any diagnostic analysis.** F1 (price fix) changes entry prices
on every trade — the existing 27-iteration results are calibrated against the wrong price and
cannot be used as a baseline for exit experiments.

**What to run:**
Run the iter21 config (`6ad6edd`) once against the corrected price data (P0-A applied) and
compare the output to the pre-fix baseline. This is a single backtest run, not a new
optimization loop.

**What to compare:**

| Metric | Pre-fix iter21 | Post-fix threshold | Action if exceeded |
|--------|---------------|-------------------|--------------------|
| Total trades | 116 | ±15 (101–131) | Re-check volume/SMA filters |
| min_test_pnl | -14.26 | ≥ -20 | Targeted re-tune (see below) |
| W/L ratio | 0.885 | ≥ 0.80 | Acceptable, proceed |
| Win rate | 69% | ≥ 63% | Acceptable, proceed |

**If all metrics stay within threshold:** the iter21 parameters transfer to the corrected-price
baseline. Document the new baseline metrics and proceed to Phase 0.3.

**If trade count or min_test_pnl falls outside threshold:** run a targeted 3–4 iteration
re-sweep of the two most price-sensitive parameters only — the volume threshold (currently
2.5×) and the SMA20 slope tolerance (currently 0.5%). Do not re-run the full 27-iteration
entry filter search. All dead ends from that run (ATR buffer, breakout window, pivot lookback,
RSI bounds, resistance ATR) are confirmed dead regardless of entry price and should not be
revisited. A targeted re-sweep takes 4–6 runs (~1 hour), not 27.

**Why a full re-optimization is not needed:** The relative filters (SMA ratios, volume ratio,
slope tolerance) are computed entirely from daily close history, not from the intraday price.
The absolute filters (price > sma50, price > high20) will shift slightly — 10:30am price is
typically closer to the day's range midpoint — but the direction of the shift is predictable:
entries that barely cleared the threshold at 9:30am open may now fail or pass at 10:30am close.
This affects the boundary cases, not the bulk of trades.

---

### Phase 0.3 — Diagnostic Analysis (no code changes, read the data)

Run Phase 0.2 first. Then run the re-established iter21 baseline once with all P0 harness
changes active (MFE/MAE, R-multiple, exit-type counts). This produces the enriched trade log
that all of Phase 0.4 depends on. **Do not skip this step** — without it you are guessing which
exit mechanism to change, and the wrong guess costs a full optimization iteration.

**How to perform the analysis:**

Open `trades.tsv` (or write a short notebook/script against it) and compute the following.
All metrics should be segmented by `fold` and by `winner` (pnl > 0) vs `loser` (pnl ≤ 0).

**Q1: What fraction of losing trades have `mfe_atr > 1.0`?**

Filter to rows where `pnl ≤ 0`, compute `sum(mfe_atr > 1.0) / count`. This directly answers
whether the position was ever meaningfully in-profit before reversing.

- **> 25%:** Exits are giving back real gains. The breakeven stop or stall exit is the culprit.
  → Run **E1 first** (delay breakeven stop).
- **10–25%:** Mixed signal. Both exit mechanics and entry quality contribute.
  → Run **E1 and E5 in parallel** on separate branches.
- **< 10%:** Most losses are true failures — price moved against position from entry.
  Exit loosening will not help. → Skip E1/E2/E3. Focus on **E6** (tighter entry quality gate)
  or accept the loss profile and focus only on growing avg win via E4.

**Q2: What is the avg MFE of winning trades vs their exit price?**

For winners: compute `avg(mfe_atr)` and `avg((exit_price - entry_price) / atr14)`. The gap
between the two is the "unrealized potential" being left in winners.

- **Gap > 1.0 ATR on average:** Winners are exiting well before their peak. The stall exit or
  breakeven stop is cutting them early. → Prioritize **E2** (loosen stall exit) and **E3**
  (trailing stop with wider activation).
- **Gap < 0.5 ATR:** Winners are exiting close to their peak — current exits are efficient.
  The W/L problem is in losers, not winners. → Focus on **E5** (SMA20 soft exit to shrink
  avg loss) rather than exit loosening.

**Q3: Which exit type dominates losing exits?**

Group losing trades by `exit_type`, compute count and avg pnl per group.

- **`breakeven_stop` > 20% of all exits AND those exits have avg pnl ≈ 0:** The breakeven
  trigger is creating a large population of "stranded" trades — moved 1.5 ATR favorably,
  got locked at 0, then closed flat. → **E1 is the highest-priority experiment.**
- **`stall_exit` losers > `stop_hit` losers:** The stall exit is firing on trades that haven't
  confirmed failure yet (they exit the stall at a small loss or near 0, then price continues
  moving up). → **E2 (loosen stall exit) combined with E5 (SMA20 soft exit as safety valve).**
- **`stop_hit` is the dominant loser exit type AND mfe_atr < 0.5 for most:** Losses are clean
  stop hits with no meaningful prior favorable movement. No exit change helps. → Focus on
  **E6** (tighter entry gate) or accept the loss profile.

**Q4: What is the shape of the R-multiple distribution?**

Bucket `r_multiple` into ranges: (< -1.0), (-1.0 to -0.5), (-0.5 to 0), (0 to 0.5),
(0.5 to 1.5), (> 1.5). Count trades in each bucket.

- **Large mass in (-0.5 to 0) bucket:** These are breakeven-stop exits. Confirm with Q3.
  The breakeven trigger is generating an artificial population. → **E1.**
- **Clean bimodal shape** — mass near -1.0R and mass near +1.5R+, almost nothing near 0:
  The exit mechanics are operating correctly. The W/L problem is structural (stops being hit
  cleanly at -1R while winners only run to +1.5R). → **E3 (wider trailing stop activation)
  or E4 (partial exit at resistance)** to push the winner bucket further right.
- **Long left tail** (many trades at -1.5R, -2.0R): The hard stop is being breached, or
  gaps are causing overshoots. This is a data/slippage issue, not exit mechanics.

**Decision matrix — which experiment to run first:**

| Q1 result | Q3 result | Run first |
|-----------|-----------|-----------|
| MFE > 1 ATR on > 25% of losers | breakeven_stop dominant | E1 |
| MFE > 1 ATR on > 25% of losers | stall_exit dominant | E2 + E5 |
| MFE < 10% of losers | any | E6, then E4 |
| Winner MFE gap > 1 ATR | any | E3 |
| Winner MFE gap < 0.5 ATR | stop_hit dominant | E5 first |

The answers dictate which Phase 0.4 experiment to run first.

---

### Phase 0.4 — Exit Experiments (ordered by Phase 0.3 decision matrix)

**Each experiment is a single backtest run (~10 min) on the corrected-price iter21 baseline.**
No new optimization loop is needed — exit parameters are orthogonal to the entry filters that
were exhaustively swept in the global-mar24 run. The entry space is solved; only the exit space
is being explored here.

**Keep/discard rule per experiment:**
- Keep if: W/L improves by ≥ 0.05 AND min_test_pnl ≥ −17 AND total trades ≥ 100
- Discard if: min_test_pnl < −17 OR W/L does not improve OR win rate drops > 5pp

Run experiments in the order dictated by Phase 0.3. The ordering below assumes the most common
expected outcome (breakeven stop is the primary suppressor based on prior iteration history).
Re-order if Phase 0.3 points elsewhere.

---

**E1: Delay the breakeven stop trigger (1.5 ATR → 2.0 ATR)**

*Run first if: Q1 > 25% of losers have MFE > 1 ATR, OR Q3 shows breakeven_stop > 20% of exits.*

In `manage_position`, find the line that moves the stop to entry when unrealized gain reaches
1.5 × ATR. Change the activation threshold from 1.5 to 2.0. No other change.

Mechanism: the current trigger fires at +1.5 ATR, locking the stop at entry. If price then
consolidates between +1.5 and +2.0 ATR (normal behavior in a continuing trend), the trade
gets stopped at breakeven — $0 P&L, not a winner. Raising the trigger to 2.0 ATR means the
trade must reach stronger momentum before the stop locks, giving it room to consolidate and
continue. Expected impact: avg win up (fewer 0R exits counted as breakeven stops), avg loss
slightly up (some trades that previously locked at 0 now take the full stop loss), net W/L
improvement if the 0R population is material. Does not affect fold1/2 min_test_pnl unless
the correction-period trades had favorable excursions > 1.5 ATR before failing (check in MFE
data first).

---

**E2: Loosen the stall exit (5 calendar days / 0.5 ATR → 7 days / 0.7 ATR)**

*Run if: Q2 shows winner MFE gap > 1 ATR, OR Q3 shows stall_exit losers > stop_hit losers.*

In `manage_position`, find the stall-exit logic (currently: exit if days_held ≥ 5 and price
moved < 0.5 ATR from entry). Change to 7 days and 0.7 ATR. The tighter direction (iter5,
e644db6) was catastrophic — that experiment confirmed the floor; this experiment tests the
ceiling in the other direction.

Mechanism: slow-trending winners that consolidate for 5–6 days before resuming get ejected
under the current rule. Extending to 7 days / 0.7 ATR keeps them alive through a normal
1-week consolidation. Risk: some trades that stalled and genuinely failed are now held to full
stop — avg loss may rise. This is why E5 (SMA20 soft exit) is the natural companion: it
provides a safety valve that catches reversals before they reach the full stop.

---

**E3: Trailing stop with wider activation (2.0 ATR) and tighter trail (1.0 ATR)**

*Run if: Q2 shows winner MFE gap > 1 ATR, OR R-multiple distribution shows winners clustered
below +1.5R when MFE suggests they could reach +2.5R+.*

In `manage_position`, find the trailing stop activation (iter7 used 1.8 ATR). Change
activation to 2.0 ATR, trail distance to 1.0 ATR. If E1 is also adopted, these two compose
cleanly: breakeven trigger at +2.0 ATR fires first, then the trailing stop activates at the
same threshold — no gap between mechanisms, no phase where the stop is at entry but not yet
trailing.

Mechanism: iter7 (50a5e95) was directionally correct — win rate +1.7pp, avg loss down — but
was discarded for min_test_pnl, not because the expectancy signal was wrong. The 1.8 ATR
activation still clipped trades during normal +1.5–1.8 ATR consolidation. 2.0 ATR activation
prevents this. The 1.0 ATR trail (unchanged from iter7) is tight enough to lock in meaningful
gains once the trade is running.

---

**E4: Partial exit at first resistance**

*Run if: MFE data shows winners consistently reaching > +2 ATR AND the R-multiple distribution
has a hard ceiling near +1.5–2.0R (winners are not running past resistance).*

In `manage_position`, add: when `current_price ≥ entry_price + res_atr × atr14` (resistance
level is reached), exit 50% of the position, move stop to entry price (breakeven), trail the
remaining 50% at 1.0 ATR. The `res_atr` value is already computed at entry in `screen_day`
and must be passed into `manage_position` via the position record.

If no resistance is found (screen_day returned `res_atr = None`), skip the partial exit and
use a fixed +2.0 ATR as the partial exit trigger instead. This ensures all trades have a
defined profit-taking level.

Mechanism: the current strategy has no structured profit-taking — the entire position exits at
stop or stall. Partial exit at resistance locks in half the position's gain at a structurally
meaningful price level (the ceiling the strategy already identifies), while keeping the
remainder in play for continuation. Expected: avg win up, avg loss unchanged (the 50% remainder
that continues is now protected by breakeven stop), win rate may dip slightly on the 50% sold.

---

**E5: Close-below-SMA20 as a soft exit**

*Run if: Q1 shows low MFE on losers (true failures) AND Q3 shows stop_hit is the dominant
loser exit AND losers take multiple days to reach their stop (days_held > 2 on average for
losing trades).*

In `manage_position`, add end-of-day check: if `daily_close < sma20` while position is open,
flag for exit at next morning's open (or next available `price_10am`). The SMA20 value must
be re-computed or passed daily — it requires the daily close series to be available inside
the position manager, which may require a small interface change to pass it in.

Mechanism: slow-bleed losers grind down over 3–5 days, cross below SMA20, then eventually hit
the hard stop well below SMA20. The SMA20 cross converts this multi-day grind into a single
clean exit at a price typically 30–50% better than the eventual stop price. Expected: avg loss
down by ~0.3–0.5 ATR per affected trade, win rate may dip (some recoveries get cut), trade
count slightly reduced.

---

**E6: Entry quality gate — minimum resistance clearance 2.0 → 3.0 ATR**

*Run if: Phase 0.3 shows losses are true failures (low MFE) and exit changes alone are
insufficient. This is an entry filter, not an exit change — run it last among E1–E6.*

In `screen_day`, change the `nearest_resistance_atr` rejection threshold from `< 2.0` to
`< 3.0`. Trades with only 2.0–3.0 ATR of overhead clearance have a structurally limited R:R
ceiling — the trade needs to move 2 ATR to reach target but only has 2 ATR before hitting
resistance. Requiring 3.0 ATR of clearance ensures a minimum 3:1 implied R:R ratio.

Expected: trade count drops by ~10–20 from the 116 baseline (trades that pass all other
filters but have resistance between 2.0 and 3.0 ATR). Average win should rise because the
remaining trades have more runway. Complements E4 — more clearance means partial exit at
+2 ATR is still well below the structural ceiling.

---

### Phase 0.5 — Combinations

Run only after Phase 0.4 identifies which individual experiments improve W/L. Do not
combine experiments that were individually discarded — only combine keepers.

For each combination: run as a single backtest, compare to the best individual keeper from
Phase 0.4. Keep the combination only if W/L improves further without violating the
min_test_pnl or trade count floors.

**E1 + E3: Delayed breakeven (2.0 ATR) + trailing stop (2.0 ATR activation)**

The natural composition. Breakeven trigger fires at +2.0 ATR, locking the stop at entry at
the exact same threshold where the trailing stop would activate. In practice: from +2.0 ATR
onward the position is protected by a trailing stop rather than a fixed breakeven. There is
no phase where the stop is at entry but not yet trailing — the two mechanisms become one
continuous ratchet. Run this combination only if E1 and E3 were both individual keepers.
If only E1 kept, add E3 to it and test; if E3 degraded W/L individually it will not help here.

**E2 + E5: Looser stall exit + SMA20 soft exit**

E2 (looser stall exit) risks holding through reversals that the tighter rule would have
exited profitably. E5 (SMA20 soft exit) catches exactly those reversals — a position that
stalls and then crosses below SMA20 is the definition of a failed stall. Together they decouple
the two concerns: stall exit duration is loosened to let winners breathe, SMA20 cross provides
the safety valve that fires before the full stop is hit. Run this combination if E2 showed
avg win improvement but also raised avg loss, and E5 individually reduced avg loss.

**E6 + E4: Resistance clearance gate (3.0 ATR) + partial exit at resistance**

E6 (tighter entry gate) selects only trades with ≥ 3.0 ATR of overhead room. E4 (partial
exit at resistance) takes 50% off when that ceiling is reached. Together: only enter when
meaningful runway exists, then systematically harvest half the gain at the structural target.
This is the cleanest R:R profile achievable within the current strategy structure. Run this
combination if E6 reduced trade count but increased avg win, and E4 showed partial-exit gains
outweighing the complexity cost. Note: E6 + E4 will reduce total trades significantly — verify
the 100-trade floor is still met before keeping.

---

### Phase 0.6 — Static Sector Exclusion (lightweight, not a full sector strategy)

One-time rule change, not an optimization loop. Do NOT attempt per-sector parameter tuning —
with 116 total trades across 7 folds, per-sector walk-forward gives ~10–15 trades per sector
per fold, which is statistically meaningless. The blacklist is a single static gate.

**How to implement:**

1. In `prepare.py`, add a GICS sector lookup for each ticker at download time. Use
   `yfinance.Ticker(t).info['sector']` (or `'sectorKey'` for the GICS code). Write the sector
   as a column in the parquet file: `df['sector'] = sector_string`. Cache it with the price
   data so it does not require a re-download on every run.

2. Define the exclusion set as a module-level constant in `train.py` (editable section):
   ```python
   EXCLUDED_SECTORS = {'Utilities', 'Consumer Staples', 'Real Estate'}
   ```
   These map to the yfinance `sector` string values for XLU, XLP, and XLRE constituents.

3. In `screen_day`, add as the first check after the earnings filter:
   ```python
   if df['sector'].iloc[-1] in EXCLUDED_SECTORS:
       return None
   ```
   This fires before any indicator computation — zero performance cost.

4. Run once as a single backtest on the corrected-price baseline. Do not treat the sector list
   as a tunable parameter. If removing a sector causes trade count to drop below 100, that is
   acceptable — do not add sectors back to recover trade count.

**Why these three sectors:** Utilities, Consumer Staples, and REITs are income/dividend sectors
that attract capital during equity corrections as defensive rotation (yield becomes relatively
attractive). ECL and KO entered fold1 for exactly this reason — not because they had genuine
momentum, but because they were receiving defensive inflows. Blacklisting these removes one
class of structurally false positives without touching the tech, industrials, healthcare, or
materials entries that drive the strategy's positive folds.

**What this does not fix:** The fold1 tech entries (APP, HUBS, HOOD) are genuine momentum
breakouts that failed due to macro regime shift (tariff correction). No sector filter addresses
these — they entered correctly and failed for macro reasons. Accept them as the irreducible
fold1/2 floor.

---

### Phase 0.7 — Re-test Entry Configuration Against Fixed Exits

**When to run:** After Phase 0.4–0.5 produce a strategy with W/L ≥ 1.2 and the final
`screen_day` + `manage_position` bodies are copied into the master branch `train.py`. Create
a new worktree from master for this phase, the same way the global-mar24 worktree was created.

**This is not a 30-iteration sweep.** The full entry filter search space was exhaustively
explored in the prior run: ATR buffer, breakout window, pivot lookback, RSI bounds, resistance
ceiling, volume threshold combinations, SMA structure variants. Those dead ends are confirmed
regardless of exit mechanics and must not be revisited. The only reason to re-test anything
is that improved exits may shift the Pareto frontier — some entry configurations that were
previously too costly on the downside may now be acceptable with better exit protection.

**How many runs: 5 to 8 targeted re-tests, then stop.**

---

**Step 1: Re-establish the new baseline (1 run)**

Run `train.py` as-is with the exit-optimized strategy (copied from master). This is the
reference point for all subsequent comparisons. Document the full fold-by-fold metrics:
min_test_pnl, W/L ratio, total trades, win rate. This replaces the prior optimization's
baseline — all comparisons in this phase are against this number, not against any result from
the global-mar24 run.

---

**Step 2: Re-test the closest prior discard decisions (3–4 runs)**

Do not re-run all 27 prior iterations. Re-run only the configurations that were discarded
specifically because of min_test_pnl regression — not because of trade count or win rate.
These are the ones whose discard decision may be invalidated if exits now protect the downside
better.

The configurations to re-test (copy the relevant `screen_day` change from the notes in
`results.tsv` or `harness_upgrade.md`, paste into the new worktree's `train.py`):

- **Drop sma50>sma100 requirement** (from iter12): was discarded at -23.63 min with 133
  trades. If the new exits raise fold1/2 minimum by ≥ $5, this configuration may now satisfy
  the min_test_pnl floor while exceeding the 130-trade requirement. This is the highest-value
  re-test — it directly addresses the 116 vs 130 trade count tension.

- **Volume threshold 2.2×** (from iter10): was discarded at -50.14 min with 147 trades. That
  minimum was catastrophic without exit protection. If E5 (SMA20 soft exit) is adopted and
  cuts fold1/2 losses, re-test this to see whether the volume relaxation is viable. Only run
  this re-test if E5 was adopted in Phase 0.4 — if it wasn't, skip it.

- **SMA20 slope on the sma50>sma100-relaxed base** (from iter19): was discarded at -23.63
  with 126 trades — worse than both parents. Re-test only if Step 2's first re-test shows that
  the sma50>sma100 relaxation is now viable; this combination may then close the gap between
  126 and 133 trades while keeping the min floor.

---

**Step 3: Read the signal, decide whether to go deeper**

After Step 2, two outcomes are possible:

**Frontier did not shift:** The re-tested configurations still produce min_test_pnl values
below the floor even with improved exits. This means the exit improvements raised the baseline
uniformly but did not change which entry configurations are viable. **Stop here.** The current
baseline (Step 1) is the final strategy. Do not run more iterations.

**Frontier shifted:** One or more re-tested configurations now meets both the min_test_pnl
floor and the 130-trade requirement. In this case, run 3–4 additional experiments in that
direction only — for example, if the sma50>sma100 relaxation is now viable, sweep the SMA20
slope tolerance (0%, 0.5%, 1%) on top of it to find the optimal combination. Stop when
incremental min_test_pnl gains flatten across iterations. This is a focused sub-sweep of at
most 5 additional runs, not a new 30-iteration cycle.

---

**What not to do in this phase:**

- Do not re-test configurations that were discarded for reasons unrelated to min_test_pnl
  (e.g., ATR buffer changes added zero trades regardless of exits — that is still true).
- Do not treat this as a fresh optimization with an open search space. The entry filter space
  is solved. This phase only re-checks whether the exit changes moved the keep/discard boundary
  for the specific configurations that sat just outside it.
- Do not run more than 8 total iterations unless the frontier clearly shifted and a specific
  narrow direction is identified. If 8 runs produce no improvement over the Step 1 baseline,
  declare the entry configuration final and move to Phase 1.

---

| Metric | Current (iter21) | Target |
|--------|-----------------|--------|
| W/L ratio | 0.885 | ≥ 1.2 |
| min_test_pnl | -14.26 | ≥ -17 (allow $3 regression) |
| Total trades | 116 | ≥ 100 (allow some reduction for quality) |
| Win rate | 69% | ≥ 65% (allow some reduction) |

### Validation checklist

- [ ] P0-A/B/C/D all implemented and passing full test suite
- [ ] New baseline (iter21 on corrected price) established and documented
- [ ] Phase 0.2 diagnostic questions answered with data from enriched trade log
- [ ] At least one exit experiment (E1–E6) improves W/L to ≥ 1.0 without violating min_test_pnl floor
- [ ] W/L ≥ 1.2 achieved (via experiment or combination) with ≥ 100 trades and min_test_pnl ≥ -17
- [ ] Sector exclusion (if adopted) shows fold1/2 defensive entries removed from trade log
- [ ] `/fetch-strategies` run to register final strategy in `strategies/` before Phase 1

---

## Phase 1 — Pre-Market Signal CLI

**Goal:** Given the baseline strategy, produce pre-market BUY signals (new positions) and RAISE-STOP
signals (existing positions), triggered manually from the terminal, on the local machine.

**No:** OpenClaw, VPS, messaging, sector strategies, mid-day data, scheduled automation.

### Prerequisites

1. Baseline optimization completes in the `multisector-mar23` worktree.
2. Run `/fetch-strategies` to extract and register the baseline strategy.
3. In master `train.py`: update `BACKTEST_START` / `BACKTEST_END` / `TRAIN_END` / `TEST_START` to match
   the baseline run, copy the finalized `screen_day()` and `manage_position()` bodies from the worktree.
   Remove any placeholder strategies from the `strategies/` registry that predate the baseline.

### Deliverables

#### `portfolio.json` schema (user-maintained)

```json
{
  "positions": [
    {
      "ticker":       "AAPL",
      "entry_price":  182.50,
      "entry_date":   "2026-03-10",
      "shares":       10,
      "stop_price":   174.20,
      "notes":        "optional free text"
    }
  ]
}
```

User edits this file manually after each trade (entry and exit). The two scripts read it read-only.
Schema is extended in Phase 3 when OpenClaw takes over portfolio management.

#### `screener.py`

Scans all tickers in the parquet cache for BUY signals.

```
uv run screener.py
```

Behavior:
1. Load all `*.parquet` files from `CACHE_DIR` (same path `train.py` uses).
2. Fetch pre-market price for each ticker via `yfinance.Ticker(t).fast_info['last_price']`.
   - Fall back to previous close if `last_price` is unavailable (weekend / pre-open gap).
3. Append a synthetic "today" row to each df with `price_1030am = current_price` and `volume = 0`
   (today's volume is unavailable pre-market; volume rules use prior-day data only — see P0-E).
4. Call `screen_day(df, today, current_price=current_price)` from `train.py` (import directly).
5. Print armed candidates sorted by `prev_vol_ratio` descending.

Output columns: ticker, current_price, entry_threshold (high20 + 0.01), suggested_stop, atr14, rsi14,
prev_vol_ratio, vol_trend_ratio, gap_pct, resistance_atr, days_to_earnings.

**`current_price` interface:** `screen_day()` must accept an optional `current_price=None` parameter.
When `None` (harness path), it reads `df["price_10am"]` as before — no change to backtesting behavior.
When passed a value (live path), it uses that price instead. This is the only coupling to break between
the harness and live use; all other strategy logic is interval-agnostic.

**Pre-market vs. open price:** Pre-market prices are thin and noisy. The screener uses them for early
candidate identification only — "these 12 tickers meet all indicator conditions, AAPL threshold is
$183.50, currently at $181 pre-market." The **confirmed signal** fires at 9:30 AM when the actual
opening price is checked against the threshold. Never make final entry decisions on pre-market prices
alone.

#### Gap filter (pre-entry guard)

Analysis of losing trades shows ~72% are 1-day holds consistent with gap-down events — the stock
opens against the position before any stop management is possible. The largest single-day losses
(ORCL, SOFI, AAL, BBY…) are almost certainly earnings/news gaps visible in pre-market. These
dominate avg_loss and cannot be rescued by any stop technique.

**Step 1 — gap threshold analysis (one-off script `analyze_gaps.py`):**

Before implementing the filter, establish the threshold empirically from `trades.tsv`:
1. For each trade, load the ticker's parquet and look up `close` on `entry_date − 1 trading day`
2. Compute `gap = (entry_price − prev_close) / prev_close`
3. Plot gap vs pnl, and gap distribution for winners vs losers
4. Find the natural breakpoint — expected: large negative gaps (< −2%) correlate strongly with
   worst losses. Also check what fraction of winners had negative gaps to assess filter cost.

**Step 2 — gap filter in `screener.py`:**

After the EOD candidate list is generated, add a 9:30am gap check before committing to entry:
- Compute `gap = (today_open − prev_close) / prev_close` for each candidate
  (use `yfinance.Ticker(t).fast_info['open']` at market open, or pre-market quoted price as
  an earlier estimate — pre-market prices are thin/noisy but useful for early warning)
- Skip entries where `gap < −X%` (threshold set from the analysis above)

This filter is not backtestable with pre-market data but is fully backtestable using the actual
open, which is already in the historical data. Add `gap_pct` to the screener output for all
candidates regardless of filter outcome so context is visible.

#### `position_monitor.py`

Checks stop-raise conditions for all open positions.

```
uv run position_monitor.py
```

Behavior:
1. Read `portfolio.json`.
2. For each position, load its ticker's parquet from `CACHE_DIR`.
3. Fetch current price via `yfinance.Ticker(t).fast_info['last_price']` and append as today row.
4. Call `manage_position(position_dict, df)` from `train.py` (import directly).
5. Print RAISE-STOP signals where `new_stop > position['stop_price']`.

Output per signal: ticker, current_price, current_stop → new_stop, delta_atr, reason
(breakeven / trail / time-exit / early-stall).

No writes — user updates `portfolio.json` manually after confirming.

### Validation checklist

- [ ] `analyze_gaps.py` runs against `trades.tsv` and produces a gap vs pnl scatter with a visible breakpoint
- [ ] `screener.py` runs on fresh terminal, finds at least one armed candidate on a normal market day
- [ ] Screener output shows `prev_vol_ratio`, `vol_trend_ratio`, and `gap_pct` columns
- [ ] A ticker with flat prior volume (yesterday < 0.8× MA30) is correctly excluded
- [ ] A candidate with gap < threshold is excluded from the armed list (threshold from gap analysis)
- [ ] `position_monitor.py` runs with a sample `portfolio.json`, correctly outputs a RAISE-STOP for a
  position that is 1.5+ ATR in profit
- [ ] `screen_day()` in `train.py` master matches the baseline worktree output (spot-check 3 tickers)
- [ ] No old strategies remain in `strategies/` registry

---

## Phase 2 — Per-Sector Strategies

**Goal:** Replace the single baseline strategy with 11 GICS sector-specific strategies, each
independently optimized.

**Deferred still:** OpenClaw, VPS, messaging, mid-day data, scheduling, volatility adaptation.

### GICS classification notes

A few sectors have non-obvious assignments worth knowing before picking tickers:

- **Agricultural companies** are split across three sectors by business activity, not crop:
  - Grain/food processors (ADM, BG) → **Consumer Staples**
  - Fertilizers/agrochemicals (MOS, CF, NTR, CTVA) → **Materials**
  - Farm equipment (DE) → **Industrials**
- **Defense companies** (RTX, LMT, NOC, GD, TXT) → **Industrials** (Aerospace & Defense
  sub-industry). Classified by manufacturing/systems activity, not by government end customer.
  Their correlation with XLI holds reasonably well outside geopolitical shock events. If the
  Industrials strategy consistently underperforms on defense names specifically, split
  Aerospace & Defense into its own strategy at that point — don't pre-emptively do it.
- **No separate high-volatility group.** High-vol outliers (COIN, MSTR, SMCI, PLTR) stay in
  their respective sectors. The volatility-adaptive parameters in Phase 5 handle their extreme
  ATR automatically — no binary classification needed.

### Ticker selection per sector

For each of the 11 sectors, choose 8–12 tickers using these criteria in order:

1. **S&P 500 membership** — ensures liquidity and data quality in yfinance
2. **Top by market cap in sector** — institutional flows make technical patterns more reliable
3. **High correlation with sector ETF** — β > ~0.5 vs. XLK/XLF/XLE/etc. over 6-month rolling
   window. Low-β tickers (e.g., MSTR vs. XLK) are behavioral outliers; include them anyway
   since Phase 5 volatility-adaptive parameters will handle them, but note they may produce
   noisy optimization results
4. **Behavioral diversity within sector** — aim for sub-industry spread. In Financials: a
   megabank (JPM), an investment bank (GS), a payment network (V), an asset manager (BLK).
   They all correlate with XLF but have different volatility profiles, giving Optuna more signal
   to calibrate the volatility-adaptive parameters

This selection is a one-time manual judgment per sector. It does not need to match the live
trading universe exactly — it just needs to be representative.

### Steps

1. For each of the 11 GICS sectors: create a worktree using `/prepare-optimization` with the
   sector ticker list. All 11 can be prepared in sequence and run simultaneously — they are
   fully independent (no shared state, no ordering dependency). Running in parallel reduces
   wall-clock time from 11× to 1×.
2. Run `/fetch-strategies` after each sector run completes.
3. **Compare each sector specialist against the baseline** on the holdout test set for that
   sector's tickers. Decision rule: if the specialist beats the baseline, adopt it. If it
   doesn't, investigate before discarding (overfitting? not enough iterations? sector too
   heterogeneous?). Do not auto-adopt a worse strategy just because it's sector-specific.
4. Add sector lookup and introduce `strategy_runner.py` (see below).
5. Route each ticker to its sector strategy in both scripts via `select_strategy()`.

### Sector routing: `strategy_runner.py`

In Phase 1, `screener.py` and `position_monitor.py` import strategy functions directly from
`train.py` — one strategy, no routing needed. In Phase 2 there are 11 strategy files, and
both scripts need to call the right one per ticker without knowing the file layout.

`strategy_runner.py` is a thin strategy abstraction layer (~100 lines) that bridges the sector
selector and the actual strategy function calls:

- Accepts: ticker, recent OHLCV DataFrame, optional current price
- Calls `select_strategy()` to determine which `strategies/<name>.py` to use
- Imports that strategy dynamically and calls `screen_day()` / `manage_position()`
- Returns structured output (armed candidate dict or new stop price)
- Has no knowledge of Optuna, parquet files, backtesting, or walk-forward folds

`screener.py` and `position_monitor.py` replace their direct `train.py` import with a call to
`strategy_runner.py`. The sector routing, `YFINANCE_TO_GICS` mapping, and `select_strategy()`
all live here.

```python
YFINANCE_TO_GICS = {
    "Technology":             "IT",
    "Financial Services":     "Financials",
    "Consumer Cyclical":      "Consumer Discretionary",
    "Consumer Defensive":     "Consumer Staples",
    "Healthcare":             "Health Care",
    "Communication Services": "Communication Services",
    "Basic Materials":        "Materials",
    "Industrials":            "Industrials",
    "Energy":                 "Energy",
    "Utilities":              "Utilities",
    "Real Estate":            "Real Estate",
}

def get_sector(ticker: str, cache: dict) -> str | None:
    if ticker not in cache:
        info = yf.Ticker(ticker).info
        cache[ticker] = YFINANCE_TO_GICS.get(info.get("sector"), None)
    return cache[ticker]

def select_strategy(ticker: str, sector_cache: dict, strategies: dict):
    sector = get_sector(ticker, sector_cache)
    if sector is None:
        return None  # skip — no sector data (SPACs, stale ADRs, etc.)
    return strategies.get(sector)  # None if no strategy for this sector yet
```

Cache sector lookups in `~/.cache/autoresearch/sector_cache.json` (TTL: 30 days — sector
assignments change rarely). Do not default to a catch-all strategy when `get_sector()` returns
`None`. A wrong strategy is worse than no trade.

### Validation checklist

- [ ] Screener correctly routes a known Consumer Discretionary ticker to its sector strategy
- [ ] Sector cache file created and reused on second run
- [ ] A ticker with no yfinance sector (SPAC, stale ADR) is skipped, not routed to a fallback
- [ ] All 11 sector strategies registered in `strategies/` registry
- [ ] Each adopted sector specialist beats the baseline on its sector's holdout tickers

---

## Phase 3 — OpenClaw + VPS + Messaging

**Goal:** Move signal delivery from terminal output to Telegram/WhatsApp messages. Manual chat
commands trigger scans. User can update portfolio via chat.

**Deferred still:** Scheduled automation, mid-day data, real-time prices, volatility adaptation.

### OpenClaw capabilities

| Capability | Details |
|---|---|
| **Cron jobs** | Built into the Gateway, persist across restarts, configurable interval (`every: "30m"` etc.), active-hours limiting. Deliver output proactively via `--announce --channel telegram --to <chat-id>`. |
| **Proactive messaging** | Cron + `--announce` sends agent output directly to the user without a user-initiated message. Confirmed working. |
| **Native subagents** | `sessions_spawn` tool, up to 8 concurrent (`maxConcurrent` configurable), orchestrator pattern (`maxSpawnDepth >= 2`). Results announced back via request-reply. Best-effort — in-flight results lost if gateway restarts, acceptable for trading signals. |
| **Background exec** | `exec` tool runs shell commands including Python scripts. Background mode returns session ID; `process poll` retrieves output. Not push-based — agent must explicitly poll. |
| **Skills (SKILL.md)** | Markdown files that instruct the agent to use existing tools. No TypeScript required. Python scripts called via `exec`. |
| **Cron concurrency** | `maxConcurrentRuns: 1` default — overlapping cron fires queue, not skip. |

### VPS requirements (live system only)

| Resource | Requirement | Notes |
|---|---|---|
| RAM | 2–4 GB | OpenClaw (~1 GB) + screener peak (pandas on ~45 bars × 1000 tickers, released after run) |
| Disk | 5–10 GB | Live parquet cache (~1–2 GB) + OpenClaw + OS |
| CPU | 1–2 vCPU | No heavy computation |
| Cost | ~$5–15/month | Hetzner CX22, DigitalOcean Basic, or equivalent |

The optimization harness stays on the local development machine (compute/memory heavy: Optuna,
walk-forward across 85+ tickers, multiple parallel worktrees). The VPS is sized for the live
system only — lightweight screener + price monitor.

### Separate parquet caches

| | Harness cache (local) | Live system cache (VPS) |
|---|---|---|
| Tickers | Optimization universe (85–1000, varies per run) | Full live universe (S&P 500 + Russell 1000) |
| History depth | Full backtest window (months to years) | Last 60–90 days only |
| Updated by | `prepare.py` before each optimization run | EOD cron nightly (one bar appended per ticker) |
| Used by | `train.py` backtesting loop | Screener, mid-day rescan, indicator snapshot |

The VPS cache is built from scratch on first deployment. Each nightly EOD cron appends one bar per
ticker — no data transfer from the harness machine needed.

### NemoClaw sandbox constraints

NemoClaw's sandbox (via Landlock) restricts filesystem writes to `/sandbox` and `/tmp` only.
`~/.cache/autoresearch/` (the default parquet cache path) is blocked. Set these env vars in the
NemoClaw policy YAML at sandbox creation — no code changes needed:

```
AUTORESEARCH_CACHE_DIR=/sandbox/stock_data
SIGNALS_DIR=/sandbox/signals
PORTFOLIO_FILE=/sandbox/portfolio.json
SNAPSHOT_FILE=/sandbox/indicators_snapshot.json
```

`/tmp` is cleared on restart — use `/sandbox` for all data that must survive restarts.

### GitHub deployment flow

Strategy files are Python code (~5–10 KB) — a natural fit for git. A dedicated `strategies` branch
decouples strategy promotion from active harness development.

```
Local machine:
  1. Optimization run completes in a worktree
  2. /fetch-strategies → strategies/<name>.py registered, REGISTRY.md updated
  3. User reviews metrics, decides to promote
  4. git push → GitHub (harness repo, strategies/ directory, strategies branch)

VPS (OpenClaw):
  1. User sends "deploy IT strategy v2" via Telegram
  2. Deployer subagent: git pull strategies/<name>.py from GitHub (strategies branch)
  3. Validate: syntax check, METADATA fields present, sector matches live selector
  4. Copy to live system's strategies/ directory
  5. Update strategy_runner.py sector mapping if needed
  6. Announce confirmation to Telegram
```

VPS only needs a read-only SSH deploy key scoped to the harness repo — no direct network access
to the local machine.

### Scripts and skills

**Python scripts:**

| Script | What it does |
|---|---|
| `screener.py` | Loads cached daily data, runs strategy screener on all tickers, outputs armed candidates |
| `position_monitor.py` | Reads `portfolio.json`, runs stop-raise logic, outputs RAISE-STOP signals |
| `strategy_runner.py` | Sector routing layer — maps ticker → strategy file, delegates indicator/signal calls |
| `deployer.py` | Pulls strategy from harness repo, validates, updates strategies/ and selector |
| `sector_cache.py` | Queries yfinance `.info["sector"]`, persists to JSON cache, returns sector for a ticker |

**OpenClaw SKILL.md files (no TypeScript):**

| Skill | Instructs agent to... |
|---|---|
| `run-screener` | Run `screener.py`, parse output, format armed candidates summary, announce to channel |
| `check-stops` | Run `position_monitor.py`, format RAISE-STOP signals or "no updates", announce |
| `add-position` / `close-position` | Write/update entry to `portfolio.json` via exec |
| `deploy-strategy` | Run `deployer.py` for a named strategy, confirm success to channel |
| `update-strategies` | `git pull` from `strategies` branch on GitHub |

### Extended `portfolio.json` schema (Phase 3+)

When OpenClaw manages portfolio updates via chat, extend the schema with:

```json
{
  "positions": [
    {
      "ticker":       "AAPL",
      "entry_time":   "2026-03-24T10:02:00",
      "entry_price":  181.50,
      "entry_date":   "2026-03-24",
      "shares":       20,
      "stop_price":   176.00,
      "strategy":     "IT",
      "signal_id":    "2026-03-24-AAPL-001",
      "notes":        "optional free text"
    }
  ]
}
```

`signal_id` links the position back to the signal that generated it. `strategy` records which sector
strategy was active at entry — useful when strategies are later replaced.

### Validation checklist

- [ ] "run screener" chat message returns armed candidates in Telegram within 60 seconds
- [ ] "check stops" returns RAISE-STOP signals or "no updates" message
- [ ] "add position AAPL 182.50 10 174.20 2026-03-10" updates `portfolio.json` correctly
- [ ] "update strategies" pulls latest from GitHub `strategies` branch
- [ ] NemoClaw env vars set; screener writes to `/sandbox/stock_data/` not `~/.cache/`

---

## Phase 4 — Scheduled Automation + Mid-Day Monitoring

**Goal:** Pre-market scan runs automatically every trading morning. Mid-day re-screen added.
Real-time price monitoring for armed candidates during market hours.

**Deferred still:** Volatility adaptation.

### Key architecture insight

Strategy indicators (RSI, ATR, SMA, volume trend) are computed from historical daily closes and do
not change intraday. Only the price trigger check ("is AAPL currently below $183.50?") needs a live
price. This decouples the problem into two independent data needs and enables an important
optimization: compute all indicators once at EOD, not at 8:30 AM.

### EOD `indicators_snapshot.json`

Rather than loading 30–45 bars and computing rolling windows per ticker at 8:30 AM across 500–1000
tickers, run full indicator computation once during the EOD cron (4:30 PM) and persist results:

```json
{ "AAPL": { "RSI": 45.2, "ATR": 2.1, "SMA20": 183.5, "vol_trend": 1.4, "entry_threshold": 181.0, ... } }
```

The 8:30 AM check becomes a single comparison per ticker: `current_price < snapshot[ticker]["entry_threshold"]`
— no pandas, no rolling windows at signal time. ~80 KB for 1000 tickers × 10 indicators. The morning
scan becomes nearly instantaneous; the EOD cron is the bottleneck.

### Daily workflow

| Step | When | Data source | Notes |
|---|---|---|---|
| Batch download all tickers' history | ~8:00 AM | yfinance daily bars | Market closed — no delay |
| EOD snapshot already built | ~8:30 AM | `indicators_snapshot.json` (from prior EOD) | Read-only, no computation |
| Screen candidates, pre-market price check | ~8:30 AM | Snapshot + yfinance `fast_info` | Informational/early alert only |
| Confirmed signal at open | 9:30 AM | Actual opening price | Authoritative entry check |
| Real-time price monitoring | 9:30 AM – 4:00 PM | yfinance poll or Alpaca WebSocket | Trigger firing |
| Mid-day re-screen | ~2:00 PM | Synthetic bar from intraday partial data | Noisier than opening window |
| EOD update + snapshot rebuild | ~4:30 PM | yfinance daily close | Appends one bar, rebuilds snapshot |

### Price monitoring architecture

**Do not use a short-interval cron for real-time price monitoring.** With `maxConcurrentRuns: 1`
(default), a 2-minute cron where each run takes > 2 minutes (slow network) causes a queue backlog —
signals arrive late and out of order.

**Use a single long-running background Python process instead:**

```
9:30 AM cron:
  → exec "uv run price_monitor.py --candidates AAPL,MSFT,..." (background=true)
  → Python loops internally: every ~60s checks prices, prints signal JSON to stdout on trigger

Every 5 minutes cron:
  → agent polls background process: process poll <session_id>
  → if signal lines found in output → format and announce to Telegram/WhatsApp

4:05 PM cron:
  → agent kills the background process
```

The 5-minute poll cron is idempotent (just reading output) — occasional overlap is harmless. The
Python process has its own internal timing and is not subject to cron concurrency issues.

### Data source upgrade path

| Tier | Source | Delay | Limit | Cost | When to use |
|---|---|---|---|---|---|
| **MVP** | yfinance `fast_info` poll every 2–3 min | 15 min | None | Free | Phase 4 start; 15-min delay acceptable for daily swing signals |
| **Production** | Alpaca IEX WebSocket | Near real-time | 30 symbols | Free (paper account) | Once armed candidates list is stable; no real money deposit needed |
| **If needed** | Polygon.io | Real-time consolidated | Unlimited | $29/month | Only if Alpaca IEX causes missed signals in practice |

Alpaca IEX represents ~10–15% of trade volume but is representative for liquid large-caps (S&P 500 /
Russell 1000). The 30-symbol limit covers armed candidates comfortably — a well-calibrated screener
typically produces 5–30 candidates. If the list consistently exceeds 30, upgrade to Algo Trader Plus
($99/month) for unlimited symbols.

### Rolling window and data artifacts on disk

Python subprocesses spawned by OpenClaw's `exec` tool are ephemeral — in-memory state is lost
between calls. The rolling window lives in parquet files on disk, refreshed nightly by the EOD cron.

```
EOD cron (4:30 PM, daily):
  1. Download today's completed daily bar for all tickers via yfinance
  2. Append to each ticker's parquet file           ← one new row added
  3. Compute all indicators from .tail(45) per ticker
  4. Write indicators_snapshot.json                 ← rebuilt from scratch nightly
  (No explicit pruning — scripts always read .tail(45), never the full file)

8:30 AM screener:
  1. Read indicators_snapshot.json                  ← no parquet access at all
  2. Fetch one current pre-market price per ticker
  3. Compare: current_price < snapshot[ticker]["entry_threshold"]
  4. Announce armed candidates

Price monitor (market hours):
  1. Entry thresholds already in memory from morning scan
  2. Fetches current price periodically
  3. No rolling window access needed during the day

Mid-day rescan (2:00 PM):
  1. Read parquet .tail(44) per candidate           ← 44 completed bars
  2. Construct synthetic partial bar from today's intraday data (in-process, not saved)
  3. Append synthetic bar in-memory → run indicators → produce updated snapshot for candidates only
  4. Original parquet and full snapshot unchanged until EOD
```

| File | Updated by | Read by |
|---|---|---|
| `~/.cache/.../stock_data/<TICKER>.parquet` | EOD cron (append) | EOD cron, mid-day rescan |
| `indicators_snapshot.json` | EOD cron (full rebuild) | 8:30 AM screener, price monitor |
| `sector_cache.json` | On new ticker (lazy) | `strategy_runner.py` |
| `portfolio.json` | User / OpenClaw agent | `position_monitor.py` |
| `signals_today.json` | Price monitor, screener | `show-signals` skill |

### Signal quality and intraday confirmation

When the price trigger fires, `price_monitor.py` fetches the last 5 × 15-minute bars and computes:

| Signal | What it captures |
|---|---|
| Last 2–3 candle direction (green/red) | Is price recovering after touching trigger, or still falling? |
| Volume on trigger bar vs. 5-bar average | High volume at low = capitulation/reversal; low volume = weak dip |
| Short-term RSI (period 7–9 on 15-min bars) direction | Is momentum turning upward from oversold? |
| Bar close vs. bar open | Did price close above its open on the trigger bar? |

**Annotate signals, don't hard-gate them.** Hard-gating suppresses valid signals on noisy days.
Instead include a momentum assessment so the user can weigh context:

```
⚡ BUY signal — AAPL  [OPENING]
Trigger hit: $179.50  (entry threshold: $180.00)
Momentum: ⬆️ RECOVERING — last 2 bars green, volume above avg
15m RSI(9): 28 → 34, rising from oversold
Signal quality: CONFIRMED
```

```
⚡ BUY signal — AAPL  [MID-DAY]
Trigger hit: $179.50
Momentum: ⬇️ CONTINUING DOWN — last 2 bars red, volume expanding
15m RSI(9): 38 → 29, falling
Signal quality: CAUTION — may not have bottomed yet
```

`[OPENING]` signals (9:30 AM window) carry the most weight — high volume, clear overnight gap
information, clean price action. `[MID-DAY]` signals (2 PM rescan) are noisier; the confirmation
annotation is more important there.

The confirmation layer lives entirely in `price_monitor.py` — not in strategy files. Strategy files
identify the opportunity (daily indicators + entry threshold). The confirmation layer assesses
intraday timing of entry within that window. These are separate jobs and must stay separate so
daily strategy optimisation is unaffected.

### Cron jobs

| Job | Schedule (ET, weekdays) | Action |
|---|---|---|
| Pre-market scan | `0 8 * * 1-5` | Run `run-screener` skill, announce to Telegram |
| Start price monitor | `30 9 * * 1-5` | Run `price_monitor.py` in background with today's candidates |
| Poll price monitor | `*/5 9-16 * * 1-5` | Poll background process, announce any pending signals |
| Mid-day rescan | `0 14 * * 1-5` | Run mid-day rescan, announce updated candidates |
| Stop price monitor | `5 16 * * 1-5` | Kill `price_monitor.py` background process |
| EOD update | `30 16 * * 1-5` | Download final daily closes, append to parquet, rebuild snapshot |

### Validation checklist

- [ ] 8:30 AM cron fires on a trading day and delivers pre-market scan to Telegram
- [ ] EOD cron appends one bar per ticker and rebuilds `indicators_snapshot.json`
- [ ] Mid-day scan produces non-duplicate results (doesn't re-signal already-held tickers)
- [ ] Alpaca WebSocket connects for ≤30 tickers and emits price update within 5 seconds of trade
- [ ] `[CONFIRMED]` vs `[CAUTION]` annotation visible in signals, `[OPENING]` vs `[MID-DAY]` label present
- [ ] `price_monitor.py` exits cleanly at 4:05 PM via kill cron

---

## Phase 5 — Volatility-Adaptive Parameters

**Goal:** Replace fixed RSI/volume/ATR thresholds with k-coefficient regime-adaptive logic per sector.
Handles high-vol outliers (COIN, MSTR, SMCI) within their sector strategies — no separate classifier.

### How it works

Each sector strategy currently has fixed thresholds (e.g., `RSI_ENTRY = 45`). Phase 5 replaces
fixed thresholds with functions of continuously observable market signals:

```
RSI_entry_threshold = base_RSI + k_atr × ATR_percentile + k_adx × ADX_normalized
```

Where:
- `base_RSI`, `k_atr`, `k_adx` — fixed constants learned by Optuna during optimization
- `ATR_percentile`, `ADX_normalized` — recomputed fresh each bar from price data in the cache

MSTR in the IT sector gets a wider RSI band automatically because its ATR_percentile is high.
AAPL gets a tighter band. The same strategy code serves both.

### Regime signals

All computable from OHLCV already in the cache:

| Signal | What it proxies | Computation |
|---|---|---|
| ATR percentile (60-day rolling) | Volatility regime | ATR(60) / Close, ranked within cached universe |
| ADX (14-day) | Trending vs. choppy | Standard Wilder ADX from OHLCV |
| % tickers above 50d SMA | Market breadth / risk-on vs. off | Cross-ticker pass at each bar |
| Sector ETF momentum | Sector-level trend | Requires adding ~11 ETF tickers (XLK, XLF, XLE, etc.) to `prepare.py` |

Start with ATR percentile only (single `k` coefficient per threshold). Add additional signals
one at a time, verifying each improves out-of-sample performance before keeping it.

### How Optuna handles this

No change to the Optuna study structure. Instead of searching for `RSI_ENTRY = 45` (one scalar),
it searches for `base_RSI = 42, k_atr = 0.8` (two scalars per threshold). Trial count may need
to increase proportionally to the added parameter count. Run on top of each Phase 2 sector
strategy — do not restart from the baseline.

### Steps

1. Extend `train.py` strategy interface: replace scalar threshold constants with `(base, k)` pairs.
2. For each sector: run a new Optuna study calibrating `base_RSI`, `vol_multiplier_base`, and `k_atr`
   against ATR-percentile. Use the Phase 2 sector strategy as the warm start.
3. Update sector strategies to use `threshold = base + k × regime_signal`.
4. Update `screener.py` and `price_monitor.py` to compute regime signals at scan time and pass
   them into `screen_day()`.
5. Optionally expose k-value and ATR-percentile in signal output so context is visible.

### Validation checklist

- [ ] `screen_day()` accepts regime inputs and returns different thresholds for high-ATR vs low-ATR day
- [ ] Sector strategy Optuna studies complete with k-coefficient in [0.0, 1.0]
- [ ] Live scan computes ATR percentile from last 60 bars and passes it through correctly
- [ ] MSTR and AAPL produce visibly different RSI thresholds on the same scan day

---

## Summary

| Phase | Unlocks | Key files |
|---|---|---|
| 1 | Pre-market BUY + RAISE-STOP signals, terminal | `screener.py`, `position_monitor.py`, `portfolio.json`, `train.py` master update |
| 2 | Sector-specific signals | 11 sector worktrees, `strategy_runner.py`, sector routing |
| 3 | Chat-triggered signals, Telegram delivery | OpenClaw SKILL.md skills, VPS, GitHub deploy pipeline |
| 4 | Automatic daily scans, real-time triggers | Cron jobs, `price_monitor.py`, `indicators_snapshot.json`, Alpaca WebSocket |
| 5 | Regime-adaptive thresholds | Extended `screen_day()`, sector Optuna k-calibration |
