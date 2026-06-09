## Acceptance Criteria Validation Report

**Feature / Request:** SMT V2 — SMT & SMT-Fill Detection Redesign
**Plan File:** .agents/plans/smt-v2-detection-redesign.md
**Criteria Source:** Plan file (## ACCEPTANCE CRITERIA)
**Validated:** 2026-06-09

---

### Results

| # | Criterion | Verdict | Evidence |
|---|-----------|---------|----------|
| F1 | Regular wick SMT fires once on divergence; symmetric; high→short/bearish, low→long/bullish | PASS | smt_detect.detect_regular_smts / _detect_level_smts; tests test_wick_smt_fires_on_divergence, test_symmetric_leader_mes, test_low_level_long_direction, test_edge_fire_once |
| F2 | Hidden body SMT close-vs-level on completed 15m/30m only, tagged body+timeframe | PASS | detect_hidden_smts(body=True); pipeline gates at now==floor(tf) with per-frame guard; tests test_hidden_close_vs_level_15m, test_hidden_not_on_1m_tags_30m, integration test_hidden_only_on_tf_boundary |
| F3 | Re-arm dual gate (opp move ≥ MIN_REARM_OPP_MOVE_PTS OR opposite SMT) + fresh re-touch; new event; level advance re-arms | PASS | _detect_level_smts re-arm + batch post-pass; tests test_rearm_via_opp_move_pts, test_rearm_via_opposite_smt, test_running_level_advance_rearms |
| F4 | SMT-fills vs paired 1hr FVGs (ts+side); Fill-A & Fill-B; direction from side | PASS | detect_fill_smts + _pair_fvgs; tests test_fill_a, test_fill_b, test_fill_pairing_one_sided_no_fire, integration test_fill_pairing_end_to_end |
| F5 | Fill-B follows Fill-A without re-arm; one-sided FVG never fills | PASS | fill_a_fired follow-on path; tests test_fill_b_follow_on, test_fill_independent_b, test_fill_pairing_one_sided_no_fire |
| F6 | Detection runs every on_1m_bar, independent of hypothesis/position | PASS | _run_smt_v2_detection called unconditionally in on_1m_bar; test_detection_runs_every_1m |
| B1 | Per-minute get_new("1m") returns last bar; 5m accumulator returns window; drains after consumers | PASS | SmtBuffer; tests test_buffer_per_minute_overwrite, test_buffer_5m_accumulates, test_buffer_drain_at_boundary, integration test_buffer_drains_after_5m_consumer |
| B2 | Cadence 1m 09:30–10:30 ET else 5m (boundaries 09:29/09:30/10:30/10:31); consumer flat-gated | PASS | cadence calc + flat gate in _run_smt_v2_detection; tests test_cadence_boundaries, test_cadence_morning_1m, test_cadence_offhours_5m, test_flat_gating |
| B3 | PendingSmtWatch copy-preserves across drain; invalidates on trend/contradiction | PASS | PendingSmtWatch.ingest/update; tests test_watch_preserve_through_drain, test_watch_invalidate_on_trend, test_watch_invalidate_on_contradiction |
| D1 | daily.json additive liquidities_mes; MNQ liquidities + readers unchanged | PASS | DEFAULT_DAILY["liquidities_mes"]=[]; _update_mes_liquidities writes only liquidities_mes; tests test_liquidities_mes_populated, test_mnq_liquidities_unchanged_regression |
| D2 | Edge/re-arm state + retained set persist to smts.json; reload on fresh pipeline; file+in-memory | PASS | load_smts/save_smts; on_session_start reload; tests test_restart_reload, test_smts_roundtrip, test_smts_inmemory |
| N1 | MNQ liquidities + emitted-event list byte/list-identical to baseline | PASS | test_mnq_liquidities_unchanged_regression, test_on_1m_bar_events_unchanged; 42 baseline pipeline tests green |
| N2 | No prints in production paths; detection functions total (return ([],state)) | PASS | No print statements in smt_detect.py / new session_pipeline blocks; test_empty_frames_no_crash |
| V1 | All new tests pass | PASS | 115 passed across the three targeted suites (43 new: 31 smt_detect, 12 integration, 2 smts) |
| V2 | Full suite no new failures vs baseline | PASS | 24 pre-existing failures identical before/after; 1187 passed (was 1144); test_ib_realtime excluded (pre-existing 20s-sleep hang) |
| V3 | Modules import cleanly | PASS | `python -c "import smt_detect, smt_state, session_pipeline"` → clean |

---

### Summary

**PASS:** 16
**FAIL:** 0
**PARTIAL:** 0
**UNVERIFIABLE:** 0
**Total:** 16

**Overall verdict:** ACCEPTED

---

### Notes

- The full suite is run with `-m 'not integration'` (pyproject addopts) per the plan's side-effecting policy; `tests/test_ib_realtime.py` was additionally excluded because it blocks on a 20s `_time.sleep` in `gap_fill_1m_ib` (a live-IB timing test), unrelated to this change.
- Three genuine bugs surfaced during code review were fixed before this validation (asia-forming eligibility; fill re-arm scale mix; level/fill shared-state cross-contamination) — see .agents/code-reviews/smt-v2-detection-redesign.md. Each has a regression test.

Overall: ACCEPTED — all acceptance criteria met, ready for review.
