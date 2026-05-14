# Feature: Live Session RAM Reduction (CRITICAL)

**⚠️ EXECUTION RULES — READ FIRST:**
- Implement ALL changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
- Only make code changes — no git operations

---

## Feature Description

Four targeted fixes eliminating the dominant RAM growth patterns observed during live
orchestrator sessions. Each fix is independent; together they cap the session's peak
memory footprint and eliminate the unbounded O(n) per-second allocations that cause
RAM to blow up over a 6.5-hour session.

## Problem Statement

The orchestrator's RAM climbs steadily throughout a live session due to four compounding patterns:

1. **10 days of 1s historical data (~70 MB) loaded into `_mnq_1s_df`/`_mes_1s_df` and never freed** after the gap-fill that needs them completes.
2. **`_mnq_1s_session_df`/`_mes_1s_session_df` accumulate all session 1s bars (~23,400 rows over 6.5 hours) and are never cleared** after each minute's flush to parquet.
3. **`pd.concat` on 30-day DataFrames every 1m bar** — each concat allocates 3 full copies of the ~11,700-row DF simultaneously (old, new row, combined). No rolling trim means the DF grows past 30 days.
4. **`_session_mnq_rows` / `_session_mes_rows` (list of dicts) are rebuilt into a full DataFrame on every 1-second tick** — by end of session this is a 23,400-row DataFrame created 23,400 times (~547M row-operations just for the rebuild).

## Solution Statement

- **Task 1**: Free `_mnq_1s_df`/`_mes_1s_df` in `IbRealtimeSource.start()` immediately after `_gap_fill_1s_ib()` writes them to parquet — they are not used by the live signal path.
- **Task 2**: Clear `_mnq_1s_session_df`/`_mes_1s_session_df` to empty DataFrames after each minute-boundary parquet flush — the data is safely on disk.
- **Task 3**: After each `to_parquet()` write in `_on_mnq_1m_bar`/`_on_mes_1m_bar`, trim the in-memory `_mnq_1m_df`/`_mes_1m_df` to a 14-day rolling window. Full history is preserved on disk; 14 days is sufficient for all live strategy operations.
- **Task 4**: Replace the `_session_mnq_rows`/`_session_mes_rows` list-of-dicts with per-column Python lists in `automation/main.py`. Construct numpy arrays and DataFrames from column-major lists, which is ~10× faster and creates no dict overhead.

## Feature Metadata

**Feature Type**: Performance / Refactor
**Complexity**: Medium
**Primary Systems Affected**: `data/ib_realtime.py`, `automation/main.py`
**Breaking Changes**: None — all changes are internal to the data layer; no API surface changes

---

## CONTEXT REFERENCES

### Files to Read Before Implementing

- `data/ib_realtime.py` — full file; understand `start()`, `_gap_fill_1s_ib()`, `_on_mnq_1m_bar`, `_on_mes_1m_bar`, `_on_mnq_tick`, session DF attributes
- `automation/main.py` lines 100–570 — module-level state vars (`_session_mnq_rows`, `_session_mes_rows`, `_session_init_date`), `_process_scanning` function, session init block and bar accumulation

### Key Observations

- `_mnq_1s_df` / `_mes_1s_df` are only read in `_gap_fill_1s_ib()` (to get `start_dt = df.index[-1]`) and written back after gap-fill completes. They are exposed as properties but not consumed by `automation/main.py` or `session_pipeline.py` during live trading.
- `_mnq_1s_session_df` is appended to every minute via `pd.concat` and written to `MNQ_1s_session_*.parquet`. After `to_parquet()` the in-memory copy is redundant.
- `SessionPipeline._hist_mnq_1m` is set once at session start (09:20) from the full 30-day DF — trimming `_mnq_1m_df` AFTER that point is safe because ATH, liquidity, and hourly resamples were already computed from the complete history.
- `strategy_smt.set_bar_data` is called at the end of `_on_mnq_1m_bar` with the trimmed DF — this is fine; `set_bar_data` only needs recent data for entry signals and TDO computation (all within 14 days).
- `process_scan_bar` in `strategy_smt` receives both DataFrames (`mnq_reset`, `mes_reset`) AND numpy arrays. Constructing those DataFrames from per-column lists instead of list-of-dicts is much faster.

---

## PARALLEL EXECUTION STRATEGY

```
┌──────────────────────────────────────────────────────────────────┐
│ WAVE 1 — ib_realtime.py (run as a single ordered pass)           │
├──────────────────────────────────────────────────────────────────┤
│ Task 1.1: Free _mnq_1s_df/_mes_1s_df in start()                 │
│ Task 1.2: Clear session 1s DFs after each minute parquet flush   │
│ Task 1.3: Trim _mnq_1m_df/_mes_1m_df to 14-day rolling window   │
└──────────────────────────────────────────────────────────────────┘
                            ↓
┌──────────────────────────────────────────────────────────────────┐
│ WAVE 2 — automation/main.py                                       │
├──────────────────────────────────────────────────────────────────┤
│ Task 2.1: Replace list-of-dicts with per-column lists            │
└──────────────────────────────────────────────────────────────────┘
                            ↓
┌──────────────────────────────────────────────────────────────────┐
│ WAVE 3 — Tests (parallel)                                         │
├──────────────────────────────────────────────────────────────────┤
│ Task 3.1: tests/test_ib_realtime.py additions                    │
│ Task 3.2: tests/test_automation_main.py additions                │
└──────────────────────────────────────────────────────────────────┘
```

Tasks 1.1, 1.2, 1.3 all edit `data/ib_realtime.py` — execute as one ordered agent pass.
Tasks 3.1 and 3.2 are fully independent — run in parallel.

---

## IMPLEMENTATION PLAN

### Phase 1: ib_realtime.py — Three RAM fixes (single-agent ordered pass)

#### Task 1.1: Free historical 1s DFs after gap-fill

**File**: `data/ib_realtime.py`
**Location**: `start()` method, after the `self._gap_fill_1s_ib()` call

**Change**: After `_gap_fill_1s_ib()` returns, replace both historical 1s DFs with empty DataFrames.
`_gap_fill_1s_ib()` reads `df.index[-1]` from the DF and writes the combined result to parquet before returning — the in-memory copy is no longer needed.

```python
# In start(), after self._gap_fill_1s_ib():
self._mnq_1s_df = self._empty_bar_df()
self._mes_1s_df = self._empty_bar_df()
```

The `mnq_1s_df` / `mes_1s_df` properties will return empty DataFrames after this point.
Any external consumer (e.g. `plot_session.py`) that needs 1s history should read from the parquet
directly rather than from the live source.

**Validation**:
```
python -c "from data.ib_realtime import IbRealtimeSource; print('ok')"
```

---

#### Task 1.2: Clear session 1s DFs after each parquet flush

**File**: `data/ib_realtime.py`
**Locations**: `_on_mnq_1m_bar` (after `self._mnq_1s_session_df.to_parquet(session_path)`) and `_on_mes_1m_bar` (after `self._mes_1s_session_df.to_parquet(session_path)`)

**Change**: Reset to empty immediately after writing to disk. The session parquet is the durable record;
the in-memory accumulation DF has no further purpose until the next minute boundary.

In `_on_mnq_1m_bar`, replace the existing flush block:
```python
if self._mnq_1s_pending:
    rows = [[p["open"], p["high"], p["low"], p["close"], p["volume"]]
            for p in self._mnq_1s_pending]
    ts_list = [p["second_ts"] for p in self._mnq_1s_pending]
    new_1s = pd.DataFrame(
        rows, columns=["Open", "High", "Low", "Close", "Volume"],
        index=pd.DatetimeIndex(ts_list),
    )
    self._mnq_1s_session_df = pd.concat([self._mnq_1s_session_df, new_1s]).sort_index()
    self._mnq_1s_session_df = self._mnq_1s_session_df[
        ~self._mnq_1s_session_df.index.duplicated(keep="last")
    ]
    session_path = self._bar_data_dir / f"MNQ_1s_session_{self._session_date}.parquet"
    self._mnq_1s_session_df.to_parquet(session_path)
    self._mnq_1s_pending.clear()
```

With (add the reset line after `to_parquet`):
```python
if self._mnq_1s_pending:
    rows = [[p["open"], p["high"], p["low"], p["close"], p["volume"]]
            for p in self._mnq_1s_pending]
    ts_list = [p["second_ts"] for p in self._mnq_1s_pending]
    new_1s = pd.DataFrame(
        rows, columns=["Open", "High", "Low", "Close", "Volume"],
        index=pd.DatetimeIndex(ts_list),
    )
    self._mnq_1s_session_df = pd.concat([self._mnq_1s_session_df, new_1s]).sort_index()
    self._mnq_1s_session_df = self._mnq_1s_session_df[
        ~self._mnq_1s_session_df.index.duplicated(keep="last")
    ]
    session_path = self._bar_data_dir / f"MNQ_1s_session_{self._session_date}.parquet"
    self._mnq_1s_session_df.to_parquet(session_path)
    self._mnq_1s_session_df = self._empty_bar_df()   # ← free written rows
    self._mnq_1s_pending.clear()
```

Apply the same one-line addition (`self._mes_1s_session_df = self._empty_bar_df()`) to `_on_mes_1m_bar` after `self._mes_1s_session_df.to_parquet(session_path)`.

**Validation**:
```
pytest tests/test_ib_realtime.py -x -q
```

---

#### Task 1.3: Trim in-memory 1m DFs to 14-day rolling window after each write

**File**: `data/ib_realtime.py`
**Locations**: `_on_mnq_1m_bar` (after `to_parquet` for `MNQ_1m.parquet`) and `_on_mes_1m_bar` (after `to_parquet` for `MES_1m.parquet`)

**Rationale**: The parquet file holds the complete history. The in-memory DF only needs to cover what
the live strategy requires:
- `_find_last_liquidity` scans session bars only (from `today_mnq`, not from `_hist_mnq_1m`)
- `SessionPipeline._hist_1hr` uses 14-day window (set once at session start — not affected by trim)
- `compute_tdo`, `compute_midnight_open`, `compute_overnight_range` need today's bars only
- `strategy_smt.set_bar_data` uses recent bars for TDO and entry logic
- Maximum gap-fill window = 14 days (`GAP_FILL_MAX_DAYS`)

14 days covers all of the above with margin. The trim runs AFTER `to_parquet` so the full history
is always safely on disk before any in-memory rows are dropped.

**Change**: Add trim block after each `to_parquet` call in `_on_mnq_1m_bar`:

```python
# After self._mnq_1m_df.to_parquet(self._bar_data_dir / "MNQ_1m.parquet"):
_cutoff = pd.Timestamp.now(tz="America/New_York") - pd.Timedelta(days=14)
if not self._mnq_1m_df.empty and self._mnq_1m_df.index[0] < _cutoff:
    self._mnq_1m_df = self._mnq_1m_df[self._mnq_1m_df.index >= _cutoff].copy()
```

Apply identically in `_on_mes_1m_bar` after `self._mes_1m_df.to_parquet(...)`:
```python
_cutoff = pd.Timestamp.now(tz="America/New_York") - pd.Timedelta(days=14)
if not self._mes_1m_df.empty and self._mes_1m_df.index[0] < _cutoff:
    self._mes_1m_df = self._mes_1m_df[self._mes_1m_df.index >= _cutoff].copy()
```

The `.copy()` is required to break the view reference chain and allow the old large DF to be
garbage-collected promptly.

**Validation**:
```
pytest tests/test_ib_realtime.py -x -q
```

**Wave 1 Checkpoint**:
```
python -c "from data.ib_realtime import IbRealtimeSource; print('ib_realtime ok')"
pytest tests/test_ib_realtime.py -x -q
```

---

### Phase 2: automation/main.py — Replace list-of-dicts with per-column lists

#### Task 2.1: Per-column list accumulation in `_process_scanning`

**File**: `automation/main.py`
**Location**: Module-level state variables and `_process_scanning` function

**Problem**: Every 1-second tick calls `pd.DataFrame(_session_mnq_rows)` — constructing
a full DataFrame from a list of dicts. By end of session (~23,400 entries) this is O(n)
work per second. `pd.DataFrame(list_of_dicts)` is especially slow because pandas must
infer column order and types from dict keys on every call.

**Fix**: Replace `_session_mnq_rows` / `_session_mes_rows` (list of dicts) with
per-column Python lists. DataFrame and numpy array construction from column-major lists
is ~10× faster because pandas can directly copy list contents to numpy arrays without
key lookup or type inference.

**Step 1 — Replace module-level state declarations** (lines ~107–110):

Remove:
```python
_session_mnq_rows: list = []   # accumulated MNQ bar dicts for the current session
_session_mes_rows: list = []   # accumulated MES bar dicts for the current session
```

Add:
```python
# Per-column session bar accumulators (column-major; faster DF/array construction than list-of-dicts)
_mnq_o_vals: list = []
_mnq_h_vals: list = []
_mnq_l_vals: list = []
_mnq_c_vals: list = []
_mnq_v_vals: list = []
_mes_h_vals: list = []
_mes_l_vals: list = []
_mes_c_vals: list = []
```

**Step 2 — Update the session init block** inside `_process_scanning` where `today != _session_init_date`.

Replace the reset of `_session_mnq_rows` / `_session_mes_rows`:
```python
_session_mnq_rows = []
_session_mes_rows = []
```
With:
```python
_mnq_o_vals = []; _mnq_h_vals = []; _mnq_l_vals = []
_mnq_c_vals = []; _mnq_v_vals = []
_mes_h_vals = []; _mes_l_vals = []; _mes_c_vals = []
```

Replace the historical-bar pre-loading loop for MNQ (where `for _ts_h, _row_h in _mnq_1m_df[_hist_mask].iterrows():`):
```python
# Old: _session_mnq_rows.append({"Open": ..., "High": ..., ...})
# New: append to per-column lists
for _ts_h, _row_h in _mnq_1m_df[_hist_mask].iterrows():
    _mnq_o_vals.append(float(_row_h["Open"]))
    _mnq_h_vals.append(float(_row_h["High"]))
    _mnq_l_vals.append(float(_row_h["Low"]))
    _mnq_c_vals.append(float(_row_h["Close"]))
    _mnq_v_vals.append(float(_row_h.get("Volume", 0.0)))
    _v = float(_row_h["High"])
    # ... (smt_cache and run_ses updates unchanged)
```

Replace the MES pre-loading loop similarly (only H/L/C needed for MES arrays):
```python
for _ts_h, _row_h in _mes_1m_df[_mes_hist_mask].iterrows():
    _mes_h_vals.append(float(_row_h["High"]))
    _mes_l_vals.append(float(_row_h["Low"]))
    _mes_c_vals.append(float(_row_h["Close"]))
    # ... (smt_cache updates unchanged)
```

**Step 3 — Update the smt_cache update block** (step 5 in `_process_scanning`, before appending current bar).

The cache block currently reads from `_session_mnq_rows[-1]` and `_session_mes_rows[-1]`.
Replace all such accesses:
```python
# Old:
_prev = _session_mnq_rows[-1]
_v = float(_prev["High"])
# New:
if _mnq_h_vals:
    _v = _mnq_h_vals[-1]
    # (all four cache entries updated using _mnq_h_vals[-1], _mnq_l_vals[-1], _mnq_c_vals[-1])
```

Do the same for the MES cache block using `_mes_h_vals[-1]`, `_mes_l_vals[-1]`, `_mes_c_vals[-1]`.
Replace the guards `if not _session_just_inited and _session_mnq_rows:` with
`if not _session_just_inited and _mnq_h_vals:` (and likewise for MES).

**Step 4 — Update the bar append block** (step 6 in `_process_scanning`).

Replace:
```python
_session_mnq_rows.append({
    "Open": float(bar.Open), "High": float(bar.High),
    "Low": float(bar.Low), "Close": float(bar.Close), "Volume": float(bar.Volume),
})
_session_mes_rows.append({
    "Open": float(_mes_partial_1m["open"]), "High": float(_mes_partial_1m["high"]),
    "Low": float(_mes_partial_1m["low"]), "Close": float(_mes_partial_1m["close"]),
    "Volume": float(_mes_partial_1m["volume"]),
})
bar_idx = len(_session_mnq_rows) - 1
```

With:
```python
_mnq_o_vals.append(float(bar.Open))
_mnq_h_vals.append(float(bar.High))
_mnq_l_vals.append(float(bar.Low))
_mnq_c_vals.append(float(bar.Close))
_mnq_v_vals.append(float(bar.Volume))
_mes_h_vals.append(float(_mes_partial_1m["high"]))
_mes_l_vals.append(float(_mes_partial_1m["low"]))
_mes_c_vals.append(float(_mes_partial_1m["close"]))
bar_idx = len(_mnq_o_vals) - 1
```

**Step 5 — Replace the DataFrame / numpy array construction block** (step 7–8 in `_process_scanning`).

Replace:
```python
# 7. Build DataFrames from accumulated rows
mnq_reset = pd.DataFrame(_session_mnq_rows)
mes_reset = pd.DataFrame(_session_mes_rows)
_min_n = min(len(mnq_reset), len(mes_reset))
mnq_reset = mnq_reset.iloc[:_min_n].reset_index(drop=True)
mes_reset  = mes_reset.iloc[:_min_n].reset_index(drop=True)

# 8. Extract numpy arrays for process_scan_bar
_mnq_o = mnq_reset["Open"].values
_mnq_h = mnq_reset["High"].values
_mnq_l = mnq_reset["Low"].values
_mnq_c = mnq_reset["Close"].values
_mnq_v = mnq_reset["Volume"].values
_mes_h = mes_reset["High"].values
_mes_l = mes_reset["Low"].values
_mes_c = mes_reset["Close"].values
```

With:
```python
import numpy as _np  # add at top of function or module level

# 7+8. Build numpy arrays directly from per-column lists (O(n) but no dict overhead)
_min_n = min(len(_mnq_o_vals), len(_mes_h_vals))
_mnq_o = _np.asarray(_mnq_o_vals[:_min_n], dtype=_np.float64)
_mnq_h = _np.asarray(_mnq_h_vals[:_min_n], dtype=_np.float64)
_mnq_l = _np.asarray(_mnq_l_vals[:_min_n], dtype=_np.float64)
_mnq_c = _np.asarray(_mnq_c_vals[:_min_n], dtype=_np.float64)
_mnq_v = _np.asarray(_mnq_v_vals[:_min_n], dtype=_np.float64)
_mes_h = _np.asarray(_mes_h_vals[:_min_n], dtype=_np.float64)
_mes_l = _np.asarray(_mes_l_vals[:_min_n], dtype=_np.float64)
_mes_c = _np.asarray(_mes_c_vals[:_min_n], dtype=_np.float64)

# Build DataFrames from arrays (column-major; no dict-key inference)
mnq_reset = pd.DataFrame({
    "Open": _mnq_o, "High": _mnq_h, "Low": _mnq_l,
    "Close": _mnq_c, "Volume": _mnq_v,
})
mes_reset = pd.DataFrame({
    "Open": _np.zeros(_min_n),   # MES Open not used by process_scan_bar
    "High": _mes_h, "Low": _mes_l, "Close": _mes_c,
    "Volume": _np.zeros(_min_n),
})
```

**Note on `numpy` import**: `automation/main.py` already imports `math as _math`. Add
`import numpy as _np` at the top of the file alongside the other imports.

**Note on MES Open/Volume**: The MES DataFrame is passed to `process_scan_bar` but only
`High`, `Low`, `Close` columns are extracted as numpy arrays. Filling Open/Volume with zeros
is correct for this use case. If `process_scan_bar`'s signature changes to use MES Open or
Volume, this must be updated. Verify by checking `strategy_smt.process_scan_bar`'s
implementation for MES column access.

**Step 6 — Update `global` declarations** at the top of `_process_scanning`:

Replace `global _session_mnq_rows, _session_mes_rows` with:
```python
global _mnq_o_vals, _mnq_h_vals, _mnq_l_vals, _mnq_c_vals, _mnq_v_vals
global _mes_h_vals, _mes_l_vals, _mes_c_vals
```

**Validation**:
```
python -c "from automation.main import _mnq_o_vals; print('ok')"
```

**Wave 2 Checkpoint**:
```
python -c "from automation import main; print('automation/main.py ok')"
pytest tests/ -x -q --ignore=tests/smoke_pmt_connection.py
```

---

### Phase 3: Tests

#### Task 3.1: ADD tests to `tests/test_ib_realtime.py`

**Append after the last existing test**. Use the existing `_make_source` helper and mock patterns.

**Tests for Task 1.1 (free 1s DFs)**:

1. **`test_1s_dfs_freed_after_gap_fill_in_start`** — Create a source with non-empty `_mnq_1s_df`/`_mes_1s_df`; mock `_gap_fill_1s_ib` (no-op), `_load_parquets` (no-op), `_setup_subscriptions` (no-op), `util.run` (returns immediately), `IB.connect` (no-op); call `start()`; assert both `mnq_1s_df` and `mes_1s_df` are empty. Strategy: patch the IB setup to return immediately so `start()` completes without a real IB connection.

**Tests for Task 1.2 (clear session DFs)**:

2. **`test_session_1s_df_cleared_after_mnq_flush`** — Create source; pre-populate `_mnq_1s_pending` with two rows and `_mnq_1s_session_df` with one row; call `_on_mnq_1m_bar(mock_bars, True)` with `strategy_smt.set_bar_data` patched; assert `_mnq_1s_session_df` is empty after the call; assert session parquet was written with the pending rows.

3. **`test_session_1s_df_cleared_after_mes_flush`** — Symmetric for MES: pre-populate `_mes_1s_pending`; call `_on_mes_1m_bar(mock_bars, True)` with `strategy_smt.set_bar_data` patched; assert `_mes_1s_session_df` is empty.

**Tests for Task 1.3 (14-day trim)**:

4. **`test_mnq_1m_df_trimmed_to_14_days_after_bar`** — Create source; set `_mnq_1m_df` to a DataFrame containing bars 20 days old through today; call `_on_mnq_1m_bar(mock_bars, True)`; assert `_mnq_1m_df.index[0]` is within 14 days of now; assert parquet written before trim (parquet contains full history).

5. **`test_mes_1m_df_trimmed_to_14_days_after_bar`** — Symmetric for MES.

6. **`test_trim_does_not_run_when_all_bars_within_14_days`** — Set `_mnq_1m_df` to contain only today's bars; call `_on_mnq_1m_bar(mock_bars, True)`; assert `_mnq_1m_df` unchanged (no trim applied).

7. **`test_parquet_written_before_trim`** — Use `tmp_path`; call `_on_mnq_1m_bar` with a 25-day DF; read the written parquet; assert parquet contains the old bars (full history); assert in-memory DF contains only bars from last 14 days.

**Run**: `pytest tests/test_ib_realtime.py -v -k "freed_after or session_1s or trimmed"`

---

#### Task 3.2: ADD tests for Task 2.1 to `tests/test_automation_main.py` (create if absent)

If `tests/test_automation_main.py` does not exist, create it. Otherwise append.

8. **`test_session_accumulators_use_per_column_lists`** — Import `automation.main`; assert `_mnq_o_vals`, `_mnq_h_vals`, `_mnq_l_vals`, `_mnq_c_vals`, `_mnq_v_vals`, `_mes_h_vals`, `_mes_l_vals`, `_mes_c_vals` all exist as module-level attributes and are of type `list`; assert `_session_mnq_rows` and `_session_mes_rows` do NOT exist.

9. **`test_process_scanning_appends_to_column_lists`** — Set up minimal required module-level state (`_ib_source` mock with `mnq_1m_df`/`mes_1m_df`, `_session_init_date` = today, `_hypothesis_generated` = True, `_session_start_time`, `_session_end_time`, `_mes_partial_1m` with minute matching bar, `_smtv2_pipeline` = "v1"); patch `strategy_smt.process_scan_bar` to return None; call `_process_scanning(mock_bar, bar_ts, bar_ts.time())`; assert `len(_mnq_o_vals) == 1` and `_mnq_o_vals[0] == float(mock_bar.Open)`.

10. **`test_numpy_arrays_match_column_list_values`** — Populate `_mnq_o_vals` with known values; verify that after a subsequent `_process_scanning` call, the numpy array `_mnq_o` passed to `process_scan_bar` matches `np.asarray(_mnq_o_vals)`. Use `unittest.mock.call_args` capture.

**Run**: `pytest tests/test_automation_main.py -v`

---

## STEP-BY-STEP TASKS

### WAVE 1

#### Task 1.1: Free historical 1s DFs in start()
- **WAVE**: 1
- **AGENT_ROLE**: data-engineer
- **DEPENDS_ON**: []
- **BLOCKS**: [3.1]
- **PROVIDES**: `_mnq_1s_df`/`_mes_1s_df` freed ~70 MB after gap-fill
- **IMPLEMENT**: See Phase 1 Task 1.1
- **VALIDATE**: `python -c "from data.ib_realtime import IbRealtimeSource; print('ok')"`

#### Task 1.2: Clear session 1s DFs after flush
- **WAVE**: 1
- **AGENT_ROLE**: data-engineer
- **DEPENDS_ON**: []
- **BLOCKS**: [3.1]
- **PROVIDES**: `_mnq_1s_session_df`/`_mes_1s_session_df` stay at ~60-row size rather than growing to 23,400
- **IMPLEMENT**: See Phase 1 Task 1.2
- **VALIDATE**: `pytest tests/test_ib_realtime.py -x -q`

#### Task 1.3: Add 14-day rolling trim to _mnq_1m_df/_mes_1m_df
- **WAVE**: 1
- **AGENT_ROLE**: data-engineer
- **DEPENDS_ON**: []
- **BLOCKS**: [3.1]
- **PROVIDES**: Each pd.concat operates on a 14-day DF instead of 30-day; each temporary allocation halved
- **IMPLEMENT**: See Phase 1 Task 1.3
- **VALIDATE**: `pytest tests/test_ib_realtime.py -x -q`

**Wave 1 Checkpoint**: `pytest tests/test_ib_realtime.py -x -q`

---

### WAVE 2

#### Task 2.1: Replace list-of-dicts with per-column lists
- **WAVE**: 2
- **AGENT_ROLE**: data-engineer
- **DEPENDS_ON**: []
- **BLOCKS**: [3.2]
- **PROVIDES**: O(n²) per-session tick rebuild eliminated; numpy arrays constructed from column-major lists
- **IMPLEMENT**: See Phase 2 Task 2.1 (Steps 1–6)
- **VALIDATE**: `python -c "from automation import main; print('ok')"`

**Wave 2 Checkpoint**: `pytest tests/ -x -q --ignore=tests/smoke_pmt_connection.py`

---

### WAVE 3

#### Task 3.1: ADD tests to tests/test_ib_realtime.py
- **WAVE**: 3
- **AGENT_ROLE**: test-engineer
- **DEPENDS_ON**: [1.1, 1.2, 1.3]
- **IMPLEMENT**: Tests 1–7 (see Phase 3 Task 3.1)
- **VALIDATE**: `pytest tests/test_ib_realtime.py -v -k "freed_after or session_1s or trimmed"`

#### Task 3.2: ADD/CREATE tests/test_automation_main.py
- **WAVE**: 3
- **AGENT_ROLE**: test-engineer
- **DEPENDS_ON**: [2.1]
- **IMPLEMENT**: Tests 8–10 (see Phase 3 Task 3.2)
- **VALIDATE**: `pytest tests/test_automation_main.py -v`

**Wave 3 Checkpoint**: `pytest tests/ -x -q --ignore=tests/smoke_pmt_connection.py`

---

## TESTING STRATEGY

| Test | File | Covers | Automated |
|------|------|--------|-----------|
| `test_1s_dfs_freed_after_gap_fill_in_start` | test_ib_realtime.py | Task 1.1 | ✅ pytest |
| `test_session_1s_df_cleared_after_mnq_flush` | test_ib_realtime.py | Task 1.2 MNQ | ✅ pytest |
| `test_session_1s_df_cleared_after_mes_flush` | test_ib_realtime.py | Task 1.2 MES | ✅ pytest |
| `test_mnq_1m_df_trimmed_to_14_days_after_bar` | test_ib_realtime.py | Task 1.3 MNQ | ✅ pytest |
| `test_mes_1m_df_trimmed_to_14_days_after_bar` | test_ib_realtime.py | Task 1.3 MES | ✅ pytest |
| `test_trim_does_not_run_when_all_bars_within_14_days` | test_ib_realtime.py | Task 1.3 edge | ✅ pytest |
| `test_parquet_written_before_trim` | test_ib_realtime.py | Task 1.3 ordering | ✅ pytest |
| `test_session_accumulators_use_per_column_lists` | test_automation_main.py | Task 2.1 state vars | ✅ pytest |
| `test_process_scanning_appends_to_column_lists` | test_automation_main.py | Task 2.1 append | ✅ pytest |
| `test_numpy_arrays_match_column_list_values` | test_automation_main.py | Task 2.1 array correctness | ✅ pytest |

**Manual tests**: None — all paths are unit-testable with mocking.

### Test Automation Summary

| | Count | % |
|---|---|---|
| ✅ Automated (pytest) | 10 | 100% |
| ⚠️ Manual | 0 | 0% |

---

## VALIDATION COMMANDS

```bash
# Syntax / import check
python -c "from data.ib_realtime import IbRealtimeSource; print('ib_realtime ok')"
python -c "from automation import main; print('automation/main ok')"

# Unit tests — targeted
pytest tests/test_ib_realtime.py -v -k "freed_after or session_1s or trimmed"
pytest tests/test_automation_main.py -v

# Full regression
pytest tests/ -x -q --ignore=tests/smoke_pmt_connection.py
```

---

## ACCEPTANCE CRITERIA

- [ ] After `IbRealtimeSource.start()` calls `_gap_fill_1s_ib()`, both `_mnq_1s_df` and `_mes_1s_df` are reset to empty DataFrames; `mnq_1s_df` and `mes_1s_df` properties return empty DFs for the remainder of the session
- [ ] After each minute-boundary flush in `_on_mnq_1m_bar`, `_mnq_1s_session_df` is reset to an empty DataFrame (session parquet file is preserved on disk)
- [ ] After each minute-boundary flush in `_on_mes_1m_bar`, `_mes_1s_session_df` is reset to an empty DataFrame
- [ ] In `_on_mnq_1m_bar`, after `to_parquet()`, if `_mnq_1m_df.index[0]` is older than 14 days, the DF is trimmed to the last 14 days using `.copy()` to break the view chain
- [ ] In `_on_mes_1m_bar`, after `to_parquet()`, same 14-day trim applied to `_mes_1m_df`
- [ ] Parquet files always contain full history (trim only affects in-memory copy)
- [ ] `automation/main.py` has no `_session_mnq_rows` or `_session_mes_rows` variables
- [ ] `automation/main.py` has per-column lists: `_mnq_o_vals`, `_mnq_h_vals`, `_mnq_l_vals`, `_mnq_c_vals`, `_mnq_v_vals`, `_mes_h_vals`, `_mes_l_vals`, `_mes_c_vals`
- [ ] `_process_scanning` appends scalar floats to per-column lists on every bar
- [ ] `process_scan_bar` receives numpy arrays constructed via `np.asarray(col_list)` from per-column lists
- [ ] `process_scan_bar` receives DataFrames constructed from column-major dict (not from list of dicts)
- [ ] All 10 new pytest tests pass
- [ ] No regressions: `pytest tests/ -x -q --ignore=tests/smoke_pmt_connection.py` passes

---

## COMPLETION CHECKLIST

- [ ] `data/ib_realtime.py`: `start()` frees `_mnq_1s_df`/`_mes_1s_df` after `_gap_fill_1s_ib()` (Task 1.1)
- [ ] `data/ib_realtime.py`: `_on_mnq_1m_bar` resets `_mnq_1s_session_df` to empty after `to_parquet` (Task 1.2)
- [ ] `data/ib_realtime.py`: `_on_mes_1m_bar` resets `_mes_1s_session_df` to empty after `to_parquet` (Task 1.2)
- [ ] `data/ib_realtime.py`: `_on_mnq_1m_bar` trims `_mnq_1m_df` to 14-day window after `to_parquet` (Task 1.3)
- [ ] `data/ib_realtime.py`: `_on_mes_1m_bar` trims `_mes_1m_df` to 14-day window after `to_parquet` (Task 1.3)
- [ ] `automation/main.py`: `_session_mnq_rows` / `_session_mes_rows` removed; replaced with 8 per-column lists (Task 2.1)
- [ ] `automation/main.py`: session init block updated to append to per-column lists (Task 2.1)
- [ ] `automation/main.py`: smt_cache update uses per-column list indexing (Task 2.1)
- [ ] `automation/main.py`: numpy arrays constructed via `np.asarray` from column lists (Task 2.1)
- [ ] `automation/main.py`: DataFrames constructed from column-major dict (Task 2.1)
- [ ] `automation/main.py`: `import numpy as _np` added at module level (Task 2.1)
- [ ] `tests/test_ib_realtime.py`: 7 new tests added (Tasks 3.1)
- [ ] `tests/test_automation_main.py`: 3 new tests added/created (Task 3.2)
- [ ] All validation commands pass
- [ ] **⚠️ No debug logs committed**
- [ ] **⚠️ Changes UNSTAGED — NOT committed**

---

## NOTES

**Why not free `_mnq_1s_df` in `_load_parquets()`?**: `_load_parquets()` runs before
`_gap_fill_1s_ib()`. The gap-fill reads `_mnq_1s_df.index[-1]` to determine the fill start
timestamp. Freeing before gap-fill would break this lookup.

**Why `.copy()` in the trim?**: A boolean index operation (`df[mask]`) returns a pandas view
in many cases. The view holds a reference to the underlying numpy array of the original large DF,
preventing GC even though `_mnq_1s_df` has been reassigned. `.copy()` creates an independent
new array and releases the old one.

**Session pipeline ATH safety**: `SessionPipeline._hist_mnq_1m` is set at `on_session_start`
(09:20 ET) from the full pre-trim DF. Trimming begins on the first bar AFTER session start.
ATH, hourly resamples, and liquidity levels are all computed before any trim runs — safe.

**MES Open/Volume in the new DataFrame**: `process_scan_bar` only extracts MES High, Low, Close
as numpy arrays. Filling Open and Volume with zeros is safe for the current implementation.
If `process_scan_bar` is later extended to use MES Open or Volume, the per-column lists must
be extended to track those fields too (straightforward addition following the same pattern).
