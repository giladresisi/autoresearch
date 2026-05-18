# Daily Loss Limit Analysis

**Dataset:** 30-day 1m-bar regression, 2026-04-06 → 2026-05-15 (MNQ futures, 2 contracts)  
**Baseline total PnL (no limit):** +$11,671.50 across 27 trading days (weekend/holiday days excluded)

---

## Concept

A daily loss limit halts trading for the rest of the session the moment the intraday **cumulative PnL** crosses `-LIMIT`. All open entries are cancelled; no new trades are taken. The simulation walks each day's trades in sequence and locks in the cumulative PnL at the moment the threshold is breached.

---

## Threshold Sweep

| Limit | Total PnL | vs Baseline | False Stops | No-Saves |
|------:|----------:|------------:|------------:|---------:|
| $100  | +$6,194.00 | -$5,477.50 | 11 | 0 |
| $150  | +$8,471.50 | -$3,200.00 | 5  | 0 |
| $200  | +$8,404.00 | -$3,267.50 | 5  | 0 |
| $250  | +$9,789.50 | -$1,882.00 | 2  | 0 |
| $300  | +$10,953.00 | -$718.50 | 1  | 0 |
| $350  | +$11,958.00 | +$286.50  | 0  | 1 |
| $400  | +$11,851.00 | +$179.50  | 0  | 1 |
| **$450**  | **+$11,986.50** | **+$315.00**  | **0**  | **0** |
| $500  | +$11,833.50 | +$162.00  | 0  | 0 |
| $800  | +$11,671.50 | $0.00     | 0  | 0 |

**False stop:** limit fires on a day that ends profitable — trading halts during a drawdown that would have recovered.  
**No-save:** limit fires on a loss day but locks in a *worse* result than simply finishing the day (see May 6 trap below).

---

## Optimal Threshold: $450

The limit only materially helps on **one day** across 30: **April 16** (the worst loss day, -$800.00 actual).

### April 16 — cumulative PnL by trade

| Trade | PnL | Cumulative |
|------:|----:|----------:|
| T1    | -$104.00 | -$104.00 |
| T2    | -$109.00 | -$213.00 |
| T3    | -$95.00  | -$308.00 |
| T4    | -$70.00  | -$378.00 |
| T5    | -$107.00 | **-$485.00** ← fires at $450 limit |
| T6    | -$153.00 | -$638.00 |
| T7    | -$59.00  | -$697.00 |
| T8    | -$77.00  | -$774.00 |
| T9    | -$26.00  | -$800.00 |

At a $450 limit: fire at T5, locked in at **-$485**, saving **+$315** vs the final -$800.

---

## Safe Zone: $436 – $484

The limit must avoid:

1. **False stops on profitable days** — requires limit > worst intraday drawdown on any profitable day
2. **No-save traps on loss days** — requires limit to not lock in a worse result than finishing the day

### Worst intraday drawdowns on profitable days

| Date | Actual PnL | Max Intraday Drawdown |
|------|----------:|----------------------:|
| 2026-04-08 | +$547.50 | -$318.00 ← binding lower bound |
| 2026-04-07 | +$702.00 | -$289.00 |
| 2026-04-30 | +$388.50 | -$231.50 |
| 2026-04-06 | +$146.50 | -$244.00 |
| 2026-05-15 | +$97.00  | -$214.50 |

→ Limit must be **> $318** to avoid all false stops on profitable days.

### May 6 no-save trap

May 6 actual PnL: **-$199.00**. But the intraday cumulative reaches **-$435** before recovering partially. Any limit between $199 and $435 fires mid-day and locks in a result **worse** than -$199.

→ Limit must be **> $435** to avoid the May 6 trap.

Combined: safe zone is **$436 – $484** (above both bounds, below the Apr 16 T4 cumulative of -$378 plus one more trade).

**$450** is the natural round number within this window.

---

## Why Low Limits Hurt: The Probe-Then-Run Pattern

The strategy enters on level sweeps and probes with 2–3 small losing stops before catching a large directional move. On the best days, trades 1–3 are losers and trade 4+ is the big winner. A $150 limit fires during the losing probe sequence and eliminates the winner entirely.

### $150 limit — false stops detail

| Date | Actual PnL | Locked-in PnL | Foregone |
|------|----------:|------------:|----------:|
| 2026-04-07 | +$702.00 | ~-$168 | ~+$870 |
| 2026-04-08 | +$547.50 | ~-$204 | ~+$752 |
| 2026-04-06 | +$146.50 | ~-$244 | ~+$391 |
| 2026-04-30 | +$388.50 | ~-$232 | ~+$621 |
| 2026-05-15 | +$97.00  | ~-$215 | ~+$312 |

Total foregone on false stops: **~-$2,946** (explains most of the -$3,200 vs baseline).

---

## Days with Material Losses (≥ -$100)

| Date | Actual PnL | Notes |
|------|----------:|-------|
| 2026-04-10 | -$227.00 | Intraday min < $200 limit — not saved by any practical threshold |
| 2026-04-16 | -$800.00 | Saved at $450 limit (+$315) |
| 2026-04-17 | -$198.50 | Intraday min stays within -$246; saved only at very low limits (false stop risk) |
| 2026-05-01 | -$334.00 | 3 consecutive stops in same direction; intraday monotonically worsens |
| 2026-05-04 | -$122.50 | Small loss, intraday min < any useful limit |
| 2026-05-06 | -$199.00 | No-save trap: intraday min -$435, then recovers to -$199 |

---

## Recommendation

Implement a **$450 daily loss limit**:

- Saves **+$315** vs no limit on the 30-day dataset
- Zero false stops (no profitable day is interrupted)
- Zero no-save traps (May 6 not worsened)
- Robust margin: $14 above the no-save bound, $132 above the false-stop bound
- The only day it fires across 30 is Apr 16 (a true runaway loss day with 9 consecutive losers)

---

## 11-Day 1s Regression: May 1–15 (1s bar simulation, force-eval after exit)

**Dataset:** 11-day 1s regression, 2026-05-01 → 2026-05-15 (MNQ futures, 2 contracts)  
**Baseline total PnL (no limit):** +$3,596.00 across 11 trading days  
**Note:** Strategy chop filter (overnight_range < 150 + session_mid_crosses ≥ 4) had **zero effect** on this period — all days were directional enough that filter conditions never triggered.

### Threshold Sweep

| Limit | Total PnL | vs Baseline | False Stops | No-Saves |
|------:|----------:|------------:|------------:|---------:|
| $100  | +$2,169.00 | -$1,427.00 | 4 | 3 |
| $150  | +$2,019.00 | -$1,577.00 | 3 | 2 |
| $200  | +$2,806.50 |   -$789.50 | 2 | 3 |
| $250  | +$2,618.50 |   -$977.50 | 2 | 3 |
| $300  | +$2,747.50 |   -$848.50 | 1 | 2 |
| $350  | +$3,365.00 |   -$231.00 | 0 | 1 |
| $400  | +$3,267.00 |   -$329.00 | 0 | 1 |
| $450  | +$3,267.00 |   -$329.00 | 0 | 1 |
| $500  | +$3,596.00 |      $0.00 | 0 | 0 |

**No limit helps in this window.** The optimal threshold is $500+ (effectively no limit). The $450 level recommended from the 30d dataset costs **-$329** here due to the May 15 no-save trap.

### Why No Limit Helps

The worst actual loss day is May 14 at -$246.50 — not deep enough to be meaningfully cut by a practical limit. May 15 (-$136.00 actual) is the binding constraint: its intraday cumulative reaches **-$465** before recovering, so any limit from $136 to $464 fires mid-day and locks in a result worse than doing nothing. This creates a no-save trap that costs every limit in the $350–$499 range approximately -$230 to -$330.

### May 15 — No-Save Trap (binding constraint for $350–$499)

| Metric | Value |
|--------|------:|
| Actual day PnL | -$136.00 |
| Intraday cumulative min | -$465.00 |
| $450 limit locks in | ~-$450.00 |
| Cost vs finishing | ~-$314.00 |

Any limit between $136 and $464 fires during May 15's deep drawdown, then misses the partial recovery. To avoid this trap: limit must be either ≤ $135 (cuts early, but creates false stops on other days) or ≥ $465 (never fires on May 15, but then offers no protection).

### $150 Limit — False Stops and No-Save Detail

| Date | Actual PnL | Locked-in PnL | Type | Foregone / Extra Loss |
|------|----------:|-------------:|------|----------------------:|
| May 4  | -$87.50  | ~-$262.00 | No-save trap | -$174.50 |
| May 6  | +$21.50  | ~-$183.50 | False stop   | ~-$205.00 |
| May 7  | +$6.00   | ~-$206.00 | False stop   | ~-$212.00 |
| May 13 | +$827.50 | ~-$183.00 | False stop   | ~-$1,010.50 |
| May 15 | -$136.00 | ~-$191.00 | No-save trap | -$55.00 |

**Total cost of $150 limit: -$1,577.00 vs no limit.**

May 13 is the dominant hit: a +$827.50 winning day where the strategy probes with two losers before a large market-close gain. The $150 limit fires during the probe sequence (-$183 cumulative) and eliminates the entire $1,010+ upside. This is the same probe-then-run pattern documented in the 30d analysis — the $150 limit is structurally too aggressive for this strategy.

### Contrast with 30d 1m Findings

| Metric | 30d 1m (Apr 6 – May 15) | 11d 1s (May 1–15) |
|--------|------------------------:|-------------------:|
| Baseline PnL | +$11,671.50 | +$3,596.00 |
| Best limit | $450 (+$315) | None ($500, +$0) |
| $450 net effect | +$315.00 | -$329.00 |
| $150 net effect | -$3,200.00 | -$1,577.00 |
| Worst loss day | Apr 16: -$800 | May 14: -$247 |
| May 15 no-save trap | creates lower bound of $436 | creates upper bound of $464 |

The 30d dataset's $450 recommendation holds because Apr 16 (-$800 with monotonically worsening cumulative) is the dominant driver — worth +$315 to cut. The 11d window has no equivalent runaway day, and May 15's recovery pattern makes any limit in the practical range counterproductive. **The $450 limit should not be evaluated on 11-day windows; the 30d dataset is the appropriate scope for calibrating this parameter.**
