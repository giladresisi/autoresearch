# Code Review — GIL-32: SMT-conviction override on rule2b direction

Reviewed: `smt_conviction.py` (new), `hypothesis.py`, `session_pipeline.py`, `smt_state.py`,
`tests/test_smt_conviction.py` (new), `tests/test_smt_hypothesis.py`.
Plan: `.agents/plans/gil32-smt-conviction-override.md`.

## Stats
- Files Modified: 4 (hypothesis.py, session_pipeline.py, smt_state.py, tests/test_smt_hypothesis.py)
- Files Added: 2 (smt_conviction.py, tests/test_smt_conviction.py)
- Files Deleted: 0
- New lines: ~607 (smt_conviction 333 + test_smt_conviction 220 + 25 hyp + 21 pipeline + 6 state — tests 202)
- Deleted lines: 0 (all non-test changes are pure additions — no existing logic modified)

## Verification performed
- `pytest tests/test_smt_conviction.py` → 15 passed
- `pytest tests/test_smt_hypothesis.py` → 38 passed
- `pytest tests/test_session_pipeline.py test_smt_relevance.py test_smt_relevance_rules.py
  test_smt_invalidation.py test_smt_strategy_v2.py` → 201 passed
- `git diff --numstat` on the three production files = additions only (0 deletions) → confirms
  `_compute_smt_score_v2` / `_co_evaluate_with_smt` / confidence path / `smt_active_set` semantics /
  GIL-19 relevance UNTOUCHED.
- INVALIDATE_PTS reused via `smt_detect._invalidate_pts` — not widened, no new tier table.
- Override is a single gated flip after `r2b_dir` is set, inside `if r2b_dir is not None`,
  ungated by daily-trend. Direction mapping (short→down, long→up) is correct.
- Default `smt_conviction=0.0` is falsy → branch short-circuits → byte-identical (test asserts
  reason-dict equality vs the no-kwargs call).
- No `print()` in any production path.

## Hard-constraint check: ALL PASS
No constraint violations found.

## Findings

severity: low
file: smt_conviction.py
line: 200-204 (and session_pipeline.py:2178-2183)
issue: Collapsed wick+body standing record queries only the SURVIVOR's detect key, not the union.
detail: When a wick supersedes a body for the same (ref_name, direction), the standing record keeps
  only `type="wick"`, so the reconstructed detect key is `ref|dir|wick` only. The canonical
  `ingest_smts` survivor carries a `keys` list = union of both folded detect keys, so its
  relevance/fulfillment aggregates over BOTH variants. Here, if detect_state marks the body key
  `fulfilled`/`gone` but not the wick key, the standing record will not observe it. In practice
  wick+body of the same level fulfill/expire together, so impact is small, and this is a fidelity
  gap vs ingest_active_set rather than a crash. The detect-key reconstruction itself is otherwise
  correct (matches `_record_key`: level → `ref|dir|type`, fill → bare ref_name).
suggestion: If exact parity with ingest_smts is desired, store a `keys` list on the standing record
  (union on supersede) and query smt_status over all of them, taking ANY-fulfilled / ALL-gone like
  `collapsed_relevance`. Otherwise document the single-key simplification as intentional for Phase 1.

severity: low
file: smt_conviction.py
line: 169-182
issue: A re-firing logical SMT fully RESETS its lifecycle (fire_iso, adverse_streak, fulfilled_iso).
detail: On supersede, `by_key[lk] = new_rec` replaces the existing standing record wholesale, so a
  dynamic level that re-fires drops any accumulated `adverse_streak`, clears `fulfilled_iso`
  (restarting the residual clock), and resets `fire_iso` (re-arming birth grace). This is consistent
  with "newer wins" and likely intended, but it means an SMT that keeps re-firing can never accrue a
  sustained adverse streak nor age out its residual. Verify this matches the intended persistence
  semantics; if a re-fire should preserve streak/fulfillment, carry them forward on supersede.
suggestion: If lifecycle should persist across re-fires, copy `adverse_streak`/`fulfilled_iso`/
  earliest `fire_iso` from the existing record onto the survivor. If reset is intended, no change —
  just confirm.

severity: low
file: smt_conviction.py
line: 39, 230
issue: ATH/fill tier weights have no matching INVALIDATE_PTS entry → session-threshold fallback.
detail: `_TIER_WEIGHT` defines ATH=3.0 and fill=1.5, but `INVALIDATE_PTS_MNQ` only has
  week/day/session, and `_invalidate_pts` falls back to `session` (10 pts, the TIGHTEST threshold)
  for any unknown tier. So an ATH-tier or fill-tier standing record (reachable only via an explicit
  `tier` on a div — i.e. Phase 3, not Phase 1's raw level emissions which classify to week/day/
  session) would adverse-drop at the tightest threshold despite being high significance. Not
  reachable in Phase 1 (raw `_detect_level_smts` emissions carry no `tier`/`is_ath`; `_tier_of`
  classifies them via `_level_class` → week/day/session only; fills go to `fill` weight but use the
  session adverse threshold, which is acceptable). Flagging as a latent Phase-3 concern.
suggestion: When Phase 3 starts supplying ATH/fill tiers, add explicit ATH/fill adverse thresholds
  (or map ATH→week, fill→session deliberately) rather than relying on the silent session fallback.

severity: low
file: session_pipeline.py
line: 2175
issue: `import smt_conviction as _smt_conv` executed inside the per-bar loop body.
detail: The import runs every 1m bar. Python caches in sys.modules so the cost is negligible, and it
  matches the pre-existing local convention in the same block (`import smt_detect as _smt_detect` at
  line 2108). Stylistic only.
suggestion: Optional: hoist both local imports to module level for clarity. No functional issue.

## Notes (not defects)
- Exception isolation is solid: both `update_standing` and `conviction_score` wrap their bodies in
  try/except returning safe defaults (prior set / empty score) — total, never raise. The
  session_pipeline call site is additionally inside the existing blanket `try/except: pass` shadow
  block, so a defect cannot reach the live direction/detect path.
- `now_iso` and `fire_iso` are consistent (both `now.isoformat()` from the same `now`), so the
  grace/residual minute math does not hit a tz naive-vs-aware mismatch in the live path.
- `update_standing` is correctly fed RAW `records` (not `to_record` output); `_tier_of` handles raw
  emissions (kind "smt"/"fill", no tier) correctly.
- Supersede tie-break (`nm is None or nm >= 0`, newer-wins) matches `ingest_smts._supersedes`
  (`_record_time(new) >= _record_time(old)`).
- JSON-serializability: all record fields are str/float/int/None — serializable in and out.

## Conclusion
No critical/high/medium issues. The change is well-isolated, purely additive, constraint-compliant,
and fully covered by passing unit + integration tests. The four low-severity items are
fidelity/Phase-3 latent concerns and design-intent confirmations, not Phase-1 bugs.
