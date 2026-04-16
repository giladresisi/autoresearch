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
- `SESSION_START` / `SESSION_END` — kill zone window (currently "09:00"–"13:30" ET)
- `MIN_BARS_BEFORE_SIGNAL` — wall-clock minutes before divergence can fire (default 0)
- `TRADE_DIRECTION` — frozen at "short" (longs structurally lose across 5/6 folds)
- `SHORT_STOP_RATIO` — fraction of |entry − TDO| for short stop (current 0.40)
- `LONG_STOP_RATIO` — frozen at 0.05 (longs disabled)
- `MIN_STOP_POINTS` — minimum stop distance in MNQ points (current 2.5)
- `MIN_TDO_DISTANCE_PTS` — minimum |entry − TDO| filter (current 15.0)
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
| `mean_test_pnl` | Higher than previous best | **PRIMARY** |
| `min_test_pnl` | > 0.0 (all qualified folds profitable) | Secondary guard |
| `total_trades` per fold | ≥ 3 (sparse folds excluded automatically) | |
| `total_test_trades` (sum across folds) | ≥ 80 | Volume guard |
| `avg_rr` | ≥ 1.5 (reward/risk ratio) | |

Note: `win_rate` is informational only. The strategy is a low-win-rate / high-RR system (observed range 18–37%); a win_rate floor would reject valid configurations. Track it for directional context but do not gate on it.

When two iterations both satisfy all guards, prefer the one with higher `mean_test_pnl`.

---

## Optimization Agenda

Work through priorities in order.

**Baseline (1m bars):** SHORT_STOP_RATIO=0.40, MIN_TDO_DISTANCE_PTS=15,
SIGNAL_BLACKOUT=11:00–13:30, Thursday excluded, TRAIL_AFTER_TP_PTS=1.0,
REENTRY_MAX_MOVE_PTS=0.0, BREAKEVEN_TRIGGER_PCT=0.0.
`mean_test_pnl=+1049.03`, `min_test_pnl=+53.20`, 361 test trades, all 6 folds profitable.
Exit mix: ~94% stop (incl. 1pt trail post-TDO), ~6% session_close, 0% hard TP.
avg_rr: 1.9–3.0 across folds. Win rates: 35%, 42%, 35%, 38%, 36%, 34% (tight band).
Note: TRAIL_AFTER_TP_PTS=1.0 is genuinely 1pt at 1m (was functionally 5–10pt on 5m bars).
Optimising this is Priority 1 — it caps all post-TDO capture.

### Priority 1 — Trail width past TDO (HIGHEST PRIORITY)
Grid search: TRAIL_AFTER_TP_PTS ∈ [0.0, 1.0, 5.0, 10.0, 20.0]
**Why first:** Baseline shows 0% TP exits — current 1.0 pt trail is effectively a hard TP with 1-bar lag,
capturing none of the post-TDO continuation (historical median: 54.5 pts past TDO). This lever touches
every trade that reaches TDO and is clearly mistuned. Include 0.0 as a diagnostic (revert to hard TP).
Optimise for: mean_test_pnl (primary), exit_tp avg_pnl (secondary).

### Priority 2 — Blackout and weekday re-evaluation
**Why second:** The current SIGNAL_BLACKOUT_END=13:30 and Thursday exclusion were set before
MIN_TDO was restored to 15. Isolation testing showed the blackout extension has mixed fold effects
at MIN_TDO=50 — needs fresh measurement at MIN_TDO=15. Re-evaluate jointly.
Search: SIGNAL_BLACKOUT_END ∈ ["12:00", "13:00", "13:30"]; ALLOWED_WEEKDAYS ∈ [all, no-Thu].
Optimise for: mean_test_pnl (primary), min_test_pnl > 0 (secondary).

### Priority 3 — Stop ratio fine-tune
Grid search: SHORT_STOP_RATIO ∈ [0.25, 0.30, 0.35, 0.40]
**Why third:** At 5m bars, false wicks required a wide stop (0.40) for breathing room. At 1m, wick
noise is substantially reduced (wick gaps are much smaller bar-by-bar), so a tighter ratio may be
viable without increasing stop-outs. Tighter stop → higher R:R → better expected value if win rate
holds. Search downward from the baseline 0.40.
Optimise for: mean_test_pnl (primary), avg_rr and win_rate (secondary).

### Priority 4 — Pre-TDO progress stop lock-in
Grid search: BREAKEVEN_TRIGGER_PCT ∈ [0.0, 0.50, 0.60, 0.65, 0.70, 0.75]
**Why fourth:** Stop-out rate is ~94% at 1m. Locking stop to entry once price is 50–75% toward TDO
converts some losing stops into breakevens. Only viable after Priority 1 widens the effective stop gap.
Optimise for: mean_test_pnl (primary), reduction in max_drawdown (secondary).

### Priority 5 — Fine-tune MIN_TDO_DISTANCE_PTS
Grid search: [0.0, 10.0, 15.0, 20.0, 25.0]
**Note:** Isolation testing showed raising MIN_TDO from 15→50 was a -793 regression. 15 is the
empirically best baseline; search around it for marginal gains only.
Optimise for: avg_pnl_per_trade (primary), total_test_trades ≥ 8/fold (secondary).

### Priority 5b — Re-entry threshold (low confidence)
Grid search: REENTRY_MAX_MOVE_PTS ∈ [0.0, 5.0, 10.0, 20.0]
**Note:** Isolation testing showed re-entry at 20.0 was net negative (-110 mean, -142 min).
Only revisit after Priority 1 changes the post-TDO exit profile — a wider trail may change
whether post-stop divergences are worth re-entering.
Optimise for: mean_test_pnl with total_trades guard ≥ 8/fold.

### Priority 6 — Time-based stop (MAX_HOLD_BARS)
Grid search: MAX_HOLD_BARS ∈ [0, 60, 120, 180, 240] (bars at 1m resolution = 1–4 hours)
**Note:** Session-close exits are ~6% of trades at 1m (was ~31% at 5m) — far less at risk.
Only useful if analysis shows long-held losing trades dominating the stop-out population.
Optimise for: mean_test_pnl net of session-close trade count.

### Priority 7 — Intermediate TP (INTERMEDIATE_TP_RATIO)
Add INTERMEDIATE_TP_RATIO ∈ [0.0, 0.3, 0.5, 0.7] — partial exit before TDO.
Only implement if Priorities 1–6 leave residual session-close drag. Requires new constant + exit
check in manage_position — not yet wired.

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
