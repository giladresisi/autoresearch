# Plan: SMT Limit Entry at Anchor Close

**Status**: ✅ Planned
**Complexity**: ⚠️ Medium
**Started**: 2026-04-21
**Plan File**: `.agents/plans/smt-limit-entry-anchor-close.md`

## Summary

Replaces the current market-order entry (bar close) with a limit order at `anchor_close` (the
price the confirmation bar must break through). Adds a `WAITING_FOR_LIMIT_FILL` state to the
backtest state machine. Bar resolution is auto-detected from timestamps so the same logic works
at 1m (backtest) and 1s (live signal) granularity.

Three operating modes, all disabled by default:

- **Same-bar fill** (`LIMIT_ENTRY_BUFFER_PTS` set, `LIMIT_EXPIRY_SECONDS = None`): entry_price =
  anchor_close ± buffer, immediate — no state machine change. Entry improvement guaranteed on
  every trade; no missed-trade risk.
- **Forward-looking limit** (`LIMIT_ENTRY_BUFFER_PTS` set, `LIMIT_EXPIRY_SECONDS > 0`): limit
  placed after confirmation bar closes; fills on subsequent bars if price retraces to the level.
  Acts as a noise filter but may miss big immediate movers.
- **Hybrid** (`LIMIT_ENTRY_BUFFER_PTS` set, `LIMIT_EXPIRY_SECONDS > 0`, `LIMIT_RATIO_THRESHOLD`
  set): high-conviction bars (`entry_bar_body_ratio >= LIMIT_RATIO_THRESHOLD`) skip the wait
  window and use same-bar fill; low-conviction bars fall through to the forward-looking limit.

Adds four diagnostic fields to `trades.tsv` and a new `limit_expired` exit type so missed trades
are fully visible and analysable.

## User Story

As a backtester, I want entry fills to reflect a realistic limit order at the anchor level so that
slippage is reduced, noise entries are filtered, and missed-mover analysis is possible without
re-running the backtest.

## Acceptance Criteria

- [ ] `LIMIT_ENTRY_BUFFER_PTS = None` (default) produces **identical** output to the pre-change
  baseline — no trades changed, no new rows, no new columns populated.
- [ ] With `LIMIT_ENTRY_BUFFER_PTS = 0.0, LIMIT_EXPIRY_SECONDS = None` (same-bar fill): every
  trade's `entry_price` equals `anchor_close` (SHORT) or `anchor_close` (LONG); no trades skipped.
- [ ] With `LIMIT_ENTRY_BUFFER_PTS = 0.5, LIMIT_EXPIRY_SECONDS = None`: SHORT entry = anchor_close
  − 0.5; LONG entry = anchor_close + 0.5.
- [ ] With `LIMIT_EXPIRY_SECONDS = 120.0` and 1m bars: the limit window is `max(1, round(120/60))` = 2
  bars; signals not filled within 2 bars produce a `limit_expired` row in `trades.tsv` with `pnl = 0`.
- [ ] `bar_seconds` is computed from the session bar index at the start of each day and correctly
  converts `LIMIT_EXPIRY_SECONDS` to bars for any resolution.
- [ ] `anchor_close_price` is populated on every trade when limit entry is enabled; `None` when
  disabled.
- [ ] `limit_fill_bars` is 0 for same-bar-fill trades, ≥ 1 for forward-filled trades, `None` for
  non-limit trades and `limit_expired` rows.
- [ ] `missed_move_pts` is populated on `limit_expired` rows (≥ 0, representing the maximum
  favourable move during the wait window); `None` on all other rows.
- [ ] Session end during `WAITING_FOR_LIMIT_FILL` writes a `limit_expired` record (session_close
  variant) rather than silently discarding the signal.
- [ ] The `screen_session` live-trading shim receives `anchor_close=ac` and returns a signal
  with `entry_price = anchor_close ± buffer` when limit entry is enabled.
- [ ] With `LIMIT_RATIO_THRESHOLD=0.60`: confirmation bars with `body_ratio >= 0.60` use
  same-bar fill (`limit_fill_bars=0`); bars with `body_ratio < 0.60` enter `WAITING_FOR_LIMIT_FILL`.
- [ ] ≥ 19 new automated tests; all pre-existing tests pass.

## Execution Agent Rules

- Make ALL code changes required by the plan.
- Delete debug logs added during execution (keep pre-existing ones).
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`.

---

## Codebase Context

| File | Role |
|---|---|
| `strategy_smt.py` | Config constants (top ~200 lines); `_build_signal_from_bar()` (~L1165); `screen_session()` shim (~L1120); `find_anchor_close()` / `is_confirmation_bar()` (~L724–763) |
| `backtest_smt.py` | Session loop state machine (~L250–960); `_build_trade_record()` (~L119–207); `anchor_close` state var already exists; both `_build_signal_from_bar` call sites (~L614, ~L715) |
| `signal_smt.py` | Calls `screen_session()` — no direct `_build_signal_from_bar` call; no changes needed |
| `tests/test_smt_strategy.py` | Unit tests for `_build_signal_from_bar` and helpers |
| `tests/test_smt_backtest.py` | Integration tests for the backtest harness |

### Key invariants

- `is_confirmation_bar` (SHORT) requires `bar["High"] > anchor_close` AND `bar["Close"] < bar["Open"]`.
  This guarantees the confirmation bar traded THROUGH anchor_close on its way down, making
  same-bar fill a valid simulation (price was at anchor_close before the bar closed).
- `anchor_close` is already a live state variable in the session loop; it is set on divergence
  detection and cleared on IDLE transitions. Both call sites have it in scope.
- The two `_build_signal_from_bar` call sites are structurally identical; both need the same
  `anchor_close=anchor_close` addition.

---

## Design

### New constants (`strategy_smt.py`)

```python
# Limit entry at anchor_close instead of confirmation bar's close.
# None = disabled (entry = bar["Close"], existing behaviour).
# 0.0  = exactly at anchor_close.
# >0   = anchor_close offset by buffer (SHORT: −buffer; LONG: +buffer).
# Optimizer search space: [None, 0.0, 0.25, 0.5, 1.0]
LIMIT_ENTRY_BUFFER_PTS: float | None = None

# Forward-looking limit expiry window in wall-clock seconds.
# None = same-bar fill (LIMIT_ENTRY_BUFFER_PTS still controls entry_price;
#        no new state machine state).
# >0   = limit must fill on a subsequent bar within this window;
#        expired signals produce a limit_expired TSV record.
# Bar count derived automatically: max(1, round(LIMIT_EXPIRY_SECONDS / bar_seconds)).
# Typical values: 60 (1 min), 120 (2 min), 300 (5 min).
# Optimizer search space: [None, 60.0, 120.0, 300.0]
LIMIT_EXPIRY_SECONDS: float | None = None

# Hybrid mode: bypass forward-looking limit for high-conviction bars.
# When entry_bar_body_ratio >= threshold, use same-bar fill regardless of
# LIMIT_EXPIRY_SECONDS. Only evaluated when LIMIT_EXPIRY_SECONDS is not None.
# None = all bars use forward-looking limit (no bypass).
# 0.60 = bars where the confirmation bar used >= 60% of its range as body use same-bar fill.
# Optimizer search space: [None, 0.40, 0.50, 0.60, 0.70]
LIMIT_RATIO_THRESHOLD: float | None = None
```

### Operating modes

| `LIMIT_ENTRY_BUFFER_PTS` | `LIMIT_EXPIRY_SECONDS` | `LIMIT_RATIO_THRESHOLD` | Mode |
|---|---|---|---|
| `None` | — | — | Disabled — current behaviour (entry = bar close) |
| `0.0+` | `None` | — | Same-bar fill — entry = anchor_close ± buffer; no new state |
| `0.0+` | `> 0` | `None` | Forward limit — new `WAITING_FOR_LIMIT_FILL` state; noise filtering |
| `0.0+` | `> 0` | `0.0–1.0` | Hybrid — same-bar fill when `body_ratio >= threshold`, forward limit otherwise |

### `_build_signal_from_bar` change

Add `anchor_close: float | None = None` parameter. Entry price logic:

```python
if anchor_close is not None and LIMIT_ENTRY_BUFFER_PTS is not None:
    entry_price = anchor_close - LIMIT_ENTRY_BUFFER_PTS if direction == "short" \
                  else anchor_close + LIMIT_ENTRY_BUFFER_PTS
else:
    entry_price = float(bar["Close"])
```

Also add `anchor_close_price` to the returned signal dict:

```python
"anchor_close_price": anchor_close if LIMIT_ENTRY_BUFFER_PTS is not None else None,
```

### New state machine state (`WAITING_FOR_LIMIT_FILL`)

Inserted between `WAITING_FOR_ENTRY` and `IN_TRADE`. Only active when
`LIMIT_ENTRY_BUFFER_PTS is not None and LIMIT_EXPIRY_SECONDS is not None`.

**Transition into**: confirmation bar fires while in `WAITING_FOR_ENTRY` (or
`REENTRY_ELIGIBLE`) AND forward-looking mode is active. Signal is built with limit
entry_price and stored in `pending_limit_signal`. State variables initialised:

```python
_limit_bars_elapsed  = 0
_limit_max_bars      = max(1, round(LIMIT_EXPIRY_SECONDS / bar_seconds))
_limit_missed_move   = 0.0   # max favourable pts during wait window
```

**Each bar in `WAITING_FOR_LIMIT_FILL`**:

```python
_limit_bars_elapsed += 1

# Track maximum favourable move during wait window
if direction == "short":
    favorable = pending_limit_signal["entry_price"] - float(bar["Low"])
else:
    favorable = float(bar["High"]) - pending_limit_signal["entry_price"]
_limit_missed_move = max(_limit_missed_move, favorable)

# Check fill
filled = (direction == "short" and float(bar["High"]) >= pending_limit_signal["entry_price"]) \
      or (direction == "long"  and float(bar["Low"])  <= pending_limit_signal["entry_price"])

if filled:
    pending_limit_signal["limit_fill_bars"] = _limit_bars_elapsed
    pending_limit_signal["missed_move_pts"] = None
    state = "IN_TRADE"
    # enter position from pending_limit_signal (same as current entry block)

elif _limit_bars_elapsed >= _limit_max_bars or session_ending:
    # Write limit_expired record, return to IDLE
    _write_limit_expired_record(pending_limit_signal, _limit_missed_move, day, ts)
    state = "IDLE"; anchor_close = None; pending_limit_signal = None
```

**Session end during `WAITING_FOR_LIMIT_FILL`**: treated same as expiry — write
`limit_expired` record with `exit_type = "limit_expired_session_close"`.

### Bar resolution detection

Computed once per session day, immediately after `mnq_reset` is sliced:

```python
if len(mnq_reset) >= 2:
    bar_seconds = (mnq_reset.index[1] - mnq_reset.index[0]).total_seconds()
else:
    bar_seconds = 60.0  # safe fallback
```

### New diagnostic fields in `trades.tsv`

| Field | Type | Filled when | Meaning |
|---|---|---|---|
| `anchor_close_price` | `float\|None` | Limit entry enabled | The anchor level; `bar_close - anchor_close_price` = entry improvement of limit vs market |
| `limit_fill_bars` | `int\|None` | Limit entry enabled AND trade filled | Bars waited before fill; 0 = same-bar fill |
| `missed_move_pts` | `float\|None` | `exit_type = limit_expired*` | Max favourable pts during wait window |

### `limit_expired` exit record

Built by a new helper `_build_limit_expired_record(signal, missed_move_pts, day, expiry_ts)`:

```
entry_date      = day
entry_time      = signal["entry_time"] (confirmation bar time)
exit_time       = expiry_ts
direction       = signal["direction"]
entry_price     = signal["entry_price"]  (the limit price)
exit_price      = None → written as ""
pnl             = 0.0
exit_type       = "limit_expired"  OR  "limit_expired_session_close"
contracts       = 0
anchor_close_price = signal["anchor_close_price"]
limit_fill_bars = None
missed_move_pts = missed_move_pts
... all other signal fields populated as normal ...
```

---

## Implementation Tasks

### Task 1 — Add constants + update `_build_signal_from_bar` + `screen_session` (`strategy_smt.py`)

**WAVE**: 1 | **AGENT_ROLE**: executor | **DEPENDS_ON**: none

#### 1a. Add `LIMIT_ENTRY_BUFFER_PTS`, `LIMIT_EXPIRY_SECONDS`, and `LIMIT_RATIO_THRESHOLD` constants

In the `# ══ STRATEGY TUNING ══` block, add all three constants with the docstrings from the
Design section above. Place them near the other entry/fill constants.

#### 1b. Update `_build_signal_from_bar` signature

Add `anchor_close: float | None = None` as the final keyword parameter.

#### 1c. Update entry price logic

Replace:
```python
entry_price = float(bar["Close"])
```
With:
```python
if anchor_close is not None and LIMIT_ENTRY_BUFFER_PTS is not None:
    entry_price = anchor_close - LIMIT_ENTRY_BUFFER_PTS if direction == "short" \
                  else anchor_close + LIMIT_ENTRY_BUFFER_PTS
else:
    entry_price = float(bar["Close"])
```

#### 1d. Add `anchor_close_price` to the signal dict return value

In the `return { ... }` block, add:
```python
"anchor_close_price": anchor_close if LIMIT_ENTRY_BUFFER_PTS is not None else None,
"limit_fill_bars":    None,   # populated by state machine on fill
"missed_move_pts":    None,   # populated by state machine on expiry
```

#### 1e. Update `screen_session` call to `_build_signal_from_bar`

Pass `anchor_close=ac` (already in scope):
```python
signal = _build_signal_from_bar(
    conf_bar, entry_time, direction, _tp,
    ...,
    anchor_close=ac,   # ← new
)
```

---

### Task 2 — Update `backtest_smt.py`: imports + resolution detection + call sites + new state

**WAVE**: 2 | **AGENT_ROLE**: executor | **DEPENDS_ON**: Task 1

#### 2a. Add new constants to import

Add `LIMIT_ENTRY_BUFFER_PTS, LIMIT_EXPIRY_SECONDS, LIMIT_RATIO_THRESHOLD` to the `from strategy_smt import (...)` block.

#### 2b. Bar resolution detection

Inside `run_backtest`, immediately after the `for day, day_bars in sessions:` loop opens and
`mnq_reset` is assigned (the per-session 1m slice), add:

```python
if len(mnq_reset) >= 2:
    bar_seconds = (mnq_reset.index[1] - mnq_reset.index[0]).total_seconds()
else:
    bar_seconds = 60.0
```

#### 2c. Pass `anchor_close` at both `_build_signal_from_bar` call sites

At call site 1 (`WAITING_FOR_ENTRY` block, ~L614) and call site 2 (`REENTRY_ELIGIBLE` block,
~L715), add `anchor_close=anchor_close` as keyword argument. `anchor_close` is the existing
state variable in scope at both locations.

#### 2d. Same-bar fill / hybrid / forward-limit branching

After signal is built at each call site, determine the operating mode:

```python
if signal is not None:
    # ... existing hypothesis/fvg annotation ...
    _body_ratio = signal.get("entry_bar_body_ratio", 0.0)
    _use_forward_limit = (
        LIMIT_ENTRY_BUFFER_PTS is not None
        and LIMIT_EXPIRY_SECONDS is not None
        and (LIMIT_RATIO_THRESHOLD is None
             or _body_ratio < LIMIT_RATIO_THRESHOLD)
    )
    if _use_forward_limit:
        # Forward-looking limit — defer entry
        state = "WAITING_FOR_LIMIT_FILL"
        pending_limit_signal = signal
        _limit_bars_elapsed = 0
        _limit_max_bars = max(1, round(LIMIT_EXPIRY_SECONDS / bar_seconds))
        _limit_missed_move = 0.0
    else:
        # Immediate entry: same-bar fill (limit or bar_close)
        signal["limit_fill_bars"] = 0 if LIMIT_ENTRY_BUFFER_PTS is not None else None
        # ... existing position creation block (unchanged) ...
```

Both call sites (WAITING_FOR_ENTRY and REENTRY_ELIGIBLE) get the same wrapping.

**Hybrid logic explanation**: when `LIMIT_RATIO_THRESHOLD` is set and the confirmation bar's
`entry_bar_body_ratio >= LIMIT_RATIO_THRESHOLD`, `_use_forward_limit` is `False` — the bar
moved decisively enough that waiting risks missing the continuation. The trade enters
immediately at the limit price (same-bar fill) with `limit_fill_bars = 0`.

#### 2e. Add `WAITING_FOR_LIMIT_FILL` state handler

After the `elif state == "REENTRY_ELIGIBLE":` block, add:

```python
elif state == "WAITING_FOR_LIMIT_FILL":
    _limit_bars_elapsed += 1

    # Track max favourable move during wait window
    if pending_limit_signal["direction"] == "short":
        _limit_missed_move = max(
            _limit_missed_move,
            pending_limit_signal["entry_price"] - float(bar["Low"])
        )
    else:
        _limit_missed_move = max(
            _limit_missed_move,
            float(bar["High"]) - pending_limit_signal["entry_price"]
        )

    filled = (
        (pending_limit_signal["direction"] == "short"
         and float(bar["High"]) >= pending_limit_signal["entry_price"])
        or
        (pending_limit_signal["direction"] == "long"
         and float(bar["Low"])  <= pending_limit_signal["entry_price"])
    )

    if filled:
        pending_limit_signal["limit_fill_bars"] = _limit_bars_elapsed
        risk_per_contract = (
            abs(pending_limit_signal["entry_price"] - pending_limit_signal["stop_price"])
            * MNQ_PNL_PER_POINT
        )
        contracts = min(MAX_CONTRACTS, max(1, int(RISK_PER_TRADE / risk_per_contract))) \
                    if risk_per_contract > 0 else 1
        position = {
            **pending_limit_signal,
            "entry_date": day, "contracts": contracts,
            "total_contracts_target": contracts,
            "layer_b_entered": False, "layer_b_entry_price": None,
            "layer_b_contracts": 0, "partial_done": False, "partial_price": None,
        }
        state = "IN_TRADE"
        pending_limit_signal = None

    elif _limit_bars_elapsed >= _limit_max_bars:
        expired_record = _build_limit_expired_record(
            pending_limit_signal, _limit_missed_move, day, ts,
            exit_type="limit_expired"
        )
        trade_records.append(expired_record)
        state = "IDLE"
        pending_limit_signal = None
        anchor_close = None
```

#### 2f. Session-end handling for `WAITING_FOR_LIMIT_FILL`

In the session-end cleanup block (where `session_close` exits are written), add:

```python
if state == "WAITING_FOR_LIMIT_FILL" and pending_limit_signal is not None:
    expired_record = _build_limit_expired_record(
        pending_limit_signal, _limit_missed_move, day, session_end_ts,
        exit_type="limit_expired_session_close"
    )
    trade_records.append(expired_record)
    pending_limit_signal = None
```

#### 2g. Initialise new state variables at session start

In the per-session variable initialisation block, add:

```python
pending_limit_signal = None
_limit_bars_elapsed  = 0
_limit_max_bars      = 0
_limit_missed_move   = 0.0
```

---

### Task 3 — Add `_build_limit_expired_record` helper and update `_build_trade_record`

**WAVE**: 3 | **AGENT_ROLE**: executor | **DEPENDS_ON**: Task 2

#### 3a. Add `_build_limit_expired_record` to `backtest_smt.py`

Place near `_build_trade_record`. This function builds a trade-like dict for an expired limit:

```python
def _build_limit_expired_record(
    signal: dict,
    missed_move_pts: float,
    entry_date,
    expiry_ts,
    exit_type: str = "limit_expired",
) -> dict:
    return {
        "entry_date":        entry_date,
        "entry_time":        signal["entry_time"].strftime("%H:%M")
                             if hasattr(signal["entry_time"], "strftime")
                             else str(signal["entry_time"]),
        "exit_time":         expiry_ts.strftime("%H:%M")
                             if hasattr(expiry_ts, "strftime") else str(expiry_ts),
        "direction":         signal["direction"],
        "entry_price":       round(signal["entry_price"], 4),
        "exit_price":        "",
        "tdo":               round(signal.get("tdo", 0), 4),
        "stop_price":        round(signal["stop_price"], 4),
        "contracts":         0,
        "pnl":               0.0,
        "exit_type":         exit_type,
        "divergence_bar":    signal.get("divergence_bar", -1),
        "entry_bar":         signal.get("entry_bar", -1),
        "stop_bar_wick_pts": "",
        "reentry_sequence":  "",
        "prior_trade_bars_held": "",
        "entry_bar_body_ratio": round(signal.get("entry_bar_body_ratio", 0.0), 4),
        "smt_sweep_pts":     round(signal.get("smt_sweep_pts", 0.0), 4),
        "smt_miss_pts":      round(signal.get("smt_miss_pts", 0.0), 4),
        "bars_since_divergence": "",
        "matches_hypothesis":signal.get("matches_hypothesis"),
        "smt_type":          signal.get("smt_type", ""),
        "fvg_high":          signal.get("fvg_high", ""),
        "fvg_low":           signal.get("fvg_low", ""),
        "layer_b_entered":   False,
        "layer_b_entry_price": "",
        "layer_b_contracts": 0,
        "hypothesis_direction": signal.get("hypothesis_direction", ""),
        "pd_range_case":     signal.get("pd_range_case", ""),
        "pd_range_bias":     signal.get("pd_range_bias", ""),
        "week_zone":         signal.get("week_zone", ""),
        "day_zone":          signal.get("day_zone", ""),
        "trend_direction":   signal.get("trend_direction", ""),
        "hypothesis_score":  signal.get("hypothesis_score", ""),
        "fvg_detected":      signal.get("fvg_detected", ""),
        "displacement_body_pts": signal.get("displacement_body_pts", ""),
        "pessimistic_fills": PESSIMISTIC_FILLS,
        # New diagnostic fields
        "anchor_close_price":  round(signal["anchor_close_price"], 4)
                               if signal.get("anchor_close_price") is not None else "",
        "limit_fill_bars":     "",
        "missed_move_pts":     round(missed_move_pts, 4),
    }
```

#### 3b. Add new fields to `_build_trade_record`

In the existing `_build_trade_record` return dict, add:

```python
"anchor_close_price": round(position["anchor_close_price"], 4)
                      if position.get("anchor_close_price") is not None else "",
"limit_fill_bars":    position.get("limit_fill_bars", ""),
"missed_move_pts":    "",  # always None for filled trades
```

---

### Task 4 — Update TSV writer fieldnames

**WAVE**: 4 | **AGENT_ROLE**: executor | **DEPENDS_ON**: Task 3

In `_write_trades_tsv` (or equivalent), add to the `fieldnames` list:

```python
"anchor_close_price",
"limit_fill_bars",
"missed_move_pts",
```

These must appear after `"pessimistic_fills"` (the last Plan 4 column) to avoid shifting
existing column indices in downstream analysis scripts.

---

### Task 5 — Write tests

**WAVE**: 5 | **AGENT_ROLE**: executor | **DEPENDS_ON**: Tasks 1–4

Add to `tests/test_smt_strategy.py` (unit tests for `_build_signal_from_bar`):

**T1** — `test_limit_entry_disabled_both_none`
Both params None → `entry_price = bar["Close"]`, `anchor_close_price = None`.

**T2** — `test_limit_entry_disabled_buffer_none`
`LIMIT_ENTRY_BUFFER_PTS=None`, `anchor_close=100.0` → `entry_price = bar["Close"]`.

**T3** — `test_limit_entry_disabled_anchor_none`
`LIMIT_ENTRY_BUFFER_PTS=0.0`, `anchor_close=None` → `entry_price = bar["Close"]`.

**T4** — `test_limit_entry_short_zero_buffer`
Short, `LIMIT_ENTRY_BUFFER_PTS=0.0`, `anchor_close=100.0` → `entry_price = 100.0`,
`anchor_close_price = 100.0`.

**T5** — `test_limit_entry_long_zero_buffer`
Long, `LIMIT_ENTRY_BUFFER_PTS=0.0`, `anchor_close=100.0` → `entry_price = 100.0`.

**T6** — `test_limit_entry_short_with_buffer`
Short, `LIMIT_ENTRY_BUFFER_PTS=0.5`, `anchor_close=100.0` → `entry_price = 99.5`.

**T7** — `test_limit_entry_long_with_buffer`
Long, `LIMIT_ENTRY_BUFFER_PTS=0.5`, `anchor_close=100.0` → `entry_price = 100.5`.

**T8** — `test_limit_entry_stop_recalculates`
Short, `LIMIT_ENTRY_BUFFER_PTS=0.0`, `anchor_close=100.0`, `tdo=70.0`,
`SHORT_STOP_RATIO=0.35`, `STRUCTURAL_STOP_MODE=False` →
stop = 100.0 + 0.35 × 30.0 = 110.5.

**T9** — `test_limit_entry_tdo_check_rejects`
Short, `LIMIT_ENTRY_BUFFER_PTS=2.0`, `anchor_close=100.0` → entry = 98.0.
`tdo=99.0` (above entry) → signal is `None`.

**T10** — `test_limit_entry_partial_exit_uses_new_entry`
Short, `LIMIT_ENTRY_BUFFER_PTS=0.0`, `anchor_close=100.0`, `tdo=70.0`,
`PARTIAL_EXIT_ENABLED=True`, `PARTIAL_EXIT_LEVEL_RATIO=0.33` →
partial_exit ≈ 100.0 + (70.0 − 100.0) × 0.33 = 90.1.

Add to `tests/test_smt_backtest.py` (integration tests for state machine):

**T11** — `test_limit_entry_forward_fills_on_next_bar`
Construct a session where: bar N is the confirmation bar (closes below anchor_close, no
retrace within bar); bar N+1 has high ≥ limit_price → trade enters on bar N+1 with
`entry_price = anchor_close`, `limit_fill_bars = 1`.

**T12** — `test_limit_entry_forward_expires`
Same setup but bar N+1 and N+2 both have high < limit_price → `limit_expired` record in
`trade_records`, `pnl = 0`, `missed_move_pts ≥ 0`.

**T13** — `test_limit_entry_expiry_missed_move_populated`
Construct bars where during the wait window, price moves 10 pts favourably before expiry →
`missed_move_pts ≈ 10.0` in expired record.

**T14** — `test_limit_entry_session_close_during_wait`
Session ends while in `WAITING_FOR_LIMIT_FILL` → record with
`exit_type = "limit_expired_session_close"` written.

**T15** — `test_bar_seconds_detected_1m`
Feed a session with 1m bars (60s gap) → `bar_seconds = 60.0`,
`max_limit_bars = max(1, round(120/60)) = 2` for `LIMIT_EXPIRY_SECONDS=120`.

**T16** — `test_bar_seconds_detected_1s`
Feed a session with 1s bars (1s gap) → `bar_seconds = 1.0`,
`max_limit_bars = max(1, round(120/1)) = 120` for `LIMIT_EXPIRY_SECONDS=120`.

**T17** — `test_limit_entry_disabled_baseline_unchanged`
Run `run_backtest` with `LIMIT_ENTRY_BUFFER_PTS=None` on a synthetic session that normally
produces 2 trades → same 2 trades, same entry_prices, no `limit_expired` rows, new columns
empty.

**T18** — `test_limit_ratio_threshold_high_body_uses_same_bar`
`LIMIT_ENTRY_BUFFER_PTS=0.5`, `LIMIT_EXPIRY_SECONDS=120.0`, `LIMIT_RATIO_THRESHOLD=0.60`.
Confirmation bar has `body_ratio=0.75` (≥ threshold) → `_use_forward_limit=False`, trade
enters immediately with `limit_fill_bars=0`, no `WAITING_FOR_LIMIT_FILL` state entered.

**T19** — `test_limit_ratio_threshold_low_body_uses_forward_limit`
Same constants. Confirmation bar has `body_ratio=0.40` (< threshold) → `_use_forward_limit=True`,
state transitions to `WAITING_FOR_LIMIT_FILL`, no immediate position created.

---

### Task 6 — Run full test suite

**WAVE**: 6 | **AGENT_ROLE**: executor | **DEPENDS_ON**: Task 5

```bash
uv run pytest tests/ -x -q 2>&1 | tail -20
```

All pre-existing tests must pass. T1–T17 must pass. If any pre-existing test fails,
investigate before proceeding.

---

## Downstream Effects (no extra code, by design)

| Effect | Reasoning |
|---|---|
| Stop widens when `SHORT_STOP_RATIO` path used | Higher entry → larger `distance_to_tdo` → wider stop → fewer contracts. Correct — reflects improved entry. |
| `DISPLACEMENT_STOP_MODE` stop shrinks slightly | Structural stop is fixed at `div_bar_high + buffer`; higher entry reduces gap to stop → fewer contracts. Natural consequence. |
| Forward-limit misses big immediate movers | By design — the `missed_move_pts` diagnostic field quantifies this. |
| `TDO_VALIDITY_CHECK` rejects more signals at limit entry | Higher SHORT entry may make `tdo >= entry_price` for marginal setups. Correct — those signals had no room. |
| `MIN_TDO_DISTANCE_PTS` filter passes more signals | Larger `distance_to_tdo` → more trades pass minimum distance. Net positive. |

---

## Test Coverage Summary

| Path | Test | Status |
|---|---|---|
| Both disabled → bar_close | T1 | ✅ |
| Buffer None → bar_close | T2 | ✅ |
| Anchor None → bar_close | T3 | ✅ |
| Short same-bar fill, zero buffer | T4 | ✅ |
| Long same-bar fill, zero buffer | T5 | ✅ |
| Short same-bar fill, buffer | T6 | ✅ |
| Long same-bar fill, buffer | T7 | ✅ |
| Stop recalculates from limit entry | T8 | ✅ |
| TDO validity check on limit entry | T9 | ✅ |
| Partial exit level uses limit entry | T10 | ✅ |
| Forward fill on next bar | T11 | ✅ |
| Forward limit expires → record written | T12 | ✅ |
| `missed_move_pts` populated on expiry | T13 | ✅ |
| Session close during wait → expired record | T14 | ✅ |
| `bar_seconds` from 1m timestamps | T15 | ✅ |
| `bar_seconds` from 1s timestamps | T16 | ✅ |
| Baseline unchanged when disabled | T17 | ✅ |
| High body_ratio → same-bar fill with threshold set | T18 | ✅ |
| Low body_ratio → forward limit with threshold set | T19 | ✅ |
| Pre-existing tests (all ~620+) | full suite | ✅ by design (default None) |

**Automated**: 19 new + all pre-existing | **Manual**: 0

---

## Files Changed

| File | Change type | Description |
|---|---|---|
| `strategy_smt.py` | Edit | Three new constants (`LIMIT_ENTRY_BUFFER_PTS`, `LIMIT_EXPIRY_SECONDS`, `LIMIT_RATIO_THRESHOLD`); `anchor_close` param + entry logic in `_build_signal_from_bar`; three new fields in signal dict return; `anchor_close=ac` in `screen_session` |
| `backtest_smt.py` | Edit | Import three new constants; `bar_seconds` detection; `anchor_close` at both call sites; hybrid same-bar/forward-limit branching using `LIMIT_RATIO_THRESHOLD`; new `WAITING_FOR_LIMIT_FILL` state; session-end handling; `_build_limit_expired_record` helper; three new fields in `_build_trade_record`; updated TSV fieldnames |
| `tests/test_smt_strategy.py` | Edit | T1–T10 (unit) |
| `tests/test_smt_backtest.py` | Edit | T11–T17 (integration) |

No new files. No changes to `signal_smt.py`.

---

## Acceptance Criteria Checklist

- [ ] `LIMIT_ENTRY_BUFFER_PTS = None` → baseline output identical (T17).
- [ ] Same-bar fill: entry = anchor_close ± buffer, no state change, `limit_fill_bars = 0` (T4–T7).
- [ ] Forward limit fills on next bar with correct `limit_fill_bars` (T11).
- [ ] Expired limit → `limit_expired` row in TSV, `pnl = 0`, `missed_move_pts` set (T12, T13).
- [ ] Session close during wait → `limit_expired_session_close` (T14).
- [ ] Bar resolution auto-detected from timestamps (T15, T16).
- [ ] `anchor_close_price` populated on all limit-entry trades and expired rows.
- [ ] Hybrid mode: `body_ratio >= LIMIT_RATIO_THRESHOLD` → same-bar fill; `body_ratio < threshold` → forward limit (T18, T19).
- [ ] All 19 new tests pass; no pre-existing regressions.
- [ ] `uv run pytest tests/ -x -q` exits 0.
