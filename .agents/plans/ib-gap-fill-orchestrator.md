# Feature: IB Gap-Fill + Orchestrator Disconnect Handling

**⚠️ EXECUTION RULES — READ FIRST:**
- Implement ALL changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
- Only make code changes — no git operations

Validate documentation and codebase patterns before implementing. Match naming of existing utils, types, and models. Import from correct files.

---

## Feature Description

Two orthogonal improvements to the data-pipeline and orchestrator:

**1. Pre-session data backfill (moved out of strategy startup)**
Currently `IbRealtimeSource._gap_fill()` runs inside `start()` but consistently returns 0 bars
because the parquets already contain overnight data from the previous session's IB seed, and
`IBGatewaySource` fails to return bars for the current incomplete trading session. The IB seed
(`reqHistoricalData durationStr="3 D" keepUpToDate=True`) then correctly fills the last 3 days
when the strategy actually connects. The _gap_fill logic is therefore dead weight that runs at
the wrong time.

The fix: remove `_gap_fill()` from `IbRealtimeSource.start()` and replace it with a
**two-tier pre-session backfill** that runs at orchestrator startup:
- **Databento tier** fills parquets for all data older than 2 days (reliable, no active-session
  limitation)
- **IB seed tier** (unchanged) fills the most recent 3 days when the strategy connects

**2. IB Gateway disconnect detection and orchestrator shutdown**
IB Gateway disconnects silently after ~12–24 hours. Currently `IbRealtimeSource` retries
indefinitely on all errors; there is no way for the orchestrator to distinguish a transient
network glitch from a permanent gateway shutdown. When the gateway shuts down, the automation
subprocess hangs in the retry loop indefinitely.

The fix: detect gateway-initiated disconnects via `ib.disconnectedEvent`, raise a dedicated
`IbGatewayDisconnectedError` that bypasses the retry loop, exit the subprocess with code 2,
and have the orchestrator catch that exit code, close all open positions, log a user-facing
alert, and exit (so the operator restarts IB Gateway and relaunches the orchestrator).

## User Story

As the automated trading system operator
I want the orchestrator to keep bar data current before the strategy starts AND detect IB
Gateway shutdowns promptly
So that (a) the strategy never misses a prior-session bar, and (b) IB disconnects are surfaced
immediately rather than silently hanging the system

## Problem Statement

1. `IbRealtimeSource._gap_fill()` always returns 0 bars because the parquets already contain
   overnight bars from the prior IB seed. The parquet's `last_bar_ts >= today_midnight`, so
   the gap-fill range is `midnight → now`, which IBGatewaySource cannot service for an
   active trading session.
2. There is no mechanism for the orchestrator to know when IB Gateway has been shut down. The
   retry loop retries up to 10 times but with a 15-second delay between attempts; after 10
   failures the subprocess exits with a generic RuntimeError (exit code 1), and the orchestrator
   restarts it once, which also fails, and then waits silently until session end.

## Solution Statement

- Extract Databento rolling-window backfill into `data/databento_backfill.py` and call it
  from the orchestrator at startup (pre-session, before strategy launch).
- Remove the IB gap-fill call from `IbRealtimeSource.start()` — the IB 3-day seed already
  covers this and is more reliable.
- Add `IbGatewayDisconnectedError` to `data/ib_realtime.py`; hook `ib.disconnectedEvent` to
  detect gateway-initiated disconnects; raise this error instead of retrying.
- `automation/main.py` catches `IbGatewayDisconnectedError` and calls `sys.exit(2)`.
- `orchestrator/process.py` maps exit code 2 → `"ib_disconnected"` return value from
  `run_session()`.
- `orchestrator/main.py` handles `"ib_disconnected"` by closing any open position, logging
  a plain-text alert, and calling `sys.exit(3)` so the operator knows to restart IB and the
  orchestrator.

## Feature Metadata

**Feature Type**: Enhancement + Bug Fix
**Complexity**: Medium
**Primary Systems Affected**: `data/ib_realtime.py`, `data/databento_backfill.py` (new),
`orchestrator/main.py`, `orchestrator/process.py`, `automation/main.py`
**Dependencies**: `databento` (already in requirements), `ib_insync` (already used)
**Breaking Changes**: No — existing behavior preserved; startup now runs Databento backfill
first (graceful no-op if `DATABENTO_API_KEY` not set)

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ BEFORE IMPLEMENTING

- `data/ib_realtime.py` (lines 65–99, 245–280) — current `_gap_fill()` and `start()` to modify
- `data/sources.py` (full) — `DatabentSource.fetch()` signature and Databento symbol convention
- `prepare_futures_1m.py` (full) — existing Databento downloader pattern to reuse
- `automation/main.py` (lines 40–69, 1056–1073) — IB constants, `BAR_DATA_DIR`, `start()` call site
- `orchestrator/main.py` (full) — `run()` loop, `_close_session_position()`, where to add pre-session init
- `orchestrator/process.py` (full) — `_monitor()`, `run_session()` to modify for exit code 2
- `tests/test_ib_realtime.py` — existing IbRealtimeSource tests (must not regress)
- `tests/test_orchestrator_process.py` — existing ProcessManager tests (must not regress)
- `tests/test_orchestrator_main.py` — existing orchestrator main tests (must not regress)

### New Files to Create

- `data/databento_backfill.py` — rolling-window Databento parquet backfill
- `tests/test_databento_backfill.py` — unit tests for backfill logic

### Patterns to Follow

**Databento fetch pattern** (`data/sources.py`): `DatabentSource().fetch(symbol, start_iso, end_iso, interval="1m")` → `pd.DataFrame | None`. Symbols use `.v.0` suffix for continuous front-month: `"MNQ.v.0"`, `"MES.v.0"`. Dataset `"GLBX.MDP3"`.

**Parquet read/write pattern** (`data/ib_realtime.py` lines 62–98): read with `pd.read_parquet(path)`, write with `df.to_parquet(path)`, deduplicate with `df[~df.index.duplicated(keep="last")]`.

**Disconnect event hook pattern** (`ib_insync` IB object): `self._ib.disconnectedEvent += callback`. Callback takes no arguments. To stop the event loop from inside a callback: `from ib_insync import util; util.stop()`.

**Exit code convention** (`orchestrator/process.py` line 86): currently only `"scheduled_stop"` and `"unexpected_exit"` are returned; new `"ib_disconnected"` follows same string pattern.

**OutputChannel logging** (`orchestrator/main.py` line 56): `log_ch.writeln("[ORCH] message")`.

---

## PARALLEL EXECUTION STRATEGY

### Dependency Graph

```
┌─────────────────────────────────────────────────────┐
│ WAVE 1: Foundation (Parallel)                       │
├──────────────────────┬──────────────────────────────┤
│ Task 1.1             │ Task 1.2                     │
│ data/databento_      │ data/ib_realtime.py:          │
│ backfill.py (new)    │ IbGatewayDisconnectedError   │
│                      │ + disconnect hook             │
│                      │ + remove _gap_fill() call    │
└──────────────────────┴──────────────────────────────┘
                ↓                    ↓
┌─────────────────────────────────────────────────────┐
│ WAVE 2: Consumer Wiring (Parallel after Wave 1)     │
├──────────────────────┬──────────────────────────────┤
│ Task 2.1             │ Task 2.2                     │
│ automation/main.py:  │ orchestrator/process.py:     │
│ catch Disconnected   │ exit code 2 → ib_disconnected│
│ Error → sys.exit(2)  │ return value                 │
└──────────────────────┴──────────────────────────────┘
                ↓
┌─────────────────────────────────────────────────────┐
│ WAVE 3: Orchestrator Integration (Sequential)       │
├─────────────────────────────────────────────────────┤
│ Task 3.1                                            │
│ orchestrator/main.py: pre-session init +            │
│ ib_disconnected handler                             │
└─────────────────────────────────────────────────────┘
                ↓
┌─────────────────────────────────────────────────────┐
│ WAVE 4: Tests (Parallel)                            │
├──────────────────────┬──────────────────────────────┤
│ Task 4.1             │ Task 4.2                     │
│ tests/test_          │ Update existing tests:        │
│ databento_backfill.py│ test_ib_realtime.py,         │
│                      │ test_orchestrator_process.py,│
│                      │ test_orchestrator_main.py    │
└──────────────────────┴──────────────────────────────┘
```

### Parallelization Summary

**Wave 1 — Fully Parallel**: Tasks 1.1 and 1.2 touch different files with no shared state
**Wave 2 — Parallel after Wave 1**: Tasks 2.1 and 2.2 both depend on Wave 1 but are independent
**Wave 3 — Sequential**: Task 3.1 depends on both Wave 2 tasks (needs `run_session()` return value and `backfill_parquets` function)
**Wave 4 — Parallel**: Tests for Wave 1 and Wave 2–3 changes can be written simultaneously

### Interface Contracts

**Contract 1**: Task 1.1 provides `backfill_parquets(bar_data_dir: Path, ib_cutoff_days: int = 2, max_lookback_days: int = 30) -> None` in `data/databento_backfill.py`. Task 3.1 calls it.

**Contract 2**: Task 1.2 provides `IbGatewayDisconnectedError` exception class exported from `data/ib_realtime.py`. Task 2.1 imports and catches it.

**Contract 3**: Task 2.2 changes `ProcessManager.run_session()` to return `str | None` (`"ib_disconnected"` or `None`). Task 3.1 inspects the return value.

---

## IMPLEMENTATION PLAN

### Phase 1: Foundation

#### Task 1.1: CREATE `data/databento_backfill.py`

Purpose: Standalone Databento rolling-window parquet backfill. Fetches MNQ/MES 1m OHLCV bars from the last bar in existing parquets up to `ib_cutoff_days` days ago (older data that Databento can serve reliably). Merges into and saves existing parquets.

**Implementation**:

```python
# data/databento_backfill.py
from __future__ import annotations
import os
from pathlib import Path
import pandas as pd

MNQ_TICKER = "MNQ.v.0"
MES_TICKER  = "MES.v.0"


def backfill_parquets(
    bar_data_dir: Path,
    ib_cutoff_days: int = 2,
    max_lookback_days: int = 30,
) -> None:
    """Fetch Databento bars from last parquet bar up to `ib_cutoff_days` ago.

    Idempotent: no-op if parquets are already current up to the cutoff.
    Raises if DATABENTO_API_KEY is not set.
    """
    from data.sources import DatabentSource
    now = pd.Timestamp.now(tz="America/New_York")
    cutoff = now - pd.Timedelta(days=ib_cutoff_days)
    floor  = now - pd.Timedelta(days=max_lookback_days)
    bar_data_dir.mkdir(parents=True, exist_ok=True)

    source = DatabentSource()

    for ticker, fname in [(MNQ_TICKER, "MNQ_1m.parquet"), (MES_TICKER, "MES_1m.parquet")]:
        path = bar_data_dir / fname
        existing = pd.read_parquet(path) if path.exists() else _empty_df()
        start_ts = max(existing.index[-1], floor) if not existing.empty else floor
        if start_ts >= cutoff:
            continue  # already current
        df_new = source.fetch(ticker, start_ts.isoformat(), cutoff.isoformat(), interval="1m")
        if df_new is None or df_new.empty:
            continue
        combined = pd.concat([existing, df_new]).sort_index()
        combined = combined[~combined.index.duplicated(keep="last")]
        combined.to_parquet(path)


def _empty_df() -> pd.DataFrame:
    return pd.DataFrame(
        columns=["Open", "High", "Low", "Close", "Volume"],
        index=pd.DatetimeIndex([], tz="America/New_York"),
        dtype=float,
    )
```

**Key behaviours**:
- `DATABENTO_API_KEY` not set → `DatabentSource()` raises; caller must handle
- Parquet exists and last bar ≥ cutoff → skip (no fetch)
- Parquet missing → fetch full `max_lookback_days` window
- Merges new rows into existing, deduplicates by keeping last (same convention as `_gap_fill`)

**Validation**: `python -c "from data.databento_backfill import backfill_parquets; print('ok')"`

---

#### Task 1.2: UPDATE `data/ib_realtime.py` — Disconnect detection + remove _gap_fill call

**Changes**:

1. Add `IbGatewayDisconnectedError` class at module level (before `IbRealtimeSource`):

```python
class IbGatewayDisconnectedError(Exception):
    """Raised when IB Gateway closes the connection (not a transient network error)."""
```

2. Add `_stopping: bool = False` instance attribute in `__init__`.

3. Modify `stop()` — set `_stopping = True` before disconnect:

```python
def stop(self) -> None:
    self._stopping = True
    try:
        if self._ib and self._ib.isConnected():
            self._ib.disconnect()
    except Exception:
        pass
```

4. Modify `start()`:
   - Remove the `self._gap_fill()` call (line 249 in current code)
   - Inside the `for attempt in range(...)` loop, after `self._ib.connect(...)` and before
     `self._setup_subscriptions(...)`, register the disconnect hook:

```python
self._ib = IB()
self._ib.connect(self._host, self._port, clientId=self._client_id)

# Detect gateway-initiated disconnects (not our own stop() call)
self._disconnected_by_gateway = False

def _on_gateway_disconnect():
    if not self._stopping:
        self._disconnected_by_gateway = True
        util.stop()

self._ib.disconnectedEvent += _on_gateway_disconnect
self._setup_subscriptions(mnq_contract, mes_contract)
util.run()
if self._disconnected_by_gateway:
    raise IbGatewayDisconnectedError(
        "IB Gateway closed the connection"
    )
if self._ib.isConnected():
    break
raise ConnectionError("IB disconnected unexpectedly")
```

   - In the `except` clause of the retry loop, add a guard to NOT retry on
     `IbGatewayDisconnectedError`:

```python
except IbGatewayDisconnectedError:
    raise  # propagate immediately — do not retry
except Exception as exc:
    # existing retry logic unchanged
    ...
```

5. Keep `_gap_fill()` method body in place (do not delete) — it may be useful for manual
   invocation and has tests. Only remove the call from `start()`.

**Validation**: `python -m pytest tests/test_ib_realtime.py -x`

---

### Phase 2: Consumer Wiring

#### Task 2.1: UPDATE `automation/main.py` — Catch IbGatewayDisconnectedError

**File**: `automation/main.py`

**Import to add** (near top-of-file import block):

```python
from data.ib_realtime import IbGatewayDisconnectedError, IbRealtimeSource
```

(Replace the existing `from data.ib_realtime import IbRealtimeSource` line.)

**Change the try/finally block around `_ib_source.start()` (line ~1066)**:

```python
_executor.start()
try:
    _ib_source.start()  # blocks; retry loop is inside IbRealtimeSource
except IbGatewayDisconnectedError:
    print("[automation] IB Gateway disconnected — exiting with code 2", flush=True)
    sys.exit(2)
finally:
    _executor.stop()
```

`sys.exit(2)` raises `SystemExit(2)`. The `finally` block runs (`_executor.stop()`) and then the process exits with code 2.

**Validation**: `python -m pytest tests/ -k automation -x` (or full suite)

---

#### Task 2.2: UPDATE `orchestrator/process.py` — Exit code 2 → ib_disconnected

**Changes**:

1. In `_monitor()`: after `return "unexpected_exit"`, check returncode first:

```python
if proc.poll() is not None:
    if hasattr(proc.stdout, "close"):
        proc.stdout.close()
    reader.join(timeout=2)
    if proc.returncode == 2:
        return "ib_disconnected"
    return "unexpected_exit"
```

2. Change `run_session()` to return `str | None` — add return statements:

```python
def run_session(self, date: datetime.date) -> str | None:
    """...; returns 'ib_disconnected' if automation exited with code 2, None otherwise."""
    ...
    while True:
        proc = self._spawn()
        ...
        exit_reason = self._monitor(proc)
        if exit_reason == "scheduled_stop":
            self._log.writeln("[ORCH] Session ended — sending terminate signal")
            self._terminate(proc)
            return None
        if exit_reason == "ib_disconnected":
            self._log.writeln(
                "[ORCH] *** IB Gateway disconnected (exit code 2) — not restarting ***"
            )
            return "ib_disconnected"
        # Unexpected exit — existing restart-once logic unchanged
        if not restarted:
            self._log.writeln(...)
            restarted = True
        else:
            self._log.writeln(...)
            self._wait_until_grace_end()
            return None
```

**Important**: the `"ib_disconnected"` branch must be checked BEFORE the `restarted` flag logic so the process never restarts on an IB disconnect.

**Validation**: `python -m pytest tests/test_orchestrator_process.py -x`

---

### Phase 3: Orchestrator Integration

#### Task 3.1: UPDATE `orchestrator/main.py` — Pre-session init + ib_disconnected handler

**Changes**:

1. Add `_pre_session_init()` function:

```python
def _pre_session_init() -> None:
    """Run at orchestrator startup: Databento rolling backfill for historical bars."""
    import os
    from pathlib import Path as _Path
    bar_data_dir = _Path("data")
    if not os.environ.get("DATABENTO_API_KEY"):
        print("[ORCH] DATABENTO_API_KEY not set — skipping Databento pre-session backfill",
              flush=True)
        return
    try:
        from data.databento_backfill import backfill_parquets
        print("[ORCH] Running Databento pre-session backfill ...", flush=True)
        backfill_parquets(bar_data_dir)
        print("[ORCH] Databento pre-session backfill complete", flush=True)
    except Exception as exc:
        print(f"[ORCH] WARNING: Databento backfill failed: {exc} — "
              "IB seed will cover recent bars at session start", flush=True)
```

2. Call `_pre_session_init()` once at the start of `run()`, before the `while True:` loop:

```python
def run(summarizer: Summarizer | None = None, skip_summary: bool = False) -> None:
    """Main daemon loop. ..."""
    if not skip_summary and summarizer is None:
        summarizer = Summarizer()
    _pre_session_init()   # ← add here, before the loop
    try:
        while True:
            ...
```

3. Handle `"ib_disconnected"` return from `ProcessManager.run_session()`. Update the session
   block inside the `while True:` loop:

```python
result = ProcessManager(signal_cmd, relay, orch_ch).run_session(today)
_close_session_position(orch_ch)
relay.write_trades_tsv(...)
if summarizer is not None:
    summarizer.run(...)
if result == "ib_disconnected":
    orch_ch.writeln(
        "[ORCH] *** IB Gateway disconnected. Restart IB Gateway, then relaunch "
        "the orchestrator. All positions have been closed. ***"
    )
    sys.exit(3)
_sleep_until(next_session_open(get_et_now()), "next trading session")
```

**Note on exit codes**: code 3 from the orchestrator is distinct from the automation subprocess's
code 2, making log forensics easier.

**Validation**: `python -m pytest tests/test_orchestrator_main.py -x`

---

### Phase 4: Tests

#### Task 4.1: CREATE `tests/test_databento_backfill.py`

```python
# tests/test_databento_backfill.py
import pandas as pd
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch


@pytest.fixture
def bar_dir(tmp_path):
    return tmp_path


def _make_df(last_ts: str) -> pd.DataFrame:
    ts = pd.Timestamp(last_ts, tz="America/New_York")
    return pd.DataFrame(
        [[100.0, 101.0, 99.0, 100.5, 500.0]],
        columns=["Open", "High", "Low", "Close", "Volume"],
        index=pd.DatetimeIndex([ts]),
    )


# Test 1: no parquet exists → fetches full max_lookback_days window
def test_backfill_creates_parquets_when_missing(bar_dir):
    new_df = _make_df("2026-05-01 10:00:00")
    with patch("data.databento_backfill.DatabentSource") as MockSource:
        MockSource.return_value.fetch.return_value = new_df
        from data.databento_backfill import backfill_parquets
        backfill_parquets(bar_dir)
    assert (bar_dir / "MNQ_1m.parquet").exists()
    assert (bar_dir / "MES_1m.parquet").exists()


# Test 2: parquets current (last bar >= cutoff) → no fetch
def test_backfill_skips_when_current(bar_dir):
    now = pd.Timestamp.now(tz="America/New_York")
    recent_df = _make_df((now - pd.Timedelta(hours=1)).isoformat())
    recent_df.to_parquet(bar_dir / "MNQ_1m.parquet")
    recent_df.to_parquet(bar_dir / "MES_1m.parquet")
    with patch("data.databento_backfill.DatabentSource") as MockSource:
        from data.databento_backfill import backfill_parquets
        backfill_parquets(bar_dir, ib_cutoff_days=2)
        MockSource.return_value.fetch.assert_not_called()


# Test 3: Databento returns None → no write, no crash
def test_backfill_handles_none_response(bar_dir):
    with patch("data.databento_backfill.DatabentSource") as MockSource:
        MockSource.return_value.fetch.return_value = None
        from data.databento_backfill import backfill_parquets
        backfill_parquets(bar_dir)  # should not raise


# Test 4: Databento returns empty df → no write, no crash
def test_backfill_handles_empty_response(bar_dir):
    from data.databento_backfill import _empty_df
    with patch("data.databento_backfill.DatabentSource") as MockSource:
        MockSource.return_value.fetch.return_value = _empty_df()
        from data.databento_backfill import backfill_parquets
        backfill_parquets(bar_dir)


# Test 5: existing parquet with gap → merges and deduplicates
def test_backfill_merges_and_deduplicates(bar_dir):
    old_df = _make_df("2026-04-01 10:00:00")
    old_df.to_parquet(bar_dir / "MNQ_1m.parquet")
    old_df.to_parquet(bar_dir / "MES_1m.parquet")
    new_rows = pd.concat([
        old_df,  # duplicate row
        _make_df("2026-04-02 10:00:00"),
    ])
    with patch("data.databento_backfill.DatabentSource") as MockSource:
        MockSource.return_value.fetch.return_value = new_rows
        from data.databento_backfill import backfill_parquets
        backfill_parquets(bar_dir)
    result = pd.read_parquet(bar_dir / "MNQ_1m.parquet")
    assert len(result) == 2  # deduplicated
```

**Run**: `python -m pytest tests/test_databento_backfill.py -v`

---

#### Task 4.2: UPDATE existing test files

**`tests/test_ib_realtime.py`** — add tests:

```
test_gateway_disconnect_sets_flag:
  Mock IB object; trigger disconnectedEvent while _stopping=False.
  Assert IbGatewayDisconnectedError is raised from start().

test_stop_does_not_trigger_gateway_disconnect:
  Mock IB object; call stop() then trigger disconnectedEvent.
  Assert _disconnected_by_gateway remains False.

test_gap_fill_not_called_in_start:
  Call start() with mocked IB; assert _gap_fill() is never called.
  (Regression guard: _gap_fill() removal stays in place.)

test_ibgateway_disconnected_error_not_retried:
  Wrap start() such that the first connect raises IbGatewayDisconnectedError.
  Assert the error propagates immediately (attempt counter stays at 1).
```

**`tests/test_orchestrator_process.py`** — add tests:

```
test_monitor_returns_ib_disconnected_on_exit_code_2:
  Mock Popen with returncode=2.
  Assert _monitor() returns "ib_disconnected".

test_run_session_returns_ib_disconnected:
  Mock subprocess that exits with code 2.
  Assert run_session() returns "ib_disconnected" without restarting.

test_run_session_does_not_restart_on_ib_disconnect:
  Mock subprocess that exits with code 2.
  Assert _spawn() is only called once (no restart).

test_run_session_returns_none_on_scheduled_stop:
  Verify existing scheduled_stop path still returns None (regression).
```

**`tests/test_orchestrator_main.py`** — add tests:

```
test_pre_session_init_called_at_startup:
  Mock _pre_session_init; call run() and force KeyboardInterrupt immediately.
  Assert _pre_session_init was called once before the loop.

test_pre_session_init_skips_when_no_api_key:
  Unset DATABENTO_API_KEY; call _pre_session_init().
  Assert no exception, DatabentSource not instantiated.

test_run_exits_on_ib_disconnected:
  Mock ProcessManager.run_session to return "ib_disconnected".
  Mock _pre_session_init, _close_session_position, is_trading_day, get_et_now.
  Assert run() calls sys.exit(3).

test_run_closes_position_before_ib_disconnect_exit:
  Same mock setup as above.
  Assert _close_session_position is called before sys.exit(3).
```

**Run all**: `python -m pytest tests/test_ib_realtime.py tests/test_orchestrator_process.py tests/test_orchestrator_main.py tests/test_databento_backfill.py -v`

---

## STEP-BY-STEP TASKS

Tasks organized by execution wave. Same wave = safe to run in parallel.

---

### WAVE 1: Foundation

#### Task 1.1: CREATE `data/databento_backfill.py`

- **WAVE**: 1
- **AGENT_ROLE**: data-engineer
- **DEPENDS_ON**: []
- **BLOCKS**: [3.1]
- **PROVIDES**: `backfill_parquets(bar_data_dir, ib_cutoff_days=2, max_lookback_days=30) -> None` and `_empty_df() -> pd.DataFrame`
- **IMPLEMENT**: Full module as specified above in Phase 1 / Task 1.1
- **PATTERN**: Parquet read/write from `data/ib_realtime.py` lines 62–98; DatabentSource from `data/sources.py`
- **VALIDATE**: `python -c "from data.databento_backfill import backfill_parquets; print('import ok')"`

#### Task 1.2: UPDATE `data/ib_realtime.py`

- **WAVE**: 1
- **AGENT_ROLE**: data-engineer
- **DEPENDS_ON**: []
- **BLOCKS**: [2.1]
- **PROVIDES**: `IbGatewayDisconnectedError` class; modified `start()` with disconnect detection; `_gap_fill()` no longer called from `start()`
- **IMPLEMENT**: As specified in Phase 1 / Task 1.2. Three sub-changes: (a) add `IbGatewayDisconnectedError` at module level, (b) add `_stopping` attribute and set it in `stop()`, (c) modify `start()` to add disconnect hook and remove `_gap_fill()` call
- **PATTERN**: `ib_insync` `disconnectedEvent` usage; existing `stop()` method at line 282
- **VALIDATE**: `python -m pytest tests/test_ib_realtime.py -x`

**Wave 1 Checkpoint**: `python -c "from data.ib_realtime import IbGatewayDisconnectedError; from data.databento_backfill import backfill_parquets; print('ok')"`

---

### WAVE 2: Consumer Wiring

#### Task 2.1: UPDATE `automation/main.py`

- **WAVE**: 2
- **AGENT_ROLE**: integration-specialist
- **DEPENDS_ON**: [1.2]
- **BLOCKS**: []
- **USES_FROM_WAVE_1**: Task 1.2 provides `IbGatewayDisconnectedError`
- **IMPLEMENT**: As specified in Phase 2 / Task 2.1. Import `IbGatewayDisconnectedError`; add `except IbGatewayDisconnectedError` before the `finally` block around `_ib_source.start()`
- **VALIDATE**: `python -m pytest tests/ -k "automation" -x` (or full suite if no targeted tests exist)

#### Task 2.2: UPDATE `orchestrator/process.py`

- **WAVE**: 2
- **AGENT_ROLE**: integration-specialist
- **DEPENDS_ON**: []
- **BLOCKS**: [3.1]
- **PROVIDES**: `run_session()` returns `"ib_disconnected"` when subprocess exits with code 2; otherwise `None`
- **IMPLEMENT**: As specified in Phase 2 / Task 2.2. Two changes: (a) check `proc.returncode == 2` in `_monitor()`, (b) add `"ib_disconnected"` branch in `run_session()` that logs and returns without restarting; update return type annotation to `str | None`
- **VALIDATE**: `python -m pytest tests/test_orchestrator_process.py -x`

**Wave 2 Checkpoint**: `python -m pytest tests/test_ib_realtime.py tests/test_orchestrator_process.py -x`

---

### WAVE 3: Orchestrator Integration

#### Task 3.1: UPDATE `orchestrator/main.py`

- **WAVE**: 3
- **AGENT_ROLE**: integration-specialist
- **DEPENDS_ON**: [1.1, 2.2]
- **PROVIDES**: Orchestrator runs Databento backfill at startup; exits cleanly (code 3) on IB disconnect
- **USES_FROM_WAVE_1**: Task 1.1 provides `backfill_parquets`; Task 2.2 provides `"ib_disconnected"` return
- **IMPLEMENT**: As specified in Phase 3 / Task 3.1. Three changes: (a) add `_pre_session_init()` function, (b) call it before the `while True:` loop in `run()`, (c) check `result == "ib_disconnected"` after `ProcessManager.run_session()` and call `sys.exit(3)`
- **VALIDATE**: `python -m pytest tests/test_orchestrator_main.py -x`

**Wave 3 Checkpoint**: `python -m pytest tests/ -x`

---

### WAVE 4: Tests

#### Task 4.1: CREATE `tests/test_databento_backfill.py`

- **WAVE**: 4
- **AGENT_ROLE**: test-engineer
- **DEPENDS_ON**: [1.1]
- **IMPLEMENT**: Full test file as specified in Phase 4 / Task 4.1. Five test cases covering: missing parquets, current parquets (no-op), None response, empty response, merge+dedup
- **VALIDATE**: `python -m pytest tests/test_databento_backfill.py -v`

#### Task 4.2: UPDATE existing test files

- **WAVE**: 4
- **AGENT_ROLE**: test-engineer
- **DEPENDS_ON**: [1.2, 2.2, 3.1]
- **IMPLEMENT**: Add tests to `tests/test_ib_realtime.py`, `tests/test_orchestrator_process.py`, and `tests/test_orchestrator_main.py` as specified in Phase 4 / Task 4.2
- **VALIDATE**: `python -m pytest tests/test_ib_realtime.py tests/test_orchestrator_process.py tests/test_orchestrator_main.py -v`

**Final Checkpoint**: `python -m pytest tests/ -x` (full suite, no regressions)

---

## TESTING STRATEGY

| What | Tool | Status |
|---|---|---|
| `backfill_parquets` logic | pytest | ✅ Automated |
| `IbGatewayDisconnectedError` raise + no-retry | pytest + mock | ✅ Automated |
| Disconnect hook (`disconnectedEvent`) | pytest + mock | ✅ Automated |
| `_gap_fill()` not called from `start()` | pytest + mock | ✅ Automated |
| `sys.exit(2)` on disconnect in automation/main | pytest + mock | ✅ Automated |
| ProcessManager exit code 2 → ib_disconnected | pytest + mock | ✅ Automated |
| Orchestrator `_pre_session_init()` called | pytest + mock | ✅ Automated |
| Orchestrator sys.exit(3) on ib_disconnected | pytest + mock | ✅ Automated |
| **End-to-end: real IB connection — parquets populated** | Manual | ⚠️ Manual (requires IB Gateway hardware) |
| **End-to-end: real IB disconnect simulation** | Manual | ⚠️ Manual (requires IB Gateway hardware) |

### Unit Tests

**Status**: ✅ Automated | **Tool**: pytest | **Location**: `tests/` | **Run**: `python -m pytest tests/ -x`

### Manual Integration Tests (require live IB Gateway)

These tests cannot be automated because they require a physical IB Gateway process and live market data subscription.

#### Manual Test 1: Pre-session Databento backfill runs at orchestrator startup

**Why Manual**: Requires `DATABENTO_API_KEY` and network access to Databento API
**Steps**:
1. Ensure `DATABENTO_API_KEY` is set in `.env`
2. Delete or rename `data/MNQ_1m.parquet` and `data/MES_1m.parquet` (back them up first)
3. Start the orchestrator: `uv run python -m orchestrator.main --no-summary`
4. Observe stdout for `[ORCH] Running Databento pre-session backfill ...`
5. After a few seconds, check that parquets exist and contain data

**Expected**: Both parquets exist with bars going back `max_lookback_days` days (default 30).
Log line: `[ORCH] Databento pre-session backfill complete`.

#### Manual Test 2: `_gap_fill()` no longer runs at strategy startup

**Why Manual**: Requires IB Gateway connection to observe actual startup sequence
**Steps**:
1. Start orchestrator, wait for session start (09:20 ET)
2. Observe `orchestrator.log` — should NOT contain `[gap_fill]` lines
3. Strategy startup log should not show gap-fill attempts

**Expected**: No `[gap_fill] MNQ: 0 bars returned...` lines in logs.

#### Manual Test 3: IB disconnect detected and orchestrator exits cleanly

**Why Manual**: Requires live IB Gateway; disconnect must be hardware-level (close the gateway)
**Prerequisites**: Orchestrator running in LIVE_TRADING mode, mid-session
**Steps**:
1. Start orchestrator (or wait for it to start at session open)
2. Confirm strategy is running (see log entries in events.jsonl)
3. Close IB Gateway (via its UI: File → Exit, or kill the process)
4. Wait up to 30 seconds
5. Check `orchestrator.log` for: `[ORCH] *** IB Gateway disconnected...`
6. Check orchestrator process exits with code 3

**Expected**:
- Automation subprocess exits with code 2 within 30 seconds of IB Gateway close
- If an active position is open, a `market-close` event appears in `events.jsonl` with `reason: "session-end"`
- Orchestrator log contains: `[ORCH] *** IB Gateway disconnected. Restart IB Gateway, then relaunch the orchestrator. All positions have been closed. ***`
- Orchestrator process is no longer running

#### Manual Test 4: Orchestrator graceful behaviour when DATABENTO_API_KEY not set

**Why Manual**: Environment variable configuration test; easy but confirms graceful degradation
**Steps**:
1. Temporarily remove `DATABENTO_API_KEY` from `.env`
2. Start orchestrator
3. Observe log output

**Expected**: `[ORCH] DATABENTO_API_KEY not set — skipping Databento pre-session backfill`.
No exception, orchestrator continues normally.

---

## VALIDATION COMMANDS

### Level 1: Syntax

```bash
python -c "from data.databento_backfill import backfill_parquets; print('ok')"
python -c "from data.ib_realtime import IbGatewayDisconnectedError, IbRealtimeSource; print('ok')"
python -c "from orchestrator.process import ProcessManager; print('ok')"
python -c "from orchestrator.main import run, _pre_session_init; print('ok')"
```

### Level 2: Unit Tests

```bash
python -m pytest tests/test_databento_backfill.py -v
python -m pytest tests/test_ib_realtime.py -v
python -m pytest tests/test_orchestrator_process.py -v
python -m pytest tests/test_orchestrator_main.py -v
```

### Level 3: Full Suite (Regression)

```bash
python -m pytest tests/ -x
```

### Level 4: Manual Integration (with real IB)

See Manual Tests 1–4 in Testing Strategy above.

---

## ACCEPTANCE CRITERIA

### Functional

- [ ] `data/databento_backfill.py` exists and can be imported without error
- [ ] `backfill_parquets()` creates parquets when they do not exist, fetching up to `max_lookback_days` back
- [ ] `backfill_parquets()` skips the fetch entirely when the existing parquet's last bar is already ≥ cutoff timestamp
- [ ] `backfill_parquets()` merges new rows into existing parquet and deduplicates (keep last) before saving
- [ ] `IbGatewayDisconnectedError` is defined in `data/ib_realtime.py` and importable from it
- [ ] `IbRealtimeSource.start()` does NOT call `self._gap_fill()`
- [ ] `IbGatewayDisconnectedError` is raised when `ib.disconnectedEvent` fires while `_stopping=False`
- [ ] `IbGatewayDisconnectedError` propagates immediately without triggering the retry loop
- [ ] `automation/main.py` catches `IbGatewayDisconnectedError` and calls `sys.exit(2)`
- [ ] `ProcessManager.run_session()` returns `"ib_disconnected"` (not `None`) when the subprocess exits with code 2
- [ ] `ProcessManager.run_session()` does NOT restart the subprocess on `"ib_disconnected"`
- [ ] `orchestrator/main.py` calls `_pre_session_init()` once before the session loop begins
- [ ] `orchestrator/main.py` calls `_close_session_position()` then `sys.exit(3)` when `run_session()` returns `"ib_disconnected"`

### Error Handling

- [ ] `backfill_parquets()` does not raise when Databento returns `None`
- [ ] `backfill_parquets()` does not raise when Databento returns an empty DataFrame
- [ ] `_pre_session_init()` does not raise and logs a clear warning when `DATABENTO_API_KEY` is not set
- [ ] `_pre_session_init()` does not raise when Databento fetch throws (logs warning, continues)
- [ ] `IbRealtimeSource.stop()` setting `_stopping=True` prevents `_disconnected_by_gateway` from being set on a deliberate disconnect

### Integration / E2E (with real IB Gateway at 127.0.0.1:4002)

- [ ] Starting `IbRealtimeSource` with IB Gateway active: `start()` connects, seeds bars, and the `[gap_fill]` log line does NOT appear
- [ ] Killing IB Gateway while `IbRealtimeSource` is running: `IbGatewayDisconnectedError` is raised within 30 seconds, subprocess exits with code 2, and orchestrator log shows the disconnect alert

### Validation

- [ ] `python -c "from data.databento_backfill import backfill_parquets; print('ok')"` exits 0
- [ ] `python -c "from data.ib_realtime import IbGatewayDisconnectedError; print('ok')"` exits 0
- [ ] `python -m pytest tests/test_databento_backfill.py -v` — all tests pass
- [ ] `python -m pytest tests/test_ib_realtime.py -v` — all tests pass (including new disconnect tests)
- [ ] `python -m pytest tests/test_orchestrator_process.py -v` — all tests pass (including exit-code-2 tests)
- [ ] `python -m pytest tests/test_orchestrator_main.py -v` — all tests pass (including pre-session-init and ib_disconnected tests)
- [ ] `python -m pytest tests/ -x` — full suite passes with no regressions

### Out of Scope

- Re-fixing `IBGatewaySource.fetch()` for active-session endDateTime — IB seed already covers recent bars
- Auto-restarting IB Gateway or orchestrator on disconnect — operator must restart explicitly
- Deleting the `_gap_fill()` method body — retained to avoid breaking existing tests and for potential manual use

---

## COMPLETION CHECKLIST

- [ ] All 5 files modified/created (`data/databento_backfill.py`, `data/ib_realtime.py`, `automation/main.py`, `orchestrator/process.py`, `orchestrator/main.py`)
- [ ] 4 test files created/updated
- [ ] All Wave checkpoints pass
- [ ] Full suite `python -m pytest tests/ -x` passes
- [ ] Level 1 syntax validation passes
- [ ] All 15 automated acceptance criteria met
- [ ] Manual tests 1–4 scheduled for post-implementation verification with real IB
- [ ] Debug logs added during execution removed (keep pre-existing)
- [ ] **⚠️ CRITICAL: Changes UNSTAGED — NOT committed**

---

## NOTES

**Why not re-fix IBGatewaySource for the current-session gap-fill?**
The root cause is that `IBGatewaySource.fetch()` sends `reqHistoricalData` with an explicit
`endDateTime` that falls within the active session. IB TWS/Gateway returns 0 bars in this case.
Fixing this would require patching the endDateTime formatting or switching to a different IB
API call. This is disproportionate effort since the IB seed (`durationStr="3 D" keepUpToDate=True`)
already provides accurate recent bars. The Databento tier covers historical data, making IBGatewaySource redundant for gap-fill purposes.

**Orchestrator exit code 3 vs restart on disconnect**
The user explicitly asked for the orchestrator to "let the user know he should restart IB and
tell the agent running the orchestrator to restart the IB connection." Exiting with code 3 is
safer than any auto-restart: a position could be open and needs manual verification before
rejoining. The operator explicitly relaunching the orchestrator provides that verification checkpoint.

**`_gap_fill()` method retained (not deleted)**
The method is kept in place to avoid breaking any external callers and because it has existing
unit tests. The risk of a future caller re-enabling it by mistake is low; the method's
docstring already explains why it is not called at startup.

**DATABENTO_API_KEY env var**
The backfill function requires `DATABENTO_API_KEY` at runtime (raised by `DatabentSource()`).
`_pre_session_init()` guards for the missing key and logs a clear message. The orchestrator
continues normally; the IB seed covers the most recent 3 days regardless.
