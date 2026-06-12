## Acceptance Criteria Validation Report

**Feature / Request:** SMT V2 — three strategy updates (remove 4hr FVGs; 15:30 ET entry cutoff; dynamic cautious max-dist)
**Plan File:** .agents/plans/smt-v2-three-updates.md
**Criteria Source:** Plan file (per-change Acceptance sections)
**Validated:** 2026-06-09

---

### Results

| # | Criterion | Verdict | Evidence |
|---|-----------|---------|----------|
| C1-a | No 4hr FVG appears in daily.json liquidities after a daily run | PASS | `run_daily_fixed` signature is now `(now, hist_mnq_1m, hist_1hr, today)` — no 4hr param; only `_detect_fvgs(hist_1hr, …)` remains. Live probe: daily run wrote 1 FVG (1hr), zero 4hr path exists. |
| C1-b | 1hr FVGs still detected and still drive hypothesis levels | PASS | `daily.py` 1hr detect intact; `session_pipeline._fvg_1hr` seeding + `_extend_fvg_frames("1h",…)` intact; `_detect_fvg_1hr` → `_build_meaningful_levels` unchanged. `test_extend_fvg_frames_detects_live_1hr_fvg` + `test_live_fvg_lands_in_daily_json` pass. |
| C1-c | bos_score_4hr still produced; direction-selection output unchanged | PASS | `hypothesis.py` `b4hr` (weight 0.65) and `reason["bos_score_4hr"]` (L1000) intact; `_hist_4hr` still passed to `run_hypothesis`. Direction tests (`test_compute_direction*` within touched files, `test_smt_hypothesis` direction tests) pass unchanged. |
| C1-d | Full suite green; 4hr-FVG tests removed (not skipped) | PASS | `test_run_daily_fixed_4hr_fvg_detected` and `test_extend_fvg_frames_detects_live_4hr_fvg` deleted (grep: 0 matches). Full suite failure set byte-identical to baseline (no new failures). |
| C2-a | New entries blocked strictly after 15:30 ET wall-clock | PASS | `place_entry` guard `if _now_et_time > _NEW_ENTRY_CUTOFF` using `datetime.datetime.now(_ET).time()`. Tests: 15:29→filled, 15:30:00→filled (boundary), 15:31→blocked (market + stop), no HTTP submit, `_entry_is_live==False`. |
| C2-b | Closes/cancels/stop-mods always allowed | PASS | Guard is inside `place_entry` only. `test_close_allowed_after_cutoff` and `test_update_stop_loss_allowed_after_cutoff` (frozen 15:31) confirm both still post. `place_close`/`update_stop_loss`/`modify_stop_entry` build payloads independently. |
| C2-c | No changes outside execution/pickmytrade.py | PASS | Change 2 git diff touches only `execution/pickmytrade.py` (+ its test file). No edits to strategy/dispatcher/session_times. |
| C3-a | Max-dist thresholds shrink 15% per cautious_dist_shrinks, floored at 40, both tiers | PASS | `_factor = 0.85 ** max(0,dist_shrinks)`; `_sec_max`/`_init_max = max(40, const*_factor)`; all 5 threshold usages (3 sec, 2 init) substituted. `test_cautious_dist_shrinks_one_excludes_far_level`, `_includes_level_within_shrunk_max`, `_large_clamps_to_min` pass. |
| C3-b | Counter increments only on the two stop-out sites; unaffected by liquidity-sweep decrement | PASS | Increments at `strategy.py:604` and `session_pipeline.py:895` only. Decrement site (session_pipeline ~L782 `failed_entries -= 1`) does NOT touch `cautious_dist_shrinks` (separate counter). |
| C3-c | Counter resets to 0 at all three failed_entries reset points | PASS | Resets added at `strategy.reset_position_for_session`, `strategy.reset_position_for_new_hypothesis`, and `hypothesis.py` skip_position_reset branch. Tests: `_reset_by_session_helper`, `_reset_by_new_hypothesis_helper`, and `test_failed_entries_reset_on_direction_transition_from_none` (now also asserts shrinks==0). |
| C3-d | shrinks=0 output byte-identical to pre-change | PASS | `_factor=1.0` → `_sec_max=150`, `_init_max=110` (exact constants). `test_cautious_dist_shrinks_zero_is_unchanged` asserts the 140pt level still qualifies AND the default-kwarg call equals the explicit-0 call. |

---

### Summary

**PASS:** 11
**FAIL:** 0
**PARTIAL:** 0
**UNVERIFIABLE:** 0
**Total:** 11

**Overall verdict:** ACCEPTED

---

### Notes

- Req 4 (fire trend-broken on successful close) is correctly NOT implemented: the effect
  already holds (every live exit clears `hypothesis["direction"]="none"`, hard-gating new
  entries until a fresh hypothesis recomputes cautious targets). No code expected; none added.
- Full-suite comparison: 28 pre-existing failures at baseline; identical 28 after the change
  (environmental — live IB connection, slippage config, missing API key). Zero new failures.

Overall: ACCEPTED — all acceptance criteria met, ready for review.
