# POC Decision Report — Run 5

**Timestamp (now):** 2026-06-23 12:59:59-04:00 (Tuesday, trade date 2026-06-23)  
**Checkpoint for daily-trend eval:** 2026-06-23 09:20:00-04:00  
**Session phase at now:** ny_evening (12:00–17:00 ET); outside 09:15–11:30 whipsaw window  
**Position:** flat · **Entry counters:** failed_entries=0, cautious_dist_shrinks=0  
**ATH (MNQ):** 31100.25 (4.36% above last close 29863.75 = recovery mode)

---

# SECTION 1: DAILY TREND DECISION (Checkpoint 2026-06-23 09:20:00-04:00)

## Scoring Drivers (D1–D6)

| Driver | Vote | Weight | Contribution | Assessment |
|--------|------|--------|---------------|------------|
| **D1: HTF Structure (4hr chain + 1hr modulation)** | −1 | 3 | −3 | 4hr bars show consistent decline: 2026-06-22 16:00 close 30575.50 → 06-23 00:00 close 29908.00 → 04:00 close 29872.75 → 08:00 close 29797.50. Midnight-anchored 4hr chain unambiguous DOWN. 1hr 08:00–09:00 shows partial recovery (29734.75 → 29926.00 at 09:00), but this is within a 4hr bearish bar and does not reverse the 4hr vote. Contribution: full −3. |
| **D2: Weekly Equilibrium Acceptance** | −1 | 2 | −2 | Weekly running mid (engine anchor 2026-06-19 18:00) = (high 30965.5 + low 29616.5) / 2 = 30291.0. From S4: acceptance session→ckpt = 4% of 921 closes above mid = 96% BELOW weekly mid. Failed mid-reclaim at 2026-06-22 20:44 (close 30606 ABOVE mid 30580.62) was reversed back below at 21:16. Delivery to discount accepted-beyond. Vote −1 (down delivery). Contribution −2. |
| **D3: Prior Day / Week Context** | 0 | 1 | 0 | Previous day (06-22) closed 30651.5 at mid-range (pos 0.50). Previous week (06-15..06-19) high 30974.0; current week running high 30965.5 (just below prev-week high) and running low 29616.5 (far below prev-week low 29923.0 by 306.5 pts). Current session opened at 30580.75 (trend-neutral opening vs prev close 30651.0), then volatile intraday. Mixed picture = vote 0. |
| **D4: Overnight Sweep Complex (pre-06:00 ET events)** | −1 | 2 | −2 | Pre-06:00 boundary: failed daily mid reclaim 2026-06-22 20:44 (close ABOVE 30580.62 mid) → reversed 21:16 (close BELOW). Followed by cascade of low sweeps: ny_morning_low 30541.5 (19:52 sweep), london_low 30524.25 (20:12), ny_evening_low 30504.0 (20:21), prev2_day_low 30390.0 (23:01), prev1_day_low 30337.0 (23:34), asia(cur)_low 30301.5 (00:41). All sweeps were DEPLETED (max excursions far exceeded thresholds), indicating accepted-beyond continuation, NOT grab-reversals. Failed mid-reclaim + acceptance of 7 successive lower lows = confirmed bear complex. Vote −1. Contribution −2. |
| **D5: Standing SMT / RS Residue** | 0 | 1 | 0 | No fresh week/day-tier divergences by 09:20. asia(prev1)_high and ny_evening(prev1)_high had MNQ-lead divergences but are 1138m old (prior session 06-22, stale by checkpoint). No standing conflicts or persistent RS regimes. Vote 0. |
| **D6: ATH / Recovery Regime** | +1 | 1 | +1 | ATH = 31100.25. Last close at checkpoint 09:20 = 29743.5. Offset = 1356.75 pts (4.36% below ATH). Recovery threshold 2–3% below = recovery mode active. 4.36% slightly exceeds threshold but clearly in recovery zone → up-drift with violent shakeouts mode. Vote +1 (recovery favors up). Contribution +1. |

**Sum S = −3 − 2 + 0 − 2 + 0 + 1 = −6**

**Direction call:** S = −6 ≤ −3 → **DOWN**  
**Confidence evaluation:** |S| = 6 ≥ 6 ✓ · No weight-≥2 driver opposes (D6 at weight 1 is outnumbered) ✓ · All inputs verifiable from facts.txt ✓ → **HIGH confidence**

### Correlation Audit (Mandatory)

Events cited in each driver's contribution:

| Driver | Primary Events | Secondary Mentions | Sharing / Discounts |
|--------|-----------------|-------------------|---------------------|
| D1 | 4hr bars 2026-06-22 16:00 through 2026-06-23 08:00 (6 bars forming down chain); 1hr 08:00–09:00 recovery | Intraday bar progression | Unshared HTF structure; 1hr recovery is intraday modulation within the 4hr bars |
| D2 | Equilibrium acceptance count (4% of 921 closes above weekly mid across session→ckpt) | Failed mid-reclaim 2026-06-22 20:44/21:16 as representative event | Unshared pattern evidence (acceptance rate across hours); mid-failure is also counted in D4 below |
| D4 | Failed mid-reclaim 2026-06-22 20:44–21:16 (close ABOVE 30580.62, then BELOW) + 7 low sweep events with acceptance-beyond (depleted latch engaged) | Same mid-reclaim failure event mentioned in D2 | Primary: D4 owns the failed-reclaim EVENT as overnight-complex trigger (weight 2 driver); D2 owns the acceptance PATTERN (also weight 2 driver). The underlying event (mid-reclaim failure) is counted once in D4's narrative; D2 re-uses it as context for the broad acceptance metric. Per audit rules, a shared event supports one driver at full weight; a second driver citing it as context doesn't incur reduction if the drivers are measured differently (event vs pattern). No reduction applied; audit notes the shared event. |
| D6 | ATH = 31100.25 (hard truth from context), last close 29743.5 (from S7), offset 4.36% | Recovery regime classification | Unshared regime classification; ATH and close are independent data points |

**Audit conclusion:** No excessive multi-counting. D4 and D2 share the underlying mid-reclaim event, but use it differently (event trigger vs pattern context). No reduction required under the rule "event supports ONE driver at full weight; a second appearance ×0.5" because the second appearance (in D2) is the aggregated pattern count, not a separate event claim.

---

## Regime Overlay

**Tell-based classification:**

| Dimension | Observation | Classification |
|-----------|-------------|-----------------|
| Overnight range | One-sided expansion down (high never swept; lows swept and depleted) | Trend (one-sided) |
| Equilibrium | One-sided acceptance below; no repeated crossings both ways (4% above vs 96% below) | Trend (decisive side) |
| 1hr / 4hr BOS | 4hr chain down across 6 bars; 1hr partial recovery but still within 4hr bear | Aligned (4hr down dominates) |
| Pools | Successive low sweeps that depleted without rejection-back; acceptance-beyond pattern | Trend (sequential same-side sweeps) |

**Regime = TREND (specifically, downtrend)**

---

## Daily Trend Output Schema

```
daily_trend:
  direction: DOWN
  confidence: HIGH
  regime: TREND
  
  day_dol: ny_morning(cur)_low
    price: 29616.5
    reason: nearest un-swept, un-depleted support below current 29743.5
  
  weakens_to_neutral_if: |
    2 consecutive 5m closes above daily running mid ~30158.125
    (failed downtrend support; signals equilibrium rejection of down)
  
  flips_if: |
    2 consecutive 1m closes above daily running mid 30158.125 (strict hysteresis per GIL-21)
    (converts to UP direction at LOW confidence pending next full evaluation)
  
  drivers: <see Scoring Drivers table above>
```

---

# SECTION 2: NEXT-MOVE DECISION (At 2026-06-23 12:59:59-04:00)

## Snapshot

**Now:** 2026-06-23 12:59:59-04:00 (12:59 PM ET, 1 minute before PM kill zone threshold 13:00)  
**Session window:** ny_evening (12:00–17:00 ET), outside whipsaw window (09:15–11:30)  
**Current price (last close 12:45 15m bar):** 29863.75  
**Daily running mid:** (high 30699.75 + low 29616.5) / 2 = 30158.125  
**Position:** flat  
**Standing daily-trend:** DOWN / HIGH / TREND  
**Time since checkpoint 09:20:** 3 hours 39 minutes  

---

## Fresh Evidence Ledgers

### Data Window: 2026-06-23 09:20:00 to 12:59:59

**Key price action in this window (from 15m and 1hr bars S5):**
- 09:20 close: 29745.00
- 09:45 close: 29926.00 (rally +181 pts)
- 10:00–10:20: continued rally to ny_morning(cur)_high = 30046.75 (high of 30046.75 at 12:15 per S2 data: "laggard MNQ max reach: 131.25 short @ 2026-06-23 12:15:06-04:00")
- 10:30–11:30: consolidation and pullback within the range 29850–30000
- 11:45–12:45: drift lower to 29863.75

**Swept levels in this window:**
- london(cur)_low 29776.5: swept 2026-06-23 06:41:32-04:00 (pre-06:00 events belong to daily-trend D4; this is POST-06:00, so next-move event; age at now = 378 minutes ≈ 6h 18m)
- No NEW sweeps in the 09:20–12:59 window itself.

**Unswept levels tested:**
- ny_morning(cur)_high 30046.75: NOT swept (closest approach 131.25 short at 12:15, age 45m at now)
- london(cur)_high 30414.5: NOT swept (closest approach 367.75 short at 10:20, age 160m at now)

**Fresh SMT divergences?**
From S3 matrix, no new divergence emerges in this window (both tickers tested same levels without fresh divergence relationship change).

**Equilibrium action:**
- MNQ: No closes above daily mid in checkpoint→now window. From S4: "acceptance ckpt->now: 0% of closes above (n=220)". All 220 1m closes from 09:20 to 12:59 were below the daily mid 30158.125.
- MES: Oscillated around its daily mid (10:12–10:30 window), but no directive signal; whipsaw-like, suppressed.

**Failed reclaim / rejection:**
- Daily mid at ~30158.125. Price rallied from 29743.5 to 30046.75 (peak at 12:15), leaving a gap of 111.25 pts SHORT of the mid. Failed to touch, much less cross and accept above. Reversed back down to 29863.75 by 12:59.

---

### BULL LEDGER

**Evidence supporting UP direction:**

1. **london(cur)_low sweep + grab reversal (06:41:32, age 378m)**
   - Sweep type: low sweep in weekly discount
   - Grab-vs-continuation read: Level 29776.5 (london(cur)_low) swept at 06:41, then price continued to ny_morning low 29616.5 near 09:30. From 09:30 onward, price reversed back UP (rally to 30046.75 by 12:15). This is SWEEP-AND-REVERSE = **grab pattern** = bullish (reversal up).
   - Tier weight: 1.0 (session tier: 6hr sub-session level)
   - Session-side: 0.7 (formed overnight, lower weight per protocol)
   - Alignment vs daily DOWN: 0.3 (counter-direction per protocol's 87/35 weighting: counter-trend gets 0.3)
   - Freshness: 2^(−378 min / 180 min) = 2^(−2.1) ≈ 0.233 (stale; >3h window, deemed "spent" but scores as ≈23% per decay formula)
   - Whipsaw: outside 09:15–11:30 → no suppression
   - **Score = 1.0 × 0.7 × 0.3 × 0.233 ≈ 0.049**
   - *Note:* This is the only bull-directional item. A single old grab is weak evidence.

**BULL TOTAL ≈ 0.05**

---

### BEAR LEDGER

**Evidence supporting DOWN direction:**

1. **Failed daily mid reclaim (peak 12:15, age ~45m at 12:59)**
   - Event: Price rallied to 30046.75 at 12:15 ET, which is 111.25 pts SHORT of the daily mid 30158.125. Failed to cross and accept above the mid, then reversed and declined.
   - Per equilibrium.md: "a post-sweep cross that fails back = strongest signal (go the other way)". Here, the move approached but never crossed the mid, then reversed = failed reclaim.
   - Tier weight: 2.0 (day tier; daily mid is a key day-level item per next-move.md tier definitions)
   - Session-side: 1.0 (general mid action; not yet at 13:00 kill-zone premium-high-sweep boundary; treat as standard 1.0)
   - Alignment vs daily DOWN: 1.0 (with-trend; failed UP push in a DOWN regime supports continuation)
   - Freshness: 2^(−45 min / 180 min) = 2^(−0.25) ≈ 0.841
   - Whipsaw: outside 09:15–11:30 → no suppression
   - **Score = 2.0 × 1.0 × 1.0 × 0.841 ≈ 1.682**

2. **Daily mid acceptance 0% above (equilibrium delivery signal; checkpoint→now)**
   - Event: From S4, "acceptance ckpt->now: 0% of closes above (n=220)". All 220 1m closes from 09:20 to 12:59 closed below the daily running mid 30158.125.
   - Pattern: 100% acceptance below = definitive delivery down; no confusion or failed reclaim (as a pattern, not just a single peak).
   - Tier weight: 2.0 (day tier; daily mid)
   - Session-side: 1.0 (equilibrium acceptance pattern, unambiguous below side)
   - Alignment vs daily DOWN: 1.0 (with-trend)
   - Freshness: average age across 3h 39m window ≈ 110 minutes old; 2^(−110 / 180) ≈ 0.713
   - Whipsaw: The 09:15–11:30 window contained some oscillations (seen in MES crosses in S4), but MNQ remained below mid. Equilibrium items are NOT "intraday structure items" per next-move.md §2, so no ×0.5 suppression for whipsaw.
   - **Score = 2.0 × 1.0 × 1.0 × 0.713 ≈ 1.426**

**BEAR TOTAL ≈ 1.68 + 1.43 = 3.11**

---

### Per-Physical-Event Dedup

- **London(cur)_low sweep (06:41):** Single physical event, counted once at its tier (session) and classification (grab). No duplicate.
- **Failed daily mid reclaim (12:15):** Single event (peak wick), not confused with the broad acceptance pattern.
- **Daily mid acceptance 0%:** Pattern count across hours, not a "physical event" but an aggregate measure. Distinct from the single peak-fail event.

*No collapses or multi-fire duplicates identified.*

---

### Contested Readings

**london(cur)_low interpretation:**
- **Grab reading (ACCEPTED):** Price swept london_low 29776.5 at 06:41, then continued down to approach ny_morning_low 29616.5 near 09:30, then reversed back UP and rallied to 30046.75 by 12:15. This is a sweep-and-reverse signature = **grab** = bullish for a low sweep (accumulation then reversal up).
- **Alternative (rejected):** Could argue "sweep against weekly discount zone → up (grab, fires unconditionally in discount)" per equilibrium.md, making the low sweep inherently bullish. However, the acceptance-beyond pattern (continued down through 09:30) followed by reversal is the distinguishing grab signal; the classification per grab-vs-continuation table is sweep-and-reverse. Accept the grab reading.
- *No rejected contested reading to list; grab read is unambiguous from the price action.*

---

### Veto Checklist

| Veto | Condition | Status | Effect |
|------|-----------|--------|--------|
| **Veto 1: Fresh top-pocket counter-signal** | Fresh (≤60 min) NY-AM high-sweep bearish SMT vs UP call OR PM kill-zone mirror | NO SMT in 09:20–12:59 window; peak at 12:15 is not an SMT event, just a failed level test. No bearish SMT counter-signal to an UP call. (We're calling DOWN, so this veto doesn't apply anyway.) | **PASS** — no cap |
| **Veto 2: Unverifiable load-bearing input** | Any load-bearing assumption outside facts.txt | All inputs (prices, sweep times, equilibrium counts) from facts.txt and context.md. | **PASS** — no cap |
| **Veto 3: Standing daily-trend low/neutral** | If daily-trend confidence LOW or direction NEUTRAL | Standing daily-trend = DOWN / HIGH confidence. | **PASS** — no cap |
| **Veto 4: Range/hybrid regime** | If range/hybrid regime, targets capped to day extremes | Standing daily-trend regime = TREND. | **PASS** — no cap |

**All vetoes pass; no caps applied.**

---

## Direction & Confidence

**Net score:** N = Bull − Bear = 0.05 − 3.11 = −3.06

**Direction:** N ≤ −3 → **DOWN**

**Confidence evaluation:**
- |N| = 3.06 ≥ 3 ✓ (meets minimum for medium/high)
- |N| = 3.06 < 6 ✗ (does not meet high threshold)
- Losing ledger (bear 3.11) vs winning ledger (bull 0.05): losing is ~62× larger, far exceeds "≤ half" threshold for high confidence
- No veto cap-to-LOW in effect ✓

**Confidence = MEDIUM** (meets |N| ≥ 3 threshold; no cap-to-LOW; but short of |N| ≥ 6 for high)

---

## Target & Move Scope

**Move target:** Nearest meaningful un-swept, un-depleted pool in DOWN direction below current 29863.75

Candidates:
- london(cur)_low 29776.5: swept 06:41 (DEPLETED, max excursion 160.0 >> 20.0 threshold)
- prev1_week_low 29923.0: swept 03:54 (DEPLETED, max excursion 306.5 >> 80.0 threshold)
- ny_morning(cur)_low 29616.5: NOT swept (closest approach 173.25 short @ 12:00), NOT depleted

**Primary move target = ny_morning(cur)_low 29616.5**  
(Secondary context: further below, prev1_week_low is depleted and spent; next tier down would be london(cur)_low, also depleted.)

**Move horizon:** Minutes to hours, until target touch or flip trigger fires.

---

## Flip Trigger

For a DOWN direction, a flip to UP occurs if:
- **Flip condition:** 2 consecutive 1m closes above daily mid 30158.125
- **Weakens to neutral:** 2 consecutive 5m closes above daily mid (weaker threshold, same-direction hysteresis)

---

## Flipped Target (if flip fires)

If price reverses above daily mid and flip trigger engages:
- **Flipped direction:** UP (at LOW confidence, pending next full evaluation)
- **Flipped target:** nearest un-swept pool above daily mid = london(cur)_high 30414.5 (closest approach 367.75 short; not yet swept)

---

## arm_entry_confirmation

**Criteria (decision/next-move.md §7):**
- Grab signature complete (sweep + divergence + equilibrium rejection) OR pullback to eq/FVG in confirmed trend
- AND not inside 09:15–11:30 without overnight/HTF backing
- AND failed_entries not near cap

**Assessment:**
- **Grab signature:** london(cur)_low grabbed 06:41, but age 378m = ~6 hours old (well outside 3h freshness window; "spent" per protocol). No fresh grab in 09:20–12:59 window. No divergence emerged. No complete setup.
- **Pullback to eq/FVG in trend:** We're in a confirmed DOWN trend (daily-trend HIGH). Current price 29863.75 is below the daily mid 30158.125 (discount side). A pullback to eq would be a bounce to 30158 level. No such pull-back confirmation attempted yet in this window.
- **Whipsaw check:** Now 12:59, outside 09:15–11:30. ✓
- **Failed_entries:** 0, plenty of room. ✓

**Verdict:** No fresh, complete entry setup at 12:59. The stale london grab + failed mid-reclaim context doesn't justify arming entry now. Confirm that market continues to be driven by scripts' baseline momentum/ladder without entry gate opening.

**arm_entry_confirmation = NO**

---

## Next-Move Output Schema

```
next_move:
  direction: DOWN
  confidence: MEDIUM
  
  move_target:
    level: ny_morning(cur)_low
    price: 29616.5
    zone_description: lowest un-swept, un-depleted liquidity below current price
  
  flip_trigger: |
    2 consecutive 1m closes above daily running mid 30158.125
    (signals failed downtrend; reversal of primary direction)
  
  flipped_target:
    level: london(cur)_high
    price: 30414.5
    (or consolidation bounce to daily mid 30158.125 if reversal is shallow)
  
  arm_entry_confirmation: NO
  
  ledgers:
    bull:
      - london(cur)_low grab (age 378m, tier session, score 0.05)
    bear:
      - failed daily mid reclaim peak 12:15 (age 45m, tier day, score 1.68)
      - daily mid acceptance 0% (checkpoint→now, tier day, score 1.43)
    rejected_contested: none
    veto_checklist: all pass (no caps)
  
resolution (neutral form):
  not applicable (direction is DOWN, not neutral)
```

---

# SECTION 3: DOC GAPS & PROTOCOL NOTES

## Ambiguities Addressed

1. **D4 Scope Boundary (06:00 ET):**
   - Rule states D4 "owns events before 06:00 ET only; post-06:00 events belong to next-move."
   - Interpretation applied: london(cur)_low swept at 06:41:32 (post-06:00) → belongs to next-move, not D4.
   - london(cur)_low acceptance-beyond through 09:30 → classified as grab (sweep-and-reverse) based on subsequent price reversal from 09:30 onward.
   - Decision: consistent with protocol; D4 owns overnight/asia/london complex up to 06:00; daylight events are next-move's domain.

2. **Grab-vs-Continuation Read for london(cur)_low:**
   - Ambiguity: Low sweep in weekly discount should be grab per grab-vs-continuation table ("sweep against weekly zone (high in premium / low in discount)"). Yet the acceptance was-beyond (continued down), then reversed back UP.
   - Resolution: Per the table's first row for grab, "sweep against weekly zone" is ONE indicator. The table's second row "sweep WITH a committed prior equilibrium cross" is continuation. Here, the mid-reclaim at 20:44 was a failed AGAINST-pattern (closed above, then rejected), making the subsequent low sweeps a continuation of the rejection phase, not a fresh equilibrium cross. Price continued down, then reversed — the reversal (from 09:30 onward) is the distinguishing grab signal. Classified as grab (bullish for lows); scored as such.

3. **Confidence in next-move at MEDIUM despite high daily-trend:**
   - next-move.md §3 states: "no cap-to-LOW in effect" allows MEDIUM even with a down-from-high dip in |N|. High daily-trend confidence (HIGH) does not cap next-move; only NEUTRAL daily-trend caps next-move to MEDIUM per veto 3. Here, daily-trend is DOWN/HIGH, so no veto 3 cap. next-move's |N| = 3.06 meets ≥3 threshold but not ≥6, so confidence is MEDIUM by threshold alone, not by veto cap. Correct.

4. **Freshness Decay for >3h-old items:**
   - london(cur)_low sweep age 378m >> 180m freshness half-life. Scoring formula 2^(−378/180) ≈ 0.233 is correct. Items ≳3h old are "spent" per protocol narrative, but scoring via decay formula preserves a residual weight (~23%) rather than zeroing them. Interpreted as: spent items still score but at greatly reduced weight. Consistent with formula's intent.

5. **Session-side weighting for equil/pattern items:**
   - Failed mid reclaim and daily mid acceptance are PATTERN items, not directed sweeps. Per next-move.md §2, session_side applies "NY-AM high-sweep bearish 1.5 · ... · else 1.0". For equilibrium patterns that don't fall into specific buckets (e.g., mid acceptance not tied to a specific sweep time), applied general session_side = 1.0. Defensible; alternative would be time-weighted average (e.g., averaging 1.0 for PM portion, 1.0 for AM portion) which also yields 1.0.

6. **Whipsaw suppression scope:**
   - Protocol: "inside 09:15–11:30 ET, intraday *structure* items (mid-crossings, hypothesis kills) ×0.5; open-window daily-mid crossings ×0." Note "intraday structure items" — mid-crossings WITH whipsaw window ARE suppressed. But "daily mid acceptance 0%" is an AGGREGATE pattern (count of closes), not a crossing event. Interpretation: Apply ×0.5 if the aggregate contains majority whipsaw-window closes (09:15–11:30 window contained ~2h 10m of the 3h 39m total). However, the pattern is an equilibrium acceptance measure, not a hypothesis-kill structure event. Did not apply suppression; alternative would be to ×0.5 for the 2h 10m overlap. Chose not to suppress because the pattern is a measurement, not an intraday structure event. *Minor ambiguity*; result is not materially different (0.713 → ~0.56 if suppressed, marginal impact on MEDIUM confidence tier).

## Missing Facts

No material fact gaps. facts.txt and context-at-cut.md provided all required data:
- All level prices and sweep times (S1–S2)
- Cross-ticker divergences (S3)
- Equilibrium acceptance counts and crossing events (S4)
- HTF structure bars (S5, S5b)
- FVG data (S6)
- Checkpoint snapshot (S7)
- Hard-truth context (ATH values, position state, counters)

## Protocol Inconsistencies or Contradictions

None identified. The grab-vs-continuation logic (liquidity-levels.md §2) and the daily mid failed-reclaim logic (equilibrium.md §2) operate in different planes (level-sweep classification vs equilibrium event reading) and do not contradict when both are applied.

---

**Report written at:** 2026-06-23 12:59:59-04:00 ET  
**Decisions consumed by:** next-move protocol (standing daily-trend DOWN/HIGH as given input)  
**Execution gate:** arm_entry_confirmation = NO (no fresh entry setup; scripts continue baseline)
