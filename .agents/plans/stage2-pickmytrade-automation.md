# Feature: Stage 2 — PickMyTrade Executor + Live Automation Module

**⚠️ EXECUTION RULES — READ FIRST:**
- Implement ALL changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
- Only make code changes — no git operations

Validate documentation and codebase patterns before implementing. Match naming of existing utils,
types, and models. Import from correct files.

**⚠️ PREREQUISITE**: Stage 1 (`stage1-fill-executor-ib-realtime.md`) must be fully complete and
all its tests passing before executing this plan.

---

## Feature Description

Add live automated trading by implementing `PickMyTradeExecutor` (the live `FillExecutor`) and
`automation/main.py` (a session process that replicates `signal_smt.py`'s signal logic but
places real orders via PickMyTrade instead of simulating fills). The orchestrator gains a
`LIVE_TRADING` flag that launches `automation/main.py` instead of `signal_smt.py`.

**Scope**: No changes to any Stage 1 files. All new code is additive.

See full design: `docs/superpowers/specs/2026-04-30-tradovate-live-trading-design.md`

## User Story

As a trader with an Apex funded account,
I want the strategy to place real orders automatically via PickMyTrade,
So that live trades execute without manual intervention while using the same signal logic
validated in backtesting.

## Problem Statement

- `signal_smt.py` generates validated signals but never places orders.
- The Apex funded account cannot be accessed via the Tradovate API directly; PickMyTrade is the
  required authorised intermediary.
- Fills from real orders are not recorded; post-session P&L is based on assumed prices only.

## Solution Statement

1. Implement `PickMyTradeExecutor` — sends orders via PickMyTrade HTTP API; receives fills async
   via poll or webhook; writes `fills.jsonl` per session.
2. Implement `automation/main.py` — mirrors `signal_smt.py` logic but uses `IbRealtimeSource`
   + `PickMyTradeExecutor`; emits structured JSON events to stdout for the orchestrator.
3. Extend `orchestrator/main.py` with `LIVE_TRADING=true` flag to launch `automation/main.py`.
4. All existing Stage 1 files and tests remain unchanged.

## Feature Metadata

**Feature Type**: New Capability
**Complexity**: High
**Primary Systems Affected**: new `execution/pickmytrade.py`, new `automation/`, `orchestrator/main.py`
**Dependencies**: `httpx` (new — async HTTP client), `python-dotenv` (existing)
**Breaking Changes**: No — all existing modules untouched; `LIVE_TRADING` defaults to `false`
**Real Money**: Yes — validate in demo environment before enabling on live account

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ BEFORE IMPLEMENTING

- `execution/protocol.py` — `FillRecord`, `FillExecutor` Protocol; `PickMyTradeExecutor` implements this
- `execution/simulated.py` — reference implementation of `FillExecutor`; mirror structure
- `data/ib_realtime.py` — `IbRealtimeSource`; `automation/main.py` creates one instance
- `signal_smt.py` — full reference for `automation/main.py`; copy `_process_scanning`,
  `_process_managing`, all format_* functions, `SmtV2Dispatcher`; replace IB globals with
  `IbRealtimeSource`, replace `SimulatedFillExecutor` with `PickMyTradeExecutor`
- `orchestrator/main.py` — add `LIVE_TRADING` env flag; launch `automation/main.py` subprocess
  instead of `signal_smt.py`
- `orchestrator/relay.py` — `SessionRelay` parses stdout; `automation/main.py` must emit
  identical SIGNAL/EXIT JSON lines
- `docs/superpowers/specs/2026-04-30-tradovate-live-trading-design.md` — full design with
  PickMyTrade API notes, env vars, fills.jsonl schema
- `.env.example` — add Stage 2 env vars

### New Files to Create

- `execution/pickmytrade.py` — `PickMyTradeExecutor`
- `automation/__init__.py` — empty package marker
- `automation/main.py` — live automation session process
- `tests/test_pickmytrade_executor.py` — unit tests (mocked HTTP)
- `tests/test_automation_main.py` — unit tests (mocked IbRealtimeSource + executor)

### Files to Modify

- `orchestrator/main.py` — add `LIVE_TRADING` launch path
- `.env.example` — document Stage 2 env vars

### External API Research

**API**: PickMyTrade signal API
**Documentation**: Provided by PickMyTrade upon account setup (no public docs URL)

**Integration Pattern** (based on PickMyTrade's standard webhook interface):
```python
import httpx
response = httpx.post(
    PMT_WEBHOOK_URL,
    headers={"Authorization": f"Bearer {PMT_API_KEY}",
             "Content-Type": "application/json"},
    json={
        "action": "BUY",          # or "SELL" or "CLOSE"
        "qty": 1,
        "symbol": "MNQ1!",
        "orderType": "Market",    # or "Limit"
        "price": 21350.25,        # limit price if Limit, else omitted
        "isAutomated": True,
    },
    timeout=10.0,
)
```

**Fill polling pattern**:
```python
response = httpx.get(
    f"{PMT_BASE_URL}/fills",
    headers={"Authorization": f"Bearer {PMT_API_KEY}"},
    params={"order_id": order_id},
    timeout=10.0,
)
```

**Critical Findings**:
- PickMyTrade's fill API specifics must be verified against actual account docs at setup time.
- The executor uses a configurable `fill_mode=poll` (default) as a safe fallback.
- `isAutomated: True` must be set on all programmatic orders.
- Rate limit: conservative default 1 request/second; configurable.
- Demo vs live: separate webhook URLs; `PMT_WEBHOOK_URL` determines which is used.

**Validation Strategy**: Connect to PickMyTrade demo account, send a test market order on NQ,
verify order appears in dashboard, verify fill data available via API. Expected: HTTP 200 with
order ID, fill available within 30s.

### Patterns to Follow

**HTTP client**: `httpx` (sync) for simplicity; avoid `asyncio` to stay consistent with existing
codebase which is synchronous (ib_insync uses its own event loop via `util.run()`)
**Retry pattern**: exponential backoff with jitter, max 3 retries; see IbRealtimeSource retry loop
**Env vars**: `os.environ.get("VAR", default)` with startup validation; see `signal_smt.py` dotenv pattern
**Background threads**: `threading.Thread(daemon=True)` for fill receiver; consistent with
ib_insync's `util.run()` blocking pattern

---

## PARALLEL EXECUTION STRATEGY

### Dependency Graph

```
┌─────────────────────────────────────────────────────────────────┐
│ WAVE 1: Executor + Automation (parallel)                        │
├───────────────────────────────┬─────────────────────────────────┤
│ Task 1.1: CREATE              │ Task 1.2: CREATE                │
│   pickmytrade.py              │   automation/main.py            │
│ Agent: executor-author        │ Agent: automation-author        │
└───────────────────────────────┴─────────────────────────────────┘
                    ↓ (both complete)
┌─────────────────────────────────────────────────────────────────┐
│ WAVE 2: Integration + Infrastructure (parallel)                 │
├───────────────────────────────┬─────────────────────────────────┤
│ Task 2.1: UPDATE              │ Task 2.2: UPDATE .env.example   │
│   orchestrator/main.py        │ + add httpx dependency          │
│ Agent: integration-specialist │ Agent: config-author            │
└───────────────────────────────┴─────────────────────────────────┘
                    ↓
┌─────────────────────────────────────────────────────────────────┐
│ WAVE 3: Tests (parallel)                                        │
├───────────────────────────────┬─────────────────────────────────┤
│ Task 3.1: tests for           │ Task 3.2: tests for             │
│   pickmytrade executor        │   automation/main.py            │
└───────────────────────────────┴─────────────────────────────────┘
```

### Parallelization Summary

**Wave 1 — Fully Parallel**: `pickmytrade.py` and `automation/main.py` have no dependency
between them at creation time (automation/main.py imports pickmytrade via protocol, not directly)
**Wave 2 — Parallel after Wave 1**: Orchestrator update and env/deps update are independent
**Wave 3 — Parallel after Wave 2**: Both test files are independent

### Interface Contracts

**Contract 1**: Task 1.1 provides `PickMyTradeExecutor` implementing `FillExecutor` →
Task 1.2 `automation/main.py` creates an instance of it
**Contract 2**: Task 1.2 provides `automation/main.py` subprocess entry point →
Task 2.1 orchestrator uses it as a subprocess command
**Mock for Wave 1 parallel work**: `automation/main.py` can use a stub `PickMyTradeExecutor`
(implementing `FillExecutor` Protocol with no-op methods) during parallel development; replace
with real import when Task 1.1 completes

### Synchronization Checkpoints

**After Wave 1**: `python -c "from execution.pickmytrade import PickMyTradeExecutor; from automation.main import main; print('OK')"` (syntax)
**After Wave 2**: `python -c "import orchestrator.main"` (syntax)
**After Wave 3**: `uv run pytest tests/test_pickmytrade_executor.py tests/test_automation_main.py -v`
**Final**: `uv run pytest -x -q`

---

## IMPLEMENTATION PLAN

### Phase 1: Core New Modules

#### Task 1.1: CREATE `execution/pickmytrade.py`

**Purpose**: `PickMyTradeExecutor` implementing `FillExecutor`. Sends orders to PickMyTrade,
receives fills asynchronously via poll or webhook, writes `fills.jsonl`.

**WAVE**: 1
**AGENT_ROLE**: executor-author
**DEPENDS_ON**: [Stage 1 complete]
**BLOCKS**: [3.1]
**PROVIDES**: `PickMyTradeExecutor`

**IMPLEMENT**:

```python
# execution/pickmytrade.py
import json, threading, time, uuid, datetime
from pathlib import Path
import httpx
from execution.protocol import FillRecord, BarRow

class PickMyTradeExecutor:
    def __init__(self, *,
                 webhook_url: str,
                 api_key: str,
                 symbol: str,
                 account_id: str,
                 contracts: int,
                 fill_mode: str = "poll",          # "poll" | "webhook"
                 fill_poll_interval_s: int = 30,
                 fill_webhook_port: int = 8765,
                 fills_path: Path | None = None,
                 request_timeout_s: float = 10.0,
                 max_retries: int = 3):
        ...
        self._pending: dict[str, dict] = {}   # order_id → {signal/position info}
        self._lock = threading.Lock()
        self._fill_thread: threading.Thread | None = None
```

`place_entry(signal, bar) -> None`:
```python
order_id = f"pmt-{uuid.uuid4().hex[:8]}"
action = "BUY" if signal["direction"] == "long" else "SELL"
is_limit = signal.get("limit_fill_bars") is not None
payload = {
    "action": action,
    "qty": self._contracts,
    "symbol": self._symbol,
    "orderType": "Limit" if is_limit else "Market",
    "isAutomated": True,
}
if is_limit:
    payload["price"] = float(signal["entry_price"])
self._post_order(order_id, payload, signal)
return None
```

`place_exit(position, exit_type, bar) -> None`:
```python
order_id = f"pmt-{uuid.uuid4().hex[:8]}"
close_action = "SELL" if position["direction"] == "long" else "BUY"
payload = {"action": close_action, "qty": self._contracts,
           "symbol": self._symbol, "orderType": "Market", "isAutomated": True}
self._post_order(order_id, payload, position)
return None
```

`_post_order(order_id, payload, context)`:
- POST to `self._webhook_url` with headers `{"Authorization": f"Bearer {self._api_key}"}`
- On HTTP 200/201: store `order_id` in `self._pending`
- On failure: retry up to `max_retries` with exponential backoff (1s, 2s, 4s)
- On all retries exhausted: print `[FILL-WARN] Order {order_id} placement failed: {exc}`; do not raise

`start()`:
- Validate env vars present (raise `RuntimeError` if `PMT_WEBHOOK_URL` or `PMT_API_KEY` empty)
- Launch fill receiver in daemon thread:
  - `fill_mode == "poll"`: `_fill_poll_loop()` in thread
  - `fill_mode == "webhook"`: `_fill_webhook_server()` in thread

`_fill_poll_loop()`:
```python
while True:
    time.sleep(self._fill_poll_interval_s)
    with self._lock:
        pending = list(self._pending.items())
    for order_id, ctx in pending:
        rec = self._query_fill(order_id, ctx)
        if rec is not None:
            self._record_fill(rec)
            with self._lock:
                self._pending.pop(order_id, None)
```

`_query_fill(order_id, ctx) -> FillRecord | None`:
- GET fill status from PickMyTrade API
- If status == "filled": return `FillRecord(... fill_price=response["fill_price"] ...)`
- If pending: return None

`_fill_webhook_server()`:
- `http.server.HTTPServer` on `self._fill_webhook_port`
- Receives POST from PickMyTrade with fill JSON
- Calls `_record_fill(FillRecord(...))`

`_record_fill(rec: FillRecord)`:
- Append JSON line to `self._fills_path` (atomic write via temp file + rename)
- Print `[FILL] order={rec.order_id} price={rec.fill_price} status={rec.status}`

`stop()`:
- Set a stop event; join fill thread with 5s timeout

**VALIDATE**: `python -c "from execution.pickmytrade import PickMyTradeExecutor; print('OK')"`

---

#### Task 1.2: CREATE `automation/__init__.py` and `automation/main.py`

**Purpose**: Live automation session process. Mirrors `signal_smt.py` but uses
`IbRealtimeSource` + `PickMyTradeExecutor`. Emits the same SIGNAL/EXIT JSON stdout lines the
orchestrator expects.

**WAVE**: 1
**AGENT_ROLE**: automation-author
**DEPENDS_ON**: [Stage 1 complete]
**BLOCKS**: [2.1, 3.2]
**PROVIDES**: `automation/main.py` subprocess entry point

**IMPLEMENT**:

`automation/__init__.py` — empty file.

`automation/main.py` — copy `signal_smt.py` as the starting point, then apply these changes:

1. Replace executor creation in `main()`:
   ```python
   from execution.pickmytrade import PickMyTradeExecutor
   # ...
   fills_path = sessions_dir / today_str / "fills.jsonl"
   fills_path.parent.mkdir(parents=True, exist_ok=True)
   _executor = PickMyTradeExecutor(
       webhook_url=os.environ["PMT_WEBHOOK_URL"],
       api_key=os.environ["PMT_API_KEY"],
       symbol=os.environ.get("TRADING_SYMBOL", "MNQ1!"),
       account_id=os.environ.get("TRADING_ACCOUNT_ID", ""),
       contracts=int(os.environ.get("TRADING_CONTRACTS", "1")),
       fill_mode=os.environ.get("PMT_FILL_MODE", "poll"),
       fill_poll_interval_s=int(os.environ.get("PMT_FILL_POLL_INTERVAL_S", "30")),
       fill_webhook_port=int(os.environ.get("PMT_FILL_WEBHOOK_PORT", "8765")),
       fills_path=fills_path,
   )
   ```

2. IB client ID: use `AUTOMATION_IB_CLIENT_ID` (default 20, not 15).
   ```python
   IB_CLIENT_ID = int(os.environ.get("AUTOMATION_IB_CLIENT_ID", "20"))
   ```

3. `main()` lifecycle:
   ```python
   _executor.start()
   try:
       _ib_source.start()   # blocks until session ends or signal
   finally:
       _executor.stop()
   ```

4. Remove `HUMAN_EXECUTION_MODE = True` line (not relevant for automated execution).
   `SimulatedFillExecutor` is not imported; `PickMyTradeExecutor` is used directly.

5. `_process_scanning()` — signal handling block: same as `signal_smt.py` after Stage 1 refactor.
   `place_entry()` returns `None` for PickMyTrade; `assumed_entry` falls back to
   `signal["entry_price"]` for display purposes:
   ```python
   _entry_fill = _executor.place_entry(signal, bar)
   assumed_entry = _entry_fill.fill_price if _entry_fill else float(signal["entry_price"])
   ```

6. `_process_managing()` — exit block: same pattern:
   ```python
   _exit_fill = _executor.place_exit(_position, result, bar)
   exit_price = _exit_fill.fill_price if _exit_fill else float(bar.Close)
   ```

7. Sessions directory: read from `SESSIONS_DIR` env var (default: `sessions`).

**Startup validation** in `main()` (before connecting IB):
```python
required = ["PMT_WEBHOOK_URL", "PMT_API_KEY"]
missing = [v for v in required if not os.environ.get(v)]
if missing:
    raise RuntimeError(f"Missing required env vars: {missing}")
```

**VALIDATE**: `python -c "from automation.main import main; print('OK')"`

---

### Phase 2: Integration & Configuration

#### Task 2.1: UPDATE `orchestrator/main.py` — add `LIVE_TRADING` launch path

**Purpose**: When `LIVE_TRADING=true`, orchestrator spawns `automation/main.py` instead of
`signal_smt.py`. Everything else (relay, session lifecycle, summarizer) is unchanged.

**WAVE**: 2
**AGENT_ROLE**: integration-specialist
**DEPENDS_ON**: [1.2]
**BLOCKS**: [3.2]
**PROVIDES**: Orchestrator that can launch either signal or automation mode

**IMPLEMENT**:

In `orchestrator/main.py`, find where the signal process is launched (search for `signal_smt`
subprocess spawn). Replace hardcoded command with:

```python
import os as _os

LIVE_TRADING = _os.environ.get("LIVE_TRADING", "false").lower() == "true"

if LIVE_TRADING:
    signal_cmd = ["uv", "run", "python", "-m", "automation.main"]
else:
    signal_cmd = ["uv", "run", "python", "signal_smt.py"]
```

Use `signal_cmd` in the subprocess spawn call.

Add a startup log line:
```python
print(f"[orchestrator] mode={'LIVE_TRADING' if LIVE_TRADING else 'signal'}", flush=True)
```

**VALIDATE**: `python -c "import orchestrator.main" && echo "OK"`

---

#### Task 2.2: UPDATE `.env.example` and `pyproject.toml`

**Purpose**: Document all new Stage 2 env vars; add `httpx` dependency.

**WAVE**: 2
**AGENT_ROLE**: config-author
**DEPENDS_ON**: [1.1]
**PROVIDES**: Updated config files

**IMPLEMENT**:

In `.env.example` (create if not exists), add section:
```
# ── Stage 2: Live Trading via PickMyTrade ─────────────────────────
# Set LIVE_TRADING=true to launch automation/main.py instead of signal_smt.py
LIVE_TRADING=false

# PickMyTrade credentials (required when LIVE_TRADING=true)
PMT_WEBHOOK_URL=<set-pmt-webhook-url>
PMT_API_KEY=<set-pmt-api-key>

# Fill data retrieval: "poll" (default) or "webhook"
PMT_FILL_MODE=poll
PMT_FILL_POLL_INTERVAL_S=30
PMT_FILL_WEBHOOK_PORT=8765

# Tradovate / Apex account
TRADING_ACCOUNT_ID=<set-account-id>
TRADING_SYMBOL=MNQ1!
TRADING_CONTRACTS=1

# IB client ID for automation process (must not conflict with signal_smt.py's 15)
AUTOMATION_IB_CLIENT_ID=20
```

In `pyproject.toml`, add `httpx>=0.28` to `dependencies`.

**VALIDATE**: `uv sync && python -c "import httpx; print(httpx.__version__)"`

---

### Phase 3: Tests

#### Task 3.1: CREATE `tests/test_pickmytrade_executor.py`

**WAVE**: 3
**AGENT_ROLE**: test-author
**DEPENDS_ON**: [1.1, 2.2]
**PROVIDES**: Unit coverage of `PickMyTradeExecutor`

**IMPLEMENT**:

All tests mock `httpx.post` / `httpx.get` via `unittest.mock.patch`.

```python
# tests/test_pickmytrade_executor.py
import json, threading
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest
from execution.pickmytrade import PickMyTradeExecutor
from strategy_smt import _BarRow
import pandas as pd

def _executor(tmp_path, fill_mode="poll"):
    return PickMyTradeExecutor(
        webhook_url="https://pmt.example.com/signal",
        api_key="test-key",
        symbol="MNQ1!",
        account_id="ACC123",
        contracts=1,
        fill_mode=fill_mode,
        fill_poll_interval_s=999,   # prevent auto-polling in tests
        fills_path=tmp_path / "fills.jsonl",
    )

def _bar():
    ts = pd.Timestamp("2026-04-30 10:00:00", tz="America/New_York")
    return _BarRow(20000.0, 20005.0, 19995.0, 20000.0, 100.0, ts)
```

Tests:
- `test_place_entry_long_posts_buy_market_order`: long market signal → POST with `action=BUY`, `orderType=Market`
- `test_place_entry_short_posts_sell_market_order`: short market → `action=SELL`
- `test_place_entry_limit_posts_limit_order`: limit signal → `orderType=Limit`, `price=entry_price`
- `test_place_entry_returns_none`: always returns None (async fill)
- `test_place_exit_long_posts_sell_close`: long position exit → `action=SELL`
- `test_place_exit_short_posts_buy_close`: short position exit → `action=BUY`
- `test_place_exit_returns_none`: always returns None
- `test_order_retries_on_500`: HTTP 500 → retries up to max_retries; no exception raised
- `test_order_does_not_raise_on_failure`: all retries exhausted → prints warning, no exception
- `test_fill_polling_writes_filled_record_to_jsonl`: mock poll returns filled → FillRecord appended to fills.jsonl
- `test_fill_jsonl_format`: filled record has all FillRecord fields as valid JSON
- `test_pending_order_cleared_after_fill`: after fill received, order removed from `_pending`
- `test_start_raises_if_env_missing`: `PMT_WEBHOOK_URL=""` → `RuntimeError` on `start()`
- `test_stop_joins_fill_thread`: after `start()`, `stop()` completes within 6s
- `test_is_automated_flag_set`: every POST payload includes `isAutomated: True`

**VALIDATE**: `uv run pytest tests/test_pickmytrade_executor.py -v`

---

#### Task 3.2: CREATE `tests/test_automation_main.py`

**WAVE**: 3
**AGENT_ROLE**: test-author
**DEPENDS_ON**: [1.2, 2.1]
**PROVIDES**: Unit coverage of `automation/main.py` signal and exit flows

**IMPLEMENT**:

Mock `IbRealtimeSource` and `PickMyTradeExecutor` entirely; test the state machine logic.

```python
# tests/test_automation_main.py
from unittest.mock import patch, MagicMock, call
import pandas as pd
import pytest
```

Tests:
- `test_main_validates_env_vars_before_start`: missing `PMT_WEBHOOK_URL` → `RuntimeError` before IB connects
- `test_executor_started_before_ib_source`: `executor.start()` called before `ib_source.start()`
- `test_executor_stopped_in_finally_on_ib_error`: IbRealtimeSource raises → `executor.stop()` still called
- `test_place_entry_called_on_signal_detection`: inject a signal via mocked scan → `executor.place_entry()` called once
- `test_place_exit_called_on_managing_exit`: inject exit condition → `executor.place_exit()` called once
- `test_assumed_entry_falls_back_to_signal_price_when_fill_is_none`: executor returns None → assumed_entry = signal["entry_price"]
- `test_exit_price_falls_back_to_bar_close_when_fill_is_none`: executor returns None → exit_price = bar.Close
- `test_automation_uses_correct_ib_client_id`: `AUTOMATION_IB_CLIENT_ID=25` → IbRealtimeSource created with client_id=25
- `test_signal_json_line_emitted_to_stdout`: SIGNAL event → JSON line printed (capsys)
- `test_exit_json_line_emitted_to_stdout`: EXIT event → EXIT JSON line printed (capsys)
- `test_fills_path_in_session_directory`: fills.jsonl path is under `sessions/YYYY-MM-DD/`

**VALIDATE**: `uv run pytest tests/test_automation_main.py -v`

---

## TESTING STRATEGY

### Unit Tests

**Status**: ✅ Automated | **Tool**: pytest | **Location**: `tests/test_pickmytrade_executor.py`,
`tests/test_automation_main.py` | **Run**: `uv run pytest tests/test_pickmytrade_executor.py tests/test_automation_main.py -v`

### Regression Tests

**Status**: ✅ Automated | **Run**: `uv run pytest -x -q`
All Stage 1 tests must still pass. No Stage 1 files are modified.

### Integration Tests (PickMyTrade demo)

**Status**: ⚠️ Manual — requires PickMyTrade account + Apex demo account credentials

**Why Manual**: PickMyTrade is a third-party paid service; no public sandbox available without
an account. Bot-detection and credential requirements make full automation impractical.

#### Manual Test 1: PickMyTrade Demo — Market Order Placement

**Prerequisites**: `PMT_WEBHOOK_URL`, `PMT_API_KEY`, Apex demo account connected in PickMyTrade dashboard.

**Steps**:
1. Set env vars in `.env`.
2. `python -c "from execution.pickmytrade import PickMyTradeExecutor; ...place_entry(test_signal, bar)"`
3. Open PickMyTrade dashboard → verify order appears.
4. Wait 60s → verify fill record appears in `fills.jsonl`.

**Expected**: Order visible in dashboard within 5s; fill record in `fills.jsonl` within 60s.

#### Manual Test 2: Full Automation Session (Demo Account Only)

**Prerequisites**: IB Gateway running; PickMyTrade demo configured; `LIVE_TRADING=true` in `.env`.

**Steps**:
1. `uv run python -m automation.main` (or via orchestrator)
2. Wait for session start (09:00 ET)
3. If signal fires, verify PickMyTrade dashboard shows order
4. At session end, inspect `sessions/YYYY-MM-DD/fills.jsonl`

**Expected**: At least one order attempted; fills.jsonl written; no unhandled exceptions.

#### Manual Test 3: Orchestrator Live Trading Mode

**Steps**:
1. Set `LIVE_TRADING=true` in `.env`
2. `uv run python -m orchestrator.main`
3. Verify log shows `[orchestrator] mode=LIVE_TRADING`
4. Verify subprocess launched is `automation.main` not `signal_smt.py`

**Expected**: Orchestrator log confirms live mode; automation subprocess PID visible in `orchestrator.log`.

### Edge Cases

- **PickMyTrade HTTP 429 (rate limit)**: retry with 2× backoff — ✅ `test_order_retries_on_500`
  (same retry path; 429 triggers same handler)
- **fill_mode=webhook, PickMyTrade never calls back**: `_pending` grows; fills never written; position still managed by Python state machine — acceptable; logged as `[FILL-WARN]` — ✅ `test_order_does_not_raise_on_failure`
- **IB Gateway restart mid-session**: `IbRealtimeSource` retry loop handles reconnect; `PickMyTradeExecutor` fill thread unaffected (separate thread) — ✅ manual test 2
- **Both `signal_smt.py` and `automation/main.py` running simultaneously**: use different IB client IDs (15 vs 20); no conflict — ✅ `test_automation_uses_correct_ib_client_id`

### Test Automation Summary

| | Count | % |
|---|---|---|
| ✅ Unit (pytest) | 26 new | 52% |
| ✅ Regression (Stage 1 suite, re-run) | ~136 | 27% |
| ⚠️ Manual (PickMyTrade demo) | 3 | 6% |
| ⚠️ Manual (Full automation session) | 2 | 4% |
| **Total** | ~167 | 100% |

Manual tests require paid PickMyTrade account and Apex demo account — automation physically
impossible without credentials.

---

## VALIDATION COMMANDS

### Level 0: External Service Validation (MUST PASS BEFORE LIVE USE)

```bash
# Verify PickMyTrade connectivity (requires .env populated)
python -c "
import os; from dotenv import load_dotenv; load_dotenv()
import httpx
r = httpx.get(os.environ['PMT_WEBHOOK_URL'].replace('/signal', '/ping'),
              headers={'Authorization': f\"Bearer {os.environ['PMT_API_KEY']}\"}, timeout=5)
print('PMT reachable:', r.status_code)
"
```

If failed: verify `PMT_WEBHOOK_URL`, `PMT_API_KEY`; check PickMyTrade account status.
**DO NOT PROCEED WITH LIVE TRADING UNTIL THIS PASSES.**

### Level 1: Syntax

```bash
python -c "from execution.pickmytrade import PickMyTradeExecutor; print('OK')"
python -c "from automation.main import main; print('OK')"
python -c "import orchestrator.main; print('OK')"
```

### Level 2: Unit Tests

```bash
uv run pytest tests/test_pickmytrade_executor.py -v
uv run pytest tests/test_automation_main.py -v
```

### Level 3: Full Regression Suite

```bash
uv run pytest -x -q
```

### Level 4: Manual Validation (Demo Only)

See Manual Tests 1–3 above. Execute against PickMyTrade demo account before enabling live.

---

## ACCEPTANCE CRITERIA

- [ ] `execution/pickmytrade.py` implements `FillExecutor` Protocol; `place_entry()` and `place_exit()` POST to PickMyTrade and return `None`
- [ ] `PickMyTradeExecutor.start()` raises `RuntimeError` if `PMT_WEBHOOK_URL` or `PMT_API_KEY` empty
- [ ] Fill poll loop runs in daemon thread; writes `FillRecord` to `fills.jsonl` when fill confirmed
- [ ] `automation/main.py` mirrors `signal_smt.py` signal logic; emits identical SIGNAL/EXIT JSON stdout lines
- [ ] `automation/main.py` uses `AUTOMATION_IB_CLIENT_ID` (default 20); does not conflict with `signal_smt.py`
- [ ] `orchestrator/main.py` launches `automation.main` when `LIVE_TRADING=true`, `signal_smt.py` otherwise
- [ ] `uv run pytest tests/test_pickmytrade_executor.py -v` — all 15 new tests pass
- [ ] `uv run pytest tests/test_automation_main.py -v` — all 11 new tests pass
- [ ] `uv run pytest -x -q` — full suite passes with no regressions
- [ ] `.env.example` documents all Stage 2 env vars with masked placeholder values
- [ ] `httpx` added to `pyproject.toml` dependencies
- [ ] Manual demo validation (Tests 1–3) completed before enabling `LIVE_TRADING=true` on live account

---

## COMPLETION CHECKLIST

- [ ] Stage 1 tests all passing (prerequisite verified)
- [ ] All tasks completed in wave order
- [ ] Each task validation passed
- [ ] All validation levels executed (0–4)
- [ ] All automated tests created and passing
- [ ] Manual tests documented and completed against demo account
- [ ] Full test suite passes with no regressions
- [ ] `.env.example` updated; no real credentials written
- [ ] `httpx` added to `pyproject.toml`
- [ ] **⚠️ Debug logs added during execution REMOVED**
- [ ] **⚠️ CRITICAL: Changes UNSTAGED — NOT committed**
- [ ] **⚠️ LIVE TRADING: Only enable `LIVE_TRADING=true` after demo validation passes**

---

## NOTES

**PickMyTrade API specifics**: The exact fill query endpoint, response schema, and webhook
payload format must be verified against the user's PickMyTrade account documentation before
implementing `_query_fill()` and `_fill_webhook_server()`. The plan provides the intended
interface; implementation details of those two methods depend on PickMyTrade's actual API.

**Demo-first policy**: Regardless of how tests perform, the sequence must be:
1. Demo account order placement manual test passes
2. Full session manual test passes on demo
3. Only then set `LIVE_TRADING=true` against live Apex account

**`assumed_entry` for P&L display**: When `PickMyTradeExecutor.place_entry()` returns `None`,
`automation/main.py` uses `signal["entry_price"]` as the display price in the SIGNAL line. Real
fill data arrives async and is stored in `fills.jsonl`. Post-session P&L in `summary.md` uses
`fills.jsonl` values, not the display estimate.

**Conflict avoidance**: `signal_smt.py` (IB client 15) and `automation/main.py` (IB client 20)
can both run simultaneously without IB connection conflicts. The orchestrator ensures only one
actually sends orders (via `LIVE_TRADING` flag), but both could run for monitoring purposes.

**⚠️ GAP — Post-session summarizer not updated**: The design doc states "The post-session
summarizer (Claude API call) reads `fills.jsonl` to compute accurate P&L rather than relying on
assumed prices from `signals.log`." Neither this plan nor Stage 1 includes updating
`orchestrator/summarizer.py` (or equivalent) to read `fills.jsonl`. This is unaddressed. Confirm
whether it belongs in this stage or a future one before executing.

**⚠️ GAP — `PMT_BASE_URL` for fill queries**: The External API Research section uses
`f"{PMT_BASE_URL}/fills"` but no `PMT_BASE_URL` env var is declared in the configuration table
or `.env.example`. Before implementing `_query_fill()`, decide whether to add a `PMT_FILLS_URL`
env var or derive the fills endpoint from `PMT_WEBHOOK_URL` (e.g. strip the last path segment).
