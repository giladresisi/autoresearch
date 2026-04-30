# Feature: SMT Bar-by-Bar State Machine Refactor (Phase 2)

**⚠️ EXECUTION RULES — READ FIRST:**
- Implement ALL changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
- Only make code changes — no git operations

Read `train_smt.py` (full file) before making any changes.

---

## Feature Description

Refactor `run_backtest()` in `train_smt.py` from a day-loop + batch `screen_session()` architecture to
a per-bar state machine. This enables re-entry after mid-session stop-outs and a scale-invariant
progress-based stop lock-in (`BREAKEVEN_TRIGGER_PCT`). Adds `find_anchor_close()` and
`is_confirmation_bar()` as direction-agnostic replacements for the internals of `find_entry_bar()`.

## User Story

As a strategy optimizer
I want the backtest to allow re-entry within the same session after a stop-out
So that valid divergence setups are not abandoned after a single noise wick

## Problem Statement

The current day-loop architecture calls `screen_session()` once per day. After any trade closes,
the day is done — no re-entry possible even if the divergence setup is still valid. Additionally,
the `BREAKEVEN_TRIGGER_PTS` constant is scale-dependent (10 pts on a 23 pt stop vs a 120 pt stop
behave completely differently), making it ineffective as an optimizer parameter.

## Solution Statement

Replace the day-loop + batch scan with a per-bar state machine (IDLE / WAITING_FOR_ENTRY / IN_TRADE
/ REENTRY_ELIGIBLE). Add `find_anchor_close()` + `is_confirmation_bar()` as single-bar helpers.
Replace `BREAKEVEN_TRIGGER_PTS` with `BREAKEVEN_TRIGGER_PCT` (fraction of |entry − TDO| distance).

## Feature Metadata

**Feature Type**: Refactor
**Complexity**: High
**Primary Systems Affected**: `train_smt.py` (run_backtest, manage_position, two new helpers)
**Dependencies**: None (pure Python + pandas, no new libraries)
**Breaking Changes**: Yes — `screen_session()` and `_scan_bars_for_exit()` are removed; tests that
patch them must be rewritten. Output is backward-compatible when
`REENTRY_MAX_MOVE_PTS=0.0` and `BREAKEVEN_TRIGGER_PCT=0.0`.

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ BEFORE IMPLEMENTING

- `train_smt.py` L1–551 — Strategy constants + functions (editable section)
- `train_smt.py` L552+  — Frozen harness (DO NOT EDIT)
- `train_smt.py` `detect_smt_divergence()` — Unchanged; called per-bar in new loop
- `train_smt.py` `manage_position()` — Needs BREAKEVEN block replaced
- `train_smt.py` `find_entry_bar()` — Stays; its internals are extracted into new helpers
- `train_smt.py` `screen_session()` — Remove after new loop is live
- `train_smt.py` `_scan_bars_for_exit()` — Remove after new loop is live
- `tests/test_smt_strategy.py` — Unit tests for strategy functions; screen_session tests stay valid
- `tests/test_smt_backtest.py` — Integration tests; 3 tests mock screen_session and must be rewritten

### New Files to Create

None — all changes are in `train_smt.py` and `tests/test_smt_strategy.py` / `tests/test_smt_backtest.py`

### Patterns to Follow

- **Helper function placement**: new `find_anchor_close()` and `is_confirmation_bar()` go in the
  `# ══ STRATEGY FUNCTIONS ═══════════════════` block, above `screen_session()`
- **Constant placement**: new constants go in `# ══ STRATEGY TUNING ══════════════════════` block,
  ABOVE the `# DO NOT EDIT BELOW THIS LINE` boundary
- **Test pattern**: `_make_1m_bars()` helper in `test_smt_strategy.py`, `_build_*_signal_bars()` in
  `test_smt_backtest.py` — match existing synthetic bar construction style

---

## PARALLEL EXECUTION STRATEGY

### Dependency Graph

```
┌───────────────────────────────────────────────────────────────────────┐
│ WAVE 1: Helper functions + constants (fully parallel)                 │
├──────────────────────────┬────────────────────────────────────────────┤
│ Task 1.1: New constants  │ Task 1.2: find_anchor_close()              │
│ + freeze old ones        │ + is_confirmation_bar()                    │
│ Agent: implementer       │ Agent: implementer                         │
└──────────────────────────┴────────────────────────────────────────────┘
                    ↓ (both complete)
┌───────────────────────────────────────────────────────────────────────┐
│ WAVE 2: Core refactor (sequential — all in one file)                  │
├───────────────────────────────────────────────────────────────────────┤
│ Task 2.1: Update manage_position() — BREAKEVEN_TRIGGER_PCT block      │
│ Task 2.2: Refactor run_backtest() — bar-by-bar state machine          │
│ Task 2.3: Remove screen_session() + _scan_bars_for_exit()             │
└───────────────────────────────────────────────────────────────────────┘
                    ↓
┌───────────────────────────────────────────────────────────────────────┐
│ WAVE 3: Tests (can run new tests in parallel with each other)         │
├──────────────────────────┬────────────────────────────────────────────┤
│ Task 3.1: New unit tests │ Task 3.2: Update existing backtest tests   │
│ (find_anchor_close,      │ (rewrite 3 tests that patched screen_session│
│ is_confirmation_bar,     │ + add regression test, re-entry integration │
│ BREAKEVEN_TRIGGER_PCT,   │ tests)                                     │
│ state machine)           │                                            │
└──────────────────────────┴────────────────────────────────────────────┘
                    ↓
┌───────────────────────────────────────────────────────────────────────┐
│ WAVE 4: Validation + Docs (parallel)                                  │
├──────────────────────────┬────────────────────────────────────────────┤
│ Task 4.1: Full test suite│ Task 4.2: Update program_smt.md            │
│ verify no regressions    │ replace old agenda with Phase 3 targets    │
└──────────────────────────┴────────────────────────────────────────────┘
```

### Interface Contracts

**Contract 1**: Task 1.2 provides `find_anchor_close(bars, bar_idx, direction) → float | None`
and `is_confirmation_bar(bar, anchor_close, direction) → bool` → Task 2.2 calls both in the state machine.

**Contract 2**: Task 1.1 ensures `REENTRY_MAX_MOVE_PTS`, `BREAKEVEN_TRIGGER_PCT`, `MAX_HOLD_BARS`
exist as module-level constants → Task 2.1 and 2.2 reference them.

**Contract 3**: Task 2.2 must NOT call `screen_session()` or `_scan_bars_for_exit()` — Task 2.3
removes these once Task 2.2 is complete.

---

## IMPLEMENTATION PLAN

### Phase 1 — New Helper Functions + Constants

#### Task 1.1: ADD new constants + freeze deprecated ones in `train_smt.py`

**WAVE**: 1
**AGENT_ROLE**: implementer
**DEPENDS_ON**: []
**BLOCKS**: [2.1, 2.2]

**Implement**: In the `# ══ STRATEGY TUNING ══` section, above `BREAKEVEN_TRIGGER_PTS`:

Add these three constants (with their comments):

```python
# Re-entry after mid-session stop-out: allow a second entry on the same divergence.
# Measures how far price moved in the target direction from entry before the stop hit.
# For shorts: move = entry_price − exit_close. If move < threshold, the setup is still
# "loaded" and a new confirmation bar qualifies for re-entry.
# Set 0.0 to disable re-entry entirely.
# Optimizer search space: [0.0, 5.0, 10.0, 20.0, 30.0].
REENTRY_MAX_MOVE_PTS = 20.0

# Pre-TDO progress-based stop lock-in (replaces BREAKEVEN_TRIGGER_PTS).
# Fraction of |entry − TDO| price must travel before stop is moved to entry (breakeven).
# Scale-invariant: 0.65 means "65% of the way to TDO regardless of trade size."
# 0.0 = disable (stop frozen pre-TDO, matching current behaviour).
# Optimizer search space: [0.0, 0.50, 0.60, 0.65, 0.70, 0.75].
BREAKEVEN_TRIGGER_PCT = 0.0

# Maximum bars a trade may remain open after entry (0 = disabled).
# Applies per trade, including re-entries. Exits as "exit_time" at bar N+MAX_HOLD_BARS.
MAX_HOLD_BARS = 0
```

Then move `BREAKEVEN_TRIGGER_PTS` and `TRAIL_AFTER_BREAKEVEN_PTS` below the
`# DO NOT EDIT BELOW THIS LINE` boundary (alongside `RISK_PER_TRADE` / `MAX_CONTRACTS`),
with a comment: `# Deprecated — superseded by BREAKEVEN_TRIGGER_PCT. Frozen at 0 to preserve
# backward compatibility. Do not use in strategy logic.`

Set both frozen values to `0.0`.

**VALIDATE**: `python -c "import train_smt; print(train_smt.REENTRY_MAX_MOVE_PTS, train_smt.BREAKEVEN_TRIGGER_PCT)"`

---

#### Task 1.2: ADD `find_anchor_close()` and `is_confirmation_bar()` to `train_smt.py`

**WAVE**: 1
**AGENT_ROLE**: implementer
**DEPENDS_ON**: []
**BLOCKS**: [2.2, 3.1]

**Implement**: Add both functions in the `# ══ STRATEGY FUNCTIONS ═══` block, immediately
before `screen_session()`.

```python
def find_anchor_close(
    bars: pd.DataFrame,
    bar_idx: int,
    direction: str,
) -> float | None:
    """Return the close of the most recent opposite-direction bar at or before bar_idx.

    For "short" setups: looks backward for the most recent bullish bar (close > open).
    For "long"  setups: looks backward for the most recent bearish bar (close < open).

    Returns None if no qualifying bar exists before bar_idx.
    The result is stored as `anchor_close` in the pending-signal state — it is the
    reference price that a confirmation bar must pierce.
    """
    for i in range(bar_idx, -1, -1):
        bar = bars.iloc[i]
        if direction == "short" and bar["Close"] > bar["Open"]:
            return float(bar["Close"])
        if direction == "long" and bar["Close"] < bar["Open"]:
            return float(bar["Close"])
    return None


def is_confirmation_bar(
    bar: pd.Series,
    anchor_close: float,
    direction: str,
) -> bool:
    """Return True if `bar` qualifies as a signal confirmation candle.

    For "short": bar is bearish (close < open) AND high > anchor_close.
    For "long":  bar is bullish (close > open) AND low  < anchor_close.

    This is a single-bar check — the caller iterates bars and calls this each time.
    Replaces the forward scan loop in find_entry_bar().
    """
    if direction == "short":
        return bar["Close"] < bar["Open"] and bar["High"] > anchor_close
    else:  # "long"
        return bar["Close"] > bar["Open"] and bar["Low"] < anchor_close
```

**VALIDATE**: `python -c "import train_smt; print(train_smt.find_anchor_close.__doc__[:30])"`

---

### Phase 2 — Core Refactor

#### Task 2.1: UPDATE `manage_position()` — replace BREAKEVEN block with BREAKEVEN_TRIGGER_PCT

**WAVE**: 2
**AGENT_ROLE**: implementer
**DEPENDS_ON**: [1.1]

**Implement**: In `manage_position()`, find the block:
```python
if BREAKEVEN_TRIGGER_PTS > 0 and not position.get("tp_breached"):
    ...
```
Replace entirely with:
```python
if BREAKEVEN_TRIGGER_PCT > 0 and not position.get("tp_breached"):
    tdo_dist = abs(entry_price - tp)
    if tdo_dist > 0:
        if direction == "short":
            progress = (entry_price - current_bar["Low"]) / tdo_dist
        else:
            progress = (current_bar["High"] - entry_price) / tdo_dist
        if progress >= BREAKEVEN_TRIGGER_PCT:
            # Only tighten the stop, never widen it
            if direction == "short":
                position["stop_price"] = min(position["stop_price"], entry_price)
            else:
                position["stop_price"] = max(position["stop_price"], entry_price)
            position["breakeven_active"] = True
```

No other changes to `manage_position()`.

**VALIDATE**: `python -c "import train_smt; print('manage_position ok')" && python -m pytest tests/test_smt_strategy.py -k "manage_position" -q`

---

#### Task 2.2: REFACTOR `run_backtest()` — bar-by-bar state machine

**WAVE**: 2
**AGENT_ROLE**: implementer
**DEPENDS_ON**: [1.1, 1.2, 2.1]

This is the core change. Replace the inner logic of `run_backtest()` entirely.

**State variables** (reset per day):
```python
state = "IDLE"           # "IDLE" | "WAITING_FOR_ENTRY" | "IN_TRADE" | "REENTRY_ELIGIBLE"
pending_direction = None
anchor_close = None
position = None          # carries across days for the rare overnight case (keep existing behavior)
entry_bar_count = 0      # bars since entry, for MAX_HOLD_BARS
```

**Per-session loop structure** (inside the existing `for day in trading_days:` loop):

```python
for bar_idx, (ts, bar) in enumerate(mnq_session.iterrows()):
    mes_bar = mes_session.iloc[bar_idx] if bar_idx < len(mes_session) else None

    if state == "IN_TRADE":
        entry_bar_count += 1
        result = manage_position(position, bar)

        # MAX_HOLD_BARS time-based exit
        if MAX_HOLD_BARS > 0 and entry_bar_count >= MAX_HOLD_BARS and result == "hold":
            result = "exit_time"

        # Session-end forced close (bar end >= session_end_ts)
        bar_end = mnq_session.index[bar_idx + 1] if bar_idx + 1 < len(mnq_session) else session_end_ts
        if bar_end >= session_end_ts and result == "hold":
            result = "session_close"

        if result != "hold":
            trade, day_pnl_delta = _build_trade_record(position, result, bar, MNQ_PNL_PER_POINT)
            trades.append(trade)
            day_pnl += day_pnl_delta

            # Re-entry eligibility
            if REENTRY_MAX_MOVE_PTS > 0 and result in ("exit_stop", "exit_time"):
                if position.get("breakeven_active"):
                    # Breakeven stop: price never really moved — always eligible
                    state = "REENTRY_ELIGIBLE"
                    anchor_close = float(bar["Close"])
                else:
                    # Measure favorable move from entry to exit
                    if position["direction"] == "short":
                        move = position["entry_price"] - float(bar["Close"])
                    else:
                        move = float(bar["Close"]) - position["entry_price"]
                    if move < REENTRY_MAX_MOVE_PTS:
                        state = "REENTRY_ELIGIBLE"
                        anchor_close = float(bar["Close"])
                    else:
                        state = "IDLE"
            else:
                state = "IDLE"

            pending_direction = position["direction"] if state == "REENTRY_ELIGIBLE" else None
            position = None
            entry_bar_count = 0

    elif state == "WAITING_FOR_ENTRY":
        if anchor_close is not None and is_confirmation_bar(bar, anchor_close, pending_direction):
            signal = _build_signal_from_bar(bar, ts, pending_direction, day_tdo)
            if signal is not None:
                risk_per_contract = abs(signal["entry_price"] - signal["stop_price"]) * MNQ_PNL_PER_POINT
                contracts = (
                    min(MAX_CONTRACTS, max(1, int(RISK_PER_TRADE / risk_per_contract)))
                    if risk_per_contract > 0 else 1
                )
                position = {**signal, "entry_date": day, "contracts": contracts}
                state = "IN_TRADE"
                entry_bar_count = 0
        # If no confirmation by session end, stays WAITING until next day resets to IDLE

    elif state == "REENTRY_ELIGIBLE":
        if anchor_close is not None and is_confirmation_bar(bar, anchor_close, pending_direction):
            signal = _build_signal_from_bar(bar, ts, pending_direction, day_tdo)
            if signal is not None:
                risk_per_contract = abs(signal["entry_price"] - signal["stop_price"]) * MNQ_PNL_PER_POINT
                contracts = (
                    min(MAX_CONTRACTS, max(1, int(RISK_PER_TRADE / risk_per_contract)))
                    if risk_per_contract > 0 else 1
                )
                position = {**signal, "entry_date": day, "contracts": contracts}
                state = "IN_TRADE"
                entry_bar_count = 0

    else:  # IDLE
        if mes_bar is None:
            continue
        if mnq_session.index[bar_idx] < min_signal_ts:
            continue

        # Weekday + blackout filters are already applied at the day level;
        # apply blackout at bar level here too (matches screen_session behavior)
        if SIGNAL_BLACKOUT_START and SIGNAL_BLACKOUT_END:
            t = ts.strftime("%H:%M")
            if SIGNAL_BLACKOUT_START <= t < SIGNAL_BLACKOUT_END:
                continue

        direction = detect_smt_divergence(
            mes_session.reset_index(drop=True),
            mnq_session.reset_index(drop=True),
            bar_idx,
            0,
        )
        if direction is None:
            continue
        if TRADE_DIRECTION != "both" and direction != TRADE_DIRECTION:
            continue

        ac = find_anchor_close(mnq_session.reset_index(drop=True), bar_idx, direction)
        if ac is None:
            continue
        pending_direction = direction
        anchor_close = ac
        state = "WAITING_FOR_ENTRY"

# End of session loop: force-close any open position
if state == "IN_TRADE" and position is not None:
    last_bar = mnq_session.iloc[-1]
    trade, day_pnl_delta = _build_trade_record(position, "session_close", last_bar, MNQ_PNL_PER_POINT)
    trades.append(trade)
    day_pnl += day_pnl_delta
    position = None
    state = "IDLE"
    pending_direction = None
    anchor_close = None

# Reset pending state at day boundary (divergence signals don't carry across days)
state = "IDLE"
pending_direction = None
anchor_close = None
```

**Also add** a private helper `_build_signal_from_bar()` (above `run_backtest`, below `is_confirmation_bar`):

```python
def _build_signal_from_bar(
    bar: pd.Series,
    ts: "pd.Timestamp",
    direction: str,
    tdo: float,
) -> dict | None:
    """Build a signal dict from a confirmed entry bar, applying all validity guards.

    Returns None if the signal fails TDO_VALIDITY_CHECK, MIN_STOP_POINTS, or
    MIN_TDO_DISTANCE_PTS guards. This mirrors the guard logic in the old screen_session().
    """
    entry_price = float(bar["Close"])

    if TDO_VALIDITY_CHECK:
        if direction == "long" and tdo <= entry_price:
            return None
        if direction == "short" and tdo >= entry_price:
            return None

    distance_to_tdo = abs(entry_price - tdo)
    if MIN_TDO_DISTANCE_PTS > 0 and distance_to_tdo < MIN_TDO_DISTANCE_PTS:
        return None

    stop_ratio = SHORT_STOP_RATIO if direction == "short" else LONG_STOP_RATIO
    if direction == "short":
        stop_price = entry_price + stop_ratio * distance_to_tdo
    else:
        stop_price = entry_price - stop_ratio * distance_to_tdo

    if MIN_STOP_POINTS > 0 and abs(entry_price - stop_price) < MIN_STOP_POINTS:
        return None

    return {
        "direction":      direction,
        "entry_price":    entry_price,
        "entry_time":     ts,
        "take_profit":    tdo,
        "stop_price":     round(stop_price, 4),
        "tdo":            tdo,
        "divergence_bar": -1,   # not tracked in bar-loop mode
        "entry_bar":      -1,
    }
```

Note: `min_signal_ts` and `day_tdo` are computed at the top of the day loop, same as before.
`mes_session` and `mnq_session` are pre-sliced and reset in the same way.

Pre-compute reset-index views once per session (outside the bar loop):
```python
mes_reset = mes_session.reset_index(drop=True)
mnq_reset = mnq_session.reset_index(drop=True)
```
Then pass `mes_reset`/`mnq_reset` to `detect_smt_divergence` and `find_anchor_close` inside the loop.

**VALIDATE**: `python -m pytest tests/test_smt_backtest.py -q`

---

#### Task 2.3: REMOVE `screen_session()` and `_scan_bars_for_exit()` from `train_smt.py`

**WAVE**: 2
**AGENT_ROLE**: implementer
**DEPENDS_ON**: [2.2]

Delete both functions entirely. Do not leave dead code or wrapper stubs.

Confirm no other code in `train_smt.py` calls either function after removal.
Note: `tests/test_smt_strategy.py` has `screen_session` tests — these must be updated in Task 3.2.

**VALIDATE**: `python -c "import train_smt; assert not hasattr(train_smt, 'screen_session'); print('ok')"`
(Run this check only AFTER Task 3.2 updates the tests.)

---

### Phase 3 — Tests

#### Task 3.1: ADD new unit tests to `tests/test_smt_strategy.py`

**WAVE**: 3
**AGENT_ROLE**: test-writer
**DEPENDS_ON**: [2.2, 2.3]

Add the following test classes/functions at the end of `test_smt_strategy.py`:

**find_anchor_close tests:**
- `test_find_anchor_close_short_finds_bull_bar`: session bars where bar 2 is bullish; call at bar 4 → returns bar 2's close
- `test_find_anchor_close_long_finds_bear_bar`: session bars where bar 2 is bearish; call at bar 4 → returns bar 2's close
- `test_find_anchor_close_no_match_returns_none`: all doji bars → returns None
- `test_find_anchor_close_uses_most_recent`: two bullish bars; returns the later one's close

**is_confirmation_bar tests:**
- `test_is_confirmation_bar_short_true`: bearish bar (close<open) + high > anchor_close → True
- `test_is_confirmation_bar_short_false_not_bearish`: bullish bar → False even if high > anchor
- `test_is_confirmation_bar_short_false_wick_below_anchor`: bearish bar + high <= anchor_close → False
- `test_is_confirmation_bar_long_true`: bullish bar (close>open) + low < anchor_close → True
- `test_is_confirmation_bar_long_false_not_bullish`: bearish bar → False
- `test_is_confirmation_bar_long_false_wick_above_anchor`: bullish bar + low >= anchor_close → False

**manage_position BREAKEVEN_TRIGGER_PCT tests (monkeypatch BREAKEVEN_TRIGGER_PCT):**
- `test_breakeven_trigger_pct_fires_at_correct_progress`: short trade, set BREAKEVEN_TRIGGER_PCT=0.5; feed bar where progress = 0.5 → stop moves to entry_price, breakeven_active=True
- `test_breakeven_trigger_pct_does_not_fire_below_threshold`: progress = 0.4 with threshold 0.5 → stop unchanged
- `test_breakeven_trigger_pct_zero_disables_mechanism`: BREAKEVEN_TRIGGER_PCT=0.0 → stop never moves
- `test_breakeven_active_flag_set`: after trigger fires, position["breakeven_active"] is True
- `test_breakeven_stop_only_tightens`: if stop is already tighter than entry_price, doesn't widen

**State machine / re-entry integration tests (using run_backtest on synthetic multi-bar data):**
- `test_reentry_after_stop`: synthetic session where trade stops out early (move < REENTRY_MAX_MOVE_PTS), then a second confirmation bar fires → 2 trades recorded in 1 day
- `test_no_reentry_when_disabled`: REENTRY_MAX_MOVE_PTS=0.0, same session → only 1 trade
- `test_no_reentry_when_move_exceeds_threshold`: stop-out after large move → no second trade
- `test_reentry_breakeven_active_bypasses_move_check`: trade stopped at breakeven → REENTRY_ELIGIBLE regardless of move
- `test_state_resets_at_day_boundary`: pending divergence from day 1 does NOT carry to day 2

**VALIDATE**: `python -m pytest tests/test_smt_strategy.py -q`

---

#### Task 3.2: UPDATE `tests/test_smt_backtest.py` — rewrite screen_session-dependent tests + add regression test

**WAVE**: 3
**AGENT_ROLE**: test-writer
**DEPENDS_ON**: [2.2, 2.3]

**Tests to rewrite** (3 tests currently mock `screen_session` — they must be redesigned since the function no longer exists):

1. **`test_run_backtest_session_force_exit`**: Build synthetic bars where a short signal fires early in the session (bars 7–8) and the TP is 10,000 pts away and stop is 10,000 pts away (both impossible to hit). The session end forces closure. Assert `exit_type_breakdown` contains `"session_close"`. Do NOT patch `screen_session`.

2. **`test_run_backtest_end_of_backtest_exit`**: Build one-day synthetic data where entry fires and TP/stop are both 50,000 pts away. Run backtest with end=day+1 so the end-of-backtest logic fires. Assert `"total_trades" in stats` and `"total_pnl" in stats`.

3. **`test_one_trade_per_day_max`**: Build synthetic session bars that produce exactly one signal. Assert `stats["total_trades"] <= 1`. No patching needed.

**Regression test to add:**

```python
def test_regression_no_reentry_matches_legacy_behavior(futures_tmpdir, monkeypatch):
    """With REENTRY_MAX_MOVE_PTS=0 and BREAKEVEN_TRIGGER_PCT=0, run_backtest output
    matches expected trades from a known synthetic dataset (golden reference).

    This test ensures the refactor does not alter backtest results when re-entry
    and breakeven are both disabled — the new loop must be semantically equivalent
    to the old screen_session + _scan_bars_for_exit architecture.
    """
    import train_smt
    monkeypatch.setattr(train_smt, "REENTRY_MAX_MOVE_PTS", 0.0)
    monkeypatch.setattr(train_smt, "BREAKEVEN_TRIGGER_PCT", 0.0)
    monkeypatch.setattr(train_smt, "MIN_STOP_POINTS", 0.0)
    monkeypatch.setattr(train_smt, "MIN_TDO_DISTANCE_PTS", 0.0)
    monkeypatch.setattr(train_smt, "TDO_VALIDITY_CHECK", False)
    monkeypatch.setattr(train_smt, "TRADE_DIRECTION", "short")
    monkeypatch.setattr(train_smt, "TRAIL_AFTER_TP_PTS", 0.0)
    monkeypatch.setattr(train_smt, "SIGNAL_BLACKOUT_START", "")
    monkeypatch.setattr(train_smt, "SIGNAL_BLACKOUT_END", "")
    monkeypatch.setattr(train_smt, "ALLOWED_WEEKDAYS", frozenset({0,1,2,3,4}))

    mnq, mes = _build_short_signal_bars("2025-01-02")
    stats = train_smt.run_backtest(mnq, mes, start="2025-01-02", end="2025-01-03")

    assert stats["total_trades"] == 1
    assert len(stats["trade_records"]) == 1
    assert stats["trade_records"][0]["direction"] == "short"
```

**Also update** `test_screen_session_*` tests in `test_smt_strategy.py` that call `train_smt.screen_session(...)` directly — since `screen_session` is removed:

For each such test, replace the `screen_session` call with a direct call to the equivalent helpers (`find_anchor_close` + `is_confirmation_bar` + `_build_signal_from_bar`) OR delete the test if it is superseded by a new test added in Task 3.1.

Do NOT delete tests for guards (`TDO_VALIDITY_CHECK`, `MIN_STOP_POINTS`, `MIN_TDO_DISTANCE_PTS`, direction filter, blackout) — these guards now live in `_build_signal_from_bar()`. Rewrite those tests to call `_build_signal_from_bar()` directly.

**VALIDATE**: `python -m pytest tests/test_smt_strategy.py tests/test_smt_backtest.py -q`

---

### Phase 4 — Validation + program_smt.md Update

#### Task 4.1: Run full test suite + smoke-test train_smt.py

**WAVE**: 4
**AGENT_ROLE**: validator
**DEPENDS_ON**: [3.1, 3.2]

```bash
cd C:\Users\gilad\projects\auto-co-trader\auto-co-trader-main
python -m pytest tests/ -q --tb=short 2>&1 | tail -20
```

Verify:
- All pre-existing passing tests still pass
- All new tests pass
- `python train_smt.py` runs without error (requires real data in place)

---

#### Task 4.2: UPDATE `program_smt.md` — replace old optimization agenda with Phase 3 targets

**WAVE**: 4
**AGENT_ROLE**: implementer
**DEPENDS_ON**: [2.1, 2.2, 2.3]

The old agenda in `program_smt.md` targeted constants and functions that have already been resolved
or are now superseded. Replace the entire **Tunable Constants**, **Strategy Functions**, and
**Optimization Agenda** sections with the Phase 3 targets.

**Update `## Editable Section` → `### Tunable Constants`:**

Replace the old constant list with the post-Phase-2 set. Remove `BREAKEVEN_TRIGGER_PTS` and
`TRAIL_AFTER_BREAKEVEN_PTS` (now frozen below the boundary). Add the three new constants.
Final list:

```
- SESSION_START / SESSION_END — kill zone window (currently "09:00"–"13:30" ET)
- MIN_BARS_BEFORE_SIGNAL — wall-clock minutes before divergence can fire (default 0)
- TRADE_DIRECTION — frozen at "short" (longs structurally lose across 5/6 folds)
- SHORT_STOP_RATIO — fraction of |entry − TDO| for short stop (current 0.40)
- LONG_STOP_RATIO — frozen at 0.05 (longs disabled)
- MIN_STOP_POINTS — minimum stop distance in MNQ points (current 2.5)
- MIN_TDO_DISTANCE_PTS — minimum |entry − TDO| filter (current 50.0)
- ALLOWED_WEEKDAYS — weekdays eligible for trading; Thursday excluded (frozenset({0,1,2,4}))
- SIGNAL_BLACKOUT_START / SIGNAL_BLACKOUT_END — entry suppression window (current "11:00"–"13:30")
- TRAIL_AFTER_TP_PTS — trail stop past TDO (current 1.0)
- REENTRY_MAX_MOVE_PTS — max favorable move before re-entry is disallowed (default 20.0)
- BREAKEVEN_TRIGGER_PCT — fraction of |entry − TDO| before stop locks to entry (default 0.0)
- MAX_HOLD_BARS — time-based exit N bars after entry (default 0 = disabled)
- MNQ_PNL_PER_POINT — do NOT change (2.0)
- RISK_PER_TRADE — do NOT change (50.0)
```

**Update `### Strategy Functions`:**

Remove `screen_session()` and `_scan_bars_for_exit()` (deleted).
Add the new helpers:

```
- detect_smt_divergence() — signal detection logic; unchanged
- find_anchor_close() — finds most recent opposite-direction bar's close at divergence bar
- is_confirmation_bar() — single-bar confirmation check; replaces find_entry_bar() forward scan
- find_entry_bar() — still present; used by existing tests
- _build_signal_from_bar() — applies TDO_VALIDITY_CHECK / MIN_STOP_POINTS / MIN_TDO_DISTANCE_PTS guards
- compute_tdo() — True Day Open (9:30 AM ET bar; first-bar proxy fallback); unchanged
- manage_position() — bar-by-bar exit; BREAKEVEN_TRIGGER_PCT replaces BREAKEVEN_TRIGGER_PTS
- run_backtest() — per-bar state machine; four states: IDLE / WAITING_FOR_ENTRY / IN_TRADE / REENTRY_ELIGIBLE
```

**Update `### Forbidden Changes`:**

Add: "Do NOT call `screen_session()` or `_scan_bars_for_exit()` — both removed."
Add: "Do NOT modify `BREAKEVEN_TRIGGER_PTS` or `TRAIL_AFTER_BREAKEVEN_PTS` — frozen below boundary."

**Replace `## Optimization Agenda` entirely** with Phase 3 priorities:

```markdown
## Optimization Agenda

Work through priorities in order.

**Baseline:** SHORT_STOP_RATIO=0.40, MIN_TDO_DISTANCE_PTS=50,
SIGNAL_BLACKOUT=11:00–13:30, Thursday excluded, TRAIL_AFTER_TP_PTS=1.0,
REENTRY_MAX_MOVE_PTS=20.0, BREAKEVEN_TRIGGER_PCT=0.0.

### Priority 1 — Re-entry threshold (HIGHEST PRIORITY)
Grid search: REENTRY_MAX_MOVE_PTS ∈ [0.0, 5.0, 10.0, 20.0, 30.0]
Optimise for: mean_test_pnl (primary), total_test_trades (secondary).

### Priority 2 — Pre-TDO progress stop lock-in
Grid search: BREAKEVEN_TRIGGER_PCT ∈ [0.0, 0.50, 0.60, 0.65, 0.70, 0.75]
Optimise for: mean_test_pnl (primary), reduction in max_drawdown (secondary).

### Priority 3 — Time-based stop (MAX_HOLD_BARS)
Grid search: MAX_HOLD_BARS ∈ [0, 30, 60, 90, 120] (bars at 5m resolution)
Optimise for: total_test_trades freed and mean_test_pnl.

### Priority 4 — Fine-tune MIN_TDO_DISTANCE_PTS
Grid search: [25, 40, 50, 65]
Optimise for: avg_pnl_per_trade (primary), total_test_trades ≥ 8/fold (secondary).

### Priority 5 — TRAIL_AFTER_TP_PTS fine-tune
Grid search: [0.0, 1.0, 5.0, 10.0, 20.0]
Optimise for: avg PnL on exit_tp + post-TDO session_close trades.

### Priority 6 — Intermediate TP (INTERMEDIATE_TP_RATIO)
Add INTERMEDIATE_TP_RATIO ∈ [0.0, 0.3, 0.5, 0.7] — partial exit before TDO.
Only implement if Priorities 1–5 leave residual session-close drag.
```

**VALIDATE**: `python -c "open('program_smt.md').read()" && echo "program_smt.md readable"`

---

## TESTING STRATEGY

### Unit Tests

**Status**: ✅ Automated | **Tool**: pytest | **Location**: `tests/test_smt_strategy.py` | **Run**: `python -m pytest tests/test_smt_strategy.py -q`

- `find_anchor_close()` — 4 tests (finds recent opposite bar, returns None on no match, uses most recent) ✅
- `is_confirmation_bar()` — 6 tests (short/long true and false cases, wick vs body check) ✅
- `manage_position()` BREAKEVEN_TRIGGER_PCT — 5 tests (fires at threshold, doesn't fire below, disabled, flag set, only tightens) ✅

### Integration Tests

**Status**: ✅ Automated | **Tool**: pytest | **Location**: `tests/test_smt_backtest.py` | **Run**: `python -m pytest tests/test_smt_backtest.py -q`

- Re-entry: 2 trades in 1 day (with and without REENTRY_MAX_MOVE_PTS) ✅
- No re-entry when move exceeds threshold ✅
- Breakeven bypass of move check ✅
- Day boundary state reset ✅
- Regression: 0-reentry + 0-breakeven matches known single-trade output ✅
- Rewritten: session_force_exit, end_of_backtest_exit, one_trade_per_day_max (no longer use screen_session patch) ✅

### Edge Cases

- **Empty DataFrame**: no bars → 0 trades, no crash — ✅ `test_run_backtest_empty_data_returns_zero_trades`
- **No divergence in session**: `detect_smt_divergence` returns None every bar → IDLE throughout → 0 trades ✅
- **Anchor close is None**: `find_anchor_close` returns None → signal skipped, state stays IDLE ✅
- **Re-entry signal fails guard**: `_build_signal_from_bar` returns None for re-entry bar → state goes to IDLE ✅
- **MAX_HOLD_BARS fires**: position exits after N bars regardless of TP/stop ✅
- **Thursday filter + re-entry**: weekday check at day level, not bar level — no interaction ✅

### Test Automation Summary

| | Count | % |
|---|---|---|
| ✅ Backend unit (pytest) | 15 new | — |
| ✅ Backend integration (pytest) | 8 new / 3 rewritten | — |
| ⚠️ Manual | 0 | — |
| **Total new** | **23** | 100% |

---

## VALIDATION COMMANDS

### Level 1: Syntax
```bash
python -c "import train_smt; print('import ok')"
```

### Level 2: New constants accessible
```bash
python -c "import train_smt; print(train_smt.REENTRY_MAX_MOVE_PTS, train_smt.BREAKEVEN_TRIGGER_PCT, train_smt.MAX_HOLD_BARS)"
```

### Level 3: Unit tests
```bash
python -m pytest tests/test_smt_strategy.py -q --tb=short
```

### Level 4: Integration tests
```bash
python -m pytest tests/test_smt_backtest.py -q --tb=short
```

### Level 5: Full suite
```bash
python -m pytest tests/ -q --tb=short
```

### Level 6: Backtest smoke (requires real data)
```bash
python train_smt.py 2>&1 | tail -5
```

Expected: outputs fold metrics and holdout line, no exceptions.

---

## ACCEPTANCE CRITERIA

- [ ] `find_anchor_close(bars, bar_idx, direction)` implemented and tested (4 unit tests pass)
- [ ] `is_confirmation_bar(bar, anchor_close, direction)` implemented and tested (6 unit tests pass)
- [ ] `_build_signal_from_bar()` implemented with all guard logic from old `screen_session()`
- [ ] `manage_position()` uses `BREAKEVEN_TRIGGER_PCT` (not `BREAKEVEN_TRIGGER_PTS`); 5 unit tests pass
- [ ] `run_backtest()` uses per-bar state machine with 4 states; no call to `screen_session()` or `_scan_bars_for_exit()`
- [ ] Re-entry fires a second trade when `REENTRY_MAX_MOVE_PTS > 0` and stop-out move < threshold
- [ ] Re-entry does NOT fire when `REENTRY_MAX_MOVE_PTS = 0.0`
- [ ] `BREAKEVEN_TRIGGER_PTS` and `TRAIL_AFTER_BREAKEVEN_PTS` are frozen below DO NOT EDIT boundary
- [ ] `REENTRY_MAX_MOVE_PTS`, `BREAKEVEN_TRIGGER_PCT`, `MAX_HOLD_BARS` exist as tunable constants
- [ ] `screen_session()` removed; `_scan_bars_for_exit()` removed
- [ ] Regression test passes: with re-entry + breakeven disabled, 1 known synthetic trade produced correctly
- [ ] All pre-existing passing tests still pass (no regressions)
- [ ] `python train_smt.py` runs end-to-end without error
- [ ] `program_smt.md` Tunable Constants updated (no BREAKEVEN_TRIGGER_PTS; REENTRY_MAX_MOVE_PTS, BREAKEVEN_TRIGGER_PCT, MAX_HOLD_BARS present)
- [ ] `program_smt.md` Strategy Functions updated (screen_session/scan_bars removed; new helpers added)
- [ ] `program_smt.md` Optimization Agenda replaced with Phase 3 priorities

---

## COMPLETION CHECKLIST

- [ ] Task 1.1 complete (constants added, old ones frozen)
- [ ] Task 1.2 complete (find_anchor_close + is_confirmation_bar added)
- [ ] Task 2.1 complete (manage_position BREAKEVEN_TRIGGER_PCT)
- [ ] Task 2.2 complete (run_backtest bar-loop + _build_signal_from_bar)
- [ ] Task 2.3 complete (screen_session + _scan_bars_for_exit removed)
- [ ] Task 3.1 complete (new unit tests added and passing)
- [ ] Task 3.2 complete (backtest tests rewritten + regression test added)
- [ ] Task 4.1 complete (full suite green)
- [ ] Task 4.2 complete (program_smt.md agenda updated)
- [ ] `python -c "import train_smt"` succeeds
- [ ] **⚠️ Debug logs added during execution REMOVED (keep pre-existing)**
- [ ] **⚠️ CRITICAL: Changes UNSTAGED — NOT committed**

---

## NOTES

### Why `_build_signal_from_bar` is needed

In `screen_session()`, the guard logic (TDO_VALIDITY_CHECK, MIN_STOP_POINTS, MIN_TDO_DISTANCE_PTS,
stop placement) was embedded after `find_entry_bar()` returned. In the new bar loop,
confirmation is detected via `is_confirmation_bar()` which has no access to TDO. A separate
function receives `(bar, ts, direction, tdo)` and applies all guards, returning either a
signal dict or None. This keeps the bar loop clean.

### State resets at day boundary

`WAITING_FOR_ENTRY` and `REENTRY_ELIGIBLE` states do NOT carry across days. A divergence signal
is session-scoped. At the top of each new day's session loop, reset:
`state = "IDLE"; pending_direction = None; anchor_close = None`

Positions can carry across days (overnight holds) — this matches legacy behavior.

### mes_session alignment for detect_smt_divergence

Pre-compute `mes_reset = mes_session.reset_index(drop=True)` and `mnq_reset = mnq_session.reset_index(drop=True)`
once at the top of the session loop (outside the bar loop) to avoid repeated resets.

### backward scan in IDLE

`detect_smt_divergence` already computes session high/low from a slice starting at 0. In the new
loop, pass `session_start_idx=0` and `bar_idx=bar_idx` (position within the session slice) —
same as the old `screen_session` did.

### _build_signal_from_bar and divergence_bar / entry_bar fields

These fields are set to `-1` in the new architecture (they were internal implementation details
of the old `find_entry_bar` forward scan). The fields are preserved in the trade record for
schema compatibility but carry no semantic meaning in the bar-loop mode.
