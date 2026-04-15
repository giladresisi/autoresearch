# SMT Divergence Strategy Optimizer

Autonomous SMT divergence strategy optimizer: iterates on signal logic and tuning parameters in `train_smt.py` to maximize `mean_test_pnl` on the walk-forward evaluation of MNQ1! futures.

---

## Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| **Timeframe** | `BACKTEST_START`=2024-09-01 to `BACKTEST_END`=2026-03-20 | Fixed futures backtest window; edit in `train_smt.py` constants block only. |
| **Instruments** | MNQ1!, MES1! | Continuous futures contracts. Data fetched by `prepare_futures.py`. |
| **Iterations** | 30 | Number of experiment iterations to run before stopping. |

---

## Setup (once per session)

0. **Verify you are in an optimization worktree**: Run `git branch --show-current`. The branch must match `autoresearch/*`. If it shows `master`, stop and use `prepare-optimization` skill first.

1. **Verify futures data is cached**: Run `uv run python -c "import train_smt; train_smt.load_futures_data(); print('data ok')"`. If this raises `FileNotFoundError`, run `uv run prepare_futures.py` (requires IB-Gateway on port 4002).

2. **Compute train/test split**:
   - `TRAIN_END = BACKTEST_END − 14 calendar days` (e.g. 2026-03-20 → 2026-03-06)
   - `TEST_START = TRAIN_END`
   - `SILENT_END = TRAIN_END − 14 calendar days` (e.g. 2026-03-06 → 2026-02-20)

3. **Set walk-forward constants** in `train_smt.py` editable section (above the boundary):
   - `WALK_FORWARD_WINDOWS = 6`
   - `FOLD_TEST_DAYS = 60` (business days per test fold)
   - `FOLD_TRAIN_DAYS = 0` (expanding window)

4. **Run baseline**: `uv run python train_smt.py` — record the initial `mean_test_pnl` and `min_test_pnl`.

---

## Editable Section (above `# DO NOT EDIT BELOW THIS LINE`)

You may modify ONLY these elements:

### Tunable Constants
- `SESSION_START` — kill zone start time (default `"09:00"` ET)
- `SESSION_END` — kill zone end time (default `"13:30"` ET)
- `MIN_BARS_BEFORE_SIGNAL` — wall-clock minutes before divergence can fire (default `0`, treated as timedelta — interval-agnostic)
- `SHORT_STOP_RATIO` — stop fraction for shorts as a fraction of |entry − TDO| (default `0.25`)
- `LONG_STOP_RATIO` — stop fraction for longs; currently frozen at `0.05` (longs disabled)
- `MIN_STOP_POINTS` — minimum stop distance in MNQ points; signals below this are rejected (default `2.5`)
- `MIN_TDO_DISTANCE_PTS` — minimum |entry − TDO| in MNQ points; filters degenerate tight-TP setups (default `0.0`)
- `SIGNAL_BLACKOUT_START` / `SIGNAL_BLACKOUT_END` — "HH:MM" window in which new entries are suppressed (default `""`)
- `BREAKEVEN_TRIGGER_PTS` — move stop to entry after this many favorable MNQ points (default `0.0`)
- `TRAIL_AFTER_BREAKEVEN_PTS` — trail stop this many points behind best price once breakeven triggers (default `0.0`)
- `TRAIL_AFTER_TP_PTS` — trail stop past TDO instead of exiting hard; set points behind best post-TDO price (default `0.0`)
- `MNQ_PNL_PER_POINT` — dollar value per point per contract (default `2.0`, do NOT change)
- `RISK_PER_TRADE` — dollar risk per trade for position sizing (default `50.0`, do NOT change)

### Strategy Functions
- `detect_smt_divergence()` — signal detection logic (divergence threshold, lookback window)
- `find_entry_bar()` — entry confirmation candle logic
- `compute_tdo()` — True Day Open calculation (9:30 AM ET bar; first-bar proxy fallback)
- `screen_session()` — session pipeline (stop/TP placement, R:R ratio, guards)
- `manage_position()` — bar-by-bar exit check with breakeven/trail/trail-after-TP logic

### Forbidden Changes
- Do NOT modify anything below `# DO NOT EDIT BELOW THIS LINE`
- Do NOT change `MNQ_PNL_PER_POINT = 2.0` (fixed contract spec)
- Do NOT change `RISK_PER_TRADE = 50.0` (frozen position-sizing baseline)
- Do NOT change `TRADE_DIRECTION` away from `"short"` (longs show structural losses across 5/6 folds)
- Do NOT change `FUTURES_CACHE_DIR`, `BACKTEST_START`, `BACKTEST_END` (loaded from manifest)
- Do NOT add external imports outside the standard library and pandas/numpy

---

## Running an Experiment

```bash
uv run python train_smt.py
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

---

## Evaluation Criteria

A strategy iteration is considered an improvement if ALL of the following hold:

| Criterion | Threshold | Priority |
|-----------|-----------|----------|
| `mean_test_pnl` | Higher than previous best | **PRIMARY** |
| `min_test_pnl` | > 0.0 (all qualified folds profitable) | Secondary guard |
| `total_trades` per fold | ≥ 3 (sparse folds excluded automatically) | |
| `total_test_trades` (sum across folds) | ≥ 80 | Volume guard |
| `avg_rr` | ≥ 1.5 (reward/risk ratio) | |

Note: `win_rate` is informational only. The strategy is a low-win-rate / high-RR system (observed range 18–37%); a win_rate floor would reject valid configurations. Track it for directional context but do not gate on it.

When two iterations both satisfy all guards, prefer the one with higher `mean_test_pnl`.

---

## Optimization Agenda

Work through priorities in order. After completing each, follow the **Post-Iteration Analysis Protocol** before proceeding. All infrastructure is already wired — no new code required for priorities 1–5.

**Baseline (current configuration):** `mean_test_pnl = +170.85`, `min_test_pnl = −407`, 4/6 positive folds. Stop exits account for 73% of trades and are the sole loss source.

### Priority 1 — Widen stop ratio (HIGHEST PRIORITY)

**What:** grid search over `SHORT_STOP_RATIO`
**Search space:** `[0.25, 0.30, 0.35, 0.40, 0.45]` (step 0.05)
**Why first:** touches every trade, largest expected impact on EV. At the current 14.6 pt median stop, normal intrabar wicks on a 20–40 pt instrument still reach the stop too often. Every 0.05 step adds ~3–4 pts to the median stop. Also widens the stop gap that makes breakeven and trailing viable later.
**Watch for:** win rate climbing but avg_rr compressing as contracts drop to 1.
**Optimise for:** `mean_test_pnl` primary, `min_test_pnl > 0` secondary.

### Priority 2 — Filter degenerate tight-stop setups

**What:** grid search over `MIN_TDO_DISTANCE_PTS`
**Search space:** `[0.0, 10.0, 15.0, 20.0, 25.0]`
**Why second:** when TDO is only ~25 pts from entry, position sizing assigns 4 contracts with ~6 pt stops — noise level on this instrument. Filtering these out removes structurally broken trades before any other lever is tuned. Can be swept jointly with Priority 1 as a 2D grid without significant compute cost.
**Watch for:** trade count dropping too aggressively (target: <10% reduction from baseline).
**Optimise for:** `avg_pnl_per_trade` primary, trade count secondary.

### Priority 3 — Block the 11:xx dead zone

**What:** set `SIGNAL_BLACKOUT_START = "11:00"` / `SIGNAL_BLACKOUT_END = "12:00"`
**Why third:** 48 trades at −18.2 avg PnL = −874 drag. Removing them lifts both mean and min test PnL without touching the profitable 12:xx (56% win rate, +28.7 avg) and 13:xx windows. Binary toggle — no search needed.
**Run after:** Priorities 1 + 2, so the stop ratio and quality filter are already at their optimum before measuring the blackout's marginal effect.
**Implementation:** change `SIGNAL_BLACKOUT_START` and `SIGNAL_BLACKOUT_END` values only.

### Priority 4 — Trail the stop past TDO

**What:** grid search over `TRAIL_AFTER_TP_PTS`
**Search space:** `[0.0, 5.0, 10.0, 20.0]`
**Why fourth:** 89% of TP exits continued ≥5 pts past TDO; median further move is 54.5 pts. Exiting hard at TDO forfeits the best part of these trades. Trailing captures incremental gains proportional to momentum.
**Watch for:** trail too tight → premature exit on normal retracement; trail too wide → gives back too much TDO profit.
**Optimise for:** `exit_tp avg_pnl` primary (lift above current +173.6), total PnL secondary.

### Priority 5 — Breakeven trigger

**What:** grid search over `BREAKEVEN_TRIGGER_PTS`; pair with `TRAIL_AFTER_BREAKEVEN_PTS`
**Search space:** `BREAKEVEN_TRIGGER_PTS` ∈ [0.0, 5.0, 10.0, 15.0, 20.0, 25.0]; `TRAIL_AFTER_BREAKEVEN_PTS` ∈ [0.0, 5.0]
**Why fifth:** only viable once Priority 1 has widened the stop to 0.35+ (~20 pt median). At the current 14.6 pt median, a 10 pt breakeven trigger is 68% of the stop — too aggressive. Pairs with `TRAIL_AFTER_TP_PTS` (Priority 4): breakeven protects pre-TDO profit; trail-after-TP protects post-TDO gains.

### Priority 6 — Intermediate TP (partial exit before full TDO)

**What:** add `INTERMEDIATE_TP_RATIO` — exit at a fraction of TDO distance
**Search space:** `[0.0, 0.3, 0.5, 0.7]`
**Why sixth (lowest):** targets session-close exits (33 trades, 82% win rate, +72 avg) by locking in partial gains on in-progress winners. Lower impact than Priority 4 (46 TP trades vs 33 session-close trades, and payoff per trade is smaller).
**Implementation:** requires a new constant and an additional exit check in `manage_position` — not yet wired. Do this only if Priorities 1–5 leave residual session-close drag worth addressing.

---

## Post-Iteration Analysis Protocol

After each iteration (whether accepted or rejected), before starting the next:

### Step 1 — Record Results

Write the following to the conversation:
- Iteration number and description
- Change made (exact constant value or function modification)
- Actual `mean_test_pnl`, `min_test_pnl`, total_test_trades, avg win rate
- Accepted or rejected (reason)

### Step 2 — Compare to Expected Outcome

For each iteration, the agenda above states an "Expected effect." Explicitly state:
- Did the change produce the expected directional effect? (e.g. did stop-out rate fall?)
- Was the magnitude of improvement larger or smaller than expected?
- Were there unexpected side effects (e.g. trade count dropped sharply, one fold regressed)?

### Step 3 — Adaptive Reflection (ultrathink)

Before moving to the next agenda item, think deeply about whether the remaining proposals are still the right next steps given what you just learned. Consider:

- If iteration 2 (session window) already excludes pre-9:30 signals, iteration 6 (TDO variant) loses much of its motivation — reassess or skip.
- If stop-out rate did not fall after iteration 3 (MIN_DIVERGENCE_POINTS), the assumption that weak sweeps cause stops may be wrong — consider alternative explanations.
- If a rejected iteration produced an unexpected directional insight (e.g. wider session window beat narrower), update the remaining candidates accordingly.
- You are not required to follow the agenda order or content if reflection suggests a better path.

Write a one-paragraph reflection labeled **"Agenda Reassessment"** before each iteration, stating either "proceeding as planned: [reason]" or "diverging from agenda: [alternative and rationale]."

---

## Strategy Notes

- **One trade per day maximum** — harness only enters if no position is open
- **Direction**: shorts only (`TRADE_DIRECTION = "short"`); longs structurally lose across 5/6 walk-forward folds
- **Kill zone**: 9:00–13:30 ET; entry analysis shows 12:xx is strongest (56% win rate), 11:xx is the dead zone (21%, −18.2 avg)
- **TDO anchor**: 9:30 AM ET bar open is both TP target and stop-placement basis; first-bar proxy if 9:30 bar absent
- **Stop formula**: `entry + SHORT_STOP_RATIO × |entry − TDO|` for shorts (current ratio: 0.25 → ~14.6 pt median stop)
- **R:R at 0.25**: approximately 3.0 : 1 (1 / 0.25) — low win rate (~25–30%) is compensated by high RR
- **Exit mix (baseline)**: 73% stop-outs, 16% TP, 11% session-close; session-close exits are 82% profitable
- **SMT divergence**: bearish = MES new session high + MNQ failure; bullish = MES new session low + MNQ failure
- **Ticker naming**: IB uses `MNQ` / `MES` (not `MNQ1!` / `MES1!`)

---

## Validation Before Submitting a Change

```bash
uv run python -m pytest tests/test_smt_strategy.py tests/test_smt_backtest.py -x -q
```

All tests must pass before recording the result.
