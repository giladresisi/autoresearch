# Strategy Refactor: Stateful Scanner, Perf, and Cleanup

## Primary Goal

All strategy decision logic lives in `strategy_smt.py`. Both `backtest_smt.py` and
`signal_smt.py` make exactly **one call per bar** to the strategy layer:

- No open position → `result = process_scan_bar(state, context, bar_idx, ...)`
- Open position    → `result = manage_position(position, bar)`

`manage_position` is already correct and needs no changes.

---

## Problem: Current Architecture

`screen_session` is a stale compatibility shim not updated as the backtest evolved:

1. **Live and backtest execute different strategies.** 8+ features exist in `run_backtest`
   that `screen_session` silently ignores (full list below).
2. **~670 lines of strategy logic** are inlined in backtest's state machine, unreachable
   by `signal_smt.py`.
3. **`signal_smt.py` rescans from bar 0 on every tick** — O(N²) per session due to
   `reset_index + array extraction + full loop` repeated on every 1s bar.
4. **~9 modules of duplication** between the WAITING_FOR_ENTRY and REENTRY_ELIGIBLE
   blocks (identical ~230 lines each), and between backtest/screen_session for shared
   infrastructure (running extremes, HTF tracking, expanded ref level scan).

---

## Features Present in Backtest, Absent from screen_session

| Feature | Backtest location | screen_session |
|---------|------------------|----------------|
| Replacement hypothesis (Sol A+B) | WAITING_FOR_ENTRY L727–783 | ❌ |
| `MIN_DIV_SCORE` gate | IDLE L1381–1385 | ❌ |
| `DIV_SCORE_DECAY` / adverse-move decay | `_effective_div_score` call | ❌ |
| `HYPOTHESIS_INVALIDATION_PTS` (Sol D) | WAITING_FOR_ENTRY L785–794 | ❌ |
| `CONFIRMATION_BAR_MINUTES` synthetic bar | WAITING_FOR_ENTRY L796–809 | ❌ always 1-bar |
| Adverse anchor update | WAITING_FOR_ENTRY L812–819 | ❌ anchor never updated |
| `select_draw_on_liquidity` | WAITING_FOR_ENTRY L844–876 | ❌ old cascade |
| `secondary_target` | WAITING_FOR_ENTRY L955–956 | ❌ |
| `HYPOTHESIS_FILTER` gate | WAITING_FOR_ENTRY L898–902 | ❌ |
| `WAITING_FOR_LIMIT_FILL` state | L1151–1218 | ❌ entirely missing |
| HTF tracking state | inline, ~60 lines | ✅ duplicated |
| Expanded reference level scan | inline, ~30 lines | ✅ duplicated |
| Running extremes update | inline, ~15 lines | ✅ duplicated |

---

## Part 1 — Stateful Scanner Architecture

### New Types in strategy_smt.py

```python
class ScanState:
    """Mutable scanning state — persists across bars within one session."""
    scan_state: str   # "IDLE"|"WAITING_FOR_ENTRY"|"REENTRY_ELIGIBLE"|"WAITING_FOR_LIMIT_FILL"
    pending_direction: str | None
    anchor_close: float | None
    divergence_bar_idx: int
    conf_window_start: int
    pending_smt_sweep: float
    pending_smt_miss: float
    pending_div_bar_high: float
    pending_div_bar_low: float
    pending_smt_defended: float | None
    pending_smt_type: str
    pending_fvg_zone: dict | None
    pending_fvg_detected: bool
    pending_displacement_bar_extreme: float | None
    pending_div_score: float
    pending_div_provisional: bool
    pending_discovery_bar_idx: int
    pending_discovery_price: float
    pending_limit_signal: dict | None
    limit_bars_elapsed: int
    limit_max_bars: int
    limit_missed_move: float
    reentry_count: int
    prior_trade_bars_held: int     # caller updates after trade closes
    htf_state: dict                # per-period running extremes (moved from caller)

class SessionContext:
    """Immutable per-session data — computed once per trading day."""
    day: datetime.date
    tdo: float
    midnight_open: float | None
    overnight: dict                # {overnight_high, overnight_low}
    pdh: float | None
    pdl: float | None
    hyp_ctx: dict | None
    hyp_dir: str | None
    bar_seconds: float
    ref_lvls: dict                 # _compute_ref_levels result
```

### New Function in strategy_smt.py

```python
def process_scan_bar(
    state: ScanState,
    context: SessionContext,
    bar_idx: int,
    bar,                    # _BarRow: current MNQ bar
    mnq_reset: pd.DataFrame,
    mes_reset: pd.DataFrame,
    smt_cache: dict,        # mutable; caller updates before each call
    run_ses_high: float,
    run_ses_low: float,
    ts: pd.Timestamp,
    min_signal_ts: pd.Timestamp,
) -> dict | None:
    """
    Returns:
        None                      — nothing actionable
        {"type": "signal", ...}   — entry signal; caller applies contract sizing
        {"type": "expired", ...}  — limit order expired without fill
    Mutates state in place. Never builds trade records or sizes contracts.
    """
```

Contains ALL strategy decision logic currently inlined in IDLE, WAITING_FOR_ENTRY,
REENTRY_ELIGIBLE, and WAITING_FOR_LIMIT_FILL blocks. WAITING_FOR_ENTRY and
REENTRY_ELIGIBLE are merged into a single internal WAITING path — they are structurally
identical after the divergence-detection phase.

### screen_session rewritten as thin wrapper

```python
def screen_session(mnq_bars, mes_bars, tdo, midnight_open=None, overnight_range=None,
                   pdh=None, pdl=None, hyp_ctx=None, ...) -> dict | None:
    state = ScanState()
    context = SessionContext(tdo=tdo, ...)
    # pre-extract arrays, init running extremes + smt_cache (mutable dict)
    for bar_idx in range(n_bars):
        # update running extremes + smt_cache in-place (15 lines, same as today)
        result = process_scan_bar(state, context, bar_idx, bar, mnq_reset, mes_reset,
                                  smt_cache, run_ses_high, run_ses_low, ts, min_signal_ts)
        if result and result["type"] == "signal":
            return result
    return None
```

Correct by construction — cannot diverge from backtest behavior.

### Simplified backtest_smt.py bar loop

```python
for bar_idx in range(len(mnq_session)):
    # update running extremes + smt_cache (unchanged, ~20 lines)

    if state == "IN_TRADE":
        result = manage_position(position, bar)
        # MAX_HOLD_BARS, session_close, partial_exit, _build_trade_record (unchanged)
    else:
        scan_result = process_scan_bar(scan_state, session_ctx, bar_idx, bar,
                                       mnq_reset, mes_reset, smt_cache,
                                       run_ses_high, run_ses_low, ts, min_signal_ts)
        if scan_result is None:
            continue
        if scan_result["type"] == "expired":
            trades.append(_build_limit_expired_record(scan_result, day, ts))
        elif scan_result["type"] == "signal":
            # CONTRACT SIZING stays in backtest — harness concern:
            risk_per_contract = _compute_contracts(scan_result)  # new helper
            position = {**scan_result, "entry_date": day, "contracts": contracts, ...}
            state = "IN_TRADE"
```

Removes ~670 lines of inlined strategy logic from `run_backtest`.

### signal_smt.py changes

Replace the per-tick `screen_session` call + manual draws assembly with:

```python
# Session start (once per day):
_scan_state = ScanState()
_session_ctx = SessionContext(tdo=tdo, ...)

# Per 1s bar in _process_scanning:
result = process_scan_bar(_scan_state, _session_ctx, bar_idx, ...)
if result and result["type"] == "signal":
    signal = result
    # apply slippage, open position (unchanged)
```

The manual `_draws` dict + `select_draw_on_liquidity` block (signal_smt.py lines 507–538)
is deleted — draw selection happens inside `process_scan_bar`.

**Live-only guards that stay in signal_smt.py:**
- Startup timestamp guard (`signal["entry_time"] <= _startup_ts`)
- Re-detection guard (`signal["entry_time"] <= _last_exit_ts`)
- Per-divergence reentry counter (`_divergence_reentry_count`)
- Slippage application, POSITION_FILE persistence, hypothesis generation timing

---

## Part 2 — Performance Issues

Listed by impact. Hot path = the inner bar loop (~200K iterations per backtest run, or
~23,400 iterations per live session).

### P1 — O(N²) live rescan [Critical — hot path]
**Where:** `screen_session` called on every 1s tick; each call does `reset_index`,
array extraction, and a full bar scan from 0.
**Fix:** Stateful scanner (Part 1). Once `process_scan_bar` is in place, `_process_scanning`
calls it with just the current bar — O(1) per tick.

### P2 — `pd.concat + sort_index` on every tick [Critical — signal_smt.py hot path]
**Where:** `signal_smt.py` lines 465–467:
```python
combined_mnq = pd.concat([_mnq_1m_df, _mnq_1s_buf]).sort_index()
```
O(N) DataFrame construction on every 1s bar. With stateful scanner, the session slice
is built once at session start and extended by one row per bar. The concat disappears.
**Fix:** On session start, slice today's session from `_mnq_1m_df` into a running list
of rows. Append each new 1s bar as it arrives. Pass to `process_scan_bar` as a DataFrame
(built once) or as pre-extracted numpy arrays.

### P3 — `smt_cache` dict allocated on every bar [Hot path — both files]
**Where:** `backtest_smt.py` line 556, `screen_session` line 1123 — a new 8-key dict is
created on every bar iteration (~200K allocations per backtest).
```python
_smt_cache = {"mes_h": _ses_mes_h, "mes_l": _ses_mes_l, ...}  # new dict every bar
```
**Fix:** Allocate once per session; update values in-place each bar:
```python
_smt_cache = {"mes_h": float("nan"), ...}  # once at session start
# per bar: _smt_cache["mes_h"] = _ses_mes_h  (no allocation)
```

### P4 — `pd.Series` for synthetic confirmation bar [Hot path when CONFIRMATION_BAR_MINUTES>1]
**Where:** `backtest_smt.py` lines 803–809 and 988–994 — constructs a `pd.Series` for
the synthetic N-bar candle when `CONFIRMATION_BAR_MINUTES > 1`.
**Fix:** Use `_BarRow` (already defined in backtest_smt.py). `is_confirmation_bar` and
`_build_signal_from_bar` only access `bar["Open"]`, `bar["High"]`, `bar["Low"]`,
`bar["Close"]` and `bar.name` — `_BarRow` supports all of these.

### P5 — `ts.strftime("%H:%M")` in the inner loop [Hot path]
**Where:** Called up to 3× per bar in backtest (IDLE + WAITING_FOR_ENTRY + REENTRY_ELIGIBLE
blackout checks) and 1× in screen_session. `strftime` parses a format string each call.
**Fix:** Compare `ts.time()` against pre-computed time objects:
```python
_blackout_start_t = pd.Timestamp(f"2000-01-01 {SIGNAL_BLACKOUT_START}").time()
_blackout_end_t   = pd.Timestamp(f"2000-01-01 {SIGNAL_BLACKOUT_END}").time()
# Per bar:
if _blackout_start_t <= ts.time() < _blackout_end_t:
```
Same fix applies to silver-bullet window check.

### P6 — `import math as _math` inside screen_session function body
**Where:** `strategy_smt.py` line 1031 — inside the function, executed on every call
from signal_smt.py (~23,400 times per session).
**Fix:** Move to module top (already imported as `import math as _math` at the top of
backtest_smt.py — just needs to be added to strategy_smt.py top).

### P7 — `mnq_bars.index[bar_idx]` in screen_session inner loop
**Where:** `screen_session` lines 1092, 1130 — pandas index access in a tight loop.
**Fix:** Pre-extract: `_mnq_idx = mnq_bars.index` once before the loop, then `_mnq_idx[bar_idx]`.
(The backtest already does this correctly via `_mnq_idx = mnq_session.index`.)

### P8 — `import datetime as _dt` inside the session loop
**Where:** `backtest_smt.py` line 493 — inside the `for day in trading_days:` loop when
`EXPANDED_REFERENCE_LEVELS=True`. Called N times (once per session day).
**Fix:** Move to module top.

### P9 — `import statistics` inside `_compute_metrics`
**Where:** `backtest_smt.py` line 1461. Not in a hot loop but bad practice.
**Fix:** Move to module top.

### P10 — Redundant `hasattr` check for `_prev_day` computation
**Where:** `backtest_smt.py` lines 468–470:
```python
_prev_day = day - pd.Timedelta(days=1) if hasattr(day, "days") else \
    day.__class__.fromordinal(day.toordinal() - 1)
```
`day` is always `datetime.date` (from `ts.date()`); neither branch uses `pd.Timedelta`.
**Fix:** `_prev_day = day - datetime.timedelta(days=1)` — `datetime` already imported.

---

## Part 3 — Code Cleanup

### Dead Code

**D1 — `find_entry_bar` (strategy_smt.py lines 600–648):** The older two-pass entry
scanner, fully superseded by `is_confirmation_bar` + inline loop. Not imported or
called by backtest_smt.py or signal_smt.py. **Delete.**

**D2 — `_quarterly_session_windows` (strategy_smt.py lines 895–908):** Defined but
never called anywhere in the three files. **Delete.**

**D3 — `_mnq_bars` / `_mes_bars` module globals + `set_bar_data`:** The globals are
set but no strategy function currently reads them. The docstring says "Reserved for
multi-bar lookback logic." If no function uses them now, remove and re-add only when
a function actually needs them.

**D4 — Ghost imports in signal_smt.py:**
```python
divergence_score, _effective_div_score,
MIN_DIV_SCORE, REPLACE_THRESHOLD,
DIV_SCORE_DECAY_FACTOR, DIV_SCORE_DECAY_INTERVAL,
ADVERSE_MOVE_FULL_DECAY_PTS, ADVERSE_MOVE_MIN_DECAY,
HYPOTHESIS_INVALIDATION_PTS,
```
All imported but never directly referenced in signal_smt.py body. After the refactor
they are internal to `process_scan_bar`. Also: `detect_fvg`, `detect_displacement`,
`detect_smt_fill` — imported but not directly called (used by screen_session internally).
**Remove after Phase 5.**

### Duplicate Constants

**D5 — `MNQ_PNL_PER_POINT = 2.0` defined twice:**
- `strategy_smt.py` line 56
- `backtest_smt.py` line 99

Backtest should import from strategy. **Remove from backtest_smt.py; add to backtest import list.**

### Misplaced Code

**D6 — `print_direction_breakdown` (strategy_smt.py lines 819–850):** Only called from
`backtest_smt.py`. Not a strategy concern. **Move to backtest_smt.py.**

**D7 — `_BarRow` class (backtest_smt.py lines 46–60):** After the refactor, `process_scan_bar`
uses it for synthetic confirmation bars. **Move to strategy_smt.py; import in backtest_smt.py.**

**D8 — `SESSION_END` name collision:** `strategy_smt.py` has `SESSION_END = "15:15"`;
`signal_smt.py` has its own `SESSION_END = "13:30"` (shadow). Confusing when reading
imports. **Rename the signal_smt.py variable** to `SIGNAL_SESSION_END` or document why
it overrides.

### Duplicated Logic (within backtest_smt.py)

All of the following blocks appear 2–4× in the IDLE / WAITING_FOR_ENTRY / REENTRY_ELIGIBLE
paths. They collapse to single calls after the state machine moves into `process_scan_bar`,
but extracting them as helpers first makes the extraction safer:

**D9 — Blackout time check (3× in backtest, 1× in screen_session):**
```python
if SIGNAL_BLACKOUT_START and SIGNAL_BLACKOUT_END:
    if SIGNAL_BLACKOUT_START <= ts.strftime("%H:%M") < SIGNAL_BLACKOUT_END:
```
**Extract:** `def _in_blackout(t: time) -> bool` — also fixes P5.

**D10 — Silver-bullet window check (2×):**
```python
if SILVER_BULLET_WINDOW_ONLY:
    if not (SILVER_BULLET_START <= ts.strftime("%H:%M") < SILVER_BULLET_END):
```
**Extract:** `def _in_silver_bullet(t: time) -> bool` — also fixes P5.

**D11 — Hypothesis annotation block (~15 lines, 2×):**
```python
signal["matches_hypothesis"] = (...)
signal["hypothesis_direction"] = ...
signal["pd_range_case"] = ...
...
```
Identical in WAITING_FOR_ENTRY and REENTRY_ELIGIBLE.
**Extract:** `def _annotate_hypothesis(signal, hyp_ctx, hyp_dir) -> None`.

**D12 — Draw dict assembly + `select_draw_on_liquidity` (~20 lines, 2× in backtest, 1× in signal_smt.py):**
```python
_draws = {"fvg_top": ..., "tdo": ..., "midnight_open": ..., ...}
_tp_name, _day_tp, _sec_tp_name, _sec_tp = select_draw_on_liquidity(...)
```
**Extract:** `def _build_draws_and_select(direction, ep, sp, context, fvg_zone, run_ses_high, run_ses_low) -> tuple`.
After the refactor this lives inside `process_scan_bar` and disappears from signal_smt.py.

**D13 — Forward-limit mode determination (2×):**
```python
_use_forward_limit = (
    LIMIT_ENTRY_BUFFER_PTS is not None
    and LIMIT_EXPIRY_SECONDS is not None
    and (LIMIT_RATIO_THRESHOLD is None or _body_ratio < LIMIT_RATIO_THRESHOLD)
)
```
**Inline constant** — this expression reads only module constants and one signal field.
After extraction into `process_scan_bar` it appears once naturally.

**D14 — Contract sizing (~8 lines, 3×):**
```python
risk_per_contract = abs(signal["entry_price"] - signal["stop_price"]) * MNQ_PNL_PER_POINT
contracts = min(MAX_CONTRACTS, max(1, int(RISK_PER_TRADE / risk_per_contract))) ...
if TWO_LAYER_POSITION:
    layer_a_contracts = max(1, int(contracts * LAYER_A_FRACTION))
    ...
```
Appears in WAITING (same-bar), REENTRY (same-bar), WAITING_FOR_LIMIT_FILL (fill).
**Extract:** `def _size_contracts(entry_price, stop_price) -> tuple[int, int]` in backtest_smt.py.

**D15 — Position dict assembly (~10 lines, 3×):**
The `position = {**signal, "entry_date": day, "contracts": contracts, ...}` block with
all its field assignments appears 3×.
**Extract:** `def _open_position(signal, day, contracts, total_target, reentry_count, prior_bars_held, position_id) -> dict`.

**D16 — Displacement stop override (~8 lines, 4×):**
```python
if DISPLACEMENT_STOP_MODE and position.get("smt_type") == "displacement":
    if _extreme is not None: ...
```
Appears in WAITING forward-limit path, WAITING same-bar path, REENTRY forward-limit,
REENTRY same-bar. After `process_scan_bar` extraction this collapses to one occurrence.

**D17 — Expanded reference level scan loop (2×):**
The loop over `_ref_key` tuples + `_best_ref` accumulation is identical in IDLE
(backtest) and screen_session. After extraction into `process_scan_bar` it appears once.
**For now:** Extract `def _find_best_ref_smt(bar_idx, mes_reset, mnq_reset, ref_lvls) -> tuple | None`.

---

## Part 4 — Execution Order

### Phase 0 — Cleanup prep (low-risk, no behavior change)
These are safe to do before the main refactor and reduce noise in the diff.

- **0.1** Delete dead code: `find_entry_bar`, `_quarterly_session_windows` from strategy_smt.py.
- **0.2** Move `print_direction_breakdown` to backtest_smt.py.
- **0.3** Move `_BarRow` to strategy_smt.py; update backtest import.
- **0.4** Remove `MNQ_PNL_PER_POINT` from backtest_smt.py; add to strategy import.
- **0.5** Fix `import math as _math` placement in screen_session (move to module top).
- **0.6** Fix `import statistics` + `import datetime as _dt` placement in backtest.
- **0.7** Extract `_size_contracts` helper in backtest_smt.py (removes D14 × 3).
- **0.8** Extract `_open_position` helper in backtest_smt.py (removes D15 × 3).
- **0.9** Extract `_annotate_hypothesis` helper in backtest_smt.py (removes D11 × 2).
- **0.10** Extract `_build_draws_and_select` helper in backtest_smt.py (removes D12 × 2).
- **0.11** Replace `smt_cache` dict creation with mutable dict updated in-place (P3).
- **0.12** Replace `pd.Series` synthetic bar with `_BarRow` (P4).
- **0.13** Extract `_in_blackout(t)` + `_in_silver_bullet(t)` + pre-compute time objects (P5, D9, D10).

**Verification after Phase 0:** Backtest output must be bit-identical to pre-refactor.
Run on the full dataset; compare `total_pnl`, `total_trades`, all exit counts.

### Phase 1 — Data structures (no behavior change)
- **1.1** Add `ScanState` class to strategy_smt.py with `reset()`.
- **1.2** Add `SessionContext` dataclass to strategy_smt.py.
- **1.3** Add `build_synthetic_confirmation_bar(opens, highs, lows, closes, vols, start, end, ts) -> _BarRow`.
- **1.4** Unit tests for `ScanState.reset()` and `build_synthetic_confirmation_bar`.

### Phase 2 — Extract process_scan_bar (core work)
- **2.1** Write `process_scan_bar` by extracting and merging IDLE + WAITING_FOR_ENTRY +
  REENTRY_ELIGIBLE + WAITING_FOR_LIMIT_FILL from `run_backtest`. Use helpers from Phase 0.
  HTF state update moves into this function (operates on `state.htf_state`).
  Variable mapping — backtest local → ScanState / SessionContext field:

| Backtest local | Destination |
|---|---|
| `pending_direction` | `state.pending_direction` |
| `anchor_close` | `state.anchor_close` |
| `divergence_bar_idx` | `state.divergence_bar_idx` |
| `_conf_window_start` | `state.conf_window_start` |
| `_pending_div_bar_high/low` | `state.pending_div_bar_high/low` |
| `_pending_smt_type` | `state.pending_smt_type` |
| `_pending_fvg_zone/detected` | `state.pending_fvg_zone/detected` |
| `_pending_displacement_bar_extreme` | `state.pending_displacement_bar_extreme` |
| `_pending_div_score/provisional` | `state.pending_div_score/provisional` |
| `_pending_discovery_bar_idx/price` | `state.pending_discovery_bar_idx/price` |
| `pending_limit_signal` | `state.pending_limit_signal` |
| `_limit_bars_elapsed/max/missed` | `state.limit_bars_elapsed/max/missed` |
| `reentry_count` | `state.reentry_count` |
| `prior_trade_bars_held` | `state.prior_trade_bars_held` |
| `_bt_htf_state` | `state.htf_state` |
| `day_tdo` | `context.tdo` |
| `_day_midnight_open` | `context.midnight_open` |
| `_day_overnight` | `context.overnight` |
| `_day_pdh/pdl` | `context.pdh/pdl` |
| `_session_hyp_ctx/dir` | `context.hyp_ctx/hyp_dir` |
| `bar_seconds` | `context.bar_seconds` |
| `_bt_ref_lvls` | `context.ref_lvls` |

- **2.2** Unit tests for `process_scan_bar`:
  - IDLE → WAITING_FOR_ENTRY on divergence bar
  - WAITING → signal on confirmation bar (with and without CONFIRMATION_BAR_MINUTES>1)
  - WAITING → IDLE on `HYPOTHESIS_INVALIDATION_PTS` breach
  - Replacement: stronger divergence displaces weaker pending
  - Adverse anchor: anchor updated on adverse window close
  - `WAITING_FOR_LIMIT_FILL` → signal on fill; → expired on max_bars elapsed
  - `MIN_DIV_SCORE` gate: below-threshold divergence stays IDLE
  - Reentry count tracks correctly across consecutive stops

### Phase 3 — Wire backtest (parity verification)
- **3.1** Replace IDLE/WAITING/REENTRY/LIMIT_FILL blocks in `run_backtest` with
  `process_scan_bar` calls. Initialize `ScanState` and `SessionContext` per session.
- **3.2** Remove HTF state init/update from `run_backtest` (now inside `process_scan_bar`).
- **3.3** Fix `_prev_day` computation (P10).
- **3.4** **Parity test:** Full dataset backtest before vs. after. `total_pnl`,
  `total_trades`, `win_rate`, all exit type counts must be **bit-identical**.
  Any divergence = the new code is wrong.

### Phase 4 — Rewrite screen_session
- **4.1** Rewrite `screen_session` as a thin wrapper (~40 lines) over `process_scan_bar`.
  Add `pdh`, `pdl`, `hyp_ctx` parameters (already available at signal_smt.py call site).
- **4.2** Signal equivalence test: run both old and new `screen_session` on a sample of
  session slices; signals (entry_price, stop_price, take_profit, direction) must match.

### Phase 5 — signal_smt.py stateful upgrade
- **5.1** Add module-level `_scan_state: ScanState | None` and `_session_ctx: SessionContext | None`.
- **5.2** Create `ScanState` + `SessionContext` on session start (first bar at/after SESSION_START).
- **5.3** In `_process_scanning`: replace `screen_session(combined_mnq, ...)` with
  `process_scan_bar(_scan_state, _session_ctx, bar_idx, ...)`.
  - Remove the `pd.concat + sort_index + session_mask` block (P2).
  - Remove the `_draws` dict assembly + `select_draw_on_liquidity` call (D12).
- **5.4** On trade close: update `_scan_state.prior_trade_bars_held`; transition
  `_scan_state.scan_state` to `"REENTRY_ELIGIBLE"` or `"IDLE"` depending on move size.
- **5.5** Clean up ghost imports (D4).
- **5.6** Rename `SESSION_END` shadow in signal_smt.py to `SIGNAL_SESSION_END` (D8).

---

## What Stays Where After Refactor

### strategy_smt.py (gains)
- `_BarRow` class (moved from backtest)
- `ScanState` class
- `SessionContext` class
- `build_synthetic_confirmation_bar` helper
- `_in_blackout(t)`, `_in_silver_bullet(t)` helpers
- `_build_draws_and_select(...)` helper
- `_find_best_ref_smt(...)` helper (extracted from IDLE + screen_session)
- `process_scan_bar` (~350 lines)
- `screen_session` rewritten as ~40-line wrapper

### backtest_smt.py (keeps, loses ~670 strategy lines)
- Session loop + numpy array extraction
- Running extremes update + mutable `smt_cache` update
- `_size_contracts`, `_open_position` helpers
- `_annotate_hypothesis` helper
- `_build_trade_record`, `_build_limit_expired_record`
- Reentry eligibility check after stop (move < threshold → sets `state.scan_state`)
- Partial exit handling (stop-slide, contract reduction, PnL calc)
- `print_direction_breakdown` (moved from strategy)
- Walk-forward infrastructure, `_compute_metrics`, TSV output

### signal_smt.py (keeps, loses ~80 lines)
- IB subscriptions + tick accumulation
- Session window gating
- Startup/redetection guards, per-divergence reentry counter
- Slippage application, POSITION_FILE persistence
- Hypothesis generation timing

---

## Out of Scope

- Changing divergence detection logic or `manage_position`
- Adding new strategy features
- The `partial_exit` IN_TRADE handling (strategy concern but tightly coupled to trade
  record building; low refactor priority)
- TSV schema changes
- Parallel backtest folds (separate optimization concern)

---

## Part 5 — Bar Resolution Compatibility

The backtest runs on 1-minute bars. `signal_smt.py` runs on 1-second bars. Several
constants and lookbacks are expressed as **bar counts**, not wall-clock durations —
meaning they are 60× shorter at live resolution than intended. These must be fixed so
both callers produce semantically equivalent behaviour.

`SessionContext.bar_seconds` (added in Part 1) is the single source of truth for
conversion: `60.0` in backtest, `1.0` in live. All resolution-dependent values derive
from it at runtime.

### RC1 — `DIV_SCORE_DECAY_INTERVAL` is bar-count-based (60× faster at 1s)

**Where:** `strategy_smt.py` lines 452–473 (`_effective_div_score`):
```python
bars_held  = current_bar_idx - discovery_bar_idx
time_decay = DIV_SCORE_DECAY_FACTOR ** (bars_held // max(DIV_SCORE_DECAY_INTERVAL, 1))
```
`DIV_SCORE_DECAY_INTERVAL = 10` means "decay every 10 bars". At 1m that is 10 minutes.
At 1s that is 10 **seconds** — decay is 60× faster, making replacement extremely aggressive.

**Fix:**
1. Add a new constant: `DIV_SCORE_DECAY_SECONDS: float = 600.0` (10 minutes).
2. In `process_scan_bar` (or `_effective_div_score` if called with context), compute:
   ```python
   _decay_interval_bars = max(1, round(DIV_SCORE_DECAY_SECONDS / context.bar_seconds))
   ```
3. Pass `_decay_interval_bars` to `_effective_div_score` instead of the raw constant.
4. Keep `DIV_SCORE_DECAY_INTERVAL` as a legacy alias or remove it; it should no longer
   be read directly anywhere.

**Result:** Decay period is ~10 minutes at both 1m and 1s resolution.

### RC2 — `CONFIRMATION_BAR_MINUTES` is a bar count, not minutes

**Where:** `backtest_smt.py` lines 796–809 — the synthetic N-bar confirmation window.
The parameter name implies wall-clock minutes but the value is a **bar count**: N=3
at 1m gives a 3-minute window; at 1s it gives a 3-second window.

**Fix:**
1. Rename constant to `CONFIRMATION_WINDOW_BARS` in `strategy_smt.py`.
2. Update all references in `backtest_smt.py`, `signal_smt.py`, tests.
3. Add a comment: `# bar count — at 1s bars this is N seconds, not N minutes`.

No behaviour change in backtest (value is unchanged). At 1s: default `N=1` is
unaffected. Non-default values are already user-chosen bar counts, now honestly named.

### RC3 — `detect_fvg` lookback=20 collapses to 20 seconds at 1s

**Where:** All call sites of `detect_fvg(..., lookback=20)` inside the divergence
detection path. At 1s, the 20-bar lookback covers only 20 seconds of price history —
too narrow to detect meaningful fair-value gaps.

**Fix:** In `process_scan_bar`, compute the lookback dynamically before calling `detect_fvg`:
```python
fvg_lookback = max(3, round(1200 / context.bar_seconds))
# 1200s / 60 = 20 bars at 1m; 1200s / 1 = 1200 bars at 1s (~20 minutes)
```
Pass `fvg_lookback` to `detect_fvg` at every call site inside `process_scan_bar`.

**Result:** FVG detection always looks back ~20 minutes of price history regardless of
bar size. At 1s this correctly evaluates the full recent session context.

### RC4 — `bar_seconds` not available in `screen_session`

**Where:** `screen_session` in `strategy_smt.py` has no `bar_seconds` parameter, so
it cannot pass a meaningful `bar_seconds` to any resolution-dependent logic.

**Fix:** Resolved naturally by Part 1. `SessionContext.bar_seconds` is computed by the
caller once per session and passed to `process_scan_bar`. The caller sets:
- Backtest: `bar_seconds = 60.0` (from `bar_seconds` already computed at line 451–454)
- Live (`signal_smt.py`): `bar_seconds = 1.0`

No separate fix needed beyond Part 1 wiring.

### RC5 — `_SyntheticBar` duplicates `_BarRow` with incompatible attribute conventions

**Where:** `signal_smt.py` lines 188–202 defines `_SyntheticBar` with lowercase attrs
(`open`, `high`, `low`, `close`, `volume`). `_BarRow` (backtest, moving to strategy_smt.py
per D7) uses capitalized attrs (`Open`, `High`, `Low`, `Close`, `Volume`).

Both classes serve the same purpose: a lightweight bar holder. Having two means
`process_scan_bar` would need to handle both conventions.

**Fix (as part of D7 + Phase 5):**
1. Move `_BarRow` to strategy_smt.py (already in D7).
2. Verify all `process_scan_bar` internal accesses use capitalized attrs (they will,
   since the logic is extracted from backtest).
3. In `signal_smt.py`, replace `_SyntheticBar(...)` construction with `_BarRow(...)`,
   mapping `open→Open`, `high→High`, etc.
4. Delete `_SyntheticBar`.

**Result:** One bar holder type, one attribute convention, no adapter layer.

### RC6 — `pd.Series` construction in `_process_managing` on every 1s bar

**Where:** `signal_smt.py` lines 598–607:
```python
_bar_ser = pd.Series({
    "Open": float(_bar.open), "High": float(_bar.high),
    "Low": float(_bar.low), "Close": float(_bar.close),
    "Volume": int(_bar.volume),
}, name=_ts)
result = manage_position(_position, _bar_ser)
```
23,400 `pd.Series` allocations per 6.5-hour session (one per 1s bar while IN_TRADE).
`manage_position` only accesses `bar["High"]`, `bar["Low"]`, `bar["Close"]`, `bar.name`
— all of which `_BarRow` supports via `__getitem__` and `.name`.

**Fix (depends on RC5):**
After RC5, `_SyntheticBar` is replaced with `_BarRow`. Change `_process_managing` to:
```python
_bar_row = _BarRow(
    Open=float(_bar.open), High=float(_bar.high),
    Low=float(_bar.low), Close=float(_bar.close),
    Volume=int(_bar.volume), ts=_ts,
)
result = manage_position(_position, _bar_row)
```
No `pd.Series` creation. `manage_position` is unchanged.

### Resolution fix execution order

RC4 is free (falls out of Part 1). The rest slot into the existing phases:

| Item | When |
|------|------|
| RC2 (rename `CONFIRMATION_WINDOW_BARS`) | Phase 0 — low-risk rename |
| RC5 (consolidate `_BarRow` / `_SyntheticBar`) | Phase 5 — signal_smt.py upgrade |
| RC6 (remove `pd.Series` in `_process_managing`) | Phase 5 — after RC5 |
| RC1 (`DIV_SCORE_DECAY_SECONDS`) | Phase 2 — add constant + pass to `_effective_div_score` |
| RC3 (`fvg_lookback` dynamic) | Phase 2 — inside `process_scan_bar` |

---

## Results Impact Analysis

### Backtest results after refactor

**No change.** The Phase 3 parity test enforces bit-identical output on the full dataset
before the branch can proceed. The refactor moves code; it does not alter strategy logic.

### Live (signal_smt.py) results after refactor

**Intentional improvement.** The current `screen_session` silently omits 8+ features
that exist in the backtest state machine. After the refactor, live uses the same
`process_scan_bar` as backtest. Specific changes:

| Change | Direction | Mechanism |
|--------|-----------|-----------|
| Replacement hypothesis (Sol A+B) now active | Better | Stronger divergences replace weaker ones; stale signals discarded |
| `MIN_DIV_SCORE` gate now active | Better | Low-quality divergences filtered before entry |
| `HYPOTHESIS_INVALIDATION_PTS` (Sol D) now active | Better | Price breach before confirmation cancels signal |
| `DIV_SCORE_DECAY` + adverse-move decay now active | Better | Signal quality degrades as price moves adversely |
| Adverse anchor update now active | Better | Anchor tracks the actual sweep extreme, not the discovery price |
| `select_draw_on_liquidity` now active | Better | Optimal TP selected from full draw set (not old cascade) |
| `secondary_target` now populated | Better | Gives position manager a second exit level |
| `WAITING_FOR_LIMIT_FILL` state now active | Better | Limit order tracking: fill confirmation, expiry detection |
| `CONFIRMATION_BAR_MINUTES` window now active | Better | At 1s bars: confirmation uses a proper N-bar window (seconds-granularity) |

**Without RC1–RC3 fixes**, two issues remain at 1s resolution:
- `DIV_SCORE_DECAY` is 60× faster (10s decay epoch vs 10-minute intended) — replacement
  becomes hyper-aggressive, may discard valid signals prematurely.
- `detect_fvg` lookback is 20s — almost never detects a gap; FVG zone will be `None`
  on most bars, degrading entry precision slightly (entry falls back to non-FVG mode).

**With RC1–RC3 fixes**, live results should match backtest signal quality in expectation,
with additionally better timing precision because 1s bars allow confirmation to trigger
seconds after the signal bar rather than waiting for the next 1-minute close.

---

## ACCEPTANCE CRITERIA

### Phase 0 — Cleanup (no behavior change)
- [ ] `find_entry_bar` and `_quarterly_session_windows` deleted from strategy_smt.py
- [ ] `print_direction_breakdown` moved to backtest_smt.py (not in strategy_smt.py)
- [ ] `_BarRow` in strategy_smt.py; backtest_smt.py imports it from there
- [ ] `MNQ_PNL_PER_POINT` defined only in strategy_smt.py; backtest imports it
- [ ] `import math` at module top of strategy_smt.py (not inside screen_session)
- [ ] `import statistics` and `import datetime` at module top of backtest_smt.py
- [ ] `_size_contracts(entry_price, stop_price)` helper in backtest_smt.py
- [ ] `_open_position(...)` helper in backtest_smt.py
- [ ] `_annotate_hypothesis(signal, hyp_ctx, hyp_dir)` helper in backtest_smt.py
- [ ] `_build_draws_and_select(...)` helper in backtest_smt.py
- [ ] `smt_cache` dict created once per session, updated in-place per bar
- [ ] Synthetic confirmation bar uses `_BarRow` not `pd.Series`
- [ ] `_in_blackout(t)` and `_in_silver_bullet(t)` helpers; time objects pre-computed
- [ ] `CONFIRMATION_BAR_MINUTES` renamed to `CONFIRMATION_WINDOW_BARS` everywhere
- [ ] Phase 0 parity: test suite passes with no new failures vs 675-passing baseline

### Phase 1 — Data structures
- [ ] `ScanState` class in strategy_smt.py with all 26+ fields and `reset()` method
- [ ] `SessionContext` dataclass in strategy_smt.py with `bar_seconds` field
- [ ] `build_synthetic_confirmation_bar(...)` helper in strategy_smt.py
- [ ] Unit tests for `ScanState.reset()` and `build_synthetic_confirmation_bar` pass

### Phase 2 — process_scan_bar
- [ ] `process_scan_bar(state, context, ...)` function in strategy_smt.py
- [ ] Contains all IDLE / WAITING_FOR_ENTRY / REENTRY_ELIGIBLE / WAITING_FOR_LIMIT_FILL logic
- [ ] Returns `None`, `{"type": "signal", ...}`, or `{"type": "expired", ...}`
- [ ] `DIV_SCORE_DECAY_SECONDS = 600.0` constant added; `_effective_div_score` uses it
- [ ] `fvg_lookback` computed as `max(3, round(1200 / context.bar_seconds))` in the function
- [ ] Unit tests for process_scan_bar (8 cases from plan section 2.2) pass

### Phase 3 — Wire backtest (parity gate)
- [ ] `run_backtest` uses `process_scan_bar`; IDLE/WAITING/REENTRY/LIMIT_FILL inlined blocks removed
- [ ] HTF state init/update moved inside process_scan_bar
- [ ] `_prev_day` computed via `datetime.timedelta` (P10 fix)
- [ ] Parity test: full-dataset backtest output bit-identical to Phase 0 baseline

### Phase 4 — screen_session rewrite
- [ ] `screen_session` is ≤50 lines wrapping `process_scan_bar`
- [ ] Signal equivalence test on sample sessions passes

### Phase 5 — signal_smt.py upgrade
- [ ] `_scan_state: ScanState` and `_session_ctx: SessionContext` module-level in signal_smt.py
- [ ] `_process_scanning` calls `process_scan_bar` (no `screen_session` call, no pd.concat)
- [ ] `_SyntheticBar` replaced by `_BarRow`; `pd.Series` removed from `_process_managing`
- [ ] Ghost imports (D4) removed
- [ ] `SESSION_END` shadow renamed to `SIGNAL_SESSION_END`
- [ ] All RC1–RC6 fixes applied and verifiable

### Overall
- [ ] Test suite: ≥675 passing (no new failures introduced)
- [ ] No debug print statements added
- [ ] All files importable with no syntax errors

---

## Complexity: 🔴 High

~670 lines move from backtest → strategy_smt.py. Behavioral equivalence must be verified
numerically (Phase 3 parity test). Recommended: complete Phase 0 cleanup and verify
parity before starting Phase 2 extraction — this ensures any parity failure in Phase 3
is attributable to the extraction, not to pre-existing divergence.
