# Feature: SMT Solution F — ICT-Aligned Draw on Liquidity Target Selection

**⚠️ EXECUTION RULES — READ FIRST:**
- Implement ALL changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
- Only make code changes — no git operations

Validate documentation and codebase patterns before implementing. Match naming of existing utils, types, and models. Import from correct files.

---

## Feature Description

Replaces the strategy's single TDO take-profit target with a prioritised draw-on-liquidity hierarchy. Rather than locking every trade onto the 9:30 RTH open, the strategy selects the nearest valid draw that satisfies both a minimum RR (`MIN_RR_FOR_TARGET`) and a minimum distance (`MIN_TARGET_PTS`). If no valid draw exists for a given signal, the signal is skipped — preventing the degenerate near-TDO entries that produced the two largest single losses (-$490, -$460).

A secondary target is also selected (1.5× the primary distance) when available. The primary target acts as the existing `take_profit` (partial exit + breakeven trigger). The secondary target is a new exit type (`exit_secondary`) that closes the position when hit after primary has been breached. The trail mechanism is retained only when no secondary target exists.

**Draw hierarchy** (LONG; mirrored for SHORT):

| Priority | Level | Source |
|---|---|---|
| 1 | FVG ceiling | `_pending_fvg_zone["high"]` |
| 2 | TDO (9:30 RTH open) | `day_tdo` |
| 3 | Midnight Open | `compute_midnight_open()` |
| 4 | Session high at divergence time | `_run_ses_high` snapshot |
| 5 | Overnight high | `_day_overnight["overnight_high"]` |
| 6 | PDH (Previous Day High) | `compute_pdh_pdl()` (new) |

**Prerequisite**: `smt-solutions-a-e.md` complete. This plan executes AFTER Solutions A–E and BEFORE any optimization run.

**Isolation**: implement and validate as a standalone cycle before merging with A–E optimisation results.

## User Story

As a strategy developer  
I want every SMT trade to target a real ICT liquidity draw with a minimum 1.5× RR  
So that the strategy never enters near-TDO with a 3-pt nominal stop against a 60-pt bar wick

## Problem Statement

With TDO as the only target, entries close to TDO produce degenerate RR: `entry 9 pts from TDO → stop 3.15 pts → fills at 61-pt bar extreme → -$490 loss`. The two largest losses in the dataset share this structure. ICT treats TDO as one of six sequential draw levels, not a mandatory target.

## Solution Statement

`select_draw_on_liquidity()` selects the nearest level above `min_rr × stop_dist` and `min_pts`. Signals with no valid draw are skipped at confirmation time. A secondary target is stored in the position dict for two-level exit management.

## Feature Metadata

**Feature Type**: Enhancement  
**Complexity**: Medium-High  
**Primary Systems Affected**: `strategy_smt.py`, `backtest_smt.py`, `signal_smt.py`  
**Dependencies**: None (internal only)  
**Breaking Changes**: No — signals that previously used TDO still work if TDO is the nearest valid draw. The only behaviour change is that near-TDO entries with no valid draw at 1.5× RR are now skipped.

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ BEFORE IMPLEMENTING

- `strategy_smt.py:557` — `compute_midnight_open()` — pattern for new `compute_pdh_pdl()` helper
- `strategy_smt.py:574` — `compute_overnight_range()` — pattern for daily reference level computation
- `backtest_smt.py:306–318` — day-loop boundary where `compute_midnight_open` and `compute_overnight_range` are called — `compute_pdh_pdl` goes here
- `backtest_smt.py:606–613` — TP selection cascade (`OVERNIGHT_RANGE_AS_TP` / `MIDNIGHT_OPEN_AS_TP` / TDO) — replace with draws dict + `select_draw_on_liquidity()`
- `backtest_smt.py:665–682` — position dict construction at entry — add `secondary_target`, `secondary_target_name`, `tp_breached`
- `strategy_smt.py:1257` — `manage_position()` — add `tp_breached` flag set + secondary target check
- `backtest_smt.py:123–202` — `_build_trade_record()` — add `exit_secondary` branch (limit order, fills at `position["secondary_target"]`)
- `signal_smt.py:431` — `_process_scanning()` — mirror all backtest changes

### Patterns to Follow

- **Parameter naming**: `SCREAMING_SNAKE_CASE`, defined in `strategy_smt.py` and imported into `backtest_smt.py`
- **New helper functions**: placed near related helpers (`compute_pdh_pdl` near `compute_midnight_open` at line 557; `select_draw_on_liquidity` near `_build_signal_from_bar` at line 1165)
- **Export**: all new public functions and constants added to `from strategy_smt import (...)` in `backtest_smt.py:12–32`

---

## PARALLEL EXECUTION STRATEGY

### Dependency Graph

```
┌───────────────────────────────────────────────────────────┐
│ WAVE 1: strategy_smt.py — helpers + params (Parallel)    │
├────────────────────────────────┬──────────────────────────┤
│ Task 1.1: compute_pdh_pdl()    │ Task 1.2: select_draw_   │
│ + MIN_RR_FOR_TARGET,           │ on_liquidity() function  │
│   MIN_TARGET_PTS params        │                          │
│ Agent: strategy-spec.          │ Agent: strategy-spec.    │
└────────────────────────────────┴──────────────────────────┘
                                 ↓
┌───────────────────────────────────────────────────────────┐
│ WAVE 2: backtest_smt.py — day-loop + signal confirmation  │
├───────────────────────────────────────────────────────────┤
│ Task 2.1: call compute_pdh_pdl at day boundary            │
│ Task 2.2: replace TP cascade with draws dict + selector   │
│ Agent: backtest-specialist (sequential tasks)             │
└───────────────────────────────────────────────────────────┘
                                 ↓
┌───────────────────────────────────────────────────────────┐
│ WAVE 3: backtest_smt.py — position management             │
├───────────────────────────────────────────────────────────┤
│ Task 3.1: position dict (secondary_target, tp_breached)   │
│ Task 3.2: manage_position tp_breached + secondary check   │
│ Task 3.3: _build_trade_record exit_secondary branch       │
│ Agent: backtest-specialist (sequential tasks)             │
└───────────────────────────────────────────────────────────┘
                                 ↓
┌────────────────────────────────────────────────────────────┐
│ WAVE 4: signal_smt.py mirror (After Wave 3)               │
├────────────────────────────────────────────────────────────┤
│ Task 4.1: mirror Waves 2+3 in _process_scanning           │
│ Agent: signal-specialist                                   │
└────────────────────────────────────────────────────────────┘
                                 ↓
┌────────────────────────────────────────────────────────────┐
│ WAVE 5: Tests                                              │
├────────────────────────────────────────────────────────────┤
│ Task 5.1: unit + integration tests                         │
│ Agent: test-specialist                                     │
└────────────────────────────────────────────────────────────┘
```

### Interface Contracts

**Wave 1 → Wave 2**: `select_draw_on_liquidity(direction, entry_price, stop_price, draws, min_rr, min_pts) -> (name, price, sec_name, sec_price)` and `compute_pdh_pdl(hist_df, session_date) -> (pdh, pdl)` importable from `strategy_smt`. Constants `MIN_RR_FOR_TARGET`, `MIN_TARGET_PTS` importable from `strategy_smt`.

**Wave 3 → Wave 4**: `exit_secondary` is a defined exit type in `_build_trade_record`. `position["secondary_target"]`, `position["tp_breached"]` are defined at entry time.

---

## IMPLEMENTATION PLAN

### Wave 1: strategy_smt.py

#### Task 1.1: ADD `compute_pdh_pdl()` and new parameters

- **WAVE**: 1
- **AGENT_ROLE**: strategy-specialist
- **DEPENDS_ON**: []
- **BLOCKS**: [2.1]
- **PROVIDES**: `compute_pdh_pdl()` function + `MIN_RR_FOR_TARGET`, `MIN_TARGET_PTS` constants

**New parameters** (add to parameter block near existing TP flags):

```python
# ── Solution F: Draw-on-Liquidity target selection ────────────────────────────
MIN_RR_FOR_TARGET: float = 1.5   # reward ≥ min_rr × |entry − stop|; optimizer: [1.0, 1.5, 2.0]
MIN_TARGET_PTS:    float = 15.0  # absolute min target distance in MNQ pts; optimizer: [10, 15, 20, 25]
```

**New function** (add near `compute_midnight_open` at line ~557):

```python
def compute_pdh_pdl(
    hist_df: "pd.DataFrame",
    session_date: "datetime.date",
) -> "tuple[float | None, float | None]":
    """Return (previous_rth_day_high, previous_rth_day_low) from daily OHLCV."""
    prior = hist_df[hist_df.index.date < session_date]
    if prior.empty:
        return None, None
    prev = prior.iloc[-1]
    return float(prev["High"]), float(prev["Low"])
```

Export: add `compute_pdh_pdl`, `MIN_RR_FOR_TARGET`, `MIN_TARGET_PTS` to `from strategy_smt import (...)` in `backtest_smt.py`.

- **VALIDATE**: `python -c "from strategy_smt import compute_pdh_pdl, MIN_RR_FOR_TARGET, MIN_TARGET_PTS; print('ok')"`

---

#### Task 1.2: ADD `select_draw_on_liquidity()`

- **WAVE**: 1
- **AGENT_ROLE**: strategy-specialist
- **DEPENDS_ON**: []
- **BLOCKS**: [2.2]
- **PROVIDES**: `select_draw_on_liquidity()` function

Add near `_build_signal_from_bar` (~line 1165):

```python
def select_draw_on_liquidity(
    direction: str,
    entry_price: float,
    stop_price: float,
    draws: dict,
    min_rr: float = 1.5,
    min_pts: float = 15.0,
) -> "tuple[str | None, float | None, str | None, float | None]":
    """
    Return (primary_name, primary_price, secondary_name, secondary_price).
    Primary = nearest draw satisfying both RR and pts constraints.
    Secondary = next farther draw at least 1.5× primary distance.
    (None, None, None, None) if no valid draw exists.
    """
    stop_dist = abs(entry_price - stop_price)
    min_dist  = max(min_pts, min_rr * stop_dist)
    valid: list[tuple[str, float, float]] = []
    for name, price in draws.items():
        if price is None:
            continue
        dist = (price - entry_price) if direction == "long" else (entry_price - price)
        if dist >= min_dist:
            valid.append((name, price, dist))
    if not valid:
        return None, None, None, None
    valid.sort(key=lambda x: x[2])
    pri = valid[0]
    sec = next((v for v in valid[1:] if v[2] >= pri[2] * 1.5), None)
    return pri[0], pri[1], (sec[0] if sec else None), (sec[1] if sec else None)
```

Export: add `select_draw_on_liquidity` to `from strategy_smt import (...)` in `backtest_smt.py`.

- **VALIDATE**: `python -c "from strategy_smt import select_draw_on_liquidity; print(select_draw_on_liquidity('long', 100, 95, {'level_a': 110, 'level_b': 125}, 1.5, 5))"`

**Wave 1 Checkpoint**: `python -c "from strategy_smt import compute_pdh_pdl, select_draw_on_liquidity, MIN_RR_FOR_TARGET; print('wave1 ok')"`

---

### Wave 2: backtest_smt.py — Day Loop + Signal Confirmation

#### Task 2.1: CALL `compute_pdh_pdl` at day-loop boundary

- **WAVE**: 2
- **AGENT_ROLE**: backtest-specialist
- **DEPENDS_ON**: [1.1]
- **BLOCKS**: [2.2]
- **PROVIDES**: `_day_pdh`, `_day_pdl` available per session day

In the day-loop boundary block (`backtest_smt.py:306–318`), alongside `compute_midnight_open`:

```python
_day_pdh, _day_pdl = compute_pdh_pdl(_hist_mnq_df, day)
```

Also ensure `_hist_mnq_df` is loaded (it already is at line 272–275 for hypothesis context — reuse it).

- **VALIDATE**: `python -m pytest tests/test_smt_backtest.py -q`

---

#### Task 2.2: REPLACE TP cascade with draws dict + `select_draw_on_liquidity`

- **WAVE**: 2
- **AGENT_ROLE**: backtest-specialist
- **DEPENDS_ON**: [2.1, 1.2]
- **BLOCKS**: [3.1]
- **PROVIDES**: `_day_tp`, `sec_tp`, `tp_name`, `sec_tp_name` from draw selection

**Replace** the existing TP selection block at `backtest_smt.py:606–613`:

```python
# REMOVE:
if OVERNIGHT_RANGE_AS_TP:
    ...
elif MIDNIGHT_OPEN_AS_TP and _day_midnight_open is not None:
    _day_tp = _day_midnight_open
else:
    _day_tp = day_tdo
```

**WITH**:

```python
if pending_direction == "long":
    draws = {
        "fvg_top":        _pending_fvg_zone["high"] if _pending_fvg_zone else None,
        "tdo":            day_tdo if day_tdo and day_tdo > entry_price else None,
        "midnight_open":  _day_midnight_open if _day_midnight_open and _day_midnight_open > entry_price else None,
        "session_high":   _run_ses_high if _run_ses_high > entry_price + 1 else None,
        "overnight_high": _day_overnight.get("overnight_high"),
        "pdh":            _day_pdh,
    }
else:
    draws = {
        "fvg_bottom":    _pending_fvg_zone["low"] if _pending_fvg_zone else None,
        "tdo":           day_tdo if day_tdo and day_tdo < entry_price else None,
        "midnight_open": _day_midnight_open if _day_midnight_open and _day_midnight_open < entry_price else None,
        "session_low":   _run_ses_low if _run_ses_low < entry_price - 1 else None,
        "overnight_low": _day_overnight.get("overnight_low"),
        "pdl":           _day_pdl,
    }
tp_name, _day_tp, sec_tp_name, sec_tp = select_draw_on_liquidity(
    pending_direction, signal_entry_price, signal_stop_price,
    draws, MIN_RR_FOR_TARGET, MIN_TARGET_PTS,
)
if _day_tp is None:
    anchor_close = None
    continue  # no viable draw → skip this confirmation bar
```

Note: `signal_entry_price` and `signal_stop_price` must be computed from the bar before the draws dict is built. Read entry/stop from `_build_signal_from_bar`'s output — call `_build_signal_from_bar` first (as already happens at line 614), extract the prices, then build the draws dict and call the selector. Alternatively, compute entry/stop directly here (entry = bar close or wick, stop = based on stop ratio) to avoid calling `_build_signal_from_bar` twice. The simplest approach: call `_build_signal_from_bar` first, then check if `signal is not None` before building the draws dict.

Revised structure:

```python
signal = _build_signal_from_bar(bar, ts, pending_direction, day_tdo, ...)
if signal is None:
    continue
# Build draws dict using signal["entry_price"] and signal["stop_price"]
# ... (draws dict as above)
tp_name, _day_tp, sec_tp_name, sec_tp = select_draw_on_liquidity(...)
if _day_tp is None:
    continue  # no viable draw
# Override signal's take_profit with the selected draw
signal["take_profit"] = _day_tp
signal["tp_name"]     = tp_name
```

Retain `MIDNIGHT_OPEN_AS_TP` and `OVERNIGHT_RANGE_AS_TP` as dead-letter constants (do not delete them in this plan — they may still be imported by tests; remove in a follow-up cleanup cycle).

- **VALIDATE**: `python -m pytest tests/test_smt_backtest.py -q`

**Wave 2 Checkpoint**: `python -m pytest tests/test_smt_backtest.py tests/test_smt_strategy.py -q`

---

### Wave 3: backtest_smt.py — Position Management

#### Task 3.1: ADD `secondary_target` and `tp_breached` to position dict at entry

- **WAVE**: 3
- **AGENT_ROLE**: backtest-specialist
- **DEPENDS_ON**: [2.2]
- **BLOCKS**: [3.2]
- **PROVIDES**: Position carries secondary target + breach flag

At position dict construction (`backtest_smt.py:665`), add:

```python
position["secondary_target"]      = sec_tp
position["secondary_target_name"] = sec_tp_name
position["tp_breached"]           = False
```

---

#### Task 3.2: ADD `tp_breached` flag set and secondary target check in `manage_position`

- **WAVE**: 3
- **AGENT_ROLE**: backtest-specialist
- **DEPENDS_ON**: [3.1]
- **BLOCKS**: [3.3]
- **PROVIDES**: `exit_secondary` fires when secondary target is reached after primary breach

**Location**: `strategy_smt.py:1257` — `manage_position()`.

Find the existing logic that handles primary `take_profit` crossing (trail trigger, partial exit, breakeven). Add `tp_breached` flag set at that same check:

```python
# Set tp_breached on first primary TP crossing
if not position.get("tp_breached"):
    tp = position.get("take_profit")
    if tp is not None:
        if direction == "long"  and float(current_bar["High"]) >= tp:
            position["tp_breached"] = True
        elif direction == "short" and float(current_bar["Low"])  <= tp:
            position["tp_breached"] = True

# Secondary target check — only after primary has been breached
sec = position.get("secondary_target")
if sec is not None and position.get("tp_breached"):
    if direction == "long"  and float(current_bar["High"]) >= sec:
        return "exit_secondary"
    if direction == "short" and float(current_bar["Low"])  <= sec:
        return "exit_secondary"
```

**Trail gate**: find where `TRAIL_AFTER_TP_PTS` is applied (the trail activation logic after TDO cross) and wrap it with:

```python
if position.get("secondary_target") is None:
    # trail logic here
```

This disables trail for positions that have a secondary target — the secondary target takes over.

- **VALIDATE**: `python -m pytest tests/test_smt_strategy.py -q`

---

#### Task 3.3: ADD `exit_secondary` branch in `_build_trade_record`

- **WAVE**: 3
- **AGENT_ROLE**: backtest-specialist
- **DEPENDS_ON**: [3.2]
- **BLOCKS**: [4.1, 5.1]
- **PROVIDES**: `exit_secondary` trades recorded at secondary_target price

In `_build_trade_record` (`backtest_smt.py:131`), add a new branch alongside `exit_tp` and `exit_stop`:

```python
elif exit_result == "exit_secondary":
    # Limit order hit at secondary target — fills at defined price, no slippage
    exit_price = position["secondary_target"]
```

Add `"tp_name"` and `"secondary_target_name"` to the trade record dict for diagnostics:

```python
"tp_name":               position.get("tp_name"),
"secondary_target_name": position.get("secondary_target_name"),
```

**Wave 3 Checkpoint**: `python -m pytest tests/test_smt_backtest.py tests/test_smt_strategy.py -q`

---

### Wave 4: signal_smt.py Mirror

#### Task 4.1: MIRROR Waves 2+3 in `_process_scanning`

- **WAVE**: 4
- **AGENT_ROLE**: signal-specialist
- **DEPENDS_ON**: [3.3]
- **BLOCKS**: [5.1]
- **PROVIDES**: Live path has identical draw selection and secondary target behaviour

**Scope** (`signal_smt.py:431` — `_process_scanning()`):

1. Add `compute_pdh_pdl` call in session initialisation (call once per session day, store as module-level `_day_pdh`, `_day_pdl`)
2. Replace TP cascade with draws dict + `select_draw_on_liquidity()` — identical to backtest Task 2.2
3. Set `secondary_target`, `secondary_target_name`, `tp_breached` in position dict
4. Add `tp_breached` set + secondary target check in `_process_managing()` (the live equivalent of `manage_position`)
5. Any `exit_secondary` signal emitted by `_process_managing` must be handled (emit an EXIT signal to the caller)

Import `compute_pdh_pdl`, `select_draw_on_liquidity`, `MIN_RR_FOR_TARGET`, `MIN_TARGET_PTS` from `strategy_smt`.

- **VALIDATE**: `python -m pytest tests/ -q -k "signal"` (or full suite)

**Wave 4 Checkpoint**: `python -m pytest tests/ -q`

---

### Wave 5: Tests

#### Task 5.1: ADD tests for all new logic

- **WAVE**: 5
- **AGENT_ROLE**: test-specialist
- **DEPENDS_ON**: [1.1, 1.2, 2.1, 2.2, 3.1, 3.2, 3.3, 4.1]
- **BLOCKS**: []

Add to `tests/test_smt_strategy.py` (units) and `tests/test_smt_backtest.py` (integration). Create `tests/test_smt_solution_f.py` if test count exceeds 20.

**Required test cases:**

| # | File | Test name | What it asserts |
|---|------|-----------|-----------------|
| 1 | test_smt_strategy.py | `test_select_draw_no_valid` | returns (None,None,None,None) when all draws below min_dist |
| 2 | test_smt_strategy.py | `test_select_draw_single_valid_no_secondary` | primary returned, secondary None when only one draw qualifies |
| 3 | test_smt_strategy.py | `test_select_draw_secondary_requires_1pt5x` | second draw at 1.6× primary dist → selected as secondary |
| 4 | test_smt_strategy.py | `test_select_draw_secondary_below_threshold` | second draw at 1.4× primary dist → secondary is None |
| 5 | test_smt_strategy.py | `test_select_draw_short_direction` | dist computed as entry - price for short signals |
| 6 | test_smt_strategy.py | `test_select_draw_min_pts_overrides_rr` | min_pts floor applies when rr × stop_dist < min_pts |
| 7 | test_smt_strategy.py | `test_compute_pdh_pdl_empty_hist` | returns (None, None) on empty dataframe |
| 8 | test_smt_strategy.py | `test_compute_pdh_pdl_returns_prior_day` | returns correct High/Low from day before session_date |
| 9 | test_smt_strategy.py | `test_compute_pdh_pdl_monday_uses_friday` | correctly handles weekend gap (Monday → Friday) |
| 10 | test_smt_backtest.py | `test_signal_near_tdo_skipped` | entry within 5 pts of TDO → no trade placed |
| 11 | test_smt_backtest.py | `test_signal_with_pdh_draw_placed` | entry far from TDO, PDH qualifies → trade placed with take_profit=PDH |
| 12 | test_smt_backtest.py | `test_secondary_target_stored_in_position` | position dict has secondary_target after entry |
| 13 | test_smt_backtest.py | `test_tp_breached_false_at_entry` | tp_breached is False when position opened |
| 14 | test_smt_backtest.py | `test_tp_breached_set_on_primary_cross` | tp_breached becomes True when price crosses take_profit |
| 15 | test_smt_backtest.py | `test_exit_secondary_fires_after_primary` | bar crossing secondary level after tp_breached → exit_secondary |
| 16 | test_smt_backtest.py | `test_exit_secondary_not_before_primary` | bar crossing secondary level before tp_breached → no exit |
| 17 | test_smt_backtest.py | `test_exit_secondary_fill_at_secondary_price` | exit_price == position["secondary_target"], not bar extreme |
| 18 | test_smt_backtest.py | `test_trail_disabled_when_secondary_exists` | TRAIL_AFTER_TP_PTS has no effect when secondary_target is set |
| 19 | test_smt_backtest.py | `test_trail_active_when_no_secondary` | TRAIL_AFTER_TP_PTS still fires when secondary_target is None |
| 20 | test_smt_backtest.py | `test_tp_name_in_trade_record` | trade record contains tp_name field |

- **VALIDATE**: `python -m pytest tests/ -q`

**Wave 5 Checkpoint**: All 20 new tests pass; full suite has zero new failures.

---

## TESTING STRATEGY

### Unit Tests

**Status**: ✅ Automated | **Tool**: pytest | **Location**: `tests/test_smt_strategy.py` | **Run**: `python -m pytest tests/test_smt_strategy.py -q`

Tests 1–9: pure function logic for `select_draw_on_liquidity` and `compute_pdh_pdl`.

### Integration Tests

**Status**: ✅ Automated | **Tool**: pytest | **Location**: `tests/test_smt_backtest.py` | **Run**: `python -m pytest tests/test_smt_backtest.py -q`

Tests 10–20: synthetic bar fixtures exercising near-TDO skip, secondary target lifecycle, exit_secondary fill correctness.

### Manual Tests

None required — all paths exercisable with synthetic bar fixtures and parquet-free conftest.

### Test Automation Summary

| | Count | % |
|---|---|---|
| ✅ Unit (pytest) | 9 | 45% |
| ✅ Integration (pytest) | 11 | 55% |
| ⚠️ Manual | 0 | 0% |
| **Total** | 20 | 100% |

---

## VALIDATION COMMANDS

### Level 1: Import check

```bash
python -c "from strategy_smt import compute_pdh_pdl, select_draw_on_liquidity, MIN_RR_FOR_TARGET, MIN_TARGET_PTS; print('imports ok')"
```

### Level 2: Unit tests

```bash
python -m pytest tests/test_smt_strategy.py -q
```

### Level 3: Integration tests

```bash
python -m pytest tests/test_smt_backtest.py -q
```

### Level 4: Full suite regression + backtest smoke

```bash
python -m pytest tests/ -q
```

After tests pass, run a full backtest and compare against A–E baseline:

```bash
python backtest_smt.py
```

Expected: trade count decreases (near-TDO signals skipped), `avg_rr` increases, `mean_test_pnl` holds or improves.

---

## ACCEPTANCE CRITERIA

- [ ] `compute_pdh_pdl()` and `select_draw_on_liquidity()` importable from `strategy_smt`
- [ ] `MIN_RR_FOR_TARGET` and `MIN_TARGET_PTS` defined in `strategy_smt` and imported in `backtest_smt.py`
- [ ] TP cascade (`OVERNIGHT_RANGE_AS_TP` / `MIDNIGHT_OPEN_AS_TP` / TDO) replaced by draws dict + `select_draw_on_liquidity()` in `backtest_smt.py`
- [ ] Signals with no valid draw at minimum RR/pts are skipped (no trade placed)
- [ ] `position["secondary_target"]`, `position["secondary_target_name"]`, `position["tp_breached"]` set at entry
- [ ] `tp_breached` transitions from False → True on first primary TP crossing
- [ ] `exit_secondary` fires when secondary target hit after primary breach; NOT before
- [ ] `exit_secondary` fills at `position["secondary_target"]` (not bar extreme)
- [ ] `TRAIL_AFTER_TP_PTS` disabled when `secondary_target` is set; active when None
- [ ] All changes mirrored in `signal_smt._process_scanning`
- [ ] All 20 new tests pass
- [ ] Full suite passes with zero new regressions (post-A-E baseline)

---

## COMPLETION CHECKLIST

- [ ] Wave 1 complete (strategy_smt.py helpers + params) — checkpoint passed
- [ ] Wave 2 complete (backtest day loop + signal confirmation) — checkpoint passed
- [ ] Wave 3 complete (position dict, manage_position, _build_trade_record) — checkpoint passed
- [ ] Wave 4 complete (signal_smt.py mirror) — checkpoint passed
- [ ] Wave 5 complete (tests) — all 20 new tests passing
- [ ] Level 1–4 validation commands all pass
- [ ] Full suite regression: 0 new failures
- [ ] All acceptance criteria checked
- [ ] **⚠️ Debug logs REMOVED**
- [ ] **⚠️ Changes UNSTAGED — NOT committed**

---

## NOTES

- `OVERNIGHT_RANGE_AS_TP` and `MIDNIGHT_OPEN_AS_TP` are superseded by this solution. Do NOT delete them in this plan — remove them in a follow-up cleanup cycle after validation, since they may still be referenced by existing tests or signal_smt.py.
- `MAX_TDO_DISTANCE_PTS` becomes less relevant once target selection is dynamic (TDO is now just one of six candidates, filtered by RR). Evaluate removing it in a subsequent optimization run; do not change it in this plan.
- The `select_draw_on_liquidity` call uses `signal["entry_price"]` and `signal["stop_price"]` from `_build_signal_from_bar`. This means `_build_signal_from_bar` is called first with `day_tdo` as a placeholder TP, then the TP is overridden with the selected draw. This is acceptable since `_build_signal_from_bar` uses `take_profit` only for TDO distance validation — confirm this doesn't cause double-rejection after the override.
- The `_run_ses_high` / `_run_ses_low` snapshot at the draw dict construction point is the session high/low up to (but not including) the current bar — this is correct. It reflects the opposing extreme that was swept to produce the divergence signal.
- Secondary target fill (`exit_secondary`) is a limit order hit — fill at `position["secondary_target"]`, identical semantics to `exit_tp`. The spec bug stating "bar High/Low" was fixed in `ideas.md` before this plan was written.
