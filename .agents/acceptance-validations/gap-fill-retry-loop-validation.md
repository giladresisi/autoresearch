## Acceptance Criteria Validation Report

**Feature / Request:** Gap-fill retry loop — unify production and offline IB historical-data gap-fill behavior
**Plan File:** .agents/plans/gap-fill-retry-loop.md
**Criteria Source:** Derived from plan's Goal / Architecture / Global Constraints sections (no explicit "ACCEPTANCE CRITERIA" heading present in the plan)
**Validated:** 2026-07-11

---

### Results

| # | Criterion | Verdict | Evidence |
|---|-----------|---------|----------|
| 1 | `gap_hours_by_file(bar_data_dir) -> dict[str, float]` exists in `data/ib_realtime.py`, returns hours-since-last-bar per file, `inf` for missing/empty/unreadable | PASS | `data/ib_realtime.py:75-93`; verified via direct import (`inspect.signature` matches plan); 3 dedicated tests pass (`test_gap_hours_by_file_all_current`, `_missing_file_is_inf`, `_empty_df_is_inf`) |
| 2 | `GapFillFailedError(gaps_hours, last_error)` exists with `.gaps_hours`/`.last_error` attributes and an informative message | PASS | `data/ib_realtime.py:96-104`; `test_gap_fill_failed_error_carries_gaps_and_last_error` passes; verified attrs directly via Python import |
| 3 | `run_gap_fill_with_retries()` loops `do_one_round`, spaced `round_spacing_s` apart, prints headsup for gaps >24h, converges when all gaps ≤1h, raises `GapFillFailedError` after 5 consecutive round exceptions (counter resets on success) | PASS | `data/ib_realtime.py:107-170`; 5 dedicated tests pass covering single-round close, multi-round convergence + headsup message + sleep spacing, no-headsup-for-small-gap, failure-counter-reset, and raise-after-5-consecutive-failures |
| 4 | `gap_fill.gap_fill_until_now()` routes through `run_gap_fill_with_retries` instead of calling `source.gap_fill()` directly; public signature unchanged | PASS | `gap_fill.py:90-91`; `trade.py`'s CLI handler untouched (no diff to trade.py); both updated tests (`runs_reachable_merge_and_source`, `honours_skip_flags`) assert `run_gap_fill_with_retries` is invoked with `(source.gap_fill, bar_data_dir)` |
| 5 | `IbRealtimeSource.start()` routes through `run_gap_fill_with_retries` instead of calling `self.gap_fill()` directly; `start()` public signature unchanged; `gap_fill()` single-pass primitive itself unmodified | PASS | `data/ib_realtime.py:890` now calls `run_gap_fill_with_retries(self.gap_fill, self._bar_data_dir)`; `gap_fill()` method body (`data/ib_realtime.py:855-882`) confirmed byte-identical to pre-change (no diff hunk touches it); all 45 tests in `tests/test_ib_realtime.py` pass |
| 6 | `automation/main.py` imports `GapFillFailedError` and catches it around `_ib_source.start()`, printing a loud actionable message and exiting with code 11 | PASS | `automation/main.py:40` (import), `automation/main.py:1184-1193` (except clause + `sys.exit(11)`); `finally` cleanup still runs (structurally verified — except clause sits before `finally:`); `test_gap_fill_failed_exits_11` passes |
| 7 | `orchestrator/main.py`: `_GracefulStop` gains `exit_code: int = 0` (existing bare-raise call site at `_check_stop_requested()` unaffected); `_make_ib_health_check()` special-cases `GapFillFailedError` → loud message + `raise _GracefulStop(exit_code=11)`; routine thread-death case still exits 0; `except _GracefulStop as _stop: sys.exit(_stop.exit_code)` | PASS | `orchestrator/main.py:241-247` (`_GracefulStop.__init__`), `:349-366` (`check()` special case), `:592-595` (`except _GracefulStop as _stop: ... sys.exit(_stop.exit_code)`); `test_make_ib_health_check_gap_fill_failed_exits_11`, `test_graceful_stop_defaults_to_exit_code_0`, and updated `test_make_ib_health_check_raises_graceful_stop_on_thread_death` (now asserts `exit_code == 0` for the routine case) all pass |
| 8 | New exit code 11 is distinct from existing codes 2 (`automation/main.py` IB disconnect) and 10 (`orchestrator/main.py` missing parquets) | PASS | `grep` confirms exit code 11 used only at the two new sites (`automation/main.py:1193`, `orchestrator/main.py:357`); code 2 (`automation/main.py:1185`) and code 10 (`orchestrator/main.py:131`) untouched and distinct |
| 9 | Full regression: all plan-added/updated tests pass; no new failures introduced vs. baseline across the full suite | PASS | Targeted suites: `test_gap_fill.py` 15/15, `test_ib_realtime.py` 45/45, `test_automation_main.py` 25/25, `test_orchestrator_main.py` 27/27 all pass. Full suite (`pytest tests/ -q`): baseline was 3 failed/1373 passed/16 errors (pre-existing); after changes: 2 failed/1386 passed/16 errors — the same 16 pre-existing `test_smt_decouple_active.py` fixture errors, the same 2 pre-existing `test_smt_fill_plot.py` failures (unrelated to gap-fill), plus 12 new passing gap-fill tests and 1 previously-flaky test (`test_main_session_dirs_created`) that passed on this run. No new failures attributable to this change. |

---

### Summary

**PASS:** 9
**FAIL:** 0
**PARTIAL:** 0
**UNVERIFIABLE:** 0
**Total:** 9

**Overall verdict:** ACCEPTED

---

### Notes

- The plan's Task 7 Step 2 ("manually sanity-check the offline path against a live IB Gateway") is explicitly marked "optional but recommended" in the plan itself and requires a live IB Gateway connection with a real data gap — this was intentionally not attempted since a live trading process may be running on this machine and the manual step is not part of the automated acceptance surface. This does not affect the ACCEPTED verdict; it is a pre-authorized deferral per the plan's own wording.
- `orchestrator/process.py` (the `ProcessManager` that supervises the `automation.main` subprocess during a live session) does not special-case exit code 11 — a `GapFillFailedError`-triggered exit from `automation.main` will fall into the existing generic "unexpected exit → restart once, then wait for session end" path. This is outside the plan's stated scope (7 tasks; `orchestrator/process.py` is never mentioned) and the existing generic handling is safe (no crash, no orphaned subprocess), so it is not treated as a gap against these criteria — flagged here only as a follow-up observation, not a FAIL.
