# Code Review: Session Pipeline Unification

**Date**: 2026-05-04
**Branch**: unified-session
**Reviewer**: ai-dev-env:code-review

## Stats

- Files Modified: 3 (`backtest_smt.py`, `signal_smt.py`, `automation/main.py`)
- Files Added: 2 (`session_pipeline.py`, `tests/test_session_pipeline.py`)
- New lines: ~141
- Deleted lines: ~329

## Test Results

- `tests/test_session_pipeline.py`: 15/15 passed
- Integration tests (`test_smt_dispatch_order`, `test_smt_backtest`, `test_signal_smt`, `test_automation_main`): 89/89 passed
- Full suite (excluding `test_live_run.py` which requires IB connection): 977 passed, 3 failed, 1 skipped

## Pre-existing Failures

The 3 failures in the full suite are pre-existing (confirmed by stashing and retesting):
- `tests/test_smt_strategy_v2.py::TestEarlyExits::test_failed_entries_exactly_two_still_allowed` — pre-existing
- `tests/test_smt_strategy_v2.py::TestSignalShape::test_signal_record_shape` — pre-existing
- `tests/test_hypothesis_smt.py::test_generate_writes_hypothesis_file` — isolation artifact; passes when run alone

---

## Issues Found

### Issue 1 (medium): `today_mnq` passed to `run_hypothesis` is unfiltered by time in live paths

```
severity: medium
file: signal_smt.py, automation/main.py
line: signal_smt.py:841, automation/main.py:884
issue: today_mnq passed to SessionPipeline.on_1m_bar contains all bars for the day regardless of `now`
detail: In the live wrappers (signal_smt.py:841, automation/main.py:884), `today_mnq` is built as
  `self._mnq_1m_df[self._mnq_1m_df.index.date == now.date()]` — this includes all bars for the date,
  including those timestamped AFTER `now` if the DataFrame was pre-populated with future data.
  In backtest_smt.py:1246, `today_mnq` IS correctly filtered: `mnq_1m_today[mnq_1m_today.index <= bar_ts]`.
  Inside SessionPipeline.on_1m_bar, `today_mnq` is passed verbatim to `run_hypothesis` (line 122),
  while `recent` (used by trend/strategy) is separately filtered to `<= now` (line 105).
  In a live environment, bars arrive sequentially so future bars cannot exist yet — this is safe in
  production. However, it creates a residual live/backtest asymmetry: the plan's Fix #4 states
  "all-day MNQ/MES slices (midnight to now)" but the live path passes "all-day, no upper bound".
  The plan explicitly chose the live behavior for Fix #4, so this is intentional, but the inconsistency
  with the backtest is worth documenting.
suggestion: Either document in the wrapper docstrings that `today_mnq` in live paths is unbounded
  above (acceptable because bars arrive in real-time), or filter it to `<= now` in the wrappers
  for parity. Given the plan's explicit decision, documenting is sufficient.
```

### Issue 2 (low): `run_hypothesis` return value could be `None` — guard handles it but inconsistently

```
severity: low
file: session_pipeline.py
line: 129
issue: `if hyp_divs:` guards against None but the type annotation says `list`
detail: `run_hypothesis` is annotated as `-> list` and always returns a list ([], never None).
  The `if hyp_divs:` guard is correct and efficient (empty list is falsy), but the comment at
  line 118 says "Fix #4: all-day MNQ/MES slices" — the guard works fine, this is purely a
  documentation note. By contrast, `run_trend` and `run_strategy` are annotated `-> Optional[Signal]`
  and `-> Optional[dict]` respectively, and both are guarded with `is not None`. The pattern is
  consistent with the actual return types.
suggestion: No code change needed. The guard is correct and idiomatic.
```

### Issue 3 (low): Performance regression in backtest hot loop — numpy pre-extraction removed

```
severity: low
file: backtest_smt.py
line: 1239-1247
issue: The old backtest extracted OHLC into numpy arrays before the per-bar loop; the refactor
  replaced this with per-bar `mnq_session_bars.iloc[_bar_i]` Series access and a per-bar
  `mnq_1m_today[mnq_1m_today.index <= bar_ts]` DataFrame filter.
detail: The original code extracted `_sess_opens`, `_sess_highs`, `_sess_lows`, `_sess_closes`
  as numpy arrays to avoid per-bar label lookups. The refactored code does `iloc[_bar_i]` (O(1),
  fast) and per-bar `today_mnq` filters (O(n) boolean mask, repeated n times → O(n²) total).
  The original code passed `mnq_session_bars.iloc[: _bar_i + 1]` as `recent` (O(1) slice view);
  the new code passes `mnq_1m_today[mnq_1m_today.index <= bar_ts]` (boolean mask over all-day bars,
  which is Fix #7 — correct behavior, but O(n) cost per bar). For a typical 6.5-hour session
  (~390 bars) × all-day bars (960+) this is ~370k comparisons per day. For multi-year backtests
  this accumulates. The plan explicitly notes numpy pre-extraction was removed; this is an accepted
  tradeoff for correctness.
suggestion: If backtest performance is a concern, consider caching the cumulative slices using
  `enumerate` + a growing index into a pre-sorted array rather than repeating the boolean mask.
  For now, the correctness improvement justifies the cost.
```

### Issue 4 (low): `on_session_start` lazy import of `smt_state` inside method body

```
severity: low
file: session_pipeline.py
line: 43-46
issue: `smt_state` is imported inside `on_session_start` on every call, not at module level.
detail: The lazy import pattern was likely chosen to avoid circular imports, which is valid.
  However, `hypothesis.py`, `daily.py`, etc. are all imported at module level in `session_pipeline.py`
  while `smt_state` alone is deferred. If there is no circular import risk, this is inconsistent.
  If there IS a circular import risk, a comment explaining why would help maintainers.
suggestion: Add a brief comment: `# Imported here to avoid circular import via hypothesis -> smt_state`
  or move to module level if there is no circular import risk.
```

---

## Summary

The refactor is structurally sound. All 8 behavioral fixes are correctly implemented and verified by the 15 new unit tests. The `SessionPipeline` class properly centralizes the dispatch logic. The slippage annotation in `backtest_smt.py` using the `_before` / `day_events[_before:]` slice pattern is correct and handles all emitted events (trend, hypothesis, strategy) uniformly.

The only true behavioral concern worth noting (Issue 1) is an intentional design decision acknowledged in the plan: the live wrappers pass an unbound `today_mnq` to `SessionPipeline`, while the backtest correctly bounds it to `<= bar_ts`. In live execution this cannot cause lookahead bias because bars don't exist before they arrive, but it means the two paths remain subtly different at the `run_hypothesis` input level.

No critical or high-severity issues found.
