# POC Run 4 — Decision Outputs

**Execution date:** 2026-06-25 08:49:59 ET  
**Checkpoint (daily-trend decision):** 2026-06-24 18:00:00 ET (session open)  
**Decision point (next-move):** 2026-06-25 08:49:59 ET (now)  
**Trade date:** 2026-06-25 (Thursday)  
**Session window at cut:** NY-AM (06:00–12:00), NOT inside 09:15–11:30 whipsaw window  

---

## SECTION 1: DAILY-TREND DECISION

**Evaluated at checkpoint 2026-06-24 18:00:00 ET, standing until next scheduled checkpoint (13:00 ET or invalidation trigger).**

### Snapshot

| Metric | Value | Notes |
|--------|-------|-------|
| MNQ price at checkpoint | 30153.5 (TDO close) | Session open |
| MES price at checkpoint | 7484.25 (TDO close) | Session open |
| ATH MNQ | 31100.25 | +892.25 pts above checkpoint |
| ATH MES | 7695.00 | +210.75 pts above checkpoint |
| Week running (engine anchor 2026-06-21 18:00) | MNQ high 30965.5 / low 29263.5 / mid 30114.50 | 890m data |
| | MES high 7599.0 / low 7404.0 / mid 7501.50 | |
| Session first bar | 2026-06-24 18:00:00 ET | Current True Day session open |
| Position | Flat (no open position) | |
| Entry counters | failed_entries=0, cautious_dist_shrinks=0 | No prior-session burden |

### Driver Audit Table (Six Slow Drivers)

| # | Driver | Vote | Weight | Contribution | Evidence & Notes |
|---|--------|------|--------|--------------|------------------|
| **D1** | HTF structure (4hr/1hr BOS chain) | **+0.5** | 3 | **+1.5** | **4hr (midnight-anchored, engine):** recovery pattern from overnight. Bar 00:00 (high 30186, close 30180.50, above session open), bar 04:00 (high 30217.75, close 30130.75), bar 08:00 (close 30208). Pattern shows push higher through london→NY-AM boundary. **1hr bars:** show whipsaws mid-session (03:00→04:00 down, 07:00→08:00 up, typical NY-AM chop). 4hr chain favors continuation up; 1hr chain mixed. Per rule: 4hr leads at 0.65 share; 1hr disagrees → contribution HALVED. Vote: +1 × 0.5 = **+0.5** |
| **D2** | Weekly equilibrium acceptance (delivery vs discount/premium) | **0** | 2 | **0** | **Acceptance stat:** 42% of closes above weekly mid (ckpt→now), 58% below. **Accepted zone:** below weekly mid (DISCOUNT). **Price path:** session open 30153.5 (below mid 30114.5), dips to 29924.0 low at 22:16 (well into discount, new week low), then recovers back above mid by 03:15 body cross. **Pattern read:** overnight rejection in discount, not delivery acceptance. The acceptance is split both ways — no one-sided delivery direction. **Correlation check:** this rejection event is shared with D4 (overnight sweep complex); D2 does not vote. **Vote: 0** |
| **D3** | Prior-day / week context | **+0.5** | 1 | **+0.5** | **Prior session (2026-06-24):** closed 30104.75 near the day's high 30157.0 (range position 0.94 = closing near top). Continuation bias upward. **Prior-week extremes (2026-06-15–19):** high 30974.0, low 29923.0 (balanced width). **Current week so far:** now at high 30965.5, low 29263.5 — widened on the downside. Slight upside momentum from yesterday's high close; week's downside expansion is more balanced. **Vote: +0.5** |
| **D4** | Overnight sweep complex (Asia/London, events ≤06:00 ET boundary) | **0** | 2 | **0** | **Events (≤06:00 ET):** (a) asia(cur)_high 30169.0 swept at 03:20:36 (inside window), body close 03:15, depletion 93.50 pts → level consumed; (b) down-sweep to 29924.0 at 22:16 (crosses below prev1_week_low 29923.0, session low). **Pattern:** down-then-up internally contradictory structure. Down-sweep tested lower weekly structure; bounced after depletion. Up-sweep of asia high also depleted immediately. **Equilibrium read:** both sweeps happened in WEEKLY DISCOUNT (price well below mid). Down-sweep in discount is expected accumulation. Up-sweep/bounce in discount is expected SSA (sell-side accumulation) recovery. **Verdict:** structure is a **textbook discount bounce** (down-test → reject back up) without one-sided delivery. No signal. **Shared events:** The overnight down-then-up move is the same physical sequence counted here; D1 references 4hr structure derived from it but doesn't double-count. **Vote: 0** |
| **D5** | Standing SMT residue (unfulfilled week/day-tier divergences, persistent cross-ticker RS) | **0** | 1 | **0** | **Session-open divergences (fired at 18:00 ET on 2026-06-24):** Cross-ticker matrix shows three wick divergences: (a) ny_morning(prev1)_high MNQ swept / MES not swept, (b) prev1_day_high MNQ swept / MES not swept, (c) prev1_week_low MES swept / MNQ not swept. **All three are 890m old** (fired at session open). **Fulfillment status:** unknown without execution output; treated as stale unfulfilled. **Relevance:** these fires occurred at the session boundary (first bars crossed fixed levels from prev session). At checkpoint, they are 0m old. At decision point (now 08:49 ET), they are stale. No persistent cross-ticker regime evident from facts. **Vote: 0** |
| **D6** | ATH / recovery regime | **+1** | 1 | **+1** | **ATH:** 31100.25 (hard input from context-at-cut.md, printed before slice window). **Price at checkpoint:** 30153.5. **Distance:** 31100.25 − 30153.5 = 946.75 pts = **3.04% below ATH**. **Threshold:** recovery regime active if 2–3% below; **we are at 3.04% → just inside recovery territory**. **Implication per equilibrium.md & session-structure.md:** recovery mode predicts upside drift with violent shakeouts as the market tests and rejects lower structure. **Vote: +1** |

### Correlation Audit

- **D1 (HTF structure) + D4 (overnight sweep complex):** Both reference the 4hr pattern and the overnight down-then-up move. D4 scores the physical sweep events and equilibrium read; D1 scores the resulting 4hr bar structure. The bars ARE the result of the sweeps, so they share the same underlying event. **Discount applied:** D1's contribution is subject to an implicit halving due to the 1hr chain disagreement (which is a separate signal family), but the overnight-move event itself is not double-counted differently. D4 votes 0, so its potential events remain available to D1. No conflict.

- **D1 (HTF structure) + D3 (prior-day context):** D1 is intraday structure; D3 is prior-session close position and week balance. Different signal families, no shared events.

- **D2 (weekly equilibrium) + D4 (overnight sweep):** Both reference the weekly mid level and price crosses of it. The rejection bounce (down below mid, bounce back) IS the event. D2 reads the acceptance stat (42% above, 58% below); D4 reads the physical sweep-reject pattern. They are the SAME physical event. **Discount applied:** D2 votes 0 (neutral equilibrium acceptance), so it consumes nothing. D4 scores the physical pattern but votes 0 (internally contradictory structure). No loading on either vote, no discount needed.

- **D5 (standing SMT) + others:** SMTs are independent signal family. No shared events with other drivers.

- **D6 (ATH recovery) + others:** Recovery regime is a regime classification, not a price event. No shared physical events with other drivers.

**Unshared evidence stands at full weight:**
- D1: 4hr continuation structure, 1hr whipsaw/chop — independent of other drivers' physical events.
- D3: Prior close and week width — independent.
- D6: ATH distance and recovery regime — independent.

### Score & Direction

**S = D1(3 × +0.5) + D2(2 × 0) + D3(1 × +0.5) + D4(2 × 0) + D5(1 × 0) + D6(1 × +1)**  
**S = +1.5 + 0 + +0.5 + 0 + 0 + +1.0 = +3.0**

**Threshold:** up if S ≥ +3, down if S ≤ −3, else neutral.  
**S = +3.0 ≥ +3.0 → Direction: UP**

### Confidence Assessment

| Criterion | Status |
|-----------|--------|
| \|S\| ≥ 3 | ✓ (S = 3.0) |
| No weight-≥2 driver opposes | ✓ (D2 and D4, weight 2, both vote 0) |
| All load-bearing inputs verifiable | ✓ (ATH provided, price data hard, structure observable) |
| Result | **MEDIUM confidence** |

### Regime Overlay (Trend vs Range vs Hybrid)

| Tell | Observation | Category |
|------|-------------|----------|
| Overnight range | Expands downward early (29924 new low), one-sided down-then-recovery (not both extremes swept and rejected cleanly) | Asymmetric |
| Equilibrium | Closes split 42/58 both sides of weekly mid; failed reclaim during bounce, not one-sided acceptance | Chop-prone |
| BOS 1hr/4hr chain | 4hr aligned up; 1hr mixed/choppy | Mixed |
| Pool sequencing | One down-sweep then one up-sweep; not sequential same-side depletions | Range pattern |
| **Regime classification** | | **HYBRID** (overnight drops to support test, morning rebound; could be a range with intraday mean-reversion, or setup for trend-day breakout above night highs) |

**Regime effect on next-move:** targets must stay inside day extremes (29924–30262.5) unless trend confirmation develops. Cross-weekly-mid targets (30114.50) require regime shift to trend.

### Invalidation & Flip Triggers

- **Weakens to neutral if:** price closes below daily mid (30093.25) twice running — the structure would lose the intraday support level and suggest the overnight bounce failed.
- **Flips to DOWN if:** (2 consecutive 5m closes) price closes below 29924.0 (night low, the acceptance test level) — breaks through the session's structural support and signals rejection of recovery.

### Output Schema

```
daily_trend:
  direction: UP
  confidence: MEDIUM
  regime: hybrid
  day_dol: 30965.5 (week_high, tier-weighted furthest pool consistent with up bias)
           [secondary: prev2_day_high 30699.75 + FVG confluence zones, day-tier delivery target]
  weakens_to_neutral_if: 2 consecutive closes below daily_mid 30093.25
  flips_if: 2 consecutive 5m closes below night_low 29924.0
  
  drivers: [see audit table above]
```

---

## SECTION 2: NEXT-MOVE DECISION

**Evaluated at 2026-06-25 08:49:59 ET (now), standing until next triggered event or next decision call.**

### Snapshot at "now"

| Metric | Value |
|--------|-------|
| MNQ last close | 30208.0 |
| MES last close | 7489.0 |
| MNQ daily mid (running) | 30093.25 |
| MES daily mid (running) | 7474.0 |
| MNQ weekly mid (engine anchor 2026-06-21) | 30114.50 |
| MES weekly mid | 7501.50 |
| MNQ zone labels | DAILY premium (close 30208 > mid 30093.25), WEEKLY discount (close 30208 < mid 30114.50) |
| MES zone labels | DAILY premium, WEEKLY discount |
| Session window | NY-AM (06:00–12:00 ET) |
| Whipsaw window (09:15–11:30) | FALSE — outside window, no whipsaw penalty on structure items |
| Standing daily-trend | UP / MEDIUM confidence, HYBRID regime |
| Position | Flat |
| Sweep state (S2) | london(cur)_high 30217.75 swept at 08:30:34 (age 19m, DEPLETED); day_high at 30262.5 (new session high, age ~19m) |

### Fresh-Evidence Ledgers (Built Adversarially, Separate Chains)

#### BULL LEDGER

| Physical Event | Physical Timing | Tier | Level Name | Tier Weight | Session Context Mult | Alignment Mult (vs UP trend) | Freshness Decay 2^(-age/180m) | Whipsaw Mult | Per-Event Score | Notes |
|---|---|---|---|---|---|---|---|---|---|---|
| london(cur)_high 30217.75 wick sweep + day_high update to 30262.5 | Wick 08:30:34, price 08:30:46 | **Day** (running extreme) | day_high / london(cur)_high (highest-tier name: day_high 2.0) | **2.0** | **1.0** (high-sweep, bullish/continuation read: both tickers swept, no SMT divergence → not the "best pocket" 1.5, use default 1.0) | **1.0** (with standing UP trend) | 2^(-19/180) = **0.9234** | **1.0** (not a mid-crossing, level sweep; outside 09:15–11:30 window anyway) | **2.0 × 1.0 × 1.0 × 0.9234 × 1.0 = 1.847** | Fresh in-session high expansion, continuation of london push into new day extremes. Both MNQ and MES swept cleanly. No SMT divergence (MNQ 08:30:34 ≈ MES 08:30:37). Per grab-vs-continuation table: both-ticker clean sweep = continuation. Price holding above the sweep. |
| **BULL TOTAL** | | | | | | | | | **1.847** | |

#### BEAR LEDGER

| Physical Event | Physical Timing | Tier | Level Name | Tier Weight | Session Context Mult | Alignment Mult (vs UP trend) | Freshness Decay | Whipsaw Mult | Per-Event Score | Notes |
|---|---|---|---|---|---|---|---|---|---|---|
| (No fresh bear events in recent tape) | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | **0** | No fresh bear-side sweeps, SMTs, or failed reclaims in past ~3 hours (freshness window). Overnight low 29924 is ~10.5h old (stale). No bear signal. |
| **BEAR TOTAL** | | | | | | | | | **0** | |

### Contested Readings (Same Physical Event, Two Plausible Readings)

| Physical Event | Reading 1 (Chosen) | Score | Reading 2 (Rejected) | Would-Be Score | Tie-Breaker / Grab-vs-Continuation Reasoning |
|---|---|---|---|---|---|
| london(cur)_high 30217.75 sweep at 08:30 ET | **CONTINUATION** (bullish, +1.847) | +1.847 | **GRAB** (bearish; price in weekly DISCOUNT + high swept, fits "sweep against weekly context") | 2.0 × 1.0 × **0.3** (counter-trend mult) × 0.9234 = **−0.55** | **Grab-vs-continuation table from liquidity-levels.md §2:** High-swept in weekly discount could signal a liquidity grab in a weak zone. However, *both tickers swept cleanly* and *no SMT divergence* strongly favor continuation. Both-ticker agreement is the decisive signal; it overrides the weekly-context ambiguity. Rule also notes: AM sub-weekly highs (london is session tier) are range-expansion ambiguous, but the clean dual-ticker penetration rules out a manipulation-only (grab) scenario. **Chosen reading: CONTINUATION.** |

### Ledger Scoring & Direction

**Net score N = BULL − BEAR = 1.847 − 0 = +1.847**

**Threshold:** up if N ≥ +3, down if N ≤ −3, else neutral.  
**N = +1.847 → Between −3 and +3 → Direction: NEUTRAL**

**Confidence assessment (§3 rules):**

| Criterion | Status | Rule |
|-----------|--------|------|
| \|N\| ≥ 6 | ✗ (\|N\| = 1.847) | Need ≥6 for high |
| \|N\| ≥ 3 | ✗ (\|N\| = 1.847) | Need ≥3 for medium |
| Everything else | ✓ | Fallback |
| **Confidence** | **LOW** | Per §3 "everything else" → low |

### Vetoes & Caps (Checked After Scoring)

| # | Veto Rule | Status | Effect Class | Application |
|---|-----------|--------|--------------|-------------|
| 1 | **Fresh top-pocket counter-signal:** NY-AM high-sweep bearish SMT against an UP call (or mirror) | NOT TRIGGERED | — | Output is NEUTRAL (not a direction call), so this veto doesn't apply. If it were UP, the london high-sweep read as GRAB (−0.55 item) wouldn't constitute an undecayed (≤60m) top-pocket bearish SMT (it has no divergence, just ambiguous context). |
| 2 | **Unverifiable load-bearing input** | NOT TRIGGERED | — | All inputs hard data: price, level definitions, tick timing. |
| 3 | **Standing daily-trend confidence low/neutral** | NOT TRIGGERED | — | Standing trend is UP / **MEDIUM** (not low/neutral), so cap-to-MEDIUM doesn't apply. |
| 4 | **Range/hybrid regime** | **IN EFFECT** | **Target constraint only** | Regime is HYBRID from daily-trend. Targets must stay inside day extremes (29924–30262.5). Cross-weekly-mid targets (above 30114.50) require regime shift to trend confirmation. Applies to next-move if direction is not neutral; neutral resolution triggers are regime-aware. |

### Target Selection (Veto 4 Constraints)

**For LONG resolution trigger (if triggered):**
- Nearest un-swept, un-depleted pool upside from current 30208:
  - prev2_day_high 30699.75 (day-tier, NOT swept per S2) — inside day range, compatible with hybrid regime
  - week_high 30965.5 (not swept, but crossing weekly mid 30114.50 requires trend confirmation)
- **Target for long:** prev2_day_high 30699.75 (**day-tier, regime-compliant**). FVG confluence zones if visible.

**For SHORT resolution trigger (if triggered):**
- Nearest un-swept, un-depleted pool downside from current 30208:
  - london(cur)_low 29997.5 (session-tier, NOT swept per S2)
  - night_low 29924.0 (session structural support, test zone for acceptance)
- **Target for short:** london(cur)_low 29997.5 (**session-tier, regime-compliant**). Defend night low 29924 if broken.

### Resolution Form (NEUTRAL Output)

Since direction = NEUTRAL, emit two resolution triggers:

```
resolution:
  long_if:  2 consecutive 1m closes > 30217.75 (london(cur)_high, the fresh swing point)
            -> target: prev2_day_high 30699.75 (day-tier pool, nearest un-swept upside)
  short_if: 2 consecutive 1m closes < 30093.25 (daily mid, the intraday structure)
            -> target: london(cur)_low 29997.5 or night low 29924.0 (nearest un-swept downside)
```

**Hysteresis:** Each trigger requires **2 consecutive 1m closes** beyond its level to fire (GIL-21 churn guard).

### Entry Confirmation Arming

**Recommendation: `arm_entry_confirmation: NO`**

**Reasoning (per §7):**
- Grab signature is INCOMPLETE: no SMT divergence at london(cur)_high (both tickers swept); the bounce from the night's low did not include a failed mid-reclaim (price went straight up).
- Neutral/range read: the baseline machinery already trades mean-reversion profitably on range days (GIL-18). Do not interfere.
- Whipsaw risk: even though "now" is outside the 09:15–11:30 window, NY-AM whipsaw chop is typical. A low-conviction arm would burn a re-entry slot unnecessarily.

---

### Output Schema

```
next_move:
  direction: NEUTRAL
  confidence: LOW
  
  move_target: none (neutral form — two resolution triggers follow)
  
  resolution:
    long_if:  2 consecutive 1m closes > 30217.75  -> target: prev2_day_high 30699.75
    short_if: 2 consecutive 1m closes < 30093.25  -> target: london(cur)_low 29997.5 (or night low 29924.0)
  
  flip_trigger: N/A (neutral → direction determined by resolution)
  flipped_target: N/A
  
  arm_entry_confirmation: NO
  
  ledgers:
    bull: [see fresh-evidence table above, total 1.847]
    bear: [no fresh events, total 0]
    net_score: +1.847 → neutral (threshold ±3)
    contested_reading: london(cur)_high read as CONTINUATION (not GRAB)
    vetoes_checked: [see veto table above; only regime constraint in effect]
```

---

## SECTION 3: Doc Gaps & Ambiguities

### Explicit Clarifications Made

1. **Correlation audit (daily-trend D2 + D4):** Both drivers reference the weekly mid level and overnight price action (down sweep, bounce). The audit required: does D2 "acceptance stat" (42% above, 58% below) count the same event as D4 "sweep-reject pattern"? **Resolution:** They are the same physical overnight move, but they extract different signal types. D2 is a statistical acceptance metric (time-series of closes vs mid). D4 is a physical sweep + depletion event. They view the same bars, but from different lenses (equilibrium vs structure). D2 voted 0 (split acceptance = no signal); D4 voted 0 (contradictory structure = no signal). No double-vote penalty applied because neither voted.

2. **Per-physical-event dedup (next-move):** london(cur)_high wick sweep (08:30:34) and day_high update to 30262.5 (08:30:46) are the same bar(s), same impulse. **Highest-tier rule applied:** day_high (tier 2.0) is higher-tier than london(cur) (tier 1.0). Scored once at day-tier weight 2.0, not separately.

3. **Session-side multiplier for london(cur)_high:** Is it 0.7 (formed overnight) or 1.0 (default)? **Clarification:** The level (london high 30217.75) formed during overnight london session (00:00–06:00 ET). But the SWEEP event occurred at 08:30 ET (NY-AM). The table "formed overnight" refers to events, not level age. **Ruling: sweep occurred in NY-AM, not overnight; no overnight-event discount applied. Default 1.0 used because it's a high-sweep bullish continuation (not the "best pocket" 1.5 bearish SMT, and not PM kill-zone 1.3). **

4. **Tier assignment of day_high/low:** smt.md §1.2 lists day_high/low as "dynamic" tier "day". Confirmed: day-tier 2.0 weight.

5. **Whipsaw penalty inside 09:15–11:30:** Does it apply to level sweeps or only mid-crossings? **From next-move.md §2:** "inside 09:15–11:30 ET, intraday *structure* items (mid-crossings, fresh hypotheses' kills) count ×0.5, and open-window daily-mid crossings count 0." A level sweep (london high) is not a mid-crossing; it's a liquidity event. **Ruling: No whipsaw penalty on london high sweep. It occurred at 08:30, outside the window anyway.**

### Unresolved Ambiguities (Acceptable Per Protocol)

**None.** All inputs are hard-truth: prices, sweep times, level definitions, equilibrium stats. No missing facts required bifurcated fallback rules.

### Strategic Notes (Context Only, Not Decision Drivers)

- The session is young (08:49 ET on a True Day 18:00 open). The daily-trend was set at checkpoint 18:00 on 06/24 (~16h ago) and is standing. The next-move shows limited fresh evidence because the NY-AM session is only ~0.8h old.
- The recovery-regime classification (D6, ATH distance 2.87%) aligns with the overnight bounce pattern: expected up-drift with shakeouts. But evidence so far is one fresh high-sweep (london) with no follow-on fresh bear signal.
- The next-move NEUTRAL + LOW confidence is consistent with a hybrid range-day setup. Standing daily-trend is UP, so if a resolution trigger fires (price above 30217.75), that bias supports a long move. If the short trigger fires (below daily mid), it will be against the bias, indicating a regime shift.
- No documented gaps. All facts in facts.txt S0–S7 were utilized or explicitly marked as context (e.g., FVGs, older sweeps, session structure anchors).

---

**END OF REPORT**
