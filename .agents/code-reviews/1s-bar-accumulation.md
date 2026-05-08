# Code Review: 1s Bar Accumulation Feature

**Date:** 2026-05-08
**Reviewer:** Claude Sonnet 4.6
**Plan:** `.agents/plans/1s-bar-accumulation.md`

---

## Stats

- Files Modified: 9
- Files Added: 3 (scripts/seed_1s_parquet.py, tests/test_sources.py, tests/test_seed_1s_parquet.py)
- Files Deleted: 0
- New lines: +826
- Deleted lines: -11

---

## Test Results

All 1054 non-integration tests pass under `uv run python -m pytest tests/ --ignore=tests/test_ib_integration.py`. The 2 integration test failures (test_ib_integration.py) require a live IB Gateway and are pre-existing environmental constraints, not regressions.

Note: `tests/test_sources.py` fails when run with the system Python (`python -m pytest`) because `databento` is only installed in the project venv. It passes correctly under `uv run python -m pytest`. This is a test execution environment issue, not a code defect.

---

## Issues Found

---

```
severity: medium
file: C:\Users\gilad\projects\auto-co-trader\automation\data\ib_realtime.py
line: 200
issue: outer exception handler in _gap_fill_1s_ib() aborts all instruments on any error
detail: The outer try/except wraps both the IB connect and the per-instrument loop. If
        reqHistoricalData raises for MNQ, the except clause fires and MES is never processed.
        This mirrors the existing _gap_fill() pattern, so it is consistent with the codebase
        convention, but it means a transient IB error on the first instrument silently skips
        the second. The merge_session_1s_parquets() function uses per-instrument try/except
        inside the loop, which is safer.
suggestion: Wrap the per-instrument reqHistoricalData call in its own try/except (inside the
            instrument loop) rather than relying on the outer handler, matching the pattern
            used in merge_session_1s_parquets(). Low urgency given the existing _gap_fill()
            uses the same outer-catch approach and this is startup-only code.
```

```
severity: low
file: C:\Users\gilad\projects\auto-co-trader\automation\scripts\seed_1s_parquet.py
line: 35
issue: DatabentSource() instantiation precedes --dry-run check, requiring DATABENTO_API_KEY even for dry runs
detail: main() calls source = DatabentSource() before checking args.dry_run. If
        DATABENTO_API_KEY is not set, the script raises RuntimeError even with --dry-run,
        making it difficult to test the script's range-printing logic without a real API key.
        The test test_seed_dry_run_prints_without_writing works around this by setting a
        fake key via patch.dict, and test_seed_script_is_runnable passes DATABENTO_API_KEY=test-key
        in the env. But the CLI UX is unexpected for users who run --dry-run to preview.
suggestion: Move source = DatabentSource() inside the non-dry-run branch or defer it after
            the dry-run gate. For --dry-run, instantiation can be skipped entirely since
            fetch() is never called.
```

```
severity: low
file: C:\Users\gilad\projects\auto-co-trader\automation\orchestrator\main.py
line: 186-190
issue: thread.join(timeout=15.0) may time out during _gap_fill_1s_ib(), leaving the IB connection open
detail: _stop_pre_session_ib() joins the pre-session thread with a 15-second timeout. The
        _gap_fill_1s_ib() call inside start() can take significantly longer than 15 seconds
        when filling a large gap (e.g., filling 1h of 1s data requires ~2 IB calls at 1800s
        chunks; each IB request may itself take several seconds). When the join times out,
        the function returns and the daemon thread continues running in the background.
        The thread holds an IB connection at client_id+1=11. The subsequent session subprocess
        uses client_id=20 (no conflict), and merge_session_1s_parquets uses client_id=16
        (no conflict), so there is no functional breakage. But the dangling connection may
        cause IB Gateway to log spurious disconnect errors when the daemon thread is eventually
        collected at process exit.
suggestion: This is acceptable given the daemon-thread design. Document the timeout as
            best-effort in the docstring. If tighter cleanup is needed, add a _stopping check
            inside _gap_fill_1s_ib() at the top of each chunk iteration so the loop exits
            early when stop() is called.
```

```
severity: low
file: C:\Users\gilad\projects\auto-co-trader\automation\data\databento_backfill.py
line: 84
issue: backfill_1s_parquets() always calls Databento even when the parquet is already current
detail: Unlike backfill_parquets() which has an early-continue guard ('if start_ts >= cutoff:
        continue'), backfill_1s_parquets() makes a Databento API call even when the last bar
        in the parquet is only seconds old. On a healthy system after a normal session, the
        parquet will be nearly current and Databento will return an empty response, but the
        API call still consumes quota and adds latency on every orchestrator startup.
suggestion: Add a short-circuit analogous to backfill_parquets(): if last_bar is within
            N minutes of now (e.g., 5 minutes), skip the fetch. This is consistent with the
            design intent that _gap_fill_1s_ib() covers the last 2 minutes anyway.
            Note: the plan explicitly says 'no cutoff' meaning no fixed historical cutoff,
            not necessarily that every startup must always hit the API. The distinction is
            between 'end=now (not end=2days_ago)' and 'skip if already fresh'.
```

---

## Verified Correct (Focus Areas)

**Thread safety in _start_pre_session_ib / _stop_pre_session_ib:**
IbRealtimeSource is constructed in the caller thread. source.start() runs in the daemon thread. stop() correctly guards on None for both _event_loop and _ib. An immediate stop() before connect() is safe. The (source, thread) tuple is returned to the caller and used as a pair — no shared mutable state between threads other than the IbRealtimeSource internals, which are designed for this use.

**IB connection lifecycle in _gap_fill_1s_ib() and merge_session_1s_parquets():**
Both use try/finally for disconnect. _gap_fill_1s_ib: if connect raises, finally checks ib.isConnected() (False) and skips disconnect. merge_session_1s_parquets: uses ib_ok flag — only disconnects if connect succeeded. Both patterns are correct.

**Session parquet vs main parquet write isolation:**
_on_mnq_1m_bar and _on_mes_1m_bar write pending 1s bars to MNQ_1s_session_YYYYMMDD.parquet / MES_1s_session_YYYYMMDD.parquet. MNQ_1s.parquet and MES_1s.parquet are never written during the live session. Confirmed by tests test_on_mnq_1m_bar_flushes_1s_pending_to_session_parquet and test_on_mes_1m_bar_flushes_1s_pending_to_session_parquet which assert absence of the main parquet file.

**Gap duration calculation in merge_session_1s_parquets:**
gap_s = max(0, int((gap_end - gap_start).total_seconds()) - 1). For a 120-second gap (09:18 -> 09:20), this yields 119, which is confirmed by test_merge_session_gap_fill_called_with_correct_duration asserting durationStr="119 S". The -1 correctly excludes the gap_end bar (which is the first session bar, already present). The max(0, ...) prevents negative values. The if gap_s > 1 guard skips the IB call for negligible gaps.

**backfill_1s_parquets() has no artificial cutoff:**
end=now.tz_convert("UTC").isoformat() is passed directly. Confirmed by test_backfill_1s_no_cutoff_calls_with_end_near_now asserting the end argument is within 60 seconds of pd.Timestamp.now(tz="UTC").

**_gap_fill_1s_ib() skips when parquets are empty:**
needs_fill = any(not df.empty and ...) correctly returns False when both DFs are empty (empty df is falsy in the not df.empty check), so no IB connection is opened. Confirmed by test_gap_fill_1s_ib_skips_when_empty_parquet.

**merge_session_1s_parquets() crash-safe / IB failure non-fatal:**
IB connect failure sets ib_ok=False, prints a warning, and continues to the merge loop. The merge proceeds without gap fill. The finally block is guarded by ib_ok so no disconnect is attempted on a failed connection. Confirmed by test_merge_session_noop_when_no_session_files (no IB opened when no session files) and test_merge_session_integrates_into_main (merge completes with mock IB returning empty bars).

**No security vulnerabilities:**
No shell injection (no subprocess calls with user-controlled strings). No hardcoded credentials (conId defaults are contract IDs, not secrets, and match the pre-existing pattern in tests). No SQL. The IB connection parameters (host, port) are read from environment variables with safe defaults.

---

## Summary

The implementation is functionally correct across all reviewed focus areas. The four issues identified are all low-to-medium severity and involve UX behavior (dry-run API key requirement), robustness at the edges (outer exception catching scope, join timeout), and minor efficiency (redundant API call when parquet is fresh). No bugs, no security issues, no data corruption paths were found. The session/main parquet isolation is correctly enforced and tested. The IB connection lifecycle is correct in both new functions.
