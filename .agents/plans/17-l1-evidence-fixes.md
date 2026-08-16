# Plan 17 — L1 evidence fixes from the 2026-08-10 09:20 ET audit  ⚠️ Medium

User decisions (2026-08-15 session "l1-fixes"): implement items 1-6 below EXCEPT the
SMT-at-acceptance handling (analysis only, deferred). Sources: 08-10 09:20 audit of
`manual-l1-thesis/20260810_0920000400_openrouter.json` + facts sheet.

## Baseline
`.venv pytest agent/ -q` — pre-existing environment issues, not regressions:
- `agent/contracts/test_schemas.py` cannot import (`jsonschema` not installed in .venv) —
  excluded via `--ignore`.
- `test_contracts.py::test_pending_resolution_accepted_when_immature` fails for the same
  missing-`jsonschema` reason (imports it inside the test).
All other modules pass (contracts 136 passed, derive_facts_lib 66 passed post-change).

## Changes

### 1. Running day/week extreme promotion (decision: BOTH P1 tier weight and P2 eligibility)
- `derive_facts.py`: new `_promoted_session_extremes(data, levels, swept_at, now, wk_anchor)`
  → `{tkr: {name: "day"|"week"}}`: a session-tier level that was the running day/week extreme
  at its reference time (sweep time if swept, else now) is tier-promoted. Level NAME and the
  hashed S0-S7 text stay untouched — promotion is an additive map + S9 tag.
- SMT candidates: a session-tier candidate promoted for BOTH assets → tier promoted,
  `meaningful=True` (P2-eligible).
- `bench/facts.py`: override `level_tiers[tkr][name]["tier"]` with the promoted tier
  (drives P1 auto-injection weight + §2.1e machinery).
- S9: tag promoted close-status rows and candidates.

### 2. P4 gate — new extreme after last mid interaction voids P4
- `derive_facts._mid_tf_state`: freshness compares the crossing against the LATEST tier
  extreme on EITHER side (`max(hi_ts, lo_ts)`), not just the tested side — covers the
  wick-extreme-without-completed-bar-crossing case. Recovery is automatic (a newer completed-
  bar crossing re-arms).
- S9: explicit `[P4-ELIGIBLE / P4-INELIGIBLE ...]` tag per mature mid row.

### 3. Unconditional P3 position rows (decision: both mids × both assets, always)
- `derive_facts`: new `bundle.mid_position = {tkr: {mid: {price, side, dist_pts, dist_ratio}}}`
  computed always; rendered as a dedicated S9 block.
- `bench/facts.py`: export `mid_position`.
- `score_thesis_evidence`: new `mid_position=None` param; when a mid has NO mature HTF status
  at all (never crossed in 24h — price parked on one side), inject a position-only P3
  (`tf:"1h"`, direction from side).

### 4. P4 supersedes P3 per (asset, mid) regardless of tf
- `score_thesis_evidence` P3/P4 auto-injection: if ANY tf yields a fresh P4 for (asset, mid),
  do not inject P3 for the other (stale) tf. Order: #2 gate first, then this.

### 5. Most-extreme-swept-only P1 (decision: strict — applies even while the deeper read is immature)
- `derive_facts`: new `_extremity_shadowed_levels(lv, swept_at)`: among an asset's SWEPT
  same-side levels (any tier/family), only the most extreme survives; ties broken by tier
  rank. Folded into `bundle.suppressed_p1_levels` (S9 hiding, P1 injection skip, declared-P1
  zeroing, near-maturity skip all inherit).

### 6. Equilibrium-reversion staleness becomes a HARD P1 gate
- S9 tag reworded to `[STALE ... NOT usable P1 evidence]`.
- `bench/facts.py`: export `p1_stale_levels` ({tkr: [names]}).
- `score_thesis_evidence`: new `p1_stale_levels=None` param — stale P1s skipped in auto-
  injection and zeroed if declared. `validate_thesis` threads both new keys.
- P2 untouched (has its own shelf-life mechanism).

### Docs
`agent-docs/strategy/decisions/thesis.md`: §2.1 P2/P3 rows, §2.1b (session nesting text is
stale vs code + add extremity rule), §2.1c (staleness now hard), new §10 entry for the
2026-08-10 09:20 audit.

## Deliverable checklist / tests (named)
- [x] `_extremity_shadowed_levels`: (a) deeper swept low suppresses shallower swept low,
      (b) unswept deeper level suppresses nothing, (c) equal-price tie keeps higher tier,
      (d) sides independent / cross-tier. (test_derive_facts_lib.py, 4 tests)
- [x] `_mid_tf_state` freshness: opposite-side extreme AFTER crossing → fresh=False; both
      extremes before crossing → fresh=True.
- [x] `score_thesis_evidence`: fresh P4 supersedes stale-tf P3 same asset+mid; stale-only
      tfs still P3; position P3 injected from mid_position (and not when crossing evidence
      exists / when mid_position absent); p1_stale_levels zeroes declared + skips injection;
      stale gate leaves P2 alone. (test_contracts.py, 8 tests)
- [x] Promotion: `_promoted_session_extremes` day/week/none cases + Monday week-window
      guard; candidate promotion requires both assets (verified end-to-end on 08-10);
      level_tiers override in bench facts dict (verified end-to-end).
- [x] End-to-end 2026-08-10 09:20 rebuild: asia(cur)_low promoted day-tier both assets, SMT
      candidate meaningful=True; MNQ london(cur)_low extremity-suppressed; MES/MNQ
      prev1_day_high stale-gated; MID POSITION block renders (both assets ~ABOVE weekly
      mid → two UP P3 1.5s); MES daily_mid P3[4h] superseded by P4[1h]. Auto-ledger:
      net −0.8125 DOWN, ceiling LOW (was −7.06 DOWN).
- [x] Harness rerun 08-10 09:20 (openrouter): DOWN/HYBRID/LOW, same ledger (−0.8125),
      tighter falsifiers (2×5m closes above daily mid), exhausted_if time-boxed 90m →
      walk-forward exhausts 10:50. Pre-fix result backed up as
      `20260810_0920000400_openrouter.before_l1_fixes.json`.
- [x] Test debt found (pre-existing, NOT from this plan): test_menu_membership's two
      validate_thesis assertions fail because their fixture thesis declares bias UP with an
      empty evidence ledger — broken since the "empty ledger + directional bias → reject"
      rule shipped; test_schemas.py + one test_contracts test need `jsonschema` in .venv.

## Addendum (2026-08-15, same session): P2 effective-verdict dispatch
- **Stage 1 SHIPPED:** P2's condition (c) reads the effective (post-partial-bar) verdict
  in the auto-injection — 'reverse' on an acceptance fires P2; 'reverse' on a rejection
  falls back to flipped P1 (stale P2 no longer escapes the §10 machinery); 'omit' blocks
  both; discount-stage P2 inherits ×0.5. 5 new tests, all green (contracts 141 passed).
- **Stage 2 A/B RUN — VERDICT: DO NOT ENABLE (knob `p2_discount_fire_ratio` stays None).**
  Distance-safe discount-fire at ×0.5, thresholds {0.25, 0.375, 0.5}, across all 52 stored
  manual-l1-thesis boundaries (deterministic rescore, ground truth = MNQ +4h move; results:
  scratchpad `ab_stage2_results.tsv`). Findings: rung fires at only 4/52 boundaries even at
  0.25 (3 at 0.375, 2 at 0.5); flips exactly ONE call (08-10 09:20 DOWN→UP) whose grade is
  horizon-dependent (fwd4h −16.25 = noise-level wrong; intraday swing to TDO = right);
  at 0.375/0.5 zero bias changes (inert). Also surfaced a latent interaction: a ×0.5
  discount-fired 4h P2 displaces a FULL-weight mature 1h P2 via tf-dedup's prefer-4h rule
  (07-24: weakened a correct DOWN call by 0.66). No edge + added complexity + one wart →
  keep off; re-test when more stored boundaries accumulate.

## Deferred (explicitly NOT in this plan)
- SMT-at-acceptance (divergent acceptance) treatment — open design question, analysis only.
- Renaming levels in S1/S2 text (hashed core) — promotion is tag/tier-only.
- Staleness/near-maturity for promoted session levels (tuple tier stays "session").
