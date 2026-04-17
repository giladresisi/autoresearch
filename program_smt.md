# SMT Divergence Strategy Optimizer

Autonomous SMT divergence strategy optimizer: iterates on signal logic and tuning parameters in `train_smt.py` to maximize `avg_expectancy` on the walk-forward evaluation of MNQ1! futures.

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
- `SESSION_START` / `SESSION_END` — kill zone window (currently "09:00"–"13:30" ET)
- `MIN_BARS_BEFORE_SIGNAL` — wall-clock minutes before divergence can fire (default 0)
- `TRADE_DIRECTION` — frozen at "short" (longs structurally lose across 5/6 folds)
- `SHORT_STOP_RATIO` — fraction of |entry − TDO| for short stop (current 0.40)
- `LONG_STOP_RATIO` — frozen at 0.05 (longs disabled)
- `MIN_STOP_POINTS` — minimum stop distance in MNQ points (current 2.5)
- `MIN_TDO_DISTANCE_PTS` — minimum |entry − TDO| filter (current 15.0)
- `MAX_TDO_DISTANCE_PTS` — ceiling on |entry − TDO| distance (default 999.0 = disabled).
  Cross-tab: TDO<20 has EP=$32–$59 across ALL sequences including 5th+; TDO>100 EP=−$2.04.
  Optimizer search space: [15, 20, 25, 30, 40, 999]
- `MAX_REENTRY_COUNT` — max re-entries per session day (default 999 = disabled).
  Less impactful when MAX_TDO_DISTANCE_PTS is tight; at TDO<20 even Seq#5+ has EP=$32.
  Optimizer search space: [1, 2, 3, 4, 999]
- `MIN_PRIOR_TRADE_BARS_HELD` — min bars prior trade must survive before re-entry allowed
  (default 0 = disabled). DIAGNOSTIC ONLY — do not include in optimization runs.
  Extended diagnostics: removing fast re-entries (prior_bars<10, n=1781) provides no EP gain.
- `MIN_SMT_SWEEP_PTS` — min pts MES must exceed prior session extreme (default 0.0 = disabled);
  optimizer search [0, 1, 2, 5]
- `MIN_SMT_MISS_PTS` — min pts MNQ must fail to match MES (default 0.0 = disabled);
  optimizer search [0, 1, 2, 5]

**NOTE**: `MIN_CONFIRM_BODY_RATIO` is intentionally absent. Extended diagnostics showed
near-doji confirmation bars (ratio 0.00–0.10) have WR=0.352 and EP=$20.70 — the best bucket.
Filtering them would actively harm quality. This constant is not implemented.

- `ALLOWED_WEEKDAYS` — weekdays eligible for trading; Thursday excluded (frozenset({0,1,2,4}))
- `SIGNAL_BLACKOUT_START` / `SIGNAL_BLACKOUT_END` — entry suppression window (current "11:00"–"13:30")
- `TRAIL_AFTER_TP_PTS` — trail stop past TDO (current 1.0)
- `REENTRY_MAX_MOVE_PTS` — max favorable move before re-entry is disallowed (default 0.0)
- `BREAKEVEN_TRIGGER_PCT` — fraction of |entry − TDO| before stop locks to entry (default 0.0)
- `MAX_HOLD_BARS` — time-based exit N bars after entry (default 0 = disabled)
- `MNQ_PNL_PER_POINT` — do NOT change (2.0)
- `RISK_PER_TRADE` — do NOT change (50.0)

### Strategy Functions
- `detect_smt_divergence()` — signal detection logic; unchanged
- `find_anchor_close()` — finds most recent opposite-direction bar's close at divergence bar
- `is_confirmation_bar()` — single-bar confirmation check; replaces find_entry_bar() forward scan
- `find_entry_bar()` — still present; used by existing tests
- `_build_signal_from_bar()` — applies TDO_VALIDITY_CHECK / MIN_STOP_POINTS / MIN_TDO_DISTANCE_PTS guards
- `screen_session()` — compatibility shim for signal_smt.py live trading (uses new helpers)
- `compute_tdo()` — True Day Open (9:30 AM ET bar; first-bar proxy fallback); unchanged
- `manage_position()` — bar-by-bar exit; BREAKEVEN_TRIGGER_PCT replaces BREAKEVEN_TRIGGER_PTS
- `run_backtest()` — per-bar state machine; four states: IDLE / WAITING_FOR_ENTRY / IN_TRADE / REENTRY_ELIGIBLE

### Forbidden Changes
- Do NOT modify anything below `# DO NOT EDIT BELOW THIS LINE`
- Do NOT change `MNQ_PNL_PER_POINT = 2.0` (fixed contract spec)
- Do NOT change `RISK_PER_TRADE = 50.0` (frozen position-sizing baseline)
- Do NOT change `TRADE_DIRECTION` away from `"short"` (longs show structural losses across 5/6 folds)
- Do NOT change `FUTURES_CACHE_DIR`, `BACKTEST_START`, `BACKTEST_END` (loaded from manifest)
- Do NOT add external imports outside the standard library and pandas/numpy
- Do NOT modify `BREAKEVEN_TRIGGER_PTS` or `TRAIL_AFTER_BREAKEVEN_PTS` — frozen below boundary

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
| `avg_expectancy` | Higher than previous best | **PRIMARY** |
| `win_rate` (per fold avg) | ≥ 0.38 (improvement from ~0.32 baseline) | Guard |
| `avg_rr` | ≥ 1.5 | Guard |
| `total_test_trades` (sum) | ≥ 80 | Volume guard |
| `mean_test_pnl` | ≥ 1,500 (prevents trivially thin strategy) | Floor guard |
| `min_test_pnl` | > 0 (all qualified folds profitable) | Secondary guard |

`avg_expectancy` maps to `avg_pnl_per_trade` in the `print_results()` output.
`wl_ratio` = `avg_win_rate / (1 - avg_win_rate)` — target > 0.70, stretch goal > 1.0.
When two iterations both satisfy all guards, prefer the one with higher `avg_expectancy`.

---

## Optimization Agenda (Quality-Focused)

Work through in order. At each step, use the best accepted configuration as the base.
Primary metric: `avg_expectancy`. Guards: `win_rate ≥ 0.38`, `total_test_trades ≥ 80`.

### Priority 1 — MAX_TDO_DISTANCE_PTS (highest expected impact)

Grid search: [15, 20, 25, 30, 40, 999]

This is the master lever. Cross-tab analysis: at TDO<20, every re-entry sequence
(including 5th+) has WR > 0.34 and EP > $31. At TDO>100, even Seq#1 has EP=−$8.
Setting to 20 keeps ~828/2908 trades (28%) but projects WR 32%→38–40%, EP $18→$45–50.
Setting to 15 is more aggressive (~500 trades) but may reduce volume near the 80/fold guard.

At tight TDO, re-entry count becomes much less important — do not apply MAX_REENTRY_COUNT
simultaneously in this step.

Optimise for: avg_expectancy (primary), win_rate (secondary), total_test_trades ≥ 80 guard.

### Priority 2 — SIGNAL_BLACKOUT_END extension to 13:00

Test: ["12:00", "13:00"]

Cross-tab confirms: 12:xx × Seq#5+ = n=468 trades, EP=−$0.2 — dead weight. The 13:xx
window (WR=0.416, EP=$13.14) remains accessible after blackout extension. This is a pure
quality gain with negligible volume cost after MAX_TDO is applied.

Optimise for: avg_expectancy (primary).

### Priority 3 — MAX_REENTRY_COUNT (test after Priority 1 is locked)

Grid search: [1, 2, 3, 4, 999] using best MAX_TDO_DISTANCE from Priority 1.

Expected lower impact than in the original agenda. At TDO<20, all re-entry sequences are
high quality so the cap may not improve metrics. Most likely to help at the boundary
TDO (20–30 range) where Seq#5+ drops to EP=$6. If Priority 1 converges to MAX_TDO=20,
this step may produce no improvement — accept 999 (disabled) and move on.

Optimise for: avg_expectancy (primary), total_test_trades ≥ 80 guard.

### Priority 4 — SMT Strength Filters

Grid search: MIN_SMT_SWEEP_PTS ∈ [0, 1, 2, 5]; MIN_SMT_MISS_PTS ∈ [0, 1, 2, 5]

Filters marginal SMT divergences. A strong divergence (MES overshot by 5 pts, MNQ failed
by 3 pts) is more reliable than a marginal one (both by 0.5 pts). Effect magnitude unknown
from current diagnostics — these fields are now captured in trade records, enabling post-run
analysis. Test 2D grid; if neither filter improves EP, disable both.

### Priority 5 — 09:00 Window Isolation (if trade volume allows)

The 09:00 window (WR=59%, WL=1.44) is the only known path to WL > 1.0 without extreme
volume sacrifice. With ~183 trades total (~30/fold) it is borderline for reliable statistics.
Evaluate whether the combined filters from Priorities 1–4 naturally concentrate trades in
the 09:00 window, or whether an explicit time restriction is warranted. If testing, treat
as a separate strategy evaluation rather than a parameter of the main strategy.

### NOT IN AGENDA — MIN_CONFIRM_BODY_RATIO

Diagnostics show near-doji bars are the best confirmation candles (EP=$20.70). Do not test
this filter. The constant is not present in the codebase.

### NOT IN AGENDA — MIN_PRIOR_TRADE_BARS_HELD

Diagnostics show removing prior_bars<10 re-entries (73% of all re-entries) gains no EP
improvement. The constant exists for diagnostic completeness (default 0 = disabled) but
must not be included in optimizer search spaces.

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
- **Stop formula**: `entry + SHORT_STOP_RATIO × |entry − TDO|` for shorts (current ratio: 0.40 → ~2.5:1 R:R)
- **Exit mix (1m baseline)**: ~94% stop-outs (incl. 1pt trail after TDO), ~6% session-close, 0% hard TP; session-close exits are profitable at high rates
- **SMT divergence**: bearish = MES new session high + MNQ failure; bullish = MES new session low + MNQ failure
- **Ticker naming**: IB uses `MNQ` / `MES` (not `MNQ1!` / `MES1!`)

---

## Validation Before Submitting a Change

```bash
uv run python -m pytest tests/test_smt_strategy.py tests/test_smt_backtest.py -x -q
```

All tests must pass before recording the result.
