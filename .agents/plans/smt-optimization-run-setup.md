# Feature: SMT Optimization Run Setup

**⚠️ EXECUTION RULES — READ FIRST:**
- Implement ALL changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
- Only make code changes — no git operations

---

## Feature Description

Prepare `train_smt.py` and `program_smt.md` for the next SMT parameter optimization run:

1. **`train_smt.py`**: Add `mean_test_pnl` to the summary output block (new primary metric), and annotate `LONG_STOP_RATIO` / `SHORT_STOP_RATIO` constants with iteration-1 search instructions so the optimization agent can find them inline.
2. **`program_smt.md`**: Rewrite the evaluation criteria, output format, and optimization protocol to (a) use `mean_test_pnl` as the primary target, (b) embed proposals 2–6 from PROGRESS.md as a structured iteration agenda, and (c) add a post-iteration reflection protocol instructing the agent to analyze results vs expectations and use extended reasoning to decide whether remaining proposals are still valid before proceeding.

## User Story

As the SMT strategy optimizer,
I want `train_smt.py` to emit `mean_test_pnl` and `program_smt.md` to contain the full iteration agenda with adaptive reflection hooks,
So that each optimization iteration maximizes total P&L (mean across folds) and the agent can intelligently redirect the remaining agenda based on actual results.

## Problem Statement

- `min_test_pnl` (worst-fold robustness) is no longer the primary target; `mean_test_pnl` (total P&L health) takes precedence.
- The optimization agenda for proposals 1–6 exists only in `PROGRESS.md`, not in the files the optimization agent reads.
- The agent currently has no protocol for reflecting on results vs expectations or revisiting the agenda mid-run.

## Solution Statement

- Add `mean_test_pnl` computation + print to `train_smt.py` (frozen section is untouched; output block is in `__main__`).
- Annotate `LONG_STOP_RATIO` / `SHORT_STOP_RATIO` constants in `train_smt.py` with the iteration-1 search space so the agent sees it inline.
- Rewrite `program_smt.md` evaluation criteria, output format, and add an "Optimization Agenda" section with proposals 2–6 and a post-iteration analysis protocol.
- Update `PROGRESS.md` "SMT Parameter Optimization Run" section to reflect new primary target and link plan.

## Feature Metadata

**Feature Type**: Enhancement
**Complexity**: Low
**Primary Systems Affected**: `train_smt.py`, `program_smt.md`, `PROGRESS.md`
**Dependencies**: None
**Breaking Changes**: No — adds `mean_test_pnl` line to output; does not remove `min_test_pnl`

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ BEFORE IMPLEMENTING

- `train_smt.py` (lines 88–98) — LONG_STOP_RATIO / SHORT_STOP_RATIO constants to annotate
- `train_smt.py` (lines 699–766) — `__main__` block where `min_test_pnl` is computed and printed; add `mean_test_pnl` here
- `program_smt.md` (lines 1–128) — Full file to update
- `PROGRESS.md` (lines 844–939) — "SMT Parameter Optimization Run" section; update primary target and link plan

### New Files to Create

None.

### Patterns to Follow

- Output format in `train_smt.py` uses `f"{metric}: {value:.2f}"` — match exactly.
- `program_smt.md` uses markdown tables for evaluation criteria.
- `PROGRESS.md` status fields follow `**Status**: ✅ Planned` pattern.

---

## PARALLEL EXECUTION STRATEGY

### Dependency Graph

```
┌────────────────────────────────────────────────────────────────┐
│ WAVE 1: Parallel                                               │
├────────────────────────────────────────────────────────────────┤
│ Task 1.1: UPDATE train_smt.py  │ Task 1.2: UPDATE program_smt │
└────────────────────────────────────────────────────────────────┘
                          ↓
┌────────────────────────────────────────────────────────────────┐
│ WAVE 2: Sequential                                             │
├────────────────────────────────────────────────────────────────┤
│ Task 2.1: UPDATE PROGRESS.md — Deps: 1.1, 1.2                 │
└────────────────────────────────────────────────────────────────┘
                          ↓
┌────────────────────────────────────────────────────────────────┐
│ WAVE 3: Validation                                             │
├────────────────────────────────────────────────────────────────┤
│ Task 3.1: Run tests + smoke-run train_smt.py                   │
└────────────────────────────────────────────────────────────────┘
```

### Parallelization Summary

**Wave 1 — Fully Parallel**: Tasks 1.1 and 1.2 touch different files.
**Wave 2 — Sequential**: Task 2.1 needs both Wave 1 tasks done.
**Wave 3 — Sequential**: Validation after all changes are in.

---

## IMPLEMENTATION PLAN

### Phase 1: Code Changes (Wave 1 — parallel)

#### Task 1.1: UPDATE `train_smt.py` — add `mean_test_pnl` output + annotate stop-ratio constants

**Purpose**: Add `mean_test_pnl` to the harness summary output and embed iteration-1 search instructions in the constants block.

**Changes**:

**1a — Annotate stop ratio constants (lines 91–94)**

Current block (lines 91–94):
```python
# Per-direction stop placement ratios (fraction of |entry - TDO| distance).
# Both default to 0.45, matching the original hardcoded value.
LONG_STOP_RATIO  = 0.45
SHORT_STOP_RATIO = 0.45
```

Replace with:
```python
# Per-direction stop placement ratios (fraction of |entry - TDO| distance).
# Both default to 0.45, matching the original hardcoded value.
#
# ITERATION 1 — Asymmetric stop ratios (HIGHEST PRIORITY)
# Tune LONG_STOP_RATIO and SHORT_STOP_RATIO independently.
# Search space: each ∈ [0.30, 0.55] (step 0.05 suggested for initial grid).
# Optimise for: mean_test_pnl (primary), min_test_pnl > 0 (secondary guard).
# Constraint: total test trades ≥ 80 across all folds.
LONG_STOP_RATIO  = 0.45
SHORT_STOP_RATIO = 0.45
```

**1b — Add `mean_test_pnl` to the summary block (lines 746–757)**

After the `if _qualified:` block that computes `min_test_pnl`, add `mean_test_pnl` computation in both branches and print it.

Current block (lines 746–757):
```python
    # R2: Exclude folds with < 3 test trades — sparse folds are noise-dominated
    _qualified = [(p, t) for p, t in fold_test_pnls if t >= 3]
    if _qualified:
        min_test_pnl = min(p for p, t in _qualified)
        _n_included  = len(_qualified)
    else:
        min_test_pnl = min(p for p, t in fold_test_pnls) if fold_test_pnls else 0.0
        _n_included  = len(fold_test_pnls)

    print("---")
    print(f"min_test_pnl:                {min_test_pnl:.2f}")
    print(f"min_test_pnl_folds_included: {_n_included}")
```

Replace with:
```python
    # R2: Exclude folds with < 3 test trades — sparse folds are noise-dominated
    _qualified = [(p, t) for p, t in fold_test_pnls if t >= 3]
    if _qualified:
        min_test_pnl  = min(p for p, t in _qualified)
        mean_test_pnl = sum(p for p, t in _qualified) / len(_qualified)
        _n_included   = len(_qualified)
    else:
        _source       = fold_test_pnls if fold_test_pnls else [(0.0, 0)]
        min_test_pnl  = min(p for p, t in _source)
        mean_test_pnl = sum(p for p, t in _source) / len(_source)
        _n_included   = len(_source)

    print("---")
    print(f"mean_test_pnl:               {mean_test_pnl:.2f}")
    print(f"min_test_pnl:                {min_test_pnl:.2f}")
    print(f"min_test_pnl_folds_included: {_n_included}")
```

**Validation**:
```bash
uv run python train_smt.py 2>&1 | grep -E "mean_test_pnl|min_test_pnl"
```
Expected: lines for both `mean_test_pnl` and `min_test_pnl` appear in output.

---

#### Task 1.2: UPDATE `program_smt.md` — new primary target + optimization agenda

**Purpose**: Make `mean_test_pnl` the primary optimization target, embed proposals 2–6 as a structured agenda, and add the post-iteration analysis + adaptive reflection protocol.

**Full replacement content for `program_smt.md`**:

```markdown
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
```

---

### Phase 2: PROGRESS.md Update (Wave 2)

#### Task 2.1: UPDATE `PROGRESS.md` — reflect new primary target + link plan

**Purpose**: Keep PROGRESS.md in sync with the new primary target and link the plan file.

**Change**: In the "SMT Parameter Optimization Run" section (around line 844), update the status fields and add the plan file link, and update the "Optimisation objective" block to reflect `mean_test_pnl` as primary.

Locate the block:
```markdown
**Status**: 🔲 Planned
**Date scoped**: 2026-04-02
```

Replace with:
```markdown
**Status**: ✅ Planned
**Date scoped**: 2026-04-02
**Plan File**: .agents/plans/smt-optimization-run-setup.md
```

Locate the "Optimisation objective" block (around line 926):
```markdown
Primary: **maximise `min_test_pnl`** (worst fold) — ensures robustness across regimes.
Secondary: **Sharpe ≥ 2.0 in every fold**, total test trades ≥ 80.
```

Replace with:
```markdown
Primary: **maximise `mean_test_pnl`** (average fold P&L) — maximises total return across regimes.
Secondary: **`min_test_pnl` > 0** (all qualified folds profitable), **Sharpe ≥ 2.0 in every fold**, total test trades ≥ 80.
```

Also update the "Agent instructions for next run" block to reference `mean_test_pnl`:

Locate:
```markdown
3. Run `uv run python train_smt.py` after each change and compare `min_test_pnl`.
4. Keep changes that improve `min_test_pnl` without dropping total test trades below 80.
```

Replace with:
```markdown
3. Run `uv run python train_smt.py` after each change and compare `mean_test_pnl` (primary) and `min_test_pnl` (guard).
4. Keep changes that improve `mean_test_pnl` without dropping total test trades below 80 or `min_test_pnl` below 0.
```

**Validation**: `grep -n "mean_test_pnl\|min_test_pnl" PROGRESS.md` — both should appear.

---

### Phase 3: Validation (Wave 3)

#### Task 3.1: Run tests and smoke-check output

**Steps**:
1. Run the test suite to confirm `mean_test_pnl` computation doesn't break any existing assertions.
2. Smoke-run `train_smt.py` and verify both metrics appear in output.

```bash
uv run python -m pytest tests/test_smt_strategy.py tests/test_smt_backtest.py -x -q
uv run python train_smt.py 2>&1 | grep -E "^(mean_test_pnl|min_test_pnl)"
```

Expected test output: all tests pass.
Expected grep output: exactly two lines — one for `mean_test_pnl` and one for `min_test_pnl`.

---

## STEP-BY-STEP TASKS

### WAVE 1: Parallel

#### Task 1.1: UPDATE `train_smt.py`

- **WAVE**: 1
- **AGENT_ROLE**: strategy-engineer
- **DEPENDS_ON**: []
- **BLOCKS**: [3.1]
- **PROVIDES**: `mean_test_pnl` in output, iteration-1 annotations on stop-ratio constants
- **IMPLEMENT**: See Task 1.1 in Implementation Plan above (changes 1a and 1b)
- **VALIDATE**: `uv run python train_smt.py 2>&1 | grep -E "mean_test_pnl|min_test_pnl"`

#### Task 1.2: UPDATE `program_smt.md`

- **WAVE**: 1
- **AGENT_ROLE**: strategy-engineer
- **DEPENDS_ON**: []
- **BLOCKS**: [3.1]
- **PROVIDES**: Updated optimizer instructions with new primary target, agenda, and reflection protocol
- **IMPLEMENT**: Full replacement of `program_smt.md` with content specified in Task 1.2 above
- **VALIDATE**: `grep -n "mean_test_pnl\|Agenda Reassessment\|Post-Iteration" program_smt.md`

**Wave 1 Checkpoint**: Both files updated; no syntax errors.

---

### WAVE 2: After Wave 1

#### Task 2.1: UPDATE `PROGRESS.md`

- **WAVE**: 2
- **AGENT_ROLE**: strategy-engineer
- **DEPENDS_ON**: [1.1, 1.2]
- **BLOCKS**: [3.1]
- **PROVIDES**: PROGRESS.md reflects new primary target and plan link
- **IMPLEMENT**: See Task 2.1 in Implementation Plan above
- **VALIDATE**: `grep -n "mean_test_pnl\|Plan File" PROGRESS.md`

**Wave 2 Checkpoint**: PROGRESS.md updated.

---

### WAVE 3: Validation

#### Task 3.1: Run tests and smoke-check

- **WAVE**: 3
- **AGENT_ROLE**: validator
- **DEPENDS_ON**: [1.1, 1.2, 2.1]
- **IMPLEMENT**: Run test suite and smoke-run train_smt.py (see Task 3.1 above)
- **VALIDATE**: All tests pass; `mean_test_pnl` and `min_test_pnl` both appear in output

**Final Checkpoint**: All passing.

---

## TESTING STRATEGY

### Unit Tests

**Status**: ✅ Automated | **Tool**: pytest | **Location**: `tests/test_smt_strategy.py`, `tests/test_smt_backtest.py` | **Run**: `uv run python -m pytest tests/test_smt_strategy.py tests/test_smt_backtest.py -x -q`

No new test cases are required — `mean_test_pnl` is a derived scalar computed only in `__main__`. The existing test suite exercises the backtest logic; adding mean to the print block doesn't affect testable units.

### Smoke Test (Manual)

**Why Manual**: `mean_test_pnl` output is a printed side-effect of `__main__`; grep on actual output is the fastest verification.
**Steps**:
1. `uv run python train_smt.py 2>&1 | grep -E "^(mean_test_pnl|min_test_pnl)"`
2. Confirm two lines appear with numeric values
**Expected**: `mean_test_pnl: 830.XX` and `min_test_pnl: 640.XX` (approximately baseline values)

### Edge Cases

- **Zero qualified folds**: `mean_test_pnl` falls back to average of all `fold_test_pnls` — ✅ covered by else-branch in 1b
- **Single fold**: mean == min == that fold's pnl — ✅ arithmetic correct

### Test Automation Summary

| | Count | % |
|---|---|---|
| ✅ Backend (pytest) | 2 test files (existing) | 90% |
| ⚠️ Manual smoke | 1 | 10% |
| **Total** | 3 | 100% |

Manual smoke: testing `__main__` print output via subprocess grep is the natural verification for this kind of change; a pytest capture of stdout would be over-engineering for a 1-line metric addition.

---

## VALIDATION COMMANDS

### Level 1: Syntax Check

```bash
uv run python -c "import train_smt; print('import ok')"
```

### Level 2: Unit Tests

```bash
uv run python -m pytest tests/test_smt_strategy.py tests/test_smt_backtest.py -x -q
```

### Level 3: Smoke Run

```bash
uv run python train_smt.py 2>&1 | grep -E "^(mean_test_pnl|min_test_pnl|min_test_pnl_folds)"
```

Expected output (values approximate):
```
mean_test_pnl:               830.XX
min_test_pnl:                640.XX
min_test_pnl_folds_included: 6
```

---

## ACCEPTANCE CRITERIA

- [ ] `train_smt.py` imports cleanly (`uv run python -c "import train_smt"` exits 0)
- [ ] `uv run python train_smt.py` output contains a `mean_test_pnl:` line with a numeric value
- [ ] `mean_test_pnl` appears before `min_test_pnl` in the summary block
- [ ] `LONG_STOP_RATIO` / `SHORT_STOP_RATIO` constants in `train_smt.py` are annotated with iteration-1 search space [0.30, 0.55]
- [ ] `program_smt.md` header states `mean_test_pnl` as the optimization target
- [ ] `program_smt.md` evaluation criteria table lists `mean_test_pnl` as PRIMARY
- [ ] `program_smt.md` contains "Optimization Agenda" section with iterations 2–6
- [ ] `program_smt.md` contains "Post-Iteration Analysis Protocol" section with "Agenda Reassessment" step
- [ ] `PROGRESS.md` "SMT Parameter Optimization Run" section has status ✅ Planned and Plan File link
- [ ] `PROGRESS.md` optimization objective reads `mean_test_pnl` as primary
- [ ] All existing pytest tests pass

---

## COMPLETION CHECKLIST

- [ ] Task 1.1 complete: `train_smt.py` updated
- [ ] Task 1.2 complete: `program_smt.md` updated
- [ ] Task 2.1 complete: `PROGRESS.md` updated
- [ ] Task 3.1 complete: tests pass, smoke-run confirms output
- [ ] All acceptance criteria met
- [ ] **⚠️ Debug logs added during execution REMOVED (keep pre-existing)**
- [ ] **⚠️ CRITICAL: Changes UNSTAGED — NOT committed**

---

## NOTES

**Why `mean_test_pnl` over `min_test_pnl`**: The worst-fold metric optimizes for robustness at the cost of overall return. The user's stated goal is to improve total P&L, not just protect against the worst case. `min_test_pnl > 0` is retained as a guard to prevent strategies that are profitable on average but blow out one regime.

**Why iteration 1 is in `train_smt.py` not `program_smt.md`**: The constants `LONG_STOP_RATIO` and `SHORT_STOP_RATIO` are already in the editable block. Adding the search space as inline comments means any agent reading the file before the optimizer even starts can see what the first job is. It avoids the agent needing to cross-reference two files for the first change.

**Adaptive reflection rationale**: Proposals 2–6 were written before any optimization runs. It's expected that some will be invalidated by what iteration 1 and 2 reveal about the data. The "Agenda Reassessment" step forces explicit reasoning rather than blind sequential execution.
