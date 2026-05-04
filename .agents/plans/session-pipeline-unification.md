# Feature: Session Pipeline Unification

**⚠️ EXECUTION RULES — READ FIRST:**
- Implement ALL changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
- Only make code changes — no git operations

Validate documentation and codebase patterns before implementing. Match naming of existing utils, types, and models. Import from correct files.

---

## Feature Description

Three code paths share the same per-session dispatch sequence (daily → trend → hypothesis → strategy) but each implements it independently, with 8 concrete behavioral differences that cause backtest results to diverge from live execution. This refactor extracts the shared sequence into `session_pipeline.py` and fixes all 8 live/backtest divergences in the process.

## User Story

As a developer maintaining the SMT trading system,
I want all three execution paths (backtest, signal_smt, automation) to use a single shared session pipeline module,
So that any change to dispatch logic applies everywhere simultaneously and backtest results faithfully represent live behavior.

## Problem Statement

`backtest_smt.run_backtest_v2`, `signal_smt.SmtV2Dispatcher`, and `automation/main.SmtV2Dispatcher` each implement the daily/trend/hypothesis/strategy dispatch loop. They have diverged in 8 ways:

1. **`run_strategy` frequency**: backtest calls it every 1m bar; live only on 5m boundaries (4-minute fill-detection blind window in live)
2. **ATH seeding**: backtest seeds `all_time_high` from full historical data; live resets to `DEFAULT_GLOBAL` (ATH=0.0), then only updates from today's bars
3. **`hist_1hr` / `hist_4hr` missing in live**: backtest passes both to `run_hypothesis`; live passes neither (BOS/CHoCH scoring and FVG detection silently disabled)
4. **`run_hypothesis` MNQ/MES slice**: backtest passes session-only bars (09:20+); live passes all-day bars
5. **Hourly resample inconsistencies**: window (14d vs unbounded), `label=` parameter, Volume column inclusion all differ
6. **`today_bars` to `run_daily`**: backtest filters to bars ≤ 09:20; live passes all-day bars (including future bars at construction time)
7. **`recent` bars scope**: backtest passes session-only bars to trend/strategy; live passes all-day bars
8. **`bar_dict` missing body fields**: live omits `body_high` / `body_low` from the 1m bar dict passed to trend/strategy

Issue #9 (slippage) is intentionally backtest-only and requires no change.

## Solution Statement

Create `session_pipeline.py` with a `SessionPipeline` class. The class accepts historical data slices and an event-emit callback at construction, then exposes two methods: `on_session_start(now, today_mnq_at_open)` and `on_1m_bar(now, mnq_bar_row, mes_bar_row, today_mnq, today_mes) -> list[dict]`. All three callers are refactored to instantiate `SessionPipeline` and delegate to it, deleting their local `SmtV2Dispatcher` implementations.

Behavioral decisions confirmed by user:
- `run_strategy` frequency → **every 1m bar** (fix live)
- `recent` bars scope → **all-day from midnight** (fix backtest)
- Module name → **`session_pipeline.py`**

## Feature Metadata

**Feature Type**: Refactor + Bug Fix
**Complexity**: Medium
**Primary Systems Affected**: `backtest_smt.py`, `signal_smt.py`, `automation/main.py`, new `session_pipeline.py`
**Dependencies**: None (internal only)
**Breaking Changes**: No — public APIs of `run_backtest_v2`, `signal_smt.main`, `automation.main.main` are unchanged

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ BEFORE IMPLEMENTING

- `backtest_smt.py` (lines 1146–1395) — `run_backtest_v2`: the reference implementation; all behavioral choices come from here except issues 4 and 7 where user chose the live behavior
- `signal_smt.py` (lines 808–895) — `SmtV2Dispatcher` to be replaced; note `_emit_v2_signal` at line 810 stays as the emit callback
- `automation/main.py` (lines 856–936) — identical `SmtV2Dispatcher` to be replaced; note its own `_emit_v2_signal` stays
- `hypothesis.py` (lines 761–900) — `run_hypothesis` signature: `(now, mnq_1m, mes_1m, hist_mnq_1m, hist_mes_1m, *, hist_1hr=None, hist_4hr=None) -> list`
- `daily.py` (lines 207–321) — `run_daily` signature: `(now, mnq_1m, hist_mnq_1m, hist_hourly_mnq) -> None`
- `trend.py` (lines 95–115) — `run_trend` signature: `(now, mnq_1m_bar, mnq_1m_recent) -> Optional[Signal]`; bar dict keys: `time, open, high, low, close`
- `strategy.py` (lines 112–132) — `run_strategy` signature: `(now, mnq_bar, mnq_1m_recent) -> Optional[dict]`; bar dict keys: `time, open, high, low, close, body_high, body_low`
- `smt_state.py` (line 20) — `DEFAULT_GLOBAL = {"all_time_high": 0.0, "trend": "up"}`
- `tests/test_smt_dispatch_order.py` — tests that assert current dispatch behavior; will need updating for issues 1, 4, 7

### New Files to Create

- `session_pipeline.py` — `SessionPipeline` class (shared dispatch logic)
- `tests/test_session_pipeline.py` — unit tests for `SessionPipeline`

### Patterns to Follow

**ATH seeding pattern** (from `backtest_smt.py` line 1202–1206):
```python
hist_for_ath = mnq_all.iloc[:_mnq_pos_day]
seeded_global = copy.deepcopy(DEFAULT_GLOBAL)
if not hist_for_ath.empty:
    seeded_global["all_time_high"] = float(hist_for_ath["High"].max())
save_global(seeded_global)
```

**Hourly resample pattern** (standardised — 14d window, label="left", no Volume):
```python
_agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
hist_1hr_full = hist_mnq_1m.resample("1h", label="left").agg(_agg).dropna(subset=["Open"])
hist_1hr = hist_1hr_full[hist_1hr_full.index >= now - pd.Timedelta(days=14)]
hist_4hr = hist_mnq_1m.resample("4h", label="left").agg(_agg).dropna(subset=["Open"])
```

**bar_dict with body fields** (from `backtest_smt.py` line 1301–1308):
```python
mnq_1m_bar = {
    "time": bar_ts.isoformat(), "open": _o, "high": _h, "low": _l, "close": _c,
    "body_high": max(_o, _c), "body_low": min(_o, _c),
}
```

---

## PARALLEL EXECUTION STRATEGY

### Dependency Graph

```
┌────────────────────────────────────────────────────┐
│ WAVE 1: Foundation (Parallel)                      │
├────────────────────────────────────────────────────┤
│ Task 1.1: CREATE session_pipeline.py               │
│ Task 1.2: CREATE tests/test_session_pipeline.py    │
└────────────────────────────────────────────────────┘
                         ↓
┌────────────────────────────────────────────────────┐
│ WAVE 2: Consumers (Parallel after Wave 1)          │
├────────────────────────────────────────────────────┤
│ Task 2.1: REFACTOR backtest_smt.run_backtest_v2    │
│ Task 2.2: REFACTOR signal_smt.SmtV2Dispatcher      │
│ Task 2.3: REFACTOR automation/main.SmtV2Dispatcher │
└────────────────────────────────────────────────────┘
                         ↓
┌────────────────────────────────────────────────────┐
│ WAVE 3: Test Alignment (Sequential)                │
├────────────────────────────────────────────────────┤
│ Task 3.1: UPDATE test_smt_dispatch_order.py        │
│ Task 3.2: Full suite validation                    │
└────────────────────────────────────────────────────┘
```

### Interface Contracts

**Task 1.1 provides** → Tasks 2.1, 2.2, 2.3 consume:
```python
class SessionPipeline:
    def __init__(
        self,
        hist_mnq_1m: pd.DataFrame,   # all bars strictly before today's midnight
        hist_mes_1m: pd.DataFrame,
        emit_fn: Callable[[dict], None],
    ) -> None: ...

    def on_session_start(
        self,
        now: pd.Timestamp,            # 09:20 ET timestamp
        today_mnq_at_open: pd.DataFrame,  # today's bars up to and including now
    ) -> None: ...

    def on_1m_bar(
        self,
        now: pd.Timestamp,
        mnq_bar_row: pd.Series,       # current 1m bar (Open/High/Low/Close)
        mes_bar_row: pd.Series,
        today_mnq: pd.DataFrame,      # all today's bars from midnight up to now
        today_mes: pd.DataFrame,
    ) -> list[dict]: ...              # list of emitted event dicts (may be empty)
```

### Parallelization Summary

**Wave 1 — Fully Parallel**: Tasks 1.1, 1.2 — no dependencies
**Wave 2 — Parallel after Wave 1**: Tasks 2.1, 2.2, 2.3 — all read `session_pipeline.py`, write independent files
**Wave 3 — Sequential**: Tasks 3.1, 3.2 — need all Wave 2 consumers in place

---

## IMPLEMENTATION PLAN

### Phase 1: Create `session_pipeline.py`

#### Task 1.1: CREATE `session_pipeline.py`

**Purpose**: Single source of truth for the daily/trend/hypothesis/strategy dispatch pipeline. Fixes all 8 behavioral divergences by implementing the correct behavior once.

**Implementation**:

```python
# session_pipeline.py
# Shared per-session bar-dispatch pipeline used by backtest_smt, signal_smt, automation.
from __future__ import annotations

import copy
from typing import Callable

import pandas as pd

import daily as _daily_mod
import hypothesis as _hyp_mod
import strategy as _strat_mod
import trend as _trend_mod


class SessionPipeline:
    """Dispatches daily → trend → hypothesis → strategy for one trading session.

    Fixes: ATH seeding, hist_1hr/4hr to hypothesis, run_strategy every 1m bar,
    bar_dict body fields, consistent hourly resample, all-day 'recent' scope.
    """

    def __init__(
        self,
        hist_mnq_1m: pd.DataFrame,
        hist_mes_1m: pd.DataFrame,
        emit_fn: Callable[[dict], None],
    ) -> None:
        self._hist_mnq_1m = hist_mnq_1m
        self._hist_mes_1m = hist_mes_1m
        self._emit = emit_fn
        self._daily_triggered = False
        self._hist_1hr: pd.DataFrame | None = None
        self._hist_4hr: pd.DataFrame | None = None

    def on_session_start(
        self,
        now: pd.Timestamp,
        today_mnq_at_open: pd.DataFrame,
    ) -> None:
        """Seed ATH, reset state, compute resamples, call run_daily. Call once at 09:20 ET."""
        from smt_state import (
            DEFAULT_DAILY, DEFAULT_GLOBAL, DEFAULT_HYPOTHESIS, DEFAULT_POSITION,
            save_daily, save_global, save_hypothesis, save_position,
        )

        # Fix #2: Seed ATH from full history before resetting state.
        seeded_global = copy.deepcopy(DEFAULT_GLOBAL)
        if not self._hist_mnq_1m.empty:
            seeded_global["all_time_high"] = float(self._hist_mnq_1m["High"].max())
        save_global(seeded_global)
        save_daily(copy.deepcopy(DEFAULT_DAILY))
        save_hypothesis(copy.deepcopy(DEFAULT_HYPOTHESIS))
        save_position(copy.deepcopy(DEFAULT_POSITION))

        # Fix #5: Unified hourly resample — 14-day window, label="left", no Volume.
        _agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
        _14d_ago = now - pd.Timedelta(days=14)
        if not self._hist_mnq_1m.empty:
            _1hr_full = (
                self._hist_mnq_1m.resample("1h", label="left")
                .agg(_agg)
                .dropna(subset=["Open"])
            )
            self._hist_1hr = _1hr_full[_1hr_full.index >= _14d_ago]
            self._hist_4hr = (
                self._hist_mnq_1m.resample("4h", label="left")
                .agg(_agg)
                .dropna(subset=["Open"])
            )
        else:
            self._hist_1hr = pd.DataFrame(columns=list(_agg))
            self._hist_4hr = pd.DataFrame(columns=list(_agg))

        # Fix #6: Pass only bars up to now (≤ 09:20) to run_daily.
        _daily_mod.run_daily(now, today_mnq_at_open, self._hist_mnq_1m, self._hist_1hr)
        self._daily_triggered = True

    def on_1m_bar(
        self,
        now: pd.Timestamp,
        mnq_bar_row: pd.Series,
        mes_bar_row: pd.Series,
        today_mnq: pd.DataFrame,
        today_mes: pd.DataFrame,
    ) -> list[dict]:
        """Process one completed 1m bar. Returns list of emitted event dicts."""
        if not self._daily_triggered:
            return []

        _o = float(mnq_bar_row["Open"])
        _h = float(mnq_bar_row["High"])
        _l = float(mnq_bar_row["Low"])
        _c = float(mnq_bar_row["Close"])

        # Fix #8: bar_dict always includes body_high / body_low.
        mnq_1m_bar = {
            "time": now.isoformat(),
            "open": _o, "high": _h, "low": _l, "close": _c,
            "body_high": max(_o, _c), "body_low": min(_o, _c),
        }

        # Fix #7: recent = all-day bars from midnight up to now.
        recent = today_mnq[today_mnq.index <= now]

        events: list[dict] = []

        # Trend runs first: validates existing hypothesis before a new one may form.
        trend_sig = _trend_mod.run_trend(now, mnq_1m_bar, recent)
        if trend_sig is not None:
            self._emit(trend_sig)
            events.append(trend_sig)

        is_5m = (now.minute % 5 == 0)

        if is_5m:
            # Fix #4: all-day MNQ/MES slices (midnight to now).
            # Fix #3: pass hist_1hr and hist_4hr.
            hyp_divs = _hyp_mod.run_hypothesis(
                now,
                today_mnq,
                today_mes,
                self._hist_mnq_1m,
                self._hist_mes_1m,
                hist_1hr=self._hist_1hr,
                hist_4hr=self._hist_4hr,
            )
            if hyp_divs:
                for d in hyp_divs:
                    self._emit(d)
                events.extend(hyp_divs)

        # Fix #1: run_strategy on every 1m bar (not just 5m boundaries).
        strat_sig = _strat_mod.run_strategy(now, mnq_1m_bar, recent)
        if strat_sig is not None:
            self._emit(strat_sig)
            events.append(strat_sig)

        return events
```

**Validate**: `uv run python -c "from session_pipeline import SessionPipeline; print('OK')"`

---

#### Task 1.2: CREATE `tests/test_session_pipeline.py`

**Purpose**: Verify `SessionPipeline` implements the correct dispatch behavior and fixes all 8 issues.

**Test cases**:

1. **`test_on_session_start_seeds_ath_from_history`**: Pass `hist_mnq_1m` with max High=25000. After `on_session_start`, assert `smt_state.load_global()["all_time_high"] == 25000`.

2. **`test_on_session_start_resets_state_files`**: After `on_session_start`, assert `load_daily()`, `load_hypothesis()`, `load_position()` all equal their DEFAULT values (except `all_time_high` update).

3. **`test_on_session_start_computes_hourly_resamples`**: After `on_session_start`, assert `pipeline._hist_1hr` is not None and non-empty; bars older than 14 days are excluded.

4. **`test_on_session_start_calls_run_daily_with_filtered_bars`**: Mock `daily.run_daily`; assert it is called exactly once with `today_mnq_at_open` (not all-day bars) and with the 14-day-windowed hourly resample.

5. **`test_on_1m_bar_calls_trend_every_bar`**: Two-bar session (09:20, 09:21); mock `run_trend`; assert called twice.

6. **`test_on_1m_bar_calls_hypothesis_only_on_5m`**: Two-bar session; 09:20 is 5m boundary, 09:21 is not; assert `run_hypothesis` called once (only at 09:20).

7. **`test_on_1m_bar_calls_strategy_every_bar`** (Fix #1): Two-bar session; assert `run_strategy` called twice (not just at 5m boundary).

8. **`test_on_1m_bar_bar_dict_has_body_fields`** (Fix #8): Capture bar dict passed to `run_trend`; assert `"body_high"` and `"body_low"` keys present with correct values.

9. **`test_on_1m_bar_recent_includes_midnight_bars`** (Fix #7): Construct `today_mnq` starting at 00:00 ET; assert `recent` passed to `run_trend` includes the midnight bar.

10. **`test_on_1m_bar_hypothesis_receives_hist_resamples`** (Fix #3): After `on_session_start`, call `on_1m_bar` at a 5m boundary; capture kwargs passed to `run_hypothesis`; assert `hist_1hr` and `hist_4hr` are non-None DataFrames.

11. **`test_on_1m_bar_emits_events_via_callback`**: Emit callback captures all calls; verify events from trend/hypothesis/strategy are all passed to it.

12. **`test_on_1m_bar_skips_if_daily_not_triggered`**: Call `on_1m_bar` without calling `on_session_start` first; assert returns empty list and no module calls fire.

13. **`test_ath_gate_uses_seeded_ath_not_zero`** (Fix #2): Hist with max High=25000; today's bar low=24000; assert hypothesis ATH gate uses 25000, not 0.0.

14. **`test_hourly_resample_excludes_volume`** (Fix #5): Assert `_hist_1hr` columns do not include "Volume".

15. **`test_hourly_resample_label_left`** (Fix #5): Assert `_hist_1hr` index timestamps are left-aligned (resample label="left").

**Validate**: `uv run pytest tests/test_session_pipeline.py -v`

---

### Phase 2: Refactor Consumers

#### Task 2.1: REFACTOR `backtest_smt.run_backtest_v2`

**Purpose**: Replace the inline daily/trend/hypothesis/strategy logic with `SessionPipeline`. Fix issues 4, 5, 6, 7, 8 in the backtest path.

**Dependencies**: Task 1.1

**Steps**:

1. Add import at top of `run_backtest_v2`: `from session_pipeline import SessionPipeline`

2. Remove the following blocks from the per-day loop body:
   - ATH seeding block (lines 1200–1206) — now in `SessionPipeline.on_session_start`
   - Hourly/4hr resample block (lines 1223–1243) — now in `SessionPipeline.on_session_start`
   - `_daily_mod.run_daily(...)` call (line 1249) — now in `SessionPipeline.on_session_start`
   - The per-bar trend/hypothesis/strategy calls (lines 1316–1339) — now in `SessionPipeline.on_1m_bar`

3. Keep in the backtest (these are NOT moving to the shared module):
   - `save_global / save_daily / save_hypothesis / save_position` state resets — SessionPipeline handles them internally; remove duplicates from backtest
   - Levels snapshot write (lines 1251–1260) — stays in backtest, called immediately after `on_session_start`
   - Numpy array pre-extraction for performance (lines 1282–1288) — keep
   - Slippage annotation on market-close/market-entry events (lines 1319–1338) — keep in backtest post-emit
   - End-of-session event (lines 1344–1355) — keep in backtest
   - Trade-pairing loop (lines 1360–1394) — keep in backtest

4. Per-day loop new structure:

```python
# Construct pipeline for this day
def _backtest_emit(evt: dict) -> None:
    day_events.append(evt)

pipeline = SessionPipeline(hist_mnq_1m, hist_mes_1m, _backtest_emit)
today_at_open = mnq_1m_today[mnq_1m_today.index <= session_start_ts]
pipeline.on_session_start(session_start_ts, today_at_open)

# Optional levels snapshot (write_events path)
if write_events:
    ...  # unchanged

for _bar_i in range(_n_sess):
    bar_ts = _sess_idx[_bar_i]
    now = bar_ts
    mnq_bar_row = mnq_session_bars.iloc[_bar_i]
    mes_pos = mes_session_bars.index.searchsorted(bar_ts, side="right")
    mes_bar_row = mes_session_bars.iloc[mes_pos - 1] if mes_pos > 0 else mes_session_bars.iloc[0]
    today_mnq = mnq_1m_today[mnq_1m_today.index <= bar_ts]
    today_mes = mes_1m_today[mes_1m_today.index <= bar_ts]
    pipeline.on_1m_bar(now, mnq_bar_row, mes_bar_row, today_mnq, today_mes)
    # Post-process slippage on events just appended to day_events
    for evt in day_events[-len(pipeline_result):]:
        if evt.get("kind") == "market-close":
            evt["slippage"] = V2_MARKET_CLOSE_SLIPPAGE_PTS
        if evt.get("kind") in ("market-close", "market-entry"):
            evt["slippage"] = V2_MARKET_CLOSE_SLIPPAGE_PTS
```

Note: to apply slippage only to newly-emitted events, track `day_events` length before and after each `on_1m_bar` call:
```python
_before = len(day_events)
pipeline.on_1m_bar(now, mnq_bar_row, mes_bar_row, today_mnq, today_mes)
for evt in day_events[_before:]:
    if evt.get("kind") in ("market-close", "market-entry"):
        evt["slippage"] = V2_MARKET_CLOSE_SLIPPAGE_PTS
```

5. Remove the `import daily as _daily_mod`, `import hypothesis as _hyp_mod`, `import strategy as _strat_mod`, `import trend as _trend_mod` lines from inside `run_backtest_v2` (they are now imported at module level by `session_pipeline.py`).

**Validate**: `uv run pytest tests/test_smt_dispatch_order.py tests/test_smt_backtest.py -v`

---

#### Task 2.2: REFACTOR `signal_smt.SmtV2Dispatcher`

**Purpose**: Replace `SmtV2Dispatcher` class body with a thin wrapper around `SessionPipeline`. Fix issues 1, 2, 3, 5, 6, 7, 8 in the signal_smt live path.

**Dependencies**: Task 1.1

**Steps**:

1. Add import near top of `signal_smt.py`:
   ```python
   from session_pipeline import SessionPipeline
   ```

2. Replace the entire `SmtV2Dispatcher` class body (lines ~815–895) with:

```python
class SmtV2Dispatcher:
    """Thin wrapper: wires IB bar callbacks into SessionPipeline."""

    def __init__(
        self,
        mnq_1m_df: pd.DataFrame,
        mes_1m_df: pd.DataFrame,
        hist_mnq_1m: pd.DataFrame,
        hist_mes_1m: pd.DataFrame,
    ) -> None:
        self._mnq_1m_df = mnq_1m_df
        self._mes_1m_df = mes_1m_df
        self._pipeline = SessionPipeline(hist_mnq_1m, hist_mes_1m, _emit_v2_signal)

    def on_session_start(self, now: pd.Timestamp) -> None:
        today_at_open = self._mnq_1m_df[
            (self._mnq_1m_df.index.date == now.date()) &
            (self._mnq_1m_df.index <= now)
        ]
        self._pipeline.on_session_start(now, today_at_open)

    def on_1m_bar(
        self,
        now: pd.Timestamp,
        mnq_bar_row: pd.Series,
        mes_bar_row: pd.Series,
    ) -> None:
        today_mnq = self._mnq_1m_df[self._mnq_1m_df.index.date == now.date()]
        today_mes = self._mes_1m_df[self._mes_1m_df.index.date == now.date()]
        self._pipeline.on_1m_bar(now, mnq_bar_row, mes_bar_row, today_mnq, today_mes)
```

3. The `_emit_v2_signal` module-level function at line ~810 is unchanged — it is now passed as the callback.

4. Verify no other code in `signal_smt.py` references the old private attributes (`self._daily`, `self._hyp`, `self._strat`, `self._trend`, `self._daily_triggered`, `self._session_date`) — these are no longer needed.

**Validate**: `uv run pytest tests/test_signal_smt.py tests/test_automation_main.py -v`

---

#### Task 2.3: REFACTOR `automation/main.py` `SmtV2Dispatcher`

**Purpose**: Identical refactor to Task 2.2 for the automation path.

**Dependencies**: Task 1.1

**Steps**:

1. Add import near top of `automation/main.py`:
   ```python
   from session_pipeline import SessionPipeline
   ```

2. Replace the entire `SmtV2Dispatcher` class body (lines ~856–936) with the same thin wrapper as Task 2.2, but using `automation/main.py`'s own `_emit_v2_signal` function as the callback:

```python
class SmtV2Dispatcher:
    """Thin wrapper: wires IB bar callbacks into SessionPipeline."""

    def __init__(
        self,
        mnq_1m_df: pd.DataFrame,
        mes_1m_df: pd.DataFrame,
        hist_mnq_1m: pd.DataFrame,
        hist_mes_1m: pd.DataFrame,
    ) -> None:
        self._mnq_1m_df = mnq_1m_df
        self._mes_1m_df = mes_1m_df
        self._pipeline = SessionPipeline(hist_mnq_1m, hist_mes_1m, _emit_v2_signal)

    def on_session_start(self, now: pd.Timestamp) -> None:
        today_at_open = self._mnq_1m_df[
            (self._mnq_1m_df.index.date == now.date()) &
            (self._mnq_1m_df.index <= now)
        ]
        self._pipeline.on_session_start(now, today_at_open)

    def on_1m_bar(
        self,
        now: pd.Timestamp,
        mnq_bar_row: pd.Series,
        mes_bar_row: pd.Series,
    ) -> None:
        today_mnq = self._mnq_1m_df[self._mnq_1m_df.index.date == now.date()]
        today_mes = self._mes_1m_df[self._mes_1m_df.index.date == now.date()]
        self._pipeline.on_1m_bar(now, mnq_bar_row, mes_bar_row, today_mnq, today_mes)
```

3. Remove now-unreferenced imports of `daily`, `hypothesis`, `strategy`, `trend` from inside the old `SmtV2Dispatcher.__init__` (they were lazy-imported via `import daily as _daily_mod` inside `__init__`).

**Validate**: `uv run pytest tests/test_automation_main.py -v`

---

### Phase 3: Test Alignment

#### Task 3.1: UPDATE `tests/test_smt_dispatch_order.py`

**Purpose**: Align existing dispatch-order tests with the new unified behavior (issues 1, 4, 7).

**Dependencies**: Tasks 2.1, 2.2, 2.3

**Required changes**:

1. **`test_1m_only_dispatches_trend`** (line 135): The assertion `calls["strategy"] == 2` and comment "trend and strategy fire on every bar" are already correct for the new every-bar behavior. No change needed to the assertion. However, the fake callbacks must still match the `run_strategy` signature (takes `mnq_bar: dict, mnq_1m_recent: pd.DataFrame`). Verify the mock signature matches — update if `body_high`/`body_low` would cause an issue (they won't; mock ignores args).

2. **`test_5m_dispatch_order_is_trend_then_hypothesis_then_strategy`** (line 51): This test patches the module-level functions in `daily`, `hypothesis`, `trend`, `strategy`. After the refactor, `run_backtest_v2` calls through `SessionPipeline` which imports these modules at import time. The monkeypatching approach (`monkeypatch.setattr(_daily_mod, "run_daily", ...)`) still works because `SessionPipeline` holds references to the module objects and uses late attribute lookup. Verify the test still passes without changes; if it doesn't (due to import order), change the patch targets to `session_pipeline._daily_mod.run_daily` etc.

3. **`test_trend_invalidation_blocks_same_bar_fill`** (line 194): Tests `run_trend` and `run_strategy` directly — not affected by refactor. No change.

4. **`test_run_backtest_v2_smoke_one_day`** (line 287): Tests the public API of `run_backtest_v2` — no change needed.

5. **`test_old_run_backtest_unchanged`** (line 312): Tests the legacy `run_backtest` function — no change needed.

**Validate**: `uv run pytest tests/test_smt_dispatch_order.py -v`

---

#### Task 3.2: Full Suite Validation

**Purpose**: Confirm no regressions across the entire test suite.

**Dependencies**: Task 3.1

**Steps**:

1. Run full test suite: `uv run pytest --tb=short -q`
2. Compare pass/fail count to baseline (940 passed, 9 skipped from last run)
3. For any new failures: investigate whether caused by behavioral change (expected, document) or implementation error (fix)
4. Key test files to check: `tests/test_smt_dispatch_order.py`, `tests/test_session_pipeline.py`, `tests/test_smt_backtest.py`, `tests/test_signal_smt.py`, `tests/test_automation_main.py`

**Validate**: `uv run pytest --tb=short -q 2>&1 | tail -5`

---

## STEP-BY-STEP TASKS

### WAVE 1: Foundation

#### Task 1.1: CREATE `session_pipeline.py`

- **WAVE**: 1
- **AGENT_ROLE**: core-developer
- **DEPENDS_ON**: []
- **BLOCKS**: [2.1, 2.2, 2.3]
- **PROVIDES**: `SessionPipeline` class with `on_session_start` and `on_1m_bar`
- **IMPLEMENT**: As specified in Phase 1 above. File goes in project root (same level as `daily.py`, `trend.py`, etc.)
- **PATTERN**: ATH seeding from `backtest_smt.py:1200–1206`; bar dict from `backtest_smt.py:1301–1308`; hourly resample unified
- **VALIDATE**: `uv run python -c "from session_pipeline import SessionPipeline; print('OK')"`

#### Task 1.2: CREATE `tests/test_session_pipeline.py`

- **WAVE**: 1
- **AGENT_ROLE**: test-developer
- **DEPENDS_ON**: []
- **BLOCKS**: [3.2]
- **PROVIDES**: 15 unit tests covering all 8 fixed behaviors
- **IMPLEMENT**: All 15 test cases listed in Phase 1 Task 1.2. Use `monkeypatch` to patch `session_pipeline._daily_mod.run_daily` etc. Use `tmp_path` + `smt_state` isolation pattern from `test_smt_dispatch_order.py:37–44`.
- **PATTERN**: `tests/test_smt_dispatch_order.py:37–44` for state isolation fixture; `tests/test_smt_dispatch_order.py:56–70` for fake module functions
- **VALIDATE**: `uv run pytest tests/test_session_pipeline.py -v` (will fail until Task 1.1 is done; run after 1.1)

**Wave 1 Checkpoint**: `uv run pytest tests/test_session_pipeline.py -v`

---

### WAVE 2: Consumers

#### Task 2.1: REFACTOR `backtest_smt.run_backtest_v2`

- **WAVE**: 2
- **AGENT_ROLE**: core-developer
- **DEPENDS_ON**: [1.1]
- **BLOCKS**: [3.1, 3.2]
- **PROVIDES**: `run_backtest_v2` using `SessionPipeline` with fixes 1, 2, 3, 4, 5, 6, 7, 8
- **USES_FROM_WAVE_1**: Task 1.1 provides `SessionPipeline(hist_mnq_1m, hist_mes_1m, emit_fn)`
- **IMPLEMENT**: As specified in Phase 2 Task 2.1. Keep levels.json, slippage, trade-pairing, end-of-session event in backtest. Track `len(day_events)` before/after each `on_1m_bar` call to apply slippage only to new events.
- **VALIDATE**: `uv run pytest tests/test_smt_dispatch_order.py tests/test_smt_backtest.py -v`

#### Task 2.2: REFACTOR `signal_smt.SmtV2Dispatcher`

- **WAVE**: 2
- **AGENT_ROLE**: core-developer
- **DEPENDS_ON**: [1.1]
- **BLOCKS**: [3.2]
- **PROVIDES**: `signal_smt.SmtV2Dispatcher` as a thin wrapper around `SessionPipeline`
- **USES_FROM_WAVE_1**: Task 1.1 provides `SessionPipeline`
- **IMPLEMENT**: As specified in Phase 2 Task 2.2. Public API (`on_session_start(now)`, `on_1m_bar(now, mnq_bar_row, mes_bar_row)`) is unchanged so existing callers inside `signal_smt.py` need no changes.
- **VALIDATE**: `uv run pytest tests/test_signal_smt.py -v`

#### Task 2.3: REFACTOR `automation/main.py` `SmtV2Dispatcher`

- **WAVE**: 2
- **AGENT_ROLE**: core-developer
- **DEPENDS_ON**: [1.1]
- **BLOCKS**: [3.2]
- **PROVIDES**: `automation/main.SmtV2Dispatcher` as a thin wrapper around `SessionPipeline`
- **USES_FROM_WAVE_1**: Task 1.1 provides `SessionPipeline`
- **IMPLEMENT**: As specified in Phase 2 Task 2.3. Identical to Task 2.2 except uses `automation/main.py`'s own `_emit_v2_signal`.
- **VALIDATE**: `uv run pytest tests/test_automation_main.py -v`

**Wave 2 Checkpoint**: `uv run pytest tests/test_smt_dispatch_order.py tests/test_smt_backtest.py tests/test_signal_smt.py tests/test_automation_main.py -v`

---

### WAVE 3: Test Alignment

#### Task 3.1: UPDATE `tests/test_smt_dispatch_order.py`

- **WAVE**: 3
- **AGENT_ROLE**: test-developer
- **DEPENDS_ON**: [2.1, 2.2, 2.3]
- **BLOCKS**: [3.2]
- **IMPLEMENT**: As specified in Phase 3 Task 3.1. Patch targets may need updating to `session_pipeline._daily_mod` etc. if monkeypatching stops working through module indirection. Run tests first to see if changes are needed before modifying.
- **VALIDATE**: `uv run pytest tests/test_smt_dispatch_order.py -v`

#### Task 3.2: Full Suite Validation

- **WAVE**: 3
- **AGENT_ROLE**: qa-engineer
- **DEPENDS_ON**: [3.1]
- **IMPLEMENT**: Run full suite, compare to baseline (940 passed / 9 skipped), investigate and fix any regressions.
- **VALIDATE**: `uv run pytest --tb=short -q`

**Final Checkpoint**: All tests pass at or above baseline count. No regressions.

---

## TESTING STRATEGY

### Unit Tests

**Status**: ✅ Automated | **Tool**: pytest | **Location**: `tests/test_session_pipeline.py` | **Run**: `uv run pytest tests/test_session_pipeline.py -v`

| # | Test | Issue Fixed | Status |
|---|---|---|---|
| 1 | `test_on_session_start_seeds_ath_from_history` | #2 ATH seeding | ✅ |
| 2 | `test_on_session_start_resets_state_files` | General | ✅ |
| 3 | `test_on_session_start_computes_hourly_resamples` | #5 Resample | ✅ |
| 4 | `test_on_session_start_calls_run_daily_with_filtered_bars` | #6 today_bars filter | ✅ |
| 5 | `test_on_1m_bar_calls_trend_every_bar` | General | ✅ |
| 6 | `test_on_1m_bar_calls_hypothesis_only_on_5m` | General | ✅ |
| 7 | `test_on_1m_bar_calls_strategy_every_bar` | #1 Strategy freq | ✅ |
| 8 | `test_on_1m_bar_bar_dict_has_body_fields` | #8 Body fields | ✅ |
| 9 | `test_on_1m_bar_recent_includes_midnight_bars` | #7 Recent scope | ✅ |
| 10 | `test_on_1m_bar_hypothesis_receives_hist_resamples` | #3 hist_1hr/4hr | ✅ |
| 11 | `test_on_1m_bar_emits_events_via_callback` | General | ✅ |
| 12 | `test_on_1m_bar_skips_if_daily_not_triggered` | General | ✅ |
| 13 | `test_ath_gate_uses_seeded_ath_not_zero` | #2 ATH gate | ✅ |
| 14 | `test_hourly_resample_excludes_volume` | #5 Resample | ✅ |
| 15 | `test_hourly_resample_label_left` | #5 Resample | ✅ |

### Integration Tests

**Status**: ✅ Automated | **Tool**: pytest | **Location**: `tests/test_smt_dispatch_order.py`, `tests/test_smt_backtest.py` | **Run**: `uv run pytest tests/test_smt_dispatch_order.py tests/test_smt_backtest.py -v`

### Regression Tests

**Status**: ✅ Automated | **Tool**: pytest | **Run**: `uv run pytest --tb=short -q`

Baseline: 940 passed / 9 skipped (from last full suite run post-PickMyTrade fix).

### Manual Tests

None — all paths are automatable via mock injection.

### Test Automation Summary

| | Count | % |
|---|---|---|
| ✅ New unit tests (pytest) | 15 | 83% |
| ✅ Existing integration tests updated | 3 | 17% |
| ⚠️ Manual | 0 | 0% |
| **Total** | 18 | 100% |

---

## VALIDATION COMMANDS

### Level 1: Syntax

```bash
uv run python -c "from session_pipeline import SessionPipeline; print('import OK')"
uv run python -c "import backtest_smt; print('backtest_smt OK')"
uv run python -c "import signal_smt; print('signal_smt OK')"
uv run python -c "import automation.main; print('automation.main OK')"
```

### Level 2: Unit Tests

```bash
uv run pytest tests/test_session_pipeline.py -v
```

### Level 3: Integration Tests

```bash
uv run pytest tests/test_smt_dispatch_order.py tests/test_smt_backtest.py tests/test_signal_smt.py tests/test_automation_main.py -v
```

### Level 4: Full Suite

```bash
uv run pytest --tb=short -q
```

Expected: ≥ 940 passed, ≤ 9 skipped, 0 new failures.

---

## ACCEPTANCE CRITERIA

### Functional — `session_pipeline.py`
- [ ] `session_pipeline.py` exists at the project root and imports cleanly with no errors
- [ ] `SessionPipeline.on_session_start` seeds `global.json["all_time_high"]` from `hist_mnq_1m["High"].max()`, not from `DEFAULT_GLOBAL` value of 0.0 (Fix #2)
- [ ] `SessionPipeline.on_session_start` with empty `hist_mnq_1m` does not raise; `all_time_high` stays at `DEFAULT_GLOBAL` value
- [ ] `SessionPipeline.on_session_start` passes only bars with index ≤ `now` (09:20) to `run_daily` — not all-day bars (Fix #6)
- [ ] `SessionPipeline.on_session_start` produces `_hist_1hr`: 14-day lookback, `label="left"`, columns Open/High/Low/Close only (no Volume) (Fix #5)
- [ ] `SessionPipeline.on_session_start` produces `_hist_4hr`: same column set and `label="left"` as `_hist_1hr` (Fix #5)
- [ ] `SessionPipeline.on_session_start` with empty `hist_mnq_1m` produces empty-DataFrame `_hist_1hr` / `_hist_4hr` without raising
- [ ] `SessionPipeline.on_1m_bar` calls `run_strategy` on every 1m bar regardless of whether it is a 5m boundary (Fix #1)
- [ ] `SessionPipeline.on_1m_bar` constructs bar dict with `body_high = max(open, close)` and `body_low = min(open, close)` (Fix #8)
- [ ] `SessionPipeline.on_1m_bar` passes `hist_1hr` and `hist_4hr` as keyword arguments to `run_hypothesis` at every 5m boundary (Fix #3)
- [ ] `SessionPipeline.on_1m_bar` passes all-day MNQ/MES slices (midnight to `now`) to `run_hypothesis` — not session-only (Fix #4)
- [ ] `SessionPipeline.on_1m_bar` builds `recent` from all-day bars (midnight to `now`) passed to `run_trend` and `run_strategy` (Fix #7)
- [ ] `SessionPipeline.on_1m_bar` returns `[]` without calling any module when `on_session_start` has not been called first
- [ ] `SessionPipeline.on_1m_bar` returns the list of all event dicts emitted during that bar (same objects passed to `emit_fn`)

### Functional — Shared Utilities
- [ ] Slippage annotation logic is extracted to a shared utility (function or module) and is no longer duplicated inline per caller; `run_backtest_v2` calls this shared utility
- [ ] `_emit_v2_signal` (or an equivalent base emit function) is moved to a shared live-paths module so `signal_smt.py` and `automation/main.py` import from one place rather than each defining it independently
- [ ] The 1hr and 4hr resample computation is identical across all three paths (window, label, columns) — achieved by all paths going through `SessionPipeline.on_session_start`

### Functional — Consumers
- [ ] `backtest_smt.run_backtest_v2` instantiates `SessionPipeline` per day and delegates all dispatch logic (daily/trend/hypothesis/strategy) to it; the inline versions of these calls are removed
- [ ] `signal_smt.SmtV2Dispatcher` is replaced with a thin wrapper holding a `SessionPipeline`; its public API (`on_session_start(now)`, `on_1m_bar(now, mnq_bar_row, mes_bar_row)`) is preserved unchanged
- [ ] `automation/main.SmtV2Dispatcher` is identically replaced with a thin wrapper
- [ ] Slippage annotation on `market-close` / `market-entry` events still appears correctly in `run_backtest_v2` output after moving to the shared utility
- [ ] `levels.json` snapshot is still written by `run_backtest_v2` when `write_events=True` (it remains in the backtest, not moved, as it is not used by live paths)
- [ ] Module signatures (`run_hypothesis`, `run_daily`, etc.) are updated if doing so makes the code cleaner or more explicit (e.g., making `hist_1hr`/`hist_4hr` required rather than optional kwargs); any such changes are consistent across all callers

### Functional — V2 Live Dispatcher Wiring
- [ ] When `SMT_PIPELINE=v2`, `signal_smt.main()` instantiates `SmtV2Dispatcher()` and assigns it to `_smtv2_dispatcher`
- [ ] When `SMT_PIPELINE=v2`, `automation.main.main()` instantiates `SmtV2Dispatcher()` and assigns it to `_smtv2_dispatcher`
- [ ] `SmtV2Dispatcher.on_session_start(now, mnq_df, mes_df)` creates a `SessionPipeline` with the passed DFs as hist, and calls `pipeline.on_session_start` exactly once per trading day
- [ ] `SmtV2Dispatcher.on_session_start` is idempotent: calling it twice on the same date does not recreate the pipeline
- [ ] `SmtV2Dispatcher.on_session_start` resets the pipeline for a new trading day
- [ ] `SmtV2Dispatcher.on_1m_bar` returns without error when called before `on_session_start`
- [ ] `SmtV2Dispatcher.on_1m_bar` passes a today-only slice of the provided DFs to `SessionPipeline.on_1m_bar`
- [ ] `_on_bar_1m_complete` in both files calls `on_session_start` on the first bar at/after 09:20 ET and `on_1m_bar` on every subsequent bar when `_smtv2_dispatcher` is not None
- [ ] `tests/test_smt_v2_dispatcher.py` contains ≥ 8 test cases passing for both `signal_smt.SmtV2Dispatcher` and `automation.main.SmtV2Dispatcher`

### Integration / E2E
- [ ] `run_backtest_v2("2025-11-14", "2025-11-14", write_events=False)` returns a dict with keys `"trades"`, `"events"`, `"metrics"` (existing smoke test still passes)
- [ ] `legacy run_backtest(mnq_df, mes_df, start, end)` is left as-is and remains in the codebase (may become uncallable due to refactoring — that is acceptable)

### Validation
- [ ] `uv run python -c "from session_pipeline import SessionPipeline; print('OK')"` exits 0
- [ ] `uv run pytest tests/test_session_pipeline.py -v` — 15/15 (or more) tests pass
- [ ] `uv run pytest tests/test_smt_dispatch_order.py -v` — all tests pass
- [ ] `uv run pytest tests/test_smt_backtest.py tests/test_signal_smt.py tests/test_automation_main.py -v` — all pass
- [ ] `uv run pytest --tb=short -q` — ≥ 940 passed, 0 new failures vs. baseline

### Out of Scope
- `levels.json` — backtest-only artifact; stays in `run_backtest_v2`, not shared
- Legacy `run_backtest` function — left in place; no changes required even if it becomes uncallable
- Changes to `smt_state.py`, `strategy_smt.py`, `hypothesis_smt.py` — not part of this refactor
- Live order-routing logic inside `automation/main._emit_v2_signal` — routing stays per-file; only the common serialization/print logic moves to shared module

---

## TASK 4: Wire V2 Live Dispatcher in `signal_smt.py` and `automation/main.py`

**Status**: ✅ Implemented (added after initial execution — see note below)

**Background**: After Tasks 2.2 and 2.3 were completed, investigation revealed that `SmtV2Dispatcher` was defined in both files but never instantiated at runtime — `main()` read `SMT_PIPELINE` but did not create the dispatcher or wire it into `_on_bar_1m_complete`. Both files always ran V1 (`process_scan_bar` via `_on_bar` → `_process()`).

**Root cause**: `IbRealtimeSource` replaces `self._mnq_1m_df` with a new object on each bar (`pd.concat`), so a DF reference captured at dispatcher construction time goes stale immediately. The original `SmtV2Dispatcher.__init__(mnq_1m_df, mes_1m_df, ...)` design was therefore broken for live use.

**Fix implemented**:

1. **`SmtV2Dispatcher` redesigned** — DFs removed from `__init__`; passed at call time instead:
   - `__init__(self)` — no args; `_pipeline = None`, `_session_date = None`
   - `on_session_start(now, mnq_1m_df, mes_1m_df)` — creates `SessionPipeline` lazily with current history snapshot; idempotent per trading day
   - `on_1m_bar(now, mnq_bar_row, mes_bar_row, mnq_1m_df, mes_1m_df)` — guards before session start; passes today's slice to pipeline

2. **`main()` wiring** — when `SMT_PIPELINE=v2`:
   ```python
   _smtv2_dispatcher = SmtV2Dispatcher()
   ```

3. **`_on_bar_1m_complete` wiring** — after hypothesis block:
   ```python
   if _smtv2_dispatcher is not None:
       _mnq_df = _ib_source.mnq_1m_df   # fresh ref every bar
       _mes_df = _ib_source.mes_1m_df
       if bar_time >= _SESSION_DAILY_TRIGGER_TIME:  # 09:20 ET
           _smtv2_dispatcher.on_session_start(_bar_ts_v2, _mnq_df, _mes_df)
       _mnq_bar = _mnq_df.iloc[-1] if not _mnq_df.empty else pd.Series(dtype=float)
       _mes_bar = _mes_df.iloc[-1] if not _mes_df.empty else pd.Series(dtype=float)
       _smtv2_dispatcher.on_1m_bar(_bar_ts_v2, _mnq_bar, _mes_bar, _mnq_df, _mes_df)
   ```

4. **`_SESSION_DAILY_TRIGGER_TIME`** constant added to both files:
   ```python
   _SESSION_DAILY_TRIGGER_TIME = pd.Timestamp("2000-01-01 09:20").time()
   ```

5. **Tests**: `tests/test_smt_v2_dispatcher.py` — 16 tests (8 cases × 2 modules) covering:
   - init produces no-op pipeline
   - on_1m_bar before session_start is no-op
   - on_session_start creates pipeline and seeds ATH
   - on_session_start idempotent per day
   - on_session_start resets for new day
   - on_1m_bar delegates to pipeline
   - fresh DFs passed at call time (not stale stored refs)
   - today_mnq slice contains only today's bars

---

## COMPLETION CHECKLIST

- [x] `session_pipeline.py` created
- [x] `tests/test_session_pipeline.py` created with 15 tests
- [x] `backtest_smt.run_backtest_v2` refactored (inline logic removed, `SessionPipeline` used)
- [x] `signal_smt.SmtV2Dispatcher` replaced with thin wrapper
- [x] `automation/main.SmtV2Dispatcher` replaced with thin wrapper
- [x] `tests/test_smt_dispatch_order.py` patch targets verified/updated
- [x] `live_emit.py` created (shared emit module)
- [x] `_annotate_slippage` helper extracted in `backtest_smt.py`
- [x] V2 live dispatcher wired in `signal_smt.py` `main()` and `_on_bar_1m_complete`
- [x] V2 live dispatcher wired in `automation/main.py` `main()` and `_on_bar_1m_complete`
- [x] `tests/test_smt_v2_dispatcher.py` created with 16 tests (8 × 2 modules)
- [x] Level 1 (syntax) passes
- [x] Level 2 (unit) passes — 15/15 session_pipeline + 16/16 dispatcher
- [x] Level 3 (integration) passes
- [x] Level 4 (full suite) passes
- [x] No debug logs introduced during execution
- [x] **Changes UNSTAGED — NOT committed**

---

## NOTES

**Slippage (Issue #9)**: Intentionally not moved into `SessionPipeline`. Slippage is backtest accounting that adjusts simulated P&L; it is not a signal property and should not propagate to live paths. The backtest applies it post-hoc by annotating events in `day_events` after each `on_1m_bar` call.

**Levels snapshot**: The `write_events` levels.json write in `run_backtest_v2` stays in the backtest. It is a regression-visualisation artifact, not part of the dispatch pipeline.

**`_emit_v2_signal` stays per-file**: Each of the three callers has its own `_emit_v2_signal` function with different routing logic (stdout print for signal_smt, PickMyTrade for automation, list append for backtest). The callback pattern keeps this decoupled.

**Patch targets in tests**: `SessionPipeline` imports `daily`, `hypothesis`, `strategy`, `trend` at module level as `_daily_mod`, `_hyp_mod`, etc. Tests that monkeypatch these modules should patch `session_pipeline._daily_mod.run_daily` (etc.) OR patch the module objects directly (`monkeypatch.setattr(daily, "run_daily", fake)`), since Python module imports are shared references.

**Behavioral note on issue #4**: Changing `run_hypothesis` to receive all-day MNQ/MES (midnight to now) instead of session-only (09:20 to now) may cause hypothesis to see more bar history. This is the user's chosen direction and matches the live path's existing behavior. Run a backtest before/after the refactor and note any PnL delta as a sanity check.
