# Code Review: SMT 5m Optimization Harness

**Date**: 2026-04-01  
**Plan**: `.agents/plans/smt-5m-optimizer.md`  
**Reviewer**: Claude Code (ai-dev-env:code-review)

---

## Stats

- Files Modified: 5
- Files Added: 0
- Files Deleted: 0
- New lines: +70 (approx, across 5 files)
- Deleted lines: -25 (approx)

---

## Test Suite Results

All 250 runnable tests pass. The 37 errors (yfinance/ib_insync import failures in
test_data_sources, test_e2e, test_prepare, test_v3_f, test_screener_*, test_position_monitor)
are **pre-existing** — caused by missing optional dependencies in this environment,
unrelated to this changeset. SMT-specific tests (test_smt_backtest.py, test_smt_strategy.py)
all pass (34/34).

---

## Issues Found

---

```
severity: low
file: prepare_futures.py
line: 61
issue: Stale docstring — process_ticker still says "1m bars"
detail: The function docstring reads "Fetch and cache 1m bars for one futures ticker."
        After switching INTERVAL to "5m", this is misleading. It's not a logic bug but
        creates confusion for anyone reading the function in isolation.
suggestion: Change to "Fetch and cache futures bars for one ticker at INTERVAL resolution."
```

---

```
severity: low
file: data/sources.py
line: 76-77
issue: Comment contradicts the constant value (_IB_CONTFUTURE_MAX_DAYS = 14, comment says 7)
detail: The comment above _IB_CONTFUTURE_MAX_DAYS reads:
        "Empirically, requests >~10 D of 1m data timeout; 7 D is a safe ceiling."
        The value was changed from 7 to 14 in this changeset (per the diff) but the comment
        was not updated. The cap applies only when interval == "1m" (new logic), but the
        comment still implies 7 D is the safe ceiling, which conflicts with the 14-day value.
suggestion: Update comment to: "Empirically, 1m requests >~14 D of data timeout; 14 D is a safe ceiling."
```

---

```
severity: low
file: train_smt.py
lines: 142-143
issue: Stale docstring — detect_smt_divergence Args still specify "1m OHLCV DataFrame"
detail: Lines 142-143 read:
            mes_bars: 1m OHLCV DataFrame for MES, index = ET datetime
            mnq_bars: 1m OHLCV DataFrame for MNQ, same index alignment
        After this feature the function is interval-agnostic. The "1m" qualifier is now incorrect.
        (manage_position line 327 and run_backtest line 384 also have stale "1m" references but
        those are in the frozen DO NOT EDIT section below the boundary — leaving them is correct.)
suggestion: Change both lines to "OHLCV DataFrame for MES/MNQ, index = ET datetime (any interval)".
```

---

```
severity: low
file: tests/test_smt_backtest.py
line: 3
issue: Stale module docstring — still says "synthetic 1m DataFrames"
detail: The module docstring reads:
        "Uses synthetic 1m DataFrames written to a tmpdir. No IB connection required."
        All bar builders now use freq="5min". The docstring is inaccurate.
suggestion: Change to "Uses synthetic 5m DataFrames written to a tmpdir. No IB connection required."
```

---

```
severity: low
file: tests/test_smt_backtest.py
lines: 240-262 (test_fold_loop_smoke)
issue: Comment in plan says 18 bars = "exactly the kill zone (09:00-10:30)" but last bar is 10:25
detail: The plan states "18 bars × 5min = 90 minutes = exactly the kill zone (09:00-10:30)".
        In practice, 18 bars starting at 09:00 with freq=5min gives 09:00 through 10:25 (inclusive),
        which is 85 minutes — the 10:30 bar is not included.
        The test is still correct (run_backtest works fine; no test assertion depends on 10:30),
        but the comment in the plan is wrong by one bar.
        This is a documentation issue in the plan, not a code bug.
        The actual SESSION_END="10:30" mask uses <= so the 10:30 bar would be included in a
        19-bar dataset (periods=19); with periods=18 the 10:30 bar is simply absent.
        The smoke test asserts only that _compute_fold_params and run_backtest run without error,
        so there is no test regression. No fix needed in code.
suggestion: No code change required. Note for awareness only.
```

---

## Logic Verification (No Issues Found)

**`_bars_for_minutes` helper (train_smt.py:113-125)**
- At 5m: `delta_secs=300`, `bar_mins=5`, `round(5/5)=1`, `max(1,1)=1`. Correct.
- At 1m: `delta_secs=60`, `bar_mins=1`, `round(5/1)=5`, `max(1,5)=5`. Matches old hard-coded value.
- Edge case `len(df) < 2`: returns 1 (safe floor). Correct.
- Edge case `delta_secs <= 0`: returns 1 (safe floor). Correct.

**`detect_smt_divergence` guard (train_smt.py:148-149)**
- `_threshold = _min_bars if _min_bars is not None else MIN_BARS_BEFORE_SIGNAL`
- Backward-compat: callers passing no `_min_bars` (all existing tests) get the global, which
  monkeypatching still controls. Verified correct.

**`screen_session` loop (train_smt.py:274-282)**
- `_min_bars` computed from session slice (not full DF), so bar size inference uses the correct
  kill-zone timestamps. `range(_min_bars, n_bars)` starts at the right index.
- `_min_bars` passed to `detect_smt_divergence` as the 5th arg. Guard inside is then always
  satisfied on the first iteration (bar_idx == _min_bars, so bar_idx - 0 >= _min_bars). Redundant
  but harmless.

**Signal geometry at 5m (`_build_short/long_signal_bars`)**
- 90 bars at 5min starting 09:00 → last bar is 16:25. Session mask (<=10:30) captures 19 bars.
- Divergence bar index 7 = 09:35, confirmation bar index 8 = 09:40. Both within session. Correct.
- `_min_bars=1` at 5m → scan starts at bar_idx=1, reaching bar 7 without issue. Correct.

**`data/sources.py` contfuture branch**
- `duration_days = requested_days` for non-1m intervals. For 90-day 5m request: single call
  with `durationStr="90 D"`, `endDateTime=""`. No chunking (chunk_days unused in contfuture branch).
- Trim at end: `df[(df.index >= start_dt) & (df.index < end_dt)]` correctly filters IB's
  most-recent data to the requested window. Correct.

**`conftest.py` futures bootstrap**
- `pytest_configure` writes `fetch_interval: "5m"` only when manifest is absent. This enables
  `import train_smt` on fresh machines. Tests that call `load_futures_data()` use the
  `futures_tmpdir` fixture which creates the `5m/` subdir explicitly. No gap.

---

## Security

No security issues. No hardcoded secrets. No SQL/injection surfaces. No new external URLs.

---

## Summary

4 low-severity stale docstring/comment issues. No logic bugs, no security issues, no test
regressions. The core design (interval-aware cap, `_bars_for_minutes` helper, backward-compat
`_min_bars` parameter) is correctly implemented and verified by the test suite.
