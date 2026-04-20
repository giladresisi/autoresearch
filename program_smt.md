# SMT Divergence Strategy Optimizer

Autonomous SMT divergence strategy optimizer: iterates on signal logic and tuning parameters in `strategy_smt.py` to maximize `mean_test_pnl` on the walk-forward evaluation of MNQ1! futures.

---

## Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| **Timeframe** | `BACKTEST_START`=2024-09-01 to `BACKTEST_END`=2026-03-20 | Fixed futures backtest window; defined in `backtest_smt.py` (do not edit). |
| **Instruments** | MNQ1!, MES1! | Continuous futures contracts. Data fetched by `prepare_futures.py`. |
| **Iterations** | 30 | Number of experiment iterations to run before stopping. |

---

## Setup (once per session)

0. **Verify you are in an optimization worktree**: Run `git branch --show-current`. The branch must match `autoresearch/*`. If it shows `master`, stop and use `prepare-optimization` skill first.

1. **Verify futures data is cached**: Run `uv run python -c "import backtest_smt; backtest_smt.load_futures_data(); print('data ok')"`. If this raises `FileNotFoundError`, run `uv run prepare_futures.py` (requires IB-Gateway on port 4002).

2. **Walk-forward constants** in `backtest_smt.py` (frozen — do not change):
   - `WALK_FORWARD_WINDOWS = 6`
   - `FOLD_TEST_DAYS = 60` (business days per test fold)
   - `FOLD_TRAIN_DAYS = 0` (expanding window)

3. **Run baseline**: `uv run python backtest_smt.py` — record the initial `mean_test_pnl`, `min_test_pnl`, total test trades, and average win rate across folds.

---

## Editable Section (`strategy_smt.py` only)

Edit **only `strategy_smt.py`**. Do not modify `backtest_smt.py` (frozen harness).

You may modify ONLY these constants:

### Core Tunable Constants

| Constant | Current Default | Optimizer Search Space |
|----------|----------------|------------------------|
| `SESSION_START` | "09:00" | ["09:00", "09:30"] |
| `SESSION_END` | "13:30" | ["10:30", "11:00", "12:00", "13:30"] |
| `MIN_BARS_BEFORE_SIGNAL` | 0 | [0, 10, 20, 30] (minutes) |
| `TRADE_DIRECTION` | "both" | ["both", "short"] |
| `SHORT_STOP_RATIO` | 0.35 | [0.25, 0.30, 0.35, 0.40, 0.45] |
| `LONG_STOP_RATIO` | 0.35 | [0.25, 0.30, 0.35, 0.40, 0.45] |
| `MIN_STOP_POINTS` | 2.5 | do not change |
| `MIN_TDO_DISTANCE_PTS` | 0.0 | [0.0, 5.0, 10.0, 15.0] |
| `MAX_TDO_DISTANCE_PTS` | 15.0 | [10.0, 15.0, 20.0, 25.0, 40.0, 999.0] |
| `MAX_REENTRY_COUNT` | 1 | [1, 2, 3, 4, 999] |
| `REENTRY_MAX_MOVE_PTS` | 999.0 | [0.0, 5.0, 10.0, 20.0, 999.0] |
| `BREAKEVEN_TRIGGER_PCT` | 0.0 | [0.0, 0.50, 0.60, 0.65, 0.70] |
| `MAX_HOLD_BARS` | 120 | [60, 120, 240, 0] |
| `ALLOWED_WEEKDAYS` | {0,1,2,3,4} | [{0,1,2,3,4}, frozenset({0,1,2,4})] |
| `SIGNAL_BLACKOUT_START` | "11:00" | ["", "11:00"] |
| `SIGNAL_BLACKOUT_END` | "13:00" | ["", "12:00", "13:00", "13:30"] |
| `TRAIL_AFTER_TP_PTS` | 1.0 | [0.0, 1.0, 5.0, 10.0, 20.0] |
| `MIN_SMT_SWEEP_PTS` | 0.0 | [0.0, 1.0, 2.0, 5.0] |
| `MIN_SMT_MISS_PTS` | 0.0 | [0.0, 1.0, 2.0, 5.0] |

### Plan 1–3 Feature Flags (approved defaults — re-validate in full walk-forward)

| Constant | Current Default | Optimizer Search Space |
|----------|----------------|------------------------|
| `HIDDEN_SMT_ENABLED` | True | [True, False] — confirm at new baseline |
| `SMT_OPTIONAL` | True | [True, False] — confirm displacement adds value |
| `MIN_DISPLACEMENT_PTS` | 10.0 | [8.0, 10.0, 15.0] |
| `DISPLACEMENT_STOP_MODE` | True | [True, False] — confirm at new baseline |
| `MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT` | 0 | [0, 2, 3] |
| `PARTIAL_EXIT_ENABLED` | True | do not change (keep True) |
| `PARTIAL_EXIT_FRACTION` | 0.33 | [0.25, 0.33, 0.50] |
| `PARTIAL_EXIT_LEVEL_RATIO` | 0.33 | [0.25, 0.33, 0.50, 0.67] |

### ICT Structure Features (untested at current quality level)

| Constant | Current Default | Optimizer Search Space |
|----------|----------------|------------------------|
| `MIDNIGHT_OPEN_AS_TP` | False | [True, False] |
| `SILVER_BULLET_WINDOW_ONLY` | False | [True, False] |
| `OVERNIGHT_SWEEP_REQUIRED` | False | [True, False] |
| `OVERNIGHT_RANGE_AS_TP` | False | [True, False] — only with OVERNIGHT_SWEEP_REQUIRED=True |
| `STRUCTURAL_STOP_MODE` | False | [True, False] — re-test with new baseline |
| `STRUCTURAL_STOP_BUFFER_PTS` | 2.0 | [1.0, 2.0, 3.0, 5.0] — only when STRUCTURAL_STOP_MODE=True |
| `TWO_LAYER_POSITION` | False | [True, False] — requires FVG_ENABLED=True |
| `FVG_ENABLED` | False | [True, False] |
| `FVG_LAYER_B_TRIGGER` | False | [True, False] |
| `FVG_MIN_SIZE_PTS` | 2.0 | [1.0, 2.0, 3.0, 5.0] |
| `SMT_FILL_ENABLED` | False | [True, False] |
| `INVALIDATION_MSS_EXIT` | False | [True, False] |

### Do NOT Change

- `INVALIDATION_CISD_EXIT`, `INVALIDATION_SMT_EXIT` — prior experiments showed exits too close to entry; no guard mechanism implemented
- `MNQ_PNL_PER_POINT = 2.0` — fixed contract spec
- `RISK_PER_TRADE = 50.0` — frozen position-sizing baseline
- `MIN_PRIOR_TRADE_BARS_HELD` — diagnostic only; extended diagnostics showed no EP improvement at any threshold
- `FUTURES_CACHE_DIR`, `BACKTEST_START`, `BACKTEST_END` — loaded from manifest; defined in `backtest_smt.py`
- Anything in `backtest_smt.py`

---

## Running an Experiment

```bash
uv run python backtest_smt.py
```

Output format (one key=value per line):
```
fold1_train_total_pnl: X.XX
fold1_train_total_trades: N
fold1_test_total_pnl: X.XX
fold1_test_total_trades: N
...
---
mean_test_pnl:               X.XX
min_test_pnl:                X.XX
min_test_pnl_folds_included: N
---
holdout_total_pnl:    X.XX
holdout_total_trades: N
```

The `avg_pnl_per_trade` value from `print_results()` is the per-trade expectancy.
Win rate and RR are also printed per fold — track the average across folds.

---

## Evaluation Criteria

An iteration is accepted if ALL of the following hold:

| Criterion | Threshold | Priority |
|-----------|-----------|----------|
| `mean_test_pnl` | Higher than current best | **PRIMARY** |
| Win rate (avg across folds) | ≥ 0.60 | Guard |
| `avg_rr` (avg across folds) | ≥ 1.5 | Guard |
| Total test trades (sum) | ≥ 400 | Volume guard |
| `min_test_pnl` | > 0 (all qualified folds profitable) | Secondary guard |

When two iterations both satisfy all guards, prefer the one with higher `mean_test_pnl`.

**Baseline (post-Plans-1-3 defaults, full 6-fold run 2024-09-01 → 2026-03-20):**

| Fold | Trades | Win Rate | Test PnL | avg_rr |
|------|--------|----------|----------|--------|
| 1 | 92 | 65.2% | $2,677 | 2.05 |
| 2 | 111 | 84.7% | $7,679 | 2.03 |
| 3 | 100 | 75.0% | $4,832 | 2.45 |
| 4 | 119 | 69.8% | $4,263 | 2.14 |
| 5 | 77 | 76.6% | $3,398 | 1.37 |
| 6 | 70 | 90.0% | $5,731 | 2.92 |

- **mean_test_pnl**: $4,763 | **min_test_pnl**: $2,677 | **Total trades**: 569 | **Avg WR**: 76.9% | **Avg avg_rr**: 2.17

---

## Optimization Agenda (30 Iterations)

Work through in priority order. At each step, start from the **current best accepted configuration**.
Primary metric: `mean_test_pnl`. Guards: WR ≥ 0.60, total_test_trades ≥ 100.

---

### Group 1 — Validate Plan 1–3 Defaults in Full Walk-Forward (Iterations 1–4)

These features were approved in 1-fold fast tests. Confirm they hold across all 6 folds before using them as the fixed base for subsequent iterations.

#### Iteration 1 — DISPLACEMENT_STOP_MODE

Test: `[True, False]`

With the correct bar-extreme stop for displacement entries, 1-fold showed +$2K vs SMT_OPTIONAL alone. Validate this holds across all regimes. If False wins, revert `DISPLACEMENT_STOP_MODE` and continue.

#### Iteration 2 — MIN_DISPLACEMENT_PTS threshold

Test: `[8.0, 10.0, 15.0]` (with `SMT_OPTIONAL=True`, `DISPLACEMENT_STOP_MODE` at its accepted value)

The current 10.0 was the Plan 3 default. Tighter (15.0) reduces noisy entries; looser (8.0) increases volume. Find the best trade-off.

#### Iteration 3 — PARTIAL_EXIT_LEVEL_RATIO

Test: `[0.25, 0.33, 0.50, 0.67]`

Current default 0.33 was the 1-fold winner. Test whether 0.25 (earlier lock-in) or 0.50 (midpoint) performs better across 6 folds.

#### Iteration 4 — HIDDEN_SMT_ENABLED

Test: `[True, False]`

Approved in Round 1 for +30.6% PnL. Re-confirm the gain holds after all subsequent changes. If False wins, revert.

---

### Group 2 — Stop and Exit Mechanics (Iterations 5–10)

#### Iteration 5 — MAX_TDO_DISTANCE_PTS

Test: `[10.0, 15.0, 20.0, 25.0, 40.0, 999.0]`

Current default is 15.0 from prior optimization. Re-test with the new baseline — displacement entries and Plan 3 features may shift the optimal ceiling. Cross-tab: TDO>100 loses money; TDO<20 is the best quality tier. Expected optimum still near 15–20.

#### Iteration 6 — SHORT_STOP_RATIO

Test: `[0.25, 0.30, 0.35, 0.40, 0.45]`

Stop ratio is the main RR lever for shorts. With WR at 82.5%, we can afford to widen the stop to improve RR and reduce stop-outs. Wider stop → higher avg_rr at expense of more capital at risk per trade.

#### Iteration 7 — LONG_STOP_RATIO

Test: `[0.25, 0.30, 0.35, 0.40, 0.45]`

Longs are now active (`TRADE_DIRECTION = "both"`). Long and short setups have different structural characteristics — test whether asymmetric ratios improve overall performance. Test LONG_STOP_RATIO independently after SHORT is locked.

#### Iteration 8 — BREAKEVEN_TRIGGER_PCT

Test: `[0.0, 0.50, 0.60, 0.65, 0.70]`

Moves stop to entry after price travels X% toward TDO. Currently disabled (0.0). At WR=82.5%, most trades reach TDO — a breakeven trigger could protect gains on the 17.5% of losers that would otherwise stop out fully. Expected effect: lower max_drawdown, mild reduction in avg_rr.

#### Iteration 9 — TRAIL_AFTER_TP_PTS

Test: `[0.0, 1.0, 5.0, 10.0, 20.0]`

Currently 1.0 — a trailing stop 1 MNQ point behind the best post-TDO price. A wider trail (5–10 pts) allows winners to run further; 0.0 exits exactly at TDO. Expected effect: higher avg_rr at cost of some converted TPs becoming stop-outs.

#### Iteration 10 — PARTIAL_EXIT_FRACTION

Test: `[0.25, 0.33, 0.50]`

Fraction of contracts closed at the partial exit level. Current 0.33 was approved in Round 2. Test whether closing half (0.50) earlier improves drawdown more than it costs in final-leg PnL.

---

### Group 3 — Session and Timing (Iterations 11–14)

#### Iteration 11 — TRADE_DIRECTION

Test: `["both", "short"]`

Current default is "both". The prior finding that longs lose was based on a 52% WR baseline — at the current quality level, longs may behave differently. Confirm whether "both" outperforms "short" in the full 6-fold run.

#### Iteration 12 — ALLOWED_WEEKDAYS (Thursday)

Test: `[frozenset({0,1,2,3,4}), frozenset({0,1,2,4})]`

Thursday previously showed 25% WR on the old baseline. Re-test with current quality filters in place. If Thursday still underperforms, exclude it.

#### Iteration 13 — SESSION_END

Test: `["10:30", "11:00", "12:00", "13:30"]`

The current blackout ("11:00"–"13:00") already suppresses the dead zone. Test whether a hard session cutoff at 10:30 or 11:00 concentrates entries into the strongest morning window.

#### Iteration 14 — SIGNAL_BLACKOUT relaxation

Test combinations: `SIGNAL_BLACKOUT_START/END` in `["", "11:00"] × ["", "12:00", "13:00", "13:30"]`

The current "11:00"–"13:00" window was set empirically. Test whether relaxing the end (to 13:30) captures useful afternoon setups, or whether extending the start (to 11:30) further improves quality.

---

### Group 4 — Re-entry Mechanics (Iterations 15–17)

#### Iteration 15 — MAX_REENTRY_COUNT

Test: `[1, 2, 3, 4, 999]`

Currently 1 (one re-entry per session). With high-quality TDO distances now filtered, re-entries at the same setup may be structurally sound. Test whether allowing 2–3 re-entries per day adds volume without degrading quality.

#### Iteration 16 — REENTRY_MAX_MOVE_PTS

Test: `[0.0, 5.0, 10.0, 20.0, 999.0]`

0.0 = disallow re-entry if price moved at all toward target before stop. 999.0 = always allow. An intermediate threshold (10–20 pts) allows re-entry only when the setup is still "fresh." Expected effect: filters late-session revenge re-entries.

#### Iteration 17 — MIN_TDO_DISTANCE_PTS

Test: `[0.0, 5.0, 10.0, 15.0]`

Lower-bound filter on |entry − TDO|. Currently disabled (0.0). Very tight TDO setups (< 5 pts) have degenerate stops — a floor here may improve average RR without significant volume loss.

---

### Group 5 — Signal Quality Filters (Iterations 18–21)

#### Iteration 18 — MIN_SMT_SWEEP_PTS

Test: `[0.0, 1.0, 2.0, 5.0]`

How far MES must exceed the prior session extreme. Marginal sweeps (< 1 pt) may be noise. Expected effect: fewer signals but higher conviction per trade.

#### Iteration 19 — MIN_SMT_MISS_PTS

Test: `[0.0, 1.0, 2.0, 5.0]`

How far MNQ must fail to match MES. A 3-pt miss is a stronger divergence than a 0.5-pt one. Test independently after MIN_SMT_SWEEP_PTS is locked; they may be correlated.

#### Iteration 20 — MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT

Test: `[0, 2, 3]`

Gates displacement entries to sessions where at least N ICT/hypothesis rules confirm direction. Score 0 = disabled (current). Score 2 requires pd_range + one other rule to agree. Expected effect: fewer displacement entries, higher WR on that subset.

#### Iteration 21 — MIN_BARS_BEFORE_SIGNAL

Test: `[0, 10, 20, 30]` (minutes after session open)

Currently 0 — the first real signal opportunity is already bar 1 due to empty prior-session slice. A forced warm-up (20–30 min) ensures the session's structure is established before entries are taken.

---

### Group 6 — ICT Structure Features (Iterations 22–27)

Test each feature independently, starting from the best configuration to date. Most of these were previously neutral or deferred — re-test at the new quality level.

#### Iteration 22 — MIDNIGHT_OPEN_AS_TP

Test: `[False, True]`

Uses 00:00 ET globex open as TP target instead of 9:30 RTH open (TDO). ICT canonical reversion target. Previously neutral. May unlock overnight setups that TDO misses.

#### Iteration 23 — SILVER_BULLET_WINDOW_ONLY

Test: `[False, True]`

Restricts divergence detection to 09:50–10:10 ET. This is the ICT "Silver Bullet" kill zone — highest-conviction window for reversal patterns. Expected effect: significant volume reduction but possible WR improvement. Only proceed if total_test_trades ≥ 100.

#### Iteration 24 — OVERNIGHT_SWEEP_REQUIRED

Test: `[False, True]`

Requires the overnight high (for shorts) or low (for longs) to have been swept before the session signal fires. ICT confirmation that liquidity above/below has already been cleared. Previously neutral — re-test.

#### Iteration 25 — TWO_LAYER_POSITION + FVG_ENABLED

Test: `TWO_LAYER_POSITION=True, FVG_ENABLED=True, FVG_LAYER_B_TRIGGER=True` vs baseline

The two-layer (Layer A at divergence, Layer B on FVG retracement) was neutral in Round 2 because layer_b_triggers=0 in the test window. Test across all 6 folds — FVG retracements may appear in different regimes.

#### Iteration 26 — STRUCTURAL_STOP_MODE

Test: `[False, True]` with `STRUCTURAL_STOP_BUFFER_PTS` ∈ `[1.0, 2.0, 5.0]`

Stop beyond the divergence bar's wick extreme. Rejected in Round 1 (killed RR at old stop ratios). Re-test with current quality baseline: at WR=82.5% and better setups, the structural stop may work correctly without the ratio stop's mechanical dependency on TDO distance.

#### Iteration 27 — SMT_FILL_ENABLED

Test: `[False, True]`

MES fills a FVG that MNQ has not — alternative divergence type. Previously neutral (fill_entries=0 in test window). Confirm whether fills generate entries in other 6-fold regimes.

---

### Group 7 — Combinatorial and Final Tuning (Iterations 28–30)

#### Iteration 28 — Asymmetric Stop Combo

Using best `SHORT_STOP_RATIO` from Iter 6 and best `LONG_STOP_RATIO` from Iter 7, test them together. When tested independently, the other is at its default — the combined effect may differ.

#### Iteration 29 — Best ICT Feature Combo

Take the best-performing ICT feature(s) from Group 6 (Iterations 22–27) and test them simultaneously. Features that were marginal individually may compound when combined.

#### Iteration 30 — Final Best-of-All

Apply all accepted changes simultaneously and run the full walk-forward as a final sanity check. Compare `mean_test_pnl`, `min_test_pnl`, win rate, and total_test_trades against the original baseline. This is the submission candidate.

---

## Post-Iteration Analysis Protocol

After each iteration (whether accepted or rejected), before starting the next:

### Step 1 — Record Results

Write to the conversation:
- Iteration number and description
- Exact change made (constant name and value)
- Actual `mean_test_pnl`, `min_test_pnl`, total_test_trades, avg win rate across folds
- Accepted or rejected (and why)

### Step 2 — Compare to Expected Outcome

Explicitly state:
- Did the change produce the expected directional effect?
- Was the magnitude larger or smaller than expected?
- Were there unexpected side effects (sharp volume drop, one fold regressed, etc.)?

### Step 3 — Adaptive Reflection

Before moving to the next agenda item, think deeply about whether remaining proposals are still the right next steps. Write a one-paragraph **"Agenda Reassessment"** stating either "proceeding as planned: [reason]" or "diverging from agenda: [alternative and rationale]."

Examples of when to diverge:
- If win rate is already ≥ 0.85 after Group 1, tighter quality filters (Group 5) may be counterproductive — prioritise volume recovery instead.
- If `TRADE_DIRECTION="short"` wins in Iter 11, skip `LONG_STOP_RATIO` (Iter 7) and repurpose that iteration for an unlisted parameter.
- If `STRUCTURAL_STOP_MODE=True` wins in Iter 26, revisit `SHORT_STOP_RATIO` / `LONG_STOP_RATIO` since the stop mechanism has changed.

---

## Strategy Notes

- **One trade per day maximum** — harness only enters if no open position
- **Direction**: both longs and shorts active (`TRADE_DIRECTION = "both"`)
- **Kill zone**: 09:00–13:30 ET; signal blackout 11:00–13:00 suppresses dead zone
- **TDO anchor**: 9:30 AM ET bar open is TP target and stop-placement basis; first-bar proxy if 9:30 bar absent
- **Stop formula (short)**: `entry + SHORT_STOP_RATIO × |entry − TDO|`; current ratio 0.35 → ~2.86:1 R:R
- **Stop formula (long)**: `entry − LONG_STOP_RATIO × |entry − TDO|`; current ratio 0.35 → ~2.86:1 R:R
- **SMT divergence**: bearish = MES new session high + MNQ failure; bullish = MES new session low + MNQ failure
- **Displacement entries**: large-body candle (body ≥ MIN_DISPLACEMENT_PTS) when no wick SMT fires; stop = bar extreme when DISPLACEMENT_STOP_MODE=True
- **Partial exit**: closes PARTIAL_EXIT_FRACTION of contracts at PARTIAL_EXIT_LEVEL_RATIO × distance to TP
- **Ticker naming**: IB uses `MNQ` / `MES` (not `MNQ1!` / `MES1!`)

---

## Validation Before Submitting a Change

```bash
uv run python -m pytest tests/test_smt_strategy.py tests/test_smt_backtest.py tests/test_hypothesis_smt.py -x -q
```

All tests must pass before recording the result.
