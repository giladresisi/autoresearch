# Plan 19 (Part B) — Roll-on-completion ⚠️ Medium

Status: SPEC (2026-08-16). **Blocked on Plan 18 (DOL floor raise) being tested and
committed first** — the floor makes completions rare-but-meaningful; rolling on
noise completions would churn. Game 2: what happens AFTER the (now-deepened) first
DOL is reached. The roll's fresh call is unconstrained — it may continue the L1
direction to a deeper pool or flip; that judgment belongs to the new call, not to
this mechanism.

## Motivation (data)

L1 Phase-2 (15 days @ 09:20): 11/13 directional calls right; 8 of those 11 reached
their DOL within 9–26 minutes and then stood down until the recall cadence —
surrendering the rest of the move (08-04: completed +13min into a +566 day; 08-13:
+16min into a +350 day; 07-27: +13min into a −639 day). The L2 track's §10.3 leaves
"management for a next-deeper DOL that goes unreached same-day" open — this plan is
that answer on the L1 side.

## Rule

When a standing directional thesis dies with cause `completed` **via DOL touch**
(bench `Lifecycle.cause == "completed"` with `dol_touched`, or the `price_beyond(DOL)`
atom of `exhausted_if` firing) while NOT falsified: issue the next L1 call IMMEDIATELY
(next 1m close after the touch) instead of waiting for the recall cadence. Fresh facts;
the touched pool is now swept and drops out of the menu; the next-deeper pool or a
projection becomes the candidate draw. `time_elapsed` exhaustion, TTL, recall, and
falsification keep today's behavior exactly.

Design decisions (defaults; each is a knob):
1. **Trigger scope:** roll iff the fired exhausted_if atom is the DOL `price_beyond`
   (or bench `dol_touched`). Nothing else rolls.
2. **Chain cap:** max 3 rolls per chain (mirrors L2's 3-attempt convention); then fall
   back to normal cadence. Tag `parent_thesis_id` + `roll_index` for audit.
3. **Churn guard:** no roll if the previous roll in this chain was < 15 min ago —
   defer to the next 5m close.
4. **No carryover:** new bias/confidence/DOL from fresh facts; a post-completion flip
   (07-31/08-05 fades) is a feature. No confidence inheritance.
5. **Doc guidance (thesis.md):** DOL-touch exhaustion now means "target reached →
   reassess", so exhausted_if should prefer the pure DOL term; stop padding with
   defensive `time_elapsed` (still legal for genuinely time-boxed reads).
6. **L2/L3 contract:** each roll = NEW thesis_id → L2 re-plans, new plan_id, fresh
   3-attempt budget. No mid-plan mutation.

## Implementation surfaces (in order)

1. Harness: multi-call session walk (`manual-l1-thesis` `--walk` mode or sibling
   script): call 09:20 → walk predicates → on DOL-touch completion re-call at next 1m
   close (cap + churn guard) → aggregate the chain. This is the A/B instrument.
2. Bench: `run_bench` honors completed+dol_touched by scheduling the next decision
   immediately; death causes otherwise unchanged.
3. Production orchestrator adapter: OUT OF SCOPE (thesis.md not in KB_FILES).

## Test plan (named cases)

Deterministic (stub backend):
- [ ] DOL-touch completion triggers exactly one re-call at the next 1m close.
- [ ] time_elapsed exhaustion does NOT roll; falsification does NOT roll.
- [ ] Chain cap blocks the 4th roll; churn guard defers a <15-min repeat.
- [ ] Chain tags (parent_thesis_id, roll_index) present on every rolled result.

LLM A/B (~≤24 calls): roll ON vs OFF on the 8 fast-completion days (07-16, 07-17,
07-21, 07-27, 07-28, 07-31, 08-04, 08-13) + 08-05 (fade) + 08-12. **The OFF arm must
be re-recorded AFTER Plan 18 lands** on any day whose D1 changed (07-17, 07-21, 07-31,
08-05 at minimum) — the current Phase-2 results predate the floor raise there.
- [ ] Trend days (07-27, 08-04, 08-13): chain keeps direction, collects next target(s);
      report cumulative captured range vs single-call.
- [ ] Fade days (07-31, 08-05): roll flips or goes NEUTRAL rather than re-chasing;
      wrong-roll rate = rolls falsified within 30 min.
- [ ] Churn: no chain exceeds 3 rolls; no UP→DOWN→UP inside an hour.
- [ ] Success: captured-range improvement on ≥2 of 3 trend days; wrong-roll rate ≤ 1/3;
      zero validator/fallback regressions vs the OFF arm.

Then: blind forward week (Phase 4) covering L1 fixes + DOL refit + floor + roll.

## Open questions (decide before implementation)

1. Roll timing: next 1m close (default) vs a 5m settle after the touch (fewer
   wrong-rolls on spike-throughs; A/B both if cheap).
2. Should a roll's new falsified_if be constrained by the just-swept pool (leave
   unconstrained by default; watch in the A/B).
3. Projections as roll targets at chain depth ≥2 — BAND-only for late rolls?
