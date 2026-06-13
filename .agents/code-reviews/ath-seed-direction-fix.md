# Code Review — GIL-23 ATH seed / rule2b direction diagnostics

Worktree: `C:\Users\gilad\projects\auto-co-trader\ath-seed-direction-fix`
Branch: `autoresearch/ath-seed-direction-fix`

## Stats
- Files Modified (source): 2 (hypothesis.py, session_pipeline.py)
- Files Modified (tests): 2 (tests/test_session_pipeline.py, tests/test_smt_hypothesis.py)
- Files Modified (other): 1 (feature.md)
- Files Added: 0 source (1 untracked plan: .agents/plans/ath-seed-direction-fix.md)
- New source lines: ~46 (19 hypothesis.py + 27 session_pipeline.py)
- Tests: 88 passed (tests/test_session_pipeline.py + tests/test_smt_hypothesis.py)

## Verdict
Code review passed. No technical issues detected. All focus-area hazards were
investigated and found safe.

## Focus-area findings

### 1. NameError / scope across rule2b return sub-branches — SAFE
The six diagnostic locals are pre-initialized at hypothesis.py:846-851, BEFORE the
`if _anchor_age_ok:` block. The three sub-branches that reach the single return point
(`if r2b_dir is not None:`, line 972) are:
- low-sweep branch (853-905): never touches the diagnostics → safe defaults recorded.
- high-sweep / below-mid branch (907-908): never touches them → safe defaults.
- high-sweep / above-mid branch (909-971): recomputes all six.
No path can reach line 972 with an unbound name. Confirmed `_anchor_age_ok == False`
also reaches the return with pure defaults.

### 2. Above-mid recompute consistency with pre-init — SAFE
- `_ath`: pre-init 846 `global_state.get("all_time_high")`; recompute 925 identical source.
- `_session_ath_val`: pre-init 847 `float(global_state.get("session_ath") or _ath or 0)`;
  recompute 951-953 is the byte-identical expression. No divergence.
- `_recovery_gap` / three bools: default to 0.0/False; recompute only in the above-mid
  premium path. Consistent.

### 3. Backtest seed byte-identical — CONFIRMED
- `_full_history_ath()` returns `0.0` when `_smt_state._IN_MEMORY` is True, so
  `max(_hist_ath, 0.0) == _hist_ath` (session_pipeline.py:384). No disk read in backtest.
- The changed session_ath line (386) `max(float(_global.get("session_ath", 0.0) or 0.0), _hist_ath)`:
  `DEFAULT_GLOBAL` (smt_state.py:113) has NO `session_ath` key, and the backtest calls
  `reset_in_memory()` per date (backtest_smt.py:1248) which clears `_STORE`. So
  `load_global()` returns DEFAULT_GLOBAL → `.get("session_ath", 0.0) == 0.0` →
  `max(0.0, _hist_ath) == _hist_ath`, identical to the old unconditional clobber.
- Regression test `test_backtest_seed_ignores_disk_parquet` locks this in.

### 4. Silent-failure / no-print conventions — SAFE
- `_full_history_ath()` swallows all errors via `except Exception: return 0.0`. The
  broad catch matches the existing repo pattern (`seed_global_from_prior`, smt_state.py:222).
  A failure degrades to the windowed seed (max() no-op) rather than crashing the session.
- No `print()` / stdout introduced in production paths.

### 5. Rounding correctness — SAFE
- `_session_ath_val` is always a float (coerced at both init sites) → `round(_, 2)` safe.
- `_ath` may be None → guarded: `round(float(_ath), 2) if _ath is not None else None` (979).
- `_recovery_gap` always float → `round(_, 4)` safe.

## Notes (non-blocking)
- The `except Exception` in `_full_history_ath` is intentionally broad; consistent with
  the codebase's session-seed error handling. No change recommended.
- `_full_history_ath` reads only the `High` column (`columns=["High"]`) — efficient,
  avoids loading the full parquet.
