# Feature: signal_smt.py — Tick-Based 1s Bar Ingestion

**⚠️ EXECUTION RULES — READ FIRST:**
- Implement ALL changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
- Only make code changes — no git operations

Validate documentation and codebase patterns before implementing. Match naming of existing utils, types, and models. Import from correct files.

---

## Feature Description

Replace the two `reqHistoricalData(..., barSizeSetting="1 secs", keepUpToDate=True)` subscriptions in `signal_smt.py` with `reqTickByTickData("AllLast")` subscriptions. Accumulate individual trade ticks into per-second OHLCV bars; emit a completed bar and trigger signal processing only when the first tick of a new second arrives (i.e., the previous second's bar is finalized).

This reduces the worst-case signal detection latency from ~5 seconds (IB's batch delivery cadence for 1s keepUpToDate bars) to ~1 second, without changing any signal logic, buffer structures, or position management code.

## User Story

As a trader running the SMT signal generator,
I want signal detection to trigger within ~1 second of the pattern forming,
So that my entry price is not materially degraded by stale data.

## Problem Statement

`reqHistoricalData` with `barSizeSetting="1 secs"` and `keepUpToDate=True` delivers new bars in batches approximately every 5 seconds (IB server-side behavior). Since `_process_scanning` / `_process_managing` is called on each 1s bar callback, the state machine evaluates at most 12 times per minute instead of the intended 60. This creates up to 5s of entry latency after a signal forms.

Confirmed via live test: 18 callbacks in 90 seconds (1 per ~5s) despite `barSizeSetting="1 secs"`. Also confirmed that `reqTickByTickData("AllLast")` fires per individual trade tick with millisecond-level timestamps.

## Solution Statement

Subscribe to `reqTickByTickData(contract, "AllLast", 0, False)` for MNQ and MES. In the tick callback, accumulate each tick into a running OHLCV dict for the current second. When a tick belonging to a new second arrives, finalize the previous second's OHLCV into a DataFrame row, append it to the existing `_mnq_1s_buf` / `_mes_1s_buf`, and trigger `_process()` (MNQ side only — MES is passive). The rest of the codebase — buffers, state machine, signal detection, position management — is unchanged.

## Feature Metadata

**Feature Type**: Enhancement (latency reduction)
**Complexity**: Low
**Primary Systems Affected**: `signal_smt.py`, `tests/test_signal_smt.py`
**Dependencies**: `ib_insync` (already installed — uses `reqTickByTickData` and `Ticker` object)
**Breaking Changes**: No — public interface and all downstream logic unchanged

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ BEFORE IMPLEMENTING

- `signal_smt.py` (lines 248–266) — `on_mes_1s_bar` and `on_mnq_1s_bar`: the two functions being replaced
- `signal_smt.py` (lines 127–138) — `_append_1s_bar`: the function that converts a bar object to a DataFrame row; new code must produce compatible output
- `signal_smt.py` (lines 141–146) — `_bar_timestamp`: handles tz-aware vs tz-naive timestamps; tick timestamps from `reqTickByTickData` come as UTC datetimes — must convert to ET
- `signal_smt.py` (lines 271–279) — `_process()`: the entry point called by the current `on_mnq_1s_bar`; must be called the same way by the new MNQ tick handler
- `signal_smt.py` (lines 282–341) — `_process_scanning`: consumes `_mnq_1s_buf` and `_mes_1s_buf` via their index; buffer row schema must be identical
- `signal_smt.py` (lines 419–453) — `main()`: where `reqHistoricalData` 1s calls and `updateEvent` wiring live; this is where subscriptions are replaced
- `tests/test_signal_smt.py` (lines 485–519) — `test_1s_buf_cleared_on_1m_bar` and `test_mes_1s_buf_always_appended`: the two tests that directly call `on_mes_1s_bar` with a BarList mock; these need updating to call the new tick handler with a Ticker mock
- `tests/test_signal_smt.py` (lines 70–87) — `_make_mock_bar`: existing bar builder for reference; a parallel `_make_mock_ticker` builder is needed for tick tests

### New Files to Create

None. All changes are in `signal_smt.py` and `tests/test_signal_smt.py`.

### Patterns to Follow

**Tick callback signature** (confirmed via live test):
```python
def on_mnq_tick(ticker):          # ticker is an ib_insync Ticker object
    if not ticker.tickByTicks:
        return
    t = ticker.tickByTicks[-1]    # TickByTickAllLast: .time (UTC datetime), .price, .size
```

**Tick timestamp conversion** (UTC → ET, floor to second):
```python
ts = pd.Timestamp(t.time).tz_convert("America/New_York")
second_ts = ts.floor("s")         # floor to second boundary
```

**Accumulator dict structure**:
```python
{"open": price, "high": price, "low": price, "close": price, "volume": size, "second_ts": second_ts}
```

**Finalizing accumulator → DataFrame row** (must match `_append_1s_bar` output schema):
```python
row = pd.DataFrame(
    [[acc["open"], acc["high"], acc["low"], acc["close"], acc["volume"]]],
    columns=["Open", "High", "Low", "Close", "Volume"],
    index=pd.DatetimeIndex([acc["second_ts"]]),
)
_mnq_1s_buf = pd.concat([_mnq_1s_buf, row])
```

**Mock Ticker for tests**:
```python
def _make_mock_ticker(ts: str, price: float, size: float = 1.0, tz: str = "UTC"):
    tick = mock.MagicMock()
    tick.time = pd.Timestamp(ts, tz=tz)
    tick.price = price
    tick.size = size
    ticker = mock.MagicMock()
    ticker.tickByTicks = [tick]
    return ticker
```

---

## PARALLEL EXECUTION STRATEGY

This feature is a focused two-file change with a strict dependency: tests must be updated after the implementation. Sequential execution is appropriate.

```
WAVE 1: signal_smt.py changes
         ↓
WAVE 2: test_signal_smt.py updates
         ↓
WAVE 3: Validation
```

### Parallelization Summary

**Wave 1 — Sequential**: Implement tick accumulators + handlers + main() wiring
**Wave 2 — Sequential after Wave 1**: Update/add tests
**Wave 3 — Sequential after Wave 2**: Run full test suite

### Interface Contracts

**Wave 1 → Wave 2**: `on_mnq_tick(ticker)` and `on_mes_tick(ticker)` exist and accept an ib_insync `Ticker` object. `_mnq_1s_buf` and `_mes_1s_buf` are populated with the same OHLCV DataFrame schema as before.

---

## IMPLEMENTATION PLAN

### Phase 1: signal_smt.py — New module-level state

Add two tick accumulator variables directly after the existing `_mnq_1s_buf` / `_mes_1s_buf` declarations (line 52). These track the running OHLCV for the in-progress second:

```python
# Per-instrument tick accumulators for the current in-progress second
_mnq_tick_bar: dict | None = None   # {open, high, low, close, volume, second_ts}
_mes_tick_bar: dict | None = None
```

Also add them to the `global` declaration in `main()`.

### Phase 2: signal_smt.py — New tick handler helpers

Add two functions after `_build_1s_buffer_df` (after line 124):

**`_update_tick_accumulator(acc, price, size, second_ts)`**
- If `acc` is None or `second_ts != acc["second_ts"]` → finalize (if not None) and return a new accumulator `{"open": price, "high": price, "low": price, "close": price, "volume": size, "second_ts": second_ts}` plus a boolean `finalized=True`
- Else → update `acc["high"] = max(acc["high"], price)`, `acc["low"] = min(acc["low"], price)`, `acc["close"] = price`, `acc["volume"] += size`, return `acc, False`

**`_acc_to_df_row(acc)`**
- Converts an accumulator dict to a one-row OHLCV DataFrame with a tz-aware ET DatetimeIndex at `acc["second_ts"]`
- Must produce identical schema to `_append_1s_bar` output

### Phase 3: signal_smt.py — Replace 1s handlers

**Remove** `on_mes_1s_bar` (lines 248–253) and `on_mnq_1s_bar` (lines 256–266).

**Add** `on_mes_tick(ticker)`:
```
global _mes_tick_bar, _mes_1s_buf
- Guard: if not ticker.tickByTicks → return
- Extract tick: t = ticker.tickByTicks[-1]
- Convert: second_ts = pd.Timestamp(t.time).tz_convert("America/New_York").floor("s")
- Call _update_tick_accumulator(_mes_tick_bar, t.price, t.size, second_ts)
- If finalized: append previous acc row to _mes_1s_buf
- Reassign _mes_tick_bar
```

**Add** `on_mnq_tick(ticker)`:
```
global _mnq_tick_bar, _mnq_1s_buf
- Same accumulator logic as on_mes_tick
- Additionally: if finalized → build a synthetic bar object (or pass acc dict directly)
  and call _process() with it
```

For `_process()`, it currently receives a bar object and calls `_bar_timestamp(bar)` on it, then reads `bar.open/high/low/close/volume`. The simplest approach: create a lightweight named object from the finalized accumulator to pass to `_process()`. Alternatively, refactor `_process()` to accept a `pd.Timestamp` and a `pd.Series` directly. **Use the named object approach** to minimize blast radius — keep `_process()`, `_process_scanning()`, and `_process_managing()` completely unchanged.

Synthetic bar object:
```python
class _SyntheticBar:
    """Minimal bar-like object built from a finalized tick accumulator."""
    __slots__ = ("date", "open", "high", "low", "close", "volume")
    def __init__(self, acc):
        self.date   = acc["second_ts"]
        self.open   = acc["open"]
        self.high   = acc["high"]
        self.low    = acc["low"]
        self.close  = acc["close"]
        self.volume = acc["volume"]
```

Place this class definition above the handler functions, below the helpers section. `_bar_timestamp` already handles `pd.Timestamp` inputs with tz set, so `_SyntheticBar.date = acc["second_ts"]` (which is an ET-localized Timestamp) passes through correctly.

### Phase 4: signal_smt.py — Update main()

In `main()`:

1. Remove the two `reqHistoricalData` calls for 1s bars (the `mnq_1s` and `mes_1s` variables, lines 432–446).
2. Remove `mnq_1s.updateEvent += on_mnq_1s_bar` and `mes_1s.updateEvent += on_mes_1s_bar` (lines 449, 451).
3. Add after the 1m subscriptions:
```python
mnq_tick = _ib.reqTickByTickData(mnq_contract, "AllLast", 0, False)
mes_tick = _ib.reqTickByTickData(mes_contract, "AllLast", 0, False)
mnq_tick.updateEvent += on_mnq_tick
mes_tick.updateEvent += on_mes_tick
```
4. Add `_mnq_tick_bar = None` and `_mes_tick_bar = None` to the initialization block alongside `_mnq_1s_buf = _empty_bar_df()`.

### Phase 5: tests/test_signal_smt.py — Add mock Ticker builder

Add `_make_mock_ticker` helper alongside the existing `_make_mock_bar`:
```python
def _make_mock_ticker(ts: str, price: float, size: float = 1.0, tz: str = "UTC"):
    """Build a mock ib_insync Ticker object as delivered by reqTickByTickData."""
    tick = mock.MagicMock()
    tick.time  = pd.Timestamp(ts, tz=tz)
    tick.price = price
    tick.size  = size
    ticker = mock.MagicMock()
    ticker.tickByTicks = [tick]
    return ticker
```

### Phase 6: tests/test_signal_smt.py — Update existing 1s tests

**`test_1s_buf_cleared_on_1m_bar`** (line 485): This test calls `on_mnq_1m_bar` and checks that `_mnq_1s_buf` is cleared. The test does NOT call `on_mnq_1s_bar` directly — it tests the 1m handler, which is unchanged. **No changes needed.**

**`test_mes_1s_buf_always_appended`** (line 506): Currently calls `on_mes_1s_bar(bars, hasNewBar=True)` with a BarList mock. Must be updated to call `on_mes_tick(ticker)` with a Ticker mock. The assertion (`len(_mes_1s_buf) == 1`) remains the same, but only fires after a second boundary is crossed. Test must provide two ticks at different second timestamps to trigger finalization.

Updated test logic:
```python
# Tick 1: establishes accumulator for second S
ticker1 = _make_mock_ticker("2025-01-02 14:06:00.100000", price=20001.0, tz="UTC")
signal_smt.on_mes_tick(ticker1)
assert len(signal_smt._mes_1s_buf) == 0   # not yet finalized

# Tick 2: new second → finalizes tick 1's bar into buffer
ticker2 = _make_mock_ticker("2025-01-02 14:06:01.200000", price=20002.0, tz="UTC")
signal_smt.on_mes_tick(ticker2)
assert len(signal_smt._mes_1s_buf) == 1   # previous second finalized
```

### Phase 7: tests/test_signal_smt.py — Add new tick-specific tests

Add the following new tests after the existing buffer management tests:

**`test_tick_accumulator_builds_ohlcv`**: Send three ticks within the same second to `on_mnq_tick`; assert the accumulator's high = max of the three prices, low = min, close = last, volume = sum, open = first.

**`test_tick_boundary_triggers_process`**: Set up SCANNING state with aligned buffers; send a first tick (second S); send a second tick (second S+1); assert `_mnq_1s_buf` has grown by 1 and `_process()` was invoked. Use `monkeypatch` to capture `_process` calls.

**`test_tick_no_tickbyticks_is_noop`**: Call `on_mnq_tick` with a Ticker whose `tickByTicks` is empty; assert no state change.

**`test_mes_tick_does_not_call_process`**: Send two ticks at different seconds to `on_mes_tick`; assert `_mes_1s_buf` grows but `_process` is never called. Verify via monkeypatching `signal_smt._process`.

**`test_synthetic_bar_timestamp_et`**: Create a `_SyntheticBar` from an accumulator with `second_ts` in ET; call `_bar_timestamp` on it; assert result timezone is `America/New_York`.

**`test_tick_ohlcv_schema_matches_append_1s_bar`**: Finalize an accumulator via `_acc_to_df_row`; assert columns are `["Open", "High", "Low", "Close", "Volume"]` and index timezone is ET. This verifies compatibility with `_mnq_1s_buf` / `_mes_1s_buf` schema.

---

## STEP-BY-STEP TASKS

### WAVE 1: signal_smt.py Changes

#### Task 1.1: ADD module-level tick accumulator variables to signal_smt.py

- **WAVE**: 1
- **AGENT_ROLE**: implementer
- **DEPENDS_ON**: []
- **BLOCKS**: [1.2, 1.3, 1.4]
- **PROVIDES**: `_mnq_tick_bar` and `_mes_tick_bar` module globals
- **IMPLEMENT**: Insert after line 52 in `signal_smt.py`:
  ```python
  _mnq_tick_bar: dict | None = None   # running OHLCV accumulator for in-progress second
  _mes_tick_bar: dict | None = None
  ```
  Also add both to the `global` declaration in `main()` (line 392).
  Add `_mnq_tick_bar = None` and `_mes_tick_bar = None` to the initialization block in `main()`.
- **VALIDATE**: `python -c "import signal_smt; print(signal_smt._mnq_tick_bar, signal_smt._mes_tick_bar)"`

#### Task 1.2: ADD _SyntheticBar class and tick helper functions to signal_smt.py

- **WAVE**: 1
- **AGENT_ROLE**: implementer
- **DEPENDS_ON**: [1.1]
- **BLOCKS**: [1.3]
- **PROVIDES**: `_SyntheticBar`, `_update_tick_accumulator`, `_acc_to_df_row`
- **IMPLEMENT**: Insert after `_build_1s_buffer_df` (after line 124):
  1. `_SyntheticBar` class with `__slots__ = ("date", "open", "high", "low", "close", "volume")` and `__init__(self, acc)` that sets fields from accumulator dict.
  2. `_update_tick_accumulator(acc, price, size, second_ts) -> tuple[dict, dict | None]`: returns `(new_acc, finalized_acc_or_None)`. If `acc` is None or `second_ts != acc["second_ts"]` → `finalized = acc`, create new accumulator; else update existing and `finalized = None`. Returns `(updated_acc, finalized)`.
  3. `_acc_to_df_row(acc) -> pd.DataFrame`: one-row DataFrame with columns `["Open","High","Low","Close","Volume"]` and a single ET DatetimeIndex entry at `acc["second_ts"]`.
- **VALIDATE**: `python -c "import signal_smt; print('helpers ok')"`

#### Task 1.3: REPLACE on_mes_1s_bar and on_mnq_1s_bar with on_mes_tick and on_mnq_tick

- **WAVE**: 1
- **AGENT_ROLE**: implementer
- **DEPENDS_ON**: [1.2]
- **BLOCKS**: [1.4]
- **PROVIDES**: `on_mes_tick(ticker)`, `on_mnq_tick(ticker)`
- **IMPLEMENT**:
  - Delete `on_mes_1s_bar` (lines 248–253) and `on_mnq_1s_bar` (lines 256–266).
  - Add `on_mes_tick(ticker)`:
    ```
    global _mes_tick_bar, _mes_1s_buf
    if not ticker.tickByTicks: return
    t = ticker.tickByTicks[-1]
    second_ts = pd.Timestamp(t.time).tz_convert("America/New_York").floor("s")
    _mes_tick_bar, finalized = _update_tick_accumulator(_mes_tick_bar, t.price, t.size, second_ts)
    if finalized is not None:
        _mes_1s_buf = pd.concat([_mes_1s_buf, _acc_to_df_row(finalized)])
    ```
  - Add `on_mnq_tick(ticker)`:
    ```
    global _mnq_tick_bar, _mnq_1s_buf
    if not ticker.tickByTicks: return
    t = ticker.tickByTicks[-1]
    second_ts = pd.Timestamp(t.time).tz_convert("America/New_York").floor("s")
    _mnq_tick_bar, finalized = _update_tick_accumulator(_mnq_tick_bar, t.price, t.size, second_ts)
    if finalized is not None:
        _mnq_1s_buf = pd.concat([_mnq_1s_buf, _acc_to_df_row(finalized)])
        _process(_SyntheticBar(finalized))
    ```
- **VALIDATE**: `python -c "import signal_smt; print(hasattr(signal_smt, 'on_mnq_tick'), hasattr(signal_smt, 'on_mes_tick'))"`

#### Task 1.4: UPDATE main() — replace 1s reqHistoricalData with reqTickByTickData

- **WAVE**: 1
- **AGENT_ROLE**: implementer
- **DEPENDS_ON**: [1.3]
- **BLOCKS**: []
- **PROVIDES**: Wired `reqTickByTickData` subscriptions in `main()`
- **IMPLEMENT**:
  - Remove the `mnq_1s = _ib.reqHistoricalData(...)` block (lines 432–436).
  - Remove the `mes_1s = _ib.reqHistoricalData(...)` block (lines 442–446).
  - Remove `mnq_1s.updateEvent += on_mnq_1s_bar` and `mes_1s.updateEvent += on_mes_1s_bar`.
  - Add after the 1m `reqHistoricalData` calls:
    ```python
    mnq_tick = _ib.reqTickByTickData(mnq_contract, "AllLast", 0, False)
    mes_tick = _ib.reqTickByTickData(mes_contract, "AllLast", 0, False)
    mnq_tick.updateEvent += on_mnq_tick
    mes_tick.updateEvent += on_mes_tick
    ```
  - Update the module-level comment on line 3: `# Subscribes to dual 1m IB streams + reqTickByTickData("AllLast") per instrument`
- **VALIDATE**: `python -c "import signal_smt; import ast, inspect; src = inspect.getsource(signal_smt.main); assert 'reqTickByTickData' in src; assert 'on_mnq_1s_bar' not in src; print('main() ok')"`

**Wave 1 Checkpoint**: `python -c "import signal_smt; print('import ok')"`

---

### WAVE 2: Test Updates

#### Task 2.1: ADD _make_mock_ticker helper to test_signal_smt.py

- **WAVE**: 2
- **AGENT_ROLE**: implementer
- **DEPENDS_ON**: [1.3]
- **BLOCKS**: [2.2, 2.3]
- **PROVIDES**: `_make_mock_ticker` test helper
- **IMPLEMENT**: Insert after `_make_mock_bar` (after line 87):
  ```python
  def _make_mock_ticker(ts: str, price: float, size: float = 1.0, tz: str = "UTC"):
      """Build a mock ib_insync Ticker as delivered by reqTickByTickData."""
      tick = mock.MagicMock()
      tick.time  = pd.Timestamp(ts, tz=tz)
      tick.price = price
      tick.size  = size
      ticker = mock.MagicMock()
      ticker.tickByTicks = [tick]
      return ticker
  ```
- **VALIDATE**: `python -m pytest tests/test_signal_smt.py -x -q 2>&1 | head -5`

#### Task 2.2: UPDATE test_mes_1s_buf_always_appended to use on_mes_tick

- **WAVE**: 2
- **AGENT_ROLE**: implementer
- **DEPENDS_ON**: [2.1]
- **BLOCKS**: [2.3]
- **PROVIDES**: Updated buffer test using Ticker mock
- **IMPLEMENT**: Replace the body of `test_mes_1s_buf_always_appended`:
  - Remove the `bars = [None, bar]; signal_smt.on_mes_1s_bar(bars, hasNewBar=True)` lines.
  - Add two ticks at different seconds (14:06:00 UTC and 14:06:01 UTC) via `on_mes_tick`.
  - Assert `_mes_1s_buf` has length 0 after tick 1 (still in accumulator), length 1 after tick 2 (boundary crossed).
  - Also initialize `_mnq_tick_bar` and `_mes_tick_bar` to None via monkeypatch in the test setup (or directly before calling on_mes_tick).
- **VALIDATE**: `python -m pytest tests/test_signal_smt.py::test_mes_1s_buf_always_appended -v`

#### Task 2.3: ADD six new tick-specific tests

- **WAVE**: 2
- **AGENT_ROLE**: implementer
- **DEPENDS_ON**: [2.1, 2.2]
- **BLOCKS**: []
- **PROVIDES**: Test coverage for accumulator logic, boundary crossing, and schema compatibility
- **IMPLEMENT**: Add after the existing buffer management tests (after line 519):

  1. **`test_tick_accumulator_ohlcv_correct`**: monkeypatch `_mnq_tick_bar = None` and `_mnq_1s_buf = empty`. Send three ticks within the same second (prices 20001, 20005, 19998) via `on_mnq_tick`. Assert accumulator has `open=20001`, `high=20005`, `low=19998`, `close=19998`, `volume=3`. Buffer still empty (no boundary crossed). Also monkeypatch `_process` to a no-op so the buffer check is not confused by process calls.

  2. **`test_tick_boundary_finalizes_bar`**: Send tick at second S then tick at S+1 via `on_mnq_tick`. Assert `_mnq_1s_buf` has 1 row after second tick, with correct OHLCV from the first tick. Monkeypatch `signal_smt._process` to no-op.

  3. **`test_tick_boundary_calls_process`**: Monkeypatch `signal_smt._process` to a spy. Send two ticks at different seconds. Assert spy was called exactly once.

  4. **`test_mes_tick_does_not_call_process`**: Monkeypatch `signal_smt._process` to a spy. Send two MES ticks at different seconds via `on_mes_tick`. Assert spy was never called and `_mes_1s_buf` has 1 row.

  5. **`test_tick_no_tickbyticks_is_noop`**: Call `on_mnq_tick` with a Ticker whose `tickByTicks = []`. Assert `_mnq_1s_buf` is unchanged and `_mnq_tick_bar` is unchanged.

  6. **`test_acc_to_df_row_schema`**: Call `signal_smt._acc_to_df_row({"open": 100.0, "high": 105.0, "low": 99.0, "close": 102.0, "volume": 5.0, "second_ts": pd.Timestamp("2025-01-02 09:01:00", tz="America/New_York")})`. Assert columns == `["Open","High","Low","Close","Volume"]`, index timezone is ET, len == 1.

- **VALIDATE**: `python -m pytest tests/test_signal_smt.py -k "tick" -v`

**Wave 2 Checkpoint**: `python -m pytest tests/test_signal_smt.py -v`

---

### WAVE 3: Final Validation

#### Task 3.1: Run full test suite and confirm no regressions

- **WAVE**: 3
- **AGENT_ROLE**: implementer
- **DEPENDS_ON**: [2.3]
- **PROVIDES**: Green suite with new tests included
- **IMPLEMENT**: No code changes. Run the suite and fix any failures.
- **VALIDATE**: `python -m pytest tests/ -v --ignore=tests/test_ib_connection.py`

---

## TESTING STRATEGY

| Test | Type | Status | Tool | File | Run Command |
|------|------|--------|------|------|-------------|
| `test_tick_accumulator_ohlcv_correct` | Unit | ✅ Automated | pytest | `tests/test_signal_smt.py` | `pytest tests/test_signal_smt.py::test_tick_accumulator_ohlcv_correct` |
| `test_tick_boundary_finalizes_bar` | Unit | ✅ Automated | pytest | `tests/test_signal_smt.py` | `pytest tests/test_signal_smt.py::test_tick_boundary_finalizes_bar` |
| `test_tick_boundary_calls_process` | Unit | ✅ Automated | pytest | `tests/test_signal_smt.py` | `pytest tests/test_signal_smt.py::test_tick_boundary_calls_process` |
| `test_mes_tick_does_not_call_process` | Unit | ✅ Automated | pytest | `tests/test_signal_smt.py` | `pytest tests/test_signal_smt.py::test_mes_tick_does_not_call_process` |
| `test_tick_no_tickbyticks_is_noop` | Unit | ✅ Automated | pytest | `tests/test_signal_smt.py` | `pytest tests/test_signal_smt.py::test_tick_no_tickbyticks_is_noop` |
| `test_acc_to_df_row_schema` | Unit | ✅ Automated | pytest | `tests/test_signal_smt.py` | `pytest tests/test_signal_smt.py::test_acc_to_df_row_schema` |
| `test_mes_1s_buf_always_appended` (updated) | Unit | ✅ Automated | pytest | `tests/test_signal_smt.py` | `pytest tests/test_signal_smt.py::test_mes_1s_buf_always_appended` |
| All existing SCANNING/MANAGING tests | Unit | ✅ Automated | pytest | `tests/test_signal_smt.py` | `pytest tests/test_signal_smt.py -v` |
| No regressions in train_smt / backtest | Regression | ✅ Automated | pytest | `tests/` | `pytest tests/ --ignore=tests/test_ib_connection.py` |
| Live per-tick callback firing | Manual | ⚠️ Manual | Live IB | N/A | Run `python signal_smt.py` with IB Gateway active; confirm 1s bar completions in stdout within 1s of a trade |

**Manual test justification**: Requires a live IB Gateway connection with active CME market data subscription. Cannot be automated without a live brokerage account.

### Test Automation Summary

| | Count | % |
|---|---|---|
| ✅ Automated (pytest) | 9 | 90% |
| ⚠️ Manual | 1 | 10% |
| **Total** | 10 | 100% |

---

## VALIDATION COMMANDS

### Level 1: Syntax Check

```bash
python -c "import signal_smt; print('import ok')"
```

### Level 2: Unit Tests (tick-specific only)

```bash
python -m pytest tests/test_signal_smt.py -k "tick" -v
```

### Level 3: Full signal_smt test suite

```bash
python -m pytest tests/test_signal_smt.py -v
```

### Level 4: Full test suite (no regressions)

```bash
python -m pytest tests/ -v --ignore=tests/test_ib_connection.py
```

---

## ACCEPTANCE CRITERIA

### Functional
- [ ] `on_mnq_1s_bar` and `on_mes_1s_bar` are removed from `signal_smt.py`
- [ ] `on_mnq_tick(ticker)` and `on_mes_tick(ticker)` exist and accept an ib_insync `Ticker` object
- [ ] Multiple ticks within the same second accumulate into a single OHLCV bar: open = first price, high = max, low = min, close = last, volume = sum of sizes
- [ ] A completed second's bar is appended to `_mnq_1s_buf` / `_mes_1s_buf` only when the first tick of the next second arrives (boundary crossing)
- [ ] `_process()` is called exactly once per completed second, triggered by MNQ boundary crossing only
- [ ] `on_mes_tick` never calls `_process()`
- [ ] `_mnq_1s_buf` and `_mes_1s_buf` retain the same schema: columns `Open/High/Low/Close/Volume`, ET-localized DatetimeIndex

### Error Handling
- [ ] Ticker with empty `tickByTicks` list → no-op (no state change, no exception)
- [ ] First tick ever (accumulator is `None`) → accumulator initialized, no bar appended yet, `_process()` not called

### Integration
- [ ] `main()` uses `reqTickByTickData("AllLast")` for MNQ and MES; 1s `reqHistoricalData` calls removed
- [ ] `_SyntheticBar` objects pass through `_bar_timestamp()` correctly (ET-localized `pd.Timestamp` as `.date`)
- [ ] All existing state machine logic (`_process_scanning`, `_process_managing`, `screen_session`) unchanged

### Validation
- [ ] 6 new tick-specific tests pass — verified by: `pytest tests/test_signal_smt.py -k "tick" -v`
- [ ] `test_mes_1s_buf_always_appended` updated and passing — verified by: `pytest tests/test_signal_smt.py::test_mes_1s_buf_always_appended -v`
- [ ] No regressions — verified by: `pytest tests/ --ignore=tests/test_ib_connection.py`

### Out of Scope
- Sub-second signal evaluation — process fires on second boundary only, not on every tick
- Changes to `_process_scanning`, `_process_managing`, `screen_session`, or `manage_position`
- Changes to the 1m data pipeline, parquet persistence, or gap-fill logic
- Changes to `train_smt.py` or any backtest code

---

## COMPLETION CHECKLIST

- [ ] Task 1.1 complete: tick accumulator module globals added
- [ ] Task 1.2 complete: `_SyntheticBar`, `_update_tick_accumulator`, `_acc_to_df_row` added
- [ ] Task 1.3 complete: `on_mes_tick` and `on_mnq_tick` implemented, old handlers removed
- [ ] Task 1.4 complete: `main()` uses `reqTickByTickData`, old 1s `reqHistoricalData` removed
- [ ] Task 2.1 complete: `_make_mock_ticker` helper added to test file
- [ ] Task 2.2 complete: `test_mes_1s_buf_always_appended` updated
- [ ] Task 2.3 complete: 6 new tick tests added and passing
- [ ] Task 3.1 complete: full suite green
- [ ] Level 1–4 validation commands all pass
- [ ] All acceptance criteria met
- [ ] **⚠️ Debug logs added during execution REMOVED (keep pre-existing)**
- [ ] **⚠️ CRITICAL: Changes UNSTAGED — NOT committed**

---

## NOTES

### Why process only on second boundary (not on every tick)
SMT divergence patterns form over multi-bar structure (session highs/lows across minutes). Sub-second evaluation provides no signal accuracy benefit but adds complexity. Processing once per completed second gives ≤1s latency — a 5x improvement over the current ≤5s — which is sufficient for this strategy.

### Why _SyntheticBar instead of refactoring _process()
`_process()`, `_process_scanning()`, and `_process_managing()` all read `bar.open/high/low/close/volume` and call `_bar_timestamp(bar)`. Introducing `_SyntheticBar` lets all three functions remain unchanged and keeps the diff minimal. `_bar_timestamp` already handles pd.Timestamp inputs with tz set.

### Alignment gate behavior with tick data
The gate at `_process_scanning` line 291 (`_mes_1s_buf.index[-1] != _mnq_1s_buf.index[-1]`) compares the last *completed* second in each buffer. If MES has no trades in a given second, its buffer won't have that second's bar, and the gate will block until alignment is restored. This is the correct behavior — signals should not fire when one instrument has stale data.

### What happens to seconds with zero ticks
If no tick arrives for an instrument in a given second, that second simply has no bar in the buffer. The accumulator remains on the previous second until the next tick arrives. The alignment gate handles this gracefully.
