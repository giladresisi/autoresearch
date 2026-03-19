# Feature: Phase 4 — Agent Instructions (`program.md`)

**⚠️ EXECUTION RULES — READ FIRST:**
- Implement ALL changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
- Only make code changes — no git operations

Validate documentation and codebase patterns before implementing. Match naming of existing utils, types, and models. Import from correct files.

---

## Feature Description

Rewrite `program.md` to replace the legacy nanochat/GPU optimization instructions with updated instructions for the stock Sharpe optimization loop. The new instructions must guide an autonomous LLM agent through: setting up a fresh run branch, verifying cached data, running the backtester, logging results to `results.tsv`, and looping indefinitely to maximize Sharpe ratio by modifying `train.py`.

## User Story

As an LLM agent executing the optimization loop
I want clear, unambiguous instructions for the stock strategy experiment session
So that I can run autonomously overnight without pausing to ask the user for clarification

## Problem Statement

`program.md` still contains nanochat/GPU instructions that reference:
- `val_bpb` as the metric (lower is better) — should be `sharpe` (higher is better)
- GPU VRAM as a constraint — irrelevant for the stock backtester
- A 5-minute wall-clock training budget — irrelevant for the backtester
- `results.tsv` schema with `val_bpb` and `memory_gb` columns — must be replaced

## Solution Statement

Rewrite `program.md` top-to-bottom, preserving the overall structure (setup → experiment loop → output format → logging → loop rules) while substituting all stock-specific content. The document is the single source of truth for how the agent runs; it must be complete and self-contained.

## Feature Metadata

**Feature Type**: Refactor
**Complexity**: Low
**Primary Systems Affected**: `program.md` (documentation only), `tests/test_program_md.py` (new test)
**Dependencies**: None (no external APIs or packages)
**Breaking Changes**: No — `program.md` is read by the agent at session start, not imported by code

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ BEFORE IMPLEMENTING

- `program.md` (entire file) — Why: Current nanochat instructions to be replaced
- `train.py` (lines 371–389) — Why: `print_results()` and `__main__` define the exact output format the agent must parse; grep commands must match exactly
- `prepare.py` (lines 1–20) — Why: Constants `TICKERS`, `BACKTEST_START`, `BACKTEST_END`, `CACHE_DIR` that the agent references; verify spelling and casing match
- `prd.md` (§Feature 6, §Phase 4, §Success Criteria) — Why: Canonical spec for program.md content and results.tsv schema

### New Files to Create

- `tests/test_program_md.py` — Structural pytest that validates required sections and commands are present in `program.md`

### Files to Modify

- `program.md` — Full rewrite per spec below

### Patterns to Follow

**Existing structure (program.md)**: Keep the four sections: Setup, Experimentation, Output format, Logging results, Experiment loop. Replace all content.
**Logging discipline**: No `print()` in test file; use assertions only.
**Test naming**: `test_*` functions, plain pytest (no fixtures needed).

---

## SPECIFICATION: New `program.md` Content

The rewritten `program.md` must contain **all** of the following sections and content. Implementation must match this specification exactly — not paraphrase it.

### Section 1: Title and preamble

```
# autoresearch
```

One-sentence intro: autonomous stock strategy optimizer using Sharpe ratio.

### Section 2: Setup (once)

Numbered steps:
1. **Agree on a run tag**: propose based on today's date (e.g. `mar18`). Branch `autoresearch/<tag>` must not already exist.
2. **Create the branch**: `git checkout -b autoresearch/<tag>` from current master.
3. **Read the in-scope files**: `README.md`, `prepare.py` (read-only), `train.py` (the file you modify).
4. **Verify data exists**: Check `~/.cache/autoresearch/stock_data/` contains `.parquet` files. If not, tell the human to run `uv run prepare.py` and wait.
5. **Initialize results.tsv**: Create with just the header row. Baseline recorded after first run.
6. **Confirm and go**: Confirm setup looks good, then begin the loop.

### Section 3: Experimentation rules

**What you CAN do**:
- Modify `screen_day()` in `train.py` — screener criteria, thresholds, indicator parameters, entry/exit rules, indicator helper functions.
- Modify `manage_position()` in `train.py` — stop management logic, breakeven trigger level, trailing stop behavior.
- Add new indicator helper functions that `screen_day()` or `manage_position()` call.

**What you CANNOT do**:
- Modify `run_backtest()` — it is the evaluation harness. Its loop logic (stop detection, mark-to-market, entry at `price_10am + 0.03`, position sizing at `$500 / entry_price`) is fixed and must not be touched.
- Modify `print_results()` or the output format block — the agent parses this output; changing it breaks the loop.
- Modify `load_ticker_data()` or `load_all_ticker_data()` — fixed data loading infrastructure.
- Modify `CACHE_DIR`, `BACKTEST_START`, `BACKTEST_END` constants.
- Modify `prepare.py` — read-only data pipeline.
- Modify the Sharpe computation formula inside `run_backtest()`.
- Install new packages or add dependencies beyond what's in `pyproject.toml`.

**The goal**: maximize `sharpe` (higher Sharpe = better). Unlike the old `val_bpb`, **higher is better**.

**Simplicity criterion**: All else being equal, simpler is better. A small Sharpe improvement that adds ugly complexity is not worth it. A simplification that maintains equal or better Sharpe is always a win. Prefer interpretable threshold changes over convoluted logic.

**First run**: Always run the strategy as-is to establish the baseline before making changes.

### Section 4: Output format

The script prints a fixed-format block when it completes successfully:

```
---
sharpe:              1.234567
total_trades:        12
win_rate:            0.583
avg_pnl_per_trade:   18.45
total_pnl:           221.40
backtest_start:      2026-01-01
backtest_end:        2026-03-01
```

Extract the key metrics:
```
grep "^sharpe:" run.log
grep "^total_trades:" run.log
```

Exit code 0 on success. Exit code 1 if cache is empty (no parquet files found).

### Section 5: Logging results

Log to `results.tsv` (tab-separated, NOT comma-separated — commas break in descriptions).

Header row and 5 columns:
```
commit	sharpe	total_trades	status	description
```

1. `commit`: git commit hash (short, 7 chars)
2. `sharpe`: Sharpe ratio achieved (e.g. `1.234567`) — use `0.000000` for crashes
3. `total_trades`: number of trades completed in the backtest — use `0` for crashes
4. `status`: `keep`, `discard`, or `crash`
5. `description`: short text description of what this experiment tried

Example:
```
commit	sharpe	total_trades	status	description
a1b2c3d	0.000000	0	keep	baseline (no trades, strict screener)
b2c3d4e	1.234567	12	keep	relaxed CCI threshold to -30
c3d4e5f	0.872000	9	discard	removed volume filter
d4e5f6g	0.000000	0	crash	divide-by-zero in custom indicator
```

Do NOT commit `results.tsv` — it is intentionally untracked.

### Section 6: The experiment loop

LOOP FOREVER:

1. Check git state: current branch and commit.
2. Modify `train.py` with an experimental idea. Edit the file directly.
3. `git commit` (commit the change to `train.py` only; leave `results.tsv` untracked).
4. Run: `uv run train.py > run.log 2>&1` (redirect everything — do NOT use tee).
5. Extract results: `grep "^sharpe:" run.log` and `grep "^total_trades:" run.log`
6. If grep output is empty, the run crashed. Run `tail -n 50 run.log` to read the Python traceback and attempt a fix. If you cannot fix it within a few attempts, give up: log status `crash`, `git reset --hard HEAD~1`, and move on.
7. Record the result in `results.tsv`.
8. If Sharpe **improved (higher)** compared to the current best → keep the commit, advance the branch.
9. If Sharpe is equal or worse → `git reset --hard HEAD~1` (revert to previous commit).

**NEVER STOP**: Once the loop has begun, do NOT pause to ask the user if you should continue. The user may be asleep. You are autonomous. Loop until manually stopped. If you run out of ideas: try relaxing individual screener criteria, combining near-misses, varying the stop management trigger level, or adjusting position sizing logic.

**Crashes**: If a run crashes and the fix is trivial (typo, missing import), fix and re-run. If the underlying idea is broken, log `crash` and move on.

**No trades**: A Sharpe of `0.0` with `total_trades: 0` means the screener found zero signals. Try relaxing one threshold at a time to generate at least some trades before optimizing.

---

## PARALLEL EXECUTION STRATEGY

### Dependency Graph

```
┌──────────────────────────────────────────────────────────┐
│ WAVE 1: Parallel Foundation                              │
├─────────────────────────┬────────────────────────────────┤
│ Task 1.1: REWRITE       │ Task 1.2: CREATE test file     │
│ program.md              │ tests/test_program_md.py       │
│ Agent: writer           │ Agent: tester                  │
└─────────────────────────┴────────────────────────────────┘
                          ↓
┌──────────────────────────────────────────────────────────┐
│ WAVE 2: Validation (Sequential)                          │
├──────────────────────────────────────────────────────────┤
│ Task 2.1: Run full test suite; human review program.md   │
└──────────────────────────────────────────────────────────┘
```

### Parallelization Summary

**Wave 1 — Fully Parallel**: Tasks 1.1 and 1.2 — no shared state; test contract is defined by this plan
**Wave 2 — Sequential**: Task 2.1 — depends on both Wave 1 tasks completing

### Interface Contracts

**Contract for Task 1.2**: The test checks that `program.md` contains specific required strings. Those strings are defined in the TESTING STRATEGY below — Task 1.2 can be written before Task 1.1 completes by using the plan spec as the contract.

### Synchronization Checkpoints

**After Wave 1**: `pytest tests/test_program_md.py -v`
**After Wave 2**: `pytest -v` (full suite, all 51 + new tests pass)

---

## IMPLEMENTATION PLAN

### Wave 1, Task 1.1: REWRITE `program.md`

Replace entire file contents with new stock optimization instructions matching the SPECIFICATION section above exactly.

Key content requirements:
- Section 1: Title `# autoresearch` and one-line intro
- Section 2: 6-step setup (branch, read files, verify cache, init results.tsv)
- Section 3: CAN/CANNOT lists, goal = maximize sharpe (higher is better), simplicity criterion, first-run baseline rule
- Section 4: Exact output block from `print_results()` in `train.py` (7 lines after `---`), grep commands for `^sharpe:` and `^total_trades:`
- Section 5: results.tsv header `commit\tsharpe\ttotal_trades\tstatus\tdescription` and example rows
- Section 6: Numbered loop steps (1–9), NEVER STOP instruction, crash/no-trades handling

Do not preserve any content from the current `program.md` (it is the old nanochat file).

**Validate**: Visual review — does it read clearly as agent instructions? Run `grep "sharpe" program.md | wc -l` — should be ≥ 10 occurrences.

---

### Wave 1, Task 1.2: CREATE `tests/test_program_md.py`

Write a pytest file that loads `program.md` and asserts all required structural elements are present.

**Required assertions** (each must be a separate test function):

```python
import pathlib
import re

PROGRAM_MD = pathlib.Path(__file__).parent.parent / "program.md"

def _content():
    return PROGRAM_MD.read_text(encoding="utf-8")

def test_file_exists():
    assert PROGRAM_MD.exists(), "program.md must exist"

def test_title():
    assert "# autoresearch" in _content()

def test_setup_section():
    c = _content()
    assert "Setup" in c
    assert "autoresearch/" in c  # branch naming pattern

def test_verify_cache_instruction():
    c = _content()
    assert "~/.cache/autoresearch/stock_data" in c
    assert "prepare.py" in c  # tells agent to run prepare.py if cache missing

def test_results_tsv_header():
    c = _content()
    assert "commit\tsharpe\ttotal_trades\tstatus\tdescription" in c

def test_grep_sharpe_command():
    c = _content()
    assert 'grep "^sharpe:" run.log' in c

def test_grep_total_trades_command():
    c = _content()
    assert 'grep "^total_trades:" run.log' in c

def test_output_block_format():
    """Output block must contain the 7 lines from print_results()."""
    c = _content()
    assert "sharpe:" in c
    assert "total_trades:" in c
    assert "win_rate:" in c
    assert "avg_pnl_per_trade:" in c
    assert "total_pnl:" in c
    assert "backtest_start:" in c
    assert "backtest_end:" in c

def test_higher_sharpe_is_better():
    c = _content()
    assert "higher" in c.lower()
    # Must NOT say "lower is better" (val_bpb legacy)
    assert "lower is better" not in c

def test_cannot_modify_prepare():
    c = _content()
    assert "prepare.py" in c
    # Must have restriction on modifying prepare.py
    assert "CANNOT" in c or "cannot" in c.lower()

def test_cannot_modify_run_backtest():
    """Backtest loop is the evaluation harness — must be explicitly off-limits."""
    c = _content()
    assert "run_backtest" in c

def test_can_modify_screen_day():
    c = _content()
    assert "screen_day" in c

def test_can_modify_manage_position():
    c = _content()
    assert "manage_position" in c

def test_cannot_modify_backtest_window():
    c = _content()
    assert "BACKTEST_START" in c
    assert "BACKTEST_END" in c

def test_never_stop_instruction():
    c = _content()
    # NEVER STOP or equivalent
    assert "NEVER" in c or "never stop" in c.lower()

def test_results_tsv_not_committed():
    c = _content()
    assert "untracked" in c.lower() or "do not commit" in c.lower() or "NOT commit" in c

def test_run_command():
    c = _content()
    assert "uv run train.py > run.log 2>&1" in c

def test_git_reset_hard():
    c = _content()
    assert "git reset --hard HEAD~1" in c

def test_no_val_bpb_references():
    c = _content()
    assert "val_bpb" not in c, "Legacy val_bpb metric must not appear in new program.md"

def test_no_vram_references():
    c = _content()
    assert "vram" not in c.lower(), "Legacy VRAM references must not appear"

def test_no_five_minute_budget():
    c = _content()
    assert "5 minute" not in c.lower() and "5-minute" not in c.lower()

def test_no_trades_guidance():
    """Agent must have guidance for the 'zero trades' scenario."""
    c = _content()
    assert "no trade" in c.lower() or "0 trade" in c.lower() or "zero signal" in c.lower() or "no signal" in c.lower()

def test_status_values():
    c = _content()
    assert "keep" in c
    assert "discard" in c
    assert "crash" in c
```

**Validate**: `pytest tests/test_program_md.py -v` — all tests pass.

---

### Wave 2, Task 2.1: Validate & Human Review

**Steps**:
1. Run `pytest tests/test_program_md.py -v` — all tests must pass.
2. Run full suite: `pytest -v` — all 51 pre-existing tests + new program_md tests pass; 0 failures.
3. Read `program.md` top-to-bottom. Verify:
   - A first-time reader (no prior context) can follow the setup steps
   - The loop steps are unambiguous
   - The metric direction (higher = better) is stated clearly
   - The results.tsv example shows a zero-trade baseline (common on first run with strict screener)

---

## TESTING STRATEGY

| What | Type | Automation | Tool | File | Run command |
|------|------|-----------|------|------|-------------|
| File exists | Structural | ✅ | pytest | `tests/test_program_md.py` | `pytest tests/test_program_md.py::test_file_exists -v` |
| Title present | Structural | ✅ | pytest | `tests/test_program_md.py` | `pytest tests/test_program_md.py::test_title -v` |
| Setup section present | Structural | ✅ | pytest | `tests/test_program_md.py` | `pytest tests/test_program_md.py::test_setup_section -v` |
| Cache path instruction | Structural | ✅ | pytest | `tests/test_program_md.py` | `pytest tests/test_program_md.py::test_verify_cache_instruction -v` |
| results.tsv header correct | Structural | ✅ | pytest | `tests/test_program_md.py` | `pytest tests/test_program_md.py::test_results_tsv_header -v` |
| Grep sharpe command | Structural | ✅ | pytest | `tests/test_program_md.py` | `pytest tests/test_program_md.py::test_grep_sharpe_command -v` |
| Grep total_trades command | Structural | ✅ | pytest | `tests/test_program_md.py` | `pytest tests/test_program_md.py::test_grep_total_trades_command -v` |
| Output block 7 fields | Structural | ✅ | pytest | `tests/test_program_md.py` | `pytest tests/test_program_md.py::test_output_block_format -v` |
| Higher sharpe = better | Structural | ✅ | pytest | `tests/test_program_md.py` | `pytest tests/test_program_md.py::test_higher_sharpe_is_better -v` |
| Cannot modify prepare.py | Structural | ✅ | pytest | `tests/test_program_md.py` | `pytest tests/test_program_md.py::test_cannot_modify_prepare -v` |
| Cannot modify backtest window | Structural | ✅ | pytest | `tests/test_program_md.py` | `pytest tests/test_program_md.py::test_cannot_modify_backtest_window -v` |
| NEVER STOP instruction | Structural | ✅ | pytest | `tests/test_program_md.py` | `pytest tests/test_program_md.py::test_never_stop_instruction -v` |
| results.tsv untracked | Structural | ✅ | pytest | `tests/test_program_md.py` | `pytest tests/test_program_md.py::test_results_tsv_not_committed -v` |
| Run command present | Structural | ✅ | pytest | `tests/test_program_md.py` | `pytest tests/test_program_md.py::test_run_command -v` |
| Git reset hard present | Structural | ✅ | pytest | `tests/test_program_md.py` | `pytest tests/test_program_md.py::test_git_reset_hard -v` |
| No val_bpb references | Legacy cleanup | ✅ | pytest | `tests/test_program_md.py` | `pytest tests/test_program_md.py::test_no_val_bpb_references -v` |
| No VRAM references | Legacy cleanup | ✅ | pytest | `tests/test_program_md.py` | `pytest tests/test_program_md.py::test_no_vram_references -v` |
| No 5-minute budget | Legacy cleanup | ✅ | pytest | `tests/test_program_md.py` | `pytest tests/test_program_md.py::test_no_five_minute_budget -v` |
| No-trades guidance | Edge case | ✅ | pytest | `tests/test_program_md.py` | `pytest tests/test_program_md.py::test_no_trades_guidance -v` |
| Status values (keep/discard/crash) | Structural | ✅ | pytest | `tests/test_program_md.py` | `pytest tests/test_program_md.py::test_status_values -v` |
| Content clarity/agent usability | Readability | ⚠️ Manual | Human review | `program.md` | Read top-to-bottom |

**Manual test justification**: Content clarity — whether a first-time reader can follow the instructions without confusion — requires human judgment. Structural correctness (all required strings present) is fully automated; the manual step is strictly about prose quality and logical flow.

### Test Automation Summary

| | Count | % |
|---|---|---|
| ✅ Automated (pytest structural) | 23 | 96% |
| ⚠️ Manual (human readability review) | 1 | 4% |
| **Total** | 24 | 100% |

---

## VALIDATION COMMANDS

### Level 1: Structural Completeness

```bash
# All legacy references removed
grep -n "val_bpb\|vram\|VRAM\|peak_vram\|5 minute\|5-minute\|num_steps\|mfu_percent\|total_tokens" program.md
# Expected: no output (zero matches)

# Required metric references present
grep -c "sharpe" program.md
# Expected: >= 10

# results.tsv header tab-separated
grep "commit	sharpe	total_trades	status	description" program.md
# Expected: one match
```

### Level 2: Unit Tests (structural pytest)

```bash
pytest tests/test_program_md.py -v
# Expected: 20/20 passing, 0 failures
```

### Level 3: Full Test Suite (regression)

```bash
pytest -v
# Expected: 51 pre-existing + 20 new = 74 tests, 0 failures
```

### Level 4: Manual Human Review

Read `program.md` from top to bottom. Verify:
- [ ] First-time reader can follow setup steps without external reference
- [ ] Optimization direction is unmistakably stated (higher Sharpe = keep)
- [ ] Loop steps are numbered and unambiguous
- [ ] Crash handling section covers: easy fix (re-run), hard failure (log crash, reset, move on)
- [ ] No-trades guidance is actionable (agent knows to relax thresholds)
- [ ] results.tsv example shows a realistic zero-trade baseline row
- [ ] The document stands alone — no dependency on prior sessions or memory

---

## STEP-BY-STEP TASKS

### WAVE 1: Foundation (Parallel)

#### Task 1.1: REWRITE `program.md`

- **WAVE**: 1
- **AGENT_ROLE**: writer
- **DEPENDS_ON**: []
- **BLOCKS**: [2.1]
- **PROVIDES**: Updated program.md with stock optimization instructions
- **IMPLEMENT**:
  1. Read current `program.md` to understand structure being replaced
  2. Write new file matching SPECIFICATION section above completely
  3. Ensure all 6 sections are present: title/intro, setup, experimentation rules, output format, logging, experiment loop
  4. Include the exact output block from `train.py:print_results()` (lines 373–380 of train.py)
  5. Include tab-separated header: `commit\tsharpe\ttotal_trades\tstatus\tdescription`
  6. Include example rows showing baseline (0 trades is common on first run), keep, discard, crash
  7. State "higher Sharpe = better" explicitly (twice if needed — once in rules, once in loop)
  8. Include NEVER STOP instruction
- **VALIDATE**: `grep -c "sharpe" program.md` → ≥ 10; `grep "val_bpb" program.md` → no output

#### Task 1.2: CREATE `tests/test_program_md.py`

- **WAVE**: 1
- **AGENT_ROLE**: tester
- **DEPENDS_ON**: []
- **BLOCKS**: [2.1]
- **PROVIDES**: 20-test structural pytest suite for program.md
- **IMPLEMENT**: Write all 20 test functions from the TESTING STRATEGY section above. Use `pathlib.Path` to locate `program.md` relative to the test file (`../../program.md` from `tests/`). No fixtures needed — each test reads the file independently.
- **VALIDATE**: `pytest tests/test_program_md.py -v` after Task 1.1 completes

**Wave 1 Checkpoint**: `pytest tests/test_program_md.py -v` — all 23 tests pass.

---

### WAVE 2: Validation (Sequential)

#### Task 2.1: Run validation suite + human review

- **WAVE**: 2
- **AGENT_ROLE**: qa
- **DEPENDS_ON**: [1.1, 1.2]
- **PROVIDES**: Confirmed Phase 4 complete
- **IMPLEMENT**:
  1. Run `pytest tests/test_program_md.py -v` — verify 20/20
  2. Run `pytest -v` — verify 71 total tests, 0 failures
  3. Run Level 1 grep checks (no val_bpb, sharpe count ≥ 10, header present)
  4. Read program.md and apply the human review checklist from Level 4
- **VALIDATE**: All validation levels pass

**Final Checkpoint**: `pytest -v` — 71 tests pass, 0 failures.

---

## ACCEPTANCE CRITERIA

### Functional
- [ ] `program.md` is fully rewritten — zero nanochat/GPU content remaining (`val_bpb`, `vram`, `5 minute`, `peak_vram_mb` do not appear)
- [ ] `program.md` contains a 6-step setup section (tag, branch, read files, verify cache, init results.tsv, confirm)
- [ ] `program.md` states the optimization direction explicitly: higher Sharpe = better
- [ ] `program.md` explicitly names `screen_day()` and `manage_position()` as the only modifiable functions
- [ ] `program.md` explicitly names `run_backtest()` as off-limits (evaluation harness, must not be touched)
- [ ] `program.md` contains the exact output block from `train.py:print_results()` (7 fields: sharpe, total_trades, win_rate, avg_pnl_per_trade, total_pnl, backtest_start, backtest_end)
- [ ] `program.md` contains the grep commands `grep "^sharpe:" run.log` and `grep "^total_trades:" run.log`
- [ ] `program.md` contains the results.tsv header `commit\tsharpe\ttotal_trades\tstatus\tdescription` (tab-separated, 5 columns)
- [ ] `program.md` includes a NEVER STOP instruction (agent runs autonomously until manually interrupted)
- [ ] `program.md` includes guidance for the zero-trades scenario (relax screener thresholds to generate signals)
- [ ] `program.md` includes the run command `uv run train.py > run.log 2>&1`
- [ ] `program.md` includes `git reset --hard HEAD~1` for discarding experiments

### Error Handling
- [ ] `program.md` describes crash handling: trivial fixes get a re-run; fundamentally broken ideas get logged as `crash` and discarded
- [ ] `program.md` states that `results.tsv` must NOT be committed (intentionally untracked)

### Validation
- [ ] `tests/test_program_md.py` is created with 23 structural tests, all passing — verified by: `pytest tests/test_program_md.py -v`
- [ ] Full test suite passes with 0 failures — verified by: `pytest -v` (74 total: 51 pre-existing + 23 new)
- [ ] Legacy content removed — verified by: `grep "val_bpb\|vram\|5 minute" program.md` returns no output
- [ ] Sharpe references present — verified by: `grep -c "sharpe" program.md` returns ≥ 10

### Out of Scope
- Modifying `train.py`, `prepare.py`, or any Python source files — Phase 4 is documentation only
- Verifying the agent can actually run the loop end-to-end — that is Phase 5 (E2E test)
- Testing prose quality programmatically — readability is a manual human review only

---

## COMPLETION CHECKLIST

- [ ] Task 1.1: `program.md` rewritten
- [ ] Task 1.2: `tests/test_program_md.py` created (20 tests)
- [ ] Wave 1 checkpoint: `pytest tests/test_program_md.py -v` — 20/20 pass
- [ ] Level 1 grep checks pass (no legacy content, sharpe count ≥ 10)
- [ ] Level 2: `pytest tests/test_program_md.py -v` — 20/20 pass
- [ ] Level 3: `pytest -v` — 74 tests, 0 failures
- [ ] Level 4: Human review checklist complete
- [ ] All acceptance criteria met
- [ ] **⚠️ Debug logs added during execution REMOVED**
- [ ] **⚠️ CRITICAL: Changes UNSTAGED — NOT committed**

---

## NOTES

### Why 23 structural tests for a markdown file?

`program.md` is the agent's primary interface. A wrong grep command (`^val_bpb:` instead of `^sharpe:`) or the wrong results.tsv schema would silently break the entire experiment loop — experiments would log to the wrong columns and the agent would make bad keep/discard decisions. Structural tests catch these failures early.

### Zero-trade baseline is normal

With `BACKTEST_START = "2026-01-01"` and `BACKTEST_END = "2026-03-01"`, the strict v2 screener may find zero matches. `program.md` must explicitly tell the agent this is expected on first run and how to handle it (relax criteria to generate signals before optimizing Sharpe).

### Tab separator in results.tsv

The original `program.md` specified tab-separated values explicitly ("NOT comma-separated — commas break in descriptions"). This must be preserved and the example rows must use literal tab characters, not spaces.

### Backtest window constants live in both files

`BACKTEST_START` and `BACKTEST_END` appear in both `prepare.py` and `train.py`. The agent instructions must make clear that both are off-limits for modification — the window is fixed per the PRD.

### Script deliverables check

Phase 4 does not introduce any runnable scripts. The output artifact is `program.md` (a document) and `tests/test_program_md.py` (a test file). No script runnability criteria apply.
