# DECISION: Level-1 Thesis (direction · regime · DOL · falsification/exhaustion)

> Pure policy — every change to weights/thresholds/predicates is a strategy change: **backtest/
> bench before merging**. All numbers are v1 seeds pending calibration. AUTHORING ONLY at this
> stage: this doc is NOT wired into `run_agent.KB_FILES` yet, and `daily-trend.md` / `next-move.md`
> remain the live KB for shadow mode / the A/B baseline until a gated cutover (see
> `agent-optimizations.md` §2.1, §11 "KB doc restructuring"). This doc cherry-picks and
> supersedes the DIRECTIONAL content of both — it is not a merge in place.
>
> Output target: ONE standing `thesis.json` per `agent-optimizations.md` §2.1 — bias, regime,
> DOL, `falsified_if`, `exhausted_if`, `confidence` (code-derived, self-report is audit-only §8),
> `recall`. Predicates use the CLOSED vocabulary of §6 only: `price_beyond`, `n_closes_beyond`,
> `level_swept`, `level_depleted`, `time_elapsed`, `clock_after`, `all_of`/`any_of` (depth 1).
> Prefer S8 menu items (IDs `D*`/`F*`/`X*`/`R*`, `derive_facts.render_menus_text`) when a menu
> item fits; otherwise use the documented escape hatch — any schema-valid, facts-grounded
> predicate built from the same atoms (e.g. `n_closes_beyond(tf="1h"/"4h")`, not yet a generated
> menu family — see §7 gaps).

## 1. Cadence

Event-driven only (`agent-optimizations.md` §3/§5): the mandatory 18:00 ET session-open call,
plus recall events / TTL expiry / `falsified_if` / `exhausted_if` firing. No fixed intraday
checkpoints (06:00/09:20/13:00 are retired — §11 reuse map). A flip in bias still requires the
hysteresis baked into whichever predicate fired (e.g. `n_closes_beyond(..., n=2)`), not a raw
re-narration of the same evidence on every call.

## 2. Evidence catalog — four PRIMARY criteria (equal max weight) + secondary (lower, accumulating)

Fresh tape and standing/slow context are no longer split into separate documents (that split was
daily-trend vs next-move) — one evidence ledger, scored per call, gated by the maturity rule in
§3.

### 2.1 Primary criteria (each capped at the same max points — see §4)

| # | Criterion | Definition |
|---|---|---|
| P1 | **HTF close beyond/before at any liquidity sweep** | General acceptance/rejection test at ANY swept level (fixed or running, either asset) — independent of whether an SMT is present there. `n_closes_beyond(price=level, side=sweep_direction, tf, n=1)` true → **accepted beyond** (continuation-leaning); the SAME check on the opposite `side` true instead → **rejected/closed before** (reversal-leaning). Gated by the maturity rule (§3) — a sweep with no qualifying HTF close yet scores ZERO, not a default direction. 4hr close scores more than 1hr (§4). |
| P2 | **Meaningful SMT + HTF rejection at that liquidity** | Requires ALL of: (a) an SMT divergence present at the level, (b) the level is week-or-day tier or higher (`prev1/2_day_high/low`, `prev1_week_high/low`, `TDO`/`TWO`, running `day_high/low`/`week_high/low`) — explicitly EXCLUDING session-tier (6hr sub-session extremes, `liquidities_session_prior`) and fill/FVG tier, (c) the relevant HTF bar (4hr if it exists over the window, else 1hr) on the LAGGER (see §5 leader/lagger) closed BEFORE — not beyond — the liquidity. Reversal-only by design (no symmetric "SMT + accept-beyond" bonus form — an accepted push is scored by P1 on its own). Points scale with tier (week > day). **Deliberately stacks with P1** when both independently fire on the same physical sweep — this is not double-counting-as-bug, it is P2 rewarding the specific SMT-plus-genuine-rejection combination as a stronger tell than either alone; do not dedupe P1↔P2. |
| P3 | **Position vs. equilibrium** | Current price vs. daily mid AND vs. weekly mid (`equilibrium.md` §1 definitions: running-extreme midpoints). Daily and weekly weighted EQUALLY (v1 seed — not yet established that weekly should outweigh daily). **Gap:** `weekly_mid` is not currently an exposed fact field (only `day_mid` is — `derive_facts.py` `FactsBundle.day_mid`); a code addition is needed before this half of P3 is machine-checkable. Until then, treat the weekly leg as context-only / low-confidence input, flagged as unverifiable load-bearing (one confidence tier down, per the existing veto pattern). |
| P4 | **Reclaim / failed reclaim of daily or weekly mid, HTF-confirmed** | A close-beyond-then-fails-back (or fails-to-reclaim) sequence on the daily OR weekly mid, confirmed specifically by an HTF (1h/4h) close — not any close. Daily and weekly weighted equally; 4hr scores more than 1hr (same scaling as P1). Note: `equilibrium.md` §3 and `next-move.md` mark plain weekly-mid TOUCH/CROSS as proven noise (GIL-39 B, +$83 shipped suppressing it as a trigger) — P4's weekly leg is a materially different, stricter signal (HTF-close-confirmed reclaim/failed-reclaim, not a bare touch/cross), so it is enabled alongside the daily leg without a separate gate. The daily half is parallel to the existing "failed reclaim of daily mid" item in `next-move.md` §2 (weight 2, "the strongest intraday tell"). |

### 2.2 Secondary criteria (lower max weight; accumulate only if aligned)

Carried over from the existing docs, capped below the P1–P4 max (§4) — confirming color, not a
primary mover:

- **D1** HTF structure chain (4hr sets sign, 1hr modulates) · **D3** prior-day/week context ·
  **D4** overnight sweep complex (pre-06:00 Asia/London manipulation, one unit) · **D6**
  ATH/recovery regime — from `daily-trend.md` §2.
- Fresh SMT divergence as a plain ledger item (i.e. NOT meeting P2's meaningful-tier + HTF-reject
  bar) · sweep grab-vs-continuation classification outside the HTF-close lens
  (`liquidity-levels.md` §2 table) · continuation items (a) mid-rejection / (b) sustained
  acceptance · laggard-fail CANDIDATE item · clusters (≥3 distinct events, ×1.5 once) — from
  `next-move.md` §2.
- All existing multipliers still apply within this secondary pool: tier weight, session-side,
  alignment-vs-standing-bias, freshness decay, whipsaw dampener (`next-move.md` §2–3).
- Vetoes/caps (`next-move.md` §4) still apply post-scoring: fresh top-pocket counter-signal,
  unverifiable load-bearing input, low/neutral standing bias, range/hybrid regime.

## 3. Maturity gate (applies to P1, P2, and the HTF-close half of P4)

A liquidity sweep is **not usable evidence in either direction** until at least one HTF bar
(≥1hr) has **closed** since the sweep occurred. Before that closes: the sweep sits in a pending/
suspicious state — it must not be scored as continuation OR reversal evidence, and must not
silently default toward either. Once ≥1 qualifying HTF bar has closed: the close-beyond/
close-before read becomes scorable per P1/P2/P4. A 4hr close occurring since the sweep makes the
read admissible at the higher (4hr) weight tier in §4; absent a 4hr close yet, a 1hr close makes
it admissible at the lower tier. This is a strict gate, not a discount — an immature sweep
contributes zero to P1/P2/P4, not a partial or default-direction score.

## 4. Weighting

- **P1–P4 are weighted EQUALLY against each other** (same max achievable points per criterion) —
  no criterion among the four is a priori stronger; whichever fire, fire at par.
- Within P1, P2, and P4: a qualifying **4hr** close scores strictly more than a qualifying
  **1hr** close (both are eligible per the maturity gate in §3; 4hr is simply worth more).
- Within P2: points scale further by liquidity tier — week-tier scores more than day-tier (both
  qualify; session-tier and below do not qualify for P2 at all — §2.1).
- Secondary criteria (§2.2) are capped at a LOWER max than any single P1–P4 criterion. Their
  role is to accumulate when they agree with each other and/or with the P1–P4 read, nudging
  confidence — they cannot outweigh the primary four on their own.
- Exact point values are v1 seeds pending a dedicated calibration/bench pass (same status as
  every number in `daily-trend.md`/`next-move.md`) — this doc fixes the STRUCTURE (equal-weight
  primaries, subordinate secondaries, the maturity gate), not final numbers.

## 5. Both-asset requirement (leader/lagger)

MNQ remains the decision ticker (the executor trades MNQ), but **P1, P2, P3, and P4 must each be
evaluated on BOTH MNQ and MES at full weight** — do not apply `next-move.md`'s existing MES×0.5
ticker-scope discount to the four primaries. At times MES's own sweep/HTF-close/equilibrium
behavior is the actual trigger for, or the clearer explanation of, a meaningful move, and must be
considered as such rather than treated as a discounted echo of MNQ. The existing ×0.5 MES-only
discount is preserved for the SECONDARY criteria in §2.2 only (unchanged from `next-move.md`).

**Leader/lagger, for P1/P2's close-beyond/before read specifically:** in a live (unresolved) SMT,
the leader is the asset that failed to reach/confirm the level — it never traded through it, so
there is no accept-vs-reject close question to ask on the leader's side at that level. Only the
lagger (the asset that actually pushed through) has price action there to classify via P1/P2.
This is distinct from the SMT's own negation/expiry condition (§6) — whether the LEADER later
also confirms beyond the level, dissolving the divergence entirely — which is a different check,
not a close/wick read of the same push. P2's "relevant HTF bar" (§2.1) is always the lagger's.

## 6. SMT lifecycle (pending → confirmed → expired)

1. **Discovery** — a raw cross-ticker divergence at a level is a bias flag only, never acted on
   at discovery (~9% of raw fires precede an actual trend change; ~15% are wrong-way).
2. **Confirmation** — requires ALL three, checked on the LAGGER's own structure (independent
   tracker per asset; today's `trend.py` only tracks MNQ structure — a parallel MES tracker is a
   code prerequisite, not yet built):
   - **Structural break:** lagger's close moves beyond its own most recent swing point, real-body
     close (not a wick tag).
   - **Displacement:** that break bar's range is a clear multiple (~2–3×) of the recent local
     average bar range.
   - **FVG:** a 3-bar imbalance forms in the break direction.
   All three together = confirmed reversal. Any one missing (especially the FVG) = noise, keep
   waiting — a mere stall is NOT sufficient; it can still resolve back into the original trend.
3. **HTF close read (P1/P2)** is a magnitude/confluence weight layered on a confirmed or
   forming reversal — NOT a gate that can block or substitute for step 2's confirmation. A HTF
   close-beyond can still be followed by a bigger reversal (more trapped participants fueling a
   sharper unwind once step 2 fires) — it is evidence about eventual size, not a veto.
4. **Invalidation/expiry** — if the lagger instead matches/exceeds the leader's continuation
   (both assets re-align on the original trend) before step 2 ever fires, the pending SMT is
   killed — it does not sit as latent bias indefinitely. Expect multi-session lag between
   discovery and confirmation (observed: >1 day) — lag alone is never invalidation, only this
   explicit re-alignment is.

## 7. Predicate mapping & known gaps

- P1/P4's HTF close tests map directly to `n_closes_beyond(price, side, tf, n=1)` with
  `tf="1h"` or `tf="4h"` — both already valid atoms in the closed vocabulary
  (`agent-optimizations.md` §6). **Gap:** `derive_facts._MENU_PREDICATE_CFG` does not yet
  generate 1h/4h variants, a `weekly_mid` level-class, or a general swept-level close-class — the
  S8 menu currently only offers 5m daily-mid variants (falsification/recall) and `price_beyond`
  for pools/anti-pools. Until a code change adds these menu families, the model must use the
  documented escape hatch (any schema-valid, facts-grounded predicate) to express P1/P2/P4 reads
  in `falsified_if`/`exhausted_if`/`recall.events` — this doc does not invent a new predicate
  type, it composes existing atoms the generator hasn't been configured to enumerate yet.
- P3's weekly-mid leg needs the `weekly_mid` fact field added to `FactsBundle` (parallel to the
  existing `day_mid`) before it is machine-checkable — see §2.1 P3 gap note. Per the sandwich
  principle (`agent-optimizations.md` §1), the model must not compute this itself; it is a facts
  layer prerequisite.
- DOL selection is unchanged from the existing S8 `_dol_menu`/validator contract: nearest
  meaningful, in-facts, unswept, undepleted, correct-side pool ≥ `DOL_MIN_DRAW_DISTANCE_PTS`
  (5.0) away — prefer the DOL menu's `D*` IDs directly.

## 8. Output schema

Per `agent-optimizations.md` §2.1 exactly — this doc does not alter the contract, only how the
model should arrive at the values:

```
thesis:
  bias: UP | DOWN | NEUTRAL
  regime: TREND | RANGE | HYBRID
  dol: {level, price}                     # only if bias != NEUTRAL; from S8 DOL menu
  falsified_if: [ <predicate> ]           # closed vocab, §6/§7; prefer menu IDs, else escape hatch
  exhausted_if: [ <predicate> ]           # typically DOL touch + optional beyond/time terms
  confidence: HIGH | MEDIUM | LOW          # code-derived (agent-optimizations.md §8); self-report audit-only
  recall: {events: [ <predicate> ], max_age_min}
  reasoning: audit-only
  # audit-only evidence table (not part of thesis.json; logged for calibration/backtest):
  evidence:
    P1..P4: <criterion, asset, tier/tf, direction, points, maturity-gate status>
    secondary: <criterion, points, alignment>
```

## 9. Failure modes this doc encodes

Acting on raw SMT at discovery (Phase-3, −$3,756/5d) → §6 confirmation gate · immature-sweep
false signal (no HTF close yet, direction assumed) → §3 maturity gate · HTF-close-as-hard-gate
falsified by the July 1 vs July 2 case (close-beyond followed by a BIGGER reversal, not a
negation) → §6 step 3 corrected model · MES treated as a discounted echo when it was the actual
trigger → §5 both-asset requirement · conflating plain weekly-mid touch/cross (proven noise,
GIL-39) with the HTF-close-confirmed reclaim/failed-reclaim P4 actually scores → §2.1 P4 note,
these are different signals · one-event-many-votes (daily-trend.md run-1 failure) → §2.2
secondary pool retains the correlation-audit/dedup discipline; P1↔P2 stacking is the ONE
deliberate exception, called out explicitly in §2.1 P2 so it is never "fixed" as if it were the
same bug.
