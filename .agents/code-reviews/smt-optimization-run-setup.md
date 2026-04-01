# Code Review: SMT Optimization Run Setup

**Plan**: `.agents/plans/smt-optimization-run-setup.md`
**Reviewer**: Claude Code (code-review skill)
**Date**: 2026-04-02

---

## Stats

- Files Modified: 5 (train_smt.py, program_smt.md, PROGRESS.md, prepare_futures.py, data/sources.py — last two are out-of-scope for this plan but were modified in the same changeset by a prior plan)
- Files Added: 4 (tests/test_data_sources.py, data/sources.py, .gitignore, pyproject.toml — from Databento pipeline plan)
- Files Deleted: 0
- New lines (plan scope only — train_smt.py + program_smt.md + PROGRESS.md): ~380
- Deleted lines (plan scope only): ~10

---

## Issues Found

---

```
severity: medium
file: train_smt.py
line: 759–762
issue: else-branch changes _n_included from 0 to 1 when fold_test_pnls is empty
detail: In the original code, when fold_test_pnls is empty the else-branch set
  _n_included = len(fold_test_pnls) == 0, correctly reporting zero folds included.
  The new code uses _source = fold_test_pnls if fold_test_pnls else [(0.0, 0)],
  so when fold_test_pnls is empty, _source = [(0.0, 0)] and _n_included = 1.
  The printed line "min_test_pnl_folds_included: 1" is misleading — it implies one
  real fold was counted when there were actually zero folds. The synthetic (0.0, 0)
  tuple is a sentinel, not a real fold. In practice, six folds always run, so this
  path is only reached if every fold has < 3 test trades; but the diagnostic value
  of the printed count is silently corrupted in that edge case.
suggestion: Separate the _n_included assignment from _source:
  _source = fold_test_pnls if fold_test_pnls else [(0.0, 0)]
  min_test_pnl  = min(p for p, t in _source)
  mean_test_pnl = sum(p for p, t in _source) / len(_source)
  _n_included   = len(fold_test_pnls)  # always reflects real fold count
```

---

```
severity: low
file: PROGRESS.md
line: 894
issue: Proposal 1 still targets min_test_pnl after primary objective was changed to mean_test_pnl
detail: Line 894 reads "Optimise for: `min_test_pnl` across all 6 folds (worst-fold
  robustness)". All other references in the section (lines 929–937) were correctly
  updated to mean_test_pnl as primary. This one line in the "Asymmetric stop ratios"
  proposal was missed, leaving a contradictory instruction for the optimizer agent:
  the agenda section in program_smt.md says "Optimise for: mean_test_pnl (primary)",
  but PROGRESS.md proposal 1 still says min_test_pnl.
suggestion: Update line 894 to:
  - Optimise for: `mean_test_pnl` (primary), `min_test_pnl > 0` (guard)
```

---

```
severity: low
file: train_smt.py
line: 123–147
issue: load_futures_data() modified outside the plan's stated scope with no mention in plan
detail: The plan's Task 1.1 specifies exactly two changes: (1a) annotate stop-ratio
  constants lines 91–100, and (1b) add mean_test_pnl to the __main__ block. However,
  load_futures_data() was also modified (docstring rewritten, path resolution logic
  changed from single IB path to two-path priority lookup). This change is correct
  and desirable for Databento integration, but it belongs to the Databento pipeline
  plan — not to this plan. The change is present and functionally sound, but was
  not listed in the plan's IMPLEMENTATION PLAN, ACCEPT CRITERIA, or COMPLETION
  CHECKLIST, making it invisible to reviewers tracking plan compliance.
  No functional bug — the logic is correct (historical_path checked first, then
  ib_path, then FileNotFoundError). This is a plan-drift note, not a logic error.
suggestion: No code change needed. Note: if the Databento pipeline plan is the
  source of this change, it is correctly attributed there. Cross-check against
  .agents/execution-reports/databento-historical-pipeline.md to confirm.
```

---

## Verified Correct

The following focus areas were explicitly checked and are correct:

**mean_test_pnl computation — if-branch (line 754–757)**
- `_qualified` = folds with >= 3 test trades (correct filter)
- `mean_test_pnl = sum(p ...) / len(_qualified)` — correct arithmetic mean over qualified folds only
- `min_test_pnl` computed from same `_qualified` list — consistent

**mean_test_pnl computation — else-branch (line 758–762)**
- Falls back to all folds when none have >= 3 trades
- Handles empty fold_test_pnls via synthetic sentinel (see Issue #1 for _n_included side-effect)
- Avoids ZeroDivisionError: sentinel [(0.0, 0)] ensures len(_source) >= 1

**min_test_pnl still printed (line 766)**
- Present, in correct position (after mean_test_pnl on line 765)
- min_test_pnl_folds_included still printed on line 767
- Print order matches plan spec and program_smt.md output format example

**Frozen section not modified**
- `# DO NOT EDIT BELOW THIS LINE` boundary is at line 450 in the current file (line 436 in HEAD before additions)
- All changes in train_smt.py are at lines 91–100 (constants block, editable) and lines 752–767 (__main__, which is below the frozen line but is the harness entry point, not the frozen strategy/harness internals)
- The frozen harness functions (lines 450–704) were not touched in this changeset

**program_smt.md — mean_test_pnl as primary target**
- Line 1 header updated
- Setup step 4 updated to record both metrics
- Evaluation criteria table: mean_test_pnl listed as PRIMARY, min_test_pnl as secondary guard
- Optimization Agenda sections 1–6 present and correctly structured
- Post-Iteration Analysis Protocol section present with Agenda Reassessment step

**PROGRESS.md — status and plan link**
- Line 846: `**Status**: ✅ Planned` — correct
- Line 848: `**Plan File**: .agents/plans/smt-optimization-run-setup.md` — correct
- Lines 929–930: Optimisation objective updated to mean_test_pnl primary, min_test_pnl secondary
- Lines 936–937: Agent instructions updated to reference both metrics

---

## Summary

Two issues require attention before using this changeset for an optimization run:

1. **Medium** — `_n_included` in the else-branch reports 1 instead of 0 when no folds ran. This corrupts the diagnostic print in the degenerate-case path; simple one-line fix.

2. **Low** — PROGRESS.md line 894 still says "Optimise for: `min_test_pnl`" in proposal 1, contradicting the new primary target everywhere else in the file. Will confuse an optimizer agent reading that section.

The core `mean_test_pnl` computation logic is correct. The frozen section is untouched. `min_test_pnl` and `min_test_pnl_folds_included` are still computed and printed correctly.
