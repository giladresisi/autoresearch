# Code Review: IB Gap-Fill + Orchestrator Disconnect Handling

**Date**: 2026-05-07
**Branch**: live
**Reviewer**: Claude (ai-dev-env:code-review)

---

## Stats

- Files Modified: 5 (`data/ib_realtime.py`, `automation/main.py`, `orchestrator/process.py`, `orchestrator/main.py`, plus `PROGRESS.md`)
- Files Added: 2 (`data/databento_backfill.py`, `tests/test_databento_backfill.py`)
- Files Updated (tests): 2 (`tests/test_ib_realtime.py`, `tests/test_orchestrator_process.py`, `tests/test_orchestrator_main.py`)
- New lines: ~386 across all files
- Test suite: 48 passed, 1 skipped (integration, expected)

---

## Focused Questions from Prompt

### 1. IbGatewayDisconnectedError propagation — is it truly not retried?

**Yes, confirmed correct.** The exception is raised inside the `try` block, immediately caught by `except IbGatewayDisconnectedError: raise`, which re-raises it before the outer `except Exception` retry handler can execute. The error propagates directly out of `start()`. Test `test_ibgateway_disconnected_error_not_retried` verifies `connect()` is called exactly once with `max_retries=3`.

### 2. `_stopping` flag thread safety — race between `stop()` and the disconnect callback?

**No race condition in the current usage.** `IbRealtimeSource.stop()` is never called while `util.run()` is active. In `automation/main.py`, the `finally` block only calls `_executor.stop()`, not `_ib_source.stop()`. When `stop()` is called (from other callers), `util.run()` has already returned. Additionally, `ib_insync` uses a single-threaded asyncio event loop; `disconnectedEvent` fires synchronously via `IB.disconnect()` in the same thread where `_stopping` was set. The guard is safe.

### 3. `databento_backfill.py` edge cases

See findings below — one medium issue with timezone precision and one low-severity issue with the `start_ts` derivation.

### 4. `orchestrator/main.py` sys.exit(3) — before or after position close?

**After — correct.** The call order in `run()` is:
1. `ProcessManager(...).run_session(today)` → returns `"ib_disconnected"`
2. `_close_session_position(orch_ch)` — position is closed here
3. `relay.write_trades_tsv(...)` — trades TSV written
4. `summarizer.run(...)` — session summarized
5. `if result == "ib_disconnected": sys.exit(3)` — exit last

`_close_session_position` is guaranteed to run before `sys.exit(3)`. The test `test_run_closes_position_before_ib_disconnect_exit` verifies this ordering explicitly.

---

## Issues Found

```
severity: medium
file: data/databento_backfill.py
line: 36
issue: start_ts.isoformat() passes an ET-offset ISO string to DatabentSource.fetch() whose internal Databento client expects UTC
detail: backfill_parquets computes start_ts and cutoff as pd.Timestamp objects with tz="America/New_York". Calling .isoformat() on them produces strings like "2026-04-07T10:00:00-04:00". DatabentSource.fetch() passes these directly to databento's client.timeseries.get_range(start=..., end=...). The Databento Python SDK accepts ISO 8601 strings with timezone offsets, so this should work at the API level. However, if Databento internally truncates to date-only or interprets the offset unexpectedly, bars near midnight ET could be missed or duplicated. This is low probability but untested and has caused issues with other financial data APIs.
suggestion: Convert to UTC before calling isoformat: start_ts.tz_convert("UTC").isoformat() and cutoff.tz_convert("UTC").isoformat(). This is unambiguous and matches what most financial data APIs prefer. Alternatively, add a test that verifies the date range passed to DatabentSource.fetch() covers the expected UTC window.
```

```
severity: medium
file: data/databento_backfill.py
line: 33
issue: start_ts derived from existing.index[-1] may include the last stored bar's timestamp as the fetch start, causing the first bar to be re-fetched or missed
detail: The line is: start_ts = max(existing.index[-1], floor). The last stored bar's timestamp is used as the fetch start. Depending on whether DatabentSource uses inclusive or exclusive range semantics for `start`, this either re-fetches the last stored bar (wasted call, deduplicated on write — harmless) or misses any bars at exactly that timestamp (if exclusive). More importantly, if the parquet ends at e.g. 16:59 ET, the fetch starts at 16:59, which may not include data at 17:00 ET. The existing pattern in _gap_fill() uses the same approach, so this is consistent. But for a new function meant to be the canonical backfill source, the semantics should be explicit.
suggestion: Use existing.index[-1] + pd.Timedelta(minutes=1) as start_ts to fetch bars strictly after the last stored bar. This avoids the ambiguity and matches typical bar data semantics (each bar's timestamp is its open time).
```

```
severity: low
file: tests/test_ib_realtime.py
line: 203-215
issue: test_stop_does_not_trigger_gateway_disconnect_flag tests a manually-written copy of the callback logic, not the actual _on_gateway_disconnect closure
detail: The test manually writes a function that mirrors the guard, then calls it and asserts the flag was not set. This does not actually exercise the closure that start() registers. If the guard (if not self._stopping) were accidentally removed from the real closure, this test would still pass. It's a tautological test.
suggestion: Exercise the actual callback by calling start() with a mock IB that fires disconnectedEvent after stop() sets _stopping=True. The test_gateway_disconnect_raises_ibgateway_disconnected_error test provides a pattern: capture callbacks via disconnect_callbacks.append, call stop(), then invoke the captured callback, and assert _disconnected_by_gateway is False.
```

```
severity: low
file: orchestrator/main.py
line: 70
issue: bar_data_dir = _Path("data") is a relative path that assumes CWD = project root
detail: _pre_session_init uses _Path("data") (relative). This is consistent with BAR_DATA_DIR = Path("data") in automation/main.py, but if the orchestrator is ever launched from a different CWD, the backfill writes to the wrong directory without error. The parquet files would exist in the wrong place and IbRealtimeSource would not load them.
suggestion: Derive the path from the module file: _Path(__file__).parent.parent / "data", which is the same pattern used for _SIGNAL_SMT and _SESSIONS_DIR in the same file. This makes the path CWD-independent.
```

```
severity: low
file: data/ib_realtime.py
line: 275-276
issue: break path after util.run() is dead code that could mislead future maintainers
detail: Lines 275-276: "if self._ib.isConnected(): break" — util.run() with no awaitables runs the event loop forever and only exits when util.stop() is called or an exception occurs. When util.stop() is called by _on_gateway_disconnect, _disconnected_by_gateway is True so line 274 raises instead. The only other normal return would require an external caller to stop the loop. The break path (and the subsequent disconnect at lines 295-299) is therefore unreachable in any real scenario. This is pre-existing code that was not introduced by this PR, but it creates a confusing code path.
suggestion: Add a comment: "# util.run() only returns via util.stop() in _on_gateway_disconnect; this path is a safety net". No code change required unless clarity is important.
```

```
severity: low
file: tests/test_orchestrator_main.py
line: 156-161
issue: test_pre_session_init_skips_when_no_api_key assertion is vacuously true
detail: When DATABENTO_API_KEY is not set, _pre_session_init() returns before the try block is entered. The patched backfill_parquets is never imported or called. mock_bp.assert_not_called() passes trivially — the mock is simply never reached. The test does verify the function doesn't raise, but the assert_not_called() check adds no value as a guard.
suggestion: The test is still useful as a no-raise regression guard. To make the assertion meaningful, also verify that DatabentSource is not instantiated: assert backfill is never imported (e.g., check that the import never fires via sys.modules). Alternatively, restructure as: "does _pre_session_init() call backfill_parquets? No. Does it raise? No." — two separate tests.
```

---

## Summary

**No critical or high-severity issues.** The implementation is correct on the specific concerns raised:

- `IbGatewayDisconnectedError` is truly not retried — propagates immediately.
- `_stopping` flag has no race condition — single-threaded event loop, `stop()` never called concurrently with `util.run()`.
- `sys.exit(3)` happens after `_close_session_position()` — correct order confirmed by code and test.
- `sys.exit(2)` in `automation/main.py` runs the `finally` block (Python guarantees this for `SystemExit`) — `_executor.stop()` executes before process exits.

The two medium-severity findings are in `databento_backfill.py`: timezone precision (passthrough of ET-offset ISO strings to Databento API) and inclusive vs. exclusive fetch range semantics. Neither will cause a production crash, but both could cause subtle bar gaps or duplicates under specific timing conditions.

The `bar_data_dir = _Path("data")` relative path (low severity) is the most likely to cause a real-world issue if the orchestrator is ever launched from a non-root CWD.
