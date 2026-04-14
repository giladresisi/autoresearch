# Implementation Plan: signal_smt.py + train_smt.py Refactoring

**Created:** 2026-04-14
**Complexity:** 🔴 High
**Spec:** `signal_smt.md`

**Execution agent rules:**
- Make ALL code changes required by the plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`

---

## Overview

Two parallel bodies of work:

1. **Refactor `train_smt.py`** (root of main repo) — fix `compute_tdo` to use the
   00:00 ET midnight bar (futures instrument, not equity), update `screen_session` to
   remove internal session slicing and accept a pre-computed `tdo: float` parameter,
   and update `run_backtest` to supply both. Update all existing tests to match the new
   signatures.

2. **Create `signal_smt.py`** (root of main repo) — realtime SMT signal generator per
   the full spec in `signal_smt.md`. Dual IB subscriptions (1m + 1s) per instrument,
   1s-buffer–driven signal detection, state machine (SCANNING / MANAGING).

---

## Files

| File | Action |
|---|---|
| `train_smt.py` | Modify — `compute_tdo`, `screen_session`, `run_backtest` |
| `tests/test_smt_strategy.py` | Modify — update callers + mocks for new signatures |
| `tests/test_smt_backtest.py` | Modify — update `screen_session` mock signatures |
| `signal_smt.py` | Create — new realtime script |
| `tests/test_signal_smt.py` | Create — unit tests (no IB required) |

---

## Interface Contracts Between Waves

**Wave 1 → Wave 2 contract:**
- `compute_tdo(mnq_bars: pd.DataFrame, date: datetime.date) -> float | None`
  returns Open of the `00:00 ET` bar for `date` (first bar of day as fallback).
  Input `mnq_bars` must contain full-day data (not session-sliced).
- `screen_session(mnq_bars: pd.DataFrame, mes_bars: pd.DataFrame, tdo: float) -> dict | None`
  receives pre-sliced session bars and pre-computed TDO float.
  Does NOT reference `SESSION_START`, `SESSION_END`, or `compute_tdo` internally.

**Wave 2 → Wave 3 contract:**
- `run_backtest` call site pattern (for mock compatibility):
  ```python
  tdo = compute_tdo(mnq_df[mnq_df.index.date == day], day)
  signal = screen_session(mnq_session, mes_session, tdo or 0.0)
  ```
- All tests passing: `pytest tests/test_smt_strategy.py tests/test_smt_backtest.py`

**Wave 3 → Wave 4 contract:**
- `signal_smt.py` exposes importable helpers: `_apply_slippage`, `_format_signal_line`,
  `_format_exit_line`, `_build_1s_buffer_df` (empty DataFrame factory).
- State machine logic factored into testable functions (no IB dependency at unit level).

---

## Tasks

### WAVE 1 — train_smt.py core changes
*Can be done in one pass (same file). No dependencies on other waves.*

---

#### Task 1.1 — Fix `compute_tdo` (midnight bar)
**File:** `train_smt.py`
**Lines affected:** ~251–265 (the `compute_tdo` function)

Change `target_time` from `09:30:00` to `00:00:00`:

```python
# BEFORE:
target_time = pd.Timestamp(f"{date} 09:30:00", tz="America/New_York")

# AFTER:
target_time = pd.Timestamp(f"{date} 00:00:00", tz="America/New_York")
```

Update the fallback comment: "Falls back to first available bar on that date if
the 00:00 bar is absent."

No other changes to the function body needed.

---

#### Task 1.2 — Update `screen_session` signature
**File:** `train_smt.py`
**Lines affected:** ~302–412 (the `screen_session` function)

**a) Change signature:**
```python
# BEFORE:
def screen_session(
    mnq_bars: pd.DataFrame,
    mes_bars: pd.DataFrame,
    date: datetime.date,
) -> dict | None:

# AFTER:
def screen_session(
    mnq_bars: pd.DataFrame,   # pre-sliced to session window by caller
    mes_bars: pd.DataFrame,   # pre-sliced to session window by caller
    tdo: float,               # True Day Open, pre-computed by caller
) -> dict | None:
```

**b) Remove the internal session mask block** (~lines 329–339):
```python
# DELETE ENTIRELY:
session_mask = (
    (mnq_bars.index.date == date)
    & (mnq_bars.index.time >= pd.Timestamp(f"2000-01-01 {SESSION_START}").time())
    & (mnq_bars.index.time <= pd.Timestamp(f"2000-01-01 {SESSION_END}").time())
)
mnq_session = mnq_bars[session_mask]
mes_session = mes_bars[session_mask]

if mnq_session.empty or mes_session.empty:
    return None
```
After deletion, rename `mnq_session` → `mnq_bars` and `mes_session` → `mes_bars`
throughout the function body (they are the same thing now, the caller passes
the already-sliced data). Set `n_bars = len(mnq_bars)`.

**c) Remove the `compute_tdo` call** (~line 374):
```python
# DELETE:
tdo = compute_tdo(mnq_bars, date)
if tdo is None:
    continue
```
`tdo` is now the function parameter. Add a guard at the top of the scan loop instead:
```python
if tdo is None or tdo == 0.0:
    return None
```

**d) Update the `min_signal_ts` reference** to use `mnq_bars` instead of `mnq_session`:
```python
min_signal_ts = mnq_bars.index[0] + pd.Timedelta(minutes=MIN_BARS_BEFORE_SIGNAL)
```

**e) Update `entry_time`** to use `mnq_bars.index[entry_idx]` (was `mnq_session.index[entry_idx]`).

**f) Update docstring** to reflect new signature: note that caller pre-slices session bars
and pre-computes TDO, and that SESSION_START/END are not used internally.

---

### WAVE 2 — Update callers and tests
*Depends on Wave 1 (new signatures). Tasks 2.1, 2.2, 2.3 are independent of each other.*

---

#### Task 2.1 — Update `run_backtest`
**File:** `train_smt.py`
**Lines affected:** ~594–616 (the day loop signal detection block)

For each day, compute TDO from the full per-day slice before calling `screen_session`:

```python
# BEFORE (in the day loop):
if position is None:
    signal = screen_session(mnq_session, mes_session, day)

# AFTER:
if position is None:
    mnq_day = mnq_df[mnq_df.index.date == day]
    day_tdo = compute_tdo(mnq_day, day)
    if day_tdo is None:
        equity_curve.append(equity_curve[-1])
        continue
    signal = screen_session(mnq_session, mes_session, day_tdo)
```

`mnq_session` is still computed separately (for `_scan_bars_for_exit`) — no change there.

The `SESSION_START`/`SESSION_END` usages in `run_backtest` (the session_mask and
session_end_ts) are correct and remain unchanged — they belong here as the caller.

---

#### Task 2.2 — Update `tests/test_smt_strategy.py`
**File:** `tests/test_smt_strategy.py`

**a) Update `compute_tdo` tests** (lines ~200–244):
- `test_compute_tdo_finds_930_bar` → rename to `test_compute_tdo_finds_midnight_bar`.
  Build a DataFrame that includes a `00:00:00 ET` bar. Assert TDO = that bar's Open.
- `test_compute_tdo_proxy_no_930_bar` → rename to `test_compute_tdo_proxy_no_midnight_bar`.
  Build a DataFrame starting at e.g. `09:00:00 ET` (no midnight bar). Assert TDO = first bar Open.
- `test_compute_tdo_returns_none_on_empty` — no change needed.

**b) Update all `screen_session` call sites** (~lines 269–695):
Every call currently has the form `screen_session(mnq, mes, datetime.date(...))`.
Change to `screen_session(mnq, mes, tdo_value)` where `tdo_value` is a float.
For tests that were previously relying on internal TDO computation:
- Add a `00:00 ET` bar to the test fixture DataFrame (so `compute_tdo` would return it)
  OR pass a literal float (e.g. `20100.0`) directly.
- Tests that monkeypatched `compute_tdo` with `lambda bars, date: X.X` can instead
  pass the float directly to `screen_session` — **remove those `monkeypatch` calls**.

**c) Specific tests to update:**
| Test | Change |
|---|---|
| `test_screen_session_returns_short_signal` | Pass `tdo=19900.0` (bearish: TDO below entry) |
| `test_screen_session_returns_long_signal` | Pass `tdo=20100.0` (bullish: TDO above entry) |
| `test_screen_session_no_divergence_returns_none` | Pass any float; result unchanged |
| `test_tdo_is_take_profit` | Remove 9:30 bar setup; pass tdo float directly |
| All tests using `monkeypatch.setattr(train_smt, "compute_tdo", lambda ...)` | Remove monkeypatch; pass tdo float instead |

The `detect_smt_divergence` and `find_entry_bar` tests require no changes — their
signatures are unchanged.

---

#### Task 2.3 — Update `tests/test_smt_backtest.py`
**File:** `tests/test_smt_backtest.py`

All `patched_screen` functions currently have signature `(mnq_b, mes_b, d)` where `d`
is a `datetime.date`. After the change, the 3rd argument is `tdo: float`.

Update every `patched_screen` / mock function in the file:
```python
# BEFORE:
def patched_screen(mnq_b, mes_b, d):
    sig = original_screen(mnq_b, mes_b, d)

# AFTER:
def patched_screen(mnq_b, mes_b, tdo):
    sig = original_screen(mnq_b, mes_b, tdo)
```

Affected tests (scan for all `def patched_screen` definitions):
- `test_run_backtest_session_force_exit` (~line 141)
- `test_run_backtest_end_of_backtest_exit` (~line 168)
- `test_one_trade_per_day_max` (~line 214) — uses `counting_screen(mnq_b, mes_b, d)`

Also: `_build_short_signal_bars` and `_build_long_signal_bars` helpers start bars at
`09:00:00 ET`, so `run_backtest` will call `compute_tdo` on those bars and find no
midnight bar. The fallback in `compute_tdo` returns the first available bar's Open.
Verify the existing integration tests still pass with this fallback in place — no fixture
changes required since the fallback handles this case.

---

### WAVE 3 — Create `signal_smt.py`
*Depends on Wave 1 (imports `compute_tdo`, `screen_session`, `manage_position` with new signatures).*

---

#### Task 3.1 — Create `signal_smt.py`
**File:** `signal_smt.py` (new, in repo root)

##### 3.1.a Configuration constants (top of file)
```python
from pathlib import Path
import datetime, json
import pandas as pd
from ib_insync import IB, util, ContFuture

IB_HOST           = "127.0.0.1"
IB_PORT           = 4002
IB_CLIENT_ID      = 10

MNQ_CONID         = "770561201"   # MNQM6, expires 2026-06-18; next: MNQU6 = 793356225
MES_CONID         = "770561194"   # MESM6, expires 2026-06-18; next: MESU6 = 793356217

SESSION_START     = "09:00"       # ET — caller-side only; never passed to screen_session
SESSION_END       = "13:30"       # ET — caller-side only

ENTRY_SLIPPAGE_TICKS = 2          # ticks adverse fill; 1 tick = 0.25 MNQ points
MAX_LOOKBACK_DAYS    = 30
MNQ_PNL_PER_POINT    = 2.0

REALTIME_DATA_DIR = Path("data/realtime")
POSITION_FILE     = REALTIME_DATA_DIR / "position.json"
```

##### 3.1.b Import strategy functions
```python
from train_smt import screen_session, manage_position, compute_tdo
```

##### 3.1.c State variables (module-level)
```python
# IB and contracts (set in main())
_ib: IB = None
_mnq_contract = None
_mes_contract = None

# In-memory DataFrames
_mnq_1m_df:    pd.DataFrame = None   # loaded from parquet + gap-filled
_mes_1m_df:    pd.DataFrame = None
_mnq_1s_buf:   pd.DataFrame = None   # current-minute 1s bars, cleared on new 1m bar
_mes_1s_buf:   pd.DataFrame = None

# State machine
_state:         str = "SCANNING"     # "SCANNING" | "MANAGING"
_position:      dict | None = None
_startup_ts:    pd.Timestamp | None = None
_last_exit_ts:  pd.Timestamp = pd.Timestamp("1970-01-01", tz="America/New_York")

# Derived time objects (set in main())
_session_start_time = None
_session_end_time   = None
```

##### 3.1.d Helper: `_empty_bar_df()`
Returns an empty DataFrame with columns `["Open","High","Low","Close","Volume"]` and
a tz-aware DatetimeIndex (America/New_York).

##### 3.1.e Helper: `_apply_slippage(signal: dict) -> float`
```python
if signal["direction"] == "long":
    return signal["entry_price"] + ENTRY_SLIPPAGE_TICKS * 0.25
else:
    return signal["entry_price"] - ENTRY_SLIPPAGE_TICKS * 0.25
```

##### 3.1.f Helper: `_compute_pnl(position: dict, exit_price: float) -> float`
```python
sign = 1 if position["direction"] == "long" else -1
return sign * (exit_price - position["assumed_entry"]) * position["contracts"] * MNQ_PNL_PER_POINT
```

##### 3.1.g Helper: `_format_signal_line(ts, signal, assumed_entry) -> str`
Produces: `[HH:MM:SS] SIGNAL    long  | entry ~XXXXX.XX (+2t slip) | stop XXXXX.XX | TP XXXXX.XX | RR ~XXx`

##### 3.1.h Helper: `_format_exit_line(ts, exit_type, exit_price, pnl, contracts) -> str`
Produces: `[HH:MM:SS] EXIT      tp    | filled XXXXX.XX | P&L +$XX.XX | 1 MNQ1! contract`

##### 3.1.i Helper: `_load_parquets() -> tuple[pd.DataFrame, pd.DataFrame]`
Load `REALTIME_DATA_DIR/MNQ_1m.parquet` and `MES_1m.parquet`.
Return empty DataFrames (with correct columns + tz-aware index) if files don't exist.

##### 3.1.j Helper: `_gap_fill_1m(mnq_df, mes_df) -> tuple[pd.DataFrame, pd.DataFrame]`
1. Determine `last_ts`: `mnq_df.index[-1]` if non-empty, else `now - MAX_LOOKBACK_DAYS days`
2. Apply 30-day cap: `last_ts = max(last_ts, now - MAX_LOOKBACK_DAYS days)`
3. Import `IBGatewaySource` from the project's data source module
4. Fetch MNQ 1m bars from `last_ts → now` and MES 1m bars
5. Append to DataFrames, save parquets, return updated DataFrames

Check existing `IBGatewaySource` usage in `prepare_futures.py` to match the calling convention.

##### 3.1.k Callback: `on_mnq_1m_bar(bars, hasNewBar)`
```python
if not hasNewBar: return
bar = bars[-1]
# Convert bar to row, append to _mnq_1m_df, save parquet
# Clear _mnq_1s_buf
_mnq_1s_buf = _empty_bar_df()
```

##### 3.1.l Callback: `on_mes_1m_bar(bars, hasNewBar)`
Same as above for MES.

##### 3.1.m Callback: `on_mes_1s_bar(bars, hasNewBar)`
```python
if not hasNewBar: return
# Append bar to _mes_1s_buf (no signal logic)
```

##### 3.1.n Callback: `on_mnq_1s_bar(bars, hasNewBar)` — main entry point
```python
if not hasNewBar: return
bar = bars[-1]
_append_1s_bar(_mnq_1s_buf, bar)
_process(bar)
```

##### 3.1.o Core: `_process(bar)`
```python
def _process(bar):
    global _state, _position, _last_exit_ts

    bar_ts   = _bar_timestamp(bar)
    bar_time = bar_ts.time()

    if _state == "SCANNING":
        _process_scanning(bar, bar_ts, bar_time)
    else:
        _process_managing(bar, bar_ts, bar_time)
```

##### 3.1.p Core: `_process_scanning(bar, bar_ts, bar_time)`
```python
# 1. Session gate
if bar_time < _session_start_time or bar_time > _session_end_time:
    return

# 2. Alignment gate: MES 1s buffer must have same latest ts as MNQ 1s buffer
if _mes_1s_buf.empty or _mes_1s_buf.index[-1] != _mnq_1s_buf.index[-1]:
    return

# 3. Build combined DataFrames
combined_mnq = pd.concat([_mnq_1m_df, _mnq_1s_buf]) if not _mnq_1s_buf.empty else _mnq_1m_df
combined_mes = pd.concat([_mes_1m_df, _mes_1s_buf]) if not _mes_1s_buf.empty else _mes_1m_df

# 4. Slice to session window
today = bar_ts.date()
session_mask = (
    (combined_mnq.index.date == today)
    & (combined_mnq.index.time >= _session_start_time)
    & (combined_mnq.index.time <= _session_end_time)
)
mnq_session = combined_mnq[session_mask]
mes_session = combined_mes[session_mask]

# 5. Compute TDO from full 1m parquet (needs midnight bar)
tdo = compute_tdo(_mnq_1m_df, today)
if tdo is None:
    return

# 6. Detect signal
signal = screen_session(mnq_session, mes_session, tdo)
if signal is None:
    return

# 7. Stale startup guard
if _startup_ts is not None and signal["entry_time"] <= _startup_ts:
    return

# 8. Re-detection guard
if signal["entry_time"] <= _last_exit_ts:
    return

# 9. Open position
assumed_entry = _apply_slippage(signal)
_position = {**signal, "assumed_entry": assumed_entry, "contracts": 1, "instrument": "MNQ1!"}
POSITION_FILE.write_text(json.dumps({
    **_position,
    "entry_time": str(_position["entry_time"]),
}, indent=2))
print(_format_signal_line(bar_ts, signal, assumed_entry), flush=True)
_state = "MANAGING"
```

##### 3.1.q Core: `_process_managing(bar, bar_ts, bar_time)`
```python
# Convert bar to Series
bar_series = pd.Series({
    "Open": bar.open, "High": bar.high, "Low": bar.low,
    "Close": bar.close, "Volume": bar.volume,
}, name=bar_ts)

result = manage_position(_position, bar_series)

if result == "hold":
    if bar_time >= _session_end_time:
        result = "exit_session_end"
        exit_price = float(bar.close)
    else:
        return

if result == "exit_tp":
    exit_price = _position["take_profit"]
elif result == "exit_stop":
    exit_price = _position["stop_price"]

pnl = _compute_pnl(_position, exit_price)
print(_format_exit_line(bar_ts, result, exit_price, pnl, _position["contracts"]), flush=True)

_last_exit_ts = bar_ts
POSITION_FILE.unlink(missing_ok=True)
_position = None
_state = "SCANNING"
```

##### 3.1.r Main: `main()`
```python
def main():
    global _ib, _mnq_contract, _mes_contract, _mnq_1m_df, _mes_1m_df
    global _mnq_1s_buf, _mes_1s_buf, _state, _position, _startup_ts
    global _session_start_time, _session_end_time

    REALTIME_DATA_DIR.mkdir(parents=True, exist_ok=True)

    _session_start_time = pd.Timestamp(f"2000-01-01 {SESSION_START}").time()
    _session_end_time   = pd.Timestamp(f"2000-01-01 {SESSION_END}").time()

    _mnq_1s_buf = _empty_bar_df()
    _mes_1s_buf = _empty_bar_df()

    _mnq_1m_df, _mes_1m_df = _load_parquets()
    _mnq_1m_df, _mes_1m_df = _gap_fill_1m(_mnq_1m_df, _mes_1m_df)

    if POSITION_FILE.exists():
        _position = json.loads(POSITION_FILE.read_text())
        _state = "MANAGING"
        _startup_ts = None
    else:
        _state = "SCANNING"
        _startup_ts = pd.Timestamp.now(tz="America/New_York")

    _ib = IB()
    _ib.connect(IB_HOST, IB_PORT, clientId=IB_CLIENT_ID)

    from ib_insync import Future
    mnq_contract = Future(conId=int(MNQ_CONID))
    mes_contract = Future(conId=int(MES_CONID))

    mnq_1m = _ib.reqHistoricalData(
        mnq_contract, endDateTime='', durationStr='1 D',
        barSizeSetting='1 min', whatToShow='TRADES',
        useRTH=False, formatDate=2, keepUpToDate=True)
    mnq_1s = _ib.reqHistoricalData(
        mnq_contract, endDateTime='', durationStr='1800 S',
        barSizeSetting='1 secs', whatToShow='TRADES',
        useRTH=False, formatDate=2, keepUpToDate=True)
    mes_1m = _ib.reqHistoricalData(
        mes_contract, endDateTime='', durationStr='1 D',
        barSizeSetting='1 min', whatToShow='TRADES',
        useRTH=False, formatDate=2, keepUpToDate=True)
    mes_1s = _ib.reqHistoricalData(
        mes_contract, endDateTime='', durationStr='1800 S',
        barSizeSetting='1 secs', whatToShow='TRADES',
        useRTH=False, formatDate=2, keepUpToDate=True)

    mnq_1m.updateEvent += on_mnq_1m_bar
    mnq_1s.updateEvent += on_mnq_1s_bar
    mes_1m.updateEvent += on_mes_1m_bar
    mes_1s.updateEvent += on_mes_1s_bar

    util.run()

if __name__ == "__main__":
    main()
```

**Check `prepare_futures.py`** before implementing `_gap_fill_1m` to confirm the
`IBGatewaySource` import path and calling convention used elsewhere in the project.

---

### WAVE 4 — Tests for signal_smt.py
*Depends on Wave 3 (signal_smt.py must exist).*

---

#### Task 4.1 — Create `tests/test_signal_smt.py`
**File:** `tests/test_signal_smt.py` (new)

No IB connection required. All tests use synthetic DataFrames and mock `_ib`.

##### Test 1: `test_empty_bar_df_schema`
`_empty_bar_df()` returns DataFrame with correct columns and tz-aware ET DatetimeIndex.
```python
df = signal_smt._empty_bar_df()
assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
assert df.index.tz is not None
```

##### Test 2: `test_apply_slippage_long`
Long signal: assumed_entry = entry_price + ENTRY_SLIPPAGE_TICKS * 0.25
```python
signal = {"direction": "long", "entry_price": 20000.0, ...}
result = signal_smt._apply_slippage(signal)
assert result == pytest.approx(20000.5)  # 2 ticks * 0.25
```

##### Test 3: `test_apply_slippage_short`
Short signal: assumed_entry = entry_price - ENTRY_SLIPPAGE_TICKS * 0.25
```python
assert result == pytest.approx(19999.5)
```

##### Test 4: `test_compute_pnl_long_tp`
Long, exit at TP: pnl = (tp - assumed_entry) * contracts * 2.0
```python
pos = {"direction": "long", "assumed_entry": 19850.5, "contracts": 1, ...}
pnl = signal_smt._compute_pnl(pos, 19890.0)
assert pnl == pytest.approx((19890.0 - 19850.5) * 1 * 2.0)
```

##### Test 5: `test_compute_pnl_short_stop`
Short, exit at stop: pnl = -(stop - assumed_entry) * contracts * 2.0 (negative)
```python
pos = {"direction": "short", "assumed_entry": 19903.0, "contracts": 1, ...}
pnl = signal_smt._compute_pnl(pos, 19904.5)
assert pnl < 0
```

##### Test 6: `test_format_signal_line_long`
Output contains "SIGNAL", "long", entry price string, stop, TP.
```python
line = signal_smt._format_signal_line(ts, signal, 19850.5)
assert "SIGNAL" in line and "long" in line and "19850.5" in line
```

##### Test 7: `test_format_exit_line_tp`
Output contains "EXIT", "tp", exit price, P&L.
```python
line = signal_smt._format_exit_line(ts, "exit_tp", 19890.0, 78.5, 1)
assert "EXIT" in line and "tp" in line and "78.5" in line
```

##### Test 8: `test_process_scanning_session_gate_before_start`
Bar arriving before SESSION_START is silently skipped (no signal, state unchanged).
- Mock `screen_session` to raise if called.
- Set bar_time to "08:59:59 ET".
- Assert `screen_session` was never called.

##### Test 9: `test_process_scanning_session_gate_after_end`
Bar arriving after SESSION_END is silently skipped.

##### Test 10: `test_process_scanning_alignment_gate`
MES 1s buffer has different latest timestamp → `screen_session` not called.

##### Test 11: `test_process_scanning_no_signal`
Valid session bar, `screen_session` returns None → state stays SCANNING.

##### Test 12: `test_process_scanning_stale_startup_guard`
Signal `entry_time` <= `startup_ts` → skipped, state stays SCANNING.

##### Test 13: `test_process_scanning_redetection_guard`
Signal `entry_time` <= `last_exit_ts` → skipped, state stays SCANNING.

##### Test 14: `test_process_scanning_valid_signal_transitions_to_managing`
All gates pass, `screen_session` returns valid signal → state becomes MANAGING,
`position.json` is written, slippage is applied to `assumed_entry`.
```python
# Use tmp_path for POSITION_FILE
signal_smt.POSITION_FILE = tmp_path / "position.json"
# ... inject signal, verify state == "MANAGING" and position.json content
```

##### Test 15: `test_process_managing_hold`
manage_position returns "hold", time < SESSION_END → state stays MANAGING.

##### Test 16: `test_process_managing_exit_tp`
manage_position returns "exit_tp" → state becomes SCANNING, position.json deleted,
pnl is positive (using long TP scenario).

##### Test 17: `test_process_managing_exit_stop`
manage_position returns "exit_stop" → state becomes SCANNING, position.json deleted,
pnl is negative.

##### Test 18: `test_process_managing_session_end_force_close`
manage_position returns "hold" but bar_time >= SESSION_END → forced close at bar close,
state becomes SCANNING.

##### Test 19: `test_1s_buf_cleared_on_1m_bar`
Call `on_mnq_1m_bar` with a mock bars list (hasNewBar=True) → `_mnq_1s_buf` is empty
afterwards.

##### Test 20: `test_mes_1s_buf_always_appended`
State = MANAGING, `on_mes_1s_bar` called → bar appended to `_mes_1s_buf`.

##### Test 21: `test_load_parquets_missing_files`
When parquet files don't exist, `_load_parquets` returns empty DataFrames with correct
schema (not FileNotFoundError).

##### Test 22: `test_30_day_cap_on_gap_fill`
`_gap_fill_1m` with empty DataFrames requests no more than 30 days back from IB.
Mock `IBGatewaySource.fetch` and assert the `start` argument is >= `now - 31 days`.

**Run command:** `pytest tests/test_signal_smt.py -v`

---

## Test Coverage Summary

| Code path | Tests | Status |
|---|---|---|
| `compute_tdo` midnight bar | Task 2.2 — test_compute_tdo_finds_midnight_bar | ✅ |
| `compute_tdo` no midnight fallback | Task 2.2 — test_compute_tdo_proxy_no_midnight_bar | ✅ |
| `compute_tdo` empty DataFrame | Task 2.2 — test_compute_tdo_returns_none_on_empty | ✅ (unchanged) |
| `screen_session` new signature (short) | Task 2.2 — test_screen_session_returns_short_signal | ✅ |
| `screen_session` new signature (long) | Task 2.2 — test_screen_session_returns_long_signal | ✅ |
| `screen_session` no divergence | Task 2.2 — test_screen_session_no_divergence_returns_none | ✅ |
| `screen_session` TDO validity gate | Task 2.2 — existing tests (tdo_validity_check family) | ✅ |
| `screen_session` direction filter | Task 2.2 — existing tests (trade_direction family) | ✅ |
| `run_backtest` TDO from midnight bar | Task 2.3 — existing integration tests with fallback | ✅ |
| `run_backtest` mock patches updated | Task 2.3 — test_smt_backtest.py patches | ✅ |
| `_empty_bar_df` schema | Task 4.1 — Test 1 | ✅ |
| `_apply_slippage` long/short | Task 4.1 — Tests 2, 3 | ✅ |
| `_compute_pnl` long/short | Task 4.1 — Tests 4, 5 | ✅ |
| `_format_signal_line` | Task 4.1 — Test 6 | ✅ |
| `_format_exit_line` | Task 4.1 — Test 7 | ✅ |
| SCANNING session gate | Task 4.1 — Tests 8, 9 | ✅ |
| SCANNING alignment gate | Task 4.1 — Test 10 | ✅ |
| SCANNING no signal | Task 4.1 — Test 11 | ✅ |
| SCANNING stale startup guard | Task 4.1 — Test 12 | ✅ |
| SCANNING re-detection guard | Task 4.1 — Test 13 | ✅ |
| SCANNING → MANAGING transition | Task 4.1 — Test 14 | ✅ |
| MANAGING hold | Task 4.1 — Test 15 | ✅ |
| MANAGING exit_tp | Task 4.1 — Test 16 | ✅ |
| MANAGING exit_stop | Task 4.1 — Test 17 | ✅ |
| MANAGING SESSION_END force close | Task 4.1 — Test 18 | ✅ |
| 1m bar clears 1s buffer | Task 4.1 — Test 19 | ✅ |
| MES 1s buffer appended in MANAGING | Task 4.1 — Test 20 | ✅ |
| `_load_parquets` missing files | Task 4.1 — Test 21 | ✅ |
| 30-day cap on gap-fill | Task 4.1 — Test 22 | ✅ |
| Live IB connection + dual subscriptions | **Manual** — requires live IB-Gateway | ⚠️ Manual |
| Live stop/TP detection at 1s precision | **Manual** — requires live market data | ⚠️ Manual |

**Manual test justification:** IB-Gateway connection requires a running TWS/Gateway instance with active market data subscription. Cannot be automated in CI without a live brokerage account and market hours.

---

## Verification

After all tasks complete, run:

```bash
cd C:\Users\gilad\projects\auto-co-trader\auto-co-trader-main
.venv/Scripts/python -m pytest tests/test_smt_strategy.py tests/test_smt_backtest.py tests/test_signal_smt.py -v
```

All tests must pass. The full suite (`pytest`) must not regress.

---

## Risks and Mitigations

| Risk | Mitigation |
|---|---|
| `test_smt_strategy.py` has ~20 call sites patching `compute_tdo` lambda | Systematically grep for `compute_tdo` in tests; each becomes a direct float arg |
| `run_backtest` test helpers start bars at 09:00 (no midnight bar) | `compute_tdo` fallback to first bar handles this; verify tests still pass |
| `IBGatewaySource` import path may differ from expected | Read `prepare_futures.py` before implementing `_gap_fill_1m` |
| Mixed 1m+1s DataFrame index may trigger pandas concat warnings | Use `sort_index()` after concat; verify no duplicates at minute boundary |
| `_process_scanning` called ~60x/minute (per 1s bar) — performance | Session slice is ~270–330 bars; each `screen_session` call is O(n) scan, negligible at this scale |
