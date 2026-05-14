# Feature: Latency and I/O Optimization (HIGH/MEDIUM)

**⚠️ EXECUTION RULES — READ FIRST:**
- Implement ALL changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
- Only make code changes — no git operations

---

## Feature Description

Six targeted latency and I/O fixes for the live orchestrator, ordered by severity. The highest-impact
change offloads synchronous parquet writes off the IB event loop thread, eliminating a per-bar
blocking window that could cause missed ticks. The remaining fixes reduce startup I/O, eliminate
redundant disk reads per bar, and cut per-minute overhead in the data subscription layer.

## Problem Statement

1. **[HIGH] Synchronous parquet writes block the IB event loop** (`data/ib_realtime.py`):
   `_on_mnq_1m_bar` and `_on_mes_1m_bar` call `to_parquet()` synchronously on the IB callback
   thread. Every 1m bar, the IB event loop is blocked for the full disk write duration (10–500ms
   on a loaded SSD). During this window, tick callbacks are queued and partial-bar state is frozen,
   causing phantom slippage in the 1s accumulator.

2. **[MEDIUM] New Databento HTTP client on every `fetch()` call** (`data/sources.py`):
   `DatabentSource.fetch()` instantiates `db.Historical(key=self._api_key)` on every call.
   Each instantiation opens a new HTTP session, performs TLS handshake setup, and loads API
   key validation. During backfill (4 calls at startup: MNQ 1m, MES 1m, MNQ 1s, MES 1s), this
   creates 4 separate client objects that are each discarded after one request.

3. **[MEDIUM] Full parquet load just to read the last timestamp** (`data/databento_backfill.py`):
   `backfill_parquets` and `backfill_1s_parquets` call `_safe_read_parquet(path)` to get
   `existing.index[-1]`. For 1s parquets this loads ~1.7M rows (~70MB) just to check whether
   the file is already current. When it IS current (the common case), the data is immediately
   discarded. If not current, the full parquet is re-read anyway for the concat.

4. **[MEDIUM] `load_hypothesis()` / `load_position()` disk reads on every 1m bar** (`smt_state.py`):
   `session_pipeline.py:on_1m_bar` calls `load_hypothesis()` and `load_position()` at the top of
   every bar (lines 150–153). Each call reads a JSON file from disk. With 390 bars/session and
   4–6 load calls per bar, this is ~1,500–2,000 disk reads per session. On slow I/O (HDD, network
   share) these add measurable latency to bar processing; on SSD they are wasteful round trips.

5. **[LOW] Redundant IB seed callbacks process the same bars repeatedly** (`data/ib_realtime.py`):
   On IB subscription, `_on_mnq_1m_bar(bars, hasNewBar=False)` is called multiple times with the
   growing historical batch as IB delivers data incrementally. Each call passes the full accumulated
   `bars` list, not just new bars. `_seed_from_history` creates a new DataFrame from all bars and
   concats with the existing DF, even though successive calls contain almost all the same rows.
   This causes O(n²) dedup work during the 3-day historical seed phase.

6. **[LOW] `import strategy_smt` inside `_on_mnq_1m_bar` / `_on_mes_1m_bar`** (`data/ib_realtime.py`):
   Both bar callbacks do `from strategy_smt import set_bar_data` at the call site. After the first
   import, Python returns from `sys.modules` (O(1)), but the name lookup still runs on every bar.
   Hoisting to module level eliminates 780 `sys.modules` lookups per session with no downside.

## Solution Statement

- **Fix 1**: Wrap a `ThreadPoolExecutor(max_workers=1)` around all `to_parquet()` calls in
  `IbRealtimeSource`. Submit each write as a background task, holding a reference to the DF
  snapshot so it survives until the write completes. Drain the executor on `stop()`.
- **Fix 2**: Move `db.Historical(key=api_key)` from `DatabentSource.fetch()` into `__init__`,
  storing as `self._client`. All fetch calls reuse the same client.
- **Fix 3**: Add `_safe_read_last_ts(path)` that reads only the index via
  `pd.read_parquet(path, columns=[])`. Use it in `backfill_parquets` and `backfill_1s_parquets`
  for the "is this already current?" check, deferring the full read until a fetch is actually needed.
- **Fix 4**: Add a process-local write-through cache in `smt_state.py` for `hypothesis.json`.
  On `save_hypothesis`, update the cache. On `load_hypothesis`, return the cached value if valid.
  Position is excluded (executor process writes it externally; caching would cause stale reads).
- **Fix 5**: Track `_last_seed_bar_count` per instrument. In `_seed_from_history`, skip the entire
  body if `len(bars)` equals the count from the last call.
- **Fix 6**: Hoist `from strategy_smt import set_bar_data` to module level in `ib_realtime.py`.

## Feature Metadata

**Feature Type**: Performance / Latency
**Complexity**: Medium
**Primary Systems Affected**: `data/ib_realtime.py`, `data/sources.py`, `data/databento_backfill.py`, `smt_state.py`
**Breaking Changes**: None — all changes are internal to the data and state layers

---

## CONTEXT REFERENCES

### Files to Read Before Implementing

- `data/ib_realtime.py` — full file; `_on_mnq_1m_bar`, `_on_mes_1m_bar`, `_seed_from_history`,
  `start()`, `stop()`, `__init__` attribute declarations
- `data/sources.py` lines 161–250 — `DatabentSource.__init__` and `fetch()` method
- `data/databento_backfill.py` — full file; `backfill_parquets`, `backfill_1s_parquets`,
  `_safe_read_parquet`
- `smt_state.py` — full file; `_load`, `_atomic_write`, `load_hypothesis`, `save_hypothesis`,
  `_IN_MEMORY` flag, `_STORE` dict
- `session_pipeline.py` lines 119–160 — `on_1m_bar` call sites for `load_hypothesis`/`load_position`

### Key Observations

- `IbRealtimeSource` runs in a daemon thread; its callbacks execute on ib_insync's asyncio event
  loop. `to_parquet()` is not async-safe — it should never block the event loop.
- After Plan 1 (`live-session-ram-reduction.md`) trims `_mnq_1m_df` to 14 days, the DF passed
  to the background write still holds the full 30-day history (submitted before trim). This is
  correct: parquet preserves full history; in-memory DF is trimmed. Plan 2's Fix 1 is compatible
  with Plan 1.
- `smt_state._IN_MEMORY` mode (used by backtests) already bypasses disk I/O. The new hypothesis
  cache must not activate in `_IN_MEMORY` mode to avoid interfering with backtest isolation.
- `DatabentSource` is instantiated once per `backfill_parquets` / `backfill_1s_parquets` call.
  The singleton client lives for the duration of that function; no cross-call state is needed.
- `_safe_read_parquet` in `databento_backfill.py` has corruption-recovery logic (recreates empty
  on error). `_safe_read_last_ts` must NOT recreate the file — it's read-only and must not change
  disk state.
- `session_pipeline.py` is also used by backtests with `_IN_MEMORY = True`. The hypothesis cache
  is transparent to backtests because backtests use `smt_state._STORE` (in-memory), which is
  already O(1) dict access — cache adds no value but must not break the fast-path.

---

## PARALLEL EXECUTION STRATEGY

```
┌────────────────────────────────────────────────────────────────────────┐
│ WAVE 1 — Independent file edits (run all in parallel)                  │
├────────────────────────────────────────────────────────────────────────┤
│ Task 1.1: Background parquet writes — data/ib_realtime.py              │
│ Task 1.2: Databento singleton — data/sources.py                        │
│ Task 1.3: Parquet tail-read — data/databento_backfill.py              │
│ Task 1.4: Hypothesis cache — smt_state.py                              │
└────────────────────────────────────────────────────────────────────────┘
                            ↓
┌────────────────────────────────────────────────────────────────────────┐
│ WAVE 2 — ib_realtime.py low-priority fixes (ordered pass, same file)   │
├────────────────────────────────────────────────────────────────────────┤
│ Task 2.1: Seed dedup — _seed_from_history skip if bar count unchanged  │
│ Task 2.2: Import hoist — from strategy_smt import set_bar_data         │
└────────────────────────────────────────────────────────────────────────┘
                            ↓
┌────────────────────────────────────────────────────────────────────────┐
│ WAVE 3 — Tests (parallel)                                               │
├────────────────────────────────────────────────────────────────────────┤
│ Task 3.1: tests/test_ib_realtime.py — Fix 1, Fix 5, Fix 6             │
│ Task 3.2: tests/test_sources.py — Fix 2                                │
│ Task 3.3: tests/test_databento_backfill.py — Fix 3                     │
│ Task 3.4: tests/test_smt_state.py — Fix 4                              │
└────────────────────────────────────────────────────────────────────────┘
```

Tasks 1.1–1.4 edit different files and are fully independent.
Tasks 2.1–2.2 both edit `data/ib_realtime.py` — execute as one ordered agent pass after Wave 1.
Tasks 3.1–3.4 are fully independent — run in parallel.

---

## IMPLEMENTATION PLAN

### Phase 1: Background parquet writes (Fix 1)

#### Task 1.1: Offload `to_parquet()` to a single-worker executor

**File**: `data/ib_realtime.py`
**Severity**: HIGH

**Step 1 — Add executor to `__init__`** (after the last attribute declaration):

```python
# In __init__, after self._mes_1s_session_df declaration:
from concurrent.futures import ThreadPoolExecutor
self._parquet_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="parq")
```

**Step 2 — Replace the 1m parquet write in `_on_mnq_1m_bar`**:

Current (blocks event loop):
```python
self._mnq_1m_df.to_parquet(self._bar_data_dir / "MNQ_1m.parquet")
```

Replace with:
```python
_mnq_snap = self._mnq_1m_df          # capture reference before possible trim
_mnq_path = self._bar_data_dir / "MNQ_1m.parquet"
self._parquet_executor.submit(_mnq_snap.to_parquet, _mnq_path)
```

Note: `_mnq_snap` holds the pre-trim DF. After Fix 1 submits the write, Plan 1's trim
reassigns `self._mnq_1m_df`. The executor thread writes the old (full-history) DF — which is
correct. The old DF stays alive in memory until the future resolves.

**Step 3 — Replace the session 1s parquet write in `_on_mnq_1m_bar`**:

Current (after Plan 1's session reset was added):
```python
session_path = self._bar_data_dir / f"MNQ_1s_session_{self._session_date}.parquet"
self._mnq_1s_session_df.to_parquet(session_path)
self._mnq_1s_session_df = self._empty_bar_df()   # Plan 1 addition
```

Replace with:
```python
session_path = self._bar_data_dir / f"MNQ_1s_session_{self._session_date}.parquet"
_ses_snap = self._mnq_1s_session_df   # capture before clear
self._parquet_executor.submit(_ses_snap.to_parquet, session_path)
self._mnq_1s_session_df = self._empty_bar_df()   # Plan 1: clear after submission
```

**Step 4 — Apply the same replacement in `_on_mes_1m_bar`** (two writes: MES_1m.parquet and
MES_1s_session parquet). Pattern is identical; use `_mes_snap` as the snapshot variable name.

**Step 5 — Apply to `_seed_from_history`** (also writes to parquets synchronously):

```python
# Replace:
self._mnq_1m_df.to_parquet(self._bar_data_dir / "MNQ_1m.parquet")
# With:
_snap = self._mnq_1m_df
self._parquet_executor.submit(_snap.to_parquet, self._bar_data_dir / "MNQ_1m.parquet")
```
And identically for the MES branch.

**Step 6 — Drain executor on `stop()`**:

In `stop()`, after the existing `self._ib.disconnect()` block, add:
```python
try:
    self._parquet_executor.shutdown(wait=True)
except Exception:
    pass
```

`shutdown(wait=True)` blocks until all queued writes complete before the process exits, ensuring
no data is lost when the orchestrator stops.

**Validation**:
```
python -c "from data.ib_realtime import IbRealtimeSource; print('ok')"
```

---

### Phase 2: Databento singleton client (Fix 2)

#### Task 1.2: Reuse `db.Historical` across all `fetch()` calls

**File**: `data/sources.py`
**Severity**: MEDIUM
**Location**: `DatabentSource.__init__` and `DatabentSource.fetch()`

**Step 1 — Move client instantiation into `__init__`**:

Current `__init__`:
```python
def __init__(self) -> None:
    import os
    api_key = os.environ.get("DATABENTO_API_KEY")
    if not api_key:
        raise RuntimeError(
            "DATABENTO_API_KEY environment variable is required for DatabentSource"
        )
    self._api_key = api_key
```

Replace with:
```python
def __init__(self) -> None:
    import os
    import databento as db
    api_key = os.environ.get("DATABENTO_API_KEY")
    if not api_key:
        raise RuntimeError(
            "DATABENTO_API_KEY environment variable is required for DatabentSource"
        )
    self._api_key = api_key
    self._client = db.Historical(key=api_key)
```

**Step 2 — Remove client instantiation from `fetch()`** and replace all `client` references
with `self._client`:

Current line in `fetch()`:
```python
client = db.Historical(key=self._api_key)
data = client.timeseries.get_range(...)
```

Replace with:
```python
data = self._client.timeseries.get_range(...)
```

The retry block also uses `client.timeseries.get_range(...)` — replace with `self._client.timeseries.get_range(...)`.

Remove the `import databento as db` line that was inside `fetch()`.

**Step 3 — Keep `self._api_key`** — it's referenced in the retry-error log messages and may be
needed if the client is ever re-created (e.g., on auth failure). Do not remove it.

**Validation**:
```
python -c "from data.sources import DatabentSource; print('ok')"
```

---

### Phase 3: Parquet tail-read optimization (Fix 3)

#### Task 1.3: Read only the index when checking if a parquet is current

**File**: `data/databento_backfill.py`
**Severity**: MEDIUM

**Step 1 — Add `_safe_read_last_ts()` helper** after `_safe_read_parquet`:

```python
def _safe_read_last_ts(path: Path) -> "pd.Timestamp | None":
    """Return the last index value of a parquet without loading OHLCV columns.

    Returns None if the file is missing, empty, or corrupted.
    Does NOT recreate the file on corruption (read-only operation).
    """
    if not path.exists():
        return None
    try:
        idx_only = pd.read_parquet(path, columns=[])
        return idx_only.index[-1] if not idx_only.empty else None
    except Exception:
        return None
```

`pd.read_parquet(path, columns=[])` reads the parquet index metadata only, skipping all
OHLCV column data. For a 1.7M-row 1s parquet (~70MB), this reduces the read from ~70MB to
the index alone (~14MB), and returns in ~200ms instead of ~2s.

**Step 2 — Update `backfill_parquets`** to use the new helper for the current-check, reading
the full parquet only when a fetch is actually needed:

Current:
```python
existing = _safe_read_parquet(path)
last_bar = existing.index[-1] if not existing.empty else None
start_ts = max(last_bar + pd.Timedelta(minutes=1), floor) if last_bar is not None else floor
if start_ts >= cutoff:
    continue  # parquet already current — nothing to fetch
# ... use existing for concat
```

Replace with:
```python
last_bar = _safe_read_last_ts(path)
start_ts = max(last_bar + pd.Timedelta(minutes=1), floor) if last_bar is not None else floor
if start_ts >= cutoff:
    continue  # parquet already current — nothing to fetch
# Only read the full parquet when we actually need to concat new data
existing = _safe_read_parquet(path)
```

**Step 3 — Apply the same two-step pattern to `backfill_1s_parquets`**:

Current:
```python
existing = _safe_read_parquet(path)
last_bar = existing.index[-1] if not existing.empty else None
start_ts = max(last_bar + pd.Timedelta(seconds=1), floor) if last_bar is not None else floor
if last_bar is not None and start_ts >= now - pd.Timedelta(minutes=10):
    continue
# ... use existing for concat
```

Replace with:
```python
last_bar = _safe_read_last_ts(path)
start_ts = max(last_bar + pd.Timedelta(seconds=1), floor) if last_bar is not None else floor
if last_bar is not None and start_ts >= now - pd.Timedelta(minutes=10):
    continue
existing = _safe_read_parquet(path)
```

**Validation**:
```
python -c "from data.databento_backfill import backfill_parquets; print('ok')"
```

---

### Phase 4: Hypothesis read-through cache (Fix 4)

#### Task 1.4: Cache `hypothesis.json` reads in process memory

**File**: `smt_state.py`
**Severity**: MEDIUM

**Design constraints**:
- Must be transparent to `_IN_MEMORY` mode (backtests bypass disk entirely; no cache needed)
- Must NOT cache `position.json` — the executor process writes to it directly, and stale reads
  would cause the automation pipeline to miss order fills
- Must invalidate on every `save_hypothesis()` call
- Cache entries return `copy.deepcopy(cached)` so callers can mutate the dict safely

**Step 1 — Add cache variables** after the `_IN_MEMORY`/`_STORE` declarations:

```python
# Process-local hypothesis cache (invalidated on every save_hypothesis call).
# Not used in _IN_MEMORY mode; not used for position (externally mutated by executor).
_hyp_cache: dict | None = None
_hyp_cache_valid: bool = False
```

**Step 2 — Update `load_hypothesis()`**:

```python
def load_hypothesis() -> dict:
    global _hyp_cache, _hyp_cache_valid
    if not _IN_MEMORY and _hyp_cache_valid and _hyp_cache is not None:
        return copy.deepcopy(_hyp_cache)
    result = _load(HYPOTHESIS_PATH, DEFAULT_HYPOTHESIS)
    if not _IN_MEMORY:
        _hyp_cache = copy.deepcopy(result)
        _hyp_cache_valid = True
    return result
```

**Step 3 — Update `save_hypothesis()`**:

```python
def save_hypothesis(d: dict) -> None:
    global _hyp_cache, _hyp_cache_valid
    _atomic_write(HYPOTHESIS_PATH, d)
    if not _IN_MEMORY:
        _hyp_cache = copy.deepcopy(d)
        _hyp_cache_valid = True   # write-through: cache the new value immediately
```

Write-through (not just invalidate) means the next `load_hypothesis()` after a save returns the
cached value without a disk read — correct because the same process just wrote it.

**Step 4 — Add cache reset to `set_in_memory_mode()`**:

```python
def set_in_memory_mode(enabled: bool) -> None:
    global _IN_MEMORY, _hyp_cache, _hyp_cache_valid
    _IN_MEMORY = enabled
    _hyp_cache = None
    _hyp_cache_valid = False
    if not enabled:
        _STORE.clear()
```

This ensures that toggling in-memory mode (e.g., in test teardown) doesn't leave stale cache
entries that bleed into the next test.

**Validation**:
```
python -c "from smt_state import load_hypothesis, save_hypothesis; print('ok')"
```

---

### Phase 5: Seed dedup skip (Fix 5)

#### Task 2.1: Skip `_seed_from_history` when bar count hasn't changed

**File**: `data/ib_realtime.py`
**Severity**: LOW

**Step 1 — Add tracker to `__init__`** (after `self._parquet_executor`):

```python
self._last_seed_count: dict[str, int] = {"MNQ": 0, "MES": 0}
```

**Step 2 — Add early-return guard at the top of `_seed_from_history`**:

Current:
```python
def _seed_from_history(self, bars, instrument: str) -> None:
    """Bulk-populate df from IB's initial historical batch (hasNewBar=False)."""
    rows = []
    timestamps = []
    ...
```

Replace with:
```python
def _seed_from_history(self, bars, instrument: str) -> None:
    """Bulk-populate df from IB's initial historical batch (hasNewBar=False)."""
    if len(bars) == self._last_seed_count[instrument]:
        return   # callback fired with no new bars — skip redundant dedup work
    self._last_seed_count[instrument] = len(bars)
    rows = []
    timestamps = []
    ...
```

**Validation**:
```
python -c "from data.ib_realtime import IbRealtimeSource; print('ok')"
```

---

### Phase 6: Import hoist (Fix 6)

#### Task 2.2: Hoist `from strategy_smt import set_bar_data` to module level

**File**: `data/ib_realtime.py`
**Severity**: LOW

**Step 1 — Remove the inline imports** from `_on_mnq_1m_bar` and `_on_mes_1m_bar`:

In `_on_mnq_1m_bar`, remove:
```python
from strategy_smt import set_bar_data
set_bar_data(self._mnq_1m_df, self._mes_1m_df)
```
Replace the two lines with just:
```python
set_bar_data(self._mnq_1m_df, self._mes_1m_df)
```

In `_on_mes_1m_bar`, remove the same `from strategy_smt import set_bar_data` line.

**Step 2 — Add the import to the module-level lazy import block**. At the top of `ib_realtime.py`,
`ib_insync` is imported lazily inside `start()` to avoid triggering a connection at import time.
`strategy_smt` has no such constraint — it can be imported at module level.

Add after the existing module-level imports (after `import pandas as pd`):
```python
from strategy_smt import set_bar_data as _set_bar_data
```

Then in both callbacks replace `set_bar_data(...)` with `_set_bar_data(...)`.

If a circular import prevents module-level import (verify by running the validation below), use
a module-level lazy attribute instead:
```python
_set_bar_data = None  # populated on first use

def _get_set_bar_data():
    global _set_bar_data
    if _set_bar_data is None:
        from strategy_smt import set_bar_data
        _set_bar_data = set_bar_data
    return _set_bar_data
```
Then call `_get_set_bar_data()(mnq_df, mes_df)`. Only use the lazy fallback if a direct
module-level import raises `ImportError` or circular import.

**Validation**:
```
python -c "from data.ib_realtime import IbRealtimeSource; print('ok')"
```

**Wave 2 Checkpoint**:
```
python -c "from data.ib_realtime import IbRealtimeSource; print('ib_realtime ok')"
pytest tests/test_ib_realtime.py -x -q
```

---

### Phase 7: Tests

#### Task 3.1: ADD tests to `tests/test_ib_realtime.py`

**Append after the last existing test.**

**Tests for Fix 1 (background parquet writes)**:

1. **`test_parquet_write_submitted_to_executor_not_blocking`** — Create a source; replace
   `_parquet_executor` with a `MagicMock`; call `_on_mnq_1m_bar(mock_bars, True)`;
   assert `_parquet_executor.submit.called` is True; assert the submitted callable is
   `DataFrame.to_parquet` (not called synchronously). The absence of a direct `to_parquet()`
   call on the event loop thread verifies the offload.

2. **`test_executor_drained_on_stop`** — Create a source; replace `_parquet_executor` with
   a `MagicMock`; call `stop()`; assert `_parquet_executor.shutdown.called` is True with
   `wait=True`.

3. **`test_session_snap_used_not_current_df`** — Create a source; pre-populate `_mnq_1s_pending`
   with one row; patch `_parquet_executor.submit` to capture the first positional arg; call
   `_on_mnq_1m_bar(mock_bars, True)`; assert the captured first arg is the DataFrame object
   that existed BEFORE the session DF was cleared (not the empty DF).

**Tests for Fix 5 (seed dedup skip)**:

4. **`test_seed_skipped_when_bar_count_unchanged`** — Create source; set
   `_last_seed_count["MNQ"] = 5`; call `_seed_from_history([mock_bar]*5, "MNQ")`; assert
   `_mnq_1m_df` is unchanged (empty).

5. **`test_seed_runs_when_bar_count_increases`** — Create source; set
   `_last_seed_count["MNQ"] = 0`; call `_seed_from_history([mock_bar]*3, "MNQ")`; assert
   `len(_mnq_1m_df) == 3` and `_last_seed_count["MNQ"] == 3`.

**Tests for Fix 6 (import hoist)**:

6. **`test_set_bar_data_no_inline_import`** — Read `data/ib_realtime.py` as text; assert the
   string `"from strategy_smt import set_bar_data"` does NOT appear inside `_on_mnq_1m_bar`
   or `_on_mes_1m_bar` function bodies (it should only appear at module level or not at all
   if using the `_set_bar_data` alias).

**Run**: `pytest tests/test_ib_realtime.py -v -k "executor or seed_skipped or seed_runs or set_bar_data"`

---

#### Task 3.2: ADD tests to `tests/test_sources.py` (create if absent)

7. **`test_databento_client_instantiated_in_init`** — Patch `databento.Historical`; instantiate
   `DatabentSource()`; assert `databento.Historical` was called exactly once (during `__init__`).

8. **`test_databento_client_reused_across_fetch_calls`** — Patch `databento.Historical` returning
   a mock client; instantiate `DatabentSource()`; call `source.fetch(...)` twice; assert
   `databento.Historical` was called only once total (not once per fetch).

**Run**: `pytest tests/test_sources.py -v -k "databento_client"`

---

#### Task 3.3: ADD tests to `tests/test_databento_backfill.py` (create if absent)

9. **`test_safe_read_last_ts_returns_last_index`** — Write a small parquet to `tmp_path`;
   call `_safe_read_last_ts(path)`; assert the returned timestamp equals the last index value.

10. **`test_safe_read_last_ts_returns_none_for_missing_file`** — Call `_safe_read_last_ts`
    on a nonexistent path; assert returns `None`.

11. **`test_backfill_parquets_skips_full_read_when_current`** — Patch `_safe_read_parquet` to
    raise `AssertionError("full read should not happen")`; write a current parquet via
    `pd.read_parquet` mock returning a ts within cutoff; call `backfill_parquets(tmp_path)`;
    assert `_safe_read_parquet` was NOT called (only `_safe_read_last_ts` was called).

12. **`test_backfill_parquets_reads_full_parquet_when_stale`** — Make `_safe_read_last_ts`
    return a timestamp older than cutoff; assert `_safe_read_parquet` IS called for the full read.

**Run**: `pytest tests/test_databento_backfill.py -v -k "safe_read_last or backfill_parquets"`

---

#### Task 3.4: ADD tests to `tests/test_smt_state.py` (create if absent)

13. **`test_load_hypothesis_returns_cached_value`** — Call `save_hypothesis({"direction": "long", ...})`
    to populate cache; patch `smt_state._load` to raise `AssertionError("should use cache")`; call
    `load_hypothesis()`; assert no exception and returned dict has `"direction" == "long"`.

14. **`test_cache_invalidated_after_in_memory_toggle`** — Call `save_hypothesis({"direction": "short", ...})`;
    call `set_in_memory_mode(True)`; call `set_in_memory_mode(False)`; assert `_hyp_cache_valid` is
    False (cache was reset by toggle).

15. **`test_cache_not_used_in_in_memory_mode`** — Call `set_in_memory_mode(True)`; call
    `save_hypothesis({"direction": "long", ...})`; call `load_hypothesis()`; assert it reads from
    `_STORE`, not from `_hyp_cache` (verify by checking `_hyp_cache_valid` remains False).

16. **`test_position_not_cached`** — Call `load_position()` twice; verify `_hyp_cache_valid` is
    not set by position loads; position reads should always hit disk (or `_STORE` in in-memory mode).

**Run**: `pytest tests/test_smt_state.py -v -k "cached or cache_invalid or in_memory_mode"`

---

## STEP-BY-STEP TASKS

### WAVE 1 (run all four concurrently)

#### Task 1.1: Background parquet writes
- **WAVE**: 1
- **AGENT_ROLE**: data-engineer
- **DEPENDS_ON**: []
- **BLOCKS**: [3.1]
- **PROVIDES**: IB event loop unblocked from parquet writes; ~10–500ms per bar reclaimed
- **IMPLEMENT**: Phase 1 (Steps 1–6)
- **VALIDATE**: `python -c "from data.ib_realtime import IbRealtimeSource; print('ok')"`

#### Task 1.2: Databento singleton client
- **WAVE**: 1
- **AGENT_ROLE**: data-engineer
- **DEPENDS_ON**: []
- **BLOCKS**: [3.2]
- **PROVIDES**: 3 fewer TLS handshakes at startup; `self._client` reused across 4 fetches
- **IMPLEMENT**: Phase 2 (Steps 1–3)
- **VALIDATE**: `python -c "from data.sources import DatabentSource; print('ok')"`

#### Task 1.3: Parquet tail-read optimization
- **WAVE**: 1
- **AGENT_ROLE**: data-engineer
- **DEPENDS_ON**: []
- **BLOCKS**: [3.3]
- **PROVIDES**: Startup I/O reduced from ~140MB to ~28MB (index-only reads for already-current parquets)
- **IMPLEMENT**: Phase 3 (Steps 1–3)
- **VALIDATE**: `python -c "from data.databento_backfill import backfill_parquets; print('ok')"`

#### Task 1.4: Hypothesis read-through cache
- **WAVE**: 1
- **AGENT_ROLE**: data-engineer
- **DEPENDS_ON**: []
- **BLOCKS**: [3.4]
- **PROVIDES**: ~95% reduction in hypothesis.json disk reads per session (from ~1,500 to ~80)
- **IMPLEMENT**: Phase 4 (Steps 1–4)
- **VALIDATE**: `python -c "from smt_state import load_hypothesis, save_hypothesis; print('ok')"`

**Wave 1 Checkpoint**:
```
python -c "from data.ib_realtime import IbRealtimeSource; print('ib_realtime ok')"
python -c "from data.sources import DatabentSource; print('sources ok')"
python -c "from data.databento_backfill import backfill_parquets; print('backfill ok')"
python -c "from smt_state import load_hypothesis; print('smt_state ok')"
```

---

### WAVE 2 (ordered pass on ib_realtime.py)

#### Task 2.1: Seed dedup skip
- **WAVE**: 2
- **AGENT_ROLE**: data-engineer
- **DEPENDS_ON**: [1.1]
- **BLOCKS**: [3.1]
- **PROVIDES**: Eliminates O(n²) dedup during 3-day IB historical seed phase on reconnect
- **IMPLEMENT**: Phase 5 (Steps 1–2)
- **VALIDATE**: `python -c "from data.ib_realtime import IbRealtimeSource; print('ok')"`

#### Task 2.2: Import hoist
- **WAVE**: 2
- **AGENT_ROLE**: data-engineer
- **DEPENDS_ON**: [2.1]
- **BLOCKS**: [3.1]
- **PROVIDES**: Eliminates 780 `sys.modules` lookups per session
- **IMPLEMENT**: Phase 6 (Steps 1–2)
- **VALIDATE**: `python -c "from data.ib_realtime import IbRealtimeSource; print('ok')"`

**Wave 2 Checkpoint**:
```
pytest tests/test_ib_realtime.py -x -q
```

---

### WAVE 3 (all test tasks in parallel)

#### Task 3.1: ADD tests to tests/test_ib_realtime.py
- **WAVE**: 3
- **AGENT_ROLE**: test-engineer
- **DEPENDS_ON**: [1.1, 2.1, 2.2]
- **IMPLEMENT**: Tests 1–6 (Phase 7 Task 3.1)
- **VALIDATE**: `pytest tests/test_ib_realtime.py -v -k "executor or seed or set_bar_data"`

#### Task 3.2: ADD/CREATE tests/test_sources.py
- **WAVE**: 3
- **AGENT_ROLE**: test-engineer
- **DEPENDS_ON**: [1.2]
- **IMPLEMENT**: Tests 7–8 (Phase 7 Task 3.2)
- **VALIDATE**: `pytest tests/test_sources.py -v -k "databento_client"`

#### Task 3.3: ADD/CREATE tests/test_databento_backfill.py
- **WAVE**: 3
- **AGENT_ROLE**: test-engineer
- **DEPENDS_ON**: [1.3]
- **IMPLEMENT**: Tests 9–12 (Phase 7 Task 3.3)
- **VALIDATE**: `pytest tests/test_databento_backfill.py -v -k "safe_read_last or backfill_parquets"`

#### Task 3.4: ADD/CREATE tests/test_smt_state.py
- **WAVE**: 3
- **AGENT_ROLE**: test-engineer
- **DEPENDS_ON**: [1.4]
- **IMPLEMENT**: Tests 13–16 (Phase 7 Task 3.4)
- **VALIDATE**: `pytest tests/test_smt_state.py -v -k "cached or cache_invalid or in_memory_mode"`

**Wave 3 Checkpoint**:
```
pytest tests/ -x -q --ignore=tests/smoke_pmt_connection.py
```

---

## TESTING STRATEGY

| Test | File | Covers | Automated |
|------|------|--------|-----------|
| `test_parquet_write_submitted_to_executor_not_blocking` | test_ib_realtime.py | Fix 1 background submit | ✅ pytest |
| `test_executor_drained_on_stop` | test_ib_realtime.py | Fix 1 shutdown | ✅ pytest |
| `test_session_snap_used_not_current_df` | test_ib_realtime.py | Fix 1 snapshot reference | ✅ pytest |
| `test_seed_skipped_when_bar_count_unchanged` | test_ib_realtime.py | Fix 5 skip guard | ✅ pytest |
| `test_seed_runs_when_bar_count_increases` | test_ib_realtime.py | Fix 5 run when new bars | ✅ pytest |
| `test_set_bar_data_no_inline_import` | test_ib_realtime.py | Fix 6 import hoist | ✅ pytest |
| `test_databento_client_instantiated_in_init` | test_sources.py | Fix 2 singleton init | ✅ pytest |
| `test_databento_client_reused_across_fetch_calls` | test_sources.py | Fix 2 reuse | ✅ pytest |
| `test_safe_read_last_ts_returns_last_index` | test_databento_backfill.py | Fix 3 helper | ✅ pytest |
| `test_safe_read_last_ts_returns_none_for_missing_file` | test_databento_backfill.py | Fix 3 missing file | ✅ pytest |
| `test_backfill_parquets_skips_full_read_when_current` | test_databento_backfill.py | Fix 3 skip path | ✅ pytest |
| `test_backfill_parquets_reads_full_parquet_when_stale` | test_databento_backfill.py | Fix 3 fetch path | ✅ pytest |
| `test_load_hypothesis_returns_cached_value` | test_smt_state.py | Fix 4 cache hit | ✅ pytest |
| `test_cache_invalidated_after_in_memory_toggle` | test_smt_state.py | Fix 4 reset on toggle | ✅ pytest |
| `test_cache_not_used_in_in_memory_mode` | test_smt_state.py | Fix 4 in-memory bypass | ✅ pytest |
| `test_position_not_cached` | test_smt_state.py | Fix 4 position excluded | ✅ pytest |

**Manual tests**: None — all paths are unit-testable with mocking.

### Test Automation Summary

| | Count | % |
|---|---|---|
| ✅ Automated (pytest) | 16 | 100% |
| ⚠️ Manual | 0 | 0% |

---

## VALIDATION COMMANDS

```bash
# Syntax / import checks
python -c "from data.ib_realtime import IbRealtimeSource; print('ib_realtime ok')"
python -c "from data.sources import DatabentSource; print('sources ok')"
python -c "from data.databento_backfill import backfill_parquets; print('backfill ok')"
python -c "from smt_state import load_hypothesis, save_hypothesis; print('smt_state ok')"

# Targeted tests
pytest tests/test_ib_realtime.py -v -k "executor or seed or set_bar_data"
pytest tests/test_sources.py -v -k "databento_client"
pytest tests/test_databento_backfill.py -v -k "safe_read_last or backfill_parquets"
pytest tests/test_smt_state.py -v -k "cached or cache_invalid or in_memory_mode"

# Full regression
pytest tests/ -x -q --ignore=tests/smoke_pmt_connection.py
```

---

## ACCEPTANCE CRITERIA

- [ ] `IbRealtimeSource.__init__` creates a `ThreadPoolExecutor(max_workers=1)` stored as `_parquet_executor`
- [ ] `_on_mnq_1m_bar` does NOT call `DataFrame.to_parquet()` directly — all writes go via `_parquet_executor.submit()`
- [ ] `_on_mes_1m_bar` does NOT call `DataFrame.to_parquet()` directly — all writes go via `_parquet_executor.submit()`
- [ ] `_seed_from_history` does NOT call `DataFrame.to_parquet()` directly — writes go via `_parquet_executor.submit()`
- [ ] `IbRealtimeSource.stop()` calls `_parquet_executor.shutdown(wait=True)` before returning
- [ ] `DatabentSource.__init__` creates `self._client = db.Historical(key=api_key)`
- [ ] `DatabentSource.fetch()` uses `self._client.timeseries.get_range(...)` — no `db.Historical(...)` call inside `fetch()`
- [ ] `data/databento_backfill.py` has a `_safe_read_last_ts(path)` function that reads only the parquet index
- [ ] `backfill_parquets` calls `_safe_read_last_ts` for the currency check and `_safe_read_parquet` only when a fetch is needed
- [ ] `backfill_1s_parquets` applies the same two-step pattern
- [ ] `smt_state.py` has module-level `_hyp_cache` and `_hyp_cache_valid` variables
- [ ] `load_hypothesis()` returns the cached value without a disk read when `_hyp_cache_valid` is True and `_IN_MEMORY` is False
- [ ] `save_hypothesis()` updates `_hyp_cache` and sets `_hyp_cache_valid = True`
- [ ] `set_in_memory_mode()` resets `_hyp_cache_valid = False`
- [ ] `load_position()` is NOT cached (reads disk on every call)
- [ ] `IbRealtimeSource._seed_from_history` returns immediately when `len(bars) == _last_seed_count[instrument]`
- [ ] `from strategy_smt import set_bar_data` does NOT appear inside `_on_mnq_1m_bar` or `_on_mes_1m_bar` function bodies
- [ ] All 16 new pytest tests pass
- [ ] No regressions: `pytest tests/ -x -q --ignore=tests/smoke_pmt_connection.py` passes

---

## COMPLETION CHECKLIST

- [ ] `data/ib_realtime.py`: `__init__` has `_parquet_executor = ThreadPoolExecutor(max_workers=1, ...)` and `_last_seed_count` (Fix 1, Fix 5)
- [ ] `data/ib_realtime.py`: `_on_mnq_1m_bar` submits both 1m and session parquet writes to executor (Fix 1)
- [ ] `data/ib_realtime.py`: `_on_mes_1m_bar` submits both 1m and session parquet writes to executor (Fix 1)
- [ ] `data/ib_realtime.py`: `_seed_from_history` submits parquet writes to executor (Fix 1)
- [ ] `data/ib_realtime.py`: `stop()` calls `_parquet_executor.shutdown(wait=True)` (Fix 1)
- [ ] `data/ib_realtime.py`: `_seed_from_history` has bar-count guard at top (Fix 5)
- [ ] `data/ib_realtime.py`: `from strategy_smt import set_bar_data` hoisted to module level (Fix 6)
- [ ] `data/sources.py`: `DatabentSource.__init__` creates `self._client` (Fix 2)
- [ ] `data/sources.py`: `DatabentSource.fetch()` uses `self._client` throughout (Fix 2)
- [ ] `data/databento_backfill.py`: `_safe_read_last_ts()` function added (Fix 3)
- [ ] `data/databento_backfill.py`: `backfill_parquets` uses two-step read pattern (Fix 3)
- [ ] `data/databento_backfill.py`: `backfill_1s_parquets` uses two-step read pattern (Fix 3)
- [ ] `smt_state.py`: `_hyp_cache`, `_hyp_cache_valid` module variables added (Fix 4)
- [ ] `smt_state.py`: `load_hypothesis()` serves from cache when valid (Fix 4)
- [ ] `smt_state.py`: `save_hypothesis()` writes through to cache (Fix 4)
- [ ] `smt_state.py`: `set_in_memory_mode()` resets cache validity (Fix 4)
- [ ] `tests/test_ib_realtime.py`: 6 new tests added (Task 3.1)
- [ ] `tests/test_sources.py`: 2 new tests added/created (Task 3.2)
- [ ] `tests/test_databento_backfill.py`: 4 new tests added/created (Task 3.3)
- [ ] `tests/test_smt_state.py`: 4 new tests added/created (Task 3.4)
- [ ] All validation commands pass
- [ ] **⚠️ No debug logs committed**
- [ ] **⚠️ Changes UNSTAGED — NOT committed**

---

## NOTES

**Why `max_workers=1` for the parquet executor?**: Serial execution guarantees that writes for the
same file do not race. With `max_workers=2`, two concurrent writes to `MNQ_1m.parquet` could
corrupt the file. A single worker serializes all writes automatically.

**Why hold a snapshot reference before trimming (Fix 1)?**: After Plan 1 trims `_mnq_1m_df`,
`self._mnq_1m_df` points to a new (trimmed) object. The background write thread holds the only
remaining reference to the old (full-history) object via the `_mnq_snap` local. Once the write
completes, the executor releases the future and the old DF is garbage-collected. This is correct:
the parquet file always receives full history; only the in-memory DF is trimmed.

**Why not cache `position.json`?**: The `position.json` file is the IPC channel between automation
(writer) and executor (reader). The executor process also writes back fill confirmations and active
order state. Since each process has its own Python module cache, a write-through cache in automation
would not reflect executor writes — automation's cache would go stale. Caching position requires
a shared-memory or file-watch mechanism that is out of scope here.

**Fix 3 reads parquet twice when fetch is needed**: In the stale case, `_safe_read_last_ts` reads
the index (~14MB) and then `_safe_read_parquet` reads the full file (~70MB). Total reads: ~84MB
instead of ~70MB — slightly more I/O. But the common case (parquet already current) avoids the
full read entirely. Net across a typical startup: 4 parquet checks, ~0–1 stale → net savings
of 1–3 full reads (~70–210MB). This tradeoff is correct.

**Fix 6 circular import check**: `strategy_smt` imports `from data.ib_realtime import ...`? If
so, a module-level import would create a circular dependency. Verify with:
`python -c "import strategy_smt; from data.ib_realtime import IbRealtimeSource"`. If it raises
`ImportError`, use the lazy fallback described in Phase 6 Step 2.
