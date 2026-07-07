# DECISION: Daily Trend / Bias

> Pure policy — every change to weights/thresholds/cadence is a strategy change: **backtest
> before merging**. All numbers are v1 seeds pending calibration. The
> [next-move decision](next-move.md) consumes this decision's OUTPUT and never re-derives it.
> This decision exists because daily trend is the master conditioner (87% vs 35%, smt.md §2)
> and no script computes it (`daily.py estimated_dir` is a placeholder).

## 1. Cadence

Evaluate ONLY at checkpoints: **06:00** (overnight complete — D4's boundary; first morning
eval) · **~09:20** (AMD leg visible; after daily.py levels) · **13:00** (AM→PM boundary) ·
**18:00** (optional, new True Day).

**Checkpoint data rule:** an evaluation uses ONLY data at/before the checkpoint timestamp;
the standing output between checkpoints IS that evaluation (fresh tape belongs to next-move).
Between checkpoints, re-evaluate early ONLY when a standing invalidation trigger (§4) fires —
and a flip additionally requires the condition to hold **2 consecutive 5m closes**
(hysteresis; GIL-21). Continuous re-evaluation recreates Phase-3 churn.

## 2. Inputs — six slow driver groups, each counted ONCE

Fresh tape (~last 3h) belongs to next-move — never consume it here.

| # | Driver | Wt | Assess |
|---|---|---|---|
| D1 | HTF structure | 3 | **The 4hr chain SETS the vote's sign** (0.65 share); 1hr only modulates: agrees → full contribution, disagrees → contribution HALVED; vote 0 only if the 4hr chain itself is mixed/absent. Midnight-anchored `resample("4h")`/`("1h")` (engine convention) |
| D2 | Weekly equilibrium acceptance | 2 | which side of the weekly mid closes are ACCEPTED on (majority over recent hours) + failed reclaims. **Sign = DELIVERY: vote WITH the accepted side's travel** (accepted below = down). The discount-buys/premium-sells prior belongs to sweep classification (D4/next-move), not here. Week anchor = ENGINE convention (equilibrium.md §1) |
| D3 | Prior-day / week context | 1 | where yesterday closed in its range; expansion vs balance week |
| D4 | Overnight sweep complex | 2 | Asia/London manipulation legs as ONE unit: pools swept then accepted-beyond (continuation) vs rejected-back (grab). **Owns events before 06:00 ET only**; post-06:00 action may only CLASSIFY those sweeps — post-06:00 events belong to next-move |
| D5 | Standing SMT / RS residue | 1 | standing unfulfilled week/day-tier divergences; persistent (>1 day) cross-ticker RS regimes |
| D6 | ATH / recovery regime | 1 | vs scripts' `all_time_high`: ATH-expansion week = up-by-construction; >2–3% below = recovery (up-drift with violent shakeouts). If ATH not provided as hard truth: SKIP + confidence cap (§3) |

**Correlation audit (mandatory):** list each driver's underlying price events. An event
supports ONE driver at full weight; a second appearance ×0.5, further ×0 — applied to the
driver's **weighted contribution**. This holds **regardless of form**: an aggregate/pattern
metric (e.g. an acceptance percentage) built on the same underlying price action as another
driver's event IS a second appearance — there is no pattern-vs-event exemption. A driver
voting **0 consumes nothing**. If unshared evidence alone justifies a vote, no discount — but
say so in the table. (Run-1 failure: one overnight complex counted as five "families".)

## 3. Scoring, confidence

Votes are **−1 / 0 / +1 — integers only** (the sole sanctioned fraction is D1's halved
*contribution*; "half a vote" feelings vote 0). S = Σ(weight × vote) ∈ [−10, +10].
**Direction:** up if S ≥ +3, down if S ≤ −3, else **neutral** (first-class: baseline
mean-reversion pays — GIL-18; never force a call).

| Confidence | Requires |
|---|---|
| high | \|S\| ≥ 6 · no weight-≥2 driver opposes · all load-bearing inputs verifiable |
| medium | \|S\| ≥ 3 · at most one weight-≥2 opposer |
| low | everything else; an unverifiable load-bearing input always costs one tier |

## 4. Output schema (standing until next checkpoint)

```
daily_trend:
  direction: up | down | neutral
  confidence: high | medium | low
  regime: trend | range | hybrid
  day_dol: <level + price/zone>            # only if direction != neutral
  weakens_to_neutral_if: <close-based condition>
  flips_if: <close-based condition; 2x5m-close hysteresis>
  drivers: <D1..D6 audit table: vote, weight, contribution, events, sharing>
```

## 5. Regime overlay

| Tell | Trend | Range/chop |
|---|---|---|
| overnight range | one-sided expansion | narrow; both extremes swept & rejected |
| equilibrium | one-sided acceptance; failed reclaims | repeated crossings both ways |
| 1hr/4hr BOS | aligned chain | mixed/absent |
| pools | sequential same-side sweeps that DEPLETE | sweeps reversing at each extreme |

Range/hybrid ⇒ expect mean-reversion between extremes; next-move targets confined (veto 4)
and the entry gate defaults to "scripts unchanged".

## 6. Failure modes these rules encode

One-event-many-votes (run 1) → §2 audit · churn (Phase-3 ~54 reforms/day) → cadence +
hysteresis · trailing-edge anchoring (GIL-16) → D4 reads pools, not running extremes ·
SMT-derived bias (Phase-3 −$3,756) → D5 is one driver of six · front-loaded certainty (recall
caps ~18–23% at the open) → 06:00/09:20/13:00 checkpoints + living invalidations.
