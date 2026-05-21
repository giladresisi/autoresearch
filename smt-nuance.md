# SMT Divergence Nuances — Reversal vs. Continuation

Reference for updating hypothesis direction logic (rule2b and beyond).

---

## The canonical ICT interpretation

**SMT divergence at an external high is always a reversal signal by default.**
When MES touches week_high / day_high and MNQ fails to confirm, the ICT read is:
smart money engineered MES above the level to collect buy-stop liquidity, then
positioned short. The divergence is the tell. The more significant the level
(week_high > day_high > session high), the stronger the prior.

Rule2b overrides this to say "continuation UP" — that override is only valid
under specific conditions.

---

## Filters that determine reversal vs. continuation

### 1. Higher-timeframe bias (strongest filter)

> *"An SMT print against the higher-timeframe bias is statistically more likely to fail."*

| HTF bias | Bearish SMT at external high | Interpretation |
|---|---|---|
| Strongly UP | Against the trend | Likely noise / liquidity grab before continuation |
| Neutral / ambiguous | No trend context | Default reversal read applies |
| Bearish | Aligned with trend | High-probability reversal |

**This is the dominant filter.** When the weekly trend is clearly bullish and price
has been making HH/HL structure, a bearish SMT at week_high is fighting the trend.
The market probably grabbed the buy-stops to fuel the next leg up, not to reverse.

When the HTF bias is stalled, broken, or bearish, the same SMT is a strong reversal
trigger and rule2b should NOT apply.

---

### 2. Divergence magnitude (gap between instruments)

A *large gap* between MNQ's high and the level MES touched = strong divergence =
reversal. A *near-miss* = weak divergence = timing noise = rule2b (continuation) is
more defensible.

**Practical proxy for MES/MNQ:**
- Did MNQ set a new session high (or week-equivalent high) during the *same push*
  that carried MES into week_high? → Near-miss. MNQ was participating, just lagged
  by a few ticks. Rule2b applies.
- Was MNQ's last swing high from a *prior swing* — meaning MNQ stalled or reversed
  before the push even started? → Large gap. MNQ is structurally diverging, not just
  lagging. Reversal (DOWN) is the stronger read.

No canonical tick-threshold exists in the literature. A reasonable approximation:
if MNQ's session high is within ~0.1–0.15% of its equivalent level, it's a near-miss.
Beyond that it's a structural gap.

---

### 3. Post-divergence structure break (confirmation)

SMT alone is not a trade signal. It becomes high-probability when price
subsequently **breaks short-term structure** in the reversal direction:
- Bearish SMT → price makes a lower low on the 1m/5m after the divergence bar
- Or: price displaces through a nearby FVG / order block on the downside

Without this confirmation the divergence is a pending signal, not an active one.
If price consolidates and prints a higher low after the SMT → the market absorbed
the sell pressure; continuation UP is more likely.

Our strategy already handles this via the confirmation bar / stop-entry mechanism —
the question is just whether we arm a long entry or a short entry.

---

## Revised rule2b logic

Current rule2b fires any time there is a bearish SMT at an external high and
the HTF trend is UP → take UP direction.

It should be split:

| HTF bias | Divergence gap | Direction |
|---|---|---|
| Strongly UP | Near-miss (MNQ in same push, barely short) | UP (rule2b — current) |
| Strongly UP | Large gap (MNQ stalled earlier, didn't participate) | UP still, but weaker — worth tracking |
| Neutral | Near-miss | UP (ambiguous, lean with last structure) |
| Neutral | Large gap | DOWN — classic reversal |
| Bearish | Any gap | DOWN — SMT confirms HTF direction |

---

## Proposed rule2c (strong bearish SMT → reversal)

Trigger: bearish SMT at external high (MES touches, MNQ doesn't)
Condition: HTF bias is NOT strongly bullish AND divergence gap is large
(MNQ's session high is from a prior swing, not the current push)
Direction: DOWN

This would activate *instead of* rule2b when those conditions are met.

The HTF bias check is the actionable first step — it's already partially tracked
via `global.json` (`trend`, `confidence`). The divergence gap check requires
comparing the MNQ high timestamp against the MES high timestamp at the level touch,
which is available from bar data.

---

## Level hierarchy (severity of reversal signal)

| Level touched by MES | Reversal signal strength |
|---|---|
| ATH / all-time high | Strongest — near-ATH is always treated as potential cap |
| week_high | Strong — weekly external liquidity |
| day_high | Moderate |
| session high (same day) | Weaker — may just be intraday noise |

Week_high divergence is already treated specially in the codebase (via `_lv2`).
ATH divergence is handled by the `_ath_secondary` path. Day_high divergence
is the grey zone where rule2b fires most aggressively and is least justified.

---

## Summary of gaps vs. current code

| Nuance | Captured? | Notes |
|---|---|---|
| SMT at external high = reversal by default | Partially | rule2b overrides to UP regardless |
| HTF bias filter | No | `global.json` has `trend` but it's not consulted in hypothesis direction |
| Divergence magnitude (gap size) | No | rule2b fires on any non-touch, near-miss or wide gap alike |
| Post-divergence structure break | Yes | Handled by confirmation bar / stop-entry wait |
| Level significance (week > day) | Partially | `_lv2` distinguishes the level name but not reversal weighting |
