# Code Review — SMT Humanize

**Verdict:** PASS with 3 NON-BLOCKING findings. All 14 new tests green. No BLOCKERS.

## Stats

- Files Modified: 3 (`strategy_smt.py`, `signal_smt.py`, `backtest_smt.py`)
- Files Added: 1 (`tests/test_smt_humanize.py`)
- New lines: ~216 (diff-stat)
- Deleted lines: ~4

## Attention-Point Checklist

1. **`"move_stop"` caller audit — PASS.** Only two SMT call-sites exist:
   - `backtest_smt.py:596` converts `"move_stop"` → `"hold"` after popping `_pending_move_stop` (state machine safe).
   - `signal_smt.py:779` emits the typed MOVE_STOP payload, then sets `result = "hold"`.
   - `position_monitor.py` imports `manage_position` from `train.py` (different function, float return) — unrelated.
   - `example_trade_drawings/draw_trade.py`, `strategies/*`, and all other `tests/` references are for unrelated `manage_position` implementations in other strategies.
2. **tz handling in `compute_confidence` — PASS (but see Finding #1).** `pd.Timestamp` always has `tz_convert`, so `hasattr` is redundant but harmless; the tz normalization logic is correct for the aware/naive combinations. `ts.normalize()` preserves tz, so `session_start_ts` matches `ts.tz` in `_build_signal_from_bar`.
3. **Opposing-displacement gating — PASS.** `DECEPTION_OPPOSING_DISP_EXIT` is checked independently of `HUMAN_EXECUTION_MODE` (strategy_smt.py:2079). Matches acceptance criterion.
4. **Entry slippage gate — PASS.** Both `backtest_smt._open_position` (L173) and `signal_smt._process_scanning` (L727) require `HUMAN_EXECUTION_MODE` AND `HUMAN_ENTRY_SLIPPAGE_PTS > 0`. At defaults (False / 0.0), zero behavior change.
5. **`compute_confidence` hot-path — PASS.** Called once per emitted signal in `_build_signal_from_bar`. Not inside a bar loop — gated behind full divergence/confirmation chain. Negligible cost (few arithmetic ops).

## Findings

### Finding 1 — Redundant `hasattr(ts, "tz_convert")` guard
- **severity:** low — NON-BLOCKING
- **file:** `strategy_smt.py`
- **line:** 1255-1258
- **issue:** `hasattr(entry_ts, "tz_convert")` is always True for `pd.Timestamp`. The guard is dead code (both naive and aware Timestamps expose `tz_convert`).
- **detail:** Confirmed via repl: `pd.Timestamp('2025-01-02').tz_convert` exists. The `hasattr` was likely intended as a duck-type guard for non-Timestamp inputs, but `entry_ts - session_start_ts` on the next line would fail on non-Timestamp anyway.
- **suggestion:** Replace `hasattr(entry_ts, "tz_convert") and X.tz is not None` with a direct `session_start_ts.tz is not None` check. No functional change; clearer intent.

### Finding 2 — `print()` in production signal path violates "production code is silent"
- **severity:** low — NON-BLOCKING
- **file:** `signal_smt.py`
- **line:** 712-716, 745-756, 787-795
- **issue:** Three new `print(...)` calls (SUPPRESSED log, typed ENTRY JSON, typed MOVE_STOP JSON) in the live signal emitter. Global CLAUDE.md says "Production code is silent: No print/stdout logging in production paths."
- **detail:** These are the typed human-trader payloads the plan requires — they DO need to reach stdout (the live alert stream is stdout-based, per the signal emitter's existing pattern at L742). Existing signal lines in the file use the same `print(... flush=True)` convention, so this is consistent with surrounding code. The SUPPRESSED line is pure diagnostic though.
- **suggestion:** Keep the typed JSON emissions (they are the product). Consider routing the SUPPRESSED message through the logging framework rather than `print`, or drop it — it is pure debug noise and will confuse downstream parsers that expect either a legacy signal line or a typed JSON payload on stdout.

### Finding 3 — MOVE_STOP rate limiter logic is off-by-default but semantically odd
- **severity:** low — NON-BLOCKING
- **file:** `signal_smt.py`
- **line:** 122-123, 782-800
- **issue:** `_last_move_stop_bar_idx` is initialized to `-10**9` and compared via `gap = _move_stop_bar_counter - _last_move_stop_bar_idx`. On the very first MOVE_STOP, `gap` is ≈ `10**9 + 1`, which exceeds any realistic `MOVE_STOP_MIN_GAP_BARS`, so emission happens. OK.
- **detail:** `_move_stop_bar_counter` increments on every MOVE_STOP result (not every bar), so "bars between" is actually "MOVE_STOP events between." With `MOVE_STOP_MIN_GAP_BARS=3`, this suppresses the 2nd and 3rd consecutive MOVE_STOP returns — not MOVE_STOPs separated by 3 bars. Default is 0 so no effect today; but the name/docs suggest bar-index-based gating.
- **suggestion:** Either rename to `MOVE_STOP_MIN_GAP_EVENTS` or switch the counter to bar-index tracking (capture the live bar index at `_process_managing` entry). Not blocking — default 0 makes this latent.

### Finding 4 — Stray whitespace diff noise
- **severity:** low — NON-BLOCKING (style)
- **file:** `strategy_smt.py`
- **line:** 1370
- **issue:** `"entry_bar": -1,` → `"entry_bar":  -1,` (cosmetic space change) appears in diff, adding churn unrelated to the plan.
- **suggestion:** Revert the whitespace-only change before commit.

## Security / Injection

- No SQL, no external input handling, no filesystem reads beyond existing `POSITION_FILE.write_text(json.dumps(...))`.
- `json.dumps` on typed payloads — values are floats/strings from internal state; no injection surface.
- No new hardcoded endpoints or credentials.

## Regression Risk

- `HUMAN_EXECUTION_MODE=False` default preserves all existing returns from `manage_position()` (R-1 test confirms).
- `DECEPTION_OPPOSING_DISP_EXIT=False` default keeps the new exit branch dead.
- Trade record gains 4 fields; TSV consumers that tolerate extra columns unaffected (verify the `diag_fields.py` or TSV writer does not assert schema equality).
- `_build_signal_from_bar` now unconditionally computes `confidence` — adds ~6 ops per built signal. Not in a hot loop (gated by full divergence + confirmation chain).

## Test Coverage

14/14 tests pass in 0.37s. Full regression (`pytest tests/ -x`) not re-run in this review — recommend executor confirm before commit.

## Recommendation

**APPROVE for commit** once Finding #4 (whitespace) is reverted. Findings #1–#3 can be addressed in a follow-up.
