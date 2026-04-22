# Uncap Hold Time — Strategy Extension Notes

## Problem

The current strategy exits in the first 1–5 bars after entry. Structural constraints that cause this:

1. **Session window hard cap** — `SESSION_END = "10:30"` force-closes all positions at 10:30 AM ET,
   giving a maximum hold of ~90 minutes from the open.
2. **Finite TDO target** — `take_profit` is set to the nearest liquidity draw (midnight open, prev-day
   high/low, etc.). Once hit, the trailing stop activates immediately and snaps the exit within 1–2 bars.
3. **Tight trailing stop** — `TRAIL_AFTER_TP_PTS` is small; the trail runs close behind price and
   rarely allows more than 10–20 pts of continuation after TP breach.
4. **Partial exit** — contracts come off at an early level, reinforcing the short-duration framing.

## Observed Opportunity

2025-11-14 LONG: the strategy exited at +37 pts on a session that produced a 400+ pt directional
move visible on the 1h chart (TradingView MNQ1!). The SMT divergence entry was well-placed;
the strategy just had no mechanism to stay in.

## Code Reality Check (as of exploration 2026-04-22)

The plan was written against an earlier baseline. Current constants differ:

| Constraint | Plan assumed | Actual code |
|---|---|---|
| Session close | `SESSION_END = "10:30"` | `SESSION_END = "13:30"` — already extended |
| Trailing stop | "small" | `TRAIL_AFTER_TP_PTS = 1.0` — 1 MNQ point, effectively exit-at-TDO |
| Signal blackout | not mentioned | `SIGNAL_BLACKOUT_START/END = "11:00"/"13:00"` — leaves only 30 min of post-blackout session |
| Draw candidates | described as limited | Already includes `tdo`, `midnight_open`, `session_high/low`, `overnight_high/low`, `pdh/pdl`; only weekly H/L missing |
| Trail vs secondary target | treated as compatible | **Mutually exclusive in code**: trail block is skipped when `secondary_target` is set |

**The binding constraint is `TRAIL_AFTER_TP_PTS = 1.0`**, not the session window. A 1-pt trail on MNQ exits within 1 tick of any post-TDO pullback — this is why trades die in 1–5 bars after the TDO breach regardless of how long the session runs.

## Required Changes to Support Longer Holds

| Constraint | Current setting | Change needed |
|---|---|---|
| Trailing stop | `TRAIL_AFTER_TP_PTS = 1.0` | Widen to 50–100 pts; add "never widen" rule + delayed activation threshold |
| Session close | `SESSION_END = "13:30"` | Extend to `"15:15"` (CME close) or `"16:00"` (full RTH) |
| Signal blackout | ends at `"13:00"` | Adjust end alongside session extension — currently leaves only 30 min of live trading |
| TP target | Nearest liquidity draw | Add weekly high/low as candidate in `select_draw_on_liquidity`; gate at MIN_RR ≥ 3.0 |
| Partial exit level | `PARTIAL_EXIT_LEVEL_RATIO = 0.33` | Either disable for trend mode or push ratio higher so it doesn't exit most of position too early |

## Risks / Trade-offs

- **Chop exposure**: holding 2–4 hours through the mid-morning consolidation means many entries that
  looked like trend starts will reverse before the trend materialises. Expect win rate to drop.
- **Larger intraday drawdowns**: wider trail = larger open-trade drawdown on losing sessions.
- **Sample size sensitivity**: extending session window changes which days produce signals; backtests
  will need a clean re-run on the full date range.
- **Regime dependence**: the large trend on 2025-11-14 was likely news/macro driven. A strategy tuned
  to ride such days will underperform in mean-reverting regimes.

## Suggested Experiment Sequence (reordered 2026-04-22)

Original sequence led with session extension. Reordered to attack the binding constraint first.

1. **Wider trailing stop** — increase `TRAIL_AFTER_TP_PTS` from 1.0 to candidate values (25, 50, 75, 100 pts).
   This is the highest-leverage change: the 1-pt trail is the primary reason holds are short, not the session window.
   Two safeguards added alongside the wider trail:
   - **"Never widen" rule**: `stop = max(current_stop, best_after_tp - trail)` — trail can only tighten, never push the stop below its pre-trail level. One-line change in `manage_position`.
   - **Delayed activation** (`TRAIL_ACTIVATION_R`): trail only takes over after price has traveled `TRAIL_ACTIVATION_R × |entry − stop|` past TDO. Before that, the original stop holds. R-multiple chosen for scale-invariance — consistent with `BREAKEVEN_TRIGGER_PCT`, `REENTRY_MAX_MOVE_RATIO`, and `MIN_RR_FOR_TARGET`. Optimizer search space: [0.5, 1.0, 1.5, 2.0].
   Combined behavior: TDO crossed → original stop holds → activation threshold hit → trail activates (tighten-only).
   Measure: hold duration distribution, max PnL per trade, win rate impact.
2. **Extend session + adjust blackout** — set `SESSION_END = "15:15"` and update `SIGNAL_BLACKOUT_END` in tandem.
   Currently `SIGNAL_BLACKOUT_END = "13:00"` leaves only 30 min of active trading before the 13:30 close.
   Measure: how many new `session_close` exits appear? Does late-session signal quality differ?
3. **Further draw target** — add weekly high/low as a candidate in `select_draw_on_liquidity`. Gate
   it behind a minimum RR threshold (e.g., MIN_RR ≥ 3.0) so it only fires when the level is far enough.
   Note: trail-after-TP and secondary_target are mutually exclusive in `manage_position`; decide which
   mechanism owns "longer hold" before implementing this step.
4. **Disable partial on trend signals** — gate `PARTIAL_EXIT_ENABLED` to hypothesis-confirmed sessions
   only, letting trend sessions run fully. Requires defining what constitutes a "trend session."

## Out of Scope for This Track

- Changing entry logic (SMT divergence detection, confirmation bar rules)
- Multi-session/overnight holds
- Swing-trade position sizing
