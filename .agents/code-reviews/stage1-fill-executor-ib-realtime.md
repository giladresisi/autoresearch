# Code Review: Stage 1 — FillExecutor Protocol + IbRealtimeSource Refactor

**Date:** 2026-04-30
**Branch:** automation
**Reviewer:** Claude Sonnet 4.6

---

## Stats

- Files Modified: 3 (backtest_smt.py, signal_smt.py, tests/test_smt_humanize.py)
- Files Added: 6 (execution/__init__.py, execution/protocol.py, execution/simulated.py, data/ib_realtime.py, tests/test_fill_executor.py, tests/test_ib_realtime.py)
- Files Deleted: 0
- New lines: ~694 (new files) + 247 (modified files)
- Deleted lines: 628 (modified files)

**Test baseline:** 22 pre-existing failures (unchanged), 885 passed, 11 skipped (excluding IB connection tests). All 35 new/modified tests pass.

---

## Issues Found

---

```
severity: high
file: signal_smt.py
line: 748–758
issue: partial_exit not handled in _process_managing — position hangs until session end
detail: manage_position() returns "partial_exit" when PARTIAL_EXIT_ENABLED=True (the default,
        with TRAIL_AFTER_TP_PTS=25.0 also the default). _process_managing routes that result
        through the else branch (line 756) which silently returns without closing the position
        and without persisting updated stop/tp to POSITION_FILE. The position continues
        accumulating the reduced contract count implicitly (manage_position already mutated
        position["partial_done"]=True and position["partial_price"]=lvl) but no partial exit
        fill is placed, no log line is printed, and POSITION_FILE is not updated. Under default
        production config this will silently swallow every partial-exit bar.
suggestion: Add a "partial_exit" branch before the else clause that calls
        _executor.place_exit(_position, "partial_exit", bar_row), prints a partial-exit log
        line, updates POSITION_FILE, and continues (does not reset to SCANNING). Mirror the
        contract-reduction logic from backtest_smt.py lines 678–716.
```

---

```
severity: medium
file: execution/simulated.py
line: 86
issue: partial_exit fill falls back to bar.Close when partial_price is 0 (falsy)
detail: `float(position.get("partial_price") or bar.Close)` uses a truthiness check.
        partial_price is always set to the partial_exit_level in strategy_smt (an entry-scaled
        price well above zero in practice), but if partial_exit_level were ever 0.0 the fill
        would silently use bar.Close instead. The correct guard is `is not None`.
suggestion: Change line 86 to:
        partial_price = position.get("partial_price")
        fill_price = float(partial_price if partial_price is not None else bar.Close)
```

---

```
severity: medium
file: data/ib_realtime.py
lines: 142-143, 160-161
issue: IbRealtimeSource couples to strategy_smt module globals via set_bar_data calls
detail: _on_mnq_1m_bar and _on_mes_1m_bar both call `from strategy_smt import set_bar_data`
        and invoke it with the updated DataFrames. This creates a hidden coupling: importing
        IbRealtimeSource in a context where strategy_smt's globals should not be updated (e.g.
        a unit test, a future Tradovate adapter) silently mutates strategy_smt state on every
        completed bar. The refactor's stated goal is that IbRealtimeSource is "standalone,
        reusable, importable without triggering an IB connection" — the set_bar_data call
        breaks the second part of that contract.
suggestion: Add an optional `on_bar_data_updated: Callable[[pd.DataFrame, pd.DataFrame], None] | None`
        constructor parameter. Remove the hard-coded set_bar_data calls. In signal_smt.main(),
        pass `on_bar_data_updated=set_bar_data` so the coupling is explicit and opt-in.
        test_ib_realtime.py does not exercise these paths so no test changes are needed.
```

---

```
severity: medium
file: data/ib_realtime.py
line: 215-218
issue: IB retry loop breaks on util.run() returning normally, but util.run() blocks until
       the event loop stops — a normal return means the IB connection was closed, not that
       it succeeded
detail: The logic is: call util.run() (which blocks), then check isConnected(). If
        isConnected() is True, break — this never fires because util.run() only returns
        after the event loop has stopped (i.e., the IB session has ended). The effective
        behavior is: util.run() exits → isConnected() is False → raise ConnectionError →
        caught by except → retry. The break is dead code and the retry loop always exhausts
        all max_retries before raising RuntimeError, even on a successful session that ends
        naturally (e.g. IB gateway daily restart). This matches the original signal_smt.py
        behavior so it is not a regression, but the dead-code path is worth flagging.
suggestion: Remove the `if self._ib.isConnected(): break` check and the
        `raise ConnectionError(...)` line. After util.run() returns normally, fall through
        to the disconnect block and exit. Only retry on exception.
```

---

```
severity: low
file: data/ib_realtime.py
line: 220
issue: print() used in production connection-retry path
detail: The standards doc states "Production code is silent: No print/stdout logging in
        production paths." The connection-retry error message on line 220 uses print().
suggestion: Replace with a logging.warning() call using the standard logging module, or
        suppress the message and let the RuntimeError at exhaustion carry the details.
```

---

```
severity: low
file: backtest_smt.py
line: 632-638
issue: _BarRow constructed without Volume argument in the main per-bar loop
detail: The in-loop bar at line 632-638 constructs _BarRow(Open, High, Low, Close, ts)
        with ts as the 5th positional argument. _BarRow.__init__ signature is
        (Open, High, Low, Close, Volume=0.0, ts=None), so ts is silently assigned to Volume
        and name is left as None. This means bar.Volume = a Timestamp object and bar.name = None
        for every bar processed in the main backtest loop. Volume is not used in manage_position
        so there is no numeric error, but bar.name=None means executor.place_exit produces
        session_date="" for all in-loop exits (session_close and end_of_backtest already use
        the separately-built _last_bar_row which includes Volume and name correctly).
        FillRecord.session_date will be "" for all non-boundary exits in the backtest.
suggestion: Add _mnq_vols[bar_idx] as the Volume argument:
        bar = _BarRow(
            float(_mnq_opens[bar_idx]),
            float(_mnq_highs[bar_idx]),
            float(_mnq_lows[bar_idx]),
            float(_mnq_closes[bar_idx]),
            float(_mnq_vols[bar_idx]),
            ts,
        )
```

---

```
severity: low
file: tests/test_ib_realtime.py
line: 79-94
issue: test_gap_fill_skipped_if_fresh_parquet patches the instance method with itself —
       the assertion proves nothing
detail: The test does `src._gap_fill = mock_gap` then calls `src._gap_fill()` then asserts
        `mock_gap.assert_called_once()`. Because src._gap_fill was replaced with mock_gap
        before the call, calling src._gap_fill() simply calls mock_gap() directly — the real
        _gap_fill never runs and the GAP_FILL_MAX_DAYS/MAX_LOOKBACK_DAYS boundary logic is
        never exercised. The test name implies it checks gap-fill behaviour but it actually
        only confirms that Python function assignment works.
suggestion: Use `patch.object(src, '_gap_fill', wraps=src._gap_fill)` to intercept the
        real call, or redesign the test to mock IBGatewaySource.fetch and assert on the
        start_ts argument to verify the 14-day vs 30-day boundary selection.
```

---

## Focused Review: Requested Questions

**1. Does SimulatedFillExecutor.place_exit() handle all exit types that signal_smt._process_managing() can produce?**

No — `partial_exit` is missing. `manage_position()` can return `"partial_exit"` when `PARTIAL_EXIT_ENABLED=True` (the default). `_process_managing()` does not handle this case: it falls through to the `else: return` branch at line 756, silently discarding the partial exit. The executor's `place_exit()` does support `"partial_exit"` correctly (line 85-87), but it is never called from `_process_managing`. This is the **high-severity** issue above.

**2. Does the _BarRow passed to place_exit() in signal_smt.py have the right fields?**

Yes. The `bar_row` built at lines 694-697 of `signal_smt._process_managing` includes all six fields: Open, High, Low, Close, Volume, and name (as `bar_ts`). The executor only accesses `bar.High`, `bar.Low`, `bar.Close`, and `bar.name` — all present. No missing-field issues.

**3. Any _mnq_1m_df / _mes_1m_df references remaining in signal_smt.py that should use _ib_source?**

No direct bare module-level `_mnq_1m_df`/`_mes_1m_df` globals remain. Lines 367-368 in `_process_scanning` assign local variables `_mnq_1m_df = _ib_source.mnq_1m_df if _ib_source is not None else None` which correctly delegates to `_ib_source`. The `SmtV2Dispatcher` class at lines 819-820 holds its own local copies passed via constructor — also correct. No stale globals.

**4. Does _on_bar in signal_smt correctly pass mes_partial to _process_managing() / _process_scanning()?**

Partially correct. `_on_bar` (lines 129-137) receives `mes_partial` as its second argument, stores it in the module-level `_mes_partial_1m`, then calls `_process(bar)`. `_process_scanning` reads `_mes_partial_1m` directly from module scope (lines 359-361, 554-556) — correct. `_process_managing` does not read `_mes_partial_1m` at all, which is correct since manage_position only needs the MNQ bar. The callback signature matches what `IbRealtimeSource._on_mnq_tick` provides at line 182.

---

## Summary

One **high** severity bug: `partial_exit` is unhandled in `signal_smt._process_managing()`, causing silent position mismanagement under the default `PARTIAL_EXIT_ENABLED=True` config. One **medium** severity falsy-zero bug in simulated.py partial_price guard. Two additional medium issues: tight coupling of IbRealtimeSource to strategy_smt globals, and a dead-code branch in the IB retry loop. Two low-severity issues: a missing Volume argument when constructing _BarRow in backtest_smt's main loop, and a tautological unit test.
