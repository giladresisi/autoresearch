# Feature: D7 — Session-End Close + IB Disconnect Hard Close

**⚠️ EXECUTION RULES — READ FIRST:**
- Implement ALL changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
- Only make code changes — no git operations

Validate documentation and codebase patterns before implementing. Match naming of existing utils, types, and models. Import from correct files.

---

## Feature Description

Two related behaviors to prevent unmanaged open positions at end-of-session or on loss of IB connectivity:

**1. SESSION END (16:00 ET)**
At 16:00 ET, close any open position via PMT and stop accepting new entries.
- V1 pipeline (`_process_managing`): already detects 16:00 but never calls `place_close`. Fix: add `_executor.place_close("session-end")` when `result == "exit_session_end"`.
- V2 pipeline (`_on_bar` V2 path): no session-end gate exists at all. Fix: add gate in `_on_bar` that closes active position and cancels pending entry, then returns.

Edge cases:
- Already flat → no-op, stop entries only
- Pending stop entry with no fill → cancel it
- Exit already dispatched same bar by manage_position → no double-close (handled by flow structure)

**2. IB DISCONNECT WITH OPEN POSITION**
When `IbGatewayDisconnectedError` is raised:
- If position is open (V1 or V2): wait 30 seconds (wall clock, not bars)
- After 30 seconds: send hard close via PMT (`_executor.place_close("ib-disconnect")`)
- If PMT fails: log error only; do NOT retry (leave for AutoLiq)
- If already flat: no timer, exit immediately

Relationship with orchestrator: the orchestrator's `_close_session_position()` runs AFTER automation exits. Since `close_position()` clears `position.json`, the orchestrator's subsequent close finds no position and is a safe no-op.

## User Story

As the trading system
I want positions to be closed automatically at session end (16:00 ET) and on IB disconnect (after 30s grace)
So that no position is left unmanaged overnight due to the strategy exiting without sending a close order

## Problem Statement

1. V1 `_process_managing` sets `result = "exit_session_end"` at 16:00 but never calls `place_close()` — position stays open in Tradovate.
2. V2 `_on_bar` processes bars after 16:00 indefinitely — no session-end close, no entry gate.
3. `except IbGatewayDisconnectedError` immediately calls `sys.exit(2)` — no PMT close sent.

## Solution Statement

- `automation/main.py::_process_managing()`: when `result == "exit_session_end"`, call `_executor.place_close("session-end")` before computing `exit_price`.
- `automation/main.py::_on_bar()` V2 path: add gate at top of V2 block — if `_bar_ts.time() >= SESSION_CLOSE`, cancel any pending entry and close active position via `live_orders`, then return.
- `automation/main.py::main()` except block: add position check → 30s sleep → `_executor.place_close("ib-disconnect")` before `sys.exit(2)`.

## Feature Metadata

**Feature Type**: Bug Fix + Enhancement
**Complexity**: Medium
**Primary Systems Affected**: `automation/main.py`
**Secondary Systems Affected**: None (uses existing `_executor.place_close()` and `live_orders` APIs)
**Dependencies**: `live_orders` (already imported lazily in automation/main.py)
**Breaking Changes**: None — all changes are additive closes that were previously missing

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ BEFORE IMPLEMENTING

- `automation/main.py` (lines 809–931) — `_process_managing` and `_on_bar` to modify
- `automation/main.py` (lines 992–1134) — `main()` and `except IbGatewayDisconnectedError` to modify
- `execution/pickmytrade.py` (lines 127–131) — `place_close()` is synchronous, safe to call in except block
- `live_orders.py` (lines 82–94, 213–223) — `has_active_position()`, `has_pending_entry()`, `cancel_stop_entry()`, `close_position()`
- `session_times.py` — `SESSION_CLOSE = datetime.time(16, 0)` (do NOT modify)
- `tests/test_automation_main.py` — existing test patterns (monkeypatch `_state`, `_position`, `_executor`)
- `tests/test_live_orders.py` — existing live_orders test patterns

---

## IMPLEMENTATION PLAN

### WAVE 1 — Core changes (all tasks independent, run in parallel)

---

#### Task 1.1 — Fix V1 session-end close (WAVE 1)

**File**: `automation/main.py`
**Function**: `_process_managing`
**Lines to change**: around line 883 in the `if result == "exit_session_end":` branch

**Current code** (approximate):
```python
if result == "exit_session_end":
    exit_price = float(bar.Close)
```

**New code**:
```python
if result == "exit_session_end":
    try:
        _executor.place_close("session-end")
    except Exception as _exc:
        print(f"[automation] session-end close error: {_exc}", flush=True)
    exit_price = float(bar.Close)
```

**Why the try/except**: PMT close failure should never prevent position state cleanup. State is cleared regardless (lines after compute P&L).

**Edge case guard (already handled by flow)**:
The `exit_session_end` path is only reached when `manage_position()` returned "hold" AND `bar_time >= SESSION_CLOSE`. If an exit was already dispatched this bar by `manage_position()` (returning "exit_tp", "exit_stop", etc.), the code goes into the `elif result in (...)` branch and never reaches `exit_session_end`. No double-close possible.

---

#### Task 1.2 — Add V2 session-end gate (WAVE 1)

**File**: `automation/main.py`
**Function**: `_on_bar`
**Location**: In the V2 path, immediately after the `if _bar_ts.time() < SESSION_OPEN: return` gate (around line 159)

Add the session-close gate BEFORE V2 bar processing:

```python
# Existing open gate
if _bar_ts.time() < SESSION_OPEN:
    return

# NEW: session-close gate for V2
if _bar_ts.time() >= SESSION_CLOSE:
    import live_orders as _lo_sc
    if _lo_sc.has_pending_entry():
        _lo_sc.cancel_stop_entry("session-end")
    if _lo_sc.has_active_position():
        _lo_sc.close_position(float(getattr(bar, "Close", 0.0)), reason="session-end")
    return

# ... rest of V2 path continues unchanged
```

**Important**: use a different import alias (`_lo_sc`) to avoid shadowing any existing `_lo` reference in `_on_bar`.

**Double-close guard**: `close_position()` clears `position.json` immediately. On the next bar (also ≥ 16:00), `has_active_position()` returns False → no second close.

**Pending entry at session end**: `cancel_stop_entry()` sends PMT cancel + clears `stop_entry` in position.json. Always called before position close so the cancel fires even when no active position exists.

---

#### Task 1.3 — IB disconnect grace timer (WAVE 1)

**File**: `automation/main.py`
**Function**: `main()`
**Location**: the `except IbGatewayDisconnectedError:` block (around line 1117)

**Current code**:
```python
except IbGatewayDisconnectedError:
    print("[automation] IB Gateway disconnected — exiting with code 2", flush=True)
    sys.exit(2)
```

**New code**:
```python
except IbGatewayDisconnectedError:
    import time as _wall_clock
    import live_orders as _lo_dc
    _has_position = (
        (_state == "MANAGING" and _position is not None)
        or _lo_dc.has_active_position()
    )
    if _has_position:
        print(
            "[automation] IB disconnect with open position — "
            "30s grace period (reconnect IB now to resume)",
            flush=True,
        )
        _wall_clock.sleep(30)
        print("[automation] Grace period expired — issuing hard close via PMT", flush=True)
        try:
            _executor.place_close("ib-disconnect")
        except Exception as _dc_exc:
            print(
                f"[automation] Hard close failed (leaving for AutoLiq): {_dc_exc}",
                flush=True,
            )
    print("[automation] IB Gateway disconnected — exiting with code 2", flush=True)
    sys.exit(2)
```

**Key design decisions**:
- Use `time.sleep(30)` (wall clock) — not bar counts; session may be in 1s or 1m mode.
- `_executor.place_close()` is synchronous — completes before `sys.exit(2)` raises SystemExit.
- `_executor.stop()` runs in the `finally` block AFTER SystemExit propagates; by then place_close is already done.
- Single call to `_executor.place_close()` covers both V1 (uses same executor) and V2 (PMT instance).
- No retry loop — failure is logged and left for AutoLiq per user requirement.
- Orchestrator's `_close_session_position()` runs after code 2 exit; if automation closed successfully, `position.json` is cleared and orchestrator close is a safe no-op.

---

### WAVE 2 — Tests (depends on Wave 1)

---

#### Task 2.1 — V1 session-end tests (WAVE 2, DEPENDS_ON: 1.1)

**File**: `tests/test_automation_main.py`
**Section**: New test class or appended tests

Write the following 3 tests:

**Test A: `test_v1_session_end_sends_pmt_close`**
Setup:
- `_state = "MANAGING"`, `_position = {"direction": "long", ...}`
- `_session_end_time = datetime.time(13, 30)` (mock)
- Bar at 13:30:00 (at or after session end)
- `manage_position` patched to return "hold"
- `_executor.place_close` is a MagicMock

Action: call `_process_managing(bar, bar_ts, bar_ts.time())`

Assert:
- `_executor.place_close.called` is True
- First call arg is "session-end"
- `_state == "SCANNING"` after call (position cleared)

**Test B: `test_v1_session_end_already_scanning`**
Setup: `_state = "SCANNING"` (no position)

Action: ensure `_process_managing` is not even called (state routing in `_process` skips SCANNING state to `_process_scanning`).

Assert: `_executor.place_close` never called.
Note: this test documents the routing — `_process_managing` is only called when state == "MANAGING".

**Test C: `test_v1_session_end_no_double_close_on_stop`**
Setup:
- `_state = "MANAGING"`, `_position = {...}`
- Bar at 13:30:00
- `manage_position` patched to return "exit_stop" (stop hit this bar)

Action: call `_process_managing(bar, bar_ts, bar_ts.time())`

Assert:
- `_executor.place_exit` called with "exit_stop" (the normal stop close)
- `_executor.place_close` NOT called with "session-end" (no double-close)
- `_state == "SCANNING"` after call

---

#### Task 2.2 — V2 session-end tests (WAVE 2, DEPENDS_ON: 1.2)

**File**: `tests/test_automation_main.py`

**Test D: `test_v2_session_end_closes_active_position`**
Setup:
- `_smtv2_pipeline = "v2"`, `_smtv2_dispatcher` is set
- Bar at 16:00:01 ET (past SESSION_CLOSE)
- `live_orders.has_pending_entry()` returns False
- `live_orders.has_active_position()` returns True
- `live_orders.close_position` is patched as MagicMock

Action: call `_on_bar(bar, mes_partial)` with the 16:00:01 bar

Assert:
- `live_orders.close_position` called once with `reason="session-end"`
- Pipeline's `on_1m_bar` NOT called (returned early)

**Test E: `test_v2_session_end_cancels_pending_entry`**
Setup:
- Bar at 16:00:01 ET
- `live_orders.has_pending_entry()` returns True
- `live_orders.has_active_position()` returns False
- `live_orders.cancel_stop_entry` is patched as MagicMock

Action: call `_on_bar(bar, mes_partial)` with the 16:00:01 bar

Assert:
- `live_orders.cancel_stop_entry` called with "session-end"
- `live_orders.close_position` NOT called (no active position)

**Test F: `test_v2_session_end_already_flat_noop`**
Setup:
- Bar at 16:00:01 ET
- `live_orders.has_pending_entry()` returns False
- `live_orders.has_active_position()` returns False

Action: call `_on_bar(bar, mes_partial)` with the 16:00:01 bar

Assert: neither `close_position` nor `cancel_stop_entry` called.

---

#### Task 2.3 — IB disconnect grace timer tests (WAVE 2, DEPENDS_ON: 1.3)

**File**: `tests/test_automation_main.py`

**Test G: `test_ib_disconnect_v1_position_open_sends_close_after_30s`**
Setup:
- Patch `time.sleep` to be a no-op (don't actually sleep 30s)
- `automation.main._state = "MANAGING"`
- `automation.main._position = {"direction": "long", ...}`
- `automation.main._executor.place_close` is a MagicMock
- `live_orders.has_active_position()` returns False (V1 position, not in smt_state)

Trigger: call `main()` but patch `IbRealtimeSource.start()` to raise `IbGatewayDisconnectedError` immediately.

Assert:
- `time.sleep` called with 30
- `_executor.place_close` called with "ib-disconnect"
- Process exits with code 2 (`SystemExit(2)` raised)

**Test H: `test_ib_disconnect_already_flat_exits_immediately`**
Setup:
- `automation.main._state = "SCANNING"` / `_position = None`
- `live_orders.has_active_position()` returns False
- Patch `time.sleep`

Trigger: `IbGatewayDisconnectedError` raised

Assert:
- `time.sleep` NOT called
- `_executor.place_close` NOT called
- `SystemExit(2)` raised

**Test I: `test_ib_disconnect_v2_position_open_sends_close`**
Setup:
- `automation.main._state = "SCANNING"` (V2 manages separately)
- `automation.main._position = None`
- `live_orders.has_active_position()` returns True (V2 active position)
- Patch `time.sleep`, `_executor.place_close`

Trigger: `IbGatewayDisconnectedError` raised

Assert:
- `time.sleep` called with 30
- `_executor.place_close` called with "ib-disconnect"
- `SystemExit(2)` raised

**Test J: `test_ib_disconnect_close_failure_still_exits`**
Setup:
- V1 position open
- `_executor.place_close` raises RuntimeError
- Patch `time.sleep`

Trigger: `IbGatewayDisconnectedError` raised

Assert:
- `SystemExit(2)` raised (not SystemExit(1) or any other)
- No uncaught exception propagated

---

## ACCEPTANCE CRITERIA

The following criteria must all pass before this feature is considered complete.

### AC-1: V1 session-end PMT close fires

**Scenario**: V1 pipeline active at 16:00 ET with an open position.
- `_state == "MANAGING"`, `_position != None`
- `manage_position()` returns "hold" on the 16:00 bar (no natural exit)

**Expected**: `_executor.place_close("session-end")` is called exactly once.

**Test**: Task 2.1 Test A

---

### AC-2: V1 session-end does not double-close on natural exit

**Scenario**: `manage_position()` returns "exit_stop" at 16:00.

**Expected**: `place_exit("exit_stop", ...)` called; `place_close("session-end")` NOT called.

**Test**: Task 2.1 Test C

---

### AC-3: V2 session-end closes active position

**Scenario**: V2 pipeline running; `_on_bar` receives a bar at 16:00:01 ET; `has_active_position()` is True.

**Expected**: `live_orders.close_position(reason="session-end")` called; `_smtv2_dispatcher._pipeline.on_1m_bar` NOT called (early return).

**Test**: Task 2.2 Test D

---

### AC-4: V2 session-end cancels pending entry

**Scenario**: V2 pipeline; bar at 16:00:01; pending unfilled stop entry, no active position.

**Expected**: `live_orders.cancel_stop_entry("session-end")` called; `close_position` not called.

**Test**: Task 2.2 Test E

---

### AC-5: IB disconnect with V1 open position waits 30s then closes

**Scenario**: `IbGatewayDisconnectedError` raised; `_state == "MANAGING"`, `_position != None`.

**Expected**: `time.sleep(30)` called; `_executor.place_close("ib-disconnect")` called; process exits with code 2.

**Test**: Task 2.3 Test G

---

### AC-6: IB disconnect already flat — no timer, no close

**Scenario**: `IbGatewayDisconnectedError` raised; no position open.

**Expected**: `time.sleep` NOT called; `place_close` NOT called; exits with code 2.

**Test**: Task 2.3 Test H

---

### AC-7: IB disconnect close failure is non-fatal

**Scenario**: Position open; `place_close()` raises an exception.

**Expected**: error logged to stdout; no retry; process exits with code 2.

**Test**: Task 2.3 Test J

---

### AC-8: Time comparison uses wall-clock / bar time (not bar count)

**Verification**: Code inspection — 30s grace uses `time.sleep(30)` (wall clock); session-end check uses `bar_ts.time() >= SESSION_CLOSE` (timestamp comparison).

**Test**: implicit in all tests; no `_bar_counter` or `_bar_count` variables introduced.

---

## TECHNICAL NOTES

### Why `_executor.place_close()` and not `live_orders.close_position()` for disconnect

`live_orders._executor` is a different instance from `automation/main._executor`. In V2 live mode, both are PMT executors, but `live_orders` reads `LIVE_TRADING` env var at import time to decide which executor to instantiate. Using `automation/main._executor` (the authoritative live instance already running) avoids creating a second connection and ensures consistent retry/timeout settings.

### V2 session-end position state after close

`live_orders.close_position()` calls its own executor's `place_close()` AND clears `position.json` via `smt_state`. This means:
1. PMT receives the close
2. Next `_on_bar` bar (still ≥ 16:00) finds `has_active_position() == False` → no second close

### Orchestrator interaction on IB disconnect

After automation exits with code 2, the orchestrator runs `_close_session_position()` which reads `smt_state.position.json`. If automation's 30s grace close succeeded and cleared position.json, the orchestrator's close is a no-op. If the close failed (PMT error), the orchestrator provides a second attempt. Both outcomes are safe.

### V1 flow structure guarantees no double-close at session end

The `exit_session_end` branch is reached ONLY when `manage_position()` returned "hold" AND `bar_time >= SESSION_CLOSE`. Any other exit result (stop, TP, partial) goes through the `elif` branch which calls `place_exit()`, sets state to SCANNING, and does NOT reach `exit_session_end`. The two branches are mutually exclusive by construction.

---

## TEST AUTOMATION SUMMARY

| Test | File | Kind | Coverage |
|------|------|------|----------|
| test_v1_session_end_sends_pmt_close | test_automation_main.py | unit | AC-1 |
| test_v1_session_end_already_scanning | test_automation_main.py | unit | AC-2 (routing) |
| test_v1_session_end_no_double_close_on_stop | test_automation_main.py | unit | AC-2 |
| test_v2_session_end_closes_active_position | test_automation_main.py | unit | AC-3 |
| test_v2_session_end_cancels_pending_entry | test_automation_main.py | unit | AC-4 |
| test_v2_session_end_already_flat_noop | test_automation_main.py | unit | AC-3, AC-4 (negative) |
| test_ib_disconnect_v1_position_open_sends_close_after_30s | test_automation_main.py | unit | AC-5 |
| test_ib_disconnect_already_flat_exits_immediately | test_automation_main.py | unit | AC-6 |
| test_ib_disconnect_v2_position_open_sends_close | test_automation_main.py | unit | AC-5 (V2 variant) |
| test_ib_disconnect_close_failure_still_exits | test_automation_main.py | unit | AC-7 |

**Total**: 10 new tests, all automated
**Manual tests**: None — all paths testable via mock/monkeypatch
**Gaps**: None

---

## EXECUTION AGENT RULES (verbatim)

- Make ALL code changes required by the plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
