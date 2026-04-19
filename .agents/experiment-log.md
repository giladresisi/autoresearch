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

## Round 3 — Plan 3 Hypothesis Alignment

**Status:** Infrastructure complete (2026-04-20). Experiments pending.

**Runner:** `uv run python plan3_experiment_runner.py [FLAG=VALUE ...]`
_(Create from `plan2_experiment_runner.py` — add Plan 3 bool flags: `HYPOTHESIS_FILTER`, `DISPLACEMENT_STOP_MODE`, `FVG_LAYER_B_REQUIRES_HYPOTHESIS`; int flag: `MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT`; float flags: `PARTIAL_EXIT_LEVEL_RATIO`. Mirror `HYPOTHESIS_FILTER` into `backtest_smt` not `strategy_smt`.)_

**Test window:** Same 1-fold fast mode (60 business days ending at `TRAIN_END`).

**Effective baseline entering Round 3:**
trades=45, pnl=$4,798, wr=62.2%, avg_rr=6.56, max_dd=$95
(HIDDEN_SMT_ENABLED=True, PARTIAL_EXIT_ENABLED=True, PARTIAL_EXIT_FRACTION=0.33)

---

### Pre-Run Analysis (required before first experiment)

Run a fresh backtest to generate trades.tsv with the new Rule 5 hypothesis columns, then run the alignment analysis to inform which experiments are worth prioritising:

```bash
uv run python backtest_smt.py
uv run python analyze_hypothesis.py
```

Read the `VERDICT` line:
- **POTENTIAL EDGE** (aligned WR − misaligned WR ≥ 10pp, ≥4/6 folds consistent) → Run A is high-priority; the hypothesis filter has real signal.
- **NO CLEAR EDGE** → Run A is low-priority; displacement fixes (Run B) and partial level tuning (Run C) are higher-value.

Also check `fvg_detected` distribution in the new trades.tsv:
```bash
python -c "import pandas as pd; df=pd.read_csv('trades.tsv',sep='\t'); print(df['fvg_detected'].value_counts())"
```
If `fvg_detected=True` is common but `layer_b_triggers=0` (from Round 2), it means FVG zones are forming but price never retraces — regime-dependent. If `fvg_detected=False` dominates, the FVG detection threshold may need tuning before Run D is meaningful.

---

### How to Run Each Group

#### Run A — Hypothesis filter (`HYPOTHESIS_FILTER`)

Takes only signals where the session hypothesis agrees with signal direction. Tests whether the hypothesis system produces tradeable alpha or is decorative.

**Only run if `analyze_hypothesis.py` shows POTENTIAL EDGE.** If NO CLEAR EDGE, record A-1 as NEUTRAL and skip to Run B.

```bash
uv run python plan3_experiment_runner.py HYPOTHESIS_FILTER=True
```

Key metrics: `final_trades` (expect significant drop if many signals are misaligned), `final_pnl`, `win_rate`, `avg_rr`.

**Verdict logic:** APPROVE only if `final_pnl` > Round 3 baseline ($4,798) AND `final_trades` ≥ 20 (enough to be statistically meaningful). If trades drop below 20, record as NEUTRAL — insufficient sample. If P&L improves but WR/RR degrade, it may be a volatility artifact — record as NEUTRAL with a note.

---

#### Run B — Displacement re-test with Plan 3 fixes (`SMT_OPTIONAL` + `DISPLACEMENT_STOP_MODE`)

Round 2 rejected all `SMT_OPTIONAL` configs due to two structural flaws: (1) wrong stop placement (structural stop instead of displacement bar extreme), (2) no quality gate. Plan 3 fixes both. Re-test with fixes enabled.

**Always set `DISPLACEMENT_STOP_MODE=True` when `SMT_OPTIONAL=True`** — testing without it repeats a rejected Round 2 config.

```bash
# B-1: displacement entries with correct stop only
uv run python plan3_experiment_runner.py SMT_OPTIONAL=True MIN_DISPLACEMENT_PTS=10.0 DISPLACEMENT_STOP_MODE=True

# B-2: add hypothesis score gate (score >= 2 required)
uv run python plan3_experiment_runner.py SMT_OPTIONAL=True MIN_DISPLACEMENT_PTS=10.0 DISPLACEMENT_STOP_MODE=True MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT=2

# B-3: stricter score gate (score >= 3 required)
uv run python plan3_experiment_runner.py SMT_OPTIONAL=True MIN_DISPLACEMENT_PTS=10.0 DISPLACEMENT_STOP_MODE=True MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT=3
```

Key metrics: `displacement_entries` (must be > 0 to be meaningful), `final_pnl`, `win_rate`, `avg_rr`, `max_dd`.

**Verdict logic:** Compare `displacement_entries` across B-1/B-2/B-3 to understand how many entries the score gate eliminates. APPROVE if `final_pnl` > baseline and WR/RR don't degrade. NEUTRAL if `displacement_entries=0` (threshold too tight for current window). REJECT if P&L is below Round 2 A-2 result ($3,520) — the fixes made it worse, not better.

If B-1 still rejects, the slot-stealing structural flaw is not fixed by stop placement alone — document and defer to Round 4.

---

#### Run C — Partial exit level tuning (`PARTIAL_EXIT_LEVEL_RATIO`)

Round 2 approved `PARTIAL_EXIT_FRACTION=0.33` at midpoint (ratio=0.5). Tests whether adjusting the partial level earlier (0.33) or later (0.67) improves the risk-adjusted result further.

```bash
# C-1: earlier partial exit (33% of the way to TP — tighter lock-in)
uv run python plan3_experiment_runner.py PARTIAL_EXIT_LEVEL_RATIO=0.33

# C-2: later partial exit (67% of the way to TP — more favorable RR on partial leg)
uv run python plan3_experiment_runner.py PARTIAL_EXIT_LEVEL_RATIO=0.67
```

**Note:** `PARTIAL_EXIT_ENABLED=True` and `PARTIAL_EXIT_FRACTION=0.33` are already the defaults — the runner runs with them on. Only `PARTIAL_EXIT_LEVEL_RATIO` changes.

Key metrics: `partial_trades` (should equal `final_trades` — if lower, partial level was never reached), `final_pnl`, `max_dd`.

**Verdict logic:** Same as Run B in Round 2 — use `final_pnl` vs baseline and `max_drawdown` as primary signal. The baseline ratio=0.5 gives $4,798 / $95dd. APPROVE the new ratio if P&L ≥ baseline AND max_dd ≤ $95. If `partial_trades` < `final_trades` at ratio=0.33, early level is too tight (midpoint wasn't reached) — NEUTRAL.

---

#### Run D — Layer B hypothesis gate (`FVG_LAYER_B_REQUIRES_HYPOTHESIS`)

Gates the FVG retracement add-on (Layer B) to hypothesis-confirmed sessions. Only meaningful once `fvg_detected=True` is common in trades.tsv and/or after a regime shift where Layer B actually fires.

**Skip or defer Run D if:**
- `fvg_detected=True` is rare (< 20% of trades) — FVG zones aren't forming, gating them is moot.
- `layer_b_triggers=0` — Layer B still doesn't fire (regression on Round 2 finding); document and defer to Round 4.

```bash
# D-1: gate Layer B to hypothesis-confirmed sessions (score >= 1, MIN_HYPOTHESIS_SCORE default=0)
uv run python plan3_experiment_runner.py TWO_LAYER_POSITION=True FVG_ENABLED=True FVG_LAYER_B_TRIGGER=True FVG_LAYER_B_REQUIRES_HYPOTHESIS=True MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT=2
```

Key metrics: `layer_b_triggers` (if still 0 → NEUTRAL, regime unchanged), `fvg_detected` count from trades.tsv (from pre-run analysis step).

---

### Verdict Framework

Same as Rounds 1–2 with one addition for the hypothesis filter:

| Result | Criterion | Action |
|--------|-----------|--------|
| APPROVE | `final_pnl` > Round 3 baseline ($4,798) AND WR/RR stable; OR `max_dd` meaningfully lower at ≤5% P&L cost | Set flag as new default in `strategy_smt.py` (or `backtest_smt.py` for `HYPOTHESIS_FILTER`) |
| NEUTRAL | Within ±5% of baseline, no meaningful improvement; OR insufficient sample (`final_trades` < 20) | Leave False/default; document re-test conditions |
| REJECT | `final_pnl` < baseline − 10% OR WR/RR collapses vs prior approved baseline | Leave False; document root cause |

For hypothesis filter (Run A): APPROVE only if both P&L improves AND sample ≥ 20 final trades.
For displacement (Run B): compare against the Round 2 **rejected** baseline ($3,520), not just $4,798 — even if below $4,798, it may still be an improvement over the unfixed displacement config.
For partial level (Run C): `max_drawdown` is the primary signal, P&L is secondary.

---

### Results

_Fill in after running experiments. Run pre-run analysis first and record verdict in the Pre-Run Analysis row._

| Run | Config | final_pnl | final_trades | win_rate | avg_rr | max_dd | displacement | layer_b | Verdict |
|-----|--------|-----------|--------------|----------|--------|--------|--------------|---------|---------|
| Baseline | (Round 3 defaults) | $4,798 | 45 | 62.2% | 6.56 | $95 | 0 | 0 | — |
| Pre-run | analyze_hypothesis.py | — | — | — | — | — | — | — | EDGE / NO EDGE |
| A-1 | HYPOTHESIS_FILTER=True | | | | | | | | |
| B-1 | SMT_OPTIONAL=True, DISP_STOP=True, MIN_DISP=10 | | | | | | | | |
| B-2 | B-1 + MIN_SCORE=2 | | | | | | | | |
| B-3 | B-1 + MIN_SCORE=3 | | | | | | | | |
| C-1 | PARTIAL_RATIO=0.33 | | | | | | | | |
| C-2 | PARTIAL_RATIO=0.67 | | | | | | | | |
| D-1 | TWO_LAYER+FVG+LB+REQUIRES_HYP, MIN_SCORE=2 | | | | | | | | |

### Re-test Conditions

_To be filled after Round 3 experiments._
