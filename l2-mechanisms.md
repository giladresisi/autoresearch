# L2 Entry Mechanisms — 5m FVG Reversal & Continuation

Status: DESIGN (2026-07-25; ladder re-bind, close-based eligibility, and L3 artifact-autonomy
added 2026-07-27 after validation on the 07-14/16/17 session examples; thesis-anchored
continuation gap relevance and the 1m reversal fallback added 2026-08-07 after the 07-16
09:30-open study; opening settle window, ladder min-height exemption, and crossed-trigger
market execution added same day after the 07-15 09:30-open study; `fvg_1m_post_extreme`
mechanism and stop-out cooldown added 2026-08-08 after the 07-20/21/22 open studies; cooldown
made state-based, not fresh-precondition, after the 07-23/24 studies; pre-open entry
suspension, 5m distance invalidation, and the §6 episode/close-color entry logic with
early-runaway entry and SL cap added 2026-08-09 after the 08-03..08-06 studies. NOTE: the
rules were tuned on the 07-14..08-06 sample — every trading day in that sample nets positive,
several by sub-7-pt stop clearances; forward-test on unseen dates before trusting). Extends
`agent-optimizations.md` §7 (mechanism enums). Defines two new entry mechanisms the L2 trade
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

## 2. Common rules (both mechanisms)

- MNQ FVGs only; no MES/SMT counterpart required. 5m is the primary timeframe; the reversal
  mechanism has a 1m fallback (§4) for fast spike legs that print no 5m gap, and §6 defines a
  1m-based mechanism for when the 5m structure is unusable outright.
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
  not the cooldown.
- **Crossed-trigger ⇒ market execution:** whenever L3 acts (settle-window end, ladder move,
  re-bind, initial placement) and the computed trigger price is already crossed in the trade
  direction, the entry executes as a market order with the same FVG-derived stop-loss — the
  resting stop-entry is the normal case, market the degenerate case of the same mechanism.
  No separate "market entry" mechanism exists.
- **Max-distance guard:** the trigger price must sit within a capped distance of current price
  (analogous to the S8 DOL proximity guard).
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
- **1m fallback:** when the leg being reversed printed no eligible 5m FVG (fast spike/judas
  legs often complete inside one or two 5m bars), L3 binds the most recently created eligible
  1m FVG belonging to that leg instead — same buffers, height filters, guards, and lifecycle.
  Close-through disqualification runs on 1m closes for a 1m-bound gap. (Validated on the 07-16
  09:30 judas spike: no 5m gap existed; the leg's 1m gap gave short 29464.5 / SL 29481.5 →
  +159 to DOL.)
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
    cross-mechanism cancel is ever needed. If a usable 5m binding appears before this
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
  gates skipped the noise cycles that a raw exit-tick rule would have taken.

## 7. L3 binding & order lifecycle

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
- **Audit:** every bind / re-bind / disarm decision is logged (JSONL, extends the existing
  audit conventions) for post-session analysis.

## 8. Starting values (regression tuning knobs)

| Parameter | Starter | Rationale |
|---|---|---|
| Entry buffer beyond FVG far end | 7 pts | Sits well against typical MNQ 5m wick noise; later upgrade option `max(7, 0.3 × 5m ATR)` |
| Stop-loss buffer beyond opposite end | 3 pts | Total risk = gap height + 10 pts; tight for a wick-prone zone but price shouldn't come back gap+10 if the move is real — highest-variance knob: cleared by only 1.0–7.5 pts across the four validation examples; consider ATR-scaling early |
| Min FVG height | 5 pts | Filters drift-noise gaps carrying no displacement information — initial binding only; ladder targets exempt (§2) |
| Opening settle window | L1 arm time → 09:30:30 | §2; suspends placement/triggering through pre-open drift + RTH-open burst |
| 5m distance invalidation | 60 pts anti-trade beyond the gap | §2; permanent, unlike the momentary max-distance guard |
| Max FVG height | 35 pts | Caps worst-case risk at ~45 pts/attempt (×3 attempts ≈ 135 pts plan exposure) |
| Max distance, current price → trigger | 60 pts | Beyond that we donate too much of the multi-hour L1 move |
| No-move zone around resting trigger | 15 pts | See §7 |
| `fvg_1m_post_extreme` SL buffer beyond excursion extreme | 2 pts | §6; excursion-anchored, not gap-edge-anchored |
| `fvg_1m_post_extreme` SL cap | 30 pts from entry | §6; bounds deep-excursion episodes |
| `fvg_1m_post_extreme` early-runaway trigger | 25 pts beyond the exit edge | §6; plus beyond-bar-open condition |
| Stop-out cooldown | until the stop-out 1m bar closes | §2; acts on current state at the close (crossed trigger ⇒ market) |
| Leg reversal threshold | max(30 pts, 25% of leg range) | §3 |
| Min qualifying leg range | 50 pts | §3 ("significant") |
| Leg recency | 60 min | §3 |
| Leg segmentation lookback | ~4 hours | §3 |

## 9. Implementation gaps (design-complete, work remaining)

- 5m FVG detection + leg segmentation as L3 facts/data products (existing detection is 1hr/4hr
  in `daily.py` and 1m-based `detect_fvg` in `strategy_smt.py`; 5m does not exist yet).
- 5m bar construction/alignment for FVG detection.
- New §7 mechanism enum entries (`fvg_negation_reversal`, `fvg_return_continuation`,
  `fvg_1m_post_extreme`) +
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
- Per-mechanism L2-supplied invalidation criteria — deferred.
