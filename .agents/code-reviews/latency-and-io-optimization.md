# Code Review: latency-and-io-optimization

**Date**: 2026-05-14
**Branch**: automation
**Reviewer**: Claude Code (ai-dev-env:code-review)

## Stats

- Files Modified: 8
- Files Added: 0
- Files Deleted: 0
- New lines: ~402
- Deleted lines: ~79

---

## Issues Found

---

### ISSUE 1

```
severity: high
file: data/ib_realtime.py
line: 151-152
issue: _gap_fill() still writes parquets synchronously — not offloaded to executor
detail: The plan mandates that ALL to_parquet() calls in IbRealtimeSource go via
        _parquet_executor. However, _gap_fill() (lines 151-152) calls
        self._mnq_1m_df.to_parquet() and self._mes_1m_df.to_parquet() directly.
        _gap_fill() is not called at runtime (start() does not call it — confirmed by
        the regression test test_gap_fill_not_called_from_start), but the method still
        exists and its writes are synchronous. If _gap_fill() is ever called (e.g. from
        a test or a future code path), it will bypass the executor and write synchronously
        on whatever thread invokes it. More critically, the acceptance criteria explicitly
        state: "_seed_from_history does NOT call DataFrame.to_parquet() directly — writes
        go via _parquet_executor.submit()" — the same requirement was also stated for the
        on_bar handlers. _gap_fill() was not listed in the plan's fix scope, but its
        synchronous writes are inconsistent with the established pattern and represent a
        latent hazard if the method is ever re-activated.
suggestion: Either offload the two writes in _gap_fill() to self._parquet_executor.submit()
            using the same snapshot pattern, or add a comment explicitly noting the method
            is dead code and guard it with NotImplementedError to prevent accidental invocation.
```

---

### ISSUE 2

```
severity: high
file: data/ib_realtime.py
line: 226
issue: _gap_fill_1s_ib() writes parquets synchronously — not offloaded to executor
detail: Line 226 calls combined.to_parquet(self._bar_data_dir / parquet_name) inside the
        _gap_fill_1s_ib() loop. This method IS called from start() (line 453), running on
        the same thread that will later enter the IB event loop. While _gap_fill_1s_ib()
        runs before the event loop starts (so the timing concern is different), this write
        is still on the caller thread and races with any future executor writes to the same
        parquet file (MNQ_1s.parquet / MES_1s.parquet). The single-worker executor
        guarantees serial writes only for writes submitted to it; this direct call is
        outside that guarantee. If a session parquet write and this 1s write were ever to
        overlap (e.g. on a second call path), data could be corrupted.
suggestion: Offload this write to self._parquet_executor.submit() using the snapshot
            pattern: _snap = combined; self._parquet_executor.submit(_snap.to_parquet,
            self._bar_data_dir / parquet_name).
```

---

### ISSUE 3

```
severity: medium
file: data/ib_realtime.py
line: 301-302
issue: Snapshot in _seed_from_history references the same object as self._mnq_1m_df — no actual snapshot
detail: After the concat+dedup (line 299-300), self._mnq_1m_df is assigned 'combined'.
        Line 301 then does: _snap = self._mnq_1m_df. Since self._mnq_1m_df IS combined at
        this point, _snap and self._mnq_1m_df reference the same object. The comment in
        the on_bar handlers explains the snapshot pattern: "capture reference before
        possible trim". In _seed_from_history there is no trim after submission — the next
        mutation to self._mnq_1m_df can only happen if _seed_from_history is called again
        or _on_mnq_1m_bar fires. In those cases _snap still holds the correct object
        because Python assignment creates a new binding for self._mnq_1m_df, not an
        in-place mutation of the DataFrame.

        The critical issue is that _snap IS the same object as combined/self._mnq_1m_df.
        When the background thread writes it, the main thread could concat more rows into
        a NEW df assigned to self._mnq_1m_df — that part is safe (new assignment). BUT if
        the main event loop calls pd.concat([self._mnq_1m_df, ...]) on the same object
        that the background thread is writing (e.g. via iterrows under the hood), there is
        a potential read-write race on the DataFrame object.

        In practice, the next _seed_from_history call creates a brand-new DataFrame via
        pd.concat and assigns it to self._mnq_1m_df, so the previous object (_snap) is
        stable for the executor. The risk is low but the code is misleading — the
        variable name _snap implies a deep copy but is actually just an alias.
suggestion: Add a .copy() to make the intent and safety guarantee explicit:
            _snap = self._mnq_1m_df.copy()
            This matches the pattern used in _on_mnq_1m_bar where the snapshot captures
            the pre-trim state and the object is later reassigned (not mutated in place).
            The .copy() cost is negligible compared to the parquet write itself.
```

---

### ISSUE 4

```
severity: medium
file: data/ib_realtime.py
line: 591-592
issue: gap_fill_1m_ib() (module-level function) writes parquets synchronously outside executor context
detail: The standalone gap_fill_1m_ib() function at line 591 calls
        combined.to_parquet(bar_data_dir / fname) synchronously. This function is not a
        method of IbRealtimeSource and has no access to _parquet_executor — that is by
        design. However, this function and IbRealtimeSource both write to the same files
        (MNQ_1m.parquet, MES_1m.parquet). If gap_fill_1m_ib() and a live IbRealtimeSource
        session happen to run concurrently (e.g. orchestrator restart scenario), the
        standalone function's synchronous write would race with the executor-serialized
        writes from the running IbRealtimeSource.
        This is a pre-existing architectural concern (gap_fill_1m_ib is called at
        orchestrator startup before IbRealtimeSource.start()), but the new executor
        pattern makes this implicit ordering assumption more fragile.
suggestion: Add a comment to gap_fill_1m_ib() explicitly documenting that it must only
            be called before IbRealtimeSource.start() to avoid file contention. No code
            change strictly required since the orchestrator already serializes this.
```

---

### ISSUE 5

```
severity: medium
file: tests/test_ib_realtime.py
line: 766-767
issue: test_set_bar_data_no_inline_import uses fragile file path construction
detail: Line 766 builds source_path by replacing a hardcoded path fragment:
            source_path = __file__.replace("tests/test_ib_realtime.py", "data/ib_realtime.py")
        This is immediately overwritten by line 768's os.path.join approach, making line 766
        dead code. The os.path.join approach on line 768 is correct but relies on
        os.path.dirname(os.path.dirname(__file__)) navigating up two levels from the test
        file's location. On Windows with absolute paths this is generally correct, but the
        initial string replacement (line 766) uses forward slashes which would silently
        fail on Windows if __file__ uses backslashes — the line is dead code anyway so it
        does not cause a failure, but it adds confusion.
suggestion: Remove line 766 entirely (it is shadowed by line 768 immediately). Keep only
            the os.path.join approach which is cross-platform and correct.
```

---

### ISSUE 6

```
severity: low
file: smt_state.py
line: 65-66
issue: set_in_memory_mode(False) clears _STORE, which erases any in-flight state
detail: When set_in_memory_mode is called with enabled=False (disabling in-memory mode),
        line 66 calls _STORE.clear(). This is intentional per the plan ("toggling
        in-memory mode doesn't leave stale cache entries"). However, if a test or caller
        does: set_in_memory_mode(True), writes some state, then set_in_memory_mode(False),
        all written state is silently discarded. The behavior is correct for test isolation
        but could be surprising in production code. No fix required — just note that the
        clearing happens only when transitioning TO disk mode (enabled=False), not when
        transitioning TO in-memory mode.
        Additionally: _STORE.clear() is only called when enabled=False, but when
        enabled=True the old _STORE contents are NOT cleared. Enabling in-memory mode
        after some disk-mode operations does not clean up stale _STORE entries from a
        previous in-memory session. The _isolate fixture in tests handles this via
        monkeypatch, so tests are clean, but a production toggle sequence of:
        in-memory → disk → in-memory would inherit stale _STORE entries.
suggestion: Move _STORE.clear() outside the if-block so it always runs on any mode
            transition:
                _STORE.clear()
                if not enabled:
                    pass  # nothing extra needed
            Or add _STORE.clear() to the enabled=True branch as well.
```

---

### ISSUE 7

```
severity: low
file: tests/test_ib_realtime.py
line: 745
issue: Timestamp string uses % formatting that produces invalid time strings for i>=6
detail: Line 745:
            bars = [_make_bar_mock("2026-05-08 09:3%d:00" % i) for i in range(5)]
        For i=0..4 this produces 09:30:00, 09:31:00, 09:32:00, 09:33:00, 09:34:00 — valid.
        For i>=5 it would produce 09:35:00+ — still valid for range(5). But if this test
        is ever extended to range(6) or more, i=6 yields "09:36:00", i=7 "09:37:00", i=10
        "09:310:00" which is an invalid timestamp and would crash _make_bar_mock. The
        current range(5) is safe, but the format string is misleading and fragile.
suggestion: Use f-strings with zero-padded minutes:
            bars = [_make_bar_mock(f"2026-05-08 09:{30+i:02d}:00") for i in range(5)]
            Same fix applies to line 755:
            bars = [_make_bar_mock(f"2026-05-08 09:{30+i:02d}:00") for i in range(3)]
```

---

### ISSUE 8

```
severity: low
file: tests/test_ib_realtime.py
line: 697-701
issue: test_parquet_write_submitted_to_executor_not_blocking does not verify the write is non-blocking
detail: The test asserts that _parquet_executor.submit.called is True and that the
        callable_arg is not None. However, it does NOT verify that to_parquet() was NOT
        called directly on the calling thread. A future regression where both
        executor.submit() AND a direct to_parquet() call are made would pass this test.
        The test also does not verify the submitted callable is a bound DataFrame.to_parquet
        method — callable_arg being not None is a very weak assertion.
suggestion: Add a patch on DataFrame.to_parquet to assert it was NOT called directly:
            with patch.object(pd.DataFrame, "to_parquet") as mock_direct_write:
                src._on_mnq_1m_bar([bar], True)
            mock_direct_write.assert_not_called()
            This would catch any direct synchronous write that bypasses the executor.
```

---

## Summary

The six performance fixes are correctly implemented with no logic errors in the core paths.
The plan's snapshot-before-trim pattern (Fix 1) is correctly applied in the on_bar handlers.
The hypothesis cache (Fix 4) correctly guards _IN_MEMORY mode and position reads.
The seed dedup (Fix 5) correctly skips redundant work.

**Critical gaps:**
- `_gap_fill_1s_ib()` (called from `start()`) and `_gap_fill()` (currently dead code) both
  contain synchronous `to_parquet()` calls that were not migrated to the executor. The plan
  specified Steps 1-6 for Fix 1 and Step 5 explicitly covered `_seed_from_history` — but
  `_gap_fill_1s_ib()` and `_gap_fill()` were not addressed. `_gap_fill_1s_ib()` is the more
  urgent of the two as it runs on the startup path before the event loop but writes to the
  same files as the executor-managed writes. While there is no immediate concurrency hazard
  (it runs before subscriptions start), the omission is inconsistent with the stated goal of
  offloading all parquet writes.

- The snapshot variable in `_seed_from_history` is a misleading alias, not a true snapshot.
  It works correctly due to Python's reference semantics but should be clarified.
