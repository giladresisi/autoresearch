# Execution Report: Gap-Fill Retry Loop

**Date:** 2026-07-11
**Plan:** .agents/plans/gap-fill-retry-loop.md
**Executor:** Sequential, in-context (single agent, task-by-task per plan)
**Outcome:** ✅ Success

---

## Executive Summary

Unified production and offline IB historical-data gap-fill behavior behind one shared `run_gap_fill_with_retries()` loop in `data/ib_realtime.py`. Both `gap_fill.gap_fill_until_now()` (offline `trade.py gap-fill`) and `IbRealtimeSource.start()` (production, used by both `automation.main` and the orchestrator's pre-session thread) now retry the existing single-pass `gap_fill()` primitive every ~10.8 minutes until all 4 main parquets are caught up, instead of giving up after one pass. A new `GapFillFailedError` (raised after 5 consecutive round exceptions) propagates to both production call sites, each exiting with a new dedicated code `11`, distinct from the existing `2` (IB disconnect) and `10` (missing parquets).

**Key Metrics:**
- **Tasks Completed:** 7/7 (100%)
- **Tests Added:** 12 (9 in `tests/test_gap_fill.py`, 1 in `tests/test_automation_main.py`, 2 in `tests/test_orchestrator_main.py`) + 6 existing tests updated in place (2 in `test_gap_fill.py`, 4 in `test_ib_realtime.py`, 1 in `test_orchestrator_main.py`)
- **Test Pass Rate:** 100% on all targeted suites; full suite 1386 passed / 2 failed (pre-existing, unrelated) / 16 errors (pre-existing, unrelated) / 4 skipped / 12 deselected
- **Files Modified:** 8 (4 implementation, 4 test files)
- **Lines Changed:** +380 / -9
- **Execution Time:** ~35 minutes
- **Alignment Score:** 10/10 — implemented verbatim per the plan's exact code blocks, no deviations

---

## Implementation Summary

**Task 1 — `gap_hours_by_file()` + `GapFillFailedError`** (`data/ib_realtime.py:70-104`)
Added `_GAP_FILE_NAMES`, `_GAP_HEADSUP_HOURS=24`, `_GAP_CAUGHT_UP_HOURS=1` module constants; `gap_hours_by_file(bar_data_dir) -> dict` computes hours-since-last-bar per file (`float('inf')` for missing/empty/unreadable, matching existing graceful-degradation conventions); `GapFillFailedError(gaps_hours, last_error)` exception with both attributes plus a descriptive `str()`.

**Task 2 — `run_gap_fill_with_retries()`** (`data/ib_realtime.py:107-170`)
Shared retry loop: prints a one-time headsup for gaps >24h, calls `do_one_round()` in a loop spaced `round_spacing_s` (default 650s ≈ 10.8min) apart, resets the failure counter on any successful round (transient errors don't erode toward the cap), raises `GapFillFailedError` after `max_consecutive_failures` (default 5) consecutive round exceptions, and returns once every file's gap is `<= _GAP_CAUGHT_UP_HOURS`.

**Task 3 — Wire `gap_fill.gap_fill_until_now()`** (`gap_fill.py:90-91`)
Replaced the single `source.gap_fill()` call with `run_gap_fill_with_retries(source.gap_fill, bar_data_dir)`. `trade.py`'s CLI handler required no change (unchanged public signature).

**Task 4 — Wire `IbRealtimeSource.start()`** (`data/ib_realtime.py:890`)
Replaced `self.gap_fill()` with `run_gap_fill_with_retries(self.gap_fill, self._bar_data_dir)`. The single-pass `gap_fill()` primitive itself (`data/ib_realtime.py:855-882`) is untouched, confirmed via diff (no hunk touches its body).

**Task 5 — `automation/main.py` exit 11** (`automation/main.py:40`, `:1184-1193`)
Imports `GapFillFailedError` alongside the existing `IbGatewayDisconnectedError`/`IbRealtimeSource` import; adds `except GapFillFailedError as exc:` between the existing `except IbGatewayDisconnectedError:` block and `finally:`, printing a loud actionable message (last error, per-file gaps, the exact recovery command) and `sys.exit(11)`. The `finally:` cleanup block (IB disconnect + executor.stop) still runs regardless of which branch fires.

**Task 6 — `orchestrator/main.py` `_GracefulStop(exit_code)`** (`orchestrator/main.py:77-78`, `:241-247`, `:349-366`, `:592-595`)
`_GracefulStop` gained an `exit_code: int = 0` constructor param (the sole other raise site, `_check_stop_requested()`, is unaffected by the default). `_make_ib_health_check()`'s `check()` now special-cases `isinstance(exc, GapFillFailedError)`: prints a dedicated loud message and `raise _GracefulStop(exit_code=11)` instead of the routine maintenance-break message + `_GracefulStop()`. The routine "thread died, no GapFillFailedError" case is unchanged (still exits 0). `run()`'s `except _GracefulStop as _stop:` handler now does `sys.exit(_stop.exit_code)` instead of a hardcoded `sys.exit(0)`.

**Task 7 — Full regression pass**
`uv run pytest tests/ -q`: all targeted suites green; no new failures vs. the pre-implementation baseline (see Test Results below). Task 7 Step 2 (manual live-IB-Gateway sanity check) was explicitly marked optional/deferred in the plan and not attempted — a live trading process may be running on this machine and it requires a real IB Gateway connection with an actual gap present.

---

## Divergences from Plan

### Divergence #1: Line numbers shifted after Task 1+2 insertion

**Classification:** ⚠️ ENVIRONMENTAL

**Planned:** Plan cites `data/ib_realtime.py:779` for the `start()` call site and specific line ranges in `tests/test_ib_realtime.py` (213-235, 238-275, 304-341, 666-701) based on the file's state before Tasks 1-2 were applied.
**Actual:** After inserting ~103 lines for Task 1+2 (`gap_hours_by_file`, `GapFillFailedError`, `run_gap_fill_with_retries`), `start()`'s `self.gap_fill()` call moved to line 890, and the `tests/test_ib_realtime.py` line ranges shifted proportionally (final positions: 213, 240/270, 308/333, 672/695).
**Reason:** Sequential task execution naturally shifts absolute line numbers as earlier tasks insert code.
**Root Cause:** Expected consequence of the plan's own sequential structure — not a plan defect. All edits were re-located by function/test name (`grep -n`) rather than trusting stale absolute line numbers, and every code block matched the plan's literal content exactly at its new location.
**Impact:** None — purely mechanical, no behavioral difference.
**Justified:** Yes.

No other divergences. Every code block, test, docstring, and message string was implemented character-for-character as specified in the plan.

---

## Test Results

**Tests Added (12 new):**
- `test_gap_hours_by_file_all_current` / `_missing_file_is_inf` / `_empty_df_is_inf` — gap measurement correctness across present/missing/empty parquet states
- `test_gap_fill_failed_error_carries_gaps_and_last_error` — exception attribute + message contract
- `test_run_gap_fill_with_retries_single_round_closes_gap` — no sleep when the first round already closes the gap
- `test_run_gap_fill_with_retries_multiple_rounds_until_caught_up` — multi-round convergence, headsup message, `time.sleep(650.0)` spacing
- `test_run_gap_fill_with_retries_no_headsup_for_small_initial_gap` — no spurious headsup message under the 24h threshold
- `test_run_gap_fill_with_retries_failure_counter_resets_on_success` — a single success mid-sequence resets the consecutive-failure counter
- `test_run_gap_fill_with_retries_raises_after_max_consecutive_failures` — `GapFillFailedError` raised with correct `gaps_hours`/`last_error` after 5 straight failures
- `test_gap_fill_failed_exits_11` (automation) — `GapFillFailedError` from `_ib_source.start()` → `sys.exit(11)`
- `test_make_ib_health_check_gap_fill_failed_exits_11` (orchestrator) — pre-session thread death with `GapFillFailedError` → `_GracefulStop(exit_code=11)`, distinct message, no "maintenance break" text
- `test_graceful_stop_defaults_to_exit_code_0` (orchestrator) — default constructor behavior preserved for the pre-existing bare-raise call site

**Tests Updated (6, to accommodate the new retry-loop indirection without hanging on a real infinite loop against empty `tmp_path` fixtures):**
- `test_gap_fill_until_now_runs_reachable_merge_and_source` / `test_gap_fill_until_now_honours_skip_flags` (`test_gap_fill.py`) — patch `run_gap_fill_with_retries` to assert it's called with `(source.gap_fill, bar_data_dir)` and invoke the callable once
- `test_gap_fill_not_called_from_start`, `test_gateway_disconnect_raises_ibgateway_disconnected_error`, `test_ibgateway_disconnected_error_not_retried`, `test_1s_dfs_freed_after_gap_fill_in_start` (`test_ib_realtime.py`) — patch `data.ib_realtime.run_gap_fill_with_retries` to immediately invoke the passed callable once
- `test_make_ib_health_check_raises_graceful_stop_on_thread_death` (`test_orchestrator_main.py`) — additionally asserts `exit_code == 0` for the routine maintenance-break case

**Test Execution (final, post-implementation):**
```
tests/test_gap_fill.py            15 passed
tests/test_ib_realtime.py         45 passed
tests/test_automation_main.py     25 passed
tests/test_orchestrator_main.py   27 passed
tests/ (full suite)               1386 passed, 2 failed, 4 skipped, 12 deselected, 16 errors
```

**Baseline (pre-implementation, full suite):** 1373 passed, 3 failed, 4 skipped, 12 deselected, 16 errors — identical failure/error set except one flaky pre-existing test (`test_main_session_dirs_created`) that failed in the baseline run and passed in the final run (test-isolation flake unrelated to this change, confirmed by running it standalone: passes reliably). The 2 final failures (`tests/test_smt_fill_plot.py::test_regression_plot_renders_fill_mark`, `::test_session_plot_renders_fill_mark`) and all 16 errors (`tests/test_smt_decouple_active.py` fixture issue) are byte-identical to baseline and untouched by this change.

**Pass Rate:** 100% on every suite touched by the plan; 0 new failures across the full repository.

---

## What was tested

- `gap_hours_by_file()` returns near-zero hours for all-current parquets, `inf` for any missing file, and `inf` for an empty (zero-row) parquet.
- `GapFillFailedError` carries `.gaps_hours` and `.last_error` and includes both in its string representation.
- `run_gap_fill_with_retries()` returns immediately with no `time.sleep` call when the first round already closes the gap.
- `run_gap_fill_with_retries()` loops correctly across multiple rounds, prints the "Large gap detected" headsup exactly once only when the *initial* gap exceeds 24h, and sleeps `round_spacing_s` between rounds.
- `run_gap_fill_with_retries()` does NOT print the headsup message when the initial gap is small.
- `run_gap_fill_with_retries()`'s consecutive-failure counter resets after any successful round — a fail/ok/fail×5 sequence needs 7 total calls to trip the cap, not 6.
- `run_gap_fill_with_retries()` raises `GapFillFailedError` with the correct `gaps_hours` (re-measured fresh) and `last_error` after exactly `max_consecutive_failures` consecutive round exceptions.
- `gap_fill.gap_fill_until_now()` constructs the fill-only `IbRealtimeSource` with the expected kwargs and routes its `gap_fill` method through `run_gap_fill_with_retries(source.gap_fill, bar_data_dir)`, both in the reachable/merge happy path and with `check_reachable=False, merge_sessions=False`.
- `IbRealtimeSource.start()` still never calls the legacy private `_gap_fill()`, still raises `IbGatewayDisconnectedError` on gateway-initiated disconnect without retry, and still frees the in-memory 1s DataFrames after the gap-fill prologue — all preserved now that the prologue runs through `run_gap_fill_with_retries`.
- `automation.main.main()` catches `GapFillFailedError` raised from `_ib_source.start()`, prints the actionable recovery message, and exits with code 11 (not propagating uncaught, not confused with the code-2 IB-disconnect path).
- `orchestrator.main._make_ib_health_check()`'s returned `check()` raises `_GracefulStop(exit_code=11)` with a distinct gap-fill-specific message (containing `"trade.py gap-fill"`, not `"maintenance break"`) when the pre-session thread died with a `GapFillFailedError`, while the routine thread-death case (any other exception) still raises `_GracefulStop()` with `exit_code == 0` and the original maintenance-break message.
- `_GracefulStop()` with no arguments still defaults to `exit_code == 0`, preserving the existing `_check_stop_requested()` call site's behavior untouched.

---

## Validation Results

| Level | Command | Status | Notes |
|-------|---------|--------|-------|
| 1 | `uv run pytest tests/test_gap_fill.py -q -k "gap_hours_by_file or gap_fill_failed_error"` (Task 1, red) | ✅ | 4 failed as expected (ImportError) before implementation |
| 2 | `uv run pytest tests/test_gap_fill.py -q -k "gap_hours_by_file or gap_fill_failed_error or run_gap_fill_with_retries"` (Task 1+2, green) | ✅ | 9/9 passed after implementation |
| 3 | `uv run pytest tests/test_gap_fill.py -q` (Task 3) | ✅ | 15/15 passed (6 original + 9 new) |
| 4 | `uv run pytest tests/test_ib_realtime.py -q -k "..."` (Task 4, pre-wiring sanity) | ✅ | 4/4 passed before `start()` wiring change |
| 5 | `uv run pytest tests/test_ib_realtime.py -q` (Task 4, full) | ✅ | 45/45 passed |
| 6 | `uv run pytest tests/test_automation_main.py -q -k test_gap_fill_failed_exits_11` (Task 5, red→green) | ✅ | Failed uncaught before the except clause; passed after |
| 7 | `uv run pytest tests/test_automation_main.py -q` (Task 5, full) | ✅ | 25/25 passed |
| 8 | `uv run pytest tests/test_orchestrator_main.py -q -k "..."` (Task 6, red→green) | ✅ | 3 failed (`AttributeError: no exit_code`) before; 3/3 passed after |
| 9 | `uv run pytest tests/test_orchestrator_main.py -q` (Task 6, full) | ✅ | 27/27 passed |
| 10 | `uv run pytest tests/ -q` (Task 7, full regression) | ✅ | 1386 passed / 2 failed (pre-existing, unrelated) / 16 errors (pre-existing, unrelated); 0 new failures vs. baseline |

---

## Challenges & Resolutions

**Challenge 1: Plan-cited absolute line numbers went stale mid-execution**
- **Issue:** Task 4/5/6 plan text cites specific line numbers (e.g. `data/ib_realtime.py:779`) that no longer matched after Task 1+2's insertions shifted the file.
- **Root Cause:** Expected consequence of sequential single-file edits within one plan run (see Divergence #1).
- **Resolution:** Re-located every target via `grep -n` on function/test names before each edit, then matched the plan's literal code content at the new location — zero-risk since the plan supplies exact code text, not just line numbers.
- **Time Lost:** Negligible (a few extra `grep` calls).
- **Prevention:** Not needed — this is standard practice for sequential plan execution; the plan's code blocks are the source of truth, not the line numbers.

**Challenge 2: A previously-dispatched background subagent returned without doing any work**
- **Issue:** An initial attempt to delegate this plan's execution to a background `plan-executor` subagent returned "completed" after only 26 seconds / 2 tool calls, with a result message that was effectively an echo of the dispatch prompt. `git status` afterward showed zero implementation changes.
- **Root Cause:** Unclear (subagent-side failure, not diagnosable from the parent's context) — possibly the subagent misinterpreted its task as a status update rather than a full autonomous execution.
- **Resolution:** Abandoned that background delegation and implemented the plan directly, task-by-task, in the current context — the approach reflected in this report.
- **Time Lost:** ~1 minute (dispatch + notification round-trip).
- **Prevention:** For future large autonomous plan executions, verify early (e.g., `git status` after a short wait) that a dispatched background agent has actually started producing file changes before trusting a "completed" notification at face value.

---

## Files Modified

**Implementation (4 files):**
- `data/ib_realtime.py` — added `gap_hours_by_file()`, `GapFillFailedError`, `run_gap_fill_with_retries()`; wired `start()` to use the retry loop (+113/-1)
- `gap_fill.py` — wired `gap_fill_until_now()` to use the retry loop (+2/-1)
- `automation/main.py` — import `GapFillFailedError`; catch it around `_ib_source.start()`, exit 11 (+10/-1)
- `orchestrator/main.py` — import `GapFillFailedError`; `_GracefulStop(exit_code)`; `_make_ib_health_check()` special-case; `except _GracefulStop as _stop: sys.exit(_stop.exit_code)` (+22/-1)

**Tests (4 files):**
- `tests/test_gap_fill.py` — 2 existing tests updated, 9 new tests appended (+176/-6)
- `tests/test_ib_realtime.py` — 4 existing tests updated to patch the new retry-loop indirection (+8/-0)
- `tests/test_automation_main.py` — 1 new test appended (+27/-0)
- `tests/test_orchestrator_main.py` — 1 existing test updated, 2 new tests appended (+28/-1)

**Total:** +380 insertions(-), -9 deletions (per `git diff --shortstat HEAD`)

---

## Success Criteria Met

- [x] `gap_hours_by_file()` and `GapFillFailedError` added to `data/ib_realtime.py`
- [x] `run_gap_fill_with_retries()` added, shared by both call sites
- [x] `gap_fill.gap_fill_until_now()` routes through the retry loop
- [x] `IbRealtimeSource.start()` routes through the retry loop; single-pass `gap_fill()` primitive unmodified
- [x] `automation/main.py` catches `GapFillFailedError` → exit 11
- [x] `orchestrator/main.py` `_GracefulStop(exit_code)` + `_make_ib_health_check()` special case → exit 11 via the existing `_GracefulStop` mechanism
- [x] Full regression pass: no new failures vs. baseline
- [ ] Manual live-IB-Gateway sanity check (plan Task 7 Step 2) — explicitly optional/deferred per the plan; not attempted (live trading process may be running on this machine)

Full detail in `.agents/acceptance-validations/gap-fill-retry-loop-validation.md`: 9/9 derived acceptance criteria PASS, overall verdict ACCEPTED.

---

## Recommendations for Future

**Plan Improvements:**
- None needed — this plan was unusually precise (exact code blocks, exact test code, exact line-number-based navigation aids) and executed with zero ambiguity or rework.

**Process Improvements:**
- When delegating full autonomous plan execution to a background subagent, verify with a quick `git status` check shortly after dispatch that real file changes are underway before trusting an early "completed" notification (see Challenge 2).

**CLAUDE.md Updates:**
- None needed.

**Follow-up (non-blocking, out of this plan's scope):**
- `orchestrator/process.py`'s `ProcessManager` (which supervises the `automation.main` subprocess during a live session) does not special-case the new exit code 11 — a `GapFillFailedError` exit from `automation.main` currently falls into the generic "unexpected exit → restart once, then wait for session end" path rather than getting a dedicated, clearly-logged "gap-fill failed" branch the way exit code 2 does. This is safe (no crash, no orphaned subprocess) but could be given the same first-class treatment as `IbGatewayDisconnectedError` in a future small follow-up if desired.

---

## Conclusion

**Overall Assessment:** All 7 tasks implemented exactly as specified in the plan, with all 12 new tests and 6 updated tests passing, zero regressions introduced across the full 1400+ test repository, and all 9 derived acceptance criteria passing. The one environmental divergence (line-number drift from sequential edits) was fully expected and handled without any risk to correctness.
**Alignment Score:** 10/10 — implementation matches the plan's literal code blocks verbatim at every step.
**Ready for Production:** Yes, pending the plan's own explicitly-optional manual live-IB-Gateway sanity check, which is a deployment-verification step rather than a code-completeness gate.

All changes remain **UNSTAGED** — nothing was committed or pushed, per explicit instruction.
