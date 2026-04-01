# Plan: SMT 5m Optimization Harness

## Overview

**Feature**: SMT Strategy — Switch optimizer from 1m to 5m bars  
**Type**: Enhancement  
**Complexity**: ⚠️ Medium  
**Status**: 📋 Planned  
**Date**: 2026-04-01

### Problem

`train_smt.py` runs on 1m bars, but IB-Gateway only provides ~14 days of 1m ContFuture history
(error 10339 for explicit `endDateTime`). This gives too few sessions for meaningful walk-forward
optimization. 5m bars support `durationStr="90 D"` via `endDateTime=''`, yielding ~3 months /
~40–60 kill-zone sessions.

### User Story

As a strategy developer, I want the optimizer to run on 5m bars so that I get 3 months of history
and full 6-fold walk-forward evaluation instead of a single degenerate fold.

### Scope

**Task A only** — optimizer update. Task B (real-time screener) is a separate feature.

---

## Pre-Execution Baseline

Before touching any file, run the full test suite and document the baseline:

```bash
cd C:\Users\gilad\projects\auto-co-trader\auto-co-trader-main
uv run pytest tests/ -q 2>&1 | tail -5
```

Expected baseline (from PROGRESS.md): **346 passed, 2 skipped**. Record the actual count.
Any pre-existing failures must be noted separately so they are not attributed to this feature.

---

## Execution Agent Rules

- Make ALL code changes required by the plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`

---

## Affected Files

| File | Change |
|------|--------|
| `prepare_futures.py` | `INTERVAL` → `"5m"`, `BACKTEST_START` → today − 90 days |
| `data/sources.py` | Make contfuture `_IB_CONTFUTURE_MAX_DAYS` cap 1m-only |
| `train_smt.py` | Add `_bars_for_minutes`, update `detect_smt_divergence` + `screen_session` |
| `tests/conftest.py` | Futures bootstrap manifest: `"fetch_interval"` → `"5m"` |
| `tests/test_smt_backtest.py` | Fixture dir + manifest + bar builders: 1m → 5m |

---

## Key Design Decisions (from clarification)

1. **`durationStr` format**: Use `f"{duration_days} D"` with `duration_days = requested_days`
   (i.e. `"90 D"`) for 5m contfuture — not the 1m cap.
2. **Cap logic**: Option A — apply `_IB_CONTFUTURE_MAX_DAYS` cap only when `interval == "1m"`.
   For all other intervals, use `requested_days` directly (no cap).
3. **`MIN_BARS_BEFORE_SIGNAL` refactor**: Keep constant name; its value (5) now means
   "5 minutes". Add `_bars_for_minutes(df, minutes) -> int` helper. `screen_session` computes
   `_min_bars = _bars_for_minutes(session, MIN_BARS_BEFORE_SIGNAL)` and passes it to
   `detect_smt_divergence` as new optional parameter `_min_bars`. At 1m: `_min_bars = 5`.
   At 5m: `_min_bars = 1`. Backward-compat: existing tests monkeypatch `MIN_BARS_BEFORE_SIGNAL`
   and call `detect_smt_divergence` without `_min_bars` → guard still reads global. ✓

---

## Implementation Tasks

### WAVE 1 — Code changes (all parallel)

#### Task 1.1 — `prepare_futures.py` (WAVE 1)
**AGENT_ROLE**: Code editor  
**DEPENDS_ON**: nothing

Changes:
1. Line 46: `INTERVAL = "1m"` → `INTERVAL = "5m"`
2. Line 40: `BACKTEST_START = (_TODAY - datetime.timedelta(days=14)).isoformat()` →
   `BACKTEST_START = (_TODAY - datetime.timedelta(days=90)).isoformat()`
3. Update module docstring (lines 1–18): replace "1m bars" references with "5m bars", update
   limitation note to reflect 90-day window instead of 7/14-day window, update cache path
   reference from `1m/` to `5m/`.

**Acceptance check**: `INTERVAL == "5m"`, `BACKTEST_START` is today − 90 days.

---

#### Task 1.2 — `data/sources.py` — interval-aware contfuture cap (WAVE 1)
**AGENT_ROLE**: Code editor  
**DEPENDS_ON**: nothing

In `IBGatewaySource.fetch()`, within the `if contract_type == "contfuture":` block (around
line 203–215), replace:

```python
requested_days = max(1, (end_dt - start_dt).days)
# Cap at _IB_CONTFUTURE_MAX_DAYS — larger requests reliably timeout
duration_days = min(requested_days, _IB_CONTFUTURE_MAX_DAYS)
```

with:

```python
requested_days = max(1, (end_dt - start_dt).days)
# _IB_CONTFUTURE_MAX_DAYS cap applies only to 1m — larger intervals
# support longer history windows via endDateTime='' without timing out.
if interval == "1m":
    duration_days = min(requested_days, _IB_CONTFUTURE_MAX_DAYS)
else:
    duration_days = requested_days
```

No other changes to `data/sources.py`.

**Acceptance check**: For interval="1m", cap is still 14. For interval="5m" with 90-day
request, `duration_days = 90`.

---

#### Task 1.3 — `train_smt.py` — `_bars_for_minutes` + `detect_smt_divergence` + `screen_session` (WAVE 1)
**AGENT_ROLE**: Code editor  
**DEPENDS_ON**: nothing

**Sub-task A: Update comment on `MIN_BARS_BEFORE_SIGNAL`**

At line 72–74, update the comment so it reads:
```python
# Minimum wall-clock minutes before a divergence signal can fire after session open.
# Converted to bar count at runtime by _bars_for_minutes() so it works at any interval.
# At 1m: 5 bars = 5 min. At 5m: 1 bar = 5 min.
MIN_BARS_BEFORE_SIGNAL = 5
```

**Sub-task B: Add `_bars_for_minutes` helper**

Insert the following function immediately before `def detect_smt_divergence` (currently around
line 112):

```python
def _bars_for_minutes(df: pd.DataFrame, minutes: int) -> int:
    """Return the number of bars spanning `minutes` wall-clock minutes.

    Infers bar size from the gap between the first two index entries.
    Falls back to 1 if the DataFrame has fewer than 2 rows or the gap is zero.
    """
    if len(df) < 2:
        return 1
    delta_secs = (df.index[1] - df.index[0]).total_seconds()
    if delta_secs <= 0:
        return 1
    bar_mins = delta_secs / 60
    return max(1, round(minutes / bar_mins))
```

**Sub-task C: Update `detect_smt_divergence` signature and guard**

Current signature (line 112):
```python
def detect_smt_divergence(
    mes_bars: pd.DataFrame,
    mnq_bars: pd.DataFrame,
    bar_idx: int,
    session_start_idx: int,
) -> str | None:
```

New signature — add optional `_min_bars` parameter:
```python
def detect_smt_divergence(
    mes_bars: pd.DataFrame,
    mnq_bars: pd.DataFrame,
    bar_idx: int,
    session_start_idx: int,
    _min_bars: int | None = None,
) -> str | None:
```

Update the guard (currently line 130):
```python
    if bar_idx - session_start_idx < MIN_BARS_BEFORE_SIGNAL:
```
→
```python
    _threshold = _min_bars if _min_bars is not None else MIN_BARS_BEFORE_SIGNAL
    if bar_idx - session_start_idx < _threshold:
```

Update docstring: add a line documenting the new parameter.

**Sub-task D: Update `screen_session` scan loop**

In `screen_session`, before the `for bar_idx in range(MIN_BARS_BEFORE_SIGNAL, n_bars):` loop
(line 255), add:
```python
    _min_bars = _bars_for_minutes(mnq_session, MIN_BARS_BEFORE_SIGNAL)
```

Change the loop start:
```python
    for bar_idx in range(MIN_BARS_BEFORE_SIGNAL, n_bars):
```
→
```python
    for bar_idx in range(_min_bars, n_bars):
```

Update the `detect_smt_divergence` call inside the loop to pass `_min_bars`:
```python
        direction = detect_smt_divergence(
            mes_session.reset_index(drop=True),
            mnq_session.reset_index(drop=True),
            bar_idx,
            0,
            _min_bars,
        )
```

**Sub-task E: Update `load_futures_data` docstring**

Line 94: `"""Load MNQ and MES 1m parquets from FUTURES_CACHE_DIR/1m/.` → 
`"""Load MNQ and MES futures parquets from FUTURES_CACHE_DIR/{interval}/.`

---

#### Task 1.4 — `tests/conftest.py` — futures bootstrap (WAVE 1)
**AGENT_ROLE**: Code editor  
**DEPENDS_ON**: nothing

In `pytest_configure` (lines ~186–197), the futures manifest bootstrap writes:
```python
"fetch_interval": "1m",
```
Change to:
```python
"fetch_interval": "5m",
```

This is the fallback manifest created on fresh machines where `prepare_futures.py` hasn't run.

---

#### Task 1.5 — `tests/test_smt_backtest.py` — update fixtures + bar builders (WAVE 1)
**AGENT_ROLE**: Code editor  
**DEPENDS_ON**: nothing

**Sub-task A: `futures_tmpdir` fixture**

Line 71: `interval_dir = cache_dir / "1m"` → `interval_dir = cache_dir / "5m"`
Line 82: `"fetch_interval": "1m",` → `"fetch_interval": "5m",`

**Sub-task B: `_build_short_signal_bars`**

Line 19: `freq="1min"` → `freq="5min"`

Signal geometry is preserved:
- bar 7 (09:35 ET at 5m): `mes_highs[7] = base + 30` (MES new high, MNQ does not follow)
- bar 8 (09:40 ET): `opens[8] = base + 2`, `closes[8] = base - 2` (bearish bar), `mnq_highs[8] = base + 6`
- Session window 09:00–10:30 → 18 bars at 5m; signal indices 7 and 8 are within range ✓
- `_min_bars = 1` at 5m, so scan starts at bar_idx=1; bar 7 is reachable ✓

**Sub-task C: `_build_long_signal_bars`**

Line 44: `freq="1min"` → `freq="5min"`

Same index geometry reasoning applies. ✓

**Sub-task D: `test_one_trade_per_day_max`**

Line 223: `freq="1min"` → `freq="5min"`

The test builds flat bars (no signal geometry), n=90. At 5m, 90 bars covers 09:00–16:30 but the
session slice still captures only 09:00–10:30 (18 bars). Test asserts `total_trades <= 1`, which
holds regardless. ✓

**Sub-task E: `test_fold_loop_smoke`**

Line 249: `freq="1min"` → `freq="5min"` and `periods=90` → `periods=18`

18 bars × 5min = 90 minutes = exactly the kill zone (09:00–10:30). This keeps the synthetic data
realistic without excess bars.

---

### WAVE 2 — Run test suite (sequential, after Wave 1)

#### Task 2.1 — Run full test suite (WAVE 2)
**AGENT_ROLE**: Validator  
**DEPENDS_ON**: Tasks 1.1–1.5

```bash
cd C:\Users\gilad\projects\auto-co-trader\auto-co-trader-main
uv run pytest tests/ -x -q 2>&1 | tail -20
```

**Expected**: All previously passing tests still pass (346 baseline). No new failures.

If any test fails related to `MIN_BARS_BEFORE_SIGNAL` or `detect_smt_divergence`, diagnose
whether the monkeypatch in `test_smt_strategy.py` is being bypassed:
- Tests that patch `MIN_BARS_BEFORE_SIGNAL = 2` and call `detect_smt_divergence` without
  `_min_bars` parameter should still use the patched value as the `_threshold` fallback. ✓
- The one test that patches to 5 and expects `bar_idx=2` to return None still works because
  `_threshold = MIN_BARS_BEFORE_SIGNAL = 5` (patched) and `2 - 0 = 2 < 5 → None`. ✓

---

### WAVE 3 — Live IB-Gateway validation (sequential, after Wave 2)

#### Task 3.1 — Download 5m data and run backtest (WAVE 3)
**AGENT_ROLE**: Validator  
**DEPENDS_ON**: Task 2.1, IB-Gateway running on localhost:4002

**Step 1**: Delete any existing 1m cache (stale data from previous runs):
```bash
# Remove old 1m parquets if they exist — fresh 5m download required
ls ~/.cache/autoresearch/futures_data/
```
If `1m/` or stale `5m/` files exist, note their presence. Do NOT delete them automatically
(user data). Instead, note that re-running `prepare_futures.py` will place new 5m parquets
in the `5m/` subdirectory alongside any existing files.

**Step 2**: Run `prepare_futures.py`:
```bash
uv run prepare_futures.py
```
Expected output:
- `MNQ: saved N bars to ~/.cache/autoresearch/futures_data/5m/MNQ.parquet`
- `MES: saved N bars to ~/.cache/autoresearch/futures_data/5m/MES.parquet`
- `Manifest written to ~/.cache/autoresearch/futures_data/futures_manifest.json`
- N should be substantially more than the previous ~6900 1m bars (expect ~2500–4000 5m bars
  for 90 calendar days)

If `prepare_futures.py` fails (IB timeout, connection refused): document the error, do NOT
proceed to Step 3. Report to user.

**Step 3**: Run the optimizer:
```bash
uv run train_smt.py
```
Expected indicators of success:
- Manifest loaded with `backtest_start` ~90 days ago
- `_compute_fold_params` does NOT reduce to 1 fold (3 months > 130 bdays threshold → full
  6-fold walk-forward runs)
- Walk-forward loop completes without errors
- Stats printed: `total_trades > 0`, fold breakdown visible

Capture the last ~40 lines of output and include in the execution report.

---

## Test Coverage Map

### New code paths

| Path | Test | Status |
|------|------|--------|
| `_bars_for_minutes(df, 5)` at 1m — returns 5 | `test_smt_strategy.py::test_detect_smt_bearish` (calls through `screen_session` indirectly via `run_backtest`) | ✅ Covered via integration |
| `_bars_for_minutes(df, 5)` at 5m — returns 1 | `test_smt_backtest.py::test_run_backtest_empty_data_returns_zero_trades` (passes 5m fixtures) | ✅ Covered |
| `_bars_for_minutes` with <2 bars (edge) | `test_smt_backtest.py::test_run_backtest_empty_data_returns_zero_trades` (empty DF) | ✅ Covered (fallback=1) |
| `detect_smt_divergence` with `_min_bars=None` fallback | All existing `test_smt_strategy.py` direct calls (no `_min_bars` arg) | ✅ Covered |
| `detect_smt_divergence` with `_min_bars=1` at 5m | `test_smt_backtest.py::test_run_backtest_long_trade_tp_hit` | ✅ Covered |
| `screen_session` computes `_min_bars` and passes to divergence | `test_smt_backtest.py::test_fold_loop_smoke` | ✅ Covered |
| `IBGatewaySource.fetch` contfuture branch — 5m, no cap | No unit test (requires live IB) | ⚠️ Manual — live IB only; cannot be unit-tested without real broker connection |
| `IBGatewaySource.fetch` contfuture branch — 1m, still capped at 14 | `tests/test_data_sources.py` existing ContFuture tests | ✅ Covered by existing tests |
| `prepare_futures.py` — INTERVAL="5m", BACKTEST_START=today-90 | Verified by `prepare_futures.py` live run (Task 3.1) | ⚠️ Manual — requires IB-Gateway |

### Existing code re-validated

| Area | Tests |
|------|-------|
| `detect_smt_divergence` guard logic (existing monkeypatch tests) | `test_smt_strategy.py` — 24 tests, all use `autouse` patch | ✅ |
| `screen_session` signal pipeline | `test_smt_backtest.py` — 10 integration tests | ✅ |
| `run_backtest` walk-forward | `test_fold_loop_smoke`, `test_metrics_shape` | ✅ |
| Futures manifest loading | `test_smt_backtest.py::futures_tmpdir` fixture | ✅ |
| ContFuture data source (existing tests) | `test_data_sources.py` | ✅ |

### Test Automation Summary

- **Automated**: 8 new/updated test targets across existing test files — 100% of automatable paths
- **Manual (2)**: Live IB-Gateway calls — automation-impossible without a real broker connection
  (mocking would not validate the actual IB API behavior that this feature depends on)
- **Gaps remaining**: None for code logic; IB API behavior is validated via live run in Task 3.1

---

## Risks & Mitigations

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| IB rejects `"90 D"` for 5m ContFuture (error/timeout) | Low — PROGRESS.md states IB supports `"3 M"` → `"90 D"` should work | Fall back to `"60 D"` (update `BACKTEST_START = today - 60`) |
| `_compute_fold_params` still triggers 1-fold path (< 130 bdays) | Low — 90 calendar days ≈ 65 bdays > 130 threshold? | Actually 65 < 130 — see note below |
| `detect_smt_divergence` tests break due to `_min_bars` parameter interaction | Low — backward-compat default `_min_bars=None` preserves monkeypatch behavior | Run test suite first; any failures are immediate |
| Stale 1m parquets in cache override fresh 5m download | Medium | `prepare_futures.py` reads from the `INTERVAL`-based subdir; 1m and 5m dirs are separate |

> **Note on fold threshold**: `_compute_fold_params` uses `< 130 bdays`. 90 calendar days ≈ 65
> business days — still below 130. This means the short-window path (1 fold) will still trigger
> for 90-day data. However the goal is 40–60 kill-zone sessions with more trades per fold, not
> necessarily 6 folds. The full 6-fold path requires > 130 business days (~6 months). The
> immediate improvement is in trade count per available period, not fold count. This is consistent
> with the PROGRESS.md comment that "40–60 kill-zone sessions → enough trades for walk-forward
> stats". The full 6-fold path can be unlocked by accumulating more data over time or lowering
> the `short_threshold`.

---

## Acceptance Criteria

- [ ] `prepare_futures.py` has `INTERVAL = "5m"` and `BACKTEST_START = today - 90 days`
- [ ] `data/sources.py` contfuture branch caps at `_IB_CONTFUTURE_MAX_DAYS` only when `interval == "1m"`; no cap for other intervals
- [ ] `train_smt.py` has `_bars_for_minutes` helper; `detect_smt_divergence` accepts `_min_bars` optional param; `screen_session` computes and passes `_min_bars`
- [ ] `tests/conftest.py` futures bootstrap writes `"fetch_interval": "5m"`
- [ ] `tests/test_smt_backtest.py` uses `freq="5min"` for all bar builders and `"5m"` subdir in fixture
- [ ] Full test suite passes with no new failures (baseline: 346 passed, 2 skipped)
- [ ] `uv run prepare_futures.py` completes with IB-Gateway active, writing 5m parquets
- [ ] `uv run train_smt.py` runs without error, printing backtest stats

---

## Interface Contracts Between Parallel Tasks

These are the output/input contracts that parallel Wave 1 tasks must honor so Wave 2 can run.

| Producer | Consumer | Contract |
|----------|----------|----------|
| Task 1.3 (`train_smt.py`) | Task 2.1 (test suite) | `detect_smt_divergence` still takes 4 positional args + 1 optional kwarg `_min_bars`. Monkeypatch of `MIN_BARS_BEFORE_SIGNAL` still controls fallback threshold. |
| Task 1.4 (`conftest.py`) | Task 2.1 (test suite) | `pytest_configure` writes a futures manifest with `"fetch_interval": "5m"`. `train_smt.load_futures_data()` reads `manifest["fetch_interval"]` to find the parquet subdirectory. |
| Task 1.5 (`test_smt_backtest.py`) | Task 2.1 (test suite) | `futures_tmpdir` fixture creates `cache_dir / "5m"` subdir and writes `"fetch_interval": "5m"` in manifest. `train_smt.load_futures_data()` will look in `5m/` subdir. |
| Task 1.1 (`prepare_futures.py`) | Task 3.1 (live run) | Writes parquets to `~/.cache/autoresearch/futures_data/5m/{ticker}.parquet` and manifest with `"fetch_interval": "5m"`. |
| Task 1.2 (`data/sources.py`) | Task 3.1 (live run) | ContFuture fetch for interval `"5m"` uses `duration_days = requested_days` (no 14-day cap). |

---

## Execution Notes

### Signal geometry preservation at 5m

`_build_short_signal_bars` and `_build_long_signal_bars` create bars starting at `09:00:00` with
`freq="5min"`. The session window `09:00–10:30` covers 18 bars at 5m (indices 0–17). Divergence
fires at bar index 7 (09:35), confirmation at index 8 (09:40) — both well within the 18-bar
window. `_min_bars = _bars_for_minutes(session_5m, 5) = max(1, round(5/5)) = 1`, so the scan
loop starts at index 1, reaching index 7 without issue.

At 1m (current), `_min_bars = max(1, round(5/1)) = 5`, which matches the previous hard-coded
`MIN_BARS_BEFORE_SIGNAL = 5`. The refactor is a no-op for existing 1m behavior.

### Why `test_smt_strategy.py` requires no changes

All 24 unit tests in `test_smt_strategy.py` call `detect_smt_divergence` **directly** (not
through `screen_session`) without the new `_min_bars` parameter. The `autouse` fixture patches
`train_smt.MIN_BARS_BEFORE_SIGNAL = 2`, and the guard reads `_threshold = _min_bars if _min_bars
is not None else MIN_BARS_BEFORE_SIGNAL`. Since `_min_bars` is `None` (not passed), `_threshold =
MIN_BARS_BEFORE_SIGNAL = 2` (the monkeypatched value). All assertions remain valid.

The one test that explicitly sets `MIN_BARS_BEFORE_SIGNAL = 5` (overriding the autouse patch):
`test_min_bars_guard_blocks_early_signal` calls with `bar_idx=2, session_start_idx=0` and expects
`None`. With the new code: `_threshold = 5`, `2 - 0 = 2 < 5 → None`. ✓

### `_bars_for_minutes` edge cases

- Empty DF (< 2 rows): returns 1 — matches the `n=0` path in `run_backtest` where empty
  DataFrames are passed; `screen_session` returns `None` before the scan loop anyway.
- Zero delta (pathological data): returns 1 — safe floor.
- Non-integer result: `round()` rounds half-to-even; at common intervals (1m, 2m, 5m, 15m, 30m),
  the result is always an exact integer since `5 / bar_mins` is a rational.

### `manage_position` and `run_backtest` (frozen section) — no changes needed

Both functions are resolution-agnostic: `manage_position` compares price levels (not timestamps),
and `run_backtest` iterates over whatever bars are in the session slice. Switching to 5m reduces
the bar count per session from ~90 to ~18, meaning fewer position management iterations, but the
logic is identical. No changes required.

### Cache directory isolation

`prepare_futures.py` writes to `CACHE_DIR/INTERVAL/{ticker}.parquet`. At 5m, this is
`~/.cache/autoresearch/futures_data/5m/`. Any existing 1m parquets in `1m/` are untouched.
`load_futures_data` reads the interval from the manifest, so it will correctly locate `5m/`
parquets after `prepare_futures.py` writes the updated manifest.

### Fold count reality check

With 90 calendar days of 5m data (~65 business days), `_compute_fold_params` will still take
the short-window path (< 130 bdays → 1 fold). The immediate gain is not fold count but **trade
count per fold**: 65 bdays × ~1 kill-zone signal per 2–3 days ≈ 20–30 trades vs. the previous
~5–10 from 14 days. This is sufficient for statistical validity of per-fold metrics. The 6-fold
path requires ~6 months of accumulated 5m data — achievable by running `prepare_futures.py`
daily and extending the date window manually.

---

## Parallel Execution Summary

```
Wave 1 (run in parallel — 5 agents):
  1.1  prepare_futures.py      — INTERVAL + BACKTEST_START
  1.2  data/sources.py         — interval-aware contfuture cap
  1.3  train_smt.py            — _bars_for_minutes + detect_smt_divergence + screen_session
  1.4  tests/conftest.py       — futures bootstrap manifest
  1.5  tests/test_smt_backtest.py — fixtures + bar builders

Wave 2 (sequential, after all of Wave 1):
  2.1  Run full test suite (uv run pytest tests/ -x -q)

Wave 3 (sequential, after Wave 2 passes, requires IB-Gateway):
  3.1  uv run prepare_futures.py + uv run train_smt.py
```

**Max speedup**: Wave 1 tasks are fully independent (different files, no shared state).
5 agents can complete Wave 1 in the time of the slowest single task.
**Total tasks**: 8 (5 parallel + 1 sequential + 1 manual + baseline pre-check)
