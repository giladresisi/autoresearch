# SMT Divergence Strategy Optimizer

Autonomous SMT divergence strategy optimizer: iterates on signal logic and tuning parameters in `train_smt.py` to maximize `min_test_pnl` on the walk-forward evaluation of MNQ1! futures.

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

4. **Run baseline**: `uv run python train_smt.py` — record the initial `min_test_pnl`.

---

## Editable Section (above `# DO NOT EDIT BELOW THIS LINE`)

You may modify ONLY these elements:

### Tunable Constants
- `SESSION_START` — kill zone start time (default `"09:00"` ET)
- `SESSION_END` — kill zone end time (default `"10:30"` ET)
- `MIN_BARS_BEFORE_SIGNAL` — minimum bars before divergence can fire (default `5`)
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
min_test_pnl: X.XX
min_test_pnl_folds_included: N
---
holdout_total_pnl: X.XX
holdout_total_trades: N
```

---

## Evaluation Criteria

A strategy iteration is considered an improvement if ALL of the following hold:

| Criterion | Threshold |
|-----------|-----------|
| `min_test_pnl` | > 0.0 (all qualified folds profitable) |
| `win_rate` (average across folds) | ≥ 0.40 |
| `total_trades` per fold | ≥ 3 (sparse folds excluded automatically) |
| `avg_rr` | ≥ 1.5 (reward/risk ratio) |

---

## Strategy Notes

- **One trade per day maximum** — harness only enters if no position is open
- **Kill zone**: 9:00–10:30 AM ET (NY open; institutional activity highest)
- **TDO anchor**: 9:30 AM bar open is both TP target and stop-placement basis
- **Stop formula**: `entry ± 0.45 × |entry − TDO|` (short: +, long: −)
- **R:R**: approximately 2.22 : 1 (1 / 0.45)
- **SMT divergence**: bearish = MES new session high + MNQ failure; bullish = MES new session low + MNQ failure
- **Ticker naming**: IB uses `MNQ` / `MES` (not `MNQ1!` / `MES1!`)

---

## Common Optimizations to Try

1. **Adjust kill zone**: Try `SESSION_END = "11:00"` or `"09:45"` for tighter entries
2. **Tighten MIN_BARS_BEFORE_SIGNAL**: Try `3` or `8` to filter early/late signals
3. **Modify stop ratio**: Change `0.45` in `screen_session()` to adjust R:R
4. **Entry confirmation**: Tighten `find_entry_bar()` wick requirement
5. **Session bias filter**: In `screen_session()`, add a check that entry direction aligns with pre-session trend

---

## Validation Before Submitting a Change

```bash
uv run python -m pytest tests/test_smt_strategy.py tests/test_smt_backtest.py -x -q
```

All tests must pass before recording the result.
