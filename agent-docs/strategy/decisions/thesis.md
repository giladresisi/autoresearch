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

**Session-anchored recall.** When current evidence is thin or immature (§2.2, §3), prefer a
`clock_after` at the NEXT sub-session boundary (00:00 / 06:00 / 12:00 ET, or the 18:00 open) as
the recall event, rather than only an arbitrary minutes-elapsed count. A sub-session boundary is
where new manipulation/distribution structure actually forms, so it is a more meaningful re-ask
time than "60 minutes from now". The S8 menu now generates this as an `R*` entry
(`next_subsession`, `derive_facts._next_subsession_boundary` → `clock_after(et_time=…)`) — copy
that entry's `et_time` rather than inventing one.

## 2. Evidence catalog — four PRIMARY criteria (equal max weight) + secondary (lower, accumulating)

Fresh tape and standing/slow context are no longer split into separate documents (that split was
daily-trend vs next-move) — one evidence ledger, scored per call, gated by the maturity rule in
§3.

### 2.1 Primary criteria (each capped at the same max points — see §4)

| # | Criterion | Definition |
|---|---|---|
| P1 | **HTF close beyond/before at any liquidity sweep** | General acceptance/rejection test at ANY swept level (fixed or running, either asset) — independent of whether an SMT is present there. `n_closes_beyond(price=level, side=sweep_direction, tf, n=1)` true → **accepted beyond** (continuation-leaning); the SAME check on the opposite `side` true instead → **rejected/closed before** (reversal-leaning). Gated by the maturity rule (§3) — a sweep with no qualifying HTF close yet scores ZERO, not a default direction. 4hr close scores more than 1hr (§4). Unlike P2, P1 has NO tier floor — session-tier sweeps still qualify — but points scale by tier (session < day < week, same ladder as P2 — §4), so a pile of session-tier reads cannot numerically out-tally one week-tier read. Evaluated PER ASSET, on that asset's own copy of the level (§5) — see §6 for what it means when the two assets' P1 reads disagree. |
| P2 | **Meaningful SMT + HTF rejection at that liquidity** | Requires ALL of: (a) an SMT divergence present at the level, (b) the level is week-or-day tier or higher (`prev1/2_day_high/low`, `prev1_week_high/low`, `TDO`/`TWO`, running `day_high/low`/`week_high/low`) — explicitly EXCLUDING session-tier (6hr sub-session extremes, `liquidities_session_prior`) and fill/FVG tier, (c) the relevant HTF bar (4hr if it exists over the window, else 1hr) on the LAGGER (see §5 leader/lagger) closed BEFORE — not beyond — the liquidity. Reversal-only by design (no symmetric "SMT + accept-beyond" bonus form — an accepted push is scored by P1 on its own). Points scale with tier (week > day). **Deliberately stacks with P1** when both independently fire on the same physical sweep — this is not double-counting-as-bug, it is P2 rewarding the specific SMT-plus-genuine-rejection combination as a stronger tell than either alone; do not dedupe P1↔P2. |
| P3 | **Position vs. equilibrium — daily/weekly weight set by phase, not fixed** | Current price vs. daily mid AND vs. weekly mid (`equilibrium.md` §1 definitions: running-extreme midpoints), evaluated PER ASSET. The daily-vs-weekly split is NOT fixed 50/50 — see §2.1a for the phase rule that sets which one dominates a given call. See §6 for what it means when the two assets agree on one equilibrium (e.g. both above weekly mid) but disagree on the other (e.g. split daily-mid placement). **Partial code-derivation (plan 15 Task 6):** the model references the mid as a synthetic `daily_mid`/`weekly_mid` level and declares `direction: accept` (price accepted ABOVE the mid → bullish lean) / `reject` (sits BELOW → bearish lean); code derives the sign via `validate_contracts._mid_side` (a position read, no accept/reject-of-a-sweep). Conservatively scoped: reuses the P1 tier/tf multipliers, no new tables; the §2.1a dynamic daily/weekly weighting stays reasoning-only (its per-asset touch timestamps are not yet structured facts — §8 gap). |
| P4 | **Reclaim / failed reclaim of daily or weekly mid, HTF-confirmed** | A close-beyond-then-fails-back (or fails-to-reclaim) sequence on the daily OR weekly mid, confirmed specifically by an HTF (1h/4h) close — not any close. Daily and weekly weighted equally; 4hr scores more than 1hr (same scaling as P1). Note: `equilibrium.md` §3 and `next-move.md` mark plain weekly-mid TOUCH/CROSS as proven noise (GIL-39 B, +$83 shipped suppressing it as a trigger) — P4's weekly leg is a materially different, stricter signal (HTF-close-confirmed reclaim/failed-reclaim, not a bare touch/cross), so it is enabled alongside the daily leg without a separate gate. The daily half is parallel to the existing "failed reclaim of daily mid" item in `next-move.md` §2 (weight 2, "the strongest intraday tell"). **Partial code-derivation (plan 15 Task 6):** the model references the mid as a `daily_mid_high`/`daily_mid_low`/`weekly_mid_high`/`weekly_mid_low` synthetic level (the `_high`/`_low` encoding the reclaim direction) with `direction: accept` (reclaim held) / `reject` (failed reclaim); code derives the UP/DOWN sign via the same `_evidence_side` polarity as P1. Conservatively scoped — reuses the P1 tier/tf multipliers and the §3 maturity gate, no new multiplier tables. |
| P5 | **FVG fill (fair-value-gap zone visited)** | A completed-bar 1hr/4hr fair-value gap (`derive_facts.fvgs`) that the 1s tape has since re-entered ("visited" — `daily.py` convention). A visited zone is the fill event. Scored (plan 15 Task 4) via `validate_contracts._fvg_side`: the model copies the S6/S9 zone id verbatim (carrying the `bull`/`bear` kind) as the evidence `level` and declares `direction: accept` (the zone held its own bias) / `reject` (violated); code derives the sign from the bull/bear kind + accept/reject (bull↔high, bear↔low — the same mirrored polarity as P1). Reuses the P1/P2 tier/tf multipliers and the §3 maturity gate; magnitude is neutral (no per-tf clearance close for a zone fill). Motivating case (example #10): MES 1hr bull zone 7544.75–7558.0 (2026-07-14 00:00), visited — previously never appeared in the ledger at all. |

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
(MNQ's own family, MES's own family, separately). **This is a pure, static price comparison across
the FULL tracked depth** (prev1…prev7_day, prev1…prev3_week) — it does not depend on which level
got swept first; a level's fixed price is known for the whole session regardless of sweep timing.

**Code-enforced (`derive_facts._nested_prev_levels`, computed once per facts build into
`bundle.suppressed_p1_levels[asset]`, applied in `score_thesis_evidence` as a hard zero, exactly
like the §3 maturity gate), not merely a prompt instruction the model can miss.** The S9 close-status
block skips a nested level's line entirely (not offered as fresh P1 evidence at all) UNLESS that
level is also a live SMT candidate, in which case it renders tagged `[nested/duplicate ... P2-
candidate context only]` — visible for P2 judgment, structurally unusable as a P1 item.
`derive_facts._dol_menu` also excludes `bundle.suppressed_p1_levels["MNQ"]` from both the UP and
DOWN draw lists — a nested level cannot become the thesis's DOL either, closing the gap where a
level unusable as P1 evidence could still be the #1 (nearest) DOL menu entry and get selected
(the 2026-07-13 18:00 ET case in §10).

**Worked example (2026-07-14 01:00 ET, MNQ day lows):** `prev1_day_low`=29386.5, `prev2`=29677.5,
`prev3`=29395.0, `prev4`=28910.25, `prev5`=29209.75, `prev6`=29683.25, `prev7`=29522.5. Scanning
from most-recent: `prev1` is always valid (nothing more recent to be nested under). `prev2`, `prev3`,
`prev5`, `prev6`, `prev7` are each nested — a more-recent level (`prev1` or `prev4`) already sits at
least as deep. `prev4` is the ONE exception among the deeper levels: no more-recent level (`prev1`,
`prev2`, `prev3`) reaches as low as 28910.25, so it remains a valid, independent pool. The eligible
set for fresh P1/P2 lookup at this boundary is exactly `{prev1_day_low, prev4_day_low}` — not a
fixed "prev1/prev2 only" cutoff, but whichever subset survives the price comparison at each boundary.

**Worked example:** `prev1_day_high` (yesterday's high) sits ABOVE `prev2_day_high` (the day
before). `prev1_day_high` is the relevant, farther-out high-side pool; `prev2_day_high` does NOT
extend beyond it, so **`prev2_day_high` is IGNORED** — it must not be scored as an independent P1
accept/reject event, must not count toward P2's meaningful-tier check on its own, and must not be
offered as a DOL. The identical rule applies to prev-day lows (the most recent low is relevant
unless an older low sits farther below it) and prev-week highs/lows.

**Same-asset scope only — a nested level still carries cross-asset (P2/SMT) evidence.** The
exclusion above applies ONLY to a single asset's OWN P1 accept/reject tallying and its own DOL
eligibility — it prevents one physical sweep from being double-counted across nested same-asset
named levels. It does NOT exclude a nested level from P2 / SMT-candidate evaluation, because a
cross-asset divergence at that level is evidence about the OTHER asset's behavior, not a second
echo of this asset's own sweep. Concretely, at the **2026-07-14 01:00 ET** boundary MNQ swept its
`prev3_day_low` (29395.0) while MES never came within 15.5 points of its own `prev3_day_low`
(7516.25) all session — a genuine, uncontested SMT. That SMT must NOT be dropped just because
MNQ's `prev3_day_low` is nested under (does not extend beyond) MNQ's own more-recent
`prev1_day_low` (29386.5): the nesting rule silences MNQ's redundant same-asset P1 count at the
deeper low, but the MNQ-swept / MES-unswept divergence is independent evidence and stays a live P2
candidate. Depth-of-history levels (`prev3_day`…`prev7_day`, `prev2_week`/`prev3_week`) exist
precisely so these deeper cross-asset divergences are visible; §2.1b prunes same-asset P1 stacking,
it never prunes cross-asset candidacy.

**Deliberate simplification — SMT-candidate scanning is NOT itself nesting-gated.** The intent is
"do not go hunting for brand-new divergences at nested liquidities" (a nested level's own accept/
reject reading is stale/redundant, so a fresh divergence there is low-value noise) — but implementing
a literal "was this level still the frontier at the moment the SMT fired" check would require
tracking, per level, WHEN it became superseded (the sweep timestamp of whatever deeper level nests
it), which is a second, harder-to-verify temporal condition. Instead, `bundle.smt_candidates` (S3's
cross-ticker matrix) scans the full tracked depth exactly as before — unrestricted by nesting — and
relies on P2's OWN pre-existing requirement (the lagger's HTF close must REJECT, not accept, thesis.md
§2.1) as the natural filter: a nested level whose divergence shows a plain ACCEPT (continuation, e.g.
`prev7_day_low` in the worked example above — MNQ swept it while MES never confirmed, but MNQ's own
close continued through rather than rejecting) does not qualify as meaningful P2 evidence regardless
of nesting, so it costs nothing to leave the scan unrestricted. This is why `prev3_day_low`'s SMT
survives (genuine reject shape) while `prev6_day_low` (a "both eventually swept, >15min apart" reading,
not even a wick-divergence candidate) and `prev7_day_low` (a real divergence tag, but MNQ ACCEPTED, not
rejected, so it fails P2's own gate) do not end up mattering — without a separate, explicit exclusion
list. If a future case surfaces a nested level with a genuine reject-shape divergence that should NOT
count (e.g. one already fully accounted for at a shallower, non-nested level), revisit this — it has
not been needed yet.

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

### 2.1c Stretch discount + regime flag

**Stretch** = the distance from current price to the OPPOSITE-SIDE (farther) own-day running
extreme (`day_high`/`day_low` — whichever price has moved AWAY from), normalized by `avg_range_1h`
(a v1-seed ATR — mean of the last 20 completed 1h true ranges, `high − low`; `derive_facts.
avg_range_1h`). Deliberately the farther extreme, not the nearer one: price sitting at/near a fresh
extreme it just made is exactly the "stretched" case this exists to flag, and that is when the
distance to the extreme it ran AWAY FROM is largest — the nearer extreme is ~0 distance at that
same moment and would (wrongly) read as "not stretched." It answers "how many average hours of
range has price traveled from the extreme it left behind?". A large value means price has run far
from the day's origin/structure. The raw distance and the normalized multiple are both rendered in
S9 (`STRETCH & DISTANCE`); the "stretched" flag trips at `> 3.0×` avg 1h range (v1 seed, pending
calibration).

Two soft effects, both SOFT (prompt-level, not new code gates — same status as §2.1a's
unverifiable-load-bearing veto):

- **P3 equilibrium discount.** A high-stretch reading DISCOUNTS the P3 equilibrium weight for this
  call, using §6's exact "cap the ceiling, don't override the net score" mechanic with a new
  trigger — tally P1–P4 normally, but cap the confidence ceiling. Price far from structure means
  the equilibrium read is less load-bearing than the momentum, so a P3-heavy confidence should not
  stand at full height.
- **Undigested-TREND veto.** Before declaring `regime: TREND`, check the S9 stretch line. An
  undigested high-stretch TREND declaration (price already stretched far, no pullback/digestion) is
  an unverifiable load-bearing input — drop one confidence tier (soft, matching the §2.1a
  precedent), NOT a hard rejection.

**Nearest-meaningful-level distance** (also rendered in S9): for each direction, the distance from
price to the nearest named DOL pool, normalized by `avg_range_1h`. When it is large (`> 3.0×`, v1
seed → "sparse structure"), few named levels sit nearby — happens after a big stretch OR early in a
session before structure has formed. Prefer a closer existing predicate (a smaller-`n` daily-mid
close, a nearer anti-pool) over a far default. This is guidance, not a gate.

**SMT exhaustion — tier-relative shelf life (plan 15 Task 5).** Stretch is not only a confidence
discount on the current read; it also ages out a STALE SMT so it stops counting as continuation
evidence. For each S9 SMT candidate, code computes `stretch_since_fire` = distance from the swept
(lagger) ticker's current price to its price when the SMT fired, normalized by that ticker's
`avg_range_1h`. Past a tier-relative shelf life — **session `> 2.0×`, day `> 4.0×`, week `> 8.0×`**
(each tier ~double the tier below; `derive_facts.SMT_SHELF_LIFE`, all v1 seeds pending calibration,
same status as the `> 3.0×` stretch flag above) — the candidate is tagged `suggested_exhausted` in
S9. This is a CODE SUGGESTION, model-overridable: the model may set `exhausted: true` on a P2
evidence item it agrees is played out, which zeroes that item's points in `score_thesis_evidence`
(the same "zeroed, not scored" mechanic as the §3 maturity gate — a pure code mechanic on a model
judgment, never a new sign). It stays LLM-judged, not hard-gated: the model may disagree with the
suggestion and score the item normally. Motivating case (example #10): a stale bearish SMT kept
counting as continuation evidence 5.6× past its origin, long after it had played out.

### 2.1d Duplicate-simultaneous-sweep collapsing

Distinct from §2.1b's same-family nesting: when TWO OR MORE named levels for the SAME asset — of
ANY tier, and possibly from DIFFERENT families (a session sub-block low vs. a day-tier low) — share
the IDENTICAL sweep timestamp and side, they are restatements of one physical price move, not
independent structural events. Two concrete shapes seen in practice:

- **Exact cross-family duplicate.** A prior day's own sub-session low (`ny_evening(prev1)_low`) can
  be EXACTLY the same price, swept at the EXACT same timestamp, as that day's own `prev1_day_low` —
  by construction, whichever sub-block contains the day's extreme has a sub-block low identical to
  the day low. Both are the SAME event under two labels.
- **Opening-gap cascade.** A session's opening bar can cross several old, already-superseded levels
  in the same instant (e.g. `prev6_day_low`, `prev2_day_low`, `asia(prev1)_low`, `london(prev1)_low`,
  `ny_morning(prev1)_low` all "swept 18:00:00" in the 2026-07-14 01:00 ET example) — one drop, not
  five-to-six independent bearish signals.

**Code-enforced** (`derive_facts._duplicate_sweep_losers`, folded into the same
`bundle.suppressed_p1_levels[asset]` set §2.1b uses): among levels sharing a (side, swept_at) key,
keep only the highest-tier-weighted representative (week > day > session; ties broken by the more
extreme price) and suppress the rest from fresh P1 evidence — same "S9 skips it unless it's also an
SMT-candidate site" rendering rule as §2.1b, and the same code backstop in `score_thesis_evidence`
(never applied to P2). Note §2.1b's day/week nesting rule already resolves most day-tier duplicates
on its own (the redundant day-tier levels are typically ALSO nested, e.g. `prev6_day_low` above is
superseded by `prev4_day_low` regardless); this rule's main remaining bite is on session-tier
sub-blocks and the day/session cross-family exact-duplicate case, which §2.1b's family-scoped
comparison does not reach.

### 2.1e Cross-family price-cluster confluence (audit-only)

When a currently-tracked, NAMED level (a `prevN_day`/`prev1_week`/session-tier pool, etc.) sits
within an ATR-relative tolerance (`0.25 × avg_range_1h`, v1 seed pending calibration) of an OLD,
UNTRACKED historical extreme — a day-high/low or week-high/low from the long-horizon S5b history
that is NOT among the currently-tracked prev-N levels — that is a real structural coincidence worth
seeing. It is surfaced as a soft S9 annotation only (`CROSS-FAMILY CONFLUENCE`), in the same
audit-only spirit as the 09:15–11:30 whipsaw boolean and the §2.2 session-maturity fact.

Explicitly:
- It is **NOT** a new scored criterion — it is not wired into `score_thesis_evidence`'s point math.
- It is **NOT** a new tracked-level family.
- It is **NOT** a change to §2.1b's nested/superseded-prior-liquidity pruning — that rule concerns
  levels WITHIN one tracked family and stays exactly as-is. §2.1e is orthogonal: a coincidence
  ACROSS families (a tracked level vs. an untracked old extreme).

The "old, untracked" filter skips the most-recent tracked rows when scanning (the last 2 daily rows
= prev1/prev2 day, the last 1 weekly row = prev1 week) so a level never trivially matches its own
tracked extreme — a v1-seed heuristic. Motivating case (found during manual testing): a
currently-swept session-tier level coincided almost exactly with an untracked day-high from several
weeks back — a coincidence that was previously invisible to the model.

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

**Session-maturity soft prior.** A thin or immature evidence ledger EARLY in a session is expected
data-scarcity, NOT market ambiguity. Manual testing during a design session found that ledger size
and retry/failsafe rate both track how much of the trading day has elapsed: early London /
NY-morning calls show 0-item ledgers and 1–2 retries, while NY-afternoon calls show 7–10 item
ledgers and 0 retries. That is the natural consequence of fewer HTF bars having closed, not
evidence that the market is contradictory. S9 renders `session_elapsed_frac`
(`derive_facts.session_elapsed_frac`) and the mature P1/P2-eligible item count
(`mature_evidence_count`) so this can inform confidence: a thin ledger at `session_elapsed_frac ≈
0.1` should temper confidence via data-scarcity, and must NOT be read as a §6-style contradiction.
This stays SOFT — a rendered fact + this guidance, NOT a new `ARI_*`/`XL_*` hard validator gate —
deliberately matching the treatment of the 09:15–11:30 ET whipsaw-window fact (a rendered boolean
in S0, not code-enforced). Escalating it to a hard gate is out of scope without evidence that
justifies it.

**Immature-item pending resolution (plan 15 Task 7).** An immature (`mature: False`) day/week
evidence item is informative even though it scores zero — the model already narrates when/how it
will resolve. That is now STRUCTURED: an evidence item may carry an optional `pending_resolution`
`{resolves_tf: 1h|4h, resolves_at, implied_direction_if_confirmed: UP|DOWN}`. `resolves_at` is
CODE-computed — the S9 `PENDING RESOLUTION` block renders the next 1h / 4h close timestamp after
`now` (pure arithmetic on known bar boundaries), which the model COPIES rather than computing a
clock time itself (consistent with the "code computes, model copies" bias). It is informational
only: `score_thesis_evidence` never reads it (zero points either way), and the validator's single
check is audit-only — a `resolves_at` not strictly after `now` is a warning, never a hard
rejection (`validate_contracts._validate_pending_resolution`).

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
- **Clearance-magnitude multiplier (P1/P2/P4).** Within these criteria, points scale further by
  HOW FAR the qualifying HTF bar closed past the level: `ratio = |close − level| / avg_range[tf]`
  (the same v1-seed ATR as §2.1c, per timeframe). v1-seed buckets (pending calibration): weak
  `< 0.5×` → ×0.75, normal `0.5–1.5×` → ×1.0, strong `> 1.5×` → ×1.25. A shallow poke past a level
  scores less than a decisive clearance. This is code-derived in `score_thesis_evidence`
  (`_magnitude_mult`, fed by `derive_facts.build_evidence_magnitude`), NEVER model-declared — the
  model judges which level and accept/reject; code computes how far. A missing/immature/absent
  magnitude (e.g. P2 with no per-tf close, or no `avg_range`) is neutral ×1.0.
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

**Reused mechanic — the §2.1c stretch cap.** The high-stretch P3 discount (§2.1c) reuses THIS
section's "tally normally, cap the confidence ceiling" mechanic with a different trigger (price
stretched far from structure, rather than a cross-asset P1/P3 disagreement). The net score is still
tallied and still decides direction; only the ceiling is capped. Same code path, new trigger.

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
  (`agent-optimizations.md` §6). **Closed (plan 13 + plan 14):** `derive_facts._MENU_PREDICATE_CFG`
  now generates 1h/4h daily-mid variants, a `weekly_mid` level-class, general swept-level and
  meaningful-SMT close-classes (plan 13), AND a session-anchored `next_subsession` recall class
  (plan 14, `clock_after` at the next boundary — §1). The escape hatch remains available for reads
  the generator still does not enumerate, but the common P1/P2/P4 + recall shapes are now on-menu.
- P3's weekly-mid leg needs the `weekly_mid` fact field added to `FactsBundle` (parallel to the
  existing `day_mid`) before it is machine-checkable. Per the sandwich principle
  (`agent-optimizations.md` §1), the model must not compute this itself; it is a facts layer
  prerequisite.
- §2.1a's daily/weekly phase weighting needs per-asset "latest daily-extreme touch" and "latest
  daily-mid touch/cross" as structured facts (currently rendered-text-only in S1/S4) — see §2.1a's
  own gap note.
- DOL selection is unchanged from the existing S8 `_dol_menu`/validator contract: nearest
  meaningful, in-facts, unswept, undepleted, correct-side, NOT nested/duplicate-suppressed
  (§2.1b/§2.1d) pool ≥ `DOL_MIN_DRAW_DISTANCE_PTS` (5.0) away — prefer the DOL menu's `D*` IDs
  directly.
- **No-liquidity rule (code-enforced, not soft).** A sufficiently deep, sustained trend can sweep
  every named pool on its own side within the tracked lookback (prevN_day/week — no deeper
  history is tracked), leaving that direction's S8 DOL menu `(none eligible)`. This is the SAME
  situation as price beyond the all-time high — no resistance exists above it either — not a cue
  to look further back in history for an older (e.g. prior-month) level. `score_thesis_evidence`
  takes an optional `dol_available: {"UP": bool, "DOWN": bool}` (from `facts.menus.dol`); when the
  net-score-implied `expected_bias` has no eligible DOL, it is downgraded to NEUTRAL and the
  confidence ceiling to LOW — a directional read with nothing to draw to is not a real actionable
  direction. This changes `expected_bias` itself (the same value `ARI_THESIS_BIAS` compares the
  declared bias against), so a model that correctly declares NEUTRAL here converges immediately,
  it is not a retry-inducing gate. `_TASK_THESIS` tells the model this proactively (check the
  DOL menu before declaring bias) so it converges without needing the correction spelled out on
  a retry; §10 has the worked example that motivated this (2026-07-17 ET, deep in a multi-day
  MNQ/MES decline).
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
- **Closed (plan 14):** the plan-13 menu/facts additions are now joined by session-anchored recall
  (`next_subsession` `R*` menu — §1), clearance-magnitude scaling on the P1/P2 ledger (§4,
  `build_evidence_magnitude` → `_magnitude_mult`), and the stretch / nearest-distance / session-
  maturity / cross-family-confluence S9 facts (§2.1c, §2.1e, §2.2).
- **Soft-gate design rationale.** Session-maturity (§2.2), stretch (§2.1c), and cross-family
  confluence (§2.1e) are deliberately SOFT — rendered S9 facts + this doc's guidance, NOT
  `ARI_*`/`XL_*` hard validator gates. This matches the existing 09:15–11:30 whipsaw-window fact
  (a rendered boolean, not code-enforced). The clearance-magnitude multiplier (§4) IS code-enforced
  because it is a pure how-far arithmetic on an already-mature, already-scored item — it changes the
  magnitude of a point that would score anyway, it does not gate whether an item counts. Escalating
  any of the three soft facts to a hard gate is out of scope pending evidence that justifies it.

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
                                           # (validate_contracts ARI_THESIS_BIAS) — EXCEPT when the
                                           # net-score-implied direction has no eligible DOL (§8
                                           # no-liquidity rule): expected_bias downgrades to NEUTRAL
                                           # (confidence LOW) in code before this check runs, not a
                                           # retry target. P3/P4 are NOT yet in this ledger —
                                           # reasoning-only still (§8 gap).
  reasoning: audit-only
```

**Code-derived inputs the scoring/confidence path now consumes (plan 14).** The evidence-item
schema is UNCHANGED — magnitude is code-derived, not a new declared field. The
confidence/scoring path additionally consumes: (a) a clearance-magnitude multiplier on each
mature P1/P2 item's points (`(asset, level, tf) → ratio`, §4), applied in
`score_thesis_evidence`; and (b) the stretch, nearest-distance, and session-maturity S9 facts as
reasoning inputs (not schema fields) that shape confidence per §2.1c / §2.2. The model still
declares only `criterion/asset/level/tier/tf/direction/mature` per item.

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

Additional modes surfaced by manual point-in-time testing during a design session (`manual-l1-thesis/`):
· thin early-session ledger misread as market ambiguity (0-item ledger at London/NY-morning read as
contradiction) → §2.2 session-maturity soft prior (`session_elapsed_frac`/`mature_evidence_count`) ·
high-stretch `TREND` declared without discounting distance from structure (price far from own-day
extreme, no digestion) → §2.1c stretch discount/veto (P3 ceiling cap + one-tier undigested-TREND
drop) · a shallow poke past a level scored equal to a decisive clearance → §4 clearance-magnitude
multiplier (weak ×0.75 / normal ×1.0 / strong ×1.25 on `|close − level| / avg_range[tf]`) · a far
default recall/predicate chosen when a closer one existed, because structure was sparse (post-stretch
or pre-formation) → §1 session-anchored recall + §2.1c nearest-distance guidance · a tracked level
coinciding with an old, untracked historical extreme staying invisible to the model → §2.1e
audit-only cross-family confluence annotation · a malformed level name (e.g. `asia_cur_high` for
the facts sheet's `asia(cur)_high`) passing the model's own free-text check and only being caught
after the fact by `SEM_LEVEL_NOT_IN_FACTS` → `evidence[].level`/`dol.level`/`level_swept`+
`level_depleted`'s `name` are now an ENUM of the facts' actual level names for THIS call
(`schemas.build_thesis_schema`), structurally impossible to violate under strict schema-constrained
decoding · a directional bias declared deep into a multi-day MNQ/MES decline (2026-07-17 ET) with
no eligible DOL left on that side — the model reused an already-swept level as its DOL
(`SEM_DOL_WRONG_SIDE`) or omitted one (`SYN_DIRECTIONAL_MISSING_DOL`), and the same evidence net
score kept re-arguing for the same unsupported direction on retry → the no-liquidity rule above
(this section's own DOL bullet), `expected_bias` downgraded to NEUTRAL in code rather than left as
an unwinnable retry loop between "bias must match net score" and "bias must have a valid DOL" ·
depth-of-history levels (`prev3_day`…`prev7_day`) initially scored as independent P1 evidence
without a nesting check, so a single opening-bar cascade (2026-07-14 01:00 ET: one drop through 6+
old day/session lows in the same instant) was tallied as 6+ independent bearish signals, and a
session sub-block low exactly duplicating that day's own day-low (`ny_evening(prev1)_low` ≡
`prev1_day_low`, same price AND timestamp) was double-counted under two labels → §2.1b's nesting
rule promoted from a documented-but-unenforced principle to a code backstop
(`suppressed_p1_levels`), plus the new §2.1d duplicate-simultaneous-sweep collapse for the
cross-family/session-tier cases §2.1b's family-scoped comparison does not reach.

**2026-07-13 18:00 ET — a nested level was still offered and selected as DOL.** §2.1b's own text
says a nested level is ineligible for "P1/P2/P4/**DOL**" alike, and `suppressed_p1_levels` was
wired into P1 scoring and S9 rendering, but `derive_facts._dol_menu` never received or checked
that set — `prev7_day_low` (29329.0), nested under `prev3_day_low` (28910.25, deeper, no
more-recent level reaches as low), still surfaced as the #1 (nearest) DOWN draw and was picked as
the thesis's DOL, while the genuinely eligible `prev3_day_low` sat unused at the back of the same
menu → `_dol_menu` now takes the caller's `suppressed_p1_levels["MNQ"]` and excludes those names
from both the UP and DOWN lists, same treatment as the existing swept/depleted/proximity guards.
