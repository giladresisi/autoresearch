# L2 Entry Mechanisms — 5m FVG Reversal & Continuation

Status: DESIGN (2026-07-25; ladder re-bind, close-based eligibility, and L3 artifact-autonomy
added 2026-07-27 after validation on the 07-14/16/17 session examples; thesis-anchored
continuation gap relevance and the 1m reversal fallback added 2026-08-07 after the 07-16
09:30-open study; opening settle window, ladder min-height exemption, and crossed-trigger
market execution added same day after the 07-15 09:30-open study; `fvg_1m_post_extreme`
mechanism and stop-out cooldown added 2026-08-08 after the 07-20/21/22 open studies; cooldown
made state-based, not fresh-precondition, after the 07-23/24 studies; pre-open entry
suspension, 5m distance invalidation, and the §6 episode/close-color entry logic with
early-runaway entry and SL cap added 2026-08-09 after the 08-03..08-06 studies; DOL-floor
entry veto (§2), §4 1m-fallback widening to 5m-unusable states, and the §7
`extreme_reject_close` mechanism added 2026-08-15 after the 08-07/08-10 studies and a
bar-level re-check of 07-16..08-10 (1m-resolution simulation — several fills are same-bar
fill/stop ambiguous; 1s replay required before trusting the package numbers); deeper-gap
takeover on re-entry (§8) and the 08-11..08-14 oracle-L1 forward test (§10) added same day
after the 08-14 study and the 07-17/07-21/07-23 backcheck; §10.3 unseen-date sweep
(`extreme_reject_close` base rates, SL-cap knob finding) and the DOL-proximity study on
the real S8 menu (0.5x draw floor insufficient; data-backed floor ~1.0x avg_1h) added
2026-08-16; §7 far-extreme fallback (post-09:30 extreme when the counter-thesis 24h day
extreme is >150 pts at arm) added 2026-08-19 after the 08-18 gap-down study and a
07-15..08-18 backcheck, with the §5 max-height 35→45 alternative recorded as a
NOT-adopted candidate in §11 — that candidate was ADOPTED 2026-08-22 together with an entry
buffer trim 7→3 and a 25-pt stop-loss cap, off a 1s replay calibrated to reproduce all ten
documented 5m bindings exactly (§2 stop-loss, §9 knobs, §11 grid); the §7 fallback was
RE-KEYED from arm-time DISTANCE (>150 pts) to the AGE of the counter-thesis 24h extreme
(>2h) on 2026-08-26 after the 08-25 study — 08-07 and 08-25 are adjacent in distance and
need opposite answers, and a 19-day re-keyed backcheck gives +655.37 vs +466.00 (§7, §9,
§11 named tests); §5's pre-arm-penetration ambiguity was RESOLVED 2026-08-26 in favour of the
FRESH-TICK reading (a 19-day §5-only 1s A/B: fresh +570.50 vs literal +410.50, and the
"max-distance guard alone suffices" hypothesis REFUTED — the guard delays the literal entry
into a chase rather than blocking it); §6 was SETTLED for implementation on 2026-08-26 — the un-thresholded
RR-to-DOL clause retired as provably inert, the USABLE-5m test given an exhaustive
four-part definition, four normative clauses added in §6.1 after an independent simulator
hit every ambiguity, and §6.2 flagging the §6 day records as ERA-BOUND. NOTE: the
rules were tuned on the 07-14..08-06 sample — every trading day in that sample nets positive,
several by sub-7-pt stop clearances; forward-test on unseen dates before trusting). Extends
`agent-optimizations.md` §7 (mechanism enums). Defines four entry mechanisms the L2 trade
plan may arm and the L3 executor exercises. Numeric values are starting points for regression
tuning; the rules are fixed.

---

## 1. Division of responsibility (locked)

- **L2 authorizes mechanism CLASSES, L3 does all concrete binding.** The L2 output arms a
  mechanism kind (with direction + `valid_while`); the L3 scripts deterministically detect legs
  and FVGs from bar data, select the eligible FVG, place/move/cancel the stop-entry order, and
  enforce every guard. Even the FVG *choice* is arithmetic and therefore belongs in code (the
  sandwich principle).
- Consequences: no `arm_when` schema extension is needed (two-phase arming is L3-internal); the
  §7 mechanism shape survives as roughly `{kind, direction, valid_while}` with near-empty
  params; new FVGs never require an L2 recall — L3 re-binds deterministically, keeping L2
  output few and stable (the Phase-3 churn lesson).
- **Mechanism artifacts never pass through the facts sheet.** The FVGs and legs these
  mechanisms use are not facts-sheet items, not S8 menu entries, and never appear in the L2
  prompt or output — L2 cannot name a specific FVG. L3 detects them continuously from bar data,
  including artifacts created *after* the L2 call, and at every moment selects the most
  relevant artifact for each armed mechanism, placing/moving the resting order accordingly.
  This is the template for every future mechanism: L2 arms a class; the scripts own noticing
  the relevant artifacts in the bars and acting on the most relevant one at any given moment.
- Per-mechanism invalidation criteria supplied by L2 — deferred to a later stage.

## 2. Common rules (all mechanisms)

- MNQ FVGs only; no MES/SMT counterpart required. 5m is the primary timeframe; the reversal
  mechanism has a 1m fallback (§4) for any 5m-unusable state, §6 defines a
  1m-based mechanism for when the 5m structure is unusable outright, and §7 is a bar-pattern
  mechanism (no FVG) for the first rejecting bar at a swept day extreme.
- Entry is always a **resting stop-entry order** placed **beyond the FVG's far end with a fixed
  buffer** in the trade direction. The buffer is the wick-deception guard; tightening/removing
  it is a later optimization. The order is placed only when the mechanism's precondition is
  met, never speculatively.
- **FVG eligibility (close-based):** any relevant FVG (relevance is defined per mechanism —
  see §3–§6) that price has not *closed* beyond, on the FVG's own timeframe (a close through
  the far end disqualifies) since its creation. Wick-traversal alone
  does NOT disqualify — a gap that was wicked through but repelled the close is a *proven*
  barrier (validated on the 07-16 09:30 open, where the decisive ladder target had been
  wick-crossed hours earlier). Fresh and partially-mitigated gaps both qualify. Wick-deception
  protection comes from the entry/stop buffers and the ladder re-bind below, not from
  tick-disqualification.
- **FVG timestamping — two DISTINCT instants; never conflate them (PINNED 2026-08-27):**
  - **IDENTITY** (the gap's name, its stable key, and how it is reported) = **the MIDDLE
    bar's timestamp**. A three-bar pattern on bars `T-1 / T / T+1` is "the FVG at `T`" — the
    bar the imbalance is visible across, and the bar a charting platform draws it against.
    Worked example (1m): bars 16:30 / 16:31 / 16:32 → the gap is **16:31**.
  - **EXISTENCE** (the earliest instant anything may act on it) = **the THIRD bar's
    COMPLETION** = `label(T+1) + timeframe`. The pattern cannot be known before that bar
    closes. Same example → the 16:31 gap exists from **16:33:00**.
  - Everything time-gated reads EXISTENCE, never identity: close-based eligibility and the
    close-through scan (both start at existence), §4's "creating bar at or after 09:30",
    §6's "1m FVGs created since that extreme", and every binding decision. A gap is invisible
    to the engine until its existence instant, and carries its identity timestamp forever
    after.
  - **Reading older prose in this document.** Text written before this split uses the THIRD
    bar's LABEL as the identity — e.g. "labelled 09:35 … only exists at 09:40:00" (§11
    erratum) and "labelled 04:05 / completes 04:10". Under the pinned convention those same
    gaps are identified **09:30** and **04:00**. Only the NAME moves: both EXISTENCE instants
    (09:40:00, 04:10) are unchanged, so every conclusion drawn from them — the 08-21 erratum
    and its "no §5 entry" verdict included — stands exactly as written.
- **Distance invalidation (5m gaps):** once price displaces beyond a 5m FVG in the
  anti-trade direction by more than ~60 pts, that gap is **permanently** dead for entry — it
  does not revive when price returns (unlike the max-distance guard, which is momentary). A
  real reversal builds gradually and leaves fresh structure; a violent return to distant
  stale gaps is the deception pattern — better to miss the trade than enter on it. This also
  flips §6's "no usable 5m" precondition on. Threshold must stay ≥ 60: the 07-23 ladder rally
  peaked 56.25 pts beyond its bound gap (+304 winner survives); the 07-21 / 08-05 judas
  excursions ran 68–90+ pts (invalidation correctly kills the fade-the-stale-gap entries, and
  on 08-03/08-06 kills reversal attempts that price had escaped by 120–140 pts).
- **Ladder re-bind (both mechanisms):** while a stop-entry rests unfilled, if price ticks into
  the next eligible same-direction 5m FVG farther along the adverse path, L3 moves the
  stop-entry and stop-loss beyond that FVG (same buffers; cancel-confirm-then-place; no-move
  zone still applies). Repeats if a further FVG is entered. The deeper a manipulation swing
  runs through stacked gaps, the better the entry and the safer the stop — this converted the
  07-16 09:30 judas swing from a double stop-out into a +217 DOL capture. Must execute within
  seconds on fast tape (the validated case had a ~5s window between ladder trigger and fill).
- Minimum FVG height filter (displacement-born, not drift noise) and maximum height cap
  (bounds worst-case risk and traversal give-up). The minimum applies to **initial binding
  only**: a gap serving as a ladder target / deepest-penetration binding is exempt — in that
  role the gap is not displacement evidence, it refines the price and stop of an entry the
  bound gap already justified. (Validated on the 07-15 open, where a 2.75-pt gap was the
  day's true rejection level and the safest stop anchor.)
- **Opening settle window:** from the L1 arm time (e.g. a 09:20 call) through 09:30:30 —
  covering both the pre-open drift and the opening auction burst — L3 neither places nor
  triggers entries for these mechanisms. (Pre-open entries produced two fills in fourteen
  studies, both whipsaw stop-outs within ~70 s, zero winners; suspending them cost no date
  anything.) Penetrations occurring *during* the
  window count for eligibility/close-through tracking only — they do NOT satisfy an entry
  precondition and do NOT arm crossed-trigger market execution. The precondition (retrace
  into the gap / trigger cross) must be satisfied fresh after the window ends. The opening
  auction burst whipsaws any sub-30 s trigger regardless of entry logic. (Validated 07-15:
  the fresh 09:31 re-entry gives a stop fill 2 pts better than a window-end market entry,
  same +205 ride; 07-16: unchanged — the decisive upper-gap entry occurs post-window at
  09:30:33; 07-17: attempt 1 becomes a −17.75 resting-stop loss instead of a −29.75
  window-end market-entry loss.)
- **Stop-out cooldown (all mechanisms):** after a stop-loss is hit, L3 neither places nor
  triggers entries for the plan until the 1m bar in which the stop was hit closes. State
  tracking continues, and at that close L3 **acts on the current state** — re-bind to
  whatever is now eligible, place the resting order if the trigger is uncrossed, or fire the
  crossed-trigger market execution if price is already beyond it. Unlike the settle window,
  NO fresh-precondition requirement: mid-session, post-stop state reflects a real
  displacement that just took the stop — demanding it repeat forfeits the move (07-24: the
  breakdown crossed the trigger 19 s after the stop; state-based re-entry at the bar close
  → market 28579 → DOL TP +146.5, where a fresh-requirement lockout watched the −280
  collapse flat). Verified no-change on every other studied stop-out (07-17, 07-21, 07-23:
  cooldown-end price uncrossed → resting order, identical fills). The churn guard against
  re-entering noise cycles is the mechanism's own entry definition (e.g. §6 strictly-inside),
  not the cooldown. Post-stop binding preference is subject to the deeper-gap takeover and
  blacklist (§8).
- **Crossed-trigger ⇒ market execution:** whenever L3 acts (settle-window end, ladder move,
  re-bind, initial placement) and the computed trigger price is already crossed in the trade
  direction, the entry executes as a market order with the same FVG-derived stop-loss — the
  resting stop-entry is the normal case, market the degenerate case of the same mechanism.
  No separate "market entry" mechanism exists.
- **Max-distance guard:** the trigger price must sit within a capped distance of current price
  (analogous to the S8 DOL proximity guard).
- **No standing L1 plan → the mechanisms stay dark (decided 2026-08-17).** Every mechanism
  assumes a DOL-bearing plan; when L1 resolves to no-liquidity NEUTRAL (or no plan is
  armed for any reason), L3 places nothing, triggers nothing, and tracks state only.
  Re-arming is L1's recall problem — the recall cadence fires at subsession boundaries and
  can arm a fresh plan when liquidity re-forms — NOT L2's: no fallback DOL, no self-armed
  thesis (the §10.2 adverse-day bounds rest entirely on the plan/DOL discipline).
  Known cost: 07-17 under the 1.0x floor (the +91.25 oracle day goes untraded); accepted —
  its only pool was consumed within 4 minutes of the open, exactly what §10.3 says such
  pools do. Quantified going forward by the §11 planless-day shadow ledger.
- **DOL-floor veto (all mechanisms, every entry decision) — INERT since plan 16
  (2026-09-13).** The rule below is preserved as the record of what was measured, but it no
  longer refuses anything: the 09:20 DOL stopped being the target when selection moved to the
  entry fill (`l2-target-selection.md`), so the room-remaining quantity this floor measured
  no longer exists. `DOL_FLOOR_PTS` is still computed and every firing is written as
  `would_have_vetoed`, so the counterfactual stays recoverable and the knob can be re-aimed
  at the T2 target. Every figure below that credits the floor with suppressing a chase
  (08-10, 07-17, §10.2's adverse-day bounds) describes the PRE-PLAN-16 engine.
  an entry is vetoed unless at
  least ~60 pts remain between the entry/trigger price and the plan's DOL (the nearest un-hit
  DOL when the thesis names several; all mechanisms assume a DOL-bearing plan). Applies to
  resting-order placement, crossed-trigger market execution, episode fires, and post-stop
  re-entries; re-evaluated on every re-bind (new trigger price), never between re-binds
  (trigger and DOL are both fixed). This is an ABSOLUTE floor, deliberately not an RR ratio —
  the 08-10 09:51/09:59 chase entries carried tight stops (RR ≈ 2.9) yet only ~45 pts of
  target: the target sat within one opening-noise whipsaw of the fill. It is measured from
  the TARGET, not from the adverse extreme: a distance-from-extreme veto implicitly assumes
  a fixed move size and forfeits the big-reversal days (08-03's winner entered 189 pts past
  the extreme with 123 pts still to go; 08-06 similar). Separation on 07-16..08-10: every
  winning entry had ≥ 65.75 pts remaining, every losing chase ≤ 45.5 — a thin band from few
  dates, so 60 is a starter knob (§9). Also suppresses the §4-1m 09:36 whipsaw on 08-10
  (40.5 pts remaining).
- **Single stop-entry policy:** when multiple mechanisms are armed, only one resting stop-entry
  exists at a time — the one whose trigger price is closest to current price. Other armed
  mechanisms may only fire via market/limit entries while it rests. First trigger wins.
- **Stop-loss (capped, 2026-08-22):** the **nearer** of (a) the opposite end of the bound FVG
  plus the stop buffer — the structural anchor — and (b) a fixed **25 pts from the entry
  price**. Structural risk is `gap height + entry buffer + stop buffer`, so with the 3-pt
  buffers the cap binds from a gap height of **19 pts** upward; below that the stop is purely
  structural and the cap is inert. Stays subject to the existing validator rule (stop must
  not satisfy any thesis `falsified_if`).
  - Rationale (calibrated 1s replay of all ten documented 5m bindings, 2026-08-22 — the
    replay reproduces every recorded P&L exactly; see §11): gap height is a poor proxy for
    how much adverse room an entry actually needs. Across the winners the room CONSUMED was
    23.75 / 22.00 / 5.50 / 11.25 pts on gaps of 26.75 / 23.75 / 19.75 / 12.75 — while the
    two over-height gaps (42.5 and 44 pts) consumed only **10.75 and 2.75**. The tallest
    gaps needed the LEAST room: a tall gap is violent-displacement evidence, and if price
    respects it at all it rejects from the edge. The structural stop is therefore
    systematically mispriced in the upper height band, and the cap corrects it without
    touching small-gap bindings.
  - **25, not 20.** A 20-pt cap scores marginally higher on the sample but preserves the
    08-07 winner by **0.25 pts** at the 3-pt entry buffer — inside the noise, one tick from
    turning +202 into −20. At 25 the same two winners clear by 5.25 and 7.00. A 20-pt cap is
    also outright destructive at the OLD 7-pt entry buffer (it kills both 08-07 and 08-13,
    −300 on the sample): the affordable cap is a FUNCTION of the entry buffer, not an
    independent knob — trimming the buffer improves the entry price and therefore shrinks
    the excursion measured from it.
  - No MINIMUM stop distance is imposed, deliberately: the 5-pt min-height filter already
    floors initial bindings at 11 pts of risk, and the ladder / deepest-penetration /
    takeover roles are min-height EXEMPT precisely because a small gap is the best stop
    anchor in that role (07-15's 2.75-pt gap, 08-14's 3.0-pt takeover gap).
  - Cap is measured from the FILL price, so it is fixed at entry and never re-computed;
    a ladder re-bind (§2) recomputes both trigger and stop, hence a fresh cap.

## 3. "Last trend" — deterministic leg segmentation

ZigZag-style segmentation on 5m bars:

- Walk the last ~4 hours of 5m bars tracking the current leg's extreme. The leg **ends** when
  price retraces from that extreme by more than `max(30 pts, 25% of the leg's range so far)`.
  Smaller pullbacks do not interrupt the leg.
- **Qualifying leg:** range ≥ 50 pts.
- **Last trend** = the most recent qualifying leg whose extreme formed ≤ 60 min ago. If two
  qualify in the window, take the larger range.
- **FVG belongs to a leg** if its middle bar's timestamp falls within the leg's time span and
  the gap's direction matches the leg's direction.
- The counter-trend "bump" (mechanism 5m-FVG-continuation) = the sub-threshold or
  thesis-contradicting leg after the trend leg.
- **Scope of leg recency:** the 60-min recency test identifies the *last trend* for the
  reversal mechanism (which leg is being reversed) only. It does NOT limit continuation gap
  relevance — see §5: a gap that has never been closed through is standing structure however
  old its parent leg's extreme is. (Validated on the 07-16 09:30 open: the winning bearish 5m
  gaps were created 06:45/07:10 by a leg whose extreme was ~100 min old at arm time; a
  leg-age veto would have forfeited a +217 DOL capture.)

## 4. Mechanism: `fvg_negation_reversal`

ICT basis: inversion FVG (IFVG) / change in state of delivery — an FVG from the prior trend is
a discount/premium array that should hold if the trend is alive; full traversal means the
algorithm stopped respecting its own inefficiency.

- **Precondition (L2):** HIGH/MEDIUM-confidence L1 thesis expecting a reversal of the last
  trend.
- **Binding (L3):** the most recently created eligible 5m FVG belonging to the trend leg being
  reversed (e.g. a bullish FVG from the uptrend when expecting down).
- **1m fallback (widened 2026-08-15):** applies whenever no USABLE 5m FVG exists for the
  reversal — either the leg printed none (fast spike/judas legs often complete inside one or
  two 5m bars), or every 5m candidate is distance-invalidated / beyond the max-distance guard
  (the same 5m-unusable state that arms §6). L3 binds the most recently created eligible
  counter-thesis 1m FVG instead, requiring only that the gap's creating (third) bar is at or
  after 09:30 ET — the pattern's earlier bars may be pre-open (load-bearing on 08-03, whose
  rescue gap builds on the 09:29 bar). Same buffers, height filters, guards, and lifecycle;
  close-through disqualification runs on 1m closes; the §2 5m distance invalidation does NOT
  extend to 1m-bound gaps (close-through eligibility only, for now). A resting 1m negation
  stop MAY coexist with an armed §6 — single-stop-entry policy and first-trigger-wins govern,
  and the 3-attempt counter is SHARED per plan across all mechanisms. (Original case
  validated on the 07-16 09:30 judas spike: no 5m gap existed; the leg's 1m gap gave short
  29464.5 / SL 29481.5 → +159 to DOL. Widening 1s-verified 2026-08-16: 08-06 — negation
  long fills 29309.5 at 09:34:40, SL never touched → DOL +266.5 clean on one attempt,
  preempting §6's later +145.75; 08-03 — fill 09:34:39, stopped −22 at 09:34:49, resting
  refill 09:35:08 → TP 09:59:56 +222 → **+200 on 2 attempts** (vs +123.25 §6-only); 08-05 —
  fill 09:50:11, stopped −20 at 09:50:24, and the same-gap refill is BARRED because the
  09:50 1m close (30013) closed through the gap bottom (§2 eligibility) — one attempt only,
  leaving the budget for §6's +237.5 winner (a naive same-gap refill burns the third attempt
  −20 and locks the day out at −55); inert on 07-21, where §6's 09:39:11 entry precedes the
  09:40 trigger cross; its one bad 08-10 fill is killed by the §2 DOL-floor veto.)
- **Order:** stop-entry in the reversal direction, `entry buffer` beyond the FVG's far end
  (e.g. short 7 pts below a bullish FVG's lower bound). Full traversal of the gap = the
  expected move has started.
- **Stop-loss:** opposite end of the FVG + stop buffer (e.g. bullish-FVG top + 3 pts for a
  short).
- **Deferred, deliberately:** the IFVG-retrace entry variant (limit on retrace into the
  inverted gap); deeper-FVG fallbacks beyond the most-recent one; the 1m-close-confirm trigger
  alternative (1–2 closes beyond) — shelved as the A/B counterpart to the buffer approach.
- Residual risk accepted: no retrace requirement → some chase-into-exhaustion risk; a violent
  stop-run deeper than the buffer still catches us (thesis confidence is the backstop).

## 5. Mechanism: `fvg_return_continuation`

ICT basis: return to imbalance — a trend-side FVG acts as a premium/discount array; price
returns to rebalance the inefficiency, then continues. Entering on *exit beyond* the gap (not a
limit fade inside it) converts the textbook anticipation entry into a confirmation entry.

- **Precondition (L2):** L1 thesis expecting continuation of a move (whether still in the move
  or after a short-term counter-trend liquidity-grab bump).
- **Gap relevance (thesis-anchored):** the "move to be continued" is the one named by the L1
  thesis (direction toward the DOL), not the §3 last-trend computation. Any 5m FVG whose
  direction matches the thesis qualifies regardless of how long ago its parent leg's extreme
  formed — relevance is bounded by close-based eligibility (§2) and the max-distance guard,
  never by leg age.
- **Binding (L3), two-phase:** find an eligible thesis-direction 5m FVG per the relevance rule
  above. The stop-entry is placed only **after price retraces into that FVG** — "into" = any tick
  entering the gap's range. **SETTLED 2026-08-26: the retrace-in tick must be FRESH, i.e. strictly
  after the settle window ends (09:30:30).** Penetrations completed before the window ends —
  whether pre-arm or in-window — count for eligibility and close-through tracking ONLY; they do
  not satisfy this precondition and do not arm §2's crossed-trigger market execution. (This
  supersedes the earlier "or immediately if price is already inside / has already entered it"
  clause, which is DELETED; see the §11 resolution.) Order sits `entry buffer` beyond the FVG's
  far end in the continuation direction (e.g. short below a bearish FVG's lower bound during a
  downtrend). Full negation of the gap is accepted as the cost of certainty.
- **Double-FVG variant:** if the counter-trend bump created its own opposite-direction 5m
  FVGs, place the single stop-entry beyond the **farther** of the two far ends (trend FVG vs.
  bump FVG — e.g. the lower of the two lower bounds for a short) — full change-of-delivery
  confirmation on the retrace leg — provided both sit within the max-distance guard.
- **Stop-loss:** opposite end of the bound FVG + stop buffer.
- **Self-falsification:** price closing through the FVG in the anti-trend direction (inversion)
  kills the setup; the order is pulled and never fires. (Consistent with the close-based
  eligibility rule in §2 — wicks through the gap do not pull the order, closes do.)
- Forward-tested on the 08-07 judas-against-thesis open (outside the tuning sample, no rule
  change needed): the 09:33 wick into the 09:00 bearish gap [29805, 29831.75] placed the stop
  at 29798 (laddered up from the 09:10 gap per §2), filled 09:34; the gap-anchored stop
  29834.75 survived the 09:45 poke to 29821.75 by 13 pts and the trade ran ~230 pts of
  favorable excursion. The 09:30 5m bar closed 29805.75 — INSIDE the gap, 26 pts short of
  inversion; a marginally stronger judas closing through would have killed the setup
  permanently (accepted §2 behavior, revisit only on a studied counter-example). The
  counterpart idea of also trading 1m continuation gaps while usable 5m structure exists was
  considered and DECLINED (churn risk, no motivating miss — 08-07 is fully handled by 5m).

## 6. Mechanism: `fvg_1m_post_extreme`

Entry off 1m continuation FVGs when the 5m structure is unusable at the moment the thesis
becomes actionable — typically right after an opening judas that prints a new day extreme
against the thesis. 1m gaps are transient, so this mechanism trades their *rejection*, not a
resting order beyond them.

- **Preconditions (all required; L2 arms the class, L3 verifies continuously):**
  - L1 supplies direction AND a DOL (the RR test needs the target).
  - **No usable 5m FVG** — DEFINITION SETTLED 2026-08-26, and it is exhaustive: a
    thesis-direction 5m gap counts as USABLE at a moment iff ALL of (a) height in the
    [min, max] band, (b) NOT inverted — no 5m close through it in the anti-trade direction
    since creation, (c) NOT permanently distance-invalidated (§2, 60 pts anti-trade beyond
    the gap), and (d) its fixed trigger is within the max-distance guard of current price.
    If no gap is usable, the precondition holds. **The former "or offers bad risk:reward to
    the DOL at its fixed entry price" clause is DELETED** — see the RR-clause retirement
    note below. A usable gap disqualifies this mechanism entirely — so it never coexists
    with a resting 5m stop-entry, and no cross-mechanism cancel is ever needed. (A resting 1m
    negation stop from §4's widened fallback MAY coexist — the same 5m-unusable state arms
    both; single-stop-entry policy and first-trigger-wins apply.) If a usable 5m binding
    appears before this mechanism's trade triggers, this mechanism stands down.
    - **RR-clause retirement (2026-08-26).** The RR test was never implementable: §6 stated
      it only qualitatively, §9 never carried a threshold, and §11 listed it as an
      unimplemented gap. Solving for the threshold R* from the recorded days gives an
      unbounded-below interval, **0 ≤ R* < 3.57**: the only upper constraint comes from 5m
      gaps that DID bind (08-13's RR 3.57 is the tightest; 07-17 5.07, 08-07 7.92, 08-14 8.13,
      08-18 9.03, 07-31 9.19), and there is **no lower constraint at all**, because at every
      recorded §6 fire the count of usable 5m gaps was ZERO — the precondition was satisfied
      by inversion / distance-invalidation / max-distance, never by RR. Robustness scan over
      09:30–12:00: a usable 5m gap exists in 0/150 minutes on 07-21 and 1/150 on 08-05, so
      those two days are airtight regardless of fire timing; 08-06 (52/150, max RR 3.65) and
      08-10 (45/150, max RR 2.85) are timing-dependent — and the worst case cuts the same
      way, since 08-06 firing beside its usable gap would demand R* ≥ 3.65, contradicting
      R* < 3.57. Either the clause is inert or the record is inconsistent under ANY single
      threshold. **Resolution: R* = 0, clause deleted.** This reproduces every recorded day
      unchanged and removes the last under-specified rule from §6's preconditions.
  - A new day extreme printed against the thesis direction after the last 5m FVG's creation
    (the 5m structure is stale relative to where price now is).
  - Thesis-direction (continuation) 1m FVGs have been created since that extreme — they are
    necessarily closer to price than any 5m gap. Continuation-only: a thesis-direction gap is
    displacement evidence that the market is already moving the thesis's way; counter-thesis
    1m gaps do not qualify. An SMT in the thesis direction is corroborating context, never a
    requirement.
- **Episode model:** an *episode* begins when price enters a candidate gap — strictly inside,
  at least one tick beyond the near edge (edge touches don't count; no traversal requirement)
  — and it tracks the excursion extreme until a trade starts. Adverse 1m closes beyond the
  gap do NOT invert a candidate for this mechanism (unlike §2 close-based eligibility) — the
  close-color gates below replace close-through invalidation, and the episode survives
  escapes and re-entries (validated 08-05, where the winning entry came two bars after a
  close above the gap top).
- **Entry (short side; mirror for long).** All entries are by market:
  - *The entering bar* (the bar that enters the gap from outside) never triggers intra-bar —
    its verdict waits for its close: enter at the close only if it closed beyond the gap on
    the exit side AND against its own open (red). A with-trend-colored close = the move may
    not be ready — skip, episode continues.
  - *Early-runaway exception:* if, before the entering bar closes, price has already run
    ≥ 25 pts beyond the gap's exit-side edge AND is beyond the bar's open in the trade
    direction, enter by market immediately (don't donate the rest of the bar; 07-21: entry
    29149.75 instead of the 29139.50 close fill). The beyond-open condition is load-bearing —
    it blocked a false fire on 08-05 where the runaway price was still above the bar's open.
  - *Subsequent bars of a live episode:* exit-tick market entry, gated by the previous
    completed 1m bar — if it closed inside the gap or beyond it on the exit side, or closed
    against its open (red), the exit tick fires; if it closed with-trend-colored beyond the
    gap on the adverse side, defer to the current bar's close (enter there if it closed
    beyond the exit side — accepting the worse price as the cost of the missing conviction).
  - *A skip voids the cycle (strict reading, explicit):* entries fire only on an actual exit
    TICK (or close verdict) of a live cycle — never on already-crossed state carried over
    from a skipped one. When an entering bar's close verdict is a skip (wrong color), that
    cycle is consumed even if price now sits beyond the gap on the exit side; no market
    entry fires at the next bar's open, and the crossed-trigger rule (§2) does NOT apply
    within this mechanism. A fresh re-entry into the gap must occur and complete a new
    cycle. (This is how 07-21 traded — the 09:36 green skip required the 09:38→09:39
    re-entry before the winning exit-tick entry — and how 08-10 traded, where the 09:50 red
    skip was followed by the 09:51 re-entry cycle rather than an 09:51:00 market fill.)
- **Stop-loss:** the episode's excursion extreme **+ 2 pts**, capped at **30 pts** from the
  entry price (for market entries, entry price = mid of the current 1s bar at placement).
  Risk is proportionate to rejection depth, and the cap bounds deep-excursion episodes —
  accepted trade-off: a capped stop is no longer structure-anchored (08-06's capped stop
  survived by 6.75 pts).
- Among multiple candidate gaps, the first to complete its cycle fires (naturally the
  last-entered gap at each retrace extreme). Standard rules otherwise unchanged: min/max
  height filters (primary-binding role), single-stop-entry policy for any resting order,
  attempt counter per `plan_id`, stop-out cooldown (§2).
- Validation (final rule set): 07-21 (thesis down, DOL = TDO 29072.50; 5m structure
  distance-invalidated by the 09:30 judas): gap B early-runaway −30, gap A exit-tick entry
  29199 at 09:39:11 → TDO +126.50, day +96.50 vs −92.25 for the 5m-only alternative. 08-03
  +123.25, 08-05 +237.50, 08-06 +145.75 — each a single clean entry after the close-color
  gates skipped the noise cycles that a raw exit-tick rule would have taken. First
  out-of-sample forward test 08-10 (thesis up, DOL = TDO): −15.75, −15.75, then early-runaway
  long → TDO TP +65.75, day +34.25 — every rule (settle window, color gates, skip-voids-cycle,
  early-runaway, SL cap, DOL TP) exercised as designed. (Under the 2026-08-15 additions the
  08-10 09:51/09:59 chase entries are suppressed by the §2 DOL-floor veto — 45.5 pts
  remaining — and the day is owned by §7's 09:42 entry, +96.25; the §6 rules themselves stand
  unchanged, the veto and §7 simply sit in front of them. On 07-21/08-03/08-05 §6's validated
  entries all pass the veto.)

### 6.1 Deterministic reading — SETTLED 2026-08-26 (read this before implementing §6)

The §6 prose above admits more than one reading in four places, and a first independent
simulator hit every one of them. These are now normative; implement exactly this.

1. **Intra-bar ordering is load-bearing.** The episode begins at the TICK that first enters
   the gap strictly beyond the near edge. The early-runaway test and the episode's excursion
   extreme are evaluated ONLY over ticks at or after that tick — never over the whole
   entering bar. Named trap: 08-06's 09:38 bar prints 29437.00 (= gap top + 25) at 09:38:14,
   **thirteen seconds BEFORE price enters the gap at 09:38:27**; scanning the whole bar fires
   a phantom runaway on a print that preceded the episode.
2. **Early-runaway fills at the trigger price** (`exit-side edge ± 25`), not at the market
   mid. Named test: 07-21 gap B fills **29149.75** = 29174.75 − 25 exactly. Close-verdict and
   exit-tick entries DO fill at the 1s mid at placement — **snapped to the 0.25 tick AGAINST
   the trade** (up for a long, down for a short), added 2026-09-10. The mid of a 1s bar
   spanning an odd number of ticks is off-grid: 08-19's second entry booked **29672.875**, a
   price no broker gives, and every P&L quoted off a crossed trigger inherited it. The snap
   direction follows this section's standing rule that ambiguity resolves adversely, and it
   moves a fill by at most half a tick — an order of magnitude inside the ±2 pt market-fill
   tolerance §11 measures itself to, so no recorded row moves.
3. **The stop-out cooldown gates entry evaluation** — no cycle may complete while it is in
   force, and all gap cycles reset to idle across it (a fresh re-entry is required after).
   Without this the machine re-enters within seconds of every stop-out.
4. **Candidate-gap evaluation order:** most-recently-ENTERED gap first, per §6's own
   parenthetical ("naturally the last-entered gap at each retrace extreme"); the first gap to
   complete its cycle fires. Measured OUTCOME-NEUTRAL on all four recorded days (creation
   order gives byte-identical results), so this is a tie-break convention, not a result driver
   — specified only so two implementations agree.

**Known residual (±1 cycle, deliberately left open, do NOT silently "fix" it):** the
subsequent-bar gate is implemented DOC-LITERAL — previous completed bar closed inside the
gap, or beyond it on the exit side, or against its own open → the exit tick fires; closed
with-trend-coloured beyond the gap on the adverse side → defer to the current bar's close.
An alternative raw-exit-tick reading (no previous-bar gate) matches 08-06 better (+150.00 vs
the recorded +145.75, entry 29426.00 at 09:43:06 vs the recorded 29430.25) but is worse on
08-05 (+250.88 vs +235.38 doc-literal, recorded +237.50) and directly contradicts §6's own
statement that the colour gates "skipped the noise cycles that a raw exit-tick rule would
have taken". Doc-literal is therefore the specified reading; the discrepancy is a known
±1-cycle uncertainty on 08-06, not a defect to chase.

### 6.2 The §6 day records are ERA-BOUND — do not use them as verbatim regression targets

The four §6 numbers above were recorded under rule sets that no longer exist. Re-deriving
them requires era-matching, and two of the four CHANGE under current rules by design:

| Day | Recorded | Era of record | Under CURRENT rules |
|---|---|---|---|
| 08-05 | +237.50, one entry | max height 35, no DOL-floor veto | **+235.38, one entry** — reproduces (2.12 = fill convention) |
| 08-10 | +34.25, three entries | pre-veto | **no fire** — CORRECT, and §6 already says so: the 09:51/09:59 entries are veto-suppressed at 45.5 pts remaining and the day belongs to §7 |
| 08-06 | +145.75, one entry | **max height 35** | one entry, +125.12 — shape reproduces; at max height 45 the 36.75-pt gap [29375.25, 29412.00] becomes a candidate and adds a −26.12 cycle |
| 07-21 | +96.50, two entries | max height 35, pre-veto | +61.00, three entries — runaway leg exact (29149.75, −30.00); one extra close-verdict cycle at 09:38:00 unexplained by any stated rule |

An implementation should be checked against the CURRENT-rules column, not the recorded one.
The 08-06 height interaction is the sharpest lesson: **a knob change dated after a validation
silently invalidates that validation's entry set**, and §6 records carry no era stamp.
Remaining known gap: 07-21's extra 09:38:00 cycle — one cycle, −27.88, cause not identified;
treat a reproduction within one cycle of these figures as passing.

## 7. Mechanism: `extreme_reject_close`

ICT basis: liquidity sweep / turtle soup — a stop-run through a fresh day extreme that
immediately rejects (the extreme-making 1m bar closes back in the thesis direction) marks the
manipulation completing. This mechanism trades the FIRST reversing 1m bar in that specific
signature — an entry the FVG mechanisms often catch only later (or, as on 08-10, only via
chase entries the §2 DOL-floor veto now suppresses).

- **State machine (L2 arms the class; all state is L3, 1m bars, TICK-based extreme tracking;
  "day" = the 24h session opening at the prior 18:00 ET — an RTH-only reading would wrongly
  arm 08-07 off the 09:33 high while the 08:50 high 29867.25 stood above it):**
  - After 09:30 ET price moves against the thesis and prints a new day extreme. From the next
    1m bar, count consecutive 1m closes with no tick beyond the standing extreme; any
    new-extreme TICK restarts the count, even if that bar closes in the thesis direction
    (08-10: the 09:35 bar crossed the 09:34 low seconds in and restarted the count despite
    closing green).
  - Three consecutive quiet closes ⇒ **armed**.
  - While armed, the next 1m bar that ticks a new day extreme AND closes in the thesis
    direction fires a **market entry at that bar's close**. A new-extreme bar closing in the
    adverse direction does not fire and does not disarm — the graph may still want to extend;
    stay armed for the next new-extreme bar.
- **Stale-extreme fallback (re-keyed from DISTANCE to AGE 2026-08-26; originally the
  far-extreme fallback, 2026-08-19, from the 08-18 gap-down study):** when at plan arm the
  counter-thesis 24h day extreme is more than ~2h old (starter knob, §9), the machine tracks
  the **post-09:30 extreme** instead. The invariant: this gate identifies the liquidity pool
  whose sweep the mechanism is waiting for, and STALENESS decides which pool that is. An
  extreme set forty minutes ago IS the liquidity the market is currently working — the RTH
  move is still inside that structure and must take it out before the signature means
  anything; an extreme belonging to an earlier session phase is not, and the RTH leg prints
  its own sweep signature. Argue future counter-examples against that invariant rather than
  adding a fourth knob. After a large overnight displacement in the thesis direction the
  24h extreme is unreachable and §7 is otherwise dead weight on
  exactly the days that gap in the thesis direction — the sweep-and-reject signature then
  prints at the RTH extreme. Validated 08-18 (thesis DOWN, DOL prev1_week_low 29533.5, day
  high 30124.25 printed 20:51 the prior evening, 425.5 pts away): armed 09:34 after the
  09:31 high 29755; the 09:40 new-extreme bar closes green → correctly no fire; the 09:41
  bar ticks 29770 — the deception high, which also terminated 3.75 pts inside an over-height
  42.5-pt 5m bear gap no mechanism could bind — and closes red → market short 29760.25 at
  09:42:00 (1s mid 29761.1), cap15 SL 29770 survives the 09:42:24 push (29767.25) by 2.75
  pts (w3c30 29773 by 5.75) → TP 29533.5 at 11:01:26, **+226.75 on one attempt**, on a day
  the unmodified rule set leaves entirely untraded (the only eligible bear 5m gaps are 42.5
  and 1.75/3.25 pts — over/under the height filters — and no counter-thesis 24h extreme is
  ever approached, so §5/§6/§7 all stay dark).
  - **Why distance was replaced (2026-08-26, from the 08-25 study).** The 150-pt threshold
    was calibrated to sit above 08-07's arm distance, keeping the documented wrong-arm case
    (the 09:33 RTH high under the 08:50 high 29867.25) on the strict 24h rule. **08-25 breaks
    that split.** Thesis DOWN, DOL daily_mid 29218.38: the counter-thesis 24h high 29420.00
    sits only 123.50 pts away — inside the old "strict" cluster — yet the RTH rally tops at
    29416.00, **4.00 pts short** of it, so the strict rule never arms and the day's whole move
    is forfeited. 08-07 (152.50) and 08-25 (123.50) are ADJACENT in distance and need OPPOSITE
    answers, so no distance threshold separates them. Their AGES do, with room to spare:
    08-07's extreme was set 08:54, **0.60h** before the arm; 08-25's at 05:57, **3.55h**.
    Measured ages over 07-15..08-25 leave a wide gap with nothing in it — 0.02, 0.02, 0.07,
    0.42, 0.55, **0.60 … 1.90**, 2.43, 3.55, 6.42, 7.28, 8.00, 12.65, 12.65, 13.65, 14.50,
    14.92, 15.43, 15.50 — so T is not a fitted knob: totals are IDENTICAL for any T in
    1.0h–3.0h.
  - **Second defect the re-key fixes: the old threshold depended on an unpinned measurement
    instant.** 08-07's arm distance is 135.75 at a 09:20 arm but **152.50 at 09:30** — it
    straddles 150, so the same day is "strict" or "fallback" depending only on when you
    measure, and the documented wrong arm fires in the 09:30 reading. §7 said "arm-time
    distance" without pinning the instant while L1 arms at 09:20 and the §2 settle window ends
    09:30:30. Age moves by ten minutes across that span instead of by 17 pts against a 14-pt
    margin. **Implementations must still pin the instant explicitly: the plan's arm timestamp.**
  - **Re-keyed backcheck (2026-08-26, 19 days 07-15..08-25, MNQ 1m arming + 1s resolution,
    recorded theses/DOLs, plan scope enforced).** Totals — strict 24h **+50.75** · distance>150
    **+466.00** · 6h-session-block anchor **+35.75** · **age>2h +655.37**. Age is identical to
    distance>150 on 17 of the 19 days and better on exactly two: it keeps 08-07 strict (saving
    the −15 wrong arm) and catches 08-25 (+174.37 — market short 29392.75 at 09:50:00 off the
    09:49 new-extreme red bar, cap15 SL 29407.75 never approached, TP daily_mid 10:23:21).
    Delta **+189.37**, and it survives dropping 07-21 — the one day carrying a position that
    reaches neither SL nor DOL and is marked at the close — entirely: +436.87 vs +247.50, same
    delta. A **6h-session-block** anchor was also tested and REJECTED: it is worthless for §7
    (+35.75, below strict) because §7 needs a NEW extreme beyond the reference AFTER arming —
    on 08-25 it arms 09:53 off the 09:49 high and that high is never exceeded, so it never
    fires, while it picks up a −15 on 07-21. It changes §6, not §7. Caveat: this is a §7-ONLY
    backcheck, so nothing competes for the shared 3-attempt counter and fire counts run higher
    than the day records (07-21 2 vs 1, 07-31 3 vs 1); the same overstatement applies to all
    four policies, so the ranking holds but the totals are not day P&L. Harness validated
    against four independent records in this doc before use: 08-18 +226.75 exact, 08-05 −15.00
    exact, 07-31 28433.00 and 07-21 29116.25 price-exact (entry stamped one bar later by
    labelling convention), 08-10 +95.75 vs the recorded +96.25 (1m close vs 1s mid).
  - Original distance-era backcheck 07-15..08-18 (1m walk, known theses/DOLs, plan scope
    enforced — a fire cannot occur after the plan's DOL is touched, which silences
    07-17/07-23/08-17): inert on 07-15/07-16/07-23/07-24 (active, never fires in scope);
    fires on exactly three days — 08-18 (+226.75), and the two standing wrong-plan days,
    where it spends the plan's remaining shared attempt: 07-21 one fire 09:47 long 29116.25
    (−15 cap15 / −24 w3c30; consumes the §10.1 reserve attempt, day −42.25 → −57.25/−66.25)
    and 07-31 one fire 09:45 long 28433 replacing §6's cheaper 09:59 third attempt (−15
    cap15 / −30 w3c30, day −67.50 → −69.75/−83.75). Under the default cap15 SL the §10.2
    adverse-day band stays 0..−70; the w3c30 A/B must carry 07-31 −83.75 into its
    comparison. Net on the window ≈ +209.5 (cap15) / +186.5 (w3c30). Caveat: the post-09:30
    extreme is degenerate in the opening minutes (every burst tick is a new extreme) —
    harmless, the count just keeps restarting and the earliest possible fire remains ≥4 bars
    after the open — but the §10.2 lesson applies: extreme tracking must be tick-level on
    the fallback track too.
- **Stop-loss:** the entry bar's opposite-wick edge, CAPPED at 15 pts from the entry price
  (wick edge if nearer). The cap places the stop inside the swept zone — a plain retest of
  the sweep kills the trade (08-10's stop survived by 9.75 pts at 1s); a §6-style
  wick+3-capped-30 alternative was identical on the studied dates but WON the §10.3
  unseen sweep by ~+91 (it kept a deep-wick +316.5 winner the 15-cap stopped at −15) —
  the highest-priority §9 A/B, single-flip sensitive.
- **Scope:** lives and dies with the plan — **the DOL touch and the attempt budget**, which is
  what this document's own backchecks enforce ("plan scope enforced — a fire cannot occur
  after the plan's DOL is touched"). Falsification is deliberately NOT part of scope here:
  this document assumes L1's direction and DOL were right, so a falsifier has no work to do
  inside its frame. Unscoped it fires two
  losing shorts into the 07-21 11:14/11:28 new-day-high rally, hours after the plan
  completed. Shared per-plan 3-attempt counter, stop-out cooldown (§2), DOL-floor veto (§2)
  all apply; the settle window (§2) is moot in practice — the earliest possible fire is ≥4
  bars after the first post-open extreme. An SMT in the thesis direction is corroborating
  context, never a requirement (08-10 had an active bullish SMT from ~08:35).
- **Accepted misses (by design — the other mechanisms own them):** the reversal may come
  before three quiet closes (08-06: consecutive new lows 09:30–09:33, never armed before the
  bottom), without a further new extreme (08-03), or with the final extreme-making bar
  closing adverse. On 07-16/07-23/07-24/08-03/08-06/08-07 the mechanism correctly never
  fires in plan scope.
- Validation (1m simulation, 09:30–12:00): 08-10 (thesis up, DOL = TDO 29851.5): first
  post-open new day low 09:34 (29736.75, under the overnight 29771), tick-restart 09:35
  (29726.75), quiet 09:36–09:38 ⇒ armed 09:39; the 09:41 bar prints 29719 and closes green →
  market 29755.25 at 09:42:00 (1s-verified), SL 29740.25 (wick capped at 15), closest
  approach 9.75 pts → DOL TP 10:06:21 +96.25, vs +34.25 for the §6-only day. 08-05 (thesis down): fires 09:41:00
  short 30002.5 off the 09:40 red new-high bar; the 09:45 push to 30073.25 stops it −15;
  §6's +237.5 winner follows — accepted cost, bounded by the SL cap.

## 8. L3 binding & order lifecycle

- **Binding preference:** the most recently created eligible FVG. When a newer eligible FVG
  appears closer to price while a stop-entry rests, L3 re-binds: cancel the resting order,
  confirm the cancel, then place the new one (strict cancel-confirm-then-place — the live
  system has a known history of order moves leaving orphaned broker duplicates). The ladder
  re-bind (§2) is part of this same lifecycle: price penetrating the next eligible FVG along
  the adverse path is a re-bind trigger exactly like a newer closer FVG appearing.
- **No-move zone:** do NOT cancel/replace while current price is within ~15 pts of the resting
  trigger — re-binding there risks being flat during the exact displacement being waited for.
- **Disarm:** the order never outlives its justification. A reversal order is pulled when the
  bound leg ages out (extreme > 60 min old); a continuation order is NOT subject to leg age —
  it is pulled only when the bound FVG becomes ineligible (closed through) with no eligible
  replacement, or when the plan completes at its DOL. (The original wording here read "or
  when the thesis itself is invalidated/exhausted". That is OUT OF FRAME for this document
  and was never operationalised in any study — see the §7 scope note. Exhaustion in
  particular is the DOL: every recorded thesis sets `exhausted_if` to exactly the DOL price.)
- **Attempt counter:** after a stop-out with the setup still valid, L3 re-arms the same plan
  and re-binds to whatever FVG is *now* eligible (not necessarily the one that just failed).
  The 3-attempt counter remains per `plan_id` regardless of which FVG each attempt bound.
- **Deeper-gap takeover on re-entry (2026-08-15, from the 08-14 study; backchecked
  07-17/07-21/07-23):** when a **5m-bound** attempt (§4/§5) stops out and, between the
  stop-out and the next attempt (the stop-out bar itself included), price ticks into a
  deeper eligible thesis-appropriate FVG farther along the adverse path — any timeframe, 1m
  included — the re-entry binds that deeper gap and the failed gap is **blacklisted for the
  plan** (ignored even if price later returns to it; a stop-run through its edge is a
  falsified edge). The blacklist is strictly CONDITIONAL on the deeper-gap penetration: with
  no deeper gap penetrated, the same failed gap may re-bind as before — an unconditional
  blacklist would break 07-23's validated same-gap re-entry. Neither the min-height filter
  nor §4's creating-bar ≥ 09:30 filter applies to the takeover gap (deepest-penetration
  binding role, same §2 exemption precedent as ladder targets — 08-14's takeover gap is
  3.0 pts tall, created 09:11).
  - **Re-entry mode on the takeover gap:** §6's episode machinery, not a resting stop-entry
    — strictly-inside cycles, color gates, exit-tick with the previous-bar gate,
    skip-voids-cycle, excursion-anchored SL (+2, capped 30) — with ONE modification: a
    close-verdict (defer) entry whose excursion-anchored SL distance exceeds the 30-pt cap
    is SKIPPED (cycle consumed; wait for a closer cycle). This parameter-free gate replaces
    an earlier fixed 15-pt entry-bar-length idea: on 08-14 it skips the long noise bars
    (24.5/31.5 pts from open) exactly as the length gate would, but on 07-17 the 51-pt
    breakdown bar enters via exit-tick — a fixed length gate forfeits that day's entire
    winner. Exit-tick/intra-bar entries are never length-gated (their SL distance is
    inherently small at the zone edge).
  - **Crossed-trigger precedence at cooldown end (2026-08-17, from the 07-24 backcheck):**
    when the takeover has fired and the cooldown ends, check the FAILED binding's trigger
    first. If price is already CROSSED beyond it in the trade direction — momentum resumed
    without us — §2's crossed-trigger market execution takes precedence and the takeover
    does NOT divert (07-24: the 09:35 stop-run penetrated two eligible deeper 1m gaps, but
    at the 09:36:00 cooldown end price sat below the trigger → market 28579 → the −280
    collapse capture +146.5, which the episode's SL-cap gate would otherwise have SKIPPED
    at 45.25 pts — recreating the exact lockout the §2 cooldown rule was built to kill).
    If the trigger is UNCROSSED — chop — the takeover/episode path governs (07-21: all
    three cooldown ends uncrossed; 07-31 likewise). Reconciles every validated day; on
    08-14 it slightly IMPROVES the day (below).
  - **Cooldown boundary (1s-verified):** with the precedence rule above, cooldown-end
    resolution order is: crossed trigger → market (same FVG-derived SL); else a completed
    close verdict of the stop-out bar fires per the episode rules; else resting placement.
    (08-14: at 09:32:00 the old trigger 30252.25 IS crossed → market short at the 09:32:00
    1s mid 30245.5, SL 30268 → −22.50 — replacing the close-verdict entry 30245/SL
    30270.75/−25.75 of the earlier reading.)
  - Validation (1s replay, 2026-08-16). 08-14: attempt-1 fill 30252.25 at 09:30:33 (the
    09:30:10 in-window penetration correctly voided; fresh retrace tick 09:30:30); the
    09:31 squeeze to 30268.75 takes the SL (30268) at 09:31:05 and the SAME tick is the
    takeover penetration of the deeper 09:11 1m gap [30266.5, 30269.5]; the collapse
    leaves the trigger CROSSED at 09:32:00 → per the precedence rule, market short at
    the 1s mid 30245.5 (SL 30268, the failed binding's), stopped 09:45:21 −22.50; then
    09:46 red close skipped by the SL-cap gate (extreme 30275.5, dist 36),
    09:48 wrong-color skip, 09:49 SL-cap skip (dist 43.5), 10:05/10:06 wrong-color skips,
    10:07 close-entry 30262 (SL 30282.75, dist 20.75) → DOL 30124.25 at 10:59:52 +137.75 —
    **day +99.50 on exactly 3 attempts** (without the takeover+gates, same-gap re-binds
    lock the plan out around −60 and miss the move; the pre-precedence close-verdict
    reading gave +96.25). 07-17: attempt-1 replays exactly as validated (fill
    28644.75 at 09:30:35, SL 28662.5 at 09:31:03, −17.75); takeover penetration 09:31:14;
    the 09:31 entering bar closes inside → cycle live → exit-tick entry 09:32:05 ≈28663.75,
    and the 09:32 push to 28685.5 prints in the first seconds BEFORE the entry, so the SL
    (28687.5) sits above the already-made extreme — never threatened → DOL (overnight low
    28554.75) at 09:34:29 +109 — **day +91.25 on 2 attempts**, entry 19 pts better than the
    validated same-gap re-entry. (Provenance, 2026-08-17: this validation is ORACLE-INPUT —
    under the plan-18 1.0x DOL floor the recorded L1 resolves 07-17 to no-liquidity
    NEUTRAL, so no live plan would have armed; see §10.1.) 07-21 unaffected (no 5m-bound
    attempts); 07-23 unaffected (no deeper-gap penetration in the stop window).
- **TEMPORARY live-rollout spine gates (2026-09-17, plan 38 D15 — user decision, not a
  study result).** Two gates on WHEN any mechanism may enter, in the same tier as the
  attempt budget above — they change no mechanism's own rule, its trigger, its stop or its
  target. Both are evaluated in BAR time, and both sit behind named Executor constants so
  removing one is a one-line change:
  - **No entry after a positive trade** (`NO_ENTRY_AFTER_POSITIVE`). Once a position of the
    plan closes at a profit, the plan takes no further entry that session. Already true by
    construction today — the only positive close is `take_profit`, and reaching the target
    kills the plan on the same bar (`target_reached`); a stop is never trailed, so a
    `stop_out` is always adverse — and written as an explicit guard so it stays true if
    either of those facts changes.
  - **No entry at or after 10:30:00 ET** (`ENTRY_CUTOFF_ET`). A 10:29:59 entry is allowed;
    from 10:30:00 no resting order is placed, a resting order still unfilled is withdrawn,
    and no market mechanism fires. A position ALREADY OPEN keeps being managed to its
    stop, its target or the window end. Registry check: every ENTRY in
    `agent/trader/named_cases.py` is before 10:30 (the 11:00 / 11:01 values there are
    exits), so no documented figure moves.
  - **Removal condition:** both gates are scaffolding for the FIRST live sessions, where
    the agent stack owns the dispatcher at one contract. They come out — each on its own
    evidence — once a live session has passed the post-session conformance run with every
    delta explained (plan 38 D12) and a measured A/B over the replay set shows what the
    gate costs. Until then neither is a tuning knob: do not move 10:30.
- **Window end 13:00:00 ET (2026-09-17, plan 38 D8).** The replay window already ends at
  13:00 (its last bar is 12:59:59); live has no such edge, because the bar loop runs the
  whole CME session. At the first bar with bar time >= 13:00:00 the Executor marks any open
  position at that bar's close (a MARK, recorded as such — the live side mirrors it as the
  window-end market close) and the plan dies with reason `window_end`. Unreachable in
  replay by construction, so every replayed stream is unchanged.
- **Audit:** every bind / re-bind / disarm decision is logged (JSONL, extends the existing
  audit conventions) for post-session analysis.

## 9. Starting values (regression tuning knobs)

| Parameter | Starter | Rationale |
|---|---|---|
| Entry buffer beyond FVG far end | **3 pts** (was 7 until 2026-08-22) | Trimmed after the §11 calibrated sweep: buffer reduction is monotonically positive across all ten documented bindings (+562.75 → +602.75 at structural stops; every day improves by exactly the trim, none flips win→loss) AND it is what makes a tight SL cap affordable. UNTESTED RISK: the sample contains only days that already filled at buffer 7, so it cannot show false triggers a smaller buffer creates — the open §11 no-entry-day scan. Buffer 0 scores higher still (+632.75) and is held back pending that scan |
| Stop-loss buffer beyond opposite end | 3 pts | Structural risk = gap height + 6 pts under the 3-pt entry buffer; highest-variance knob — cleared by only 1.0–7.5 pts across the original four validation examples, which is why the 25-pt cap (not 20) was chosen |
| Stop-loss cap (distance from fill) | 25 pts | §2; SL = nearer of structural and entry±25 → binds from gap height ≥ 19. Caps worst-case risk at 25/attempt (×3 ≈ 75 pts plan exposure, vs 162 structural at height 45). 20 rejected: preserves the 08-07 winner by 0.25 pts at buffer 3 and destroys it outright at buffer 7. No minimum counterpart — see §2 |
| Min FVG height | 5 pts | Filters drift-noise gaps carrying no displacement information — initial binding only; ladder targets exempt (§2) |
| Opening settle window | L1 arm time → 09:30:30 | §2; suspends placement/triggering through pre-open drift + RTH-open burst |
| 5m distance invalidation | 60 pts anti-trade beyond the gap | §2; permanent, unlike the momentary max-distance guard |
| Max FVG height | **45 pts** (was 35 until 2026-08-22) | Raised after the §11 band sweep. NOTE its ORIGINAL rationale ("caps worst-case risk at ~45 pts/attempt") is now **obsolete** — the 25-pt SL cap bounds risk at any height. Under a corrected, completion-timestamped replay the only binding the raise still buys is 08-18 (+229.75); 08-21's 44-pt gap is never re-entered after real creation. Retained as a *character* filter, not a risk one — see the unbounded ablation in §11 |
| Max distance, current price → trigger | 60 pts | Beyond that we donate too much of the multi-hour L1 move |
| No-move zone around resting trigger | 15 pts | See §8 |
| `fvg_1m_post_extreme` SL buffer beyond excursion extreme | 2 pts | §6; excursion-anchored, not gap-edge-anchored |
| `fvg_1m_post_extreme` SL cap | 30 pts from entry | §6; bounds deep-excursion episodes |
| `fvg_1m_post_extreme` early-runaway trigger | 25 pts beyond the exit edge | §6; plus beyond-bar-open condition |
| Stop-out cooldown | until the stop-out 1m bar closes | §2; acts on current state at the close (crossed trigger ⇒ market) |
| DOL-floor veto | 60 pts remaining, entry → DOL | §2; ABSOLUTE floor, not an RR ratio; winners ≥65.75 / losing chases ≤45.5 on studied dates — thin band, tune early |
| `extreme_reject_close` quiet count | 3 consecutive 1m closes | §7; tick-based restarts |
| `extreme_reject_close` SL | opposite-wick edge, capped 15 pts from entry | §7; A/B alternative wick+3 capped 30 — identical on studied dates but +91 better on the §10.3 sweep (deep-wick winners); top-priority A/B (note: cap15 keeps the far-extreme-fallback backcheck's adverse band at 0..−70; w3c30 carries 07-31 −83.75) |
| `extreme_reject_close` stale-extreme fallback threshold | **2h, AGE of the counter-thesis 24h day extreme at the plan arm** (was: 150 pts of arm-time DISTANCE, until 2026-08-26) | §7; switches the machine to the post-09:30 extreme. NOT a fitted knob — totals identical for any T in 1.0h–3.0h (measured ages gap from 0.60h to 1.90h with nothing between). Re-keyed off 08-25, which no distance threshold can separate from 08-07 (123.50 vs 152.50 pts, opposite correct answers; ages 3.55h vs 0.60h). Backcheck 07-15..08-25: age +655.37 vs distance>150 +466.00 vs strict +50.75 vs 6h-block +35.75. Pin the measurement instant to the plan arm — the old distance key straddled 150 between a 09:20 and a 09:30 reading of 08-07 |
| Takeover defer-entry gate | skip if excursion-SL distance > the 30-pt cap | §8; parameter-free (reuses the `fvg_1m_post_extreme` SL cap) |
| Leg reversal threshold | max(30 pts, 25% of leg range) | §3 |
| Min qualifying leg range | 50 pts | §3 ("significant") |
| Leg recency | 60 min | §3 |
| Leg segmentation lookback | ~4 hours | §3 |

## 10. Forward test — 08-11..08-14 (oracle L1)

Run 2026-08-15 at 1m resolution against the full updated rule set. Oracle inputs: L1 assumed
to deliver, at 09:20, the day's actual post-09:30 direction and the liquidity level the move
in fact reached (settle window applied through 09:30:30). Net **+235.50 over 4 days**
(2 wins, 1 accepted skip, 1 win-via-takeover; 08-14 figure is 1s-verified).

- **08-11 (down, DOL 29666 = overnight low): no entry — accepted skip.** Open drive with no
  retrace: no adverse day extreme ever printed (§6/§7 cannot arm), §5's correct binding
  [29835, 29855.25] was only ever entered at/before the L1 arm (settle window voids it) and
  price never retraced into it post-window; the one fresh 5m gap (09:40, 73 pts) fails the
  35-pt max height. The ~170-pt move is out of scope BY DECISION (2026-08-15): every
  mechanism is retrace/confirmation-based, and a no-retrace runaway stays untraded (a
  displacement-entry mechanism was considered and shelved).
- **08-12 (down, DOL 29842.75 = prev RTH high): +46.75.** §4 negation short 29933 (fill
  09:31) stopped −31 on the 09:48 squeeze; the bound gap was then close-through dead → the
  widened §4-1m fallback armed; its first candidate was vetoed by the DOL floor (57.25
  remaining), its second allowed (77.75) → short 29920.5 at 09:51, survived by 3 pts (max
  high 29955 vs SL 29958), DOL TP 10:31 +77.75. First forward instance of the veto
  discriminating between candidate bindings.
- **08-13 (up, DOL 30001.5 = prev RTH high): +89.25.** Textbook §5: fresh 09:31 retrace into
  the 09:10 bull gap [29881.5, 29905.25] (wicked below, never a 5m close through), buy stop
  29912.25 filled 09:33, DOL TP during 09:36.
- **08-14 (down, DOL 30124.25 = overnight low): +99.50** under the §8 deeper-gap takeover
  (1s-verified sequence in §8 — attempt-1's real stop was the 09:31 squeeze, not 09:45 as
  the 1m read suggested); roughly −60 with lockout without it. No new day extreme printed
  (09:48 high 30280.75 vs overnight 30287.25) — §6/§7 correctly silent.

1s-verified 2026-08-16: 08-11's no-entry is solid (price sat 35 pts below the binding gap
at 09:30:30 and never returned); 08-12 exact (fill 09:31:18, stop 09:48:54 −31, re-bind fill
09:51:35 → TP 10:31:14 +77.75, max adverse excursion 3.00 pts from the SL); 08-13 clean
(fresh re-entry 09:32:35, fill 09:33:32, TP 09:36:43). Remaining caveats: oracle L1
(direction and DOL assumed correct) and the DOL choice materially shapes the captured size
(08-13's real move ran ~265 pts past its mapped DOL).

### 10.1 Real-L1 replay (recorded 07-14..07-27 theses, 2026-08-16)

Recorded L1 outputs (manual-l1-thesis/rerun_finalfinal) compared against the oracle
assumptions on the studied dates:

- **Match (validated numbers carry over):** 07-15 (DOWN, DOL asia_low 29745.75 — the +205
  ride's exact target), 07-16 (DOWN, prev2_day_low 29303.25 — matches the +159 arithmetic),
  07-17 (DOWN, london_low 28554.75 — EXACTLY the §8 takeover replay's TP; the +91.25 result
  was real-input-validated end to end UNDER THE 0.5x-FLOOR ERA in which it was recorded —
  see the demotion note below), 07-24 (DOWN, london_low ≈ the +146.5 target).
- **07-17 demotion under the plan-18 floor raise (mini-diff confirmed 2026-08-16, clean
  first attempt):** with the 1.0x draw floor the DOWN menu empties and projection_down is
  doubly stretch-gated → the model adopts **no-liquidity NEUTRAL, dol=None** — no standing
  L1 arm at 09:20, so the +91.25 day is untradeable under the new floor. The §8 takeover
  validation STANDS as oracle-input evidence of mechanism correctness; only the
  "real-input" badge is era-bound. Policy for setup-without-plan days: DECIDED 2026-08-17
  (§2 — mechanisms stay dark; re-arming is L1 recall's job; foregone setups measured by
  the §11 planless-day shadow ledger).
- **07-21 diverges: recorded bias UP** (the studied §6 +96.5 used DOWN). Old floor: the
  DOL (prev2_day_high 29220, 50.75 pts away) was swept by the 09:30 judas pre-settle →
  plan fulfilled flat, cost 0. NEW floor (mini-diff confirmed: model adopts UP +
  prev3_day_high 29796.5, 6.48x FAR, never reached): the plan STANDS. 1s-verified walk
  (ERRATA 2026-08-17 — an earlier read reported −92.25 via a triple whipsaw, produced by
  scanning only 5m gaps for the §8 takeover; the rule includes 1m gaps): §5 binds the
  08:55 bull gap [29143.25, 29164] (laddered from the 09:30-created gap at 09:31:32),
  fill 09:31:40 @ 29171, stopped 09:33:58 → −30.75 (attempt 1, the one ungated loss);
  the stop tick ITSELF penetrates the deeper eligible 1m gap [29137.25, 29138.5]
  (mid-bar 08:45) → §8 takeover fires — and at the 09:34:00 cooldown end the old trigger
  is UNCROSSED (price 29139.5 vs 29171), so the episode path governs (§8 precedence
  rule). Episode: 09:33 entering bar red-above → void; 09:34 re-enters and closes
  green-above → close-entry 09:35:00 ≈ 29142.5 (SL 29131, excursion 29133 − 2, gate
  passes at 11.5 pts) → survives the 09:41 dip (29135.5) by 4.5 pts, +82 MFE, stopped
  09:44:46 → −11.5 (attempt 2). Attempt 3 is NEVER SPENT: every later cycle color-voids
  or is skipped by the SL-cap gate (structural SL 49.5–92.75 pts as the excursion low
  deepens to 29063), including the 10:18–10:24 rally re-entries. **Day −42.25 with one
  attempt in reserve** — a right-direction day bled on timing but bounded by the §8
  machinery; §6/§7 never arm (no counter-thesis day extreme) and the veto is non-binding
  (500+ pts remaining).
- **07-23 diverges on DOL:** direction matches (DOWN) but the recorded DOL (prev2_day_low
  28700) sat 15.75 pts away at arm and was swept in the opening minute → plan complete,
  flat; the studied +304 is forfeited. SYSTEMIC FINDING: a DOL within ~60–80 pts at arm
  time interacts with the settle window (sweep completes before entries are allowed) and
  with the DOL-floor veto (nearly every entry fails the 60-pt floor) to produce flat days.
  This is an L1/L2 DOL-selection issue, not a mechanism defect — candidate L2 rule: when
  the nearest DOL is within ~80 pts at arm, target the next-deeper pool instead.

### 10.2 Wrong-thesis stress (inverted oracle, 2026-08-16)

Each forward-test day rerun with the thesis inverted and a symmetric opposite-side DOL, to
bound the bleed when L1 is wrong (the live edge depends on it):

- 08-11 inv (UP, DOL overnight high 29887): near-gap triggers veto-blocked (27.5–36.5 pts
  remaining); one deeper §5 binding fills 29779.25 at 09:30:56 (1s) and stops −17; later
  §6-long episodes in the 10:45–11:30 decline are mostly color-gated → ≈ **−17..−50**.
- 08-12 inv (UP, DOL overnight high 29992.25): DOL swept by the 09:30 push pre-settle-end →
  **flat 0**.
- 08-13 inv (DOWN into the monster rally): §4 negation −33.75; then §7 fires twice — 10:14
  short 30206.25 (stopped −10.25) and **10:36 short 30262.25, five pts off the 30267 session
  top**, riding the real afternoon decline ≈ **breakeven to positive**.
- 08-14 inv (UP, DOL overnight high 30287.25, 45.75 away): every early entry fails the
  60-pt floor; no new day low until 10:59 keeps §6/§7 silent through the morning → ≈
  **0..−45**.
- Plus the REAL recorded-L1 instances (§10.1): 07-21 under the old floor — flat 0; 07-21
  under the plan-18 floor (standing UP plan, FAR DOL) — **−42.25 with an attempt in
  reserve** (errata 2026-08-17: an earlier −92.25 read omitted 1m gaps from the takeover
  scan; correctly applied, the §8 episode gates absorb the chop after attempt 1).
- **07-31 under the plan-18 DOL-floor raise — the deepest real instance (1s-verified
  2026-08-16; floor 1.0x applied as an in-process pre-computation, plan 18 Part A NOT yet
  committed — cite its hash here when it lands):** the recorded UP thesis no longer completes
  at the near pool (+12 min, a flat day under the old 0.5x floor); the D1 moves out to
  prev4_day_high 28763.75 (2.01x), which the day never reaches, so the plan stands through
  the fade and the mechanisms trade it wrong-way three times — §5 long 28536.25 (fill
  09:40:36, SL 28511.5 at 09:42:02) −24.75; the 09:42 stop-run penetrates a LADDER of
  deeper gaps and the §8 takeover binds the DEEPEST eligible one (errata 2026-08-17: the
  1m gap [28455.75, 28477] created 08:53, not the 5m [28496.75, 28508.5] — the 1m scan;
  two nearer 1m candidates were close-through dead, verified). Episode: 09:42 entering
  bar closes red-above → void; 09:43 closes INSIDE → cycle live; 09:44:00 exit-tick long
  ≈28474 (SL capped 30 → 28444), stopped 09:44:05 → −30 (attempt 2); the gap is never
  re-entered. §6 then arms on the break to new day lows — the prior-18:00 session low
  28304.25 is first taken out at **09:54:02** (both the 1m walk's "~10:03" and a first 1s
  pass's "10:02:45" mis-timed this; the 09:54 1m bar's 28294.75 low already undercuts it —
  a lesson for §11: day-extreme tracking must be tick-level) — and fires once: 09:59:02
  exit-tick long ≈28348.25 off the [28340.5, 28352.75] gap created 09:57 (SL 28335.5,
  stopped 10:00:25) −12.75 (attempt 3) → **day −67.50 on exactly 3 attempts** — the SAME
  total as the pre-errata read (the −30 is cap-invariant), with the 10:21 and 10:35
  cycles budget-blocked. The §2 DOL-floor veto was live but
  non-binding all day (227.5 / 415.75 / 540.5 pts remaining at the three entries), so this
  bleed is NOT a near-DOL chase the veto could have caught — it is the honest cost of a far
  DOL keeping a wrong plan alive, the mirror image of 07-23 (old floor: flat → +304
  forfeited; new floor here: flat → −67.50 bled). Resolution notes: §6 prices market fills
  at the mid of the 1s bar at placement (±1–2 pts vs a prior-1m-close proxy), and the 09:40
  bar's high printed 09:40:08, BEFORE the 09:40:23 retrace into the §5 gap, so only 1s
  sees the 09:40:36 trigger re-cross. Entry triggers and both structure-anchored stops are
  identical at every resolution once the arming time is corrected.

Conclusion (re-restated 2026-08-17 after the 07-21 errata): the theoretical ceiling on
adverse-day bleed remains structural — 3 × (bound-gap height + 10) — but NOTHING in the
sample realizes it: with the §8 takeover correctly applied (1m gaps included), the brakes
— (1) the DOL-floor veto on near-DOL chases, (2) early sweeps of wrong-direction DOLs
completing plans flat, (3) the §6/§7/§8 color/cycle/SL-cap gates — hold every observed
adverse day to **0..−70** (deepest: 07-31 −67.50; 07-21 corrected to −42.25 with an
attempt unspent). The one loss no gate touches is a §5 FIRST entry into chop (07-21
attempt 1, −30.75); the first-entry color-gate fix was backchecked and REJECTED — it
costs ~−60 across the displacement winners (07-16 −35, 07-15 −11, 08-07 −10, 08-14 −4)
against ~+31 saved. Sample: seven days; extend before treating as a distribution.

### 10.3 Unseen-date sweep — `extreme_reject_close` base rates (2026-08-16)

24 unseen post-rollover sessions (06-15..08-04 minus the studied set), BOTH thesis
directions run blind per day (48 arms; DOL = nearest of overnight/prev-RTH/TDO pools ≥60
pts away). §7 fired 31 times, up to 3/arm:

- **Blind base rate: 2/31 TP wins** — yet positive expectancy in both SL variants (total
  +287.8 at cap15, +378.8 at wick+3-cap-30), carried entirely by two textbook
  sweep-rejection reversals: 06-29 (short 29807.75 at 09:53 into a −533 collapse, +527)
  and 07-07 (long 29239 fired on the bar that printed the exact session low, +169.5). The
  distribution is ~93% small losses (−9..−15) + rare huge wins. The studied-days win rate
  (3/5) does NOT generalize blind — §7's live value depends on L1 direction quality; with
  both arms run blind, half the arms are wrong-thesis by construction.
- **Knob finding (SL cap):** the 15-pt cap killed a deep-wick winner that the
  wick+3-capped-30 alternative kept — 06-22 third fire: −15 vs **+316.5**. Sweep total
  prefers w3c30 by ~+91 despite its larger per-loss cost (~−8/loss × 29). Single-flip
  sensitive; the §9 A/B just became the highest-priority knob experiment.
- The DOL-floor veto was untestable here (the sweep's own DOL selection enforced ≥60 pts —
  every fire had ≥167 remaining); its evidence base remains §10/§10.2.
- **DOL-proximity data (for the §11 L2 rule):** nearest thesis-direction pool distance at
  09:20, bucketed, over all 48 arms — P(swept by 09:35) / P(swept by 12:00) / P(price then
  reaches the next-deeper pool by 16:00):
  `<40 pts: n=10, 0.90 / 0.90 / 1.00 · 40–80: n=8, 0.75 / 0.88 / 0.75 ·
  80–150: n=9, 0.44 / 0.67 / 0.80 · >150: n=21, 0.00 / 0.33 / 0.86`.
  Pools inside ~80 pts are consumed by the opening rotation itself with 75–90% probability
  and the move then usually extends to the next pool — both halves of the case for
  re-anchoring the DOL deeper, with the probability cliff sitting between the 40–80 and
  80–150 buckets (i.e. the data puts the threshold at ~80). Caveats: 3-level pool
  approximation (not the real S8 menu), 8–21 samples per bucket, and the next-deeper DOL
  is often NOT reached same-day (86%/80% reach rates are conditional on the sweep) — the
  rule needs a management answer for unreached deeper DOLs.

  **Real-S8-menu rerun (2026-08-16, full facts pipeline per 09:20 boundary, 43 days
  06-15..08-14, 89 arms, 84 named-pool D1s + 5 projections):** the cliff is SHARPER on the
  real menu. D1 distance vs P(swept by 09:35) / P(by 12:00) / P(reach D2 after sweep):
  `40–80 pts: n=6, 0.83 / 0.83 / 0.75 · 80–150: n=27, 0.15 / 0.52 / 0.79 ·
  >150: n=51, 0.02 / 0.31 / 0.53`; in ATR units:
  `0.5–0.7x: n=4, 0.75 / 0.75 / 1.00 · 0.7–1.0x: n=5, 0.40 / 0.60 / 0.67 ·
  1.0–1.5x: n=18, 0.11 / 0.50 / 0.78 · ≥2.5x: n=24, 0.00 / 0.17 / 0.25`.
  Implications: (1) the 2026-08-16 DOL-menu refit's 0.5x-ATR draw floor is NOT sufficient —
  pools it still offers at 0.5–0.7x are swept pre-09:35 75% of the time with 100% D2
  continuation (07-17's recorded D1, london_low at 0.512x, is exactly this case);
  (2) early-sweep probability only drops to ~10% at ≥1.0x ATR / ≥80–150 pts, so the
  data-backed floor for a D1 the plan can actually trade toward is **~1.0x avg_1h (≈80+
  pts)**, with 0.7–1.0x a 40%-sweep grey zone; (3) decisive-bucket samples are small
  (n=4–6) but the cliff replicates across both bucketing schemes and the independent
  §10.3 approximation. Nuance from 07-17: a near D1 is not always a flat day — if the
  mechanisms enter BEFORE the sweep completes, the sweep IS the take-profit (+91.25 there);
  the flat/forfeit failure mode (07-21/07-23) is specifically the sweep completing inside
  the settle window, which is what the 75–83% pre-09:35 numbers measure.

  **v2 extension + unreached-D2 counterfactual (2026-08-16, 73 days 05-04..08-14, 132
  named-pool arms; pre-rollover May/early-June segment flagged — back-month data):**
  P(swept pre-09:35) by ratio, full sample: `0.5–0.7x: n=5, 0.60 · 0.7–1.0x: n=13, 0.15 ·
  1.0–1.5x: n=29, 0.07`; post-rollover only: `0.75 / 0.40 / 0.11`; pre-rollover:
  `0.00 in 0.7–1.5x` (calmer regime and/or thin back-month tape — the segments disagree
  exactly in the 0.7–1.0x band). Both segments agree 0.5–0.7x is unusable and ≥1.0x is
  safe; the floor decision is therefore **≥0.7x minimum, 1.0x if the post-rollover regime
  is weighted** (recommended — it is the live regime). Counterfactual on the 26 swept
  arms with ratio < 1.5: **policy B (hold for D2) reaches D2 by 16:00 in 73% of cases and
  beats TP-at-near-D1 by median +72.6 / mean +69.4 pts** (12:00 horizon: 62%, median
  +69.2); B ends worse than A in 19% of arms with fat tails (−393.8 worst) — but the tail
  is an artifact of no-stop scoring: those are arms where price died just past D1 and
  reversed hundreds of points, i.e. cases the mechanism SL and thesis `falsified_if` cut
  at −15..−30 long before the horizon exit. Conclusion: re-anchor to D2 and manage by the
  existing stop/invalidation machinery — no special near-pool TP policy is needed.

  **Plan-18 hand-off — deterministic outcomes on the studied set (2026-08-16, floor 1.0x
  pre-computed in process; Part A not yet committed):** exactly four studied days change
  their D1. 07-31 → TP prev4_day_high 28763.75 (2.01x), never reached, turning a near-pool
  completion into a standing wrong-way plan (−67.50, §10.2). 08-05 → the UP arm deepens
  71.75 pts to prev3_week_high 30062.5 (1.71x), touched 09:45:08 (+25 min), while the studied
  DOWN thesis and its whole L2 record are byte-identical — its DOL was the oracle TDO and the
  DOWN menu holds nothing in the excluded band under either floor (D1 london_low 1.415x, D2
  1.665x); an independent floor-override run of the facts pipeline reproduces the hand-off
  prices exactly. 07-17 → the DOWN menu empties, no-liquidity NEUTRAL — mini-diff
  CONFIRMED 2026-08-16 (model adopts NEUTRAL/dol=None on a clean first attempt) — the
  §10.1 demotion is EXECUTED (real-input badge era-bound; §8's oracle validation stands).
  07-21 → the UP menu goes FAR-only (prev3_day_high 29796.5, 6.48x, never reached;
  projection_up refused by the day-stretch gate on a day that ran +158, flagged upstream
  as a gate counter-example) — mini-diff CONFIRMED (model adopts UP + the FAR pool), and
  the 1s L2 walk under that standing plan bleeds −42.25 with an attempt in reserve
  (§10.1/§10.2; errata 2026-08-17 — the initial −92.25 read omitted 1m gaps from the
  takeover scan). Plan-18 Part A is implemented in-tree (`DOL_MIN_DRAW_RATIO = 1.0`) but
  UNCOMMITTED as of 2026-08-17 — hash citations pending. Their full 61-boundary sweep
  adds 9 changed NON-09:20 boundaries, none flipping expected_bias; all reviewed for L2
  impact: none — every L2 record anchors at a 09:20 boundary, and the one flagged row
  (07-16 09:00 DOWN going FAR-only) is not the 07-16 09:20 boundary our +159/+217 records
  use (that one is unchanged). L2 re-verification of ALL FOUR changed 09:20 days is
  COMPLETE (plan 20).

## 11. Implementation gaps (design-complete, work remaining)

### 11.0 Scope split for the entry-mechanisms cycle (written 2026-08-26)

Everything in §§1–10 is RULE, and rules are now unambiguous — no `OPEN RULE AMBIGUITY` items
remain. Everything still open below is either a KNOB VALUE or needs data that does not exist
yet. §9 already states the governing principle: *"Numeric values are starting points for
regression tuning; the rules are fixed."* So the implementing cycle builds rules and reads
knobs from config; a later tuning cycle moves the knobs against the real engine.

**IN SCOPE — the implementing cycle must deliver these:**
- Every mechanism rule in §§2–8, with knobs read from config at §9's starting values.
- The pinned conventions, each of which silently changes behaviour if got wrong:
  **5m bars left-labelled / left-closed / 18:00-ET-session-anchored** (§11, 07-23 item);
  **FVG IDENTITY = the MIDDLE bar, FVG EXISTENCE = third-bar COMPLETION** — two
  distinct instants, never conflated (§2 convention, §11 erratum);
  **§5 retrace-in must be a FRESH tick after 09:30:30** (§5 / §11 resolution);
  **§6.1's four clauses** (intra-bar ordering, runaway fill price, cooldown gating, candidate
  order); **§7's stale-extreme anchor keyed on AGE measured at the plan arm** (§7 / §9).
- All named regression tests: §8's takeover set, §7's stale-extreme set, §11's §5 fresh-tick
  set (08-21 flat, 08-11 flat, 08-18 enters 09:40:58), and §6.2's CURRENT-rules column.
- The **planless-day shadow ledger's logging hook only** — cheap, and the data cannot be
  collected retroactively. Log the paper outcome; do not act on it.

**DO NOT IMPLEMENT — retired, replaced or unadopted (listed so they are not rebuilt from
stale prose elsewhere):** the §6 per-gap RR-to-DOL disqualification test (RETIRED, §6);
§5's "or immediately if price is already inside / has already entered it" clause (DELETED,
§5); §7's distance-keyed (>150 pts) fallback (REPLACED by the age key, §7); the
6h-session-block anchor (TESTED AND REJECTED for §7, §7); the §8 SL-cap skip gate extended to
§6-proper (IDEA ONLY, zero net evidence, below); the §5 re-entry churn guard (CANDIDATE, zero
motivating examples, below).

**DEFERRED to a follow-up tuning cycle — what each one needs before it can be settled:**

| Deferred item | What it needs | Why it can wait |
|---|---|---|
| §7 SL cap: cap15 vs w3c30 | The §10.3 24-session blind sweep RE-RUN under the age anchor, on the implementation's own engine | Knob value; the rule is identical either way, and both policies capture the same winners byte-identically on the studied set |
| Entry buffer 3 → 0 | A scan of gaps that were retraced into but NEVER filled at buffer 7 — which requires an engine that emits non-fills, i.e. it can only be measured after implementation | Knob value; buffer 3 is adopted and measured safe; buffer 0 stays unadopted until the false-trigger cost is known |
| Max-height ceiling (45 vs unbounded) | More over-height (>45 pt) gap instances; the current case rests on a single favourable day (08-11) | Knob value; the 25-pt SL cap already bounds risk at any height, so the ceiling is a character filter, not a risk one |
| Planless-day shadow ledger (the DECISION) | Forward sessions where L1 resolves NEUTRAL but a mechanism setup would have fired under an oracle plan | The logging hook is in scope now; the stay-dark policy it tests can only be judged once the ledger has entries |
| §6 SL-cap skip gate extension | A motivating example — currently P&L-neutral on the one day it was checked | Zero net evidence; implementing it now would be fitting to noise |
| §5 re-entry churn guard | Stop-outs where NO deeper gap was penetrated; the original motivation was an errata artifact | Zero motivating examples remain |
| DOL draw floor 0.5x → 1.0x | Not an L2 change at all — it lands in `derive_facts.DOL_MIN_DRAW_RATIO`, shared with the L1/thesis pipeline | Different owner; propose upstream rather than editing from L2 |
| Per-mechanism L2-supplied invalidation criteria | A design pass that has not been started | Explicitly deferred since the doc's first draft |

**Rule of thumb for the implementing cycle:** if a change would alter a NUMBER, it is deferred;
if it would alter BEHAVIOUR, it is in scope and specified above.

- 5m FVG detection + leg segmentation as L3 facts/data products (existing detection is 1hr/4hr
  in `daily.py` and 1m-based `detect_fvg` in `strategy_smt.py`; 5m does not exist yet).
- 5m bar construction/alignment for FVG detection — **convention pinned, see the 07-23 item
  below: left-labelled, left-closed, 18:00-ET-session-anchored.**
- New §7 mechanism enum entries (`fvg_negation_reversal`, `fvg_return_continuation`,
  `fvg_1m_post_extreme`, `extreme_reject_close`) +
  validator support.
- Ladder re-bind + close-based eligibility tracking in the L3 binding engine (per-FVG
  close-through state on the gap's own timeframe, adverse-path next-FVG detection,
  seconds-level order move, 1m-fallback binding for spike legs, opening settle window,
  stop-out cooldown, crossed-trigger market execution).
- `fvg_1m_post_extreme` support: day-extreme tracking relative to 5m-FVG creation times,
  the four-part USABLE-5m test (§6 — height band, not inverted, not distance-invalidated,
  trigger within the max-distance guard), strictly-inside entry detection, episode state
  machine (entering-bar close verdicts, close-color gates, early-runaway trigger,
  excursion-extreme tracking with SL cap), exit-tick market execution. **The per-gap
  RR-to-DOL disqualification test is RETIRED (2026-08-26) — do not implement it**; it was
  never threshold-specified and is provably inert on the recorded days (§6). Implement the
  normative clauses in §6.1 (intra-bar ordering, runaway fill price, cooldown gating,
  candidate ordering) and validate against §6.2's CURRENT-rules column, not the recorded
  day numbers.
- 5m distance-invalidation tracking (per-gap max anti-trade excursion, permanent kill flag).
- DOL-floor veto at every entry decision point (placement, re-bind, crossed-trigger
  execution, episode fire, post-stop re-entry); nearest-un-hit-DOL selection for
  multi-target theses.
- §4 widened 1m fallback: 5m-unusable state detection shared with §6, creating-bar ≥ 09:30
  filter, coexistence with §6 under the shared attempt counter.
- `extreme_reject_close` support: day-extreme tracking against the prior-18:00 session,
  tick-based quiet counter and armed-state machine, close-direction fire at 1m close,
  wick-capped SL, plan-scoped disarm.
- Deeper-gap takeover support (§8): post-stop deeper-penetration detection (stop-out bar
  included), per-plan gap blacklist, §6 episode machinery reused in re-entry mode, the
  defer-entry SL-cap gate.
- **07-23 5m bar-construction discrepancy — RESOLVED 2026-08-26. It is an ALIGNMENT
  difference, not a resolution one, and the house convention says the studied 07-23 entry
  never existed.** Measured both ways on 07-23:
  - **1m-resampled and 1s-resampled 5m series are BYTE-IDENTICAL** under the same alignment
    (09:40 bar = O 28797.50 / H 28883.75 / L 28796.50 / **C 28856.00** from either source),
    so source resolution is NOT the cause and no 1s rebuild is needed.
  - The whole discrepancy is label/close side. **Left-labelled, left-closed** (bar `T` spans
    `[T, T+5m)` and completes at `T+5m`) puts the 09:40–09:44 bar's close at **28856.00**,
    ABOVE the bound gap top 28827.5 → anti-trade inversion → the gap is DEAD from 09:45:00.
    **Right-labelled/right-closed** (grid 09:41–09:45) closes the same-labelled bar at
    **28813.50**, below the top → gap survives. That single choice is worth the whole trade.
  - **CONVENTION PINNED: left-labelled, left-closed, 18:00-ET-session-anchored**, matching the
    house 1h/4h convention already used by `derive_facts` (18:00/22:00/02:00/06:00/10:00/14:00
    labelled by their START). For 5m the session anchor coincides with the clock grid, so the
    only real decision is the label side — make it explicit in the resampler, do not inherit a
    library default.
  - **Consequence:** under the pinned convention the bound gap inverts at 09:45:00 and 07-23's
    studied fill — which occurred after it — is INVALID; the +304 was produced by a
    right-labelled series. No headline number moves, because §10.1 already forfeits 07-23's
    +304 for an independent reason (under the recorded L1 the DOL was swept in the opening
    minute, completing the plan flat). The §8 backcheck verdict is likewise unaffected — no
    deeper gap was penetrated either way. **This is why the convention must be fixed before
    implementation: it silently decides close-through eligibility for every 5m mechanism.**
- 1s-replay verification: DONE 2026-08-16 for all package simulations (08-03 +200,
  08-05 −20-one-attempt with §6's budget preserved, 08-06 +266.5 clean, 08-10 §7 +96.25,
  08-11 no-entry, 08-12 +46.75 exact, 08-13 +89.25 clean) and the §8 takeover sequences
  (07-17 +91.25, 08-14 +99.50 post-precedence); §4/§7/§8/§10 validation texts updated to
  1s numbers.
- L2 DOL-proximity rule — DECISION-READY (data in §10.3 v2): raise the DOL draw floor
  from 0.5x to ≥0.7x avg_1h minimum, 1.0x recommended on post-rollover weighting; the
  re-anchored plan holds for D2 under the existing stop/invalidation machinery (D2 reached
  73% by 16:00, median +72.6 vs TP-at-near; the counterfactual's fat negative tail is a
  no-stop scoring artifact). The change lands in derive_facts.py `DOL_MIN_DRAW_RATIO` —
  shared with the L1/thesis pipeline (2026-08-16 refit); propose to its owner rather than
  edit unilaterally. Note the 07-17 nuance: an entry that beats the sweep uses the sweep
  as its TP — the floor shapes DOL SELECTION, it must not veto already-armed plans.
- IDEA ONLY, currently ZERO net evidence, NOT adopted: extend the §8 SL-cap skip gate to
  §6-proper close-verdict entries. On 07-31 the 10:21 §6 entry carried an excursion-anchored
  SL 71.75 pts away, capped to 30, and was stopped 25 s later — the exact pattern the gate
  skips in §8 — but under the corrected ledger the gate is P&L-NEUTRAL on that day: skipping
  10:21 frees the third attempt for the 10:35 cycle (SL exactly 30.0, allowed), which also
  loses −30 → −67.50 either way; the gate only changes WHICH entry spends the last attempt.
  If ever revisited, backcheck against every §6-validated close-verdict entry (07-21, 08-03,
  08-05, 08-06, 08-10 — note 08-06's validated §6 entry USED the cap, so the gate would have
  skipped a winner there); §6 and §8 rule text stand unchanged.
- CANDIDATE (from 07-21, single-day but structural): a §5 re-entry churn guard. §5's
  post-stop re-entry is a bare resting stop at the same trigger, with no §6-style
  cycle/color gating between attempts. (Errata 2026-08-17: 07-21's original −92.25
  triple-whipsaw motivation was an artifact of the takeover 1m-scan omission — correctly
  applied, §8 absorbs the re-entries and the day is −42.25; the candidate's remaining
  scope is stop-outs where NO deeper gap was penetrated, which currently has zero
  motivating examples. A first-entry color-gate variant was backchecked and REJECTED:
  it costs ~−60 across the displacement winners — 07-16 −35, 07-15 −11, 08-07 −10,
  08-14 −4 — against ~+31 saved; §5 first entries stay tick-based.) Backcheck list for
  any future form: 07-15, 07-17-oracle, 08-12, 08-14; the 07-24 lesson (lockouts forfeit
  collapses) cuts against over-gating.
- **Takeover scanner: MUST enumerate gaps of ALL timeframes** (the §8 text says "any
  timeframe, 1m included" — yet two independent manual walks scanned only 5m and produced
  a wrong 07-21 ledger and a mis-bound 07-31 takeover). Named regression tests for the
  implementation: 07-21 (1m gap [29137.25, 29138.5] takes over at the 09:33:58 stop tick
  → episode entry 09:35:00, day −42.25, attempt 3 unspent); 07-31 (binds the DEEPEST
  eligible penetrated gap [28455.75, 28477], not the shallower 5m — exit-tick 09:44:00,
  same −67.50 total); 08-12 and 08-05 (deeper 1m candidates exist but are close-through
  DEAD → no takeover, validated records unchanged); 07-24 (two eligible deeper 1m gaps
  penetrated, but the crossed trigger at cooldown end takes precedence → market re-entry
  28579, +146.5 preserved); 08-14 (crossed trigger at 09:32:00 → market 30245.5, day
  +99.50).
- **§7 stale-extreme fallback — named regression tests (2026-08-19; re-keyed to AGE
  2026-08-26):** 08-18 (extreme 12.65h old — fallback ACTIVE; single fire, market short
  29760.25 at 09:42:00 → TP prev1_week_low 29533.5 at 11:01:26, +226.75 — the day's ONLY
  entry); **08-25 (extreme 3.55h old — fallback ACTIVE; market short 29392.75 at 09:50:00 off
  the 09:49 new-extreme red bar, cap15 SL 29407.75 never approached, TP daily_mid 29218.38 at
  10:23:21, +174.37 — the day's ONLY entry, and the case that retired the distance key)**;
  08-07 (extreme 0.60h old — must NOT activate; the strict 24h rule governs, no wrong arm,
  §5's validated +230 ride untouched — note this day sat at 135.75 pts at a 09:20 arm but
  152.50 at 09:30, i.e. it FAILED the old distance guard under the 09:30 reading); 08-17 /
  07-17 / 07-23 (fallback active but the plan completes at the DOL touch before any fire —
  zero fires in scope); 07-21 and 07-31 (one fire each spending the plan's remaining shared
  attempt — cap15 deltas −15 and −2.25); implementation must track the post-09:30 extreme
  tick-level, pin the age measurement to the plan-arm timestamp, and disarm with the plan's
  `valid_while`, exactly like the 24h track. A 6h-session-block anchor was tested and
  REJECTED for §7 (+35.75, below strict) — see §7.
- **ADOPTED 2026-08-22 — entry buffer 7→3, SL cap 25, max height 35→45 (calibrated sweep).**
  Supersedes the 2026-08-19 "candidate B" entry. Method: a 1s replay of the §5 lifecycle
  (crossing-IN retrace strictly after 09:30:30 — continuous presence inside the gap at the
  boundary does NOT count, which is what the validated 08-13 walk did with its fresh 09:32:35
  re-entry; resting stop fills AT its price when the tape reaches it) **calibrated against
  all ten documented 5m bindings, reproducing every recorded P&L exactly** (07-17 −17.75,
  07-21 −30.75, 07-31 −24.75, 08-07 +198, 08-12 −31, 08-13 +89.25, 08-14 −15.75, 08-17 +77,
  08-18 +225.75, 08-21 +92.75).
  **ERRATUM 2026-08-22 (same day): the 08-21 row above is LOOK-AHEAD CONTAMINATED and its
  +92.75 is not attainable.** That replay timestamped each FVG with its third bar's LABEL
  instead of the bar's COMPLETION, so a 5m gap became actionable up to 5 minutes before it
  could be known. 08-21's 44-pt gap [29373.25, 29417.25] is labelled 09:35 but only exists
  at **09:40:00**; the recorded fill at 09:36:04 is impossible, and between real creation
  and the 09:54:07 DOL touch price never ticks back into it (max high 29361.50 vs the
  29373.25 bottom) — so **08-21 has NO §5 entry under the strict reading**. The other nine
  rows are unaffected (their gaps predate the arm or complete well before the fill —
  08-18's is labelled 04:05 / completes 04:10 against an 09:41:02 fill), so the grid below
  and the buffer/cap conclusions stand; only the 08-21 line and any total containing it
  must be re-derived. **Every future replay MUST gate an FVG's ACTIONABILITY on third-bar completion.**
  (Its IDENTITY stays the middle bar — §2. What completion pins is the earliest
  instant the gap may be acted on, not its name; the gap called 09:35 here is the
  09:30 gap under the pinned convention, existing from the same 09:40:00.)
  Grid totals over those ten (08-21 row inflated by the erratum):

  | SL policy | buf 7 | buf 5 | buf 3 | buf 0 |
  |---|---|---|---|---|
  | structural | +562.75 | +582.75 | +602.75 | +632.75 |
  | cap 30 | +564.50 | +582.75 | +602.75 | +632.75 |
  | **cap 25** | +574.50 | +590.50 | **+606.50** | +632.75 |
  | cap 20 | +262.00 ✗ | +272.00 ✗ | +617.25 | +640.50 |

  Adopted cell: **buffer 3 / cap 25 = +606.50** (+43.75 over the 7/structural baseline),
  worst-case risk per attempt 25 instead of 54. The ✗ cells are where cap 20 kills the
  08-07 and 08-13 winners; it survives at buffer ≤3 only by 0.25 pts, hence 25.
  Two earlier readings this sweep CORRECTED, both worth remembering: (1) a replay that
  accepts a retrace tick AT 09:30:30 gives 08-13 a spurious 09:30:42 fill into the opening
  whipsaw (MAE 49.5 vs the true 22.0) and reports −33.75 instead of +89.25 — the settle
  window is inclusive of its final second; (2) filling a traded-through resting stop at the
  1s bar MID instead of at the stop price mis-scores 07-17 (−14.88 vs −17.75).
- **Max-height ablation — why the filter is retained (2026-08-22, completion-timestamped).**
  With the SL capped at 25 the height filter no longer bounds risk, so it was ablated at
  35 / 45 / unbounded over the 18 studied days (§5-only engine; absolute totals are NOT the
  validated day results — other mechanisms and the §8 takeover are excluded — but the
  columns share one engine so the DELTAS are exact): **+1495.88 / +1725.62 / +1799.62.**
  Removing the ceiling entirely changes exactly ONE day: **08-11**, from no-entry to a
  73-pt gap [29743.00, 29816.00] (created 09:40:00) giving market 29740.00 at 09:41:44,
  capped SL 29765.00, → DOL +74.00. That is a WIN, and it is the day §10 records as an
  "accepted skip" (open drive, no retrace). So the empirical case for ANY ceiling now rests
  on a single favourable instance, not on risk control.
  Retained at 45 anyway, for a reason the data cannot yet test: a 25-pt cap on a 73-pt gap
  places the stop **48 pts inside the zone**, where the gap's own internal oscillation —
  not a structural failure — decides the trade. 08-11 survived that; one sample is not
  evidence that it generally does. **Open experiment:** re-run the ablation once more
  over-height instances exist, and record whether capped stops inside tall gaps get taken
  by intra-zone noise. If they do not, drop the ceiling entirely and let the DOL floor and
  max-distance guard do the filtering (they already kill 4 of the 7 band gaps).
- **§5 pre-arm penetration — RESOLVED 2026-08-26: the FRESH-TICK reading wins, and the
  max-distance-guard hypothesis is REFUTED.** History: §2 voids penetrations occurring DURING
  the settle window, §5 separately honoured "or immediately if price is already inside / has
  already entered it", and neither covered a penetration completed before the arm. The 08-19
  note proposed that **the max-distance guard alone is the correct discriminator and no
  recency clause is needed** — that hypothesis has now been tested and is FALSE.
  - **Method (2026-08-26).** §5-only 1s A/B over 19 days 07-15..08-25 at the adopted knobs
    (buffer 3, SL cap 25, band 5–45, max-distance 60, DOL floor 60). For each day the
    most-recently-created eligible thesis-direction 5m gap was taken as the binding, and the
    two readings scored: LITERAL (any prior penetration satisfies the precondition; act on
    current state at the window end) vs FRESH (a tick entering the gap strictly after
    09:30:30). Crucially the max-distance guard is modelled as MOMENTARY, as §2 defines it —
    a blocked order waits and places as soon as the trigger comes back within 60 pts. Eight
    days diverge. **LITERAL +410.50 vs FRESH +570.50 — FRESH by +160.00.**
  - **Why the guard cannot carry it.** Modelled correctly, the guard does not PREVENT the
    literal entry, it DELAYS it into a worse one. On 07-24, 07-31 and 08-18 the trigger is out
    of range at the window end, price then runs further from the gap, and the order places at
    the guard boundary as a crossed-trigger market fill 55–60 pts past the trigger — hitting
    the 25-pt cap on all three (−25.00 each) and displacing the fresh-tick entries those days
    actually paid for (+166.00, −20.75, +229.75). That is the chase-into-momentum pathology the
    DOL-floor veto was built for, except the veto cannot see it: all three had ample room to
    the DOL. The 08-19 note read the guard as blocking 08-18 outright; it only blocks it at the
    instant of the window end.
  - **Cost of the decision, recorded honestly.** FRESH forfeits the two days LITERAL wins:
    08-21 +169.00 (the case that opened this item) and 08-11 +136.25 — 305.25 pts of realised
    upside given up, against 75.00 of chases avoided and 395.75 of displaced winners recovered.
    08-11 is the doc's own "accepted skip", so FRESH is what §10 already assumes.
  - **Harness validated on five independent points before the verdict** (all offsets are the
    2026-08-22 buffer 7→3 trim, i.e. exactly 4.00 pts): FRESH reproduces 07-17 −13.75 (recorded
    −17.75), 07-31 −20.75 (recorded −24.75), 08-18 +229.75 (recorded +225.75); FRESH reproduces
    08-11's recorded FLAT; and LITERAL reproduces this item's own recorded 08-21 figure, +169.00
    vs +174.00 (the 5-pt gap is the 09:30:30 vs 09:30:31 fill instant). 07-15 also matches §2's
    note that the fresh re-entry fills better than a window-end market entry for the same ride
    (+209.25 vs +205.25).
  - **Per-day evidence (the eight divergent days; §5-only, attempt 1, adopted knobs).** The
    binding gap, its trigger, and the trigger's distance from price at the window end:

    | Day | th | bound gap | trig | dist @09:30:30 | LITERAL | FRESH |
    |---|---|---|---|---|---|---|
    | 07-15 | DOWN | [29958.00, 29967.00] h9.00 | 29955.00 | 4.00 | 09:30:30 fill 29951.00 TP **+205.25** | 09:30:47 fill 29955.00 TP **+209.25** |
    | 07-17 | DOWN | [28651.75, 28659.50] h7.75 | 28648.75 | 16.75 | 09:30:30 fill 28632.00 STOP **−25.00** | 09:30:35 fill 28648.75 STOP **−13.75** |
    | 07-24 | DOWN | [28601.50, 28606.75] h5.25 | 28598.50 | 85.75 | 09:30:35 fill 28539.25 STOP **−25.00** | 09:35:46 fill 28598.50 TP **+166.00** |
    | 07-31 | UP | [28514.50, 28529.25] h14.75 | 28532.25 | 64.75 | 09:31:00 fill 28587.25 STOP **−25.00** | 09:40:23 fill 28532.25 STOP **−20.75** |
    | 08-11 | DOWN | [29835.00, 29855.25] h20.25 | 29832.00 | 29.75 | 09:30:30 fill 29802.25 TP **+136.25** | no fresh re-entry → **FLAT** |
    | 08-18 | DOWN | [29766.25, 29808.75] h42.50 | 29763.25 | 78.25 | 09:30:39 fill 29703.50 STOP **−25.00** | 09:40:58 fill 29763.25 TP **+229.75** |
    | 08-21 | DOWN | [29472.25, 29485.00] h12.75 | 29469.25 | 26.75 | 09:30:30 fill 29442.50 TP **+169.00** | no fresh re-entry → **FLAT** |
    | 08-24 | DOWN | [29270.75, 29281.75] h11.00 | 29267.75 | 114.00 | plan complete before the guard allows → 0.00 | no fresh re-entry → **FLAT** |
    | | | | | | **+410.50** | **+570.50** |

    Read the `dist @09:30:30` column against the outcome: every LITERAL loss is a day whose
    trigger was OUT of the 60-pt guard at the window end (85.75 / 64.75 / 78.25) — i.e. the
    guard fired, and the entry happened anyway, later and worse. Every LITERAL win is a day
    already INSIDE the guard (29.75 / 26.75). The guard is not a filter on this decision; it
    is only a delay.
  - **Not captured by this A/B (favours FRESH further):** it models attempt 1 only. On 08-25
    both readings enter, but LITERAL spends TWO attempts (−50.00) where FRESH spends one
    (−25.00) — the literal reading burns budget as well as price.
  - Named regression tests: **08-21 must be FLAT** (prior penetration only, no fresh re-entry);
    **08-11 must be FLAT** (the §10 accepted skip); **08-18 must enter 09:40:58 at 29763.25**,
    NOT at the window end; 07-17 and 07-31 must enter on their fresh ticks.
- **§7 SL-cap A/B — narrowed 2026-08-26, NOT yet settled; cap15 stays the default.** Re-run
  under the newly adopted age anchor, 19 days 07-15..08-25: **cap15 +655.37 vs w3c30 +353.87**
  — cap15 ahead by 301.50, the OPPOSITE of the §10.3 unseen sweep (which preferred w3c30 by
  ~+91 on the strength of one deep-wick +316.5 winner). Both policies capture the same three
  winners byte-identically (08-10 +95.75, 08-18 +226.75, 08-25 +174.37); the whole difference
  is loss size on the adverse days (07-31 −45.00 vs −89.00, 08-05 −15.00 vs −30.00) plus one
  path-dependent effect — on 07-21 cap15's tighter first stop leaves budget for a second entry
  that rides to +233.50, which w3c30 never takes. Excluding 07-21 entirely, cap15 still leads
  +436.87 vs +377.87. **Verdict: the studied set and the unseen sweep disagree, and the studied
  set is the tuned one, so this does NOT overturn §10.3.** Settling it requires re-running the
  §10.3 24-session blind sweep under the age anchor — deferrable, since this is a knob VALUE
  and the rule is unchanged either way.
- **OPEN — entry-buffer false-trigger scan (blocks buffer 3 → 0).** The adopted grid only
  contains days that ALREADY filled at buffer 7, so it cannot measure the buffer's actual
  job: suppressing entries on a 1–2 pt wick past the gap edge that immediately reverses.
  Scan the studied dates for gaps that were retraced into but never filled at buffer 7, and
  score what buffers 3 / 0 would have done. Until then buffer 0 (+632.75 structural,
  +640.50 at cap 20) stays unadopted despite topping the grid.
- **Planless-day shadow ledger (forward holdout instrumentation):** on any session where
  the 09:20 boundary resolves to NEUTRAL (no plan armed) but a mechanism setup would have
  fired under an oracle plan, log the paper outcome (mechanism, entry, SL, result). The §2
  stay-dark policy rests on one known instance (07-17); this ledger decides empirically
  whether the foregone-setup cost is material — and if it ever is, the fix is upstream
  (recall cadence / projection stretch-gates), not an L2-side plan source.
- Per-mechanism L2-supplied invalidation criteria — deferred.
- **CANDIDATE (2026-09-12) — pre-open exhaustion entry. NOT a rule; recorded with its
  evidence and its open questions, per `docs/entry-mechanism-change-protocol.md` step 0.**
  The case it targets: the day's manipulation completes BEFORE 09:30 and the run starts at
  the bell, so §2's settle window and §5's fresh-tick rule both decline the trade.
  **Motivating day 08-21** — the 09:25 5m bar sweeps to 29417.25, rallies **68.00 pts** to
  29485.25 (traversing the top of §5's bound gap [29472.25, 29485.00] by 0.25), and the
  09:29 1m bar closes 29469.75, back below the gap. Production vetoed its own trigger
  29469.25 at 09:32 (`distance 72.0 vs cap 60.0`) and died `dol_reached` 09:37:20; from
  that price the day ran 249.00 pts with 3.25 pts of adverse excursion.

  **Criteria as proposed** (direction is the L1 thesis's, evaluated in that direction only):
  (1) a counter-thesis swing inside 09:15–09:29; (2) a thesis-direction 1m continuation FVG
  fully filled by the swing extreme; (3) MNQ and MES disagreeing about how far back into
  their own 1m gap stack they filled — the "1m SMT fill"; (4) the **09:29 1m bar closing in
  the thesis direction**, the manipulation-is-finished confirmation; (5) entry either market
  at 09:30:00 (**E1**) or at the first second within the first minute at which the 1s close
  has held beyond the 09:29 far wick for 10 s (**E2**); (6) SL = 09:29 counter-side wick +
  3, capped tighter than §2's 25.

  **Corpus and funnel** (1s replay, MNQ, 2026-05-01..09-04, 95 sessions with a usable
  pre-open 5m grid). 5m-close-reject encoding: 69 sessions clear a 30-pt pre-open swing →
  12 traverse and close-reject an eligible 5m FVG → **6** survive the 09:29 close-direction
  filter → **5** survive the SMT veto. The 1m-SMT-fill encoding gives **31** fires (22
  `MNQ-deeper` / 9 `MNQ-shallower`).

  **Verdict: NEGATIVE on every null-compared measure. Do not implement as an entry.**
  - **MFE before stop (the stop is part of the entry, so this is the right metric), scored
    against a matched null — same entry instant, same stop construction, no setup filter.**
    E1 fires n=31 vs null n=160: at cap 8, median R **1.22 vs 0.00** but **R≥3 29% vs 27%**
    and p75 R **3.11 vs 3.18**; at cap 25, median R 0.77 vs 0.54 and **R≥3 19% vs 23%**.
    E2 fires n=17 vs null n=79 at cap 10: median R **0.80 vs 1.23**, R≥3 29% vs 23%. The
    setup lifts the MEDIAN (it avoids the opening-second stop-out an arbitrary 09:30 entry
    takes) and does **not** lift the tail, which is the part that pays.
  - **Realised P&L, best cell of a 3-entry × 7-policy × 6-cap sweep:** E2 / breakeven-at-1R
    / cap 12 = **+531.75 pts** on 17 taken of 31 — but **2 wins** (06-25 +436.25, 08-11
    +191.50), 7 breakeven scratches, 8 losses of −12.00. **Remove the top two days and it is
    −96.00.** 08-21 books **−12.00** in that cell. Both winners come from `mark` (hold to
    13:00), an exit production does not have.
  - **The one measure that separates is DIRECTION, not fill.** MFE as a fraction of the
    09:30–13:00 range: E2 fires median **0.699** vs null **0.500**, and ≥0.75 of the range
    on **47.1% vs 24.0%**. Consistent with the first-round symmetric-barrier reading (±50
    first-touch right on **10 of 12** early fires vs a 54% null). If anything here is worth
    keeping it is a **direction/confidence input to L1/L2**, not an entry mechanism.

  **Open rule-level questions — each would be answered differently by two implementations,
  so none of this can start at step 1 as written:**
  1. **Which array defines the SMT, and which polarity.** On 08-21 the 5m arrays say MES ran
     further past its gap (MNQ = laggard); the 1m stacks say MNQ filled back to a gap created
     three minutes earlier than MES did (MNQ = the one swept). Opposite verdicts, same
     instant. Neither polarity separates: best-of-both-entries R≥2 lands at 36% for
     `MNQ-deeper` (n=22) and 33% for `MNQ-shallower` (n=9), against 39% for the unfiltered
     set.
  2. **Which entry.** They are complementary, not rankable. Adverse excursion before the
     trade runs: 08-21 **3.25 (E1) vs 21.50 (E2)**; 06-25 41.25 vs **8.00**; 08-31 21.25 vs
     **6.25**; 07-10 24.00 vs **14.25**; 06-24 **0.00** vs 179.50. Six of eight days have a
     ≤15-pt-heat fill under one or the other — but **no single (entry, cap) keeps 08-21 and
     the aggregate**, because the tight cap the aggregate needs stops 08-21 at 09:30:15.
  3. **Does an opening spike through the 09:29 wick falsify "the manipulation is finished"?**
     Worth 33R on 06-25 alone: with an abort-on-close-beyond-the-wick clause the day is a
     no-trade at +1 s; without it the 10 s hold enters at +24 s and never stops.
  4. **The encoding is not pinned.** Two faithful readings of the same prose — manipulation
     measured on the 09:25 5m bar with a 5m close-reject, vs anywhere in 09:15–09:29 with
     the 09:29 1m close-reject — produce 6 days each and agree on only 4. Admitting 1m gaps
     alongside 5m takes the set from 6 to **36**.

  **DO NOT rebuild these from the prose above:**
  - *"The 09:30 bar's wick exceeds the 09:29 bar's wick"* as a gating rule. It separates 11
    of 12 early fires perfectly and is **tautological with the stop** — the stop is the 09:29
    wick + 3, so it restates "the stop was hit in the first minute". Diagnostic only.
  - *A 50-pt pre-open swing floor.* It is anti-selective here: raising 30 → 50 cuts the
    36-day set to 10 and the R≥3 count from 8/36 to **1/10**, removing 05-18, 06-30, 08-11
    and 06-25 while keeping mostly zeros. 08-21's 68-pt swing is characteristic of the
    LOSERS in this population, not the winners.
  - *Fixed R-multiple targets.* Uniformly destructive across the sweep — they cap the only
    trades that pay for the rest.

  **Two findings that stand independently of this candidate, and are the reason it is worth
  keeping the record:**
  - **Exit policy dominates entry choice.** At a fixed entry and cap the spread across exit
    policies is 500+ pts; across entry rules it is far smaller. Anything measured on entries
    while an exit varies is measuring the exit.
  - **An isolated measurement without its own null is not evidence.** This candidate looked
    strong for four iterations on "R≥2 on 21 of 36 fires, median R 2.44" — figures computed
    with no baseline. The matched null for that same cell is 34%, with an identical R≥3 rate.
    Compute the null at the same time as the metric, or do not quote the metric.


### 11.1 Named-case registry — era stamps and pinning tests (added 2026-08-29, ADDITIVE)

Every figure above now also lives, once, in `agent/trader/named_cases.py`, and
`agent/trader/test_named_cases.py` fails when this document and that registry disagree
about any of them — in either direction, and including a pinning test that has been
renamed away. Two representations of the same rules always diverge; being *surprised* by
it is what this stops.

The ERA column exists because §6.2 found the general problem while §6's own records
carried no era stamp: **a knob change dated AFTER a validation silently invalidates that
validation's entry set.** A pre-2026-08-22 P&L may not be asserted as a current target
without an explained delta (`explained_delta` in the registry carries it).

| Day | § | Documented outcome | Era | Pinned by |
|---|---|---|---|---|
| 07-21 | §11 takeover scanner | entry 09:35:00, P&L -42.25, 2 att | `age-anchor-20260826 (2026-08-26)` | `test_0721_the_1m_gap_takes_over_at_the_stop_tick` |
| 07-21 | §6.2 | P&L +61.00, 3 att *(accepted tolerance)* | `age-anchor-20260826 (2026-08-26)` | `test_0721_within_one_cycle_of_the_current_rules_figure` |
| 07-24 | §8 crossed-trigger precedence | entry 09:36:00, @ 28579.0, P&L +146.50 | `age-anchor-20260826 (2026-08-26)` | `test_0724_crossed_trigger_precedence_preserves_the_collapse_capture` |
| 07-31 | §11 takeover scanner / §10.2 | entry 09:44:00, P&L -67.50, 3 att | `age-anchor-20260826 (2026-08-26)` | `test_0731_binds_the_deepest_penetrated_gap` |
| 08-05 | §11 takeover scanner | no takeover *(day-level gap)* | `age-anchor-20260826 (2026-08-26)` | `test_a_close_through_dead_candidate_is_never_a_takeover` |
| 08-05 | §6.2 | P&L +235.38, 1 att *(day-level gap)* | `age-anchor-20260826 (2026-08-26)` | `test_0805_is_registered_as_unreproducible_with_its_reason` |
| 08-06 | §6.2 | P&L +125.12, 1 att *(accepted tolerance)* | `age-anchor-20260826 (2026-08-26)` | `test_0806_adds_the_negative_cycle_the_height_raise_admits` |
| 08-07 | §7 / §11 §7 named tests | strict 24h track | `age-anchor-20260826 (2026-08-26)` | `test_a_fresh_extreme_keeps_the_strict_24h_track` |
| 08-10 | §6.2 | no fire | `age-anchor-20260826 (2026-08-26)` | `test_0810_does_not_fire_at_all` |
| 08-11 | §10 | FLAT | `buffer7-h35 (2026-08-19)` | `test_0811_is_a_no_entry_day_with_every_mechanism_armed` |
| 08-11 | §10 / §11 §5 | FLAT | `age-anchor-20260826 (2026-08-26)` | `test_0811_is_flat_the_documented_accepted_skip` |
| 08-12 | §10 | P&L +46.75, 2 att | `buffer7-h35 (2026-08-19)` | `test_0812_spends_two_attempts_and_the_dol_floor_vetoes_the_first_fallback_candidate` |
| 08-12 | §11 takeover scanner | no takeover *(day-level gap)* | `age-anchor-20260826 (2026-08-26)` | `test_a_close_through_dead_candidate_is_never_a_takeover` |
| 08-13 | §10 | entry 09:33:26, @ 29908.25, P&L +89.25, 1 att | `buffer7-h35 (2026-08-19)` | `test_0813_binds_the_5m_continuation_and_no_other_mechanism_preempts_it` |
| 08-14 | §10 | P&L +99.50, 3 att | `age-anchor-20260826 (2026-08-26)` | `test_0814_reaches_its_dol_on_a_documented_second_but_NOT_the_documented_shape` |
| 08-14 | §8 / §10 | entry 09:32:00, @ 30245.5, P&L +99.50, 3 att | `age-anchor-20260826 (2026-08-26)` | `test_0814_market_at_the_1s_mid_and_exactly_three_attempts` |
| 08-18 | §11 §5 per-day evidence | entry 09:40:58, @ 29763.25, P&L +229.75, 1 att *(day-level gap)* | `adopted-20260822 (2026-08-22)` | `test_0818_enters_on_its_fresh_tick_not_at_the_window_end` |
| 08-18 | §7 / §11 §7 named tests | entry 09:42:00, @ 29760.25, P&L +226.75, 1 att *(day-level gap)* | `age-anchor-20260826 (2026-08-26)` | `test_0818_fires_once_the_days_only_entry` |
| 08-21 | §11 ERRATUM 2026-08-22 | P&L +92.75 **EXCLUDED** | `buffer7-h35 (2026-08-19)` | `test_the_0821_fill_row_is_registered_as_EXCLUDED_with_its_reason` |
| 08-21 | §11 §5 pre-arm penetration | FLAT | `age-anchor-20260826 (2026-08-26)` | `test_0821_is_flat_prior_penetration_only` |
| 08-25 | §7 / §11 §7 named tests | entry 09:50:00, @ 29392.75, P&L +174.37, 1 att | `age-anchor-20260826 (2026-08-26)` | `test_0825_fires_and_is_the_case_that_retired_the_distance_key` |

**Eras.** `pre-veto` (before 2026-08-15's DOL-floor veto and §8 takeover) ·
`buffer7-h35` (entry buffer 7, structural stop, max height 35) ·
`adopted-20260822` (buffer 3, SL cap 25, max height 45) ·
`age-anchor-20260826` (those knobs plus §7's AGE-keyed anchor and §5's FRESH tick).

**Marks.** *(accepted tolerance)* — the document pre-accepts a reproduction within one
cycle and says not to chase it. *(day-level gap)* — the ENTRY reproduces but the day's
P&L cannot be checked from what is recorded here; `named_cases.NO_DAY_REPRODUCTION` gives
each reason. **EXCLUDED** — recorded, marked, and never used as a target.

**The nine uncontaminated calibrated rows** (07-17, 07-21, 07-31, 08-07, 08-12, 08-13,
08-14, 08-17, 08-18) are one `buffer7-h35` measurement superseded by the same 4.00-pt
entry-buffer trim; 08-21's row stays recorded and marked rather than deleted, because
deleting it loses the knowledge that it was considered and why it failed.

**Boundary held:** this table says WHAT was measured and WHICH test enforces it. It does
not say HOW the Executor stores facts — implementation changes far faster than rules, and
that is how a specification rots.
