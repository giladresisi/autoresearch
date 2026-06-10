# Execution Report: Incremental parquet validation (GIL-15)

**Date:** 2026-06-09
**Plan:** `.agents/plans/incremental-parquet-validation.md`
**Linear:** GIL-15
**Executor:** Team-based parallel (3 waves, 6 tasks)
**Outcome:** ✅ Success

---

## Executive Summary

Made the 1m main-parquet validation in the parquet-check engine incremental: after a one-time
full validation, subsequent runs validate only the gap-filled tail past a persisted watermark
plus the concatenation seam, with a body-integrity guard that forces a full re-scan on any
non-append rewrite (first-bar moved, truncation, interior insert). The 1s merge path gained an
additive seam WARN and the parquet-check SKILL.md was updated for the new sidecar write, flags,
and report fields. All 6 tasks landed, all 33 new automated tests pass, and the full suite shows
zero new failures versus the recorded baseline.

**Key Metrics:**
- **Tasks Completed:** 6/6 (100%)
- **Tests Added:** 33 (9 state + 9 tail + 12 incremental + 3 seam)
- **Test Pass Rate:** 33/33 new (100%); full suite 1150 passed / 23 pre-existing failures / 12 deselected
- **Files Modified:** 6 (4 created, 2 + 2 + 1 edited code/doc; 2 created test files; 2 edited test files)
- **Lines Changed:** +526 (tracked diff) plus 4 new untracked files (parquet_validation_state.py 84, parquet_tail.py 188, test_parquet_validation_state.py 166, test_parquet_tail.py 139)
- **Alignment Score:** 10/10

---

## Implementation Summary

### Wave 1 — Foundation utilities (parallel, new files)

- **`scripts/parquet_validation_state.py`** (Contract A): `VALIDATOR_VERSION`, `load_state`
  (returns `{}` on missing/corrupt — fail-safe), `get_watermark`, `set_watermark` (atomic `.tmp` +
  `os.replace`), and pure `needs_full_validation` returning one of
  `no-watermark | version-bump | body-rewritten | truncation`. Covered by
  `tests/test_parquet_validation_state.py` (9 tests).
- **`scripts/parquet_tail.py`** (Contract B): `index_bounds`, `row_count`, `read_after`,
  `bar_at_position` — pyarrow-metadata-cheap, resolving the `__index_level_0__` datetime index
  column and mirroring the existing tz handling. Covered by `tests/test_parquet_tail.py` (9 tests).

### Wave 2 — Core incremental refactor (sequential, `scripts/check_session_parquets.py`)

- Refactored `check_1m_parquet` to a watermark-gated full/incremental decision: cheap
  `index_bounds`/`row_count` probe, `needs_full_validation` gate, belt-and-suspenders
  body-integrity guard via `bar_at_position`, tail validation via `validate_session_df` + a
  `Close<=0` parity check, a new `_seam_ok` helper reusing `_is_expected_closed`, and the full
  path factored into `_full_validate_1m`.
- New report fields: `validation_scope` (`full`/`incremental`), `validated_through`,
  `full_reason`, `seam_issue`, `tail_rows`.
- `main()` gained `--full-validate` (force full, reason `forced-full`) and `--since <iso>`
  (forces an incremental re-scan from a given timestamp). Repair path re-seeds the watermark so
  the next run trusts the repaired body from a correct baseline.

### Wave 3 — 1s seam + docs (parallel)

- **`data/parquet_maintenance.py`**: added `_warn_on_seam` + a pre-concat seam WARN to
  `merge_session_1s_parquets` (overlap/duplicate and unexpected-gap cases), with a lazy import of
  `_is_expected_closed` to avoid a cycle. Merge behavior unchanged; clean seams stay silent.
- **`.claude/skills/parquet-check/SKILL.md`**: allowed-writes now lists the `.validation_state.json`
  sidecar (script-written, not agent), the `--full-validate`/`--since` flags are documented, the
  new report fields are described, and Step 3 notes the incremental tail delta vs. the periodic
  full safety net.

---

## Divergences from Plan

### Divergence #1: `--since` flag scope

**Classification:** ✅ GOOD (within plan intent)
**Planned:** `--since <iso>` described as "primarily a debugging/recovery aid" forcing an incremental re-scan from a given ts — minimal support expected.
**Actual:** Implemented and threaded through to `check_1m_parquet(since=...)`, and additionally exercised by a test (`test_seam_overlap_flagged` uses `since="2026-05-18 18:28:00"` to craft an overlap-seam scenario).
**Reason:** The seam-overlap test needed a deterministic way to position the watermark behind the tail; `--since` was the natural lever.
**Impact:** Positive — the flag is not merely declared but has real coverage through the seam test.
**Justified:** Yes.

No other divergences. Contracts A and B were implemented as specified; the incremental decision, guard, seam logic, report fields, CLI flags, repair-reset, 1s seam WARN, and SKILL.md edits all match the plan.

---

## Test Results

**New tests (33):**
- `tests/test_parquet_validation_state.py` — 9
- `tests/test_parquet_tail.py` — 9
- `tests/test_check_session_parquets.py::TestCheck1mIncremental` — 12
- `tests/test_parquet_maintenance.py` (seam) — 3

**Feature-file run** (`test_parquet_validation_state.py test_parquet_tail.py test_check_session_parquets.py test_parquet_maintenance.py -m "not integration"`):
`96 passed, 3 failed` — the 3 failures are the pre-existing `TestProcessInstrumentSessionEnd`
stale-assertion failures on the 1s `process_instrument` path (out of scope; see Coverage Gaps).

**Full suite** (`tests/ -m "not integration"` with side-effecting files deselected):
`1150 passed / 23 failed / 12 deselected`. The 23 failures are all pre-existing and identical to
the pre-implementation baseline. Zero new failures; all 33 new tests pass.

**Pass Rate:** 33/33 new (100%); 1150/1173 non-deselected (the 23 failures are pre-existing).

---

## What was tested

**Watermark store + guard (`test_parquet_validation_state.py`):**
- Missing sidecar loads as `{}`; corrupt/garbage JSON loads as `{}` (fail-safe toward full validation).
- `set_watermark` → `load_state` round-trips an entry exactly and preserves a co-resident second parquet's entry.
- `set_watermark` is atomic — no `.tmp` file left behind.
- `needs_full_validation` returns `(True, "no-watermark")` for `None`, `(True, "version-bump")` on an older validator version, `(True, "body-rewritten")` when `first_bar` changed, `(True, "truncation")` when row count dropped, and `(False, "")` on a pure append.

**pyarrow metadata + tail reader (`test_parquet_tail.py`):**
- `index_bounds` returns correct (first, last) across a multi-row-group file and `(None, None)` for a missing file.
- `row_count` equals `len(df)`.
- `read_after` returns exactly `df[df.index > watermark]` (asserted by frame equality), including when the watermark sits inside the last row group; returns empty when the watermark is at or past the last bar.
- `bar_at_position` returns the timestamp at integer positions spanning row-group boundaries and `None` for out-of-range positions.
- All helpers work on a single-row-group file; returned tail index stays tz-aware `America/New_York`.

**Incremental `check_1m_parquet` (`test_check_session_parquets.py::TestCheck1mIncremental`):**
- First run with no sidecar → `validation_scope == "full"`, `full_reason == "no-watermark"`, sidecar seeded at the last bar.
- Second run after a clean tail append → `validation_scope == "incremental"`, `tail_rows > 0`, severity ok, watermark advanced.
- No new bars since watermark → incremental, ok, watermark unchanged.
- Bad price (`Close<=0`/out-of-bounds) injected into the tail is surfaced at severity ≥ minor (core correctness guard — not silently passed).
- Seam overlap/duplicate is flagged (`seam_issue` set, severity ≥ minor); an unexpected weekday gap is flagged; an expected weekend gap passes as ok.
- A rewritten body (first bar changed / rows shrunk) under a stale watermark falls back to full validation with `full_reason` in `body-rewritten`/`truncation`.
- `--full-validate` forces full with `full_reason == "forced-full"`.
- `--dry-run` writes no sidecar while still reporting the scope it would take.
- A corrupt main followed by repair leaves a sidecar entry at the repaired last bar.
- Every `instruments_1m` entry carries `validation_scope` and `validated_through`.

**1s merge seam (`test_parquet_maintenance.py`):**
- A contiguous session merge emits no WARN and merges rows.
- An overlapping session start emits a stderr WARN and de-duplicates the index.
- An unexpected weekday seam gap emits a stderr WARN; merge behavior is unchanged.

---

## Validation Results

| Level | Command | Status | Notes |
|-------|---------|--------|-------|
| 1 | `import scripts.parquet_validation_state, scripts.parquet_tail, scripts.check_session_parquets, data.parquet_maintenance` | ✅ | `IMPORT_OK` |
| 2 | `pytest` on the 4 feature test files `-m "not integration"` | ✅ | 96 passed (incl. all 33 new); 3 pre-existing failures unchanged |
| 3 | `pytest tests/ -m "not integration"` (side-effecting deselected) | ✅ | 1150 passed / 23 pre-existing / 12 deselected; 0 new failures |
| 4 | `check_session_parquets.py --mode orchestrator-start --dry-run` | ⏭️ | Optional; deferred (a live IB Gateway is up on this machine) |

Side-effecting tests deselected per the plan's policy: `--ignore` of `test_ib_realtime.py`,
`test_ib_integration.py`, `test_orchestrator_kill_scope.py`, `test_orchestrator_main.py`,
`test_orchestrator_process.py` (live IB connects + machine-wide process kill).

---

## Coverage Gaps

Planned coverage (plan "Test Automation Summary"): **33 automated, 0 manual.**
Actual: **33 implemented and passing, 0 manual.** Planned vs actual match exactly. Residual gaps:

1. **Level 4 dry-run smoke not executed (low severity, accepted).** The plan marks it optional/
   deferred and gates it on "no live orchestrator/IB session active"; a live IB Gateway is up on
   this machine, so it was correctly not run. The two-run full→append→incremental sequence in
   `TestCheck1mIncremental` is the automated E2E equivalent the plan designated, and it passes.
   Recommended follow-up: run the dry-run once at a quiet, non-live window to confirm the live
   report carries `validation_scope`/`validated_through` on real ~861k-row parquets.

2. **3 pre-existing `TestProcessInstrumentSessionEnd` failures in a touched file (non-blocking,
   out of scope).** `test_ok_session_merges_and_backs_up`, `test_minor_session_merges_as_is`, and
   `test_late_start_escalates_to_rebuild` assert stale `action`/`merge_success` values on the 1s
   `process_instrument` path (e.g. expecting `rebuild_then_merge` where the code now returns
   `merge`). They predate this feature, are unrelated to the 1m incremental path, and were present
   in the baseline. Out of scope per the plan ("no change to merge/promotion/repair semantics");
   flagged here as a known stale-assertion cleanup for a future pass.

3. **`--since` flag (resolved — not a gap).** The plan flagged it as minimal/debugging support.
   It is implemented, threaded into `check_1m_parquet(since=...)`, and exercised by
   `test_seam_overlap_flagged` (which uses `since="2026-05-18 18:28:00"` to craft the overlap).
   No dedicated "since advances/limits the scan window" assertion exists beyond that seam use, so a
   single focused `--since` test would tighten coverage, but the path is not untested.

No correctness-critical path is uncovered: the body-integrity guard (body-rewrite / truncation /
interior-insert), the bad-tail-data surfacing guard, the fail-safe sidecar-parse path, and the
seam classifier all have direct passing tests.

---

## Challenges & Resolutions

**Challenge 1: datetime index column resolution in pyarrow metadata.**
- **Issue:** pandas writes the DatetimeIndex as `__index_level_0__`, not a named column, so the ts column for row-group statistics is not at a fixed schema position.
- **Resolution:** `parquet_tail` resolves the index column by name (`__index_level_0__`) before reading row-group statistics. Verified by `test_index_bounds_multi_rowgroup`, `test_read_after_*`, and `test_tz_preserved`.
- **Prevention:** Already documented implicitly in the contract; no further action.

**Challenge 2: parquet round-trips drop `DatetimeIndex.freq`.**
- **Issue:** Frame-equality assertions in the tail tests would fail because the in-memory expected frame carries `freq` while the read-back frame does not.
- **Resolution:** The test fixture strips `freq` from the expected index so equality holds. Documented inline in `test_parquet_tail.py`.

---

## Files Modified

**Created — code (2):**
- `scripts/parquet_validation_state.py` — Contract A watermark store + guard (84 lines)
- `scripts/parquet_tail.py` — Contract B pyarrow metadata + tail reader (188 lines)

**Created — tests (2):**
- `tests/test_parquet_validation_state.py` — 9 tests (166 lines)
- `tests/test_parquet_tail.py` — 9 tests (139 lines)

**Edited — code (2):**
- `scripts/check_session_parquets.py` — incremental `check_1m_parquet`, `_full_validate_1m`, `_seam_ok`, new report fields, `--full-validate`/`--since`, repair re-seed (+201/−18 region)
- `data/parquet_maintenance.py` — `_warn_on_seam` + seam WARN in `merge_session_1s_parquets` (+34)

**Edited — tests/docs (2):**
- `tests/test_check_session_parquets.py` — `TestCheck1mIncremental` (12 tests, +220)
- `tests/test_parquet_maintenance.py` — 3 seam tests (+56)
- `.claude/skills/parquet-check/SKILL.md` — allowed-writes sidecar, flags, report fields, Step 3 note (+24)

**Tracked diff total:** +526 insertions, −18 deletions, plus 4 new untracked files.

---

## Success Criteria Met

- [x] `parquet_validation_state.py` + `parquet_tail.py` exist and implement Contracts A and B.
- [x] Valid watermark → `validation_scope == "incremental"` (tail + seam only).
- [x] No/invalid watermark → full validation with `full_reason`, seeds the watermark.
- [x] `read_after` returns exactly `df[df.index > watermark]` (asserted by equality).
- [x] `--full-validate` forces full (`forced-full`); `--since` re-scans from a given ts.
- [x] Body-integrity guard forces full on rewrite / truncation / interior insert.
- [x] Bad tail data surfaced at severity ≥ minor (no correctness loss vs full scan).
- [x] Sidecar read/parse failure → full validation (fail-safe).
- [x] `--dry-run` performs no sidecar/`.bak` writes; still reports the would-be scope.
- [x] Two-run full→append→incremental evolves the sidecar and advances the watermark.
- [x] Repair path re-seeds the watermark to the repaired last bar.
- [x] Seam anomalies flagged; expected weekend/maintenance gaps pass.
- [x] 1s `merge_session_1s_parquets` emits stderr WARN on seam overlap/unexpected gap; behavior unchanged.
- [x] All 33 automated tests pass; full suite shows no NEW failures vs baseline.
- [x] Report exposes `validation_scope` + `validated_through` on every `instruments_1m` entry.
- [x] SKILL.md updated (allowed-writes sidecar, flags, report fields).
- [x] Changes UNSTAGED — not committed.
- [ ] Level 4 dry-run smoke — deferred/optional (live IB Gateway active). See Coverage Gaps #1.

---

## Recommendations for Future

**Plan Improvements:**
- The plan could note that the 3 `TestProcessInstrumentSessionEnd` failures live in a file this
  feature touches, so a reader expecting a fully green target file isn't surprised — they are
  baseline and out of scope.

**Process Improvements:**
- Add a single dedicated `--since` assertion (window-limited scan) to convert the resolved gap #3
  from "exercised incidentally" to "directly asserted".

**CLAUDE.md Updates:**
- None required; the work followed existing conventions (atomic sidecar writes, production-silent
  stderr `[check]` progress, mocked-IB tests against `tmp_path`).

---

## Conclusion

**Overall Assessment:** A clean, fully-aligned execution. Every planned task and test landed, the
load-bearing correctness guards (body-integrity, bad-tail surfacing, fail-safe sidecar) are
directly tested, and the change is additive with zero new failures across the full suite. The only
unexecuted item is an optional live dry-run smoke that is correctly blocked by an active IB Gateway,
and the remaining red tests are pre-existing and out of scope.

**Alignment Score:** 10/10 — implementation matches the plan's contracts, report fields, flags, and
test matrix; the single divergence (a slightly stronger `--since`) is a net positive.

**Ready for Production:** Yes — pending the standard policy that changes remain unstaged until the
user authorizes a commit, and a one-time live dry-run smoke at a quiet window as a final confidence check.
