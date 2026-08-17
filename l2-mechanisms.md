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
2026-08-16. NOTE: the
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
- **DOL-floor veto (all mechanisms, every entry decision):** an entry is vetoed unless at
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
- **Stop-loss:** at the opposite end of the bound FVG plus a small buffer. Total risk =
  gap height + entry buffer + stop buffer. Stays subject to the existing validator rule (stop
  must not satisfy any thesis `falsified_if`).

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
  entering the gap's range — or immediately if price is already inside / has already entered
  it. Order sits `entry buffer` beyond the FVG's far end in the continuation direction (e.g.
  short below a bearish FVG's lower bound during a downtrend). Full negation of the gap is
  accepted as the cost of certainty.
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
  - No usable 5m FVG: every eligible 5m gap is beyond the max-distance guard or offers bad
    risk:reward to the DOL at its fixed entry price. Bad RR disqualifies that 5m gap from
    binding entirely — so this mechanism never coexists with a resting 5m stop-entry, and no
    cross-mechanism cancel is ever needed. (A resting 1m negation stop from §4's widened
    fallback MAY coexist — the same 5m-unusable state arms both; single-stop-entry policy and
    first-trigger-wins apply.) If a usable 5m binding appears before this
    mechanism's trade triggers, this mechanism stands down.
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
- **Stop-loss:** the entry bar's opposite-wick edge, CAPPED at 15 pts from the entry price
  (wick edge if nearer). The cap places the stop inside the swept zone — a plain retest of
  the sweep kills the trade (08-10's stop survived by 9.75 pts at 1s); a §6-style
  wick+3-capped-30 alternative was identical on the studied dates but WON the §10.3
  unseen sweep by ~+91 (it kept a deep-wick +316.5 winner the 15-cap stopped at −15) —
  the highest-priority §9 A/B, single-flip sensitive.
- **Scope:** lives and dies with the plan's `valid_while`/thesis — unscoped it fires two
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
  replacement, or when the thesis itself is invalidated/exhausted.
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
- **Audit:** every bind / re-bind / disarm decision is logged (JSONL, extends the existing
  audit conventions) for post-session analysis.

## 9. Starting values (regression tuning knobs)

| Parameter | Starter | Rationale |
|---|---|---|
| Entry buffer beyond FVG far end | 7 pts | Sits well against typical MNQ 5m wick noise; later upgrade option `max(7, 0.3 × 5m ATR)` |
| Stop-loss buffer beyond opposite end | 3 pts | Total risk = gap height + 10 pts; tight for a wick-prone zone but price shouldn't come back gap+10 if the move is real — highest-variance knob: cleared by only 1.0–7.5 pts across the four validation examples; consider ATR-scaling early |
| Min FVG height | 5 pts | Filters drift-noise gaps carrying no displacement information — initial binding only; ladder targets exempt (§2) |
| Opening settle window | L1 arm time → 09:30:30 | §2; suspends placement/triggering through pre-open drift + RTH-open burst |
| 5m distance invalidation | 60 pts anti-trade beyond the gap | §2; permanent, unlike the momentary max-distance guard |
| Max FVG height | 35 pts | Caps worst-case risk at ~45 pts/attempt (×3 attempts ≈ 135 pts plan exposure) |
| Max distance, current price → trigger | 60 pts | Beyond that we donate too much of the multi-hour L1 move |
| No-move zone around resting trigger | 15 pts | See §8 |
| `fvg_1m_post_extreme` SL buffer beyond excursion extreme | 2 pts | §6; excursion-anchored, not gap-edge-anchored |
| `fvg_1m_post_extreme` SL cap | 30 pts from entry | §6; bounds deep-excursion episodes |
| `fvg_1m_post_extreme` early-runaway trigger | 25 pts beyond the exit edge | §6; plus beyond-bar-open condition |
| Stop-out cooldown | until the stop-out 1m bar closes | §2; acts on current state at the close (crossed trigger ⇒ market) |
| DOL-floor veto | 60 pts remaining, entry → DOL | §2; ABSOLUTE floor, not an RR ratio; winners ≥65.75 / losing chases ≤45.5 on studied dates — thin band, tune early |
| `extreme_reject_close` quiet count | 3 consecutive 1m closes | §7; tick-based restarts |
| `extreme_reject_close` SL | opposite-wick edge, capped 15 pts from entry | §7; A/B alternative wick+3 capped 30 — identical on studied dates but +91 better on the §10.3 sweep (deep-wick winners); top-priority A/B |
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

- 5m FVG detection + leg segmentation as L3 facts/data products (existing detection is 1hr/4hr
  in `daily.py` and 1m-based `detect_fvg` in `strategy_smt.py`; 5m does not exist yet).
- 5m bar construction/alignment for FVG detection.
- New §7 mechanism enum entries (`fvg_negation_reversal`, `fvg_return_continuation`,
  `fvg_1m_post_extreme`, `extreme_reject_close`) +
  validator support.
- Ladder re-bind + close-based eligibility tracking in the L3 binding engine (per-FVG
  close-through state on the gap's own timeframe, adverse-path next-FVG detection,
  seconds-level order move, 1m-fallback binding for spike legs, opening settle window,
  stop-out cooldown, crossed-trigger market execution).
- `fvg_1m_post_extreme` support: day-extreme tracking relative to 5m-FVG creation times,
  per-gap RR-to-DOL disqualification test, strictly-inside entry detection, episode state
  machine (entering-bar close verdicts, close-color gates, early-runaway trigger,
  excursion-extreme tracking with SL cap), exit-tick market execution.
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
- 07-23 5m bar-construction discrepancy: a 1m-resampled 5m series shows the 09:40–09:44 bar
  closing 28856, above the bound gap top 28827.5, BEFORE the validated fill — which §2
  close-through eligibility should have killed. Likely a 5m bar-alignment difference vs the
  original study; reconcile during implementation (the §8 backcheck verdict is unaffected —
  no deeper gap was penetrated either way).
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
- **Planless-day shadow ledger (forward holdout instrumentation):** on any session where
  the 09:20 boundary resolves to NEUTRAL (no plan armed) but a mechanism setup would have
  fired under an oracle plan, log the paper outcome (mechanism, entry, SL, result). The §2
  stay-dark policy rests on one known instance (07-17); this ledger decides empirically
  whether the foregone-setup cost is material — and if it ever is, the fix is upstream
  (recall cadence / projection stretch-gates), not an L2-side plan source.
- Per-mechanism L2-supplied invalidation criteria — deferred.
