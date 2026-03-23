# Feature: V3-G Harness Integrity and Objective Quality

**⚠️ EXECUTION RULES — READ FIRST:**
- Implement ALL changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
- Only make code changes — no git operations

Validate documentation and codebase patterns before implementing. Match naming of existing utils, types, and models. Import from correct files.

---

## Feature Description

V3-G addresses four remaining harness integrity weaknesses discovered via post-run analysis. The changes prevent the agent from gaming the `min_test_pnl` objective through position-sizing inflation, enforce a monthly P&L consistency floor in the keep/discard criterion, add an early-stop signal for zero-trade plateaus, and make ticker holdout the recommended default. This is a mutable-zone and `program.md`-only change — no immutable-zone edits, no `GOLDEN_HASH` update required.

## User Story

As a session operator running the auto-co-trader optimization loop,
I want structural guards that prevent the agent from gaming the objective and clear instructions for zero-trade plateaus and consistency floors,
So that the experiment loop produces genuinely robust strategies rather than artificially inflated P&L metrics.

## Problem Statement

Four weaknesses remain after V3-A through V3-F:

1. **`RISK_PER_TRADE` is agent-tunable** — it lives in the undifferentiated mutable section alongside strategy-tuning constants. An agent that raises it from 50 to 500 produces 10× dollar P&L with no genuine strategy improvement.
2. **No plateau signal** — when screener thresholds are tightened to the point that zero trades fire, the agent may iterate indefinitely without a useful feedback signal.
3. **`pnl_consistency` not enforced** — it is printed but not used in keep/discard; a strategy can pass on one good month while losing badly in others.
4. **Holdout disabled by default** — `TICKER_HOLDOUT_FRAC = 0.0` means the generalization guard from V3-D is never activated unless the operator explicitly enables it.

## Solution Statement

- Add `# ══ SESSION SETUP ══` and `# ══ STRATEGY TUNING ══` comment headers to `train.py`'s mutable section, with a "DO NOT raise to inflate P&L" inline note on `RISK_PER_TRADE`.
- Update `program.md` with: (1) STRATEGY TUNING sub-section scope instruction, (2) zero-trade plateau early-stop rule, (3) `train_pnl_consistency` floor as a secondary keep condition with `discard-inconsistent` status, (4) `TICKER_HOLDOUT_FRAC = 0.1` as the recommended default.

## Feature Metadata

**Feature Type**: Enhancement
**Complexity**: Low
**Primary Systems Affected**: `train.py` (mutable zone only), `program.md`
**Dependencies**: None (no new libraries)
**Breaking Changes**: No — all changes are additive comments or documentation instructions. Existing worktrees and cached data are unaffected. No GOLDEN_HASH update required.

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ BEFORE IMPLEMENTING

- `train.py` (lines 1–75) — The mutable section. Read the full constants block to understand the exact placement for the two new headers and the RISK_PER_TRADE inline comment.
- `program.md` — The agent instructions file. Read steps 4–8 (session setup) and the "Goal", "Experimentation rules", "Logging results", and step 8 keep/discard sections to locate exact insertion points.
- `tests/test_v3_f.py` — Pattern for V3-F unit tests; mirror this structure for V3-G tests.
- `tests/test_optimization.py` — Existing test file; new V3-G tests go here (file already exists, append new tests).

### New Files to Create

None. All test additions go into `tests/test_optimization.py`.

### Relevant Documentation — READ BEFORE IMPLEMENTING

- `prd.md` lines 1206–1324 — Full V3-G specification. Read before touching any file.

### Patterns to Follow

**Comment header style** (match exactly, including box-drawing characters):
```python
# ══ SESSION SETUP — set once at session start; DO NOT change during experiments ══════
# ══ STRATEGY TUNING — agent may modify these freely during experiments ════════════════
```

**Inline comment on RISK_PER_TRADE** (append to existing comment line or add new line):
```python
RISK_PER_TRADE = 50.0       # dollar risk per trade; DO NOT raise to inflate P&L
```

**Test file imports** (mirror test_v3_f.py):
```python
import train
import program  # N/A — program.md is not importable; use file reads for assertions
```

---

## PARALLEL EXECUTION STRATEGY

### Dependency Graph

```
┌──────────────────────────────────────────────────────────────────┐
│ WAVE 1: Code Changes (Parallel)                                   │
├──────────────────────────────────────────────────────────────────┤
│ Task 1.1: UPDATE train.py      │ Task 1.2: UPDATE program.md      │
│ Agent: code-writer              │ Agent: docs-writer               │
└──────────────────────────────────────────────────────────────────┘
                    ↓
┌──────────────────────────────────────────────────────────────────┐
│ WAVE 2: Tests (After Wave 1)                                      │
├──────────────────────────────────────────────────────────────────┤
│ Task 2.1: ADD tests to test_optimization.py — Deps: 1.1, 1.2     │
└──────────────────────────────────────────────────────────────────┘
                    ↓
┌──────────────────────────────────────────────────────────────────┐
│ WAVE 3: Validation (Sequential)                                   │
├──────────────────────────────────────────────────────────────────┤
│ Task 3.1: Run full test suite                                     │
└──────────────────────────────────────────────────────────────────┘
```

### Parallelization Summary

**Wave 1 — Fully Parallel**: Tasks 1.1 and 1.2 — no dependencies between them.
**Wave 2 — After Wave 1**: Task 2.1 — needs both files to be updated before tests can be written.
**Wave 3 — Sequential**: Task 3.1 — needs all tests written.

### Interface Contracts

**Contract 1**: Task 1.1 produces `train.py` with SESSION SETUP / STRATEGY TUNING headers → Task 2.1 asserts headers present.
**Contract 2**: Task 1.2 produces `program.md` with plateau/discard-inconsistent/TICKER_HOLDOUT_FRAC text → Task 2.1 asserts these strings present.

### Synchronization Checkpoints

**After Wave 1**: `python -c "import train"` (syntax check)
**After Wave 2**: `uv run pytest tests/test_optimization.py -x -q`

---

## IMPLEMENTATION PLAN

### Phase 1: Code Changes

No external services. Both changes are pure text edits to local files.

#### Task 1.1: UPDATE train.py — Add Mutable Section Sub-Headers

**Purpose**: Structurally separate session-setup constants from strategy-tuning constants, preventing the agent from treating `RISK_PER_TRADE` as a valid optimization target.

**Exact changes** (read `train.py` lines 1–75 first to locate the exact positions):

1. Insert this header BEFORE the `BACKTEST_START` line (currently around line 18):

```python
# ══ SESSION SETUP — set once at session start; DO NOT change during experiments ══════
# These constants define the evaluation framework. Changing them mid-session
# invalidates comparisons across experiments.
```

2. Modify the `RISK_PER_TRADE` line comment. Current:
```python
# Risk-proportional sizing: dollar risk per trade (V3-A R3)
RISK_PER_TRADE = 50.0
```
New (replace the comment line):
```python
# Risk-proportional sizing: dollar risk per trade (V3-A R3). DO NOT raise to inflate P&L.
RISK_PER_TRADE = 50.0
```

3. Insert this header BEFORE the `MAX_SIMULTANEOUS_POSITIONS` line (currently around line 56):

```python
# ══ STRATEGY TUNING — agent may modify these freely during experiments ════════════════
# Only the constants below this line are valid optimization targets.
```

**Order of constants after edit:**

SESSION SETUP block:
- `CACHE_DIR` (already has env-var comment — leave it before the SESSION SETUP header since it's technically session-setup infrastructure; the header should go after `CACHE_DIR` and before `BACKTEST_START`)

Wait — `CACHE_DIR` is set via env var and is not a user-editable constant in the same sense. The PRD shows BACKTEST_START as the first SESSION SETUP constant. Place the SESSION SETUP header between `CACHE_DIR` block (lines 11–16) and `BACKTEST_START` (line 18–19). Specifically after the blank line following `CACHE_DIR`'s closing `)`.

SESSION SETUP constants in order: `BACKTEST_START`, `BACKTEST_END`, `TRAIN_END`, `TEST_START`, `WRITE_FINAL_OUTPUTS`, `WALK_FORWARD_WINDOWS`, `FOLD_TEST_DAYS`, `FOLD_TRAIN_DAYS`, `SILENT_END`, `RISK_PER_TRADE`, `TICKER_HOLDOUT_FRAC`, `TEST_EXTRA_TICKERS`

STRATEGY TUNING constants: `MAX_SIMULTANEOUS_POSITIONS`, `CORRELATION_PENALTY_WEIGHT`, `ROBUSTNESS_SEEDS`

**Validation**: `python -c "import train"` — must complete without error.

#### Task 1.2: UPDATE program.md — Five Targeted Edits

**Purpose**: Reinforce the SESSION SETUP / STRATEGY TUNING distinction, add plateau early-stop, add `pnl_consistency` floor keep condition, and make `TICKER_HOLDOUT_FRAC = 0.1` the recommended default.

Read the full `program.md` before making changes. Perform five edits:

---

**Edit A — SESSION SETUP scope instruction in step 4 (session setup section)**

Locate the sentence that introduces the optimization constants in step 4. Add after the existing step 4b block (after the `TEST_EXTRA_TICKERS` / `AUTORESEARCH_CACHE_DIR` docs):

```
**Mutable section structure**: `train.py`'s mutable section is divided into two sub-sections:
- `# ══ SESSION SETUP ══` — set these constants once at session start; do NOT change them during experiments. Changing them invalidates cross-experiment comparisons.
- `# ══ STRATEGY TUNING ══` — only the constants below this header are valid optimization targets. Do NOT modify SESSION SETUP constants (including `RISK_PER_TRADE`) to inflate reported P&L.
```

---

**Edit B — `TICKER_HOLDOUT_FRAC` recommended default**

Locate the existing step 4b entry for `TICKER_HOLDOUT_FRAC`. It currently reads:
```
When using `TEST_EXTRA_TICKERS`, set `TICKER_HOLDOUT_FRAC = 0` to avoid overlap.
```

Replace the paragraph describing `TICKER_HOLDOUT_FRAC` / add after the existing description:

```
**Recommended default**: Set `TICKER_HOLDOUT_FRAC = 0.1` (hold out the alphabetically last 10% of tickers as a silent training-universe holdout). Only set to `0.0` if the ticker universe is small (< 20 tickers) or if `TEST_EXTRA_TICKERS` is in use.
```

---

**Edit C — Keep/discard criterion: add `train_pnl_consistency` floor**

Locate the keep/discard section (around "If `min_test_pnl` **improved (higher)** compared to the current best → keep the commit"). Add a secondary condition:

Replace:
```
8. If `min_test_pnl` **improved (higher)** compared to the current best → keep the commit, advance the branch.
```

With:
```
8. Keep a change only if **both** conditions hold:
   1. `min_test_pnl` improved (higher than current best)
   2. `train_pnl_consistency` ≥ `−RISK_PER_TRADE × 2` (minimum monthly P&L is not catastrophically negative — e.g. ≥ −$100 when `RISK_PER_TRADE = 50.0`)

   If condition 1 passes but condition 2 fails → log status `discard-inconsistent` and revert (`git reset --hard HEAD~1`).
```

---

**Edit D — Early-stop for zero-trade plateau**

Locate the section describing the experiment loop steps (after step 8, inside the loop body). Add a new rule after the `discard-fragile` instruction block:

```
**Zero-trade plateau rule**: If `train_total_trades: 0` (or `fold{N}_train_total_trades: 0`) appears for 3 or more **consecutive** iterations:
- Stop tightening screener thresholds in the current direction.
- Relax the most recently tightened threshold back to its prior value.
- Try a different modification (different indicator, different constant).

If 10 consecutive iterations all produce zero trades → log status `plateau`, run `git reset --hard HEAD~1` to revert to the last non-zero baseline, and notify the user that the screener has been over-constrained.
```

---

**Edit E — Update `status` column definition in "Logging results"**

Locate the `results.tsv` column definitions (column 10: `status`). Add `discard-inconsistent` to the list:

Current:
```
10. `status`: `keep`, `discard`, `crash`, or `discard-fragile`
    - `discard-fragile`: ...
```

New:
```
10. `status`: `keep`, `discard`, `crash`, `discard-fragile`, or `discard-inconsistent`
    - `discard-fragile`: nominal `min_test_pnl > 0` but at least one fold's `pnl_min < 0` (strategy collapses with small fill deviations); revert just like `discard`.
    - `discard-inconsistent`: `min_test_pnl` improved but `train_pnl_consistency < −RISK_PER_TRADE × 2` (monthly P&L floor violated); revert just like `discard`.
```

---

### Phase 2: Tests

#### Task 2.1: ADD Tests to tests/test_optimization.py

Append 8 new V3-G unit tests to the end of `tests/test_optimization.py`. These tests assert structural properties of `train.py` and `program.md` without requiring any live parquet cache.

**Test list:**

1. `test_v3g_session_setup_header_present` — reads `train.py` source, asserts `# ══ SESSION SETUP` substring is present.
2. `test_v3g_strategy_tuning_header_present` — reads `train.py` source, asserts `# ══ STRATEGY TUNING` substring is present.
3. `test_v3g_session_setup_before_strategy_tuning` — reads `train.py` source, asserts the line index of `# ══ SESSION SETUP` is less than the line index of `# ══ STRATEGY TUNING`.
4. `test_v3g_risk_per_trade_in_session_setup` — reads `train.py` source, asserts `RISK_PER_TRADE` assignment line appears AFTER the SESSION SETUP header and BEFORE the STRATEGY TUNING header.
5. `test_v3g_max_simultaneous_positions_in_strategy_tuning` — reads `train.py` source, asserts `MAX_SIMULTANEOUS_POSITIONS` assignment line appears AFTER the STRATEGY TUNING header.
6. `test_v3g_risk_per_trade_comment_warns_inflation` — reads `train.py` source, asserts the `RISK_PER_TRADE` line or its adjacent comment contains "inflate P&L" (case-insensitive).
7. `test_v3g_program_md_contains_plateau` — reads `program.md` as text, asserts "plateau" is present.
8. `test_v3g_program_md_contains_discard_inconsistent` — reads `program.md` as text, asserts "discard-inconsistent" is present.
9. `test_v3g_program_md_contains_ticker_holdout_frac_01` — reads `program.md` as text, asserts "TICKER_HOLDOUT_FRAC = 0.1" is present.
10. `test_v3g_program_md_contains_session_setup_instruction` — reads `program.md` as text, asserts "SESSION SETUP" is present (validates Edit A was applied).

---

## STEP-BY-STEP TASKS

Tasks organized by execution wave.

---

### WAVE 1: Code Changes (Parallel)

#### Task 1.1: UPDATE train.py mutable section

- **WAVE**: 1
- **AGENT_ROLE**: code-writer
- **DEPENDS_ON**: []
- **BLOCKS**: [2.1]
- **PROVIDES**: `train.py` with SESSION SETUP / STRATEGY TUNING headers and RISK_PER_TRADE inflation-warning comment
- **IMPLEMENT**:
  1. Read `train.py` lines 1–80 to get exact line positions
  2. Insert `# ══ SESSION SETUP — set once at session start; DO NOT change during experiments ══════` + 2-line explanatory comment between the CACHE_DIR block and `BACKTEST_START`
  3. Replace the `RISK_PER_TRADE` comment line to add "DO NOT raise to inflate P&L"
  4. Insert `# ══ STRATEGY TUNING — agent may modify these freely during experiments ════════════════` + 1-line note before `MAX_SIMULTANEOUS_POSITIONS`
- **VALIDATE**: `python -c "import train"` — must exit 0

#### Task 1.2: UPDATE program.md (five edits)

- **WAVE**: 1
- **AGENT_ROLE**: docs-writer
- **DEPENDS_ON**: []
- **BLOCKS**: [2.1]
- **PROVIDES**: `program.md` with SESSION SETUP scope instruction, TICKER_HOLDOUT_FRAC recommended default, `train_pnl_consistency` floor in keep/discard, zero-trade plateau early-stop rule, and `discard-inconsistent` status
- **IMPLEMENT**:
  1. Read full `program.md` to find exact line positions for each edit
  2. Apply Edit A (SESSION SETUP scope instruction in step 4)
  3. Apply Edit B (TICKER_HOLDOUT_FRAC = 0.1 recommended default in step 4b)
  4. Apply Edit C (keep/discard secondary condition replacing step 8 keep line)
  5. Apply Edit D (zero-trade plateau early-stop rule after discard-fragile block)
  6. Apply Edit E (add `discard-inconsistent` to status column definition)
- **VALIDATE**: `grep -c "plateau" program.md` → ≥ 1; `grep -c "discard-inconsistent" program.md` → ≥ 1; `grep -c "TICKER_HOLDOUT_FRAC = 0.1" program.md` → ≥ 1

**Wave 1 Checkpoint**: `python -c "import train"` — must succeed.

---

### WAVE 2: Tests (After Wave 1)

#### Task 2.1: ADD V3-G tests to tests/test_optimization.py

- **WAVE**: 2
- **AGENT_ROLE**: test-writer
- **DEPENDS_ON**: [1.1, 1.2]
- **BLOCKS**: [3.1]
- **PROVIDES**: 10 new V3-G unit tests covering train.py structural assertions and program.md content assertions
- **USES_FROM_WAVE_1**: Task 1.1 provides updated `train.py`; Task 1.2 provides updated `program.md`
- **IMPLEMENT**:
  1. Read `tests/test_optimization.py` to find the end of the file and existing test patterns
  2. Append a `# ── V3-G tests ──` section with all 10 tests listed in Task 2.1 above
  3. For `train.py` source reads: use `pathlib.Path` to open the file relative to the test file location (`Path(__file__).parent.parent / "train.py"`)
  4. For `program.md` reads: similar path resolution (`Path(__file__).parent.parent / "program.md"`)
- **VALIDATE**: `uv run pytest tests/test_optimization.py -k "v3g" -v` — all 10 pass

**Wave 2 Checkpoint**: `uv run pytest tests/test_optimization.py -k "v3g" -v`

---

### WAVE 3: Full Validation (Sequential)

#### Task 3.1: Run full test suite

- **WAVE**: 3
- **AGENT_ROLE**: validator
- **DEPENDS_ON**: [2.1]
- **PROVIDES**: Confirmed no regressions
- **IMPLEMENT**: Run full pytest suite; verify 10 new V3-G tests pass and existing tests are unchanged
- **VALIDATE**: `uv run pytest tests/ -x -q` — all pre-existing passing tests still pass; V3-G tests pass; no new failures

**Final Checkpoint**: `uv run pytest tests/ -x -q`

---

## TESTING STRATEGY

All tests are fully automated (pytest). No manual tests required.

### Unit Tests (train.py structural assertions)

**Status**: ✅ Automated | **Tool**: pytest | **Location**: `tests/test_optimization.py` | **Run**: `uv run pytest tests/test_optimization.py -k "v3g" -v`

1. `test_v3g_session_setup_header_present` ✅ — assert `# ══ SESSION SETUP` in train.py source
2. `test_v3g_strategy_tuning_header_present` ✅ — assert `# ══ STRATEGY TUNING` in train.py source
3. `test_v3g_session_setup_before_strategy_tuning` ✅ — assert SESSION SETUP line index < STRATEGY TUNING line index
4. `test_v3g_risk_per_trade_in_session_setup` ✅ — assert `RISK_PER_TRADE =` appears between the two headers
5. `test_v3g_max_simultaneous_positions_in_strategy_tuning` ✅ — assert `MAX_SIMULTANEOUS_POSITIONS =` appears after STRATEGY TUNING header
6. `test_v3g_risk_per_trade_comment_warns_inflation` ✅ — assert "inflate" near the RISK_PER_TRADE line

### Unit Tests (program.md content assertions)

**Status**: ✅ Automated | **Tool**: pytest | **Location**: `tests/test_optimization.py` | **Run**: `uv run pytest tests/test_optimization.py -k "v3g" -v`

7. `test_v3g_program_md_contains_plateau` ✅ — assert "plateau" in program.md
8. `test_v3g_program_md_contains_discard_inconsistent` ✅ — assert "discard-inconsistent" in program.md
9. `test_v3g_program_md_contains_ticker_holdout_frac_01` ✅ — assert "TICKER_HOLDOUT_FRAC = 0.1" in program.md
10. `test_v3g_program_md_contains_session_setup_instruction` ✅ — assert "SESSION SETUP" in program.md

### Regression Tests

**Status**: ✅ Automated | **Tool**: pytest | **Location**: `tests/` | **Run**: `uv run pytest tests/ -x -q`

- `test_harness_below_do_not_edit_is_unchanged` — GOLDEN_HASH test; must still pass since immutable zone is untouched
- All other existing tests — must pass unchanged

### Edge Cases

- **`RISK_PER_TRADE` value unchanged** ✅: assert `train.RISK_PER_TRADE == 50.0` (implicit in test 4 via import)
- **`MAX_SIMULTANEOUS_POSITIONS` value unchanged** ✅: assert `train.MAX_SIMULTANEOUS_POSITIONS == 5` (implicit in test 5 via import)

### Test Automation Summary

| | Count | % |
|---|---|---|
| ✅ Unit (pytest) | 10 | 100% |
| ✅ Regression (pytest) | all pre-existing | — |
| ⚠️ Manual | 0 | 0% |
| **Total** | 10 new + pre-existing | 100% |

No manual tests required — all assertions are text/import-level checks requiring no live parquet data.

---

## VALIDATION COMMANDS

### Level 1: Syntax Check

```bash
python -c "import train"
```

Expected: exits 0 with no output.

### Level 2: V3-G Unit Tests

```bash
uv run pytest tests/test_optimization.py -k "v3g" -v
```

Expected: 10 tests collected, all pass.

### Level 3: GOLDEN_HASH Regression

```bash
uv run pytest tests/test_optimization.py -k "harness" -v
```

Expected: `test_harness_below_do_not_edit_is_unchanged` passes (immutable zone untouched).

### Level 4: Full Suite

```bash
uv run pytest tests/ -x -q
```

Expected: all pre-existing passing tests still pass; 10 new V3-G tests added and passing; no new failures.

### Level 5: program.md Content Spot-Checks

```bash
grep -c "plateau" program.md
grep -c "discard-inconsistent" program.md
grep "TICKER_HOLDOUT_FRAC = 0.1" program.md
grep "SESSION SETUP" program.md
grep "STRATEGY TUNING" program.md
```

Expected: each returns ≥ 1 match.

### Level 6: train.py Header Spot-Checks

```bash
grep "SESSION SETUP" train.py
grep "STRATEGY TUNING" train.py
grep "inflate P&L" train.py
```

Expected: each returns exactly the relevant line.

---

## ACCEPTANCE CRITERIA

- [ ] `train.py` mutable section has `# ══ SESSION SETUP ══` header before `BACKTEST_START`
- [ ] `train.py` mutable section has `# ══ STRATEGY TUNING ══` header before `MAX_SIMULTANEOUS_POSITIONS`
- [ ] `RISK_PER_TRADE` assignment is in the SESSION SETUP sub-section with "inflate P&L" inline warning
- [ ] `MAX_SIMULTANEOUS_POSITIONS`, `CORRELATION_PENALTY_WEIGHT`, `ROBUSTNESS_SEEDS` are in the STRATEGY TUNING sub-section
- [ ] `python -c "import train"` completes without error
- [ ] No change to train.py immutable zone — `test_harness_below_do_not_edit_is_unchanged` still passes
- [ ] `program.md` contains "SESSION SETUP" scope instruction (only modify STRATEGY TUNING constants)
- [ ] `program.md` contains "TICKER_HOLDOUT_FRAC = 0.1" as recommended default in step 4b
- [ ] `program.md` keep/discard step 8 requires BOTH `min_test_pnl` improvement AND `train_pnl_consistency ≥ −RISK_PER_TRADE × 2`
- [ ] `program.md` defines `discard-inconsistent` status for failing the consistency floor
- [ ] `program.md` defines zero-trade plateau early-stop rule with 3-iteration and 10-iteration thresholds
- [ ] `program.md` status column definition includes `discard-inconsistent`
- [ ] 10 new V3-G automated tests added to `tests/test_optimization.py`, all passing
- [ ] Full test suite: all pre-existing passing tests still pass, no new failures
- [ ] All changes UNSTAGED — no git operations performed

---

## COMPLETION CHECKLIST

- [ ] Task 1.1 complete: `train.py` headers and RISK_PER_TRADE comment updated
- [ ] Task 1.2 complete: all five `program.md` edits applied
- [ ] Wave 1 checkpoint passed: `python -c "import train"` exits 0
- [ ] Task 2.1 complete: 10 V3-G tests appended to `tests/test_optimization.py`
- [ ] Wave 2 checkpoint passed: `uv run pytest tests/test_optimization.py -k "v3g" -v` → 10 pass
- [ ] Task 3.1 complete: full suite `uv run pytest tests/ -x -q` passes
- [ ] Validation commands Level 1–6 all pass
- [ ] All acceptance criteria met
- [ ] **⚠️ Debug logs added during execution REMOVED (keep pre-existing)**
- [ ] **⚠️ CRITICAL: Changes UNSTAGED — NOT committed**

---

## NOTES

- **No GOLDEN_HASH update required**: V3-G touches only the mutable zone of `train.py` (above the `# DO NOT EDIT BELOW THIS LINE` boundary) and `program.md`. The immutable zone (below the boundary) is not touched. `test_harness_below_do_not_edit_is_unchanged` must still pass with the existing hash `912907497f6da52e3f4907a43a0f176a4b71784194f9ebfab5faae133fd20ea9`.

- **Box-drawing characters**: Use `══` (U+2550 BOX DRAWINGS DOUBLE HORIZONTAL) to match the exact characters specified in the PRD. Verify the characters survive save/re-read without corruption.

- **CACHE_DIR placement**: `CACHE_DIR` is set via env-var lookup (not directly agent-settable), so it sits above the SESSION SETUP header. The SESSION SETUP header should go between the CACHE_DIR closing `)` and `BACKTEST_START`. Do not move `CACHE_DIR` or add it to the SESSION SETUP block.

- **`train_pnl_consistency` column**: This column already exists in the `results.tsv` header in `program.md` (column 9). V3-G adds *enforcement* via the keep/discard condition — no header schema change needed, only the keep/discard rule and `discard-inconsistent` status definition.

- **Backward compatibility**: Existing autoresearch worktrees on other branches are unaffected. The new headers are pure comments; all constant values and default behaviors remain identical.
