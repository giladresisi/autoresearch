# Plan 18 (Part A) — Deepen the first DOL: raise `DOL_MIN_DRAW_RATIO` 0.5 → 1.0 ✅ Simple

Status: SPEC, ready for implementation (2026-08-16). Standalone — test and commit BEFORE
Plan 19 (roll-on-completion). Scope: ONE constant + its ripples. The first draw offered
in the thesis direction gets deeper; nothing about direction, lifecycle, or post-touch
behavior changes.

## What and why

`derive_facts.DOL_MIN_DRAW_RATIO`: 0.5 → 1.0 (flat 5-pt latency guard unchanged; no-ATR
fallback unchanged). Data (L2 track, `l2-mechanisms.md` §10.3 **v2 extension**, commit
`272cf7c`: 73 days 05-04..08-14, 132 named-pool arms, pre/post-rollover segments split):
both segments agree 0.5–0.7× is unusable (60–75% swept pre-09:35) and ≥1.0× is safe
(≤11%); the 0.7–1.0× band is regime-ambiguous (40% swept post-rollover vs ~0% in May's
back-month tape) — 1.0× is chosen on post-rollover weighting (the live regime). Their
hold-for-D2 counterfactual on the 26 swept near arms additionally shows the deeper
target is positive-EV in its own right: D2 reached 73% by 16:00, median +72.6 pts vs
taking profit at the near pool (the counterfactual's fat negative tail is a no-stop
scoring artifact — those cases die at the mechanism SL / `falsified_if` long before the
horizon exit). Pools under the floor are not targets a thesis can trade toward — they
are noise the opening rotation eats. Phase-2 days whose D1 sat in the newly-excluded
band: 07-17 (0.51×), 07-21 (0.52×), 08-05 (0.86×), 07-31 (0.92×).

## Ripple effects (must be covered by validation)

1. **D1 deepens** on affected days → next-deeper pool (or projection) becomes D1.
2. **Projection eligibility can change**: a direction whose only BAND pools sat under
   1.0× now has an empty band → projection offered (if the two stretch gates pass).
3. **No-liquidity flips possible**: a direction whose ONLY pools were 0.5–1.0× with
   projections gated → menu empty → expected_bias may downgrade to NEUTRAL. Every such
   boundary must be enumerated and individually judged (it may be correct — "the only
   thing left is opening-rotation fodder" ≈ nothing to draw to).
4. **Exhaustion predicates** (S8 X-entries) move with the deeper DOL automatically.
5. **L2 coupling**: the thesis DOL is L2's take-profit — deeper TP on affected days,
   which the §10.3 v2 counterfactual says is positive-EV under the existing stop/
   invalidation machinery (no special near-pool TP policy needed on either track).
   Precision per the L2 session's correction: their §2 DOL-floor veto is a
   remaining-distance check at entry-decision time (≥60 pts entry→DOL, re-evaluated per
   re-bind) — it does NOT special-case pre-sweep entries; it merely *allows* entries
   that beat the sweep when enough distance remains (07-17's entries had 90+). Do not
   build against an exemption that doesn't exist. Flag every recorded L2 day whose DOL
   changes to the L2 track before they encode §11.

## Test / validation plan (commit gate)

Deterministic (no LLM):
- [x] Unit tests (`test_menus.py`): (a) 0.9×-away pool excluded at the new floor,
      (b) 1.1×-away pool kept, (c) flat 5-pt fallback when avg_range absent unchanged,
      (d) a band whose only pool was 0.8× → after exclusion projection is offered
      (gates permitting). All in `test_dol_floor_scales_with_avg_range` /
      `test_dol_floor_falls_back_to_flat_guard_without_avg_range` /
      `test_dol_floor_raise_frees_band_for_projection`; 24/24 test_menus.py pass.
- [x] Boundary sweep, floor 0.5 vs 1.0 (61 distinct stored boundaries harvested from
      manual-l1-thesis/**/*.json): 13 boundaries changed (D1 deepened and/or projection
      offering changed); expected_bias changes = exactly ONE (07-17 09:20 DOWN→NEUTRAL,
      the enumerated ripple-3 case — judged correct, see its row). Projection-offering
      changes: 07-01 02:00 + 07-01 08:00 + 07-02 10:30 each gained `projection_down`
      after the near DOWN pool fell under the floor. Full per-boundary detail in the
      appended hand-off rows below.
- [x] Draw-realism on the 12 changed boundaries with a surviving D1: new D1 reached
      same-day in 6/12 (06-30 08:00 +98min, 07-01 02:00 +399min, 07-01 08:00 +89min,
      07-02 10:30 +36min, 07-16 01:00 +410min, 08-05 +25min); NOT reached: 07-01 14:00,
      07-10 12:00, 07-16 09:00, 07-21, 07-23 07:00 (old D1 also never reached), 07-31.
      ~50% is below the §10.3 67–100% post-sweep prediction — note the §10.3 number
      conditions on the near arm being swept AND holding to 16:00 with no stop; our set
      includes anti-direction menus and NEUTRAL days.
- [x] Knife-edge check at 0.9 / 1.1 (all 61 boundaries): ZERO bias differences vs the
      1.0 result at either floor — the 07-17 NEUTRAL flip holds at both. Changed-D1-set
      differences only: at 0.9 the 07-31 UP change does NOT occur (its old pool sits at
      0.9214×, inside the grey zone by construction — the §10.3-known 0.92× day); at 1.1
      five boundaries (06-30 05:00/08:00, 07-02 14:00, 07-10 09:00, 07-31) additionally
      deepen a DOWN/second-direction D1. No conclusion flips.

- [x] **07-17 DOWN — the hardest ripple-3 case (per the L2 session's note 3):** its
      DOWN menu had NO D2 behind the excluded `london(cur)_low` (28554.75, 0.51×), so
      the raise empties that band — the recorded DOWN thesis's DOL becomes
      `projection_down` (if the stretch gates pass) or the direction downgrades to
      no-liquidity NEUTRAL. Enumerate which of the two the sweep produces and judge it
      explicitly; 07-17 is L2's best real-input-validated day (§8 takeover +91.25 with
      TP exactly at the excluded pool). RESULT: the sweep produces no-liquidity NEUTRAL
      (projection_down doubly gated: day-stretch 5.21× and weekly 5.98× below mid), and
      the mini-diff confirms — clean first-attempt NEUTRAL, dol=None, 0 retries (the
      baseline model had itself tried NEUTRAL on attempt 0 and was ARI-forced to DOWN).
      Judged CORRECT: everything left below was opening-rotation fodder.

LLM mini-diff (~4–6 calls):
- [x] Rerun the harness at 09:20 on every day the sweep says D1 changed (known so far:
      07-17, 07-21, 07-31, 08-05). The fresh Phase-2 results are the exact-baseline OFF
      arm (post-refit, pre-floor-raise). Pass: model adopts the deeper D1 (or a
      justified alternative), zero DOL-related rejections, no new retries/fallbacks,
      and walk-forward completions on those days are no longer sub-20-minute unless the
      market genuinely traveled ≥1.0×.

      07-17, 07-21, 07-31, 08-05). RESULT: no additional 09:20 boundaries changed, so
      the mini-diff set is exactly those four. All four PASS — outcomes in the hand-off
      table's "model adopted?" column. Only 08-05 has a caveat worth reading (inverted
      model falsifier, not DOL-related).

Hand-off gate (before the L2 track encodes their §11 rule):
- [x] Report every recorded L2 day whose DOL changed — 07-17's outcome in particular —
      back to the L2 track by FILLING THE "L2 hand-off" SECTION AT THE BOTTOM of this
      file (the L2 session reads it from there; format and required fields are already
      laid out).

Commit Part A alone once all blocks pass. Doc updates ride along: thesis.md §8 DOL
bullet (floor value + cite §10.3 v2 / `272cf7c`) and the `DOL_MIN_DRAW_RATIO` comment
block.

## Open decision — RESOLVED

Floor value: **1.0×** (was: 1.0× vs 0.9×). Resolved by §10.3 v2's segment split (see
the Cross-track notes below): 0.7–1.0× is regime-ambiguous, and post-rollover — the
live regime — reads 40% swept there; ≥1.0× is safe in both segments. The knife-edge
sweep at 0.9/1.1 stays in the plan as confirmation that nothing load-bearing sits on
the exact value.

## Cross-track notes from the L2 session (2026-08-16, commit 272cf7c)

1. **Stronger data now committed — cite §10.3 "v2 extension" instead of the 43-day/89-arm
   figures above.** `l2-mechanisms.md` §10.3 v2 (commit 272cf7c): 73 days 05-04..08-14,
   132 named-pool arms, pre/post-rollover segments split. Both segments agree 0.5–0.7×
   is unusable (60–75% swept pre-09:35) and ≥1.0× is safe (≤11%); the 0.7–1.0× band is
   regime-ambiguous (40% post-rollover vs ~0% in May's back-month tape) — this RESOLVES
   the open decision above in favor of **1.0×** (post-rollover is the live regime).
   Additionally, a hold-for-D2 counterfactual on the 26 swept near arms: D2 reached 73%
   by 16:00, median +72.6 pts vs TP-at-near-D1 — the deeper TP is positive-EV; no
   special near-pool TP policy needed on either track.
2. **Correction to the ripple-5 wording**: L2's §2 DOL-floor veto does NOT "exempt
   pre-sweep entries" — it is a remaining-distance check at entry-decision time (≥60 pts
   from entry/trigger to DOL, re-evaluated per re-bind). It merely *allows* entries that
   beat the sweep when enough distance remains (07-17's entries had 90+). Same effect on
   07-17, but don't build against an exemption that doesn't exist.
3. **07-17 DOWN is the ripple-3 case to inspect hardest**: per the L2 sweep, its DOWN
   menu had NO D2 (london(cur)_low was the only pool) — the raise empties that band, so
   the recorded DOWN thesis's DOL becomes projection_down or the direction downgrades to
   NEUTRAL. That day is L2's best real-input-validated day (§8 takeover, +91.25, TP
   exactly at the excluded london_low 28554.75). Whatever the mini-diff resolves there,
   flag the outcome back to the L2 track — their §8 validation record must be re-verified
   against the new DOL before §11 encoding.

## L2 hand-off — mini-diff outcomes (l1-fixes agent: fill this in, one row per changed day)

The L2 session consumes this section verbatim to re-verify its §8/§10 validation record
(each row's `new DOL price` becomes the take-profit in an L2 re-replay, and `bias` decides
whether the day's plan still exists at all). Please fill EVERY day the 0.5→1.0 sweep says
D1 changed — the four known (07-17, 07-21, 07-31, 08-05) plus any the sweep adds — after
the mini-diff reruns. Exact prices, not names alone: projections especially (L2 cannot
recompute your projection value without your boundary ATR).

**Method/scope (2026-08-16, l1-fixes agent):** deterministic pre-computation with an
IN-PROCESS floor override (repo constant still 0.5 — Part A not yet implemented or
committed) over the 20 known 09:20 boundaries (07-14..17, 07-21, 07-27..31, 08-03..07,
08-10..14). These 4 rows are the ONLY changed days among the 20; the full 66-boundary
enumeration (incl. non-09:20 boundaries) rides with Part A's validation sweep. The
"model adopted?" column fills at the mini-diff (needs Part A implemented). Boundary
avg_1h values are baked into the exact prices/ratios below.

| day | dir | old D1 (name @ price, ratio) | new D1 (name @ price, ratio) / NEUTRAL | model adopted? (final bias + selected DOL) | new DOL reached same-day? (time) | notes |
|---|---|---|---|---|---|---|
| 2026-07-17 | DOWN | london(cur)_low @ 28554.75, 0.51× | **NONE — deterministic bias DOWN → no-liquidity NEUTRAL.** projection_down doubly gated: day-stretch 5.21× (>3.0) AND weekly 5.98× BELOW mid (>4.0) | **YES — NEUTRAL, dol=None** (clean FIRST attempt, 0 retries, 0 violations; the pre-raise baseline had tried NEUTRAL and been ARI-forced to DOWN) | n/a | ripple-3 hard case; L2 +91.25 day. Under the new floor this day has NO standing DOWN thesis at 09:20 — your §8 takeover record needs a policy for L2 days without an L1 arm. Restores thesis.md §10's own 07-17 no-liquidity precedent. |
| 2026-07-21 | UP | prev2_day_high @ 29220.0, 0.52× | prev3_day_high @ 29796.5, **6.48× FAR** (band emptied; projection_up blocked by day-stretch 4.85×; weekly 3.67× above — under the 4.0 bar) | **YES — UP + prev3_day_high @ 29796.5** (attempts 0 and 2; retries 2 = baseline's 2, neither DOL-related: 1× SEM weekly_mid ref, 1× ARI on a DOWN wobble; no fallback; wf: DOL not touched, recall fired 10:00) | not reached by 16:59 | bias UP unchanged (net +2.75). FAR-only UP on a day that ran +158 — candidate counter-example for the day-stretch projection gate; watch closely in the mini-diff. |
| 2026-07-31 | UP | london(cur)_high @ 28642.0, 0.92× | prev4_day_high @ 28763.75, 2.01× BAND | **YES — UP + prev4_day_high @ 28763.75** (attempt 1; retries 1 vs baseline 2, not DOL-related: ARI on a DOWN wobble + SEM mid refs; no fallback; wf: falsified 09:50 as the fade set in — the falsifier governs, as predicted) | not reached by 16:59 | bias UP unchanged. Old pool touched +12min then the day faded −121; under the new floor the thesis stands through the fade and the falsifier governs — TP for your replay moves to 28763.75. |
| 2026-08-05 | UP | london(cur)_high @ 29990.75, 0.86× | prev3_week_high @ 30062.5, 1.71× BAND | **YES — UP + prev3_week_high @ 30062.5** (ALL 3 attempts chose it; retries 2 vs baseline 1, neither DOL-related: SEM daily/weekly_mid refs; no fallback. CAVEAT for L2: the model's falsified_if is INVERTED — `price_beyond 29990.75 above` on an UP thesis — so the walk-forward reads falsified 09:33, 12 min BEFORE the genuine 09:45 DOL touch; model-reasoning artifact, not a floor issue) | **09:45:08 (+25min)** | bias UP unchanged. Completes 71.75 pts deeper than the old pool, then the day faded — the clean-improvement case. (Your studied DOWN thesis that day is untouched.) |

**Additional changed boundaries from the full sweep (2026-08-16, Part A execution —
implemented floor, 61 stored boundaries, all NON-09:20 so outside the mini-diff scope).**
None of these flips expected_bias; "dir" is the menu direction whose D1 changed (it can
be the anti-direction of the day's bias — noted). Same-day touch window = boundary→16:59.

| day/boundary | dir | old D1 (name @ price, ratio) | new D1 (name @ price, ratio) | model adopted? | new DOL reached same-day? (time) | notes |
|---|---|---|---|---|---|---|
| 2026-06-30 08:00 | UP | london(cur)_high @ 30217.0, 0.6897× | prev3_day_high @ 30262.5, 1.0845× BAND | n/a (non-09:20, no mini-diff) | 09:37:54 (+98min) | bias UP unchanged (net +0.94); old D1 touched 09:35:18 (+95min) — near-identical touch, clean deepening |
| 2026-07-01 02:00 | DOWN | asia(cur)_low @ 30367.0, 0.6457× | projection_down @ 30270.5, 1.6466× PROJECTION | n/a | 08:39:12 (+399min) | bias UP unchanged (net +1.50) — anti-direction menu gained projection_down (band emptied) |
| 2026-07-01 08:00 | DOWN | london(cur)_low @ 30298.75, 0.845× | projection_down @ 30224.0, 1.8437× PROJECTION | n/a | 09:29:06 (+89min) | bias UP unchanged (net +3.00) — anti-direction menu gained projection_down |
| 2026-07-01 14:00 | DOWN | ny_morning(cur)_low @ 30087.25, 0.8505× | prev1_day_low @ 29935.25, 2.5572× BAND | n/a | not reached by 16:59 | bias DOWN unchanged (net −2.19); old D1 touched 15:57:11 (+117min) |
| 2026-07-02 10:30 | DOWN | london(cur)_low @ 29826.0, 0.826× | projection_down @ 29694.0, 1.8263× PROJECTION | n/a | 11:06:02 (+36min) | bias DOWN unchanged (net −1.75); old D1 touched +12min (the noise-completion class); projection_up already present, projection_down added |
| 2026-07-10 12:00 | UP | prev1_day_high @ 29993.25, 0.5278× | prev4_day_high @ 30094.0, 1.6186× BAND | n/a | not reached by 16:59 | bias NEUTRAL unchanged (net 0.00); old D1 touched +15min |
| 2026-07-16 01:00 | DOWN | asia(cur)_low @ 29593.5, 0.8987× | prev1_day_low @ 29396.75, 2.433× BAND | n/a | 07:49:54 (+410min) | bias UP unchanged (net +2.25) — anti-direction menu |
| 2026-07-16 09:00 | DOWN | prev2_day_low @ 29303.25, 0.7696× | prev1_week_low @ 28910.25, 4.1773× FAR | n/a | not reached by 16:59 | bias DOWN unchanged (net −7.50); the day's OWN direction becomes FAR-only (old D1 touched 09:46 +46min) — same FAR-only pattern as 07-21, watch for L2 |
| 2026-07-23 07:00 | UP | london(cur)_high @ 29183.75, 0.6988× | asia(cur)_high @ 29283.0, 1.5578× BAND | n/a | not reached by 16:59 | bias DOWN unchanged (net −1.88) — anti-direction menu; old D1 also never reached |

Also drop one line here when Part A is committed (hash + whether thesis.md §8 doc rode
along), so the L2 track knows the floor is live before encoding its §11 items.
- Part A commit: **PENDING** (not yet implemented; the rows above are pre-computation).
