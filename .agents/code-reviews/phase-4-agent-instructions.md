# Code Review: Phase 4 — Agent Instructions (`program.md`)

**Reviewer**: Claude Code
**Date**: 2026-03-19
**Branch**: master (unstaged changes)

---

## Stats

- Files Modified: 1 (`program.md`)
- Files Added: 1 (`tests/test_program_md.py`)
- Files Deleted: 0
- New lines: 95
- Deleted lines: 66

---

## Test Results

All 74 tests pass (51 pre-existing + 23 new). No regressions introduced.

```
tests/test_program_md.py — 23 passed
tests/test_backtester.py — 15 passed
tests/test_prepare.py    — 16 passed
tests/test_screener.py   — 20 passed
Total: 74 passed in 3.37s
```

---

## Issues Found

### Medium

```
severity: medium
file: .agents/plans/phase-4-agent-instructions.md
line: 442 (and 495, 511, 557, 561)
issue: Plan has internally inconsistent test counts — references both "20" and "23" tests across different sections
detail: The plan says "20 test functions" in Task 1.2 IMPLEMENT (line 495), the completion checklist (line 557),
        Level 2/3 validation commands (lines 511, 517), and the Level 3 math "51 + 20 = 74" (line 450) — but
        also says "23 tests" at the Wave 1 Checkpoint (line 498), the acceptance criteria (line 543), and the
        Notes section. The actual test file has 23 functions. The math at line 450 reads "51 pre-existing + 20 new
        = 74 tests" which is arithmetically wrong (51 + 23 = 74 is correct, 51 + 20 = 71). Only the total "74"
        and one checkpoint "23" are correct; the rest say "20".
suggestion: This is a plan file consistency issue, not a code bug. The implementation is correct (23 tests exist
            and all pass). The plan itself can be updated to say "23" everywhere, but since it is a planning
            artifact, this does not affect runtime behavior.
```

### Low

```
severity: low
file: tests/test_program_md.py
line: 11–12
issue: _content() re-reads the file from disk on every call — 23 tests × 1 read = 23 redundant file reads
detail: Each test calls _content() independently, which calls PROGRAM_MD.read_text() each time. For a 135-line
        markdown file this is negligible in practice (23 reads completes in 0.06s), but it is inconsistent
        with the pattern used in other test files in this repo (e.g. test_prepare.py uses fixtures). If
        program.md ever becomes large or tests multiply further, the cost accumulates.
suggestion: Use a module-level constant: CONTENT = PROGRAM_MD.read_text(encoding="utf-8") at the top of the
            file. All tests then reference CONTENT directly rather than calling _content(). This eliminates
            the redundant reads and matches how the content is used (read-once, assert-many). Not a bug —
            acceptable as-is for a small markdown file.
```

```
severity: low
file: tests/test_program_md.py
line: 119–121
issue: test_no_val_bpb_references uses an import re that is never used
detail: Line 6 imports the `re` module, but no test function uses it. This is dead code.
suggestion: Remove `import re` from line 6. All assertions use `in` string containment rather than regex.
```

```
severity: low
file: program.md
line: 62 (sharpe count boundary)
issue: The sharpe occurrence count is exactly at the spec minimum — one content change could break the validation check
detail: `grep -c "sharpe" program.md` returns exactly 10, which is the minimum the plan specifies (>= 10).
        The count is correct, but it is at the boundary. If a future edit removes even one mention (e.g.
        consolidating two sentences), the validation command would fail even though the document still
        contains meaningful sharpe references. This is a documentation concern, not a code bug.
suggestion: No action required. The boundary condition is intentional (plan says ">= 10") and the document
            reads clearly with 10 references. Just be aware when editing program.md in the future.
```

---

## Correctness Verification

**Output format match**: The 7-field output block in `program.md` (lines 55–63) exactly matches what `train.py:print_results()` prints (lines 374–380 of train.py). Field names, ordering, and formatting strings are consistent.

**Grep commands**: `grep "^sharpe:" run.log` and `grep "^total_trades:" run.log` correctly match the anchored format `print(f"sharpe:              {stats['sharpe']:.6f}")` — the `^` anchor is valid and precise.

**results.tsv schema**: The 5-column tab-separated header `commit\tsharpe\ttotal_trades\tstatus\tdescription` is consistent between the document text, the example rows, and the test assertion at line 37.

**git reset command**: `git reset --hard HEAD~1` is correctly used for reverting a single commit. This matches the loop semantics (each experiment is one commit).

**Branch path**: The `~/.cache/autoresearch/stock_data/` path correctly matches `CACHE_DIR` in both `prepare.py` and `train.py`.

**Legacy content removal**: Confirmed zero occurrences of `val_bpb`, `vram`, `5 minute`, `5-minute`, `peak_vram`, `mfu_percent`, `total_tokens` in the new `program.md`.

---

## Summary

The implementation is correct. All 74 tests pass with no regressions. The `program.md` rewrite is complete and self-consistent: output format matches `train.py`, grep commands match the anchored field names, the results.tsv schema is correct, and all legacy nanochat content has been removed.

The only substantive finding is a dead `import re` in the test file (low severity, no behavioral impact) and inconsistent test count numbers in the plan file (medium severity in the plan, not in code — the implementation has the correct 23 tests).
