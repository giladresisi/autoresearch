# SMT V2 — three strategy updates

⚠️ Medium. Three independent changes; no shared state. Req 4 from the original request needs **no code** (the effect already exists — see Background).

## Background (verified against code)

- **4hr FVGs are inert.** Computed/stored in `daily.json` and visited-pruned, but never consumed by any entry/exit decision. Only **1hr** FVGs feed hypothesis meaningful-levels; per-bar FVG entries use a separate 1m `detect_fvg()`. The 4hr **BOS/CHoCH** direction score (`b4hr`, weight 0.65, `hypothesis.py` ~L968-982) is a *separate* use of the 4hr timeframe and **must be kept**.
- **PMT entry chokepoint.** All new entries (market + stop) flow through `PickMyTradeExecutor.place_entry()` (`execution/pickmytrade.py`). Closes/cancels/stop-updates use `place_close()` / `update_stop_loss()` / `modify_stop_entry()` and never touch it. There is already a wall-clock ET gate at L133.
- **Cautious-target thresholds** are static module constants in `hypothesis.py`: `CAUTIOUS_SECONDARY_MAX_DIST = 150`, `CAUTIOUS_INITIAL_MAX_DIST = 110`, floor `CAUTIOUS_MIN_DIST = 40`. Read directly inside `compute_cautious_prices()`.
- **`failed_entries`** is per-hypothesis. Increments at `strategy.py:603` and `session_pipeline.py:902`. Resets to 0 at `strategy.py:671` (session), `strategy.py:682` (new hypothesis), `hypothesis.py:1228` (level-sweep none→dir). Decremented by liquidity sweep at `session_pipeline.py:782` (to be deprecated — ignored here).
- **Req 4 already holds.** Every live exit in `trend.py` calls `_clear_position_and_hypothesis(clear_active=True)` → `hypothesis["direction"]="none"`, `manual=False`. `direction=="none"` hard-gates new entries (`trend.py:277`) until `run_hypothesis()` forms a fresh hypothesis (which recomputes cautious targets). No trend-broken signal needed on successful close. **No change.**

## Decisions (locked)

1. Delete only 4hr **FVG** detection/plumbing; **keep** 4hr BOS/CHoCH.
2. 15:30 ET block: **wall-clock**, **internal to the PMT executor** only — no change to strategy/dispatcher/`session_times`.
3. Shrink **15%** per failed entry on **both** max-dist thresholds; key off the **increment event** via a **separate counter** (ignore the decrement); **floor at `CAUTIOUS_MIN_DIST` (40)**; reset where `failed_entries` resets.
4. No code.

---

## Change 1 — Remove 4hr FVGs (keep 4hr BOS/CHoCH)

**Goal:** stop computing/plumbing the 4hr FVG frame; leave 1hr FVGs and 4hr BOS/CHoCH untouched.

Touchpoints (FVG-only — do NOT touch `b4hr` / `bos_score_4hr` / `mnq_4hr` BOS code):
- `session_pipeline.py`: remove `_fvg_4hr` / `_fvg_done_4hr` members, their seeding (~L196-217 — the `_fvg_4hr_full` block, keep `_fvg_1hr`), and the `("4h", "_fvg_4hr", "_fvg_done_4hr")` tuple in the `_extend_fvg_frames` loop (~L1136). **Keep** `_hist_4hr` if (and only if) it feeds BOS/CHoCH — verify before deleting.
- `daily.py`: drop the `hist_4hr` param and the `fvgs_4hr = _detect_fvgs(hist_4hr, …); liquidities.extend(fvgs_4hr)` lines (~L274-275). Update the call in `session_pipeline.py` (~L217).
- `live_orders.py`: remove the 4hr resample (~L784-787) **only if** it is used solely for the FVG path; the 4hr frame also passed to `run_hypothesis` for BOS must stay. Inspect L784-792 carefully and split.
- `hypothesis.py`: remove the `hist_4hr` **FVG** consumption only. The `hist_4hr` BOS resample + `b4hr` scoring stays. (If `hist_4hr` is *only* used for BOS, the param stays; nothing to do here.)
- Tests: `tests/test_session_pipeline.py` (`test_extend_fvg_frames_detects_live_4hr_fvg`, `_bare_fvg_pipeline(fvg_4hr=...)`), `tests/test_smt_daily.py` (`test_run_daily_fixed_4hr_fvg_detected`).

**TDD:** first run the two 4hr-FVG tests to confirm they pass (baseline), then delete/adjust them and confirm 1hr-FVG + BOS/CHoCH tests still pass.

**Acceptance:**
- No 4hr FVG appears in `daily.json` liquidities after a daily run.
- 1hr FVGs still detected and still drive hypothesis levels.
- `bos_score_4hr` still produced (BOS/CHoCH 4hr intact); direction-selection output unchanged on a fixture.
- Full suite green (4hr-FVG tests removed, not just skipped).

## Change 2 — Block new entries after 15:30 ET in the PMT executor

**Goal:** after 15:30:00 ET wall-clock, `place_entry()` blocks (market + stop); all other order paths unaffected.

`execution/pickmytrade.py`:
- Add a module constant near `_ET`: `_NEW_ENTRY_CUTOFF = datetime.time(15, 30)`.
- In `place_entry()`, at the existing gate (L133), add a wall-clock cutoff check using `datetime.datetime.now(_ET).time()` (NOT bar time — this is a real-time guard). On block: log `"[PMT] new entry blocked after 15:30 ET"`, set `self._entry_is_live = False`, return the same `status="blocked"` `FillRecord` shape (callers unchanged). Merge with the existing `is_entry_allowed` block or add immediately after it.

**TDD (`tests/test_pickmytrade_executor.py` or `test_fill_executor.py`):**
- 15:29 ET → market entry submitted (`status="filled"`, `_post_order` called).
- 15:31 ET → market entry blocked (`status="blocked"`, no HTTP submit, `_entry_is_live==False`).
- 15:31 ET → stop entry blocked.
- 15:31 ET → `place_close()` / `update_stop_loss()` still work (not gated).
- Use a patched/frozen `datetime.datetime.now(_ET)` (monkeypatch the module clock).

**Acceptance:** new entries blocked strictly after 15:30 ET wall-clock; closes/cancels/stop-mods always allowed; no changes outside `execution/pickmytrade.py`.

## Change 3 — Dynamic cautious-target max-distance thresholds

**Goal:** shrink both max-dist thresholds 15% per failed entry (keyed off increment events), floored at `CAUTIOUS_MIN_DIST`, reset on the `failed_entries` reset points.

State:
- Add `"cautious_dist_shrinks": 0` to `DEFAULT_POSITION` in `smt_state.py` (parallel to `failed_entries`).

Increment (only the two real increment sites — NOT the decrement):
- `strategy.py:603`: `position["cautious_dist_shrinks"] = position.get("cautious_dist_shrinks", 0) + 1` alongside the `failed_entries` bump.
- `session_pipeline.py:902`: same on `_sbsc_pos`.

Reset to 0 wherever `failed_entries` resets:
- `strategy.py:671` (session), `strategy.py:682` (new hypothesis), `hypothesis.py:1228` (level-sweep none→dir). (This delivers "reset after a successful trade" — every successful managed close clears direction → new hypothesis → reset.)

Apply the shrink in `hypothesis.py`:
- Define `CAUTIOUS_DIST_SHRINK_PCT = 0.15`.
- Add param `dist_shrinks: int = 0` to `compute_cautious_prices(...)`. Inside, compute:
  ```
  _factor = (1.0 - CAUTIOUS_DIST_SHRINK_PCT) ** max(0, dist_shrinks)
  _sec_max = max(CAUTIOUS_MIN_DIST, CAUTIOUS_SECONDARY_MAX_DIST * _factor)
  _init_max = max(CAUTIOUS_MIN_DIST, CAUTIOUS_INITIAL_MAX_DIST * _factor)
  ```
  Use `_sec_max` in place of `CAUTIOUS_SECONDARY_MAX_DIST` (L55, L57, L59) and `_init_max` in place of `CAUTIOUS_INITIAL_MAX_DIST` (L77, L80). Leave the `CAUTIOUS_MIN_DIST` skip and offsets unchanged.
- Thread `dist_shrinks` from callers (each loads position at its site):
  - Formation: `hypothesis.py:1167` — read the loaded position's `cautious_dist_shrinks`.
  - `recompute_cautious_for_fill` (L139): add `dist_shrinks` param, pass through to `compute_cautious_prices` (L160). Update its 3 call sites to pass `position.get("cautious_dist_shrinks", 0)`:
    - `live_orders.py:284`, `strategy.py:405`, `strategy.py:497`.

**TDD (`tests/test_smt_hypothesis.py`):**
- `dist_shrinks=0` → identical output to today (regression guard).
- `dist_shrinks=1` → effective maxes = 150*0.85=127.5 / 110*0.85=93.5; a liquidity at 140pts that qualified at shrinks=0 is now excluded.
- Large `dist_shrinks` (e.g. 20) → both maxes clamped to 40, never below.
- Counter lifecycle: increment on stop-out, reset on new-hypothesis (assert via the existing reset paths).
- `recompute_cautious_for_fill` honors `dist_shrinks` and still no-ops under manual lock.

**Acceptance:**
- Max-dist thresholds shrink 15% per `cautious_dist_shrinks`, floored at 40, applied to both tiers.
- Counter increments only on the two stop-out increment sites; unaffected by the liquidity-sweep decrement.
- Counter (and thus shrink) resets to 0 at all three `failed_entries` reset points.
- `shrinks=0` output byte-identical to pre-change (no behavior drift on the happy path).

---

## Validation
- Run full suite before starting (baseline) and after each change.
- Per change: targeted tests green, then full suite green.
- Manual sanity: a daily run produces no 4hr FVGs but keeps 1hr FVGs + `bos_score_4hr`; an entry attempt after 15:30 ET is blocked while a close is allowed; cautious targets tighten across simulated consecutive stop-outs and snap back after a new hypothesis.
