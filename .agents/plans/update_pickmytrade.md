# Feature: Fix & Extend PickMyTrade Executor

**⚠️ EXECUTION RULES — READ FIRST:**
- Implement ALL changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
- Only make code changes — no git operations

Validate documentation and codebase patterns before implementing. Match naming of existing utils, types, and models. Import from correct files.

---

## Feature Description

The existing `PickMyTradeExecutor` sends payloads that do not comply with the PickMyTrade API: wrong field names (`action` instead of `data`, `orderType` instead of `order_type`), wrong values (`"Market"` instead of `"MKT"`, `"Limit"` instead of `"LMT"`), missing required fields (`risk_percentage`, `token` in body, `multiple_accounts`), and missing order operations (SL attach after limit fill, market-close, limit-entry modify). Additionally, `automation/main.py` mis-wires the limit-order lifecycle: it calls `place_entry` after a limit already filled (duplicate entry) rather than calling it at `limit_placed` time and attaching the SL at `limit_filled` time.

## User Story

As the automated trading system  
I want all order payloads sent to PickMyTrade to comply with the PMT API spec  
So that orders are actually placed and filled on Tradovate rather than silently rejected or mis-routed

## Problem Statement

Every order this system sends to PMT today is malformed — the field names and values differ from what PMT expects. Additionally, the limit-order flow in `automation/main.py` would place a duplicate entry order at fill time, and there is no mechanism to attach a stop loss after a limit entry fills.

## Solution Statement

Rewrite the executor payload construction from scratch using a canonical `_build_payload` helper. Add `place_stop_after_limit_fill`, `place_close`, and `modify_limit_entry` methods. Fix the `lifecycle_batch` handling in `automation/main.py` to call `place_entry` at `limit_placed` time and `place_stop_after_limit_fill` at `limit_filled` time. Update all unit tests to assert the corrected API surface.

## Feature Metadata

**Feature Type**: Bug Fix + Enhancement  
**Complexity**: Medium  
**Primary Systems Affected**: `execution/pickmytrade.py`, `automation/main.py`, `execution/protocol.py`, `tests/test_pickmytrade_executor.py`  
**Dependencies**: PMT HTTP API (existing `httpx` client — no new deps)  
**Breaking Changes**: Yes — all existing tests asserting old field names (`action`, `orderType`, `isAutomated`) will fail until Task 2.2 updates them

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ BEFORE IMPLEMENTING

- `execution/pickmytrade.py` (lines 83–104) — `place_entry`: replace payload construction entirely
- `execution/pickmytrade.py` (lines 106–123) — `place_exit`: replace with `place_close` delegation
- `execution/pickmytrade.py` (lines 125–146) — `_post_order`: remove Bearer header; keep retry/logging logic
- `execution/protocol.py` (lines 25–27) — `FillExecutor` protocol: add `place_close` and `modify_limit_entry`
- `automation/main.py` (lines 601–614) — `lifecycle_batch` block: restructure to detect `limit_placed` / `limit_filled`
- `automation/main.py` (lines 611–613) — standalone non-signal event dispatch: add executor calls for `limit_placed` / `limit_moved`
- `automation/main.py` (lines 655–689) — step 11 place_entry: branch on `_from_limit_fill`
- `tests/test_pickmytrade_executor.py` (lines 76–295) — all tests asserting old field names; update wholesale

### New Files to Create

None — all changes are to existing files.

### Relevant Documentation — READ BEFORE IMPLEMENTING

- https://docs.pickmytrade.trade/docs/update-sl-tp-tradovate-pickmytrade/ — SL/TP payload, `multiple_accounts` shape
- https://docs.pickmytrade.trade/docs/automating-tradingview-strategies-with-limit-orders/ — `LMT` / `gtd_in_second`
- https://docs.pickmytrade.trade/docs/tradingview-json-alert-configuration/ — full field reference; indicator mode uses concrete `data` values (`"buy"` / `"sell"` / `"close"`)

### Patterns to Follow

**Payload helper**: `_build_payload(self, data, **extra)` — merges base fields with per-order extras via `dict.update`  
**Thread pool dispatch**: `self._order_pool.submit(self._post_order, order_id, payload, ctx)` for async fire-and-forget  
**Synchronous call for sequenced ops**: call `self._post_order(order_id, payload, ctx)` directly (not via pool) when the next step must wait for the result — used in `place_close` and `modify_limit_entry`

---

## PARALLEL EXECUTION STRATEGY

### Dependency Graph

```
┌──────────────────────────────────────────────────────────────┐
│ WAVE 1: Foundation (Parallel)                                │
├───────────────────────────────┬──────────────────────────────┤
│ Task 1.1: Fix pickmytrade.py  │ Task 1.2: Fix protocol.py   │
│ Agent: backend-engineer       │ Agent: backend-engineer      │
└───────────────────────────────┴──────────────────────────────┘
                               ↓
┌──────────────────────────────────────────────────────────────┐
│ WAVE 2: Integration & Tests (Parallel, after Wave 1)         │
├───────────────────────────────┬──────────────────────────────┤
│ Task 2.1: Fix main.py wiring  │ Task 2.2: Update tests       │
│ Deps: 1.1                     │ Deps: 1.1                    │
│ Agent: backend-engineer       │ Agent: test-engineer         │
└───────────────────────────────┴──────────────────────────────┘
                               ↓
┌──────────────────────────────────────────────────────────────┐
│ WAVE 3: Validation (Sequential)                              │
├──────────────────────────────────────────────────────────────┤
│ Task 3.1: Full test suite + validation  Deps: 2.1, 2.2       │
└──────────────────────────────────────────────────────────────┘
```

### Parallelization Summary

**Wave 1 — Fully Parallel**: Tasks 1.1, 1.2 — no dependencies between them  
**Wave 2 — Parallel after Wave 1**: Tasks 2.1, 2.2 — both need the new executor interface from 1.1  
**Wave 3 — Sequential**: Task 3.1 — needs both Wave 2 tasks complete

### Interface Contracts

**Contract 1**: Task 1.1 provides new executor method signatures (`place_close`, `place_stop_after_limit_fill`, `modify_limit_entry`) → consumed by Task 2.1 (automation/main.py calls) and Task 2.2 (test assertions)  
**Mock for parallel work**: Task 2.2 can use `MagicMock` for executor calls; actual implementation from 1.1 only needed at Wave 3

### Synchronization Checkpoints

**After Wave 1**: `uv run pytest tests/test_pickmytrade_executor.py -x` (will fail on old assertions — expected; confirms import works)  
**After Wave 2**: `uv run pytest tests/test_pickmytrade_executor.py tests/test_automation_main.py -x`  
**After Wave 3**: `uv run pytest --tb=short -q`

---

## IMPLEMENTATION PLAN

### Phase 1: Executor Core Fixes (`execution/pickmytrade.py`)

#### Task 1.1: REWRITE `execution/pickmytrade.py`

Complete replacement of payload construction. All order methods rebuilt on top of `_build_payload`.

**`_build_payload(self, data: str, **extra) -> dict`**

Base fields present in every payload:
```python
{
    "symbol":          self._symbol,
    "data":            data,           # "buy" | "sell" | "close"
    "quantity":        self._contracts,
    "risk_percentage": 0,
    "token":           self._api_key,
    "multiple_accounts": [{
        "token":               self._api_key,
        "account_id":          self._account_id,
        "risk_percentage":     0,
        "quantity_multiplier": 1,
    }],
}
```
Then `payload.update(extra)` for per-call additions (`order_type`, `price`, `sl`, `gtd_in_second`, etc.).

**`place_entry(self, signal, bar)`** — two branches:
- Market (`signal.get("limit_fill_bars") is None`): `order_type="MKT"`, `price=entry_price`, `sl=stop_price`
- Limit (`limit_fill_bars is not None`): `order_type="LMT"`, `price=entry_price`, `gtd_in_second=0`; no `sl`
- Both: fire via `self._order_pool.submit(self._post_order, ...)`, return `None`

**`place_stop_after_limit_fill(self, position, bar)`** — attaches SL after a limit entry is detected filled:
- Same `data` direction as the entry (`"buy"` for long, `"sell"` for short)
- Extra fields: `quantity=0`, `sl=float(position["stop_price"])`, `update_sl=True`, `pyramid=False`, `same_direction_ignore=True`
- Fire via thread pool (async)

**`place_close(self, label="close")`** — flattens position and cancels unfilled limits:
- `data="close"` only; no `order_type` needed
- Call `self._post_order(order_id, payload, ctx)` **directly** (synchronous, not pooled)
  so callers can sequence a follow-up request after the close succeeds

**`place_exit(self, position, exit_type, bar)`** — delegates to `place_close`:
```python
def place_exit(self, position, exit_type, bar):
    self.place_close(label=exit_type)
    return None
```
Remove fill-record tracking for exits (PMT does not return synchronous fill details for close orders).

**`modify_limit_entry(self, old_signal, new_signal, bar)`** — cancel + re-place:
1. `self.place_close(label="modify_cancel")` — synchronous; cancels the unfilled limit
2. Build new LMT payload at `new_signal["entry_price"]`, fire via thread pool

**`_post_order`** — remove `Authorization: Bearer` header; auth is in `token` field of payload body. Keep retry loop, exponential backoff, and `[FILL-WARN]` logging unchanged.

### Phase 2: Protocol Interface (`execution/protocol.py`)

#### Task 1.2: UPDATE `execution/protocol.py`

Add `place_close` and `modify_limit_entry` to the `FillExecutor` Protocol:
```python
def place_close(self, label: str = "close") -> None: ...
def modify_limit_entry(self, old_signal: dict, new_signal: dict, bar: BarRow) -> None: ...
```
`SimulatedFillExecutor` in `execution/simulated.py` must also get no-op stubs for these two methods so it continues to satisfy the protocol.

### Phase 3: Fix `automation/main.py` Limit-Order Wiring

#### Task 2.1: UPDATE `automation/main.py`

**Change A — Standalone non-signal events (lines ~611–613)**

Current:
```python
if evt_type != "signal":
    _dispatch_event(bar_ts, result)
    return
```

Replace with:
```python
if evt_type != "signal":
    _dispatch_event(bar_ts, result)
    _bar_row_for_evt = strategy_smt._BarRow(
        float(bar.Open), float(bar.High), float(bar.Low), float(bar.Close),
        float(bar.Volume), bar_ts,
    )
    if evt_type == "limit_placed":
        _executor.place_entry(result["signal"], _bar_row_for_evt)
    elif evt_type == "limit_moved":
        _executor.modify_limit_entry(result["old_signal"], result["new_signal"], _bar_row_for_evt)
    return
```

**Change B — lifecycle_batch block (lines ~602–614)**

Replace the current block with:
```python
if evt_type == "lifecycle_batch":
    evts = result["events"]
    has_limit_placed = any(e["type"] == "limit_placed" for e in evts)
    has_limit_filled  = any(e["type"] == "limit_filled"  for e in evts)
    for _sub in evts:
        _dispatch_event(bar_ts, _sub)
    _bar_row_for_fill = strategy_smt._BarRow(
        float(bar.Open), float(bar.High), float(bar.Low), float(bar.Close),
        float(bar.Volume), bar_ts,
    )
    # If limit_placed and limit_filled are in the same batch the limit filled
    # on the same bar it was placed — skip placing it (handled via signal path below)
    if has_limit_placed and not has_limit_filled:
        _lp = next(e for e in evts if e["type"] == "limit_placed")
        _executor.place_entry(_lp["signal"], _bar_row_for_fill)
    _signal_evts = [e for e in evts if e["type"] == "signal"]
    if not _signal_evts:
        return
    result = _signal_evts[0]
    evt_type = "signal"
    result["_from_limit_fill"] = has_limit_filled
```

**Change C — Step 11 place_entry (lines ~655–689)**

Branch on `signal.get("_from_limit_fill")`:
```python
_bar_row_for_fill = strategy_smt._BarRow(
    float(bar.Open), float(bar.High), float(bar.Low), float(bar.Close),
    float(bar.Volume), bar_ts,
)
if signal.get("_from_limit_fill"):
    assumed_entry = float(signal["entry_price"])
    _position = {
        **signal,
        "assumed_entry": assumed_entry,
        "contracts": 1,
        "instrument": "MNQ1!",
        "entry_time": str(signal["entry_time"]),
        "tp_breached": False,
    }
    POSITION_FILE.write_text(json.dumps(_position, indent=2))
    _executor.place_stop_after_limit_fill(_position, _bar_row_for_fill)
else:
    _entry_fill = _executor.place_entry(signal, _bar_row_for_fill)
    assumed_entry = _entry_fill.fill_price if _entry_fill else float(signal["entry_price"])
    _position = {
        **signal,
        "assumed_entry": assumed_entry,
        "contracts": 1,
        "instrument": "MNQ1!",
        "entry_time": str(signal["entry_time"]),
        "tp_breached": False,
    }
    POSITION_FILE.write_text(json.dumps(_position, indent=2))
```
Keep all remaining step-11 code (log lines, JSON print, reentry counter, state transition) unchanged.

### Phase 4: Update Tests

#### Task 2.2: UPDATE `tests/test_pickmytrade_executor.py`

Replace all assertions on old field names. Also add tests for new methods.

---

## STEP-BY-STEP TASKS

### WAVE 1: Foundation

#### Task 1.1: REWRITE `execution/pickmytrade.py`

- **WAVE**: 1
- **AGENT_ROLE**: backend-engineer
- **DEPENDS_ON**: []
- **BLOCKS**: [2.1, 2.2]
- **PROVIDES**: Corrected executor with `_build_payload`, `place_close`, `place_stop_after_limit_fill`, `modify_limit_entry`
- **IMPLEMENT**: See Phase 1 above; rewrite `place_entry`, `place_exit`, `_post_order`; add three new methods
- **PATTERN**: `execution/pickmytrade.py` existing class structure; keep `_fill_poll_loop`, `_fill_webhook_server`, `_record_fill` unchanged
- **VALIDATE**: `uv run python -c "from execution.pickmytrade import PickMyTradeExecutor; print('OK')"`

#### Task 1.2: UPDATE `execution/protocol.py` and `execution/simulated.py`

- **WAVE**: 1
- **AGENT_ROLE**: backend-engineer
- **DEPENDS_ON**: []
- **BLOCKS**: [2.2]
- **PROVIDES**: Updated `FillExecutor` protocol; `SimulatedFillExecutor` no-op stubs
- **IMPLEMENT**: Add `place_close(label="close") -> None` and `modify_limit_entry(old, new, bar) -> None` to `FillExecutor` protocol; add no-op stubs to `SimulatedFillExecutor`
- **VALIDATE**: `uv run python -c "from execution.simulated import SimulatedFillExecutor; print('OK')"`

**Wave 1 Checkpoint**: `uv run python -c "from execution.pickmytrade import PickMyTradeExecutor; from execution.simulated import SimulatedFillExecutor; print('imports OK')"`

---

### WAVE 2: Integration & Tests (Parallel after Wave 1)

#### Task 2.1: UPDATE `automation/main.py`

- **WAVE**: 2
- **AGENT_ROLE**: backend-engineer
- **DEPENDS_ON**: [1.1]
- **BLOCKS**: [3.1]
- **PROVIDES**: Correct limit-order executor wiring
- **USES_FROM_WAVE_1**: Task 1.1 provides `place_entry` (limit path), `place_stop_after_limit_fill`, `modify_limit_entry`
- **IMPLEMENT**: Changes A, B, C from Phase 3 above
- **VALIDATE**: `uv run python -c "import automation.main; print('OK')"`

#### Task 2.2: UPDATE `tests/test_pickmytrade_executor.py`

- **WAVE**: 2
- **AGENT_ROLE**: test-engineer
- **DEPENDS_ON**: [1.1]
- **BLOCKS**: [3.1]
- **PROVIDES**: Updated test suite asserting corrected API surface
- **USES_FROM_WAVE_1**: Task 1.1 provides new payload structure and method signatures
- **IMPLEMENT**: See Testing Strategy below — update existing tests and add new ones
- **VALIDATE**: `uv run pytest tests/test_pickmytrade_executor.py -v`

**Wave 2 Checkpoint**: `uv run pytest tests/test_pickmytrade_executor.py tests/test_automation_main.py -x -q`

---

### WAVE 3: Validation

#### Task 3.1: Full Test Suite & Validation

- **WAVE**: 3
- **AGENT_ROLE**: integration-specialist
- **DEPENDS_ON**: [2.1, 2.2]
- **IMPLEMENT**: Run full test suite; confirm no regressions; verify line-count of changed files
- **VALIDATE**: `uv run pytest --tb=short -q`

**Final Checkpoint**: Zero new failures vs pre-change baseline (936 passed / 9 skipped per PROGRESS.md)

---

## TESTING STRATEGY

### Unit Tests — Updated Assertions

**File**: `tests/test_pickmytrade_executor.py`  
**Tool**: pytest  
**Run**: `uv run pytest tests/test_pickmytrade_executor.py -v`

Tests to **update** (old assertions removed, new ones added):

| Test | Old assertion | New assertion |
|---|---|---|
| `test_place_entry_long_posts_buy_market_order` | `payload["action"] == "BUY"` | `payload["data"] == "buy"` and `payload["order_type"] == "MKT"` |
| `test_place_entry_short_posts_sell_market_order` | `payload["action"] == "SELL"` | `payload["data"] == "sell"` |
| `test_place_entry_limit_posts_limit_order` | `payload["orderType"] == "Limit"` | `payload["order_type"] == "LMT"` and `payload["gtd_in_second"] == 0` |
| `test_place_exit_long_posts_sell_close` | `payload["action"] == "SELL"` | `payload["data"] == "close"` |
| `test_place_exit_short_posts_buy_close` | `payload["action"] == "BUY"` | `payload["data"] == "close"` |
| `test_is_automated_flag_set` | `payload["isAutomated"] is True` | **Delete** — `isAutomated` removed |

Tests to **add**:

| Test | Asserts | Status |
|---|---|---|
| `test_market_entry_includes_sl` | `payload["sl"] == 19980.0` (stop from signal) | ✅ pytest |
| `test_limit_entry_excludes_sl` | `"sl" not in payload` | ✅ pytest |
| `test_market_entry_includes_multiple_accounts` | `payload["multiple_accounts"][0]["account_id"] == "ACC123"` | ✅ pytest |
| `test_token_in_payload_toplevel` | `payload["token"] == "test-key"` | ✅ pytest |
| `test_no_bearer_header` | `"Authorization" not in headers` (via call_args) | ✅ pytest |
| `test_risk_percentage_zero_in_all_payloads` | `payload["risk_percentage"] == 0` for entry + exit + sl | ✅ pytest |
| `test_place_stop_after_limit_fill_long` | `data=="buy"`, `quantity==0`, `update_sl==True`, `pyramid==False`, `same_direction_ignore==True`, `sl==19980.0` | ✅ pytest |
| `test_place_stop_after_limit_fill_short` | `data=="sell"`, `sl==19980.0` | ✅ pytest |
| `test_place_close_sends_data_close` | `payload["data"] == "close"` | ✅ pytest |
| `test_place_exit_delegates_to_close` | `payload["data"] == "close"` for all exit types | ✅ pytest |
| `test_modify_limit_entry_sends_close_then_limit` | two posts: first `data=="close"`, second `data=="buy"` with new price | ✅ pytest |
| `test_modify_limit_entry_close_is_synchronous` | `_post_order` called directly (not via pool) for close step | ✅ pytest |

### Edge Cases

- **Network error on place_close**: should not raise; warn-log and continue — ✅ `test_order_does_not_raise_on_failure` (existing, unchanged)
- **modify_limit_entry close failure**: second step (re-place) still fires — ✅ new test `test_modify_limit_entry_replaces_even_if_close_fails`
- **place_stop_after_limit_fill on short**: `data` is `"sell"` — ✅ `test_place_stop_after_limit_fill_short`

### Manual Tests

#### Manual Test 1: Live PMT order placement

**Why Manual**: Requires live PMT credentials and an active Tradovate demo account; bot-detection on PMT dashboard prevents Playwright automation  
**Steps**:
1. Set `PMT_WEBHOOK_URL`, `PMT_API_KEY`, `TRADING_ACCOUNT_ID` in `.env`
2. Run `uv run python -m automation.main` with IB-Gateway active
3. Observe `[ORCH]` and signal output; confirm orders appear in Tradovate order blotter
4. Verify stop is attached after a limit entry fills  
**Expected**: Market entry → position open with SL on Tradovate; limit entry → limit order in Tradovate; after fill → SL update applied

### Test Automation Summary

| | Count | % |
|---|---|---|
| ✅ Backend (pytest) — updated | 6 | 30% |
| ✅ Backend (pytest) — new | 13 | 65% |
| ⚠️ Manual (live PMT/Tradovate) | 1 | 5% |
| **Total** | 20 | 100% |

---

## VALIDATION COMMANDS

### Level 1: Syntax & Import Check

```bash
uv run python -c "from execution.pickmytrade import PickMyTradeExecutor; from execution.simulated import SimulatedFillExecutor; import automation.main; print('imports OK')"
```

### Level 2: Unit Tests

```bash
uv run pytest tests/test_pickmytrade_executor.py -v
```

### Level 3: Integration Tests

```bash
uv run pytest tests/test_pickmytrade_executor.py tests/test_automation_main.py tests/test_fill_executor.py -x -q
```

### Level 4: Full Suite

```bash
uv run pytest --tb=short -q
```

Expected: no new failures vs baseline (936 passed / 9 skipped)

---

## ACCEPTANCE CRITERIA

- [ ] Market entry payload: `data: buy/sell`, `order_type: MKT`, `sl: stop_price`, `token` at top-level, `multiple_accounts` array with `account_id` and `token`
- [ ] Limit entry payload: `order_type: LMT`, `price: entry_price`, `gtd_in_second: 0`, no `sl` field
- [ ] `place_stop_after_limit_fill`: same-direction, `quantity: 0`, `sl`, `update_sl: true`, `pyramid: false`, `same_direction_ignore: true`
- [ ] `place_exit` sends `data: close` for all exit types
- [ ] `place_close` runs synchronously (not via thread pool)
- [ ] `modify_limit_entry` sends close then re-places limit at updated price
- [ ] `risk_percentage: 0` present in every payload
- [ ] `isAutomated`, `action`, `orderType` fields absent from all payloads
- [ ] No `Authorization: Bearer` header sent
- [ ] `FillExecutor` protocol includes `place_close` and `modify_limit_entry`
- [ ] `SimulatedFillExecutor` has no-op stubs for both new protocol methods
- [ ] `automation/main.py`: standalone `limit_placed` event calls `place_entry` (limit path)
- [ ] `automation/main.py`: standalone `limit_moved` event calls `modify_limit_entry`
- [ ] `automation/main.py`: `limit_filled` in lifecycle_batch calls `place_stop_after_limit_fill`, not `place_entry`
- [ ] `automation/main.py`: market signal path (no `_from_limit_fill` tag) unchanged
- [ ] All updated and new unit tests pass
- [ ] Full test suite: zero new failures vs 936-passed baseline

---

## COMPLETION CHECKLIST

- [ ] External service verification passed (if applicable)
- [ ] All tasks completed in wave order
- [ ] Each task validation passed
- [ ] All validation levels executed (1–4)
- [ ] All automated tests created and passing
- [ ] Manual tests documented with instructions
- [ ] Full test suite passes (unit + integration)
- [ ] No linting/type errors
- [ ] All acceptance criteria met
- [ ] Code reviewed for quality
- [ ] **⚠️ Debug logs added during execution REMOVED (keep pre-existing)**
- [ ] **⚠️ CRITICAL: Changes UNSTAGED — NOT committed**

---

## NOTES

**`place_close` synchronicity**: `modify_limit_entry` needs close to complete before re-placing so the new limit does not land before the old one is cancelled. Running `_post_order` directly (not pooled) achieves this. If the close POST fails, `_post_order` logs `[FILL-WARN]` and returns; the re-place fires regardless. Tradovate will reject or accept it depending on whether the old limit is still live — acceptable risk.

**`data: close` semantics**: PMT's `close` action closes all open positions and cancels all unfilled limit orders for the symbol. In the modify-limit case there is no open position, only an unfilled limit — so `close` cleanly cancels it. When used as a position exit, it also removes any native stop Tradovate holds. The strategy does not rely on the native stop surviving past exit, so this is safe.

**No double-order concern**: Market entries carry `sl` which places a native stop on Tradovate. The strategy's `place_exit` calls also send `data: close`. Because the initial stop does not trail, a PMT stop fill and a strategy-triggered close should not race on the same event under normal conditions.

**Limit same-bar fill edge case**: If a limit order's `limit_placed` and `limit_filled` events appear in the same `lifecycle_batch` (limit placed and filled on the same 1s bar), `place_entry` is intentionally skipped in the batch handler. The signal path handles this correctly — `_from_limit_fill: True` means only the SL-attach fires, not a redundant limit POST.

**Fill-record tracking for exits**: The existing `_fill_poll_loop` and `_query_fill` machinery tracks pending orders by `order_id` and writes fill records to `fills.jsonl`. Close orders (`data: close`) do not produce a queryable fill via the PMT fills endpoint, so no `ctx` entry is added to `_pending` for `place_close` calls. The `_fill_poll_loop` and webhook server remain unchanged — they continue to track fills for entry and SL-attach orders if a `fills_url` is configured.

**SL-attach `quantity: 0` behavior**: PMT's `same_direction_ignore: true` + `pyramid: false` + `quantity: 0` combination is the documented way to send an update-only request that modifies existing orders without opening a new position. This is equivalent to TradingView sending a same-direction alert solely to update the SL on a live position.
