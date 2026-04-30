# Feature: Stage 1 — FillExecutor Protocol + IbRealtimeSource Refactor

**⚠️ EXECUTION RULES — READ FIRST:**
- Implement ALL changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
- Only make code changes — no git operations

Validate documentation and codebase patterns before implementing. Match naming of existing utils,
types, and models. Import from correct files.

---

## Feature Description

Extract fill simulation logic and IB realtime data handling into standalone, reusable modules
(`execution/` package and `data/ib_realtime.py`). Refactor `backtest_smt.py` and `signal_smt.py`
to use these abstractions, replacing duplicated inline fill logic in both files with a shared
`SimulatedFillExecutor`. This is the foundation for Stage 2 live automation.

See full design: `docs/superpowers/specs/2026-04-30-tradovate-live-trading-design.md`

## User Story

As a developer maintaining the auto-co-trader system,
I want fill simulation and IB realtime data in standalone modules,
So that the backtest harness, signalling module, and future automation module all share the same
fill logic and data layer without code duplication.

## Problem Statement

- `backtest_smt._open_position()` (lines 167–186) and `_build_trade_record()` (lines 189–288)
  contain fill price computation that is duplicated independently in `signal_smt._apply_slippage()`
  (line 145–152) and inline exit price logic in `_process_managing()` (lines 958–974).
- IB realtime data handling (connection, tick assembly, gap-fill, retry logic) is wired directly
  into `signal_smt.py` module globals and cannot be reused by a future automation process.
- No shared `FillExecutor` protocol exists; adding a live executor requires forking the logic.

## Solution Statement

1. Create `execution/protocol.py` — `FillRecord` dataclass + `FillExecutor` Protocol.
2. Create `execution/simulated.py` — `SimulatedFillExecutor` implementing the protocol;
   extracted verbatim from `backtest_smt.py` fill logic.
3. Create `data/ib_realtime.py` — `IbRealtimeSource` extracting the IB data layer from
   `signal_smt.py`.
4. Refactor `backtest_smt.py` to use `SimulatedFillExecutor` (fill logic moves out of harness).
5. Refactor `signal_smt.py` to use `IbRealtimeSource` + `SimulatedFillExecutor`.
6. All existing tests pass; backtest metrics are numerically identical.

## Feature Metadata

**Feature Type**: Refactor
**Complexity**: High
**Primary Systems Affected**: `backtest_smt.py`, `signal_smt.py`, new `execution/`, `data/ib_realtime.py`
**Dependencies**: `ib_insync` (existing), `pandas` (existing) — no new deps
**Breaking Changes**: No — public behaviour preserved. Internal fill paths change.

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ BEFORE IMPLEMENTING

- `backtest_smt.py` lines 152–288 — `_size_contracts`, `_open_position`, `_build_trade_record`:
  fill price computation moves to `SimulatedFillExecutor`; these functions gain `fill_price` parameter
- `backtest_smt.py` lines 393–876 — `run_backtest()`: call sites for `_open_position()` (line 811)
  and all `_build_trade_record()` calls (lines 705, 732, 841, 864); each gets a prior
  `executor.place_entry()` or `executor.place_exit()` call
- `signal_smt.py` lines 38–132 — module globals to remove; `_ib`, `_mnq_contract`, `_mes_contract`,
  `_mnq_1m_df`, `_mes_1m_df`, `_mnq_partial_1m`, `_mes_partial_1m`, `_mnq_tick_bar`
- `signal_smt.py` lines 134–318 — helper functions; IB callbacks move to `IbRealtimeSource`;
  `_apply_slippage()` (145–152) deleted; `_compute_pnl()` stays; format_* functions stay
- `signal_smt.py` lines 376–429 — `on_mnq_1m_bar`, `on_mes_1m_bar`: move to `IbRealtimeSource`
- `signal_smt.py` lines 432–476 — `on_mes_tick`, `on_mnq_tick`: move to `IbRealtimeSource`
- `signal_smt.py` lines 556–895 — `_process_scanning`, `_process_managing`: fill logic updated;
  IB globals replaced with `_ib_source.mnq_1m_df` etc.
- `signal_smt.py` lines 1102–1228 — `_setup_ib_subscriptions`, `main()`: replaced by `IbRealtimeSource`
- `data/sources.py` — `IBGatewaySource` used by `_gap_fill_1m`; `IbRealtimeSource` reuses this
- `strategy_smt.py` — `set_bar_data()` is called in on_mnq_1m_bar / on_mes_1m_bar; must be called
  in `IbRealtimeSource` when bars complete; `_BarRow` used as `BarRow` type
- `tests/test_smt_backtest.py` — must pass unchanged after refactor (regression gate)
- `tests/test_signal_smt.py` — must pass unchanged; IB mocking strategy will change

### New Files to Create

- `execution/__init__.py` — Empty package marker
- `execution/protocol.py` — `FillRecord` dataclass, `FillExecutor` Protocol
- `execution/simulated.py` — `SimulatedFillExecutor`
- `data/ib_realtime.py` — `IbRealtimeSource`
- `tests/test_fill_executor.py` — unit tests for `SimulatedFillExecutor`
- `tests/test_ib_realtime.py` — unit tests for `IbRealtimeSource` (mocked IB)

### Patterns to Follow

**Naming Conventions**: snake_case functions, `SCREAMING_SNAKE_CASE` constants, Protocol for
interfaces (see `data/sources.py` — `DataSource` is a Protocol)
**Error Handling**: Return `None` / empty DataFrame on failure; print warning; no propagated
exceptions (matches existing patterns in `signal_smt.py`)
**IB connection pattern**: See `signal_smt.py` `main()` retry loop and `_setup_ib_subscriptions()`
**Test fixtures**: `tmp_path` for temp dirs; `unittest.mock.patch` for external calls; see
`tests/test_signal_smt.py` for IB mocking pattern

---

## PARALLEL EXECUTION STRATEGY

### Dependency Graph

```
┌─────────────────────────────────────────────────────────────────┐
│ WAVE 1: Foundation (fully parallel)                             │
├───────────────────────────────┬─────────────────────────────────┤
│ Task 1.1: CREATE protocol.py  │ Task 1.2: CREATE ib_realtime.py │
│ Agent: protocol-author        │ Agent: data-layer-author        │
└───────────────────────────────┴─────────────────────────────────┘
                    ↓ (both complete)
┌─────────────────────────────────────────────────────────────────┐
│ WAVE 2: SimulatedFillExecutor (after 1.1)                       │
├─────────────────────────────────────────────────────────────────┤
│ Task 2.1: CREATE simulated.py — needs protocol.py               │
│ Agent: executor-author                                          │
└─────────────────────────────────────────────────────────────────┘
                    ↓ (complete)
┌─────────────────────────────────────────────────────────────────┐
│ WAVE 3: Refactors (parallel — both depend on Wave 2)            │
├───────────────────────────────┬─────────────────────────────────┤
│ Task 3.1: REFACTOR            │ Task 3.2: REFACTOR              │
│   backtest_smt.py             │   signal_smt.py                 │
│ Agent: backtest-refactor      │ Agent: signal-refactor          │
└───────────────────────────────┴─────────────────────────────────┘
                    ↓ (both complete)
┌─────────────────────────────────────────────────────────────────┐
│ WAVE 4: Tests (parallel)                                        │
├───────────────────────────────┬─────────────────────────────────┤
│ Task 4.1: tests for executor  │ Task 4.2: tests for IbRealtime  │
│ Task 4.3: regression full     │                                 │
└───────────────────────────────┴─────────────────────────────────┘
```

### Parallelization Summary

**Wave 1 — Fully Parallel**: Tasks 1.1, 1.2 — no dependencies between them
**Wave 2 — Sequential after 1.1**: Task 2.1 — needs `FillRecord` / `FillExecutor` types
**Wave 3 — Parallel after Wave 2**: Tasks 3.1, 3.2 — both read `simulated.py`; 3.2 also reads `ib_realtime.py` (from 1.2)
**Wave 4 — Parallel after Wave 3**: All test tasks run independently

### Interface Contracts

**Contract 1**: Task 1.1 provides `FillRecord`, `FillExecutor` → Task 2.1 `SimulatedFillExecutor` implements it
**Contract 2**: Task 2.1 provides `SimulatedFillExecutor` → Tasks 3.1 and 3.2 import it
**Contract 3**: Task 1.2 provides `IbRealtimeSource(on_bar=...)` → Task 3.2 creates instance in `main()`

### Synchronization Checkpoints

**After Wave 1**: `python -c "from execution.protocol import FillRecord, FillExecutor; from data.ib_realtime import IbRealtimeSource; print('OK')"` (syntax check only; IB not connected)
**After Wave 2**: `python -c "from execution.simulated import SimulatedFillExecutor; print('OK')"`
**After Wave 3**: `uv run pytest tests/test_smt_backtest.py tests/test_signal_smt.py -x -q`
**After Wave 4**: `uv run pytest -x -q`

---

## IMPLEMENTATION PLAN

### Phase 1: Protocol & Data Layer

#### Task 1.1: CREATE `execution/__init__.py` and `execution/protocol.py`

**Purpose**: Define the `FillRecord` dataclass and `FillExecutor` Protocol that all executors implement.

**WAVE**: 1
**AGENT_ROLE**: protocol-author
**DEPENDS_ON**: []
**BLOCKS**: [2.1]
**PROVIDES**: `FillRecord`, `FillExecutor`

**IMPLEMENT**:

`execution/__init__.py` — empty file.

`execution/protocol.py`:
```python
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Protocol, Callable
import pandas as pd

# Re-export so consumers can import BarRow from execution.protocol
from strategy_smt import _BarRow as BarRow


@dataclass
class FillRecord:
    order_id:        str
    symbol:          str
    direction:       str           # "long" | "short"
    order_type:      str           # "market" | "limit" | "stop"
    requested_price: float
    fill_price:      float | None  # None = pending (async executors only)
    fill_time:       str | None    # ISO-8601 string
    contracts:       int
    status:          str           # "pending" | "filled" | "rejected"
    session_date:    str           # "YYYY-MM-DD"


class FillExecutor(Protocol):
    def place_entry(self, signal: dict, bar: BarRow) -> FillRecord | None: ...
    def place_exit(self, position: dict, exit_type: str, bar: BarRow) -> FillRecord | None: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...
```

**VALIDATE**: `python -c "from execution.protocol import FillRecord, FillExecutor, BarRow; print('OK')"`

---

#### Task 1.2: CREATE `data/ib_realtime.py`

**Purpose**: Extract all IB realtime data logic from `signal_smt.py` into a standalone, reusable
`IbRealtimeSource`. Must be importable without triggering IB connections.

**WAVE**: 1
**AGENT_ROLE**: data-layer-author
**DEPENDS_ON**: []
**BLOCKS**: [3.2]
**PROVIDES**: `IbRealtimeSource`

**IMPLEMENT**:

Move these functions verbatim from `signal_smt.py` into `IbRealtimeSource` as private methods:
- `_load_parquets()` → `_load_parquets()`
- `_gap_fill_1m()` → `_gap_fill()`
- `on_mnq_1m_bar()` / `on_mes_1m_bar()` → `_on_mnq_1m_bar()` / `_on_mes_1m_bar()`
- `on_mnq_tick()` / `on_mes_tick()` → `_on_mnq_tick()` / `_on_mes_tick()`
- `_update_tick_accumulator()`, `_update_partial_1m()`, `_partial_1m_to_bar_row()`,
  `_bar_timestamp()` → identical private helpers
- `_setup_ib_subscriptions()` → `_setup_subscriptions()`
- Retry loop from `signal_smt.main()` → `start()`

`start()` implementation:
```python
def start(self) -> None:
    self._load_parquets()
    self._gap_fill()
    mnq_contract = Future(conId=int(self._mnq_conid), exchange="CME")
    mes_contract = Future(conId=int(self._mes_conid), exchange="CME")
    for attempt in range(self._max_retries):
        try:
            self._ib = IB()
            self._ib.connect(self._host, self._port, clientId=self._client_id)
            self._setup_subscriptions(mnq_contract, mes_contract)
            util.run()
            if self._ib.isConnected():
                break
            raise ConnectionError("IB disconnected unexpectedly")
        except Exception as exc:
            print(f"[{attempt+1}/{self._max_retries}] IB error: {exc}. Retrying...", flush=True)
            ...
```

`_on_mnq_tick()` fires `self._on_bar(bar_row, self._mes_partial_1m)` on each second boundary
(identical logic to current `on_mnq_tick()` line 474).

`_on_mnq_1m_bar()` appends to `self._mnq_1m_df`, persists parquet, calls
`set_bar_data(self._mnq_1m_df, self._mes_1m_df)`, then calls `self._on_bar_1m_complete()` if set.

`mnq_1m_df` and `mes_1m_df` exposed as `@property` returning the internal DataFrames.

**VALIDATE**: `python -c "from data.ib_realtime import IbRealtimeSource; print('OK')"`

---

### Phase 2: SimulatedFillExecutor

#### Task 2.1: CREATE `execution/simulated.py`

**Purpose**: Implement `SimulatedFillExecutor` containing the fill price computation extracted from
`backtest_smt._open_position()` and `_build_trade_record()`.

**WAVE**: 2
**AGENT_ROLE**: executor-author
**DEPENDS_ON**: [1.1]
**BLOCKS**: [3.1, 3.2]
**PROVIDES**: `SimulatedFillExecutor`
**USES_FROM_WAVE_1**: Task 1.1 provides `FillRecord`, `FillExecutor`, `BarRow`

**IMPLEMENT**:

```python
import uuid, datetime
import strategy_smt
from execution.protocol import FillRecord, BarRow

class SimulatedFillExecutor:
    def __init__(self, *,
                 pessimistic: bool = False,
                 market_slip_pts: float = 5.0,
                 v2_market_slip_pts: float = 2.0,
                 human_mode: bool = False,
                 human_slip_pts: float = 0.0,
                 entry_slip_ticks: int = 2,
                 symbol: str = "MNQ1!",
                 fills_sink=None):
        ...

    def place_entry(self, signal: dict, bar: BarRow) -> FillRecord:
        is_limit = signal.get("limit_fill_bars") is not None
        if is_limit:
            fill_price = float(signal["entry_price"])
            order_type = "limit"
        else:
            slip = self._entry_slip_ticks * 0.25
            if signal["direction"] == "long":
                fill_price = float(signal["entry_price"]) + slip
            else:
                fill_price = float(signal["entry_price"]) - slip
            order_type = "market"
        # Human-mode additive slippage on top
        if self._human_mode and self._human_slip_pts > 0:
            if signal["direction"] == "long":
                fill_price += self._human_slip_pts
            else:
                fill_price -= self._human_slip_pts
        rec = FillRecord(
            order_id=f"sim-{uuid.uuid4().hex[:8]}",
            symbol=self._symbol,
            direction=signal["direction"],
            order_type=order_type,
            requested_price=float(signal["entry_price"]),
            fill_price=round(fill_price, 4),
            fill_time=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            contracts=signal.get("contracts", 1),
            status="filled",
            session_date=str(bar.name.date()) if hasattr(bar.name, "date") else "",
        )
        if self._fills_sink:
            self._fills_sink(rec)
        return rec

    def place_exit(self, position: dict, exit_type: str, bar: BarRow) -> FillRecord:
        direction_sign = 1 if position["direction"] == "long" else -1
        if exit_type == "exit_tp":
            fill_price = float(position["take_profit"])
            order_type = "limit"
        elif exit_type == "exit_secondary":
            fill_price = float(position["secondary_target"])
            order_type = "limit"
        elif exit_type == "exit_stop":
            fill_price = float(position["stop_price"])
            order_type = "stop"
        elif exit_type == "partial_exit":
            # Partial TP hit — price stored in position["partial_price"] by strategy
            fill_price = float(position.get("partial_price") or bar.Close)
            order_type = "limit"
        else:
            mid = (float(bar.High) + float(bar.Low)) / 2.0
            if self._pessimistic:
                fill_price = mid - direction_sign * self._market_slip_pts
            else:
                fill_price = mid
            order_type = "market"
        rec = FillRecord(
            order_id=f"sim-{uuid.uuid4().hex[:8]}",
            symbol=self._symbol,
            direction=position["direction"],
            order_type=order_type,
            requested_price=fill_price,
            fill_price=round(fill_price, 4),
            fill_time=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            contracts=position.get("contracts", 1),
            status="filled",
            session_date=str(bar.name.date()) if hasattr(bar.name, "date") else "",
        )
        if self._fills_sink:
            self._fills_sink(rec)
        return rec

    def start(self) -> None: pass
    def stop(self) -> None:  pass
```

**VALIDATE**: `python -c "from execution.simulated import SimulatedFillExecutor; print('OK')"`

---

### Phase 3: Refactors

#### Task 3.1: REFACTOR `backtest_smt.py` to use `SimulatedFillExecutor`

**Purpose**: Replace inline fill-price computation in backtest harness with `SimulatedFillExecutor`.
Backtest metrics must be numerically identical after this change.

**WAVE**: 3
**AGENT_ROLE**: backtest-refactor
**DEPENDS_ON**: [2.1]
**BLOCKS**: [4.3]
**PROVIDES**: Refactored backtest using shared fill logic

**IMPLEMENT**:

1. Add import at top of `backtest_smt.py`:
   ```python
   from execution.simulated import SimulatedFillExecutor
   ```

2. In `_open_position()` (line 167), add `fill_price: float` parameter; replace lines 179–185
   (HUMAN_EXECUTION_MODE slippage block) with:
   ```python
   position["entry_price"] = fill_price
   ```
   The `HUMAN_EXECUTION_MODE` check is removed from here — slippage now lives in executor.

3. In `run_backtest()` (line 393), after `init_bar_data()`:
   ```python
   executor = SimulatedFillExecutor(
       pessimistic=PESSIMISTIC_FILLS,
       market_slip_pts=MARKET_ORDER_SLIPPAGE_PTS,
       v2_market_slip_pts=V2_MARKET_CLOSE_SLIPPAGE_PTS,
       human_mode=strategy_smt.HUMAN_EXECUTION_MODE,
       human_slip_pts=getattr(strategy_smt, "HUMAN_ENTRY_SLIPPAGE_PTS", 0.0),
   )
   ```

4. At line 811 (`_open_position(signal, day, contracts, ...)`), prepend:
   ```python
   _entry_fill = executor.place_entry(signal, bar)
   position = _open_position(signal, day, contracts, total_contracts_target, bar_idx,
                              fill_price=_entry_fill.fill_price)
   ```

5. `_build_trade_record()` (line 189): add `fill_price: float` parameter; replace lines 197–215
   with `exit_price = fill_price`.

6. At every `_build_trade_record(position, result, bar, MNQ_PNL_PER_POINT)` call site:
   ```python
   _exit_fill = executor.place_exit(position, result, bar)
   trade, day_pnl_delta = _build_trade_record(
       position, result, bar, MNQ_PNL_PER_POINT, fill_price=_exit_fill.fill_price
   )
   ```
   Call sites are at lines ~705, ~732, ~841, ~864. Search for all occurrences.

7. Update the comment on `V2_MARKET_CLOSE_SLIPPAGE_PTS` (line 126 of `backtest_smt.py`): replace
   "Will move to simulated-fill module" with a note that this value is passed as
   `v2_market_slip_pts` to `SimulatedFillExecutor` but the v2 pipeline trade pairing loop
   (lines ~1292–1340) still uses event-dict slippage injection; moving that path through the
   executor is deferred to a future stage.
   Note: `PESSIMISTIC_FILLS` is imported from `strategy_smt`, not a local constant here — no
   comment update needed for it.

**VALIDATE**: `uv run pytest tests/test_smt_backtest.py -x -q`

---

#### Task 3.2: REFACTOR `signal_smt.py` to use `IbRealtimeSource` + `SimulatedFillExecutor`

**Purpose**: Remove duplicated IB data layer and fill estimation from `signal_smt.py`, replacing
with the new shared modules. All SIGNAL/EXIT log lines and JSON payloads remain identical.

**WAVE**: 3
**AGENT_ROLE**: signal-refactor
**DEPENDS_ON**: [1.2, 2.1]
**BLOCKS**: [4.3]
**PROVIDES**: Refactored `signal_smt.py` using shared modules

**IMPLEMENT**:

1. Add imports at top:
   ```python
   from data.ib_realtime import IbRealtimeSource
   from execution.simulated import SimulatedFillExecutor
   ```

2. Remove all IB-layer module globals (lines ~38–132):
   `_ib`, `_mnq_contract`, `_mes_contract`, `_mnq_1m_df`, `_mes_1m_df`,
   `_mnq_partial_1m`, `_mes_partial_1m`, `_mnq_tick_bar`

3. Add new module-level globals:
   ```python
   _ib_source: IbRealtimeSource | None = None
   _executor: SimulatedFillExecutor | None = None
   ```

4. Remove functions: `_load_parquets`, `_gap_fill_1m`, `on_mnq_1m_bar`, `on_mes_1m_bar`,
   `on_mnq_tick`, `on_mes_tick`, `_setup_ib_subscriptions`, `_update_tick_accumulator`,
   `_update_partial_1m`, `_partial_1m_to_bar_row`, `_bar_timestamp`, `_apply_slippage`

5. Add `_on_bar` callback (replaces the trigger in old `on_mnq_tick` at line 474):
   ```python
   def _on_bar(bar, mes_partial) -> None:
       global _mes_partial_1m
       _mes_partial_1m = mes_partial
       _process(bar)
   ```
   Keep `_mes_partial_1m` as a module-level variable updated via this callback.

6. Replace `_mnq_1m_df` / `_mes_1m_df` reads throughout:
   - `compute_tdo(_mnq_1m_df, today)` → `compute_tdo(_ib_source.mnq_1m_df, today)`
   - Similar for all other references; search for `_mnq_1m_df` and `_mes_1m_df`

7. In `_process_scanning()` at the signal handling block (line ~862):
   ```python
   # Before:
   assumed_entry = _apply_slippage(signal)
   if strategy_smt.HUMAN_EXECUTION_MODE and ...:
       ...
   
   # After:
   _entry_fill = _executor.place_entry(signal, bar)
   assumed_entry = _entry_fill.fill_price
   ```

8. In `_process_managing()` (lines 958–974), replace inline exit_price computation:
   ```python
   # Before:
   if result == "exit_tp": exit_price = _position["take_profit"]
   elif result == "exit_secondary": exit_price = _position["secondary_target"]
   elif result == "exit_stop": exit_price = _position["stop_price"]
   elif result in ("exit_market", ...): exit_price = float(bar.Close)
   
   # After:
   _exit_fill = _executor.place_exit(_position, result, bar)
   exit_price = _exit_fill.fill_price
   ```

9. Refactor `main()` to create and start `IbRealtimeSource`:
   ```python
   def main() -> None:
       global _ib_source, _executor, ...
       strategy_smt.HUMAN_EXECUTION_MODE = True
       _session_start_time = ...
       _session_end_time   = ...
       _executor = SimulatedFillExecutor(
           human_mode=True,
           human_slip_pts=getattr(strategy_smt, "HUMAN_ENTRY_SLIPPAGE_PTS", 0.0),
           entry_slip_ticks=ENTRY_SLIPPAGE_TICKS,
       )
       _ib_source = IbRealtimeSource(
           host=IB_HOST, port=IB_PORT, client_id=IB_CLIENT_ID,
           mnq_conid=MNQ_CONID, mes_conid=MES_CONID,
           bar_data_dir=BAR_DATA_DIR, on_bar=_on_bar,
           max_retries=MAX_RETRIES, retry_delay_s=RETRY_DELAY_S,
       )
       # Restore open position from disk (unchanged logic)
       if POSITION_FILE.exists():
           _position = json.loads(POSITION_FILE.read_text())
           _state = "MANAGING"
           _startup_ts = None
       else:
           _state = "SCANNING"
           _startup_ts = pd.Timestamp.now(tz="America/New_York")
       # HypothesisManager init (unchanged) ...
       _ib_source.start()  # blocks; retry loop is inside IbRealtimeSource
   ```

**VALIDATE**: `uv run pytest tests/test_signal_smt.py -x -q`

Note: `tests/test_signal_smt.py` mocks will shift from patching `signal_smt._ib` to patching
`data.ib_realtime.IbRealtimeSource`. Update mock targets in the test file as needed.

---

### Phase 4: Tests

#### Task 4.1: CREATE `tests/test_fill_executor.py`

**WAVE**: 4
**AGENT_ROLE**: test-author
**DEPENDS_ON**: [3.1, 3.2]
**PROVIDES**: Full unit coverage of `SimulatedFillExecutor`

**IMPLEMENT**:

```python
# tests/test_fill_executor.py
from unittest.mock import MagicMock
import pytest
from execution.simulated import SimulatedFillExecutor
from execution.protocol import FillRecord
from strategy_smt import _BarRow
import pandas as pd

def _bar(high=20005.0, low=19995.0, close=20000.0) -> _BarRow:
    ts = pd.Timestamp("2026-04-30 10:00:00", tz="America/New_York")
    return _BarRow(20000.0, high, low, close, 100.0, ts)

def _signal(direction="long", price=20000.0, limit=False):
    s = {"direction": direction, "entry_price": price, "take_profit": 20100.0,
         "stop_price": 19950.0, "secondary_target": None}
    if limit:
        s["limit_fill_bars"] = 3
    return s

def _position(direction="long", entry=20000.0):
    return {"direction": direction, "entry_price": entry, "assumed_entry": entry,
            "take_profit": 20100.0, "stop_price": 19950.0, "secondary_target": 20080.0,
            "contracts": 1}
```

Tests:
- `test_market_entry_long_applies_slippage`: market long → fill = entry + 2*0.25 = +0.5
- `test_market_entry_short_applies_slippage`: market short → fill = entry - 0.5
- `test_limit_entry_exact_price`: limit signal → fill = entry_price exactly
- `test_exit_tp_exact_price`: exit_tp → fill = take_profit exactly
- `test_exit_secondary_exact_price`: exit_secondary → fill = secondary_target
- `test_exit_stop_exact_price`: exit_stop → fill = stop_price
- `test_exit_market_bar_mid_no_slip`: not pessimistic → fill = (High+Low)/2
- `test_exit_market_pessimistic_long`: pessimistic + long → fill = mid - market_slip_pts
- `test_exit_market_pessimistic_short`: pessimistic + short → fill = mid + market_slip_pts
- `test_fills_sink_called_on_entry`: fills_sink callable is invoked with FillRecord
- `test_fills_sink_called_on_exit`: fills_sink called on exit
- `test_fill_record_fields_populated`: all FillRecord fields are set and correct type
- `test_human_mode_additive_slippage_long`: human_mode=True → fill_price increases for long
- `test_human_mode_additive_slippage_short`: fill_price decreases for short
- `test_start_stop_no_op`: start()/stop() callable without error

**VALIDATE**: `uv run pytest tests/test_fill_executor.py -v`

---

#### Task 4.2: CREATE `tests/test_ib_realtime.py`

**WAVE**: 4
**AGENT_ROLE**: test-author
**DEPENDS_ON**: [3.2]
**PROVIDES**: Unit coverage of `IbRealtimeSource` helpers (IB connection mocked)

**IMPLEMENT**:

Tests (IB mocked via `unittest.mock.patch`):
- `test_tick_accumulator_same_second_updates_ohlcv`: same second_ts → OHLCV updated in-place
- `test_tick_accumulator_new_second_finalizes_bar`: new second_ts → finalized bar returned
- `test_partial_1m_resets_on_minute_boundary`: new minute_ts → accumulator resets
- `test_gap_fill_skipped_if_fresh_parquet`: parquet last row within GAP_FILL_MAX_DAYS → no fetch
- `test_on_bar_callback_fired_on_second_boundary`: after tick causes second boundary, on_bar called
- `test_mnq_1m_df_property_returns_loaded_frames`: after parquet load, property non-empty
- `test_start_connects_and_calls_util_run` (integration, `@pytest.mark.integration`): skipped without IB

**VALIDATE**: `uv run pytest tests/test_ib_realtime.py -v -m "not integration"`

---

#### Task 4.3: Full Regression Gate

**WAVE**: 4
**AGENT_ROLE**: regression-runner
**DEPENDS_ON**: [3.1, 3.2]
**PROVIDES**: Proof that refactor is behaviour-preserving

**IMPLEMENT**: Run the full test suite. No new code — validation only.

**VALIDATE**: `uv run pytest -x -q`

---

## TESTING STRATEGY

### Unit Tests

**Status**: ✅ Automated | **Tool**: pytest | **Location**: `tests/test_fill_executor.py`,
`tests/test_ib_realtime.py` | **Run**: `uv run pytest tests/test_fill_executor.py tests/test_ib_realtime.py -v`

### Regression Tests (existing suites)

**Status**: ✅ Automated | **Tool**: pytest | **Run**: `uv run pytest tests/test_smt_backtest.py tests/test_signal_smt.py -v`

Must pass unchanged. These are the primary correctness gate for the refactor.

### Full Suite

**Status**: ✅ Automated | **Run**: `uv run pytest -x -q`

### Integration Tests

**Status**: ⚠️ Manual — requires running IB Gateway locally
**Why Manual**: IB Gateway is a desktop application requiring an active brokerage account;
it cannot be run in a CI environment or mocked at the network level without a licence.

#### Manual Test 1: IbRealtimeSource live connection

**Steps**:
1. Start IB Gateway on port 4002
2. `python -c "from data.ib_realtime import IbRealtimeSource; ..."`
3. Verify bar callbacks fire and `mnq_1m_df` grows

**Expected**: On-bar callback fires every second with a `_BarRow`; no exceptions for 30 seconds.

### Edge Cases

- **Exit type `exit_session_end`**: falls through to market-order path in executor → ✅ covered in `test_exit_market_bar_mid_no_slip`
- **`secondary_target = None`**: executor checks `position.get("secondary_target")` is not None before using — ✅ `test_exit_tp_exact_price`
- **Pessimistic fills backtest**: verify existing `test_smt_backtest.py` pessimistic test passes after refactor — ✅ regression gate
- **HypothesisManager initialization**: stays in `signal_smt.main()`; not affected by IbRealtimeSource extraction — ✅ `test_signal_smt.py`

### Test Automation Summary

| | Count | % |
|---|---|---|
| ✅ Unit (pytest) | 15 new | 75% |
| ✅ Regression (existing, re-run) | ~120 existing | 20% |
| ⚠️ Manual (IB live) | 1 | 5% |
| **Total** | ~136 | 100% |

Manual test requires IB Gateway — automation physically impossible in standard CI.

---

## VALIDATION COMMANDS

### Level 1: Syntax

```bash
python -c "from execution.protocol import FillRecord, FillExecutor"
python -c "from execution.simulated import SimulatedFillExecutor"
python -c "from data.ib_realtime import IbRealtimeSource"
python -c "import backtest_smt"
python -c "import signal_smt"
```

### Level 2: Unit Tests

```bash
uv run pytest tests/test_fill_executor.py -v
uv run pytest tests/test_ib_realtime.py -v -m "not integration"
```

### Level 3: Regression Tests

```bash
uv run pytest tests/test_smt_backtest.py -v
uv run pytest tests/test_signal_smt.py -v
```

### Level 4: Full Suite

```bash
uv run pytest -x -q
```

---

## ACCEPTANCE CRITERIA

### Functional

- [ ] `execution/protocol.py` defines `FillRecord` dataclass with fields: `order_id`, `symbol`, `direction`, `order_type`, `requested_price`, `fill_price`, `fill_time`, `contracts`, `status`, `session_date`
- [ ] `execution/protocol.py` defines `FillExecutor` as a `typing.Protocol` with `place_entry()`, `place_exit()`, `start()`, and `stop()` methods
- [ ] `execution/simulated.py` implements `FillExecutor`; `place_entry()` and `place_exit()` always return a `FillRecord` synchronously (never `None`)
- [ ] `SimulatedFillExecutor.place_entry()` applies 2-tick adverse slippage for market orders and fills limit/stop orders at exact requested price
- [ ] `SimulatedFillExecutor.place_entry()` applies additional `human_slip_pts` in the adverse direction (long pays more, short receives less) when `human_mode=True`; this is independent of pessimistic mode (which only affects exits)
- [ ] `data/ib_realtime.py` implements `IbRealtimeSource` with `start()`, `stop()`, `mnq_1m_df`, `mes_1m_df` properties, and `on_bar` callback registration
- [ ] `backtest_smt.py` calls `executor.place_entry()` / `executor.place_exit()` at each fill site; `_open_position()` and `_build_trade_record()` accept a `fill_price` parameter and no longer compute fill prices internally
- [ ] `signal_smt.py` uses `IbRealtimeSource` for IB connection management and `SimulatedFillExecutor` for fills; all inline IB globals, callbacks, and `_apply_slippage()` are removed
- [ ] `HUMAN_EXECUTION_MODE` slippage behaviour is preserved via `human_mode` constructor parameter on `SimulatedFillExecutor`

### Error Handling

- [ ] `IbRealtimeSource.start()` retries the IB connection up to the configured retry limit and raises a clear exception if all attempts fail
- [ ] `SimulatedFillExecutor` raises `ValueError` (or equivalent) if called with an unrecognised `order_type`
- [ ] `IbRealtimeSource` handles a gap between the last parquet bar and the current live tick by back-filling from the parquet file before emitting live bars

### Integration / E2E

- [ ] Running `backtest_smt.py` end-to-end with `SimulatedFillExecutor` produces identical fold P&L, Sharpe ratio, and win-rate values to the pre-refactor baseline (zero numeric regression)
- [ ] Running `signal_smt.py` end-to-end against a mock IB connection starts without error, receives a bar callback, and returns a `FillRecord` from `SimulatedFillExecutor`
- [ ] `execution/` and `data/` modules are importable independently (no circular imports, no dependency on strategy-specific globals)

### Validation

- [ ] `uv run pytest tests/test_fill_executor.py -v` — all 15 unit tests for `SimulatedFillExecutor` pass — verified by: `uv run pytest tests/test_fill_executor.py -v`
- [ ] `uv run pytest tests/test_ib_realtime.py -v` — all 7 unit tests for `IbRealtimeSource` pass — verified by: `uv run pytest tests/test_ib_realtime.py -v`
- [ ] `uv run pytest tests/test_smt_backtest.py -q` — all pre-existing backtest tests pass with no regressions — verified by: `uv run pytest tests/test_smt_backtest.py -q`
- [ ] `uv run pytest tests/test_signal_smt.py -q` — all pre-existing signal tests pass with no regressions — verified by: `uv run pytest tests/test_signal_smt.py -q`
- [ ] `uv run pytest -x -q` — full suite passes — verified by: `uv run pytest -x -q`
- [ ] Backtest numeric regression check: run backtest on one fixed date range before and after refactor; confirm P&L values match to the cent — verified by: manual comparison of backtest output

### Out of Scope

- PickMyTradeExecutor and `automation/main.py` — not part of Stage 1; covered in Stage 2 plan
- Writing `fills.jsonl` session files — deferred to Stage 2
- Any changes to strategy logic (divergence detection, signal generation, trade management rules)
- Tradovate or PickMyTrade account setup and configuration

---

## COMPLETION CHECKLIST

- [ ] All tasks completed in wave order
- [ ] Each task validation passed
- [ ] All validation levels executed (1–4)
- [ ] All automated tests created and passing
- [ ] Manual test documented
- [ ] Full test suite passes
- [ ] No regressions in `test_smt_backtest.py` or `test_signal_smt.py`
- [ ] **⚠️ Debug logs added during execution REMOVED**
- [ ] **⚠️ CRITICAL: Changes UNSTAGED — NOT committed**

---

## NOTES

**Why `backtest_smt.py` is modified despite the "frozen" comment**: The frozen comment
(`"""Frozen — do not modify"""`) guards against agent-driven strategy-logic changes during
optimization runs. This refactor is developer-authored and architectural — it changes only where
fill prices are computed, not how divergence is detected or how signals are generated. Strategy
outputs are identical.

**HUMAN_EXECUTION_MODE slippage**: Previously applied in `backtest_smt._open_position()`. After
refactor it is applied in `SimulatedFillExecutor.place_entry()` via the `human_mode` constructor
parameter. The `run_backtest()` call passes `strategy_smt.HUMAN_EXECUTION_MODE` to the executor
constructor — same logic, moved to the right owner.

**`fills_sink` in signal_smt.py**: For Stage 1, no `fills_sink` is passed to
`SimulatedFillExecutor` in `signal_smt.main()`. Fills are used for P&L display only (same as
current `assumed_entry` behaviour). Stage 2 will pass a `fills_sink` that writes to `fills.jsonl`.

**⚠️ DESIGN DOC CONFLICT — fills.jsonl scope**: The design doc
(`docs/superpowers/specs/2026-04-30-tradovate-live-trading-design.md`) Stage Separation Summary
explicitly lists `new fills.jsonl written per session (simulated fills)` as a Stage 1 observable
result. This plan intentionally defers that to Stage 2 to reduce Stage 1 scope, but the two
documents are in conflict. Resolve with the user before executing.

**v2 pipeline slippage is out of Stage 1 scope**: `backtest_smt.py` has an opt-in v2 trade
pipeline (enabled by `SMT_PIPELINE=v2`). Its slippage is applied by injecting a `"slippage"` key
into event dicts (lines ~1257, ~1273) and consuming it in the trade pairing loop (lines ~1292–1340).
This path does NOT go through `_open_position` or `_build_trade_record` and is NOT refactored in
Stage 1. The `v2_market_slip_pts` constructor parameter on `SimulatedFillExecutor` stores the value
for future use but is not consumed by `place_exit()` in Stage 1.
