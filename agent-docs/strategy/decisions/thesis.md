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
> menu family — see §8 gaps).

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
| P1 | **HTF close beyond/before at any liquidity sweep** | General acceptance/rejection test at ANY swept level (fixed or running, either asset) — independent of whether an SMT is present there. `n_closes_beyond(price=level, side=sweep_direction, tf, n=1)` true → **accepted beyond** (continuation-leaning); the SAME check on the opposite `side` true instead → **rejected/closed before** (reversal-leaning). Gated by the maturity rule (§3) — a sweep with no qualifying HTF close yet scores ZERO, not a default direction. 4hr close scores more than 1hr (§4). Unlike P2, P1 has NO tier floor — session-tier sweeps still qualify — but points scale by tier (session < day < week, same ladder as P2 — §4), so a pile of session-tier reads cannot numerically out-tally one week-tier read. Evaluated PER ASSET, on that asset's own copy of the level (§5) — see §6 for what it means when the two assets' P1 reads disagree. |
| P2 | **Meaningful SMT + HTF rejection at that liquidity** | Requires ALL of: (a) an SMT divergence present at the level, (b) the level is week-or-day tier or higher (`prev1/2_day_high/low`, `prev1_week_high/low`, `TDO`/`TWO`, running `day_high/low`/`week_high/low`) — explicitly EXCLUDING session-tier (6hr sub-session extremes, `liquidities_session_prior`) and fill/FVG tier, (c) the relevant HTF bar (4hr if it exists over the window, else 1hr) on the LAGGER (see §5 leader/lagger) closed BEFORE — not beyond — the liquidity. Reversal-only by design (no symmetric "SMT + accept-beyond" bonus form — an accepted push is scored by P1 on its own). Points scale with tier (week > day). **Deliberately stacks with P1** when both independently fire on the same physical sweep — this is not double-counting-as-bug, it is P2 rewarding the specific SMT-plus-genuine-rejection combination as a stronger tell than either alone; do not dedupe P1↔P2. |
| P3 | **Position vs. equilibrium — daily/weekly weight set by phase, not fixed** | Current price vs. daily mid AND vs. weekly mid (`equilibrium.md` §1 definitions: running-extreme midpoints), evaluated PER ASSET. The daily-vs-weekly split is NOT fixed 50/50 — see §2.1a for the phase rule that sets which one dominates a given call. See §6 for what it means when the two assets agree on one equilibrium (e.g. both above weekly mid) but disagree on the other (e.g. split daily-mid placement). |
| P4 | **Reclaim / failed reclaim of daily or weekly mid, HTF-confirmed** | A close-beyond-then-fails-back (or fails-to-reclaim) sequence on the daily OR weekly mid, confirmed specifically by an HTF (1h/4h) close — not any close. Daily and weekly weighted equally; 4hr scores more than 1hr (same scaling as P1). Note: `equilibrium.md` §3 and `next-move.md` mark plain weekly-mid TOUCH/CROSS as proven noise (GIL-39 B, +$83 shipped suppressing it as a trigger) — P4's weekly leg is a materially different, stricter signal (HTF-close-confirmed reclaim/failed-reclaim, not a bare touch/cross), so it is enabled alongside the daily leg without a separate gate. The daily half is parallel to the existing "failed reclaim of daily mid" item in `next-move.md` §2 (weight 2, "the strongest intraday tell"). |

### 2.1a P3 daily/weekly weighting — set by which reference was touched most recently

P3's daily-vs-weekly weight is dynamic, not fixed equal. Compare, across BOTH assets, the most
recent time price touched a DAILY EXTREME (running `day_high`/`day_low` — the extreme-set
timestamp already rendered in S1) against the most recent time price touched/crossed the DAILY
EQUILIBRIUM (the daily mid — the mid-cross timestamps already rendered in S4). Take the single
most recent of the four candidate timestamps (MNQ-extreme, MNQ-mid, MES-extreme, MES-mid):

- **Most recent touch was the DAILY EQUILIBRIUM** → price has just left the mid and is now
  moving TOWARD a daily extreme. How far that move ultimately reaches is governed more by the
  broader weekly context than by the day's own range — weight the **weekly** equilibrium
  position HIGHER than the daily for this call.
- **Most recent touch was a DAILY EXTREME** → price has just left the extreme and is now moving
  back TOWARD the daily equilibrium (intraday mean-reversion). That move is a narrower, day-scoped
  dynamic, less governed by the weekly trend — weight the **daily** equilibrium position HIGHER
  than the weekly for this call.

**Gap:** neither "latest daily-extreme touch time" nor "latest daily-mid touch/cross time" is
currently a structured `FactsBundle` field per asset — both exist only in RENDERED TEXT today
(S1's extreme-set timestamps, S4's mid-cross tables). A facts-layer addition (parallel to
`weekly_mid`/`swept_at`) is needed before this phase check is machine-checkable; until then it's
a reasoning-time read off the rendered S1/S4 text — flag it unverifiable-load-bearing (one
confidence tier down, existing veto pattern) if the model can't cite the exact timestamps it
compared.

### 2.1b Nested/superseded prior-liquidity levels are IGNORED, not scored

Within a same-side family of prior-liquidity levels — prev-day highs (`prev1_day_high`,
`prev2_day_high`, ...), prev-day lows, prev-week highs/lows, and any deeper history the facts
track — a level is eligible evidence for P1/P2/P4/DOL ONLY if it sits BEYOND every MORE RECENT
level in that same family. Scan a family from most-recent to oldest; drop any level that does not
exceed (is not beyond) the extreme already established by a more recent one in the same family —
it is nested inside already-superseded liquidity, not an independent pool. Applies per asset
(MNQ's own family, MES's own family, separately).

**Worked example:** `prev1_day_high` (yesterday's high) sits ABOVE `prev2_day_high` (the day
before). `prev1_day_high` is the relevant, farther-out high-side pool; `prev2_day_high` does NOT
extend beyond it, so **`prev2_day_high` is IGNORED** — it must not be scored as an independent P1
accept/reject event, must not count toward P2's meaningful-tier check on its own, and must not be
offered as a DOL. The identical rule applies to prev-day lows (the most recent low is relevant
unless an older low sits farther below it) and prev-week highs/lows.

This filter is orthogonal to, not a replacement for, tier weighting (§4) — it prunes WHICH levels
within a family are even eligible before tier weight is applied to whichever survives. It does
NOT extend to session-tier sub-blocks (`asia`/`london`/`ny_morning`/`ny_evening` `(cur)`/`(prev1)`
highs/lows) — those are a different, non-chronological-family naming scheme and P2 already
excludes session-tier outright (§2.1); this filter is specifically about prev-day/prev-week
nesting.

**Why this matters:** without it, a pile of nested, lower-significance echoes of the SAME
underlying extreme can numerically out-tally one genuinely meaningful (often week-tier) signal.
This is exactly what happened in the 2026-07-01 12:00 ET test (§6's worked example): the model's
own tally counted MNQ's accept on `prev2_day_high` as independent UP-supporting evidence, when
`prev2_day_high` sat below `prev1_day_high` and should have been dropped entirely — removing one
of the lower-tier accepts that out-voted the single meaningful week-tier reject.

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
  unverifiable load-bearing input, low/neutral standing bias, range/hybrid regime. §6's
  contradiction cap is a NEW addition to this list, not a replacement for it.

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
  no criterion among the four is a priori stronger; whichever fire, fire at par. This net score
  still determines DIRECTION even under a §6 cross-asset contradiction — §6 caps the confidence
  CEILING after scoring (same as any other veto/cap), it does not override or replace the net
  score itself.
- Within P1, P2, and P4: a qualifying **4hr** close scores strictly more than a qualifying
  **1hr** close (both are eligible per the maturity gate in §3; 4hr is simply worth more).
- Within P1 and P2: points scale further by liquidity tier — week-tier scores more than
  day-tier scores more than session-tier. P1 has no tier FLOOR (even session-tier sweeps
  qualify for P1, just at the lowest multiplier — §2.1); P2's tier floor is day-or-higher
  (session-tier does not qualify for P2 at all — §2.1). Code-derived v1 seed multipliers
  (`validate_contracts.score_thesis_evidence`): session ×0.5, day ×0.75, week ×1.0, same
  ladder applied to both criteria.
- Secondary criteria (§2.2) are capped at a LOWER max than any single P1–P4 criterion. Their
  role is to accumulate when they agree with each other and/or with the P1–P4 read, nudging
  confidence — they cannot outweigh the primary four on their own.
- Exact point values are v1 seeds pending a dedicated calibration/bench pass (same status as
  every number in `daily-trend.md`/`next-move.md`) — this doc fixes the STRUCTURE (equal-weight
  primaries, subordinate secondaries, the maturity gate, the §6 cap), not final numbers.

## 5. Both-asset requirement (leader/lagger)

MNQ remains the decision ticker (the executor trades MNQ), but **P1, P2, P3, and P4 must each be
evaluated on BOTH MNQ and MES at full weight** — do not apply `next-move.md`'s existing MES×0.5
ticker-scope discount to the four primaries. At times MES's own sweep/HTF-close/equilibrium
behavior is the actual trigger for, or the clearer explanation of, a meaningful move, and must be
considered as such rather than treated as a discounted echo of MNQ. The existing ×0.5 MES-only
discount is preserved for the SECONDARY criteria in §2.2 only (unchanged from `next-move.md`).

**Leader/lagger, for P1/P2's close-beyond/before read specifically:** in a live (unresolved) SMT,
the leader is the asset that failed to reach/confirm the level — it never traded through it, so
there is no accept-vs-reject close question to ask on the leader's side at THAT cross-ticker
comparison. Only the lagger (the asset that actually pushed through) has price action there to
classify via P1/P2's cross-ticker read. This is distinct from the SMT's own negation/expiry
condition (§7) — whether the LEADER later also confirms beyond the level, dissolving the
divergence entirely — which is a different check, not a close/wick read of the same push. P2's
"relevant HTF bar" (§2.1) is always the lagger's.

**This does NOT mean the leader's own price is unreadable.** §2.1's P1 runs per-asset on EACH
asset's own copy of a level (level names are shared, e.g. `prev1_day_high`, but each ticker's
price against that name is its own read). An asset can simultaneously (a) be the unswept
leader relative to the OTHER asset's cross-ticker sweep comparison at a shared level, while (b)
its own price still touches/wicks its own copy of that same level without closing beyond it —
both facts are real and both get read; see §6 for why the combination matters more than either
one alone.

## 6. Cross-asset primary-evidence contradiction (P1/P3 disagreement across MNQ/MES)

Running P1 and P3 per-asset (§5) can surface **direct disagreement between the two assets at the
same or an analogous meaningful level** — one asset's HTF read is ACCEPTED beyond, the other's is
REJECTED (wicked, closed back before); or the two assets agree on one equilibrium (e.g. both
above weekly mid) but disagree on the other (split daily-mid placement). This is a different
state from "evidence is thin" or "a veto fired" — it is evidence that directly opposes itself
across two correlated assets, and simply netting points across it treats a genuine unresolved
contradiction as noise to average out.

**Detection.** A primary-criterion contradiction exists when, at a shared level name (or its
per-asset analog):
- P1 fires ACCEPT-beyond on one asset's own qualifying HTF close and REJECT-before on the other
  asset's own qualifying HTF close at that level — the per-asset read from §5, not only the
  cross-ticker sweep/no-sweep comparison that feeds P2 — OR
- P3's daily-equilibrium placement disagrees between the two assets while their weekly-equilibrium
  placement agrees (or vice versa).

**Effect — tally normally, but cap the ceiling.** A contradiction does NOT mean discarding §4's
equal-weight net score in favor of a pure LTF read — score P1/P2/P3/P4 as usual, per-asset where
§5 applies. P2 is reversal-only by construction (§2.1), so a meaningful SMT ALWAYS adds real
weight to the reversal side specifically — it is not neutralized just because the OTHER asset's
P1 separately accepted-beyond; that is a genuine second, independent point on the reversal side,
not a wash against the accept. What the contradiction DOES do is cap the confidence CEILING: even
a clear net-score lean (e.g. 2 primary points one way vs 1 the other) tops out at MEDIUM, never
HIGH, while the contradiction is live. If the net score itself is genuinely thin even after
correct tallying (no clear lean either way), confidence drops to LOW instead.

**Direction — the net score, corroborated (not decided) by LTF.** Direction follows whichever
side the net score leans, same as any normal call (§4) — a contradiction changes the confidence
CEILING, not how direction is picked. Use the shortest available LTF read (30m–1h price drift on
both assets) as corroboration only: agreeing with the net-score lean is consistent with MEDIUM;
LTF disagreeing with the net-score lean too is a second, independent disagreement and drops
confidence to LOW instead of MEDIUM.

**Recall — short, and the trigger for raising to HIGH.** A thesis issued in a contradiction state
must set a SHORT `recall.max_age_min` (~30 minutes, not the standard longer window), specifically
to re-check whether the contradiction is resolving — whether the asset that had been
accepting-beyond starts showing its own rejection symptoms (closing back below/before a level it
had just accepted, even a partial one), and/or the rejecting asset's read holds or strengthens (a
decisive close with no contrary wick). Raise confidence to HIGH on the FIRST subsequent call
where the drag is unambiguous — a clean, aligned bar on BOTH assets in the net score's direction —
not merely "still not falsified." A partial/early drag sign (one asset starting to crack but not
yet a clean aligned bar) keeps confidence at MEDIUM with another short recall, rather than jumping
straight to HIGH.

**Worked example (2026-07-01 12:00→12:30 ET; user chart review, not independently re-verified
against raw bars the way §7's July 1/July 2 example was):**

*12:00 ET call.* Net-score tally: DOWN gets 2 primary points — MNQ's own P1 REJECT (wicked above
its own copy of yesterday's/the week's high on 1h/4h, closed back below/under equilibrium, not
reclaimed) PLUS a meaningful week-tier P2 SMT (MES swept/lagger, MNQ unswept/leader at that same
level — P2 is reversal-only, so it adds weight specifically to DOWN). UP gets 1 primary point —
MES's own P1 ACCEPT (both 1h and 4h closed beyond the same weekly high). Both assets sat above
weekly equilibrium (counted toward UP here) but split on daily-equilibrium placement — a P3
contradiction on the daily leg specifically, not the weekly leg. Net score: DOWN leans 2-to-1; 30m
LTF drift corroborated, already drifting down on both assets. Correct call: **bias DOWN,
confidence MEDIUM** (a real net-score lean, but the live P1 contradiction caps it below HIGH),
`recall.max_age_min` ≈30.

*12:30 ET recall.* MNQ's next 30m bar closed below equilibrium WITHOUT even wicking above (its
rejection read holds and strengthens — no contrary wick this time). MES's next 30m bar closed
below the new high it had just accepted-beyond, despite a slight wick still poking above it (the
accepting asset starting to crack — the drag signal). This is a clean, aligned bar on the DOWN
side for both assets with no meaningful contradiction remaining. Correct call: **raise to HIGH**
on this recall. (A further 13:00 ET bar showing a strong decisive down move on both assets — the
drag completing outright — reinforces an already-HIGH read at that point; the trigger was the
first clean aligned bar, which arrived at 12:30, not that later one.)

The live manual test on the original 12:00 timestamp (`manual-l1-thesis/`) returned bias UP at
MEDIUM confidence before this section existed — the miss that motivated writing it. This section's
own first draft then over-corrected into discarding the net score in favor of a pure LTF
tie-break under any contradiction, capping confidence at LOW/MEDIUM with no path back to HIGH —
the revision above (tally normally, cap the ceiling, raise on a clean drag) is the fix.

## 7. SMT lifecycle (pending → confirmed → expired)

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

## 8. Predicate mapping & known gaps

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
  existing `day_mid`) before it is machine-checkable. Per the sandwich principle
  (`agent-optimizations.md` §1), the model must not compute this itself; it is a facts layer
  prerequisite.
- §2.1a's daily/weekly phase weighting needs per-asset "latest daily-extreme touch" and "latest
  daily-mid touch/cross" as structured facts (currently rendered-text-only in S1/S4) — see §2.1a's
  own gap note.
- DOL selection is unchanged from the existing S8 `_dol_menu`/validator contract: nearest
  meaningful, in-facts, unswept, undepleted, correct-side pool ≥ `DOL_MIN_DRAW_DISTANCE_PTS`
  (5.0) away — prefer the DOL menu's `D*` IDs directly.
- **Gap:** the code-derived evidence ledger (§9; `validate_contracts.score_thesis_evidence`)
  covers P1 and P2 only — both share the same accept/reject-of-a-sweep shape (level, tier, tf,
  direction). P3 (equilibrium position — no sweep, no accept/reject) and P4 (reclaim/failed-
  reclaim — a different two-step shape) are NOT yet expressible in this ledger and remain
  reasoning-only, scored by the model's self-report the way the whole thesis was before this
  gap was partially closed. A future ledger shape for P3/P4 is a natural follow-up, not done
  here (deliberately scoped to the two criteria that produced the observed 2026-07-02 08:00
  bugs — sign misclassification and bias-contradicts-net-score, both P1-driven).
- **Gap:** §6's cross-asset contradiction detection in `score_thesis_evidence` only catches an
  EXACT shared level name evidenced with differing `direction` on both assets (e.g. both assets
  carry a P1 item on `prev1_day_high`). It does not yet resolve "analogous" levels with
  different names across assets (§6's own text allows for this) — a real cross-name analog
  contradiction would currently net-score correctly but NOT trigger the confidence-ceiling cap.

## 9. Output schema

Per `agent-optimizations.md` §2.1 exactly — this doc does not alter the contract, only how the
model should arrive at the values:

```
thesis:
  bias: UP | DOWN | NEUTRAL
  regime: TREND | RANGE | HYBRID
  dol: {level, price}                     # only if bias != NEUTRAL; from S8 DOL menu
  falsified_if: [ <predicate> ]           # closed vocab, §6/§7; prefer menu IDs, else escape hatch
                                           # code-rejected if already true at the current price —
                                           # thesis.falsified_if cannot self-invalidate at issuance
                                           # (validate_contracts XL_FALSIFIED_IF_ALREADY_TRUE)
  exhausted_if: [ <predicate> ]           # typically DOL touch + optional beyond/time terms
                                           # same issuance-time check (XL_EXHAUSTED_IF_ALREADY_TRUE)
  confidence: HIGH | MEDIUM | LOW          # code-derived from the evidence ledger below
                                           # (validate_contracts.score_thesis_evidence); self-report
                                           # is a starting point, silently clamped to the computed
                                           # ceiling. Net score still applies under a §6 contradiction;
                                           # the contradiction caps the CEILING at MEDIUM (LOW if the
                                           # net score is itself thin) until a clean aligned bar raises
                                           # it — that RAISE is not yet automated (§8/standing-thesis
                                           # gap), only the cap direction is code-enforced today.
  recall: {events: [ <predicate> ], max_age_min}  # ≈30min under a §6 contradiction — the check for
                                                  # whether it has resolved (drag), not just a re-ask
  evidence: [ <P1/P2 item> ]              # model declares criterion/asset/level/tier/tf/direction/
                                           # mature per item (§2.1); code derives each item's UP/DOWN
                                           # sign from the level's own high/low identity (never
                                           # model-declared — closes the P1 sign-misclassification
                                           # bug) plus its points (tier x tf multiplier, zeroed if
                                           # immature), sums to a net score, and REJECTS (retry, not
                                           # silent override — same split as daily-trend/next-move) a
                                           # declared bias inconsistent with that net score's sign
                                           # (validate_contracts ARI_THESIS_BIAS). P3/P4 are NOT yet
                                           # in this ledger — reasoning-only still (§8 gap).
  reasoning: audit-only
```

## 10. Failure modes this doc encodes

Acting on raw SMT at discovery (Phase-3, −$3,756/5d) → §7 confirmation gate · immature-sweep
false signal (no HTF close yet, direction assumed) → §3 maturity gate · HTF-close-as-hard-gate
falsified by the July 1 vs July 2 case (close-beyond followed by a BIGGER reversal, not a
negation) → §7 step 3 corrected model · MES treated as a discounted echo when it was the actual
trigger → §5 both-asset requirement · conflating plain weekly-mid touch/cross (proven noise,
GIL-39) with the HTF-close-confirmed reclaim/failed-reclaim P4 actually scores → §2.1 P4 note,
these are different signals · netting a genuine cross-asset P1/P3 contradiction into a
false-confident directional score (2026-07-01 12:00 ET case: bias UP/MEDIUM when the correct read
was DOWN/MEDIUM, raised to HIGH at the 12:30 recall on a clean aligned bar) → §6 contradiction cap
· discarding the net score entirely under a contradiction in favor of a pure LTF tie-break with no
path back to HIGH (this section's own first draft) → §6's revised Effect/Direction/Recall rules ·
one-event-many-votes (daily-trend.md run-1 failure) → §2.2 secondary pool retains the
correlation-audit/dedup discipline; P1↔P2 stacking is the ONE deliberate exception, called out
explicitly in §2.1 P2 so it is never "fixed" as if it were the same bug · P1 sign
misclassification (2026-07-02 08:00 ET: a rejected LOW-side sweep labeled "bearish" when
rejecting a low sweep is bullish by P1's own rule) → §9 evidence ledger, sign code-derived from
level polarity, never model-declared · bias contradicting its own net-score tally (same
2026-07-02 08:00 run: reasoning stated the net score leaned UP, declared bias DOWN anyway) → §9
ARI_THESIS_BIAS retry gate (`validate_contracts.score_thesis_evidence`) · falsified_if issued
already-true against current price (same run: falsified_if anchored 5pts from a price already on
the wrong side of it, self-invalidated 10 minutes later regardless of what the market did) → §9
XL_FALSIFIED_IF_ALREADY_TRUE / XL_EXHAUSTED_IF_ALREADY_TRUE issuance-time consistency gate,
reusing the existing stop-vs-falsification `MarketView.price_only()` cross-level pattern one
level earlier.
