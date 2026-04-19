# SMT Strategy — Experiment Log

This file is the canonical record of completed optimization experiments across all plan rounds.
A future agent implementing the next optimization round should read this first to understand
what has been tested, what the verdicts were, and what conditions must be met before retesting
rejected or neutral flags.

---

## Round 1 — Plan 1 Signal Quality Flags (2026-04-20)

**Test window:** 2026-01-19 → 2026-04-12 (60-day 1-fold fast mode via `plan1_experiment_runner.py`)
**Baseline:** trades=36, pnl=$3,885.80, wr=55.6%, avg_rr=7.47, appt=$107.94, max_dd=$130.20
**Raw data:** `plan1-results.tsv`

### Results

| Flag | Delta PnL | Verdict | Mechanistic Reason |
|------|-----------|---------|-------------------|
| `MIDNIGHT_OPEN_AS_TP=True` | $0 | NEUTRAL | Midnight open ≈ TDO in Jan–Apr 2026; trailing stop equalizes exit regardless of TP level |
| `STRUCTURAL_STOP_MODE=True` (2pt) | −$1,179 | REJECT | Wider stop moves trail anchor further from entry → smaller wins per trade; RR collapses 7.47→2.08 |
| `STRUCTURAL_STOP_MODE=True` (3pt) | −$1,119 | REJECT | Same pattern at larger buffer; confirms mode is incompatible with trailing-stop exits |
| `SILVER_BULLET_WINDOW_ONLY=True` | $0 | NEUTRAL | All 36 baseline trades already fall within 09:50–10:10 ET window; no filtering effect |
| `OVERNIGHT_SWEEP_REQUIRED=True` | $0 | NEUTRAL | All 36 baseline trades already had overnight sweeps; gate has no bite in this period |
| `OVERNIGHT_RANGE_AS_TP=True` | $0 | NEUTRAL | Overnight range equals TDO in this period; same trailing-stop equalization as midnight TP |
| **`HIDDEN_SMT_ENABLED=True`** | **+$1,189 (+30.6%)** | **APPROVE** | Close-based (body) SMT captures 9 extra trades; same WR/RR quality, lower drawdown |
| `INVALIDATION_MSS_EXIT=True` | −$2,757 | REJECT | 22/36 trades exit early via close-below-div-bar-low; drawdown 5.5×; condition fires too close to entry |
| `INVALIDATION_SMT_EXIT=True` | −$2,551 | REJECT | 20/36 early exits via SMT-defended-level breach; same destruction; session extreme is too close to entry |
| `INVALIDATION_CISD_EXIT=True` | $0 | NEUTRAL | Midnight open never breached on close after entry in this period; condition inactive |

### Approved Flag

`HIDDEN_SMT_ENABLED = True` — set as default in `strategy_smt.py` after Round 1.
New effective baseline: trades=45, pnl=$5,075, wr=57.8%, avg_rr=7.25, appt=$112.78, max_dd=$108.

### Re-test Conditions (before including in future optimization)

**`INVALIDATION_MSS_EXIT` / `INVALIDATION_SMT_EXIT`** — re-test when a `MIN_FAVORABLE_MOVE_PTS`
guard is added: condition should only activate after price has moved at least X pts in-favor first.
Without that guard, it fires immediately on any adverse close near entry, cutting every winner short.
Suggested guard: 5–10 MNQ pts of favorable move before exit condition arms.

**`STRUCTURAL_STOP_MODE`** — do not re-test until exit mechanism changes away from trailing-stop.
The incompatibility is structural: wider stop = larger trail anchor offset = smaller profit-lock.
Would only make sense with a fixed-target exit (`TRAIL_AFTER_TP_PTS = 0.0`).

**`MIDNIGHT_OPEN_AS_TP` / `OVERNIGHT_RANGE_AS_TP` / `OVERNIGHT_SWEEP_REQUIRED`** — neutral in
this 60-day window because overnight structure was homogeneous. Re-test after extending data window
or in a period with strong overnight directional moves. Note: `compute_overnight_range()` has a
performance issue — per-day `.index.date` filter on 807K-row DataFrame. Pre-compute as a
day-indexed dict at load time before running these in any batch experiment.

**`SILVER_BULLET_WINDOW_ONLY`** — neutral because all baseline setups cluster in the 09:50–10:10
window already. Useful as a noise filter in expanded-universe or longer-window tests where
off-window signals may become more common.

**`INVALIDATION_CISD_EXIT`** — only meaningful when midnight open diverges significantly from
entry zone. Re-test alongside `MIDNIGHT_OPEN_AS_TP=True` in a period with large overnight gaps.

---

## Round 2 — Plan 2 Position Architecture

**Status:** Complete (2026-04-20). All 10 runs finished (D-1 completed in background before kill signal reached it).

**Runner:** `uv run python plan2_experiment_runner.py [FLAG=VALUE ...]`

**Test window:** Same 1-fold fast mode as Round 1 (60 business days ending at `TRAIN_END`).

**Effective baseline entering Round 2:**
trades=45, pnl=$5,075, wr=57.8%, avg_rr=7.25, appt=$112.78, max_dd=$108
(Plan 1 baseline + HIDDEN_SMT_ENABLED=True. All Plan 2 constants default to False/0.0.)

**Confirming the baseline before any experiments:**
```
uv run python plan2_experiment_runner.py
```
Output should show `final_trades=45`, `final_pnl≈$5,075`. If not, stop and investigate before
proceeding — a drift here means something changed in strategy_smt.py or the data window.

---

### How to Run Each Group

#### Run A — SMT-optional displacement entries (`SMT_OPTIONAL`)

Adds entries triggered by a large single-bar displacement (body ≥ MIN_DISPLACEMENT_PTS) even
when no wick SMT exists. Only fires when `detect_smt_divergence` returns None — never on the
same bar as a wick/body SMT. `smt_type="displacement"` in trade records.

```
uv run python plan2_experiment_runner.py SMT_OPTIONAL=True MIN_DISPLACEMENT_PTS=8.0
uv run python plan2_experiment_runner.py SMT_OPTIONAL=True MIN_DISPLACEMENT_PTS=10.0
uv run python plan2_experiment_runner.py SMT_OPTIONAL=True MIN_DISPLACEMENT_PTS=15.0
```

Key metrics to compare: `displacement_entries` (how many new entries were generated),
`final_trades`, `final_pnl`, `win_rate`, `avg_rr`.

Watch for: displacement entries with lower win rate than baseline (momentum entries in an
mean-reverting strategy can hurt). If `displacement_entries=0`, the threshold is too tight
for this period — report as NEUTRAL rather than REJECT.

---

#### Run B — Partial exit at first liquidity draw (`PARTIAL_EXIT_ENABLED`)

Closes `PARTIAL_EXIT_FRACTION` of contracts at the midpoint between entry and TP, then lets
the remainder run to TP/stop/session_close. Reduces drawdown at the cost of smaller final wins.

```
uv run python plan2_experiment_runner.py PARTIAL_EXIT_ENABLED=True PARTIAL_EXIT_FRACTION=0.33
uv run python plan2_experiment_runner.py PARTIAL_EXIT_ENABLED=True PARTIAL_EXIT_FRACTION=0.5
```

**Metric interpretation — critical:** Each trade now produces 2 records. Do NOT use
`total_trades` or `mean_test_pnl` for comparison — they double-count. Use:
- `final_pnl` vs baseline $5,075 → true net P&L impact
- `final_trades` vs baseline 45 → should be identical (same entries, same final exits)
- `win_rate` and `avg_rr` computed on final exits only
- `partial_trades` → how many partial exits fired (tells you how often midpoint was reached)
- `max_drawdown` → main benefit of partial exit is drawdown reduction even at same P&L

Verdict logic: APPROVE if `final_pnl` is within ~5% of baseline AND `max_drawdown` drops
meaningfully. The goal is risk reduction, not P&L increase. REJECT if `final_pnl` drops >10%.

---

#### Run C — Two-layer position model (`TWO_LAYER_POSITION` + `FVG_ENABLED` + `FVG_LAYER_B_TRIGGER`)

Layer A enters at `LAYER_A_FRACTION` of sized contracts. Layer B enters only when price
retraces into the FVG zone identified at signal time. Combined stop tightens to FVG boundary
on Layer B entry.

**All three flags must be set together** — enabling any subset is not a valid test config:
- `FVG_ENABLED=True` alone: detect_fvg runs but Layer B never fires (FVG_LAYER_B_TRIGGER=False)
- `TWO_LAYER_POSITION=True` alone: enters at fraction of contracts but never adds (no FVG trigger)

```
uv run python plan2_experiment_runner.py FVG_ENABLED=True TWO_LAYER_POSITION=True FVG_LAYER_B_TRIGGER=True LAYER_A_FRACTION=0.33 FVG_MIN_SIZE_PTS=2.0
uv run python plan2_experiment_runner.py FVG_ENABLED=True TWO_LAYER_POSITION=True FVG_LAYER_B_TRIGGER=True LAYER_A_FRACTION=0.5 FVG_MIN_SIZE_PTS=2.0
uv run python plan2_experiment_runner.py FVG_ENABLED=True TWO_LAYER_POSITION=True FVG_LAYER_B_TRIGGER=True LAYER_A_FRACTION=0.5 FVG_MIN_SIZE_PTS=3.0
uv run python plan2_experiment_runner.py FVG_ENABLED=True TWO_LAYER_POSITION=True FVG_LAYER_B_TRIGGER=True LAYER_A_FRACTION=0.67 FVG_MIN_SIZE_PTS=2.0
```

Key metrics: `layer_b_triggers` (how often Layer B entered — if 0, FVG retracements never
occurred in this period → NEUTRAL, not REJECT), `final_pnl`, `max_drawdown`.

Watch for: if `layer_b_triggers=0`, the model degrades to a smaller-position-only strategy
(LAYER_A_FRACTION contracts throughout) → expect `final_pnl` to be proportionally lower than
baseline. That's expected behavior, not a bug. Report `layer_b_triggers` explicitly.

---

#### Run D — SMT fill divergence (`SMT_FILL_ENABLED`)

Detects inter-instrument FVG fill divergence: MES reaches a bearish FVG zone that MNQ has not
(or bullish analog). Fires as an alternative to wick SMT in the IDLE detection loop.
`smt_type="fill"` in trade records.

```
uv run python plan2_experiment_runner.py SMT_FILL_ENABLED=True
```

Key metrics: `fill_entries` (if 0 → NEUTRAL, this period had no qualifying fill divergences),
`final_pnl`, `win_rate`. This feature requires `FVG_ENABLED=False` to avoid double-detecting
the same zones — the runner leaves FVG_ENABLED at its default (False) so this is already correct.

---

### Verdict Framework (same as Round 1)

| Result | Criterion | Action |
|--------|-----------|--------|
| APPROVE | `final_pnl` > baseline AND `win_rate`/`avg_rr` stable OR `max_drawdown` improves materially | Set flag to True as new default in `strategy_smt.py` |
| NEUTRAL | `final_pnl` within ±5% of baseline, no meaningful improvement in any metric | Leave False; document re-test conditions |
| REJECT | `final_pnl` < baseline −10% OR win_rate collapses | Leave False; document why |

For partial exit (Run B): use `final_pnl` vs baseline and `max_drawdown` as primary signal.
For two-layer (Run C): if `layer_b_triggers=0`, call NEUTRAL and document re-test condition.
For displacement/fill (Runs A, D): if entry count = 0 for a given threshold, call NEUTRAL.

---

### Results

_Fill in after running experiments._

| Run | Config | final_pnl | final_trades | win_rate | avg_rr | max_dd | layer_b | displacement | fill | Verdict |
|-----|--------|-----------|--------------|----------|--------|--------|---------|--------------|------|---------|
| Baseline | (all Plan 2 flags off) | $5,075 | 45 | 57.8% | 7.25 | $108 | — | — | — | — |
| A-1 | SMT_OPTIONAL=True, MIN_DISP=8 | $3,844 | 50 | 52.0% | 5.78 | $155 | 0 | 33 | 0 | REJECT |
| A-2 | SMT_OPTIONAL=True, MIN_DISP=10 | $3,520 | 44 | 52.3% | 6.08 | $102 | 0 | 24 | 0 | REJECT |
| A-3 | SMT_OPTIONAL=True, MIN_DISP=15 | $4,386 | 40 | 57.5% | 6.93 | $158 | 0 | 12 | 0 | REJECT |
| B-1 | PARTIAL_EXIT=True, FRAC=0.33 | $4,798 | 45 | 62.2% | 6.56 | $95 | 0 | 0 | 0 | **APPROVE** |
| B-2 | PARTIAL_EXIT=True, FRAC=0.5 | $3,229 | 45 | 62.2% | 4.75 | $95 | 0 | 0 | 0 | REJECT |
| C-1 | TWO_LAYER+FVG+LB, A_FRAC=0.33, FVG_MIN=2 | $1,258 | 45 | 57.8% | 6.74 | $30 | 0 | 0 | 0 | NEUTRAL |
| C-2 | TWO_LAYER+FVG+LB, A_FRAC=0.5, FVG_MIN=2 | $2,530 | 45 | 57.8% | 7.07 | $54 | 0 | 0 | 0 | NEUTRAL |
| C-3 | TWO_LAYER+FVG+LB, A_FRAC=0.5, FVG_MIN=3 | $2,530 | 45 | 57.8% | 7.07 | $54 | 0 | 0 | 0 | NEUTRAL |
| C-4 | TWO_LAYER+FVG+LB, A_FRAC=0.67, FVG_MIN=2 | $2,530 | 45 | 57.8% | 7.07 | $54 | 0 | 0 | 0 | NEUTRAL |
| D-1 | SMT_FILL_ENABLED=True | $5,075 | 45 | 57.8% | 7.25 | $109 | 0 | 0 | 0 | NEUTRAL |

### Approved Flags

- **PARTIAL_EXIT_ENABLED = True** — reduces max_drawdown from $108 → $95 (−12%) at a cost of −$277 P&L (−5.5%). Risk-adjusted improvement.
- **PARTIAL_EXIT_FRACTION = 0.33** — B-1 significantly outperformed B-2 ($4,798 vs $3,229). Taking a smaller partial earlier preserves more running P&L.

Both defaults set in `strategy_smt.py` as of 2026-04-20.

### New Effective Baseline for Plan 3

trades=45, pnl=$4,798, wr=62.2%, avg_rr=6.56, max_dd=$95
(HIDDEN_SMT_ENABLED=True, PARTIAL_EXIT_ENABLED=True, PARTIAL_EXIT_FRACTION=0.33)

### Re-test Conditions

- **Run A (SMT_OPTIONAL displacement)**: Core structural flaw — displacement entries steal same-session SMT slots. Plan 3 adds `DISPLACEMENT_STOP_MODE` (correct stop placement) and `MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT` (quality gate). Re-test as part of Round 3 experiments after Plan 3 execution.
- **Run C (TWO_LAYER+FVG)**: `layer_b_triggers=0` across all configs in this 60-day momentum window. Not a parameter problem — FVG retracements are a regime-dependent pattern. Plan 3 adds `fvg_detected` TSV field to diagnose whether zones are forming but not retracing, or not forming at all. Re-test in a different regime window or after `FVG_LAYER_B_REQUIRES_HYPOTHESIS` gates it to hypothesis-confirmed sessions.
- **Run D (SMT_FILL_ENABLED)**: `fill_entries=0`, final_pnl=$5,075 (baseline, NEUTRAL). No fill divergences formed in this 60-day momentum regime. Re-test in Round 3 when `fvg_detected` field provides broader regime diagnostics.

## Round 3 — Plan 3 Hypothesis Alignment (pending)

_To be filled after Plan 3 experiments._
