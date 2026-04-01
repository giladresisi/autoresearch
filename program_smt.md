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
- `SESSION_END` — kill zone end time (default `"10:30"` ET)
- `MIN_BARS_BEFORE_SIGNAL` — minimum bars before divergence can fire (default `5`)
- `LONG_STOP_RATIO` — per-direction stop fraction for longs (default `0.45`)
- `SHORT_STOP_RATIO` — per-direction stop fraction for shorts (default `0.45`)
- `MNQ_PNL_PER_POINT` — dollar value per point per contract (default `2.0`, do NOT change)
- `RISK_PER_TRADE` — dollar risk per trade for position sizing (default `50.0`)

### Strategy Functions
- `detect_smt_divergence()` — signal detection logic (divergence threshold, lookback window)
- `find_entry_bar()` — entry confirmation candle logic
- `compute_tdo()` — True Day Open calculation (proxy fallback logic)
- `screen_session()` — full session pipeline (session slice, stop/TP placement, R:R ratio)
- `manage_position()` — bar-by-bar exit check logic

### Forbidden Changes
- Do NOT modify anything below `# DO NOT EDIT BELOW THIS LINE`
- Do NOT change `MNQ_PNL_PER_POINT = 2.0` (this is a fixed contract spec)
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
| `win_rate` (average across folds) | ≥ 0.40 | |
| `total_trades` per fold | ≥ 3 (sparse folds excluded automatically) | |
| `total_test_trades` (sum across folds) | ≥ 80 | Volume guard |
| `avg_rr` | ≥ 1.5 (reward/risk ratio) | |

When two iterations both satisfy all guards, prefer the one with higher `mean_test_pnl`.

---

## Optimization Agenda

Iteration 1 is embedded as comments in `train_smt.py` (see `LONG_STOP_RATIO` / `SHORT_STOP_RATIO` constants). Begin there. Iterations 2–6 are listed below in priority order. After completing each iteration, follow the **Post-Iteration Analysis Protocol** below before proceeding to the next.

### Iteration 1 — Asymmetric Stop Ratios (HIGHEST PRIORITY)

See inline instructions in `train_smt.py` at `LONG_STOP_RATIO` / `SHORT_STOP_RATIO`.

- Search space: `LONG_STOP_RATIO` ∈ [0.30, 0.55], `SHORT_STOP_RATIO` ∈ [0.30, 0.55] (step 0.05)
- Expected effect: better-calibrated RR per direction → improved `mean_test_pnl`
- Accept if: `mean_test_pnl` improves AND all guards hold

### Iteration 2 — Session Window (HIGH PRIORITY)

Kill zone is 9:00–10:30. Pre-cash (9:00–9:30) and RTH open (9:30–10:30) behave differently. Pre-9:30 divergences target a TDO that hasn't printed yet, making those signals more speculative.

- Candidates: `("09:30", "10:30")`, `("09:00", "10:00")`, `("09:30", "11:00")`
- Expected effect: removing speculative pre-9:30 signals should raise win rate and `avg_rr`
- Watch: narrowing window reduces trade count — enforce total_test_trades ≥ 80 guard
- Accept if: `mean_test_pnl` improves, total trades ≥ 80, all other guards hold

### Iteration 3 — Minimum Divergence Magnitude (MEDIUM PRIORITY)

`detect_smt_divergence()` fires on any MES breach, even 0.25 points past the session high/low. A weak liquidity sweep is less meaningful than a decisive one.

- Add a `MIN_DIVERGENCE_POINTS` constant (e.g., 2–8 MNQ points) — currently absent from the editable section
- Expected effect: fewer signals, lower stop-out rate, higher `avg_rr`
- Search: try values 2, 4, 6, 8 MNQ points; pick highest `mean_test_pnl` that keeps trades ≥ 80
- Accept if: `mean_test_pnl` improves, guards hold

### Iteration 4 — MIN_BARS_BEFORE_SIGNAL (MEDIUM PRIORITY)

Currently 5 bars = 25 min warm-up. ~50% stop-out rate suggests signals may still fire too early.

- Search space: 2–8 bars (10–40 min at 5m)
- Expected effect: more bars = fewer signals but stronger structure context → lower stop-out rate
- Trade-off: more bars = fewer trades — watch total_test_trades ≥ 80
- Accept if: `mean_test_pnl` improves, guards hold

### Iteration 5 — Entry Confirmation Tightening (LOWER PRIORITY)

`find_entry_bar()` accepts any bearish/bullish bar whose wick pierces a prior close. Requiring the confirmation bar to also close in the top/bottom X% of its range ensures conviction rather than a wick touch.

- Add `ENTRY_CLOSE_STRENGTH` constant ∈ [0.0, 0.5] (0.0 = current behaviour, disabled)
  - 0.3 for shorts: confirmation bar must close in bottom 30% of its range
  - 0.3 for longs: confirmation bar must close in top 30% of its range
- Expected effect: fewer but higher-quality entries → lower stop-out rate, better `avg_rr`
- Accept if: `mean_test_pnl` improves, guards hold

### Iteration 6 — TDO Definition Variant (LOWER PRIORITY)

`compute_tdo()` uses the 9:30 RT open. For pre-9:30 signals, TDO is unknown at signal time.

- Alternatives to evaluate:
  - Previous session close
  - 4am globex open (first available bar)
  - Flag pre-9:30 signals separately and compare their stats vs post-9:30
- Expected effect: more accurate TP anchor for pre-9:30 signals
- Note: if Session Window (iteration 2) already eliminated pre-9:30 signals, skip or reassess relevance

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
- **Kill zone**: 9:00–10:30 AM ET (NY open; institutional activity highest)
- **TDO anchor**: 9:30 AM bar open is both TP target and stop-placement basis
- **Stop formula**: `entry ± LONG/SHORT_STOP_RATIO × |entry − TDO|` (short: +, long: −)
- **R:R at 0.45**: approximately 2.22 : 1 (1 / 0.45)
- **SMT divergence**: bearish = MES new session high + MNQ failure; bullish = MES new session low + MNQ failure
- **Ticker naming**: IB uses `MNQ` / `MES` (not `MNQ1!` / `MES1!`)

---

## Validation Before Submitting a Change

```bash
uv run python -m pytest tests/test_smt_strategy.py tests/test_smt_backtest.py -x -q
```

All tests must pass before recording the result.
