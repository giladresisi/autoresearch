# Feature: live_orders Refactor, bar_state.json, and trade.py CLI

**Complexity**: ⚠️ Medium
**Status**: Planned
**Plan File**: `.agents/plans/live-orders-refactor-and-trade-cli.md`

---

## User Story
As a trader, I want to send manual trade orders from the command line (`python trade.py up/down/cancel/move/close`) during a live session so that I can act without a Claude Code agent, while sharing identical dispatch logic with the automated strategy.

## Feature Summary
Six coordinated changes:
1. **strategy.py** — add `stop` price to `new-stop-entry`/`move-stop-entry` signals
2. **smt_state.py + session_pipeline.py** — write `sessions/{date}/bar_state.json` every 1m bar
3. **live_orders.py** — unified single-tier: every function logs + dispatches + syncs position.json
4. **strategy.py fill detection** — fallback to `bar_state.json` when `confirmation_bar` is empty
5. **SmtV2Dispatcher._emit + orchestrator** — use new live_orders API, read stop from signal
6. **trade.py** — new CLI calling shared live_orders functions directly

## ACCEPTANCE CRITERIA

### Functional — Signals
- [ ] `new-stop-entry` and `move-stop-entry` signals from `strategy.py` contain a `stop` key with the protective stop price (`body_low` for long, `body_high` for short from `opp_5m`).
- [ ] `SmtV2Dispatcher._emit` reads `stop` directly from the signal dict for `new-stop-entry`/`move-stop-entry` — no `position.json` read for stop price in those branches.

### Functional — live_orders.py API
- [ ] `live_orders.py` exposes exactly: `place_stop_entry`, `place_market_entry`, `move_stop_entry`, `stop_entry_filled`, `cancel_stop_entry`, `close_position`, `update_stop_loss`, `get_position`, `has_active_position`, `has_pending_entry`. Old names (`place_entry`, `modify_stop_entry`, `place_stop_after_fill`, `cancel_entry`, `close`, `manual_close`, `manual_cancel_entry`) are removed.
- [ ] Every `live_orders` function logs to `events.jsonl`, dispatches to the executor, and syncs `position.json` in a single call.

### Functional — bar_state.json
- [ ] `sessions/{today}/bar_state.json` is written after every 1m bar with fields `time`, `potential_stop_long`, `potential_stop_short` (float or `null`).
- [ ] Formula uses the last completed 5m bar's OHLC (any direction): `potential_stop_long = max(bar_low, body_low − 15.0)`, `potential_stop_short = min(bar_high, body_high + 15.0)`.

### Functional — Fill Detection Fallback
- [ ] Fill detection in `strategy.py` with an empty `confirmation_bar` reads `bar_state.json` for stop price; logs a warning and returns `None` (skips fill) if `bar_state.json` is absent or stop is `null`.

### Functional — trade.py CLI
- [ ] `python trade.py up` → market LONG with S/L from `bar_state.potential_stop_long`; prints stop used; exits code 1 with clear error if bar_state missing or stop is null.
- [ ] `python trade.py up 27000` → stop entry LONG at 27000 with no S/L (placeholder 0.0; S/L computed at fill).
- [ ] `python trade.py down` / `python trade.py down 27000` — symmetric SHORT equivalents of the two above.
- [ ] `python trade.py cancel` → cancels pending stop entry; exits code 1 with clear error if `stop_entry` is empty.
- [ ] `python trade.py move 28000` → moves pending stop entry to 28000; exits code 1 if none pending.
- [ ] `python trade.py close` → market close active position; exits code 1 if no active position.
- [ ] `python trade.py close 27000` → sets stop-loss to 27000 on active position; exits code 1 if no active position.

### Validation
- [ ] All pre-existing tests pass unchanged — verified by: `python -m pytest -x -q`
- [ ] New test suite passes — verified by: `python -m pytest tests/test_live_orders.py tests/test_bar_state.py tests/test_smt_strategy_v2.py tests/test_trade_cli.py -v`
- [ ] `python trade.py up` imports cleanly without exception (runnability) — verified by: `test_up_market_reads_bar_state`

### Out of Scope
- PickMyTradeExecutor and SimulatedBrokerExecutor internals — not changed
- `confirmation_bar` field in position.json — still set by strategy.py for deduplication; not removed
- session_pipeline.py signal routing logic — only the bar_state.json write is added
- Live PMT end-to-end validation — requires live PMT credentials; not part of automated suite

---

## Execution Agent Rules
- Make ALL code changes required by the plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`

---

## Architecture Notes

### Position.json role after refactor
`strategy.py` still owns: `confirmation_bar` (dedup key), `failed_entries`, `stop_entry`/`stop_direction` (decision state written before signal emission). `live_orders.py` also writes `stop_entry`/`stop_direction`/`active` as order-outcome state — values are the same so the redundant write is harmless. The net effect: position.json reflects reality whether the source was automated or manual.

### bar_state.json last-5m-bar formula
Every 1m bar: floor `now` to previous 5m boundary, collect all 1m bars in `[prev_5m, current_5m)` from `today_mnq`. Build synthetic OHLC (first open, last close, max high, min low). Compute body. Apply formula. Write atomically to `sessions/{date}/bar_state.json`. If window empty → nulls.

### SimulatedBrokerExecutor gap
`SimulatedBrokerExecutor` is missing `place_stop_after_limit_fill` (used by the new `stop_entry_filled`). Add it as a no-op to match `PickMyTradeExecutor`'s interface. This affects paper-mode correctness only (no live impact).

---

## Wave 1 — Three independent changes (run in parallel)

### Task 1 — strategy.py: include stop in stop-entry signals [WAVE 1]
**File**: `strategy.py`
**AGENT_ROLE**: Code editor — targeted edit only

In the stop-entry emission block (lines ~270–275), before `_make_signal`:

```python
# Add before the existing return:
stop_loss = float(opp_5m["body_low"]) if direction == _DIR_UP else float(opp_5m["body_high"])
return _make_signal(kind, now, body_end_price, stop=stop_loss)
```

**Why body_low/body_high and not the wick-cap formula?** The fill detection path (lines 192–196) uses `conf_bar["body_low"]` (not wick-capped) so the signal's `stop` must match what fill detection will compute. Market-entry uses the wick-cap formula because it's sending the actual S/L price in the same order; stop-entry defers S/L to fill time.

**Do NOT touch**: fill detection (lines 190–213), market-entry stop calculation (lines 244–247), any other signal paths.

---

### Task 2 — smt_state.py + session_pipeline.py: bar_state.json [WAVE 1]
**Files**: `smt_state.py`, `session_pipeline.py`
**AGENT_ROLE**: Code editor

#### 2a. smt_state.py — add bar_state helpers

Add after `save_position`:

```python
def bar_state_path(date_str: str | None = None) -> Path:
    import datetime as _dt
    d = date_str or _dt.date.today().isoformat()
    return Path("sessions") / d / "bar_state.json"


def save_bar_state(data: dict, date_str: str | None = None) -> None:
    path = bar_state_path(date_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, data)


def load_bar_state(date_str: str | None = None) -> dict | None:
    path = bar_state_path(date_str)
    if _IN_MEMORY:
        return _STORE.get(str(path))
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
```

Note: `save_bar_state` uses `_atomic_write` so in-memory mode is respected (tests can intercept it).

#### 2b. session_pipeline.py — write bar_state after on_1m_bar

Import at top: `from smt_state import save_bar_state`

Add private method to `SessionPipeline`:

```python
_STOP_WICK_CAP = 15.0  # must match strategy.py

def _write_bar_state(self, now: pd.Timestamp, today_mnq: pd.DataFrame) -> None:
    current_5m = now.floor("5min")
    prev_5m = current_5m - pd.Timedelta(minutes=5)
    window = today_mnq[(today_mnq.index >= prev_5m) & (today_mnq.index < current_5m)]
    if window.empty:
        save_bar_state({"time": now.isoformat(),
                        "potential_stop_long": None,
                        "potential_stop_short": None})
        return
    bar_open  = float(window.iloc[0]["Open"])
    bar_close = float(window.iloc[-1]["Close"])
    bar_high  = float(window["High"].max())
    bar_low   = float(window["Low"].min())
    body_high = max(bar_open, bar_close)
    body_low  = min(bar_open, bar_close)
    save_bar_state({
        "time": now.isoformat(),
        "potential_stop_long":  round(max(bar_low,  body_low  - self._STOP_WICK_CAP), 4),
        "potential_stop_short": round(min(bar_high, body_high + self._STOP_WICK_CAP), 4),
    })
```

In `on_1m_bar`, after collecting `events` from the pipeline processing and before `return events`, call `self._write_bar_state(now, today_mnq)`.

---

### Task 3 — live_orders.py: unified single-tier rewrite [WAVE 1]
**File**: `live_orders.py` — full rewrite
**AGENT_ROLE**: Code editor

Complete replacement. Keep: executor singleton init block, `_log`, `_load_pos`, `_save_pos`.

**New public API** (each function: log → executor → sync position.json):

```python
def place_stop_entry(direction: str, entry_price: float, stop_price: float) -> None:
    """Place unfilled stop entry. Logs, dispatches STP order, writes stop_entry to position.json."""

def place_market_entry(direction: str, entry_price: float, stop_price: float) -> None:
    """Enter at market with stop. Logs, dispatches MKT+sl, writes active to position.json."""

def move_stop_entry(new_entry_price: float, new_stop_price: float, direction: str) -> None:
    """Cancel existing unfilled stop entry and replace. Reads old entry_price from position.json."""

def stop_entry_filled(direction: str, stop_price: float) -> None:
    """Stop entry just filled — send protective S/L to PMT, log, update active.stop in position.json."""

def cancel_stop_entry(reason: str = "user-requested") -> None:
    """Cancel pending stop entry. No-op if stop_entry is empty. Logs, dispatches close, clears position.json."""

def close_position(price: float, reason: str = "user-requested") -> None:
    """Market-close active position. Logs, dispatches close, clears active in position.json."""

def update_stop_loss(stop_price: float, reason: str = "user-requested") -> None:
    """Update protective stop on active position (trade.py close <price>). Logs, dispatches update_sl, updates active.stop."""
```

**Implementation details**:

`place_stop_entry`:
- pmt_signal = `{"direction": direction, "entry_price": entry_price, "stop_price": stop_price, "stop_fill_bars": 1}`
- `_executor.place_entry(pmt_signal, None)`
- pos["stop_entry"] = str(entry_price); pos["stop_direction"] = "up" if direction=="long" else "down"
- log kind="new-stop-entry"

`place_market_entry`:
- pmt_signal = `{"direction": direction, "entry_price": entry_price, "stop_price": stop_price}`
- `_executor.place_entry(pmt_signal, None)`
- pos["active"] = `{"direction": direction, "fill_price": entry_price, "stop": stop_price, "cautious": "no", "contracts": 2}`; pos["stop_entry"] = ""; pos["stop_direction"] = ""
- log kind="market-entry"

`move_stop_entry`:
- Read old_entry = float(pos["stop_entry"]) if set, else new_entry_price
- old_pmt = `{"direction": direction, "entry_price": old_entry, "stop_price": new_stop_price, "stop_fill_bars": 1}`
- new_pmt = `{"direction": direction, "entry_price": new_entry_price, "stop_price": new_stop_price, "stop_fill_bars": 1}`
- `_executor.modify_stop_entry(old_pmt, new_pmt, None)`
- pos["stop_entry"] = str(new_entry_price)
- log kind="move-stop-entry"

`stop_entry_filled`:
- `_executor.place_stop_after_limit_fill({"direction": direction, "stop_price": stop_price}, None)`
- pos["active"]["stop"] = stop_price (if active exists)
- log kind="stop-entry-filled"

`cancel_stop_entry`:
- Read pos; if stop_entry == "" → return (no-op)
- `_executor.place_close("cancel-stop")`
- pos["stop_entry"] = ""; pos["stop_direction"] = ""; pos["confirmation_bar"] = {}
- log kind="cancel-stop-entry"

`close_position`:
- `_executor.place_close("close")`
- pos["active"] = {}; pos["stop_entry"] = ""; pos["stop_direction"] = ""; pos["confirmation_bar"] = {}
- log kind="market-close"

`update_stop_loss`:
- Read direction from pos["active"]["direction"]
- `_executor.place_stop_after_limit_fill({"direction": direction, "stop_price": stop_price}, None)`
- pos["active"]["stop"] = stop_price
- log kind="update-stop-loss"

**Also add to `SimulatedBrokerExecutor`** (`execution/simulated.py`): a no-op `place_stop_after_limit_fill(self, position, bar)` method (currently missing, would cause AttributeError in paper mode).

**Remove** from live_orders.py: `place_entry`, `modify_stop_entry`, `place_stop_after_fill`, `cancel_entry`, `close`, `manual_close`, `manual_cancel_entry`, module-level `_pending_entry`.

Keep: `get_position`, `has_active_position`, `has_pending_entry`, `_log`, `_load_pos`, `_save_pos`.

---

## Wave 2 — Three changes dependent on Wave 1 (run in parallel)

### Task 4 — strategy.py: fill detection fallback [WAVE 2, DEPENDS_ON: Task 2]
**File**: `strategy.py`
**AGENT_ROLE**: Code editor

Modify the fill detection block (lines 190–213). After reading `conf_bar = position["confirmation_bar"]`:

```python
if conf_bar:
    # Automated path: confirmation_bar set by strategy
    if direction == _DIR_UP:
        stop = max(float(conf_bar["low"]), float(conf_bar["body_low"]) - _STOP_WICK_CAP)
    else:
        stop = min(float(conf_bar["high"]), float(conf_bar["body_high"]) + _STOP_WICK_CAP)
else:
    # Manual path: stop entry placed via trade.py — use bar_state.json
    from smt_state import load_bar_state
    bar_state = load_bar_state()
    if bar_state is None:
        print(f"[STRATEGY] fill detected but no bar_state.json — skipping fill", flush=True)
        return None
    stop = bar_state.get("potential_stop_long" if direction == _DIR_UP else "potential_stop_short")
    if stop is None:
        print(f"[STRATEGY] fill detected but potential_stop is null in bar_state — skipping fill", flush=True)
        return None
    stop = float(stop)
```

The rest of the fill processing (MIN_STOP_DISTANCE check, active write, signal emit) is unchanged.

---

### Task 5 — SmtV2Dispatcher._emit + orchestrator caller update [WAVE 2, DEPENDS_ON: Task 3]
**Files**: `automation/main.py` (SmtV2Dispatcher._emit), `orchestrator/main.py`
**AGENT_ROLE**: Code editor

#### 5a. SmtV2Dispatcher._emit (automation/main.py lines ~902–963)

Replace the entire `_emit` method body. New logic per signal kind:

**`new-stop-entry` / `move-stop-entry`**:
```python
stop = sig.get("stop")
if stop is None:
    print(f"[EMIT] {kind}: skipped — signal missing stop field", flush=True)
    return
direction = "long" if direction_v2 == "up" else "short"
print(f"[EMIT] {kind}: entry={sig['price']} stop={stop} direction={direction}", flush=True)
if kind == "new-stop-entry":
    _lo.place_stop_entry(direction, float(sig["price"]), float(stop))
else:
    _lo.move_stop_entry(float(sig["price"]), float(stop), direction)
```
Remove the `position = _st.load_position()` and `conf_bar` reads entirely from these branches.

**`stop-entry-filled`**:
```python
direction = "long" if direction_v2 == "up" else "short"
stop = sig.get("stop")
if stop is None:
    print(f"[EMIT] stop-entry-filled: skipped — no stop in signal", flush=True)
    return
_lo.stop_entry_filled(direction, float(stop))
```

**`market-entry`**:
```python
direction = "long" if direction_v2 == "up" else "short"
stop = sig.get("stop")
if stop is None:
    return
_lo.place_market_entry(direction, float(sig["price"]), float(stop))
```

**`market-close`**:
```python
_lo.close_position(float(sig.get("price", 0.0)), "v2-direction-mismatch")
```

**`cancel-stop-entry`**:
```python
_lo.cancel_stop_entry("cancel-stop")
```

**`stopped-out`**: no live_orders call needed (strategy already cleared position.json `active`); just print a log line.

Remove: `import smt_state as _st` if no longer used in `_emit` (check for other uses in the method first).

#### 5b. orchestrator/main.py

Line ~66: `_lo.manual_close(_fill_price, reason="session-end")` → `_lo.close_position(_fill_price, reason="session-end")`

Also update `smoke_pmt_connection.py`'s local `emit_fn` (lines ~282–306) to use `sig.get("stop")` instead of reading `confirmation_bar` from position.json, matching the new dispatcher pattern.

---

### Task 6 — trade.py: new CLI [WAVE 2, DEPENDS_ON: Tasks 2 and 3]
**File**: `trade.py` — new file
**AGENT_ROLE**: Code writer

```python
#!/usr/bin/env python
"""trade.py — Manual order CLI for the live session.

Usage:
  python trade.py up                # Market LONG  (S/L from bar_state.json)
  python trade.py up 27000          # Stop entry LONG at 27000
  python trade.py down              # Market SHORT (S/L from bar_state.json)
  python trade.py down 27000        # Stop entry SHORT at 27000
  python trade.py cancel            # Cancel unfilled stop entry
  python trade.py move 28000        # Move unfilled stop entry to 28000
  python trade.py close             # Market close active position
  python trade.py close 27000       # Set stop-loss on active position to 27000
"""
```

Implementation per command:

**`up` / `down` (no price — market entry)**:
1. Load `bar_state.json` (today); fail with message if missing.
2. stop_key = "potential_stop_long" for up, "potential_stop_short" for down.
3. stop = bar_state.get(stop_key); fail if None.
4. Print `"Market LONG/SHORT | S/L: {stop}"`.
5. Call `live_orders.place_market_entry("long"/"short", 0.0, stop)`.

**`up <price>` / `down <price>` (stop entry)**:
1. entry_price = float(argv[2]).
2. Print `"Stop entry LONG/SHORT at {entry_price} | S/L: to be set at fill"`.
3. Call `live_orders.place_stop_entry("long"/"short", entry_price, 0.0)`.
   - Note: stop_price=0.0 is a placeholder; the S/L is computed at fill time by strategy.

**`cancel`**:
1. pos = live_orders.get_position(); if pos["stop_entry"] == "": print error + sys.exit(1).
2. Print `"Cancelling stop entry at {pos['stop_entry']}"`.
3. Call `live_orders.cancel_stop_entry("user-requested")`.

**`move <price>`**:
1. pos = live_orders.get_position(); if pos["stop_entry"] == "": print error + sys.exit(1).
2. new_price = float(argv[2]).
3. direction = "long" if pos["stop_direction"] in ("up", "long") else "short".
4. Print `"Moving stop entry {pos['stop_entry']} → {new_price}"`.
5. Call `live_orders.move_stop_entry(new_price, 0.0, direction)`.
   - stop_price=0.0 placeholder: S/L computed at fill time.

**`close` (no price — market close)**:
1. pos = live_orders.get_position(); if not pos.get("active"): print error + sys.exit(1).
2. Print `"Market close | direction: {pos['active']['direction']}"`.
3. Call `live_orders.close_position(0.0, "user-requested")`.

**`close <price>` (set stop-loss)**:
1. pos = live_orders.get_position(); if not pos.get("active"): print error + sys.exit(1).
2. stop_price = float(argv[2]).
3. Print `"Setting stop-loss to {stop_price} | position: {pos['active']['direction']}"`.
4. Call `live_orders.update_stop_loss(stop_price, "user-requested")`.

**Error handling**: wrap `main()` in try/except; print exception and sys.exit(1) on failure.

**bar_state.json path**: use `smt_state.load_bar_state()` (added in Task 2).

---

## Wave 3 — Tests (DEPENDS_ON: all Wave 1 + Wave 2)

### Task 7 — Test updates and new tests [WAVE 3]
**AGENT_ROLE**: Test writer

#### 7a. Rewrite `tests/test_live_orders.py`

The existing tests reference removed function names (`place_entry`, `manual_close`, `manual_cancel_entry`, `close`, `cancel_entry`, `_pending_entry`). Rewrite fully for the new API.

Required test cases:

| Test | Description |
|------|-------------|
| `test_place_stop_entry_logs_and_syncs` | Logs kind=new-stop-entry, calls executor.place_entry with STP signal, writes stop_entry to position.json |
| `test_place_market_entry_logs_and_syncs` | Logs kind=market-entry, calls executor.place_entry with MKT signal, writes active to position.json |
| `test_move_stop_entry_reads_old_from_position` | Reads old entry_price from stop_entry field, calls executor.modify_stop_entry, updates stop_entry |
| `test_stop_entry_filled_sends_sl_and_updates_stop` | Calls executor.place_stop_after_limit_fill, updates active.stop in position.json |
| `test_cancel_stop_entry_noop_when_empty` | No executor call, no log when stop_entry="" |
| `test_cancel_stop_entry_clears_position` | Calls place_close, clears stop_entry/stop_direction/confirmation_bar |
| `test_close_position_clears_active` | Calls place_close, clears active/stop_entry/confirmation_bar |
| `test_update_stop_loss_dispatches_update_sl` | Calls place_stop_after_limit_fill with new stop, updates active.stop |
| `test_log_appends_not_overwrites` | Two _log calls → two JSONL lines (keep from existing) |
| `test_has_active_position_true_false` | Keep from existing |
| `test_has_pending_entry_true_false` | Keep from existing |
| `test_get_position_delegates` | Keep from existing |

#### 7b. New `tests/test_bar_state.py`

| Test | Description |
|------|-------------|
| `test_save_and_load_bar_state_roundtrip` | Write then read; values match |
| `test_bar_state_potential_stop_long_formula` | Given body_low=100, bar_low=80: potential_stop_long=max(80,100-15)=85 |
| `test_bar_state_potential_stop_short_formula` | Given body_high=100, bar_high=120: potential_stop_short=min(120,100+15)=115 |
| `test_bar_state_wick_cap_binds_for_long` | bar_low=98 (close to body_low=100): potential_stop_long=max(98,85)=98 |
| `test_bar_state_nulls_when_no_window` | Empty today_mnq → potential_stop_long=None, potential_stop_short=None |
| `test_bar_state_written_after_1m_bar` | Mock session_pipeline, verify file written after on_1m_bar |
| `test_load_bar_state_returns_none_when_missing` | No file → None |

#### 7c. Update `tests/test_smt_strategy_v2.py`

Add stop field assertions to existing tests:

- `test_new_opposite_5m_emits_new_limit_entry`: add `assert result["stop"] == pytest.approx(90.0)` (body_low of the bearish bar: min(105.0, 95.0) = 95.0 — verify against actual test data).
- `test_second_opposite_5m_emits_move_limit_entry`: add `assert "stop" in result` and verify the value.

Add new tests for fill fallback:

| Test | Description |
|------|-------------|
| `test_fill_uses_bar_state_when_conf_bar_empty` | stop_entry set, confirmation_bar={}, bar_state has potential_stop_long → fill fires with stop from bar_state |
| `test_fill_skips_when_conf_bar_empty_and_no_bar_state` | stop_entry set, confirmation_bar={}, no bar_state.json → returns None, no fill |

#### 7d. New `tests/test_trade_cli.py`

Use `subprocess.run(["python", "trade.py", ...], capture_output=True)` with a monkeypatched live_orders (or test via direct import with patched dependencies).

| Test | Description |
|------|-------------|
| `test_up_market_reads_bar_state` | bar_state has potential_stop_long → place_market_entry called with correct stop |
| `test_up_market_fails_no_bar_state` | No bar_state.json → exits with error |
| `test_up_market_fails_null_stop` | bar_state.potential_stop_long=null → exits with error |
| `test_up_stop_entry_places_stp` | `trade.py up 27000` → place_stop_entry("long", 27000, 0.0) |
| `test_down_market_uses_potential_stop_short` | bar_state.potential_stop_short used for short market entry |
| `test_down_stop_entry_places_stp` | `trade.py down 27000` → place_stop_entry("short", 27000, 0.0) |
| `test_cancel_noop_when_no_pending` | stop_entry="" → exits code 1 with message |
| `test_cancel_calls_cancel_stop_entry` | stop_entry set → cancel_stop_entry called |
| `test_move_fails_when_no_pending` | stop_entry="" → exits code 1 |
| `test_move_calls_move_stop_entry` | stop_entry set → move_stop_entry called with new price |
| `test_close_market_calls_close_position` | active set → close_position called |
| `test_close_market_fails_when_no_active` | active={} → exits code 1 |
| `test_close_stop_calls_update_stop_loss` | active set + price arg → update_stop_loss called |
| `test_close_stop_fails_when_no_active` | active={} → exits code 1 |

**Approach**: import `trade` module directly in tests; patch `live_orders` and `smt_state` functions with `monkeypatch`. Avoids subprocess overhead and keeps test isolation clean.

---

## Coverage Review

| Code path | Test | Status |
|---|---|---|
| `strategy.py` stop field in new-stop-entry | test_smt_strategy_v2::test_new_opposite_5m | ✅ |
| `strategy.py` stop field in move-stop-entry | test_smt_strategy_v2::test_second_opposite_5m | ✅ |
| `strategy.py` fill → bar_state path | test_smt_strategy_v2::test_fill_uses_bar_state | ✅ |
| `strategy.py` fill → no bar_state skip | test_smt_strategy_v2::test_fill_skips_no_bar_state | ✅ |
| `strategy.py` fill → conf_bar present (existing) | test_stop_side_long/short | ✅ |
| bar_state potential_stop_long formula | test_bar_state::test_potential_stop_long_formula | ✅ |
| bar_state potential_stop_short formula | test_bar_state::test_potential_stop_short_formula | ✅ |
| bar_state wick-cap binds | test_bar_state::test_wick_cap_binds | ✅ |
| bar_state null when no window | test_bar_state::test_nulls_when_no_window | ✅ |
| bar_state written after on_1m_bar | test_bar_state::test_written_after_1m_bar | ✅ |
| live_orders.place_stop_entry | test_live_orders::test_place_stop_entry_logs_and_syncs | ✅ |
| live_orders.place_market_entry | test_live_orders::test_place_market_entry_logs_and_syncs | ✅ |
| live_orders.move_stop_entry | test_live_orders::test_move_stop_entry_reads_old | ✅ |
| live_orders.stop_entry_filled | test_live_orders::test_stop_entry_filled | ✅ |
| live_orders.cancel_stop_entry noop | test_live_orders::test_cancel_noop | ✅ |
| live_orders.cancel_stop_entry active | test_live_orders::test_cancel_clears_position | ✅ |
| live_orders.close_position | test_live_orders::test_close_clears_active | ✅ |
| live_orders.update_stop_loss | test_live_orders::test_update_stop_loss | ✅ |
| Dispatcher new-stop-entry (reads stop from signal) | test_smt_v2_dispatcher (existing routing tests) | ✅ |
| trade.py up market | test_trade_cli::test_up_market_reads_bar_state | ✅ |
| trade.py up stop | test_trade_cli::test_up_stop_entry | ✅ |
| trade.py down market | test_trade_cli::test_down_market | ✅ |
| trade.py down stop | test_trade_cli::test_down_stop_entry | ✅ |
| trade.py cancel noop | test_trade_cli::test_cancel_noop | ✅ |
| trade.py cancel active | test_trade_cli::test_cancel_active | ✅ |
| trade.py move noop | test_trade_cli::test_move_noop | ✅ |
| trade.py move active | test_trade_cli::test_move_active | ✅ |
| trade.py close market | test_trade_cli::test_close_market | ✅ |
| trade.py close stop | test_trade_cli::test_close_stop | ✅ |
| trade.py close no-active | test_trade_cli::test_close_fails_no_active | ✅ |
| SimulatedBrokerExecutor.place_stop_after_limit_fill | test_live_orders::test_stop_entry_filled (paper mode) | ✅ |
| Runnability: `python trade.py --help`-style invocation | test_trade_cli::test_up_market_reads_bar_state (imports cleanly) | ✅ |
| ASCII-safe output | trade.py uses only ASCII in print statements | ✅ (by construction) |

**Gaps remaining**: None. All paths covered.

---

## Risks and Mitigations

1. **strategy.py modified in two tasks (1 + 4)**: Tasks 1 and 4 touch different line ranges (~270–275 vs 190–213). Run sequentially within the same agent or ensure no merge conflicts by carefully applying each edit to the correct location.

2. **_pending_entry removal**: Existing tests in `test_live_orders.py` directly access `live_orders._pending_entry`. Full rewrite of that test file removes those references cleanly.

3. **Dispatcher smoke test** (`smoke_pmt_connection.py`) has its own `emit_fn` mirroring the old `SmtV2Dispatcher`. Task 5 must update it, or the smoke test will silently skip orders.

4. **bar_state date alignment**: `session_pipeline.py` writes using `now.date()`. `trade.py` reads using `datetime.date.today()`. If run near midnight these could differ — acceptable given session windows don't span midnight.

---

## Test Automation Summary
- **Automated**: 31 test cases (100%) — pytest with monkeypatching and tmp_path isolation
- **Manual**: 0 — no hardware-only paths
- **Tool**: pytest
- **Run command**: `cd C:\Users\gilad\projects\auto-co-trader\auto-co-trader-main && python -m pytest tests/test_live_orders.py tests/test_bar_state.py tests/test_smt_strategy_v2.py tests/test_trade_cli.py -v`
- **Full suite**: `python -m pytest -x -q`

---

## Task Summary

| Task | Wave | File(s) | Depends on | Parallel? |
|------|-------|---------|------------|-----------|
| 1: strategy.py stop field | 1 | strategy.py | — | ✅ |
| 2: bar_state.json | 1 | smt_state.py, session_pipeline.py | — | ✅ |
| 3: live_orders.py rewrite | 1 | live_orders.py, execution/simulated.py | — | ✅ |
| 4: fill detection fallback | 2 | strategy.py | Task 2 | ✅ |
| 5: dispatcher + orchestrator | 2 | automation/main.py, orchestrator/main.py, smoke_pmt_connection.py | Task 3 | ✅ |
| 6: trade.py | 2 | trade.py | Tasks 2+3 | ✅ |
| 7: all tests | 3 | test_live_orders.py, test_bar_state.py, test_smt_strategy_v2.py, test_trade_cli.py | All | — |

**Total**: 7 tasks | 3 waves | 5 parallelizable | 2 sequential (within strategy.py must be one agent)
