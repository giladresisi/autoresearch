# Plan: SMT Structural Fixes & Signal Quality

**Status**: ✅ Planned
**Complexity**: 🔴 Complex
**Started**: 2026-04-21

## Summary

Combines two streams of work that must be measured together:

1. **Findings fixes** (findings.md F1, F2a/b, F2c, F3): wire live reentry guard, correct look-ahead bias in TP selection, replace disabled invalidation threshold, add pessimistic fill simulation.
2. **Structural signal quality** (structural_moves.md S1–S5): symmetric SMT detection, expanded reference levels, HTF visibility filter, displacement body size field + filter, always-on confirmation candle.

All new behaviours are behind opt-in config flags (default=False) to allow independent measurement via the walk-forward optimizer. The metric baseline is captured after this plan **and** the humanize plan are both merged — not between them.

## User Story

As a strategy researcher, I want the backtester to simulate realistic fills and the signal detector to fire only on structurally motivated moves, so that walk-forward metrics reflect real-world outcomes and the optimizer search space correctly represents each lever.

## Acceptance Criteria

- [ ] `MIDNIGHT_OPEN_AS_TP = True` is the new default; pre-9:30 signals no longer reference a future price.
- [ ] `REENTRY_MAX_MOVE_RATIO` replaces the role of the 999 sentinel; invalidation threshold is computed as a fraction of entry-to-TP distance.
- [ ] `signal_smt.py` tracks `_divergence_reentry_count` per defended level and enforces `MAX_REENTRY_COUNT` in the live path.
- [ ] With `PESSIMISTIC_FILLS=True`, stop exits fill at `bar["Low"]` (long) / `bar["High"]` (short) and TP exits fill at `bar["High"]` (long) / `bar["Low"]` (short); flag is added to trade record.
- [ ] `SYMMETRIC_SMT_ENABLED=True` detects divergences where MNQ leads and MES fails, not only the reverse.
- [ ] `EXPANDED_REFERENCE_LEVELS=True` checks sweeps against quarterly sessions, calendar day, and current calendar week H/L.
- [ ] `HTF_VISIBILITY_REQUIRED=True` suppresses signals not visible on any of 15m / 30m / 1h / 4h.
- [ ] `displacement_body_pts` is recorded on every trade (always-on diagnostic); `MIN_DISPLACEMENT_BODY_PTS > 0` filters small-body displacements.
- [ ] `ALWAYS_REQUIRE_CONFIRMATION=True` requires a confirmation bar for all entries, not just delayed ones.
- [ ] When `HIDDEN_SMT_ENABLED=True`, hidden SMT signals (body/close-based divergence) are subject to the same `EXPANDED_REFERENCE_LEVELS` and `HTF_VISIBILITY_REQUIRED` gates as wick-based signals, using close-price extremes per reference window.
- [ ] Any future signal type (FVG / SMT fill) added to the signal path must pass both `EXPANDED_REFERENCE_LEVELS` and `HTF_VISIBILITY_REQUIRED` gates at the `screen_session()` call site before a signal dict is built. This is a standing architectural rule, not a flag.
- [ ] All existing tests still pass; ≥ 22 new tests cover the new code paths.

## Execution Agent Rules

- Make ALL code changes required by the plan.
- Delete debug logs added during execution (keep pre-existing ones).
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`.

---

## Codebase Context

| File | Role |
|---|---|
| `strategy_smt.py` | Config constants (top ~200 lines); `detect_smt_divergence()` (~L372); `detect_displacement()` (~L568); `screen_session()` (~L696–857); `_build_signal_from_bar()` (~L860); `manage_position()` (~L946); `compute_tdo()` / `compute_midnight_open()` (~L474–505) |
| `backtest_smt.py` | State machine loop (~L349–754); `_build_trade_record()` + exit block (~L119–187); TDO selection (~L285–292); REENTRY_ELIGIBLE transitions (~L448–466, L562–650) |
| `signal_smt.py` | `_last_exit_ts` guard (~L491); `_apply_slippage()` (~L99–106); no `MAX_REENTRY_COUNT` check currently |
| `tests/` | 551 passing; relevant suites: `test_smt_strategy.py`, `test_smt_backtest.py`, `test_signal_smt.py` |

---

## Implementation Tasks

### Wave 1 — Isolated single-file fixes (parallel)

Each task in this wave edits a different file or non-overlapping function.

---

#### Task 1.1 — Pessimistic fill prices (F3) [`backtest_smt.py`]

**WAVE**: 1 | **AGENT_ROLE**: executor | **DEPENDS_ON**: none

In `strategy_smt.py` config block add:
```python
PESSIMISTIC_FILLS: bool = True
```

In `backtest_smt.py:_build_trade_record()` change the exit price block:
```python
if exit_result == "exit_tp":
    if PESSIMISTIC_FILLS:
        exit_price = float(exit_bar["High"]) if position["direction"] == "long" else float(exit_bar["Low"])
    else:
        exit_price = position["take_profit"]
elif exit_result == "exit_stop":
    if PESSIMISTIC_FILLS:
        exit_price = float(exit_bar["Low"]) if position["direction"] == "long" else float(exit_bar["High"])
    else:
        exit_price = position["stop_price"]
else:
    exit_price = float(exit_bar["Close"])
```

Add `"pessimistic_fills": PESSIMISTIC_FILLS` to the trade dict and to the TSV header in `_write_trades_tsv()`.

Import `PESSIMISTIC_FILLS` from `strategy_smt`.

---

#### Task 1.2 — Live reentry guard (F1) [`signal_smt.py`]

**WAVE**: 1 | **AGENT_ROLE**: executor | **DEPENDS_ON**: none

Add module-level state (near the other `_last_exit_ts` declaration):
```python
_current_divergence_level: float | None = None
_divergence_reentry_count: int = 0
```

In the session reset function (wherever `_last_exit_ts` is reset), also reset both new vars to their initial values.

In `_process_scanning()`, after the existing `signal["entry_time"] <= _last_exit_ts` guard, add:
```python
defended = signal.get("smt_defended_level")
if defended != _current_divergence_level:
    _current_divergence_level = defended
    _divergence_reentry_count = 0
else:
    _divergence_reentry_count += 1
    if MAX_REENTRY_COUNT < 999 and _divergence_reentry_count >= MAX_REENTRY_COUNT:
        return  # block re-entry on same divergence
```

Import `MAX_REENTRY_COUNT` from `strategy_smt`.

---

#### Task 1.3 — Midnight open as primary TP default (F2a/b) [`strategy_smt.py` constant only]

**WAVE**: 1 | **AGENT_ROLE**: executor | **DEPENDS_ON**: none

Change the constant:
```python
MIDNIGHT_OPEN_AS_TP: bool = True   # was False
```

No other code change needed — `backtest_smt.py` already branches on this flag at the TP selection site (~L285–292). Verify that `signal_smt.py` also reads this flag (or inherits via import) and uses `compute_midnight_open()` when True; fix if not.

---

#### Task 1.4 — Ratio-based reentry invalidation threshold (F2c) [`strategy_smt.py` + `backtest_smt.py`]

**WAVE**: 1 | **AGENT_ROLE**: executor | **DEPENDS_ON**: none

In `strategy_smt.py` config, add and replace:
```python
REENTRY_MAX_MOVE_RATIO: float = 0.5   # fraction of entry-to-TP distance; replaces hard 999 pts sentinel
# REENTRY_MAX_MOVE_PTS = 999          # deprecated — remove or keep as 999 for backward compat
```

In `backtest_smt.py` REENTRY_ELIGIBLE stop-out transition (~L448–466), replace the move threshold check:
```python
entry_to_tp = abs(position["entry_price"] - position["tdo"])
move_threshold = REENTRY_MAX_MOVE_RATIO * entry_to_tp if REENTRY_MAX_MOVE_RATIO < 99 else 9999
if move < move_threshold:
    state = "REENTRY_ELIGIBLE"
    ...
else:
    state = "IDLE"
```

Apply the same threshold logic in `signal_smt.py` if it has a parallel move-distance check; add it if missing.

Import `REENTRY_MAX_MOVE_RATIO` from `strategy_smt`.

---

### Wave 2 — strategy_smt.py structural changes (sequential — same file)

Tasks 2.1–2.5 must run in order (all touch `strategy_smt.py`).

---

#### Task 2.1 — Symmetric SMT detection (S1) [`strategy_smt.py:detect_smt_divergence()`]

**WAVE**: 2 | **AGENT_ROLE**: executor | **DEPENDS_ON**: Wave 1 complete

In `strategy_smt.py` config add:
```python
SYMMETRIC_SMT_ENABLED: bool = False
```

In `detect_smt_divergence()` (~L372–419), after the existing MES-leads check, add MNQ-leads variants when flag is enabled:
- Bearish: `cur_mnq["High"] > mnq_session_high AND cur_mes["High"] <= mes_session_high`
- Bullish: `cur_mnq["Low"] < mnq_session_low AND cur_mes["Low"] >= mes_session_low`

Return the same tuple `(direction, sweep_pts, miss_pts, smt_type, smt_defended_level)`. For MNQ-leads bearish, `smt_defended_level = mes_session_high`; for bullish, `mes_session_low`. Mark `smt_type` as `"wick_sym"` to distinguish.

---

#### Task 2.2 — Displacement body size field (S4) [`strategy_smt.py:_build_signal_from_bar()` + `detect_displacement()`]

**WAVE**: 2 | **AGENT_ROLE**: executor | **DEPENDS_ON**: Task 2.1

In `strategy_smt.py` config add:
```python
MIN_DISPLACEMENT_BODY_PTS: float = 0.0   # 0 = disabled; 12–15 pts recommended once data is available
```

In `detect_displacement()` (~L568–586): after the existing body size check, add:
```python
if MIN_DISPLACEMENT_BODY_PTS > 0 and body < MIN_DISPLACEMENT_BODY_PTS:
    return False
```

In `_build_signal_from_bar()` (~L860): compute and add to the returned dict:
```python
"displacement_body_pts": round(abs(float(bar["Close"]) - float(bar["Open"])), 2),
```

---

#### Task 2.3 — Always-on confirmation candle (S5) [`strategy_smt.py:screen_session()`]

**WAVE**: 2 | **AGENT_ROLE**: executor | **DEPENDS_ON**: Task 2.2

In `strategy_smt.py` config add:
```python
ALWAYS_REQUIRE_CONFIRMATION: bool = False
```

In `screen_session()`, in the forward scan for confirmation bar: when `ALWAYS_REQUIRE_CONFIRMATION=True`, require that the candidate bar breaks the displacement bar's body boundary before calling `is_confirmation_bar()`:
- Short: candidate bar's Close < displacement bar's body low (`min(Open, Close)` of displacement bar)
- Long: candidate bar's Close > displacement bar's body high (`max(Open, Close)` of displacement bar)

The existing confirmation bar scan already has a `divergence_bar_idx` pointer — use that index to look up the displacement bar's Open/Close.

---

#### Task 2.4 — Expanded reference levels (S2) [`strategy_smt.py:screen_session()`]

**WAVE**: 2 | **AGENT_ROLE**: executor | **DEPENDS_ON**: Task 2.3

In `strategy_smt.py` config add:
```python
EXPANDED_REFERENCE_LEVELS: bool = False
```

Add a helper `_quarterly_session_windows()` that returns the 4×6h session boundaries (Asia 18:00–0:00, London 0:00–6:00, NY Morning 6:00–12:00, NY Evening 12:00–18:00) as ET timestamps for a given date.

In `screen_session()`, before the bar loop, pre-compute reference level cache:
```python
_ref_levels = {
    "prev_session_mes_high": ..., "prev_session_mes_low": ...,
    "prev_session_mnq_high": ..., "prev_session_mnq_low": ...,
    "prev_day_mes_high": ...,    "prev_day_mes_low": ...,
    "prev_day_mnq_high": ...,    "prev_day_mnq_low": ...,
    "week_mes_high": ...,        "week_mes_low": ...,
    "week_mnq_high": ...,        "week_mnq_low": ...,
    # current session / current day updated incrementally in loop
}
```

Snapshots for "prev" levels are computed from pre-loaded bar data passed into `screen_session()` (or computed from the full day slice before the loop). Current-window running extremes are updated O(1) per bar.

In the divergence detection call when `EXPANDED_REFERENCE_LEVELS=True`, pass each reference level pair through `detect_smt_divergence()` in addition to the current session extreme. Collect all non-None results; if any returns a signal direction, use the one with the largest sweep_pts.

**Hidden SMT (body-based):** when `HIDDEN_SMT_ENABLED=True`, maintain a parallel set of close-price running extremes per reference window (`prev_session_mes_close_high`, `prev_day_mes_close_high`, etc.). Pass these to the body-based divergence check the same way wick extremes are passed to `detect_smt_divergence()`. Hidden SMT signals from any reference window are valid; use the largest close-sweep as the winner.

**FVG / SMT fill (future):** when FVG entry logic is added, it must query the same `_ref_levels` cache and only build a signal if the FVG being filled sits within a reference window where a divergence qualifies. Do not add FVG to the signal path without this gate.

Note: `screen_session()` currently receives only today's bars. The prev-day and prev-session data must be passed from `backtest_smt.py`'s day loop — add optional parameters with default `None` (backward compatible).

---

#### Task 2.5 — HTF visibility filter (S3) [`strategy_smt.py:screen_session()`]

**WAVE**: 2 | **AGENT_ROLE**: executor | **DEPENDS_ON**: Task 2.4

In `strategy_smt.py` config add:
```python
HTF_VISIBILITY_REQUIRED: bool = False
HTF_PERIODS_MINUTES: list[int] = [15, 30, 60, 240]
```

In `screen_session()`, add incremental running max/min for each HTF period. For a given bar at time `t`, the current HTF period started at `floor(t / T) * T`. At each bar:
- If `t` crosses a period boundary for timeframe T: snapshot prior-period extreme; reset current-period running extreme.
- Else: update current-period running extreme.

HTF visibility check at signal time: for each T in `HTF_PERIODS_MINUTES`, check if the divergence is visible (same logic as S1/S2 but using HTF-period extremes). If `HTF_VISIBILITY_REQUIRED=True` and no timeframe shows divergence, skip the signal (continue outer loop without building signal).

**Hidden SMT (body-based):** maintain a parallel set of close-price running extremes per HTF period (current-period max-close / min-close for MES and MNQ, plus prior-period snapshots). HTF visibility for a hidden SMT signal is satisfied when `current_T_period_MES_close_high > prior_T_period_MES_close_high AND current_T_period_MNQ_close_high ≤ prior_T_period_MNQ_close_high` (or symmetric/bullish variants). Pass if any T satisfies this.

**FVG / SMT fill (future):** any FVG signal must also satisfy the HTF visibility check before a signal dict is built. The check for an FVG entry is that the FVG itself (the gap between candle 1's close and candle 3's open) is visible as a structural feature on at least one HTF — implementation detail to be specified when FVG is designed.

Store the list of timeframes that confirmed visibility as `"htf_confirmed_timeframes"` in the signal dict (diagnostic, always recorded when filter is enabled).

---

### Wave 3 — Output and backtest wiring (parallel)

---

#### Task 3.1 — Trade record output update [`backtest_smt.py:_build_trade_record()` + `_write_trades_tsv()`]

**WAVE**: 3 | **AGENT_ROLE**: executor | **DEPENDS_ON**: Wave 2 complete

Add to the trade dict (populated from position/signal):
```python
"displacement_body_pts": position.get("displacement_body_pts"),
"pessimistic_fills": PESSIMISTIC_FILLS,
```

Add both columns to the TSV header list in `_write_trades_tsv()` (maintain existing column order; append new columns at end).

---

#### Task 3.2 — backtest_smt.py: pass prev-day/prev-session data to screen_session [`backtest_smt.py`]

**WAVE**: 3 | **AGENT_ROLE**: executor | **DEPENDS_ON**: Task 2.4

In the day loop in `backtest_smt.py`, compute `prev_day_mes` and `prev_day_mnq` bar slices (previous calendar day) and the previous quarterly session slices. Pass these to `screen_session()` as keyword args.

`screen_session()` signature change: add `prev_day_mes: pd.DataFrame | None = None`, `prev_day_mnq: pd.DataFrame | None = None`, `prev_session_mes: pd.DataFrame | None = None`, `prev_session_mnq: pd.DataFrame | None = None`. Default None = use only current-session running extreme (existing behavior when `EXPANDED_REFERENCE_LEVELS=False`).

---

### Wave 4 — Tests

**WAVE**: 4 | **AGENT_ROLE**: executor | **DEPENDS_ON**: Wave 3 complete

File: `tests/test_smt_structural_fixes.py` (new file)

#### F3 — Pessimistic fills

| # | Test | Tool |
|---|---|---|
| F3-1 | With PESSIMISTIC_FILLS=True, a long stop-out records exit_price = bar["Low"], not stop_price | pytest |
| F3-2 | With PESSIMISTIC_FILLS=True, a long TP records exit_price = bar["High"], not take_profit | pytest |
| F3-3 | With PESSIMISTIC_FILLS=False, exit_price equals exact stop/TP level (existing behavior preserved) | pytest |
| F3-4 | Trade record includes "pessimistic_fills" column | pytest |

#### F1 — Live reentry guard

| # | Test | Tool |
|---|---|---|
| F1-1 | Second signal on same smt_defended_level is counted as reentry | pytest |
| F1-2 | Signal on different smt_defended_level resets count to 0 | pytest |
| F1-3 | When count >= MAX_REENTRY_COUNT, signal is blocked | pytest |
| F1-4 | Session reset clears divergence state | pytest |

#### F2a/b — Midnight open default

| # | Test | Tool |
|---|---|---|
| F2-1 | With MIDNIGHT_OPEN_AS_TP=True (new default), TP equals midnight open bar's Open | pytest |
| F2-2 | With MIDNIGHT_OPEN_AS_TP=False, TP equals 9:30 bar's Open (backward compat) | pytest |

#### F2c — Ratio-based invalidation threshold

| # | Test | Tool |
|---|---|---|
| F2c-1 | Move of 49% of entry-to-TP distance leaves state REENTRY_ELIGIBLE | pytest |
| F2c-2 | Move of 51% of entry-to-TP distance transitions state to IDLE | pytest |
| F2c-3 | REENTRY_MAX_MOVE_RATIO >= 99 disables the check (9999-pt effective threshold) | pytest |

#### S1 — Symmetric SMT

| # | Test | Tool |
|---|---|---|
| S1-1 | MNQ-leads bearish: MNQ makes new high, MES fails → signal detected when SYMMETRIC_SMT_ENABLED=True | pytest |
| S1-2 | MNQ-leads variants not detected when SYMMETRIC_SMT_ENABLED=False | pytest |
| S1-3 | smt_type = "wick_sym" for MNQ-leads signals | pytest |

#### S4 — Displacement body size

| # | Test | Tool |
|---|---|---|
| S4-1 | displacement_body_pts recorded correctly (|Close - Open| of displacement bar) | pytest |
| S4-2 | With MIN_DISPLACEMENT_BODY_PTS=15, candle with 10pt body is rejected | pytest |
| S4-3 | MIN_DISPLACEMENT_BODY_PTS=0 disables filter | pytest |

#### S5 — Always-on confirmation

| # | Test | Tool |
|---|---|---|
| S5-1 | With ALWAYS_REQUIRE_CONFIRMATION=True, bar that is bullish but doesn't break displacement body high is not used as confirmation | pytest |
| S5-2 | With ALWAYS_REQUIRE_CONFIRMATION=False, existing confirmation logic unchanged | pytest |

#### S2 — Expanded reference levels

| # | Test | Tool |
|---|---|---|
| S2-1 | Sweep of prev-day high with EXPANDED_REFERENCE_LEVELS=True triggers signal | pytest |
| S2-2 | EXPANDED_REFERENCE_LEVELS=False uses only current-session extreme (existing behavior) | pytest |
| S2-3 | Hidden SMT (body-based): close-price sweep of prev-day close extreme triggers signal when EXPANDED_REFERENCE_LEVELS=True and HIDDEN_SMT_ENABLED=True | pytest |

#### S3 — HTF visibility

| # | Test | Tool |
|---|---|---|
| S3-1 | Signal visible on 15m timeframe passes filter | pytest |
| S3-2 | Signal visible on no HTF is suppressed when HTF_VISIBILITY_REQUIRED=True | pytest |
| S3-3 | htf_confirmed_timeframes logged in signal dict | pytest |
| S3-4 | Hidden SMT signal visible on 15m (close-price extreme) passes HTF filter when HIDDEN_SMT_ENABLED=True | pytest |

#### Regression

| # | Test | Tool |
|---|---|---|
| R-1 | Full test suite passes with all new flags at default (False/0) — no regressions | `pytest tests/` |

---

## Risk Assessment

| Risk | Severity | Mitigation |
|---|---|---|
| S2 requires prev-day data in screen_session() — breaking signature change | Medium | Add keyword args with None defaults; backtest passes data, signal_smt passes what it has available |
| S3 (HTF filter) boundary logic has subtle off-by-one on period resets | Medium | Unit test period boundaries explicitly; test with 1s-bar data where resets fire often |
| F3 pessimistic TP fills may report exit_price above take_profit (favourable gap) — optimizer may not expect this | Low | Document in trade record; pessimistic_fills=True is the default so optimizer always sees consistent fills |
| F2c ratio threshold of 0.5 may be too tight or too loose — arbitrary initial value | Low | It's an optimizable parameter; first run will surface whether 0.5 is useful |
| Wave 2 tasks are sequential — an error in Task 2.4 blocks 2.5 | Medium | Verify each task's tests pass before proceeding to next |

## Test Automation Summary

- **Total new test cases**: 26
- **Automated**: 24 (100%) — all via pytest with synthetic bar fixtures
- **Manual**: 0
- **Run command**: `pytest tests/test_smt_structural_fixes.py -v`
- **Full regression**: `pytest tests/ -x`
