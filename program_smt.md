# SMT Divergence Strategy Optimizer

Autonomous SMT divergence strategy optimizer: iterates on signal logic and tuning parameters in `strategy_smt.py` to maximize `mean_test_pnl` on the walk-forward evaluation of MNQ1! futures.

---

## Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| **Timeframe** | `BACKTEST_START`=2024-09-01 to `BACKTEST_END`=2026-03-20 | Fixed futures backtest window; defined in `backtest_smt.py` (do not edit). |
| **Instruments** | MNQ1!, MES1! | Continuous futures contracts. Data fetched by `prepare_futures.py`. |
| **Iterations** | open-ended | Work through groups in order; stop when further gains are marginal. |

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
| `SESSION_END` | "13:30" | ["11:00", "12:00", "13:30", "14:00", "15:15"] |
| `MIN_BARS_BEFORE_SIGNAL` | 0 | [0, 10, 20, 30] (minutes) |
| `TRADE_DIRECTION` | "both" | ["both", "short"] |
| `SHORT_STOP_RATIO` | 0.35 | [0.25, 0.30, 0.35, 0.40, 0.45] |
| `LONG_STOP_RATIO` | 0.35 | [0.25, 0.30, 0.35, 0.40, 0.45] |
| `MIN_STOP_POINTS` | 2.5 | do not change |
| `MIN_TDO_DISTANCE_PTS` | 0.0 | [0.0, 10.0, 15.0, 20.0, 25.0] |
| `MAX_TDO_DISTANCE_PTS` | 15.0 | [15.0, 20.0, 25.0, 30.0, 40.0, 999.0] |
| `MAX_REENTRY_COUNT` | 1 | [1, 2, 3, 4, 999] |
| `REENTRY_MAX_MOVE_PTS` | 999.0 | [0.0, 5.0, 10.0, 20.0, 999.0] |
| `BREAKEVEN_TRIGGER_PCT` | 0.0 | [0.0, 0.50, 0.60, 0.65, 0.70, 0.75] |
| `MAX_HOLD_BARS` | 120 | [60, 120, 240, 0] |
| `ALLOWED_WEEKDAYS` | {0,1,2,3,4} | [{0,1,2,3,4}, frozenset({0,1,2,4})] |
| `SIGNAL_BLACKOUT_START` | "11:00" | ["", "11:00"] |
| `SIGNAL_BLACKOUT_END` | "13:00" | ["", "12:00", "13:00", "13:30"] |
| `TRAIL_AFTER_TP_PTS` | 0.0 | [0.0, 25.0, 50.0, 75.0, 100.0] |
| `TRAIL_ACTIVATION_R` | 1.0 | [0.0, 0.5, 1.0, 1.5, 2.0] |
| `MIN_SMT_SWEEP_PTS` | 0.0 | [0.0, 1.0, 2.0, 5.0] |
| `MIN_SMT_MISS_PTS` | 0.0 | [0.0, 1.0, 2.0, 5.0] |
| `LIMIT_ENTRY_BUFFER_PTS` | 3.0 | [1.0, 2.0, 3.0] |
| `LIMIT_EXPIRY_SECONDS` | 120.0 | [60.0, 120.0, 300.0] |
| `LIMIT_RATIO_THRESHOLD` | None | [None, 0.40, 0.50, 0.60, 0.70] |
| `PARTIAL_STOP_BUFFER_PTS` | 2.0 | [0.5, 1.0, 2.0, 3.0] |

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
| `PARTIAL_EXIT_LEVEL_RATIO` | 0.33 | [0.33, 0.50, 0.67] |

### ICT Structure Features (untested at current quality level)

| Constant | Current Default | Optimizer Search Space |
|----------|----------------|------------------------|
| `MIDNIGHT_OPEN_AS_TP` | False | [True, False] |
| `SILVER_BULLET_WINDOW_ONLY` | False | [True, False] |
| `OVERNIGHT_SWEEP_REQUIRED` | False | [True, False] |
| `OVERNIGHT_RANGE_AS_TP` | False | [True, False] — only with OVERNIGHT_SWEEP_REQUIRED=True |
| `TWO_LAYER_POSITION` | False | [True, False] — requires FVG_ENABLED=True |
| `FVG_ENABLED` | False | [True, False] |
| `FVG_LAYER_B_TRIGGER` | False | [True, False] |
| `FVG_MIN_SIZE_PTS` | 2.0 | [1.0, 2.0, 3.0, 5.0] |
| `SMT_FILL_ENABLED` | False | [True, False] |
| `INVALIDATION_MSS_EXIT` | False | [True, False] |
| `INVALIDATION_CISD_EXIT` | False | [True, False] |
| `INVALIDATION_SMT_EXIT` | False | [True, False] |
| `SYMMETRIC_SMT_ENABLED` | False | [True, False] |
| `ALWAYS_REQUIRE_CONFIRMATION` | False | [True, False] |
| `HTF_VISIBILITY_REQUIRED` | False | [True, False] |
| `EXPANDED_REFERENCE_LEVELS` | False | [True, False] |

### Do NOT Change

- `STRUCTURAL_STOP_MODE = False` — tested and rejected: stop beyond wick extreme kills RR vs ratio stop
- `MIN_DISPLACEMENT_BODY_PTS = 0.0` — tested and rejected: displacement body size filter adds no value
- `CONFIRMATION_BAR_MINUTES = 1` — tested, confirmed optimal; do not change
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
| Win rate (avg across folds) | ≥ 0.75 | Guard |
| `avg_rr` (avg across folds) | ≥ 1.5 | Guard |
| Total test trades (sum) | ≥ 450 | Volume guard |
| `min_test_pnl` | ≥ $2,000 (no weak folds) | Secondary guard |
| `avg_pnl_per_trade` (avg across folds) | ≥ $25 | Quality guard |

When two iterations both satisfy all guards, prefer the one with higher `mean_test_pnl`.

_Thresholds last updated after corrected limit-entry fill direction (SHORT fills on bar Low ≤ entry_price). Current baseline WR=85.5%, avg_rr=2.14, min_test_pnl=$3,354 — guards set ~10pp below WR baseline to catch genuine regressions without blocking mild parameter-tuning noise._

**Baseline (Plans 1–3 + LIMIT_ENTRY_BUFFER_PTS=3 + LIMIT_EXPIRY_SECONDS=120 + corrected fill direction, full 6-fold walk-forward 2024-09-01 → 2026-03-20):**

| Fold | Test Trades | Win Rate | Test PnL | avg_rr |
|------|------------|----------|----------|--------|
| 1    | 110        | 87.3%    | $3,851   | 1.75   |
| 2    | 120        | 88.3%    | $5,287   | 2.06   |
| 3    | 104        | 83.7%    | $4,983   | 2.48   |
| 4    | 126        | 88.1%    | $4,704   | 1.74   |
| 5    | 91         | 81.3%    | $4,648   | 2.96   |
| 6    | 76         | 84.2%    | $3,354   | 1.84   |

- **mean_test_pnl**: $4,471 | **min_test_pnl**: $3,354 | **Total test trades**: 627 | **Avg WR**: 85.5% | **Avg avg_rr**: 2.14

---

## Optimization Agenda

Work through in priority order. At each step, start from the **current best accepted configuration**.
Primary metric: `mean_test_pnl`. Guards: WR ≥ 0.65, avg_rr ≥ 1.8, total_test_trades ≥ 400, min_test_pnl ≥ $0, avg_pnl_per_trade ≥ $20.

---

### Group 1 — Limit Entry Mechanics (Iterations 1–2)

The new pullback limit entry (`anchor_close ± LIMIT_ENTRY_BUFFER_PTS`, expires after `LIMIT_EXPIRY_SECONDS`) is the core change from the prior baseline. Optimizing these is the first priority because they directly control fill rate, fill quality, and effective RR.

#### Iteration 1 — LIMIT_ENTRY_BUFFER_PTS × LIMIT_EXPIRY_SECONDS grid

Test all combinations in `[1.0, 2.0, 3.0] × [60.0, 120.0, 300.0]` (9 combos).

Smaller buffer fills faster, recovering volume lost to the pullback requirement; larger buffer gets a better entry price but misses more setups. Longer expiry tolerates slower retracements but may fill into deteriorating setups. Grid the full 3×3 space from the current default (3.0, 120.0).

#### Iteration 2 — LIMIT_RATIO_THRESHOLD

Test: `[None, 0.40, 0.50, 0.60, 0.70]`

Suppresses limit entries where `LIMIT_ENTRY_BUFFER_PTS / (TDO distance)` exceeds the threshold — i.e., where the buffer consumes a large fraction of the trade's available travel. Without this guard, a 3-pt buffer on a 4-pt TDO setup would still place a limit entry with almost no room to TP. None = disabled (current default).

---

### Group 2 — Stop and Exit Mechanics (Iterations 3–7)

#### Iteration 3 — SHORT_STOP_RATIO

Test: `[0.25, 0.30, 0.35, 0.40, 0.45]`

Primary RR lever for shorts. At the new baseline WR of ~54%, the optimal ratio may shift: tighter stops (0.25–0.30) improve RR at cost of more stop-outs; looser stops (0.40–0.45) reduce frequency but risk more capital per trade.

#### Iteration 4 — LONG_STOP_RATIO

Test: `[0.25, 0.30, 0.35, 0.40, 0.45]`

Test after SHORT_STOP_RATIO is locked. Long and short setups have different structural characteristics (longs tend to have smaller PnL contribution at the current baseline) — asymmetric ratios may be optimal.

#### Iteration 5 — PARTIAL_EXIT_LEVEL_RATIO

Test: `[0.33, 0.50, 0.67]`

With the partial-exit level bug fixed (level now correctly tracks the actual selected TP, not the placeholder TDO), re-calibrate where in the trade's travel to take partial profits. 0.33 = early lock-in; 0.67 = hold most of the trade to near-TP.

#### Iteration 6 — PARTIAL_STOP_BUFFER_PTS

Test: `[0.5, 1.0, 2.0, 3.0]`

After partial exit, stop slides to `partial_exit_price ± PARTIAL_STOP_BUFFER_PTS`. Current default 2.0. Tighter buffer (0.5–1.0) locks in more P&L from the first leg but risks noise stop-out; looser (3.0) gives more room at the cost of giving back gain.

#### Iteration 7 — BREAKEVEN_TRIGGER_PCT

Test: `[0.0, 0.50, 0.60, 0.65, 0.70, 0.75]`

Slides stop to entry after price travels X% toward TP. Currently disabled (0.0). At WR=54% with avg_rr=1.43, converting partial losers to breakeven could meaningfully reduce drawdown. Expected: lower max_drawdown, mild reduction in avg_rr.

---

### Group 3 — Target Selection (Iterations 8–9)

#### Iteration 8 — MAX_TDO_DISTANCE_PTS

Test: `[15.0, 20.0, 25.0, 30.0, 40.0, 999.0]`

Upper-bound filter on |entry − TDO|. Current default 15.0. With the limit buffer shifting fill prices, the effective TDO distance at entry has changed slightly; the optimal ceiling may have moved. Lower ceilings filter wide, lower-probability setups; 999 = disabled.

#### Iteration 9 — MIN_TDO_DISTANCE_PTS

Test: `[0.0, 10.0, 15.0, 20.0, 25.0]`

Floor on TDO distance. Very tight TDO setups produce degenerate stops and poor RR. Currently 0.0 (disabled). A floor of 10–15 pts may improve average quality without significant volume loss.

---

### Group 4 — Signal Quality Filters (Iterations 10–13)

#### Iteration 10 — MIN_SMT_SWEEP_PTS

Test: `[0.0, 1.0, 2.0, 5.0]`

Minimum distance MES must exceed the prior session extreme. Marginal sweeps (< 1 pt) may be noise. Expected: fewer signals but higher conviction per trade.

#### Iteration 11 — MIN_SMT_MISS_PTS

Test: `[0.0, 1.0, 2.0, 5.0]`

Minimum distance MNQ must fail to match MES. A 3-pt miss is a stronger divergence signal than a 0.5-pt one. Test after MIN_SMT_SWEEP_PTS is locked — they are correlated and interact.

#### Iteration 12 — MAX_REENTRY_COUNT + REENTRY_MAX_MOVE_PTS

Test `MAX_REENTRY_COUNT` ∈ `[1, 2, 3, 4, 999]` with `REENTRY_MAX_MOVE_PTS` ∈ `[0.0, 5.0, 10.0, 20.0, 999.0]`.

Currently 1 re-entry allowed regardless of how far price has moved. Test whether allowing 2–3 re-entries recovers useful volume, and whether capping `REENTRY_MAX_MOVE_PTS` filters late re-entries after significant adverse movement.

#### Iteration 13 — MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT

Test: `[0, 2, 3]`

Gates displacement entries to sessions where at least N ICT/hypothesis rules confirm direction. Score 0 = disabled (current). Score 2 requires pd_range + one additional confirmation. Expected: fewer displacement entries, higher per-trade WR on that subset.

---

### Group 5 — Re-validate Plan 1–3 Defaults (Iterations 14–17)

These features were approved at an earlier quality level and baseline. Re-confirm with the new entry mechanics and partial-exit fix in place.

#### Iteration 14 — HIDDEN_SMT_ENABLED

Test: `[True, False]`

Approved for +30.6% PnL at earlier baseline. Confirm the gain holds with the limit buffer entry and corrected partial-exit level calculation.

#### Iteration 15 — DISPLACEMENT_STOP_MODE

Test: `[True, False]`

Bar-extreme stop for displacement entries (stop placed beyond the displacement bar's wick). Approved in 1-fold test. Re-confirm across all 6 folds.

#### Iteration 16 — SMT_OPTIONAL + MIN_DISPLACEMENT_PTS

Test `SMT_OPTIONAL` ∈ `[True, False]`; if True accepted, test `MIN_DISPLACEMENT_PTS` ∈ `[8.0, 10.0, 15.0]`.

Pure displacement entries add volume. Re-test whether this volume is profitable at the current quality level. If SMT_OPTIONAL=False wins, skip MIN_DISPLACEMENT_PTS.

#### Iteration 17 — TRAIL_AFTER_TP_PTS × TRAIL_ACTIVATION_R

**Step A** — Lock `TRAIL_AFTER_TP_PTS` ∈ `[0.0, 25.0, 50.0, 75.0, 100.0]` with `TRAIL_ACTIVATION_R` held at 1.0.

`TRAIL_AFTER_TP_PTS = 0.0` is the current default (exit exactly at TDO, no trailing). Test wider values `[25.0, 50.0, 75.0, 100.0]` to see whether trailing past TDO captures trend extension; note that wider trails convert some TPs into stop-outs on reversals.

**Step B** — After Step A, lock the best `TRAIL_AFTER_TP_PTS` and test `TRAIL_ACTIVATION_R` ∈ `[0.0, 0.5, 1.0, 1.5, 2.0]`.

`0.0` = trail activates immediately at TDO crossing (legacy); `1.0` = trail waits until price has moved 1× the initial stop distance past TDO; higher values reduce false activations on shallow TDO breaches. Note: when `TRAIL_AFTER_TP_PTS > 0`, the partial-exit stop-slide still runs (protecting against TDO-touch reversals) but contract reduction is skipped — the full position stays open for trend capture.

---

### Group 6 — ICT Structure Features (Iterations 18–27)

Test each feature independently from the best configuration to date. Most are untested at the current quality level.

#### Iteration 18 — FVG_ENABLED + FVG_MIN_SIZE_PTS

Test `FVG_ENABLED` ∈ `[True, False]`; if True accepted, test `FVG_MIN_SIZE_PTS` ∈ `[1.0, 2.0, 3.0, 5.0]`.

Fair Value Gap confirmation gate. FVG data is computed but currently bypassed. Test whether requiring a nearby FVG improves signal quality. FVG_ENABLED=True is also prerequisite for TWO_LAYER_POSITION (Iteration 24).

#### Iteration 19 — SILVER_BULLET_WINDOW_ONLY

Test: `[False, True]`

Restricts divergence detection to the 09:50–10:10 ET ICT kill zone. Significant volume reduction expected. Only accept if total_test_trades ≥ 350 after filtering.

#### Iteration 20 — OVERNIGHT_SWEEP_REQUIRED

Test: `[False, True]`

Requires the overnight high (shorts) or low (longs) to be swept before the session signal fires. Provides liquidity-clearance confirmation. Previously neutral — re-test at current quality level.

#### Iteration 21 — INVALIDATION_CISD_EXIT

Test: `[False, True]`

Exit on Change in State of Delivery. Previously excluded because exits fired too close to entry. Re-test: the limit buffer entry increases the initial travel distance before any CISD signal can fire, which may make this viable.

#### Iteration 22 — INVALIDATION_SMT_EXIT

Test: `[False, True]`

Exit if the MES/MNQ relationship inverts. Same caveat as CISD — the limit buffer should provide more separation between entry and any invalidation signal.

#### Iteration 23 — INVALIDATION_MSS_EXIT

Test: `[False, True]`

Exit on Market Structure Shift against the trade direction. Less aggressive than CISD or SMT. Test whether early structural exits reduce drawdown without sacrificing wins.

#### Iteration 24 — TWO_LAYER_POSITION + FVG_LAYER_B_TRIGGER

Test: `TWO_LAYER_POSITION=True, FVG_ENABLED=True, FVG_LAYER_B_TRIGGER=True` vs baseline.

Layer A at divergence signal, Layer B on FVG retracement. Requires FVG_ENABLED=True (set from Iteration 18 result, or enable here if FVG was rejected). Previously neutral due to zero layer_b_triggers in the test window — re-test across all 6 folds.

#### Iteration 25 — SYMMETRIC_SMT_ENABLED

Test: `[False, True]`

Allows same-direction SMT patterns in addition to divergence. Expands signal universe. Previously untested at current quality level.

#### Iteration 26 — ALWAYS_REQUIRE_CONFIRMATION

Test: `[False, True]`

Forces a confirmation bar for all entry types including displacement entries. Reduces volume; expected effect is higher WR at cost of fewer trades.

#### Iteration 27 — HTF_VISIBILITY_REQUIRED + EXPANDED_REFERENCE_LEVELS

Test each independently: `[False, True]`.

HTF visibility gates entries on higher-timeframe alignment. Expanded reference levels adds more liquidity targets to the TP candidate pool. Test independently; if both accepted, test combined.

---

### Group 7 — Session and Timing (Iterations 28–29)

#### Iteration 28 — TRADE_DIRECTION + ALLOWED_WEEKDAYS

Test `TRADE_DIRECTION` ∈ `["both", "short"]`. If "short" accepted, repurpose this iteration to also test `ALLOWED_WEEKDAYS` ∈ `[{0,1,2,3,4}, frozenset({0,1,2,4})]`.

At the current baseline, longs contribute negatively in several folds. Thursday has historically underperformed — confirm with the new baseline.

#### Iteration 29 — SESSION_END + SIGNAL_BLACKOUT

Test `SESSION_END` ∈ `["11:00", "12:00", "13:30", "14:00", "15:15"]`. Test blackout combinations if SESSION_END change is marginal.

`SESSION_END` is currently `"13:30"`. Test whether extending to 14:00 or 15:15 adds value — the post-blackout (13:00–) window would add afternoon signals. Also test earlier cutoffs (11:00, 12:00) to see if limiting to the morning kill zone improves quality. `"10:30"` dropped from the grid — below the blackout start, it would block all morning signals.

---

### Group 8 — Combinatorial and Final (Iterations 30–31)

#### Iteration 30 — Best Stop + Entry Combo

Using the best SHORT_STOP_RATIO and LONG_STOP_RATIO from Group 2, test them simultaneously with the best LIMIT_ENTRY_BUFFER_PTS × LIMIT_EXPIRY_SECONDS pair from Group 1. These interact: better fill quality (limit buffer) may justify a tighter stop; the stop was optimized against the 3-pt-buffer baseline and the combined optimum may differ.

#### Iteration 31 — Final Best-of-All

Apply all accepted changes simultaneously and run the full walk-forward as a final sanity check. Compare `mean_test_pnl`, `min_test_pnl`, win rate, and total_test_trades against both the original baseline and the current best. This is the submission candidate.

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
- If Group 1 grid shows that None (no limit buffer) is optimal, drop LIMIT_RATIO_THRESHOLD (Iter 2) and revert to market entry baseline.
- If TRADE_DIRECTION="short" wins in Iter 28, skip LONG_STOP_RATIO (Iter 4) and repurpose for an unlisted parameter.
- If win rate climbs above 0.65 after Groups 1–2, tighter quality filters (Group 4) may be counterproductive — prioritise volume recovery instead.
- If STRUCTURAL_STOP_MODE becomes re-relevant after a stop-ratio change, revisit it despite the locked status (with explicit note).

---

## Strategy Notes

- **One trade per day maximum** — harness only enters if no open position
- **Direction**: both longs and shorts active (`TRADE_DIRECTION = "both"`)
- **Kill zone**: 09:00–15:15 ET; signal blackout 11:00–13:00 suppresses dead zone
- **TDO anchor**: 9:30 AM ET bar open is TP target and stop-placement basis; first-bar proxy if 9:30 bar absent
- **Stop formula (short)**: `entry + SHORT_STOP_RATIO × |entry − TDO|`; at ratio 0.35 → ~2.86:1 R:R
- **Stop formula (long)**: `entry − LONG_STOP_RATIO × |entry − TDO|`; at ratio 0.35 → ~2.86:1 R:R
- **Limit entry**: for SHORT, place SELL limit at `anchor_close − LIMIT_ENTRY_BUFFER_PTS`; fill check is `bar["Low"] <= entry_price` (price must drop down to the level); for LONG, BUY limit at `anchor_close + LIMIT_ENTRY_BUFFER_PTS`, fill check is `bar["High"] >= entry_price`; expires after `LIMIT_EXPIRY_SECONDS`
- **SMT divergence**: bearish = MES new session high + MNQ failure; bullish = MES new session low + MNQ failure
- **Displacement entries**: large-body candle (body ≥ MIN_DISPLACEMENT_PTS) when no wick SMT fires; stop = bar extreme when DISPLACEMENT_STOP_MODE=True
- **Partial exit**: closes `PARTIAL_EXIT_FRACTION` of contracts at `PARTIAL_EXIT_LEVEL_RATIO × |entry − TP|` distance from entry; stop then slides to `partial_exit_price ± PARTIAL_STOP_BUFFER_PTS` and secondary TP is promoted
- **Ticker naming**: IB uses `MNQ` / `MES` (not `MNQ1!` / `MES1!`)

---

## Validation Before Submitting a Change

```bash
uv run python -m pytest tests/test_smt_strategy.py tests/test_smt_backtest.py tests/test_hypothesis_smt.py -x -q
```

All tests must pass before recording the result.
