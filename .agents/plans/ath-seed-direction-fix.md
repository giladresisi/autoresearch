# Plan: ATH-seed root fix + direction diagnosability (GIL-23)

EXECUTION_MODE: lightweight
EXECUTION_RATIONALE: Two localized edits to existing logic in 2 source files (session_pipeline.py seed; hypothesis.py reason dict). No new modules/interfaces/state schema. Unit-testable in isolation. Backtest path unchanged by design. ~25 LOC (LEAN: session_ath = persisted all_time_high; no parquet read).
EXECUTOR DIRECTIVE: Implement this plan sequentially yourself; do NOT use the /execute skill. Use TDD (write the failing unit tests first, then the implementation). Leave ALL changes UNSTAGED — never commit, merge, or push.

**Linear issue (full spec):** GIL-23 — https://linear.app/gilad-resisi/issue/GIL-23
**Worktree:** `autoresearch/ath-seed-direction-fix` (off `live` @ 377b38e)

---

## Background (read GIL-23 for the full root-cause writeup)

On 2026-06-11 live shorted a +600pt afternoon uptrend twice because the session-start ATH seed
collapsed to the *windowed* in-memory IB frame max (`29011.25`) instead of the true ATH (`30807`).
`rule2b`'s recovery-mode guard keys off `session_ath`; with `session_ath` below the afternoon price,
`recovery_gap` floored at 0 and the guard silently switched off → every premium high-sweep read as
bearish distribution → shorts.

**Verification caveat:** the 1s backtest baseline already seeds 30807 (its 60-day parquet window
contains the 05-26→06-04 high) and already chooses `up`, so the A/B is a **regression-safety check
only** (≈ no-op). **Primary verification = the unit tests below.**

---

## Verified anchors (line numbers re-checked against the worktree)

- **Fix 1 target:** `session_pipeline.py:358-367`, inside method **`on_daily_or_startup`** (NOT
  `on_session_start`; the spec's name drifted — `on_session_start:608` *calls* `on_daily_or_startup`).
  Current seed block:
  ```python
  # Seed all_time_high and session_ath from historical bars.
  _global = load_global()
  if not self._hist_mnq_1m.empty:
      _hist_ath = float(self._hist_mnq_1m["High"].max())
      _global["all_time_high"] = max(_global.get("all_time_high", 0.0), _hist_ath)
      _global["session_ath"] = _hist_ath          # NO max() guard
      self._session_ath = _hist_ath
  else:
      self._session_ath = None
  save_global(_global)
  ```
- **Live vs backtest discriminator:** `smt_state._IN_MEMORY` (already referenced directly in
  session_pipeline.py:585 — reuse the same access pattern via the `_smt_state` alias).
- **Full-history parquet (same source the live writer + levels.json use):**
  `paths.general_live_dir() / "MNQ_1m.parquet"` (confirmed in live_orders.py:325/814,
  parquet_maintenance.py:37). `pd` and `paths` are already imported in session_pipeline.py.
- **Fix 2 target:** `hypothesis.py:962-965`, inside **`_determine_direction`** (`global_state: dict`
  param at :681). The rule2b high-sweep branch computes `_session_ath_val`, `_ath`, `_recovery_gap`,
  `_is_false_pos_ath`, `_is_false_pos_morning`, `_is_false_pos_recovery` at :915-954, then at :962-965:
  ```python
  if r2b_dir is not None:
      reason["rule"]             = "rule2b"
      reason["last_swept_level"] = _last_liq
      return r2b_dir, reason
  ```

---

## Task 1 — Fix 1 (LEAN, adopted): derive `session_ath` from the persisted `all_time_high`

**Why lean, not the parquet read:** the code/data investigation showed it was NOT a load-path bug —
`all_time_high` already persists in the stable `general_live_dir()/global.json` and loaded correctly on
06-11 (`levels.json`=30807). The only corrupted value was `session_ath`, which the old seed clobbered
with the windowed hist max every session. So derive it from the persisted ATH instead of a second source.

Rewrite the seed block in `on_daily_or_startup` (`:360-364`):

```python
if not self._hist_mnq_1m.empty:
    _hist_ath = float(self._hist_mnq_1m["High"].max())
    _global["all_time_high"] = max(_global.get("all_time_high", 0.0), _hist_ath)
    _global["session_ath"]   = _global["all_time_high"]   # GIL-23: persisted ATH, not the windowed hist
    self._session_ath = _global["session_ath"]
else:
    self._session_ath = None
```

Notes:
- No parquet read, no `_IN_MEMORY` branch, no helper method.
- Backtest (in-memory) mode: `global.json` starts at DEFAULT (`all_time_high`=0), so
  `all_time_high` = max(0, 60-day window max) = the same value the old windowed seed produced →
  `session_ath` byte-identical to today. **Backtest seed unchanged.**
- Accepted residual risk: a genuinely lost/wrong-path `global.json` would collapse both to the hist
  window (the parquet-floor variant would have covered that, but it did not occur on 06-11).

## Task 2 — Fix 2: record the guard inputs in `direction_reason`

In `hypothesis.py` `_determine_direction`, at the rule2b return (`:962-965`), add the six diagnostic
fields before returning so a direction is reproducible from `events.jsonl`:

```python
if r2b_dir is not None:
    reason["rule"]                 = "rule2b"
    reason["last_swept_level"]     = _last_liq
    reason["session_ath"]          = round(_session_ath_val, 2)
    reason["all_time_high"]        = round(float(_ath), 2) if _ath is not None else None
    reason["recovery_gap"]         = round(_recovery_gap, 4)
    reason["is_false_pos_ath"]     = bool(_is_false_pos_ath)
    reason["is_false_pos_morning"] = bool(_is_false_pos_morning)
    reason["is_false_pos_recovery"]= bool(_is_false_pos_recovery)
    return r2b_dir, reason
```

(These locals are all in scope on this branch — `_session_ath_val`, `_ath`, `_recovery_gap`,
`_is_false_pos_*` are defined at :915-954.)

---

## Unit tests (TDD — write first, then implement)

Add to `tests/test_session_pipeline.py` (reuse the `_isolate_state` fixture + `_make_1m_bars`):

1. **`test_session_ath_seeds_from_persisted_ath_not_windowed`** — reproduce the live failure mode:
   - `save_global({"all_time_high": 30807.0, ...})` (the persisted true ATH; live mode writes to
     `general_live_dir()/global.json`, isolated via the fixture's `ACT_GLOBAL_DIR`).
   - Construct the pipeline with a *windowed* `hist_mnq_1m` whose `High.max()` ≈ **29011.25** (below
     30807), `_IN_MEMORY` False (live). Call `on_session_start`.
   - Assert `load_global()["session_ath"] == 30807.0` **and** `["all_time_high"] == 30807.0` and
     `pipeline._session_ath == 30807.0`. (Fails today: session_ath would be 29011.25.)

2. **`test_backtest_seed_session_ath_equals_window_max`** (regression-safety) — set `_IN_MEMORY` True;
   fresh in-memory global; construct pipeline with windowed max 25010; call `on_session_start`; assert
   `session_ath == 25010.0` **and** `all_time_high == 25010.0` (session_ath = window max → backtest
   determinism kept, byte-identical to the old seed).

Add to `tests/test_smt_hypothesis.py`:

3. **`test_rule2b_direction_reason_carries_guard_fields`** — drive `_determine_direction` (or
   `run_hypothesis`) into the rule2b premium high-sweep branch and assert the returned
   `direction_reason` contains all six keys: `session_ath`, `all_time_high`, `recovery_gap`,
   `is_false_pos_ath`, `is_false_pos_morning`, `is_false_pos_recovery`. (Follow the existing rule2b /
   `last_swept_level` test setup in that file for the scenario construction.)

## Validation commands

```
uv run pytest tests/test_session_pipeline.py tests/test_smt_hypothesis.py -q
uv run pytest -q          # full suite — confirm no regressions (171 tests baseline)
```

## Out of scope
D8 broker-fill reconciliation; AutoLiq/Apex. Do not add a second ATH source — reuse
`general_live_dir()/MNQ_1m.parquet`.
