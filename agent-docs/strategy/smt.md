# SMT Divergences — Concepts & Measured Facts

> Concept doc: definitions + measured facts only. Aggregation/weighting lives in
> `decisions/`. Any change to this file is a strategy change — **backtest before merging to
> master** (run baseline-vs-changed docs on trend-up, selloff, chop, deep-V days; attach P&L
> deltas to the PR). Keep negative results as prominent as positive ones.

**Summary:** an SMT is an MNQ/MES divergence at a shared liquidity level — one ticker sweeps,
the other fails. It hints at manipulation/reversal, but **alone it is ~coin-flip** (54%
direction accuracy; 9% of fires precede a big trend). Its value comes from conditioning:
daily trend (87% with-trend vs 35% counter), session×side, tier, clustering, freshness.

## 1. Definitions

**Variants** (`smt_detect.py`, pure engine; persisted via `smts.json`): **wick** (wick exceeds
level, other doesn't — strongest, supersedes body) · **body/"hidden"** (close-based on
15min/30min bars) · **fill** (one ticker reaches into an FVG zone, other doesn't; direction
from FVG polarity). Swept HIGH + divergence → bearish; swept LOW + divergence → bullish. Each
fire carries level name, direction, lead ticker, type, tier, fulfillment status.

**Level universe** (names decide class via `_level_class`):

| Class | Levels | Tier | Re-arm |
|---|---|---|---|
| dynamic | `day_high/low`, `week_high/low` (running) | day / week | re-arms after departure |
| fixed | `prev1/2_day_*`, `prev1_week_*` (recency gate: day age ≤2, week ≤1) | day / week | single-fire per session |
| fixed | prior 6hr sub-sessions `asia/london/ny_morning/ny_evening_*` (eligible after window closes) | session | single-fire |
| fixed | `TDO`/`TWO`, daily/weekly mid (equilibrium.md) | session | — |

GIL-26 FVG per-ticker edge levels (`liquidities_fvg_edges`) are **NOT in master** (side
worktree only) — never assume they exist at runtime.

**Lifecycle thresholds** (MNQ pts, week/day/session; MES 12/6/3-scale exact tables):

| Knob | Values | From | Meaning |
|---|---|---|---|
| FULFILL | 80/40/20 | fire close, with | thesis played out (**NOT a prediction flag**) |
| INVALIDATE | 40/40/10 | fire close, against | adverse run kills thesis |
| DEPLETE | 80/40/20 | the level | (level,ticker) retired in `__level_inv__` (GIL-25 latch); pair skipped if EITHER retired — the flood-stopper (06-12: 1s −$304→+$881) |
| DEPART | 80/40/20 | the level | fixed level re-arm eligibility (only dynamic levels actually re-arm; a fixed re-arm attempt multi-fired and was reverted) |

**Standing conviction** (`smt_conviction.py`, GIL-32): signed score in [−1,+1]; residual 180min
after fulfillment, 5min birth grace, 2-close sustain; tier weights ATH/week 3, day 2, fill 1.5,
session 1. `|conv| ≥ 0.5` = actionable. **Hazard:** the smoothing retains STALE SMTs — GIL-38
fired an exit hours early off a residual; fresh divergences outrank standing ones.

## 2. Measured base rates & conditioners

Sources: 33-day/806-fire predictiveness study (May 1–Jun 16 2026) + GIL-24 16-day study.

- Base: 9% of fires precede a big trend; 15% fire wrong-way; raw direction 54%.
- **No structural feature of the fire separates good from bad** — filtering is a dead end
  (GIL-17 −8pp; GIL-24 found no separator). Conditioning works:

| Conditioner | Effect |
|---|---|
| **Daily trend (master key)** | with-trend 87% vs counter-trend 35% correct; recall 64% vs 38% placebo → weight trend-conditionally, never filter |
| Sweep side | high-sweeps (bearish) ≫ low-sweeps; bullish/low-sweep inverts 22% overall, **44% in NY-AM** |
| Session | NY-AM = best bearish/high-sweep pocket AND worst bullish inversions |
| Tier | week-tier carries most signal |
| Clustering | 3–5 fires at one zone = signal; lone fire ≈ noise |
| Freshness | fresh beats standing (GIL-38) |

**Reversal-vs-continuation trap (GIL-19):** "price moved against → drop/flip" logic is right
on reversal days (deep-V +13pp, selloff +15pp) and wrong on continuation days (trend-up
−13pp) — estimate the regime before trusting SMT-direction updates.

## 3. How the engine consumes SMTs (code map)

- Direction rules, first to commit wins (`hypothesis.py`): rule1 fresh sweep → rule2b last
  sweep + daily-mid (the workhorse; equilibrium.md) → rule2 approaching-level → rule3_4 blend
  `0.65*(0.55*pd + 0.45*bos) + 0.35*smt` (threshold 0.35) → rule5 `global.trend` fallback.
- **Conviction override** (GIL-32 PR #85 + GIL-33 PR #86): standing `|conv| ≥ 0.5`
  contradicting the rule flips rule2b/rule2/rule3_4 to the SMT side (rule1 fires 0× — left
  out). Validated 05-08 +$720, zero collateral.
- **Reversal lock** (GIL-32 1b): a reversal hypothesis on a swept level blocks the opposite
  direction on that liquidity until the SMT is accepted/fulfilled (rule2b-scoped; a fresh
  strong override outranks the lock).
- Legacy `smt_score` is ~inert to direction (0 sign-flips/198, PR #73) — not a live driver.
- `SUPPRESS_WEEKLY_MID_TREND_BROKEN` (trend.py) exists, default OFF.
- **`daily.py estimated_dir` is a placeholder** — no script computes a real daily bias; that
  is the decision agent's job (decisions/daily-trend.md).

## 4. What shipped / what failed

**Shipped:** GIL-25 depletion latch + single-fire fixed levels · PR #73 V2 unification ·
GIL-32/33 conviction override · GIL-32 1b reversal lock · GIL-39 B weekly-mid TB suppression
(flag OFF, +$83). **NOT merged:** GIL-26 FVG edges.

**Failed (anti-patterns — as load-bearing as the wins):**

| Experiment | Verdict | Lesson |
|---|---|---|
| Phase-3 SMT-dominant direction | −$3,756/5d | direction often RIGHT, but ~54 reforms/day churned entry state; chop bled worst (05-18 −$3,348). Direction accuracy ≠ P&L |
| GIL-21 2-bar flip hysteresis | saves trend/selloff only | damping helps only where churn is the failure |
| GIL-22 re-entry-veto counter reset | worsens chop | the misses were direction failures, not the cap |
| GIL-16 flip-fade anchor | +$432/16d, not shippable | trailing-edge `last_liquidity` anchor is bad |
| GIL-17 sweep/momentum gate | −8pp | structural filtering doesn't work |
| GIL-18 regime entry filter | CANCELED | **chop is the MOST profitable regime (+$36/trade) — never gate entries by regime** |
| GIL-19 relevance rules | trend-up −13pp | kills correct continuation longs on pullbacks |
| GIL-31 displacement fe==2 entry | −$4,900/36d | can't tell reversal-poke from trend-start poke |
| GIL-38 day-extreme opposite-SMT exit | −$273.50 | stale residual fired hours early, cascaded |
| GIL-41 with-trend chase | −$1,294.50/4d | chases exhaustion; perturbation cascade |
| Old-era invalidation exits | −$2.5–2.8k | exits armed too close to entry cut every winner |

Cross-cutting: SMTs are context, not commands · small early perturbations cascade — judge on
whole-day P&L · chop pays the bills · fresh beats standing.

## 5. Decisions

All aggregation/weighting: `decisions/daily-trend.md` (HTF, checkpoints) and
`decisions/next-move.md` (per-call).
