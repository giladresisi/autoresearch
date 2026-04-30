# Design: Tradovate Live Trading Integration via PickMyTrade

**Date**: 2026-04-30
**Status**: Approved for implementation
**Plan Files**:
- Stage 1: `.agents/plans/stage1-fill-executor-ib-realtime.md`
- Stage 2: `.agents/plans/stage2-pickmytrade-automation.md`

---

## Problem Statement

The system generates live SMT divergence signals via `signal_smt.py` but never places orders. Fill
data (entry/exit prices, P&L) is simulated independently in two places — `backtest_smt.py` and
`signal_smt.py` — with no shared abstraction and slightly different logic. The IB realtime data
layer (connection, tick assembly, gap-fill) is also wired directly into `signal_smt.py` and cannot
be reused. There is no path to automated order execution.

---

## Goals

1. Extract fill simulation into a reusable `FillExecutor` protocol so backtesting and live
   signalling share identical fill logic.
2. Extract IB realtime data handling into a standalone `IbRealtimeSource` module reusable by both
   `signal_smt.py` and a future automation process.
3. Add a live automation module that uses IB data + PickMyTrade for order execution (required
   because the Apex funded account does not expose the Tradovate API directly; only authorised
   partners such as PickMyTrade can connect to it programmatically).

## Non-Goals

- Replacing IB-Gateway as the market data source (IB stays for all data feeds).
- Managing stop/TP orders natively in Tradovate (exits are Python-managed; no bracket orders sent
  to the exchange).
- Modifying signal detection logic or strategy parameters (strategy_smt.py is untouched).
- Direct Tradovate API integration (PickMyTrade is the required intermediary layer).

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                         DATA SOURCES                                │
│  IB-Gateway (live) ──► IbRealtimeSource    Parquet files (cached)   │
└─────────────────────────┬───────────────────────────┬───────────────┘
                          │                           │
          ┌───────────────▼──────────┐   ┌────────────▼────────────┐
          │    signal_smt.py         │   │    backtest_smt.py       │
          │  (human trader signals)  │   │   (walk-forward harness) │
          └───────────────┬──────────┘   └────────────┬────────────┘
                          │                           │
                          ▼                           ▼
              ┌───────────────────────────────────────────┐
              │          SimulatedFillExecutor             │
              │   (FillExecutor protocol — synchronous)    │
              └───────────────────────────────────────────┘

                        [Stage 2 adds below]

          ┌───────────────────────────────────────────────┐
          │              automation/main.py               │
          │   IbRealtimeSource + strategy_smt.py logic    │
          └─────────────────────────┬─────────────────────┘
                                    │
                                    ▼
              ┌─────────────────────────────────────────┐
              │        PickMyTradeExecutor               │
              │  (FillExecutor protocol — async fills)   │
              └──────────────────┬──────────────────────┘
                                 │
                          PickMyTrade API
                                 │
                          Tradovate (Apex account)
```

---

## Core Abstraction: FillExecutor Protocol

The `FillExecutor` protocol is the central interface separating signal/strategy logic from
order execution and fill recording.

```python
# execution/protocol.py

@dataclass
class FillRecord:
    order_id:        str
    symbol:          str
    direction:       str           # "long" | "short"
    order_type:      str           # "market" | "limit" | "stop"
    requested_price: float
    fill_price:      float | None  # None = pending (async executors)
    fill_time:       str | None    # ISO-8601
    contracts:       int
    status:          str           # "pending" | "filled" | "rejected"
    session_date:    str           # "YYYY-MM-DD"

class FillExecutor(Protocol):
    def place_entry(self, signal: dict, bar: BarRow) -> FillRecord | None: ...
    def place_exit(self, position: dict, exit_type: str, bar: BarRow) -> FillRecord | None: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...
```

**Return semantics**:
- `SimulatedFillExecutor` always returns `FillRecord` synchronously with `fill_price` populated.
- `PickMyTradeExecutor` returns `None` — order placed, fill recorded asynchronously.

**Fill propagation**:
- Backtest: `place_entry()` / `place_exit()` return values are used in-place to set fill prices
  before building trade records and computing P&L.
- Signalling: `place_entry()` return replaces `assumed_entry` in position dict; `place_exit()`
  return is used in the EXIT log line P&L.
- Automation: return values ignored; fill arrives via webhook/poll, written to `fills.jsonl`.

---

## Stage 1: Fill Executor + IB Realtime Module

### New Files

#### `execution/__init__.py`
Empty package marker.

#### `execution/protocol.py`
`FillRecord` dataclass and `FillExecutor` Protocol as defined above.
Also defines `BarRow` type alias (re-export from `strategy_smt._BarRow`).

#### `execution/simulated.py` — `SimulatedFillExecutor`

Extracts fill computation from:
- `backtest_smt._open_position()` — entry slippage logic
- `backtest_smt._build_trade_record()` lines 197–215 — exit fill logic

Constructor parameters:
```python
SimulatedFillExecutor(
    pessimistic: bool = False,
    market_slip_pts: float = 5.0,
    v2_market_slip_pts: float = 2.0,
    human_mode: bool = False,
    human_slip_pts: float = 0.0,
    entry_slip_ticks: int = 2,       # for signal_smt.py compatibility
    fills_sink: Callable[[FillRecord], None] | None = None,
)
```

`place_entry(signal, bar)` fill rules:
- If `signal.get("limit_fill_bars") is not None` → limit order → `fill_price = signal["entry_price"]` (exact)
- Otherwise → market order → `fill_price = signal["entry_price"] ± entry_slip_ticks * 0.25`
  (long pays more, short receives less)
- If `human_mode` → add `human_slip_pts` in the adverse direction on top

`place_exit(position, exit_type, bar)` fill rules:
- `exit_tp` → `position["take_profit"]` (exact, limit order)
- `exit_secondary` → `position["secondary_target"]` (exact, limit order)
- `exit_stop` → `position["stop_price"]` (exact, stop-limit)
- All other exit types (market): `mid = (bar.High + bar.Low) / 2`
  - If `pessimistic`: `fill_price = mid - direction_sign * market_slip_pts`
  - Else: `fill_price = mid`

#### `data/ib_realtime.py` — `IbRealtimeSource`

Extracts from `signal_smt.py`:
- `_load_parquets()` → internal `_load_parquets()`
- `_gap_fill_1m()` → internal `_gap_fill()`
- `on_mnq_1m_bar()`, `on_mes_1m_bar()` → internal callbacks, updates `mnq_1m_df`, calls `set_bar_data()`
- `on_mnq_tick()`, `on_mes_tick()` → internal callbacks
- `_update_tick_accumulator()`, `_update_partial_1m()`, `_partial_1m_to_bar_row()`, `_bar_timestamp()` → internal helpers
- `_setup_ib_subscriptions()` → internal
- Retry loop from `main()` → internal

Public interface:
```python
class IbRealtimeSource:
    def __init__(self,
                 host: str, port: int, client_id: int,
                 mnq_conid: str, mes_conid: str,
                 bar_data_dir: Path,
                 on_bar: Callable[[BarRow, dict | None], None],
                 max_retries: int = 10,
                 retry_delay_s: int = 15):
        ...

    @property
    def mnq_1m_df(self) -> pd.DataFrame: ...  # grows as bars arrive

    @property
    def mes_1m_df(self) -> pd.DataFrame: ...

    def start(self) -> None: ...   # connect, gap-fill, subscribe, util.run()
    def stop(self) -> None: ...    # disconnect
```

`on_bar(bar: BarRow, mes_partial: dict | None)` is called on every second boundary when both
MNQ and MES partials are aligned (same minute). This is the exact trigger currently in
`on_mnq_tick()` at line 474 of `signal_smt.py`.

`set_bar_data(mnq_1m_df, mes_1m_df)` is called internally whenever a new 1m bar completes.
Both consumers (signal_smt.py and automation/main.py) rely on this side-effect for strategy
context computation via `strategy_smt` globals.

### Modified Files

#### `backtest_smt.py`

The "frozen for agent optimization" constraint applies to strategy-logic changes only. This
architectural refactor is developer-authored and intentional.

Changes:
1. Import `SimulatedFillExecutor` at module top.
2. `run_backtest()`: instantiate `executor = SimulatedFillExecutor(pessimistic=PESSIMISTIC_FILLS, ...)`.
3. `_open_position()`: add `fill_price: float` parameter; replace slippage mutation block
   (lines 180–185) with `position["entry_price"] = fill_price`.
4. Call site of `_open_position()` (line 811): call `executor.place_entry(signal, bar)` first,
   pass `fill.fill_price` to `_open_position()`.
5. `_build_trade_record()`: add `fill_price: float` parameter; replace lines 197–215 (fill
   price computation) with `exit_price = fill_price`.
6. All call sites of `_build_trade_record()`: call `executor.place_exit(position, result, bar)`
   first, pass `fill.fill_price` to `_build_trade_record()`.

**Behaviour preserved**: All backtest metrics are numerically identical. The extracted logic is a
verbatim move, not a rewrite. Existing `tests/test_smt_backtest.py` must pass unchanged.

#### `signal_smt.py`

1. Remove: `_load_parquets`, `_gap_fill_1m`, `on_mnq_1m_bar`, `on_mes_1m_bar`, `on_mnq_tick`,
   `on_mes_tick`, `_setup_ib_subscriptions`, `_update_tick_accumulator`, `_update_partial_1m`,
   `_partial_1m_to_bar_row`, `_bar_timestamp`, and all associated module-level globals
   (`_ib`, `_mnq_contract`, `_mes_contract`, `_mnq_1m_df`, `_mes_1m_df`,
   `_mnq_partial_1m`, `_mes_partial_1m`, `_mnq_tick_bar`).
2. Remove `_apply_slippage()`.
3. Add imports: `IbRealtimeSource`, `SimulatedFillExecutor`.
4. Module-level: `_ib_source: IbRealtimeSource | None = None` and `_executor: FillExecutor | None = None`.
5. New `_on_bar(bar, mes_partial)` callback — replaces what `on_mnq_tick()` did at line 474:
   sets `_mes_partial_1m` from `mes_partial` and calls `_process(bar)`.
6. In `_process_scanning()`: replace `assumed_entry = _apply_slippage(signal)` with
   `fill = _executor.place_entry(signal, bar); assumed_entry = fill.fill_price`.
7. In `_process_managing()`: replace inline exit_price computation (lines 958–974) with
   `fill = _executor.place_exit(_position, result, bar); exit_price = fill.fill_price`.
8. Access `_mnq_1m_df` / `_mes_1m_df` via `_ib_source.mnq_1m_df` / `_ib_source.mes_1m_df`.
9. `main()`: create `_ib_source = IbRealtimeSource(..., on_bar=_on_bar)` and
   `_executor = SimulatedFillExecutor(...)`, call `_ib_source.start()` (this blocks, replacing
   the retry loop and `util.run()` that were in `main()`).

**Behaviour preserved**: All SIGNAL/EXIT log lines and JSON payloads identical. All existing
`tests/test_signal_smt.py` pass (mock IB layer via `IbRealtimeSource` instead of direct ib_insync
mocks).

---

## Stage 2: PickMyTrade Executor + Automation Module

### PickMyTrade API Notes

- Signal delivery: HTTP POST to a PickMyTrade-provided webhook URL with order JSON payload.
- Fill data: PickMyTrade does not guarantee a synchronous fill response; fills are queried
  asynchronously via polling (PMT fill history API) or optional webhook callback.
- Authentication: API key in `Authorization` header.
- Rate limits: not published; conservative default is ≤ 1 order request per second.

### New Files

#### `execution/pickmytrade.py` — `PickMyTradeExecutor`

```python
class PickMyTradeExecutor:
    def __init__(self,
                 webhook_url: str,
                 api_key: str,
                 symbol: str,
                 account_id: str,
                 contracts: int,
                 fill_mode: str = "poll",         # "poll" | "webhook"
                 fill_poll_interval_s: int = 30,
                 fill_webhook_port: int = 8765,
                 fills_path: Path | None = None):
        ...

    def place_entry(self, signal: dict, bar: BarRow) -> None:
        # POST {"action": "BUY"/"SELL", "qty": contracts, "symbol": symbol,
        #       "orderType": "Market"|"Limit", "price": ..., "isAutomated": true}
        # Returns None (fill arrives async)

    def place_exit(self, position: dict, exit_type: str, bar: BarRow) -> None:
        # POST close order (reverse action, market order)
        # Returns None

    def start(self) -> None:
        # Launch fill receiver (poll loop or webhook server) in background thread

    def stop(self) -> None:
        # Stop fill receiver, flush pending fills
```

`_fill_poll_loop()`: every `fill_poll_interval_s` seconds, GET fill history from PickMyTrade,
match open order IDs, write confirmed fills to `fills_path` (JSONL).

`_fill_webhook_server()`: small `http.server` on `fill_webhook_port`; PickMyTrade POSTs fill
events here; write to `fills_path`.

#### `automation/__init__.py`
Empty package marker.

#### `automation/main.py`

Mirrors `signal_smt.py` structure but uses `PickMyTradeExecutor` instead of
`SimulatedFillExecutor`. Key differences:

- Uses a separate IB client ID (`AUTOMATION_IB_CLIENT_ID=20`) so it can run alongside
  `signal_smt.py` without a clientId conflict.
- Does NOT print SIGNAL/EXIT lines for human consumption; instead emits structured JSON events
  that the orchestrator parses (same `events.jsonl` format).
- No `HUMAN_EXECUTION_MODE` slippage — actual fills come from PickMyTrade.
- Launched by `orchestrator/main.py` when `LIVE_TRADING=true` (in place of `signal_smt.py`).

---

## Session File Layout

```
sessions/YYYY-MM-DD/
├── signals.log       # raw stdout from signal_smt.py or automation/main.py
├── events.jsonl      # structured SIGNAL/EXIT events (existing)
├── trades.tsv        # entry/exit pairs with estimated prices (existing)
├── fills.jsonl       # NEW: actual or simulated fill records per order
└── summary.md        # Claude-generated post-session analysis (existing)
```

`fills.jsonl` record schema:
```json
{
  "order_id": "sim-20260430-001",
  "symbol": "MNQ1!",
  "direction": "long",
  "order_type": "market",
  "requested_price": 21350.25,
  "fill_price": 21350.75,
  "fill_time": "2026-04-30T09:35:02-04:00",
  "contracts": 1,
  "status": "filled",
  "session_date": "2026-04-30"
}
```

`fills.jsonl` is written by the `FillExecutor` implementation. For backtesting, no file is
written — records are returned in-memory. For signalling and automation, records are written to
the session directory. The post-session summarizer (Claude API call) reads `fills.jsonl` to
compute accurate P&L rather than relying on assumed prices from `signals.log`.

---

## Configuration

### Stage 1 (no new env vars)

| Variable | Where used | Notes |
|----------|-----------|-------|
| `PESSIMISTIC_FILLS` | `strategy_smt.py` | Respected by `SimulatedFillExecutor` |
| `MARKET_ORDER_SLIPPAGE_PTS` | `backtest_smt.py` | Passed to `SimulatedFillExecutor` |
| `V2_MARKET_CLOSE_SLIPPAGE_PTS` | `backtest_smt.py` | Stored in `SimulatedFillExecutor(v2_market_slip_pts=...)` constructor; v2 pipeline uses event-dict injection, not `place_exit()` — moving that path is deferred |
| `HUMAN_EXECUTION_MODE` | `strategy_smt.py` | Respected; controls slippage mode |

### Stage 2 (new env vars)

| Variable | Default | Description |
|----------|---------|-------------|
| `LIVE_TRADING` | `false` | Orchestrator launches `automation/main.py` instead of `signal_smt.py` |
| `PMT_WEBHOOK_URL` | required | PickMyTrade signal endpoint URL |
| `PMT_API_KEY` | required | PickMyTrade API key |
| `PMT_FILL_MODE` | `poll` | `poll` = query fill history; `webhook` = receive callbacks |
| `PMT_FILL_POLL_INTERVAL_S` | `30` | Seconds between fill status polls |
| `PMT_FILL_WEBHOOK_PORT` | `8765` | Port for fill callback HTTP server |
| `AUTOMATION_IB_CLIENT_ID` | `20` | Separate from `signal_smt.py`'s client ID 15 |
| `TRADING_SYMBOL` | `MNQ1!` | Symbol sent to PickMyTrade |
| `TRADING_CONTRACTS` | `1` | Contracts per trade |

All Stage 2 vars must be set in `.env` before running `automation/main.py`.

---

## Stage Separation Summary

### Stage 1 — Foundation (this PR)
- Creates `execution/` and `data/ib_realtime.py` modules
- Refactors `backtest_smt.py` and `signal_smt.py` to use shared abstractions
- **Observable result**: all existing tests pass; backtest metrics unchanged; signalling output
  unchanged; new `fills.jsonl` written per session (simulated fills)
- **No real money involved**

### Stage 2 — Live Automation (next PR)
- Adds `execution/pickmytrade.py` and `automation/main.py`
- Extends orchestrator with `LIVE_TRADING` flag
- **Observable result**: `automation/main.py` places real orders on the Apex funded account
  via PickMyTrade; real fill data written to `fills.jsonl`
- **Real money involved — validate Stage 1 thoroughly before proceeding**
