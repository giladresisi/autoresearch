# Code Review: V2 Live Dispatcher Wiring
**Date:** 2026-05-04
**Branch:** unified-session
**Reviewer:** Claude (ai-dev-env:code-review)

---

## Stats

- Files Modified: 2 (`signal_smt.py`, `automation/main.py`)
- Files Added: 3 (`live_emit.py`, `session_pipeline.py`, `tests/test_smt_v2_dispatcher.py`)
- New lines (dispatcher-related): ~85 across the two modified files
- Deleted lines (old dispatcher body): ~160 across the two modified files

---

## Issues Found

```
severity: low
file: signal_smt.py
line: 813
issue: Module-level import placed mid-file (after state machine, before class)
detail: `from live_emit import emit_v2_signal as _emit_v2_signal` appears after ~800 lines of
  function definitions rather than at the top of the file. While PEP 8 recommends top-of-file
  imports, this is not a functional bug because the import executes before any instance of
  SmtV2Dispatcher is created. The same pattern appears at the identical location in
  automation/main.py (line 854). Functionally correct but reduces readability.
suggestion: Move both `from live_emit import ...` statements to the top-of-file import block
  alongside the other module imports. This is a cosmetic clean-up; safe to defer.
```

```
severity: low
file: signal_smt.py
line: 891
issue: Inconsistent `os` import style vs. automation/main.py
detail: signal_smt.py imports `os` as a local alias inside main() (`import os as _os`, line 891)
  while automation/main.py imports `os` at module top (line 13). Both work correctly. The
  inconsistency makes the two files harder to compare and maintain together.
suggestion: Move `import os` to the top-level import block in signal_smt.py (already done in
  automation/main.py). Drop the `_os` alias; use `os` directly.
```

```
severity: low
file: tests/test_smt_v2_dispatcher.py
line: 73-81
issue: test_on_1m_bar_before_session_start_is_noop does not test the _on_bar_1m_complete wiring
detail: The test calls dispatcher.on_1m_bar() directly. It does not test the full V2 block in
  _on_bar_1m_complete (signal_smt lines 935-942 / automation/main.py lines 982-989), which
  contains the gate logic: `if bar_time >= _SESSION_DAILY_TRIGGER_TIME: on_session_start(...)`.
  Pre-09:20 bars are correctly dropped (pipeline is None), but the wiring of the callback itself
  is only covered by the existing integration tests (test_signal_smt.py, test_automation_main.py),
  not by the new dispatcher unit tests.
suggestion: Add one test that patches _ib_source and calls the _on_bar_1m_complete closure
  directly with a pre-09:20 bar, asserting _smtv2_dispatcher._pipeline remains None. This
  tests the actual in-context wiring rather than the class in isolation. Low urgency since
  integration tests cover the code path.
```

---

## No Issues Found (confirmed correct)

**09:20 boundary detection (review focus #1):** The `>=` comparison at
`signal_smt.py:938` and `automation/main.py:985` is correct. A bar timestamped exactly at
09:20 triggers `on_session_start` first, then `on_1m_bar` processes the same bar — so the
09:20 bar is included in the first session. Verified by running the comparison against a
tz-aware Timestamp.

**Idempotency guard (review focus #1):** `SmtV2Dispatcher.on_session_start` checks
`if today == self._session_date: return` before any work. The guard compares
`now.date()` (a `datetime.date`) against `self._session_date` (also set to `now.date()`).
New-day detection fires correctly because `date` objects compare by value. Test 4
(`test_on_session_start_idempotent_same_day`) confirms single initialization per day.

**Thread safety (review focus #2):** `_on_bar_1m_complete` is called from the IB event
loop, which uses asyncio's single-threaded event loop internally. All module-level globals
(`_hypothesis_generated`, `_smtv2_dispatcher`) and dispatcher instance state (`_pipeline`,
`_session_date`) are read and written exclusively on this single thread. No concurrent
mutation is possible; no locking is needed.

**Empty DF edge cases (review focus #3):** Two guards are present:
- `_mnq_bar = _mnq_df.iloc[-1] if not _mnq_df.empty else pd.Series(dtype=float)` (both files)
- `_mes_bar = _mes_df.iloc[-1] if not _mes_df.empty else pd.Series(dtype=float)` (both files)
An empty `_mnq_df` passed to `on_session_start` flows into `SessionPipeline.__init__`
which guards via `if not self._hist_mnq_1m.empty`. No crash path exists.

**`_ib_source` None safety (review focus #3):** `_on_bar_1m_complete` is a closure
defined inside `main()` and registered with `IbRealtimeSource` only after `_ib_source` is
assigned. The closure reads `_ib_source.mnq_1m_df` at line 936/983, but `_ib_source` is
never None at that point structurally — the callback cannot fire before `_ib_source.start()`.

**Pre-09:20 bars (review focus #3):** `on_1m_bar` is called unconditionally on every
completed bar (no time gate at the call site). However `SmtV2Dispatcher.on_1m_bar` returns
immediately if `self._pipeline is None`. Since `_pipeline` can only be set by
`on_session_start`, and `on_session_start` is gated on `bar_time >= 09:20`, pre-09:20 bars
are silently dropped without side effects.

**Test coverage (review focus #4):** All 16 tests pass (8 test cases × 2 parameterised
modules). Tests confirm: lazy init, idempotency, new-day reset, pre-session no-op, pipeline
delegation, fresh-DF semantics, today-slice correctness. Confirmed pre-existing failures on
HEAD (16 failures before this changeset, 0 after) proving the tests are genuinely new.

**Code quality (review focus #5):** Class is correctly documented. The "DFs at call time,
not stored at init" design decision is explicitly stated in the docstring. The two files are
functionally identical in the dispatcher and wiring sections. The only differences are the
`os` import style and the `strategy_smt.HUMAN_EXECUTION_MODE = True` line present in
`signal_smt.main()` but absent in `automation/main.main()` — this is pre-existing and
out of scope for this changeset.

---

## Summary

The V2 dispatcher wiring is correct. Three low-severity findings, all cosmetic or test
coverage improvements. No bugs, no security issues, no logic errors, no race conditions.
The changeset is ready to commit.
