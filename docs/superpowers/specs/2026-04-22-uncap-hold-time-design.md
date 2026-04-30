# Uncap Hold Time — Design Spec
_Date: 2026-04-22 | Approach: B (trail + partial coupling)_

## Problem

The strategy exits in 1–5 bars after entry even on strong trend days. The `2025-11-14` LONG is the motivating example: SMT divergence entry was well-placed on a 400+ pt directional session; the strategy exited at +37 pts.

The binding constraint is **`TRAIL_AFTER_TP_PTS = 1.0`** — 1 MNQ point. Any 1-tick adverse move after TDO is crossed triggers exit. This is effectively exit-at-TDO with a noise buffer.

Secondary constraint: `SESSION_END = "13:30"` with `SIGNAL_BLACKOUT_END = "13:00"` leaves only 30 minutes of post-blackout trading. Extending the session without fixing the trail first would just create empty session time.

## Philosophy

The TDO/midnight open is a draw-on-liquidity level. Once filled, smart money reverses — which is why exiting there is correct for a 1R trade. For a trend day, the fill of the draw is the beginning of the move, not the end. Trail mode passes through the TDO and lets the market decide when momentum is exhausted, rather than pre-committing to a TP that also signals "turn around."

Weekly H/L as a hard secondary-target exit contradicts this philosophy and is out of scope for this track.

## Approach B: Trail + Partial Coupling

Trail mode and partial exit are logically contradictory — "let the market decide" vs "take money off early." They are disabled together: when `TRAIL_AFTER_TP_PTS > 0`, the partial exit block is unreachable. This lets the optimizer search trail parameters without partial contaminating results on trail-mode trades.

## Design

### 1. Constants (`strategy_smt.py`)

```python
TRAIL_AFTER_TP_PTS  = 50.0    # was 1.0 — optimizer: [25.0, 50.0, 75.0, 100.0]
TRAIL_ACTIVATION_R  = 1.0     # new  — optimizer: [0.0, 0.5, 1.0, 1.5, 2.0]
SESSION_END         = "15:15" # was "13:30"
```

`TRAIL_ACTIVATION_R` is R-multiples of the initial stop distance that price must travel past TDO before the trail takes over. `0.0` = activate immediately (reproduces current behaviour, used as one optimizer cell). R-multiple chosen for scale-invariance, consistent with `BREAKEVEN_TRIGGER_PCT` and `REENTRY_MAX_MOVE_RATIO`.

`SIGNAL_BLACKOUT_END` stays `"13:00"` — no edit needed. Post-blackout window becomes 13:00–15:15 (2h15m).

`MAX_HOLD_BARS = 120` unchanged. At 1-min bars this is a 2-hour per-trade cap; `exit_time` remains the ceiling for late entries.

### 2. Signal dict (`_build_signal_from_bar`)

Add one field to the returned dict:

```python
"initial_stop_pts": abs(entry_price - stop_price),
```

Used by `manage_position` to compute the activation threshold without importing stop-ratio constants. Diagnostic-only; no filter logic reads it.

### 3. `manage_position` — three changes

**Change 1 — TDO crossing: defer stop, mark breach only**

```python
# Before (sets stop immediately):
if crossed:
    position["tp_breached"] = True
    position["best_after_tp"] = max(tp, current_bar["High"])
    position["stop_price"]    = position["best_after_tp"] - TRAIL_AFTER_TP_PTS
    return "hold"

# After (defers stop to activation check):
if crossed:
    position["tp_breached"] = True
    position["best_after_tp"] = max(tp, current_bar["High"])
    return "hold"
```

For shorts, `best_after_tp = min(tp, current_bar["Low"])` on the crossing bar (not `max`).

**Change 2 — Post-TDO bars: activation threshold + never-widen**

```python
# Before (long shown; short uses min/Low/+):
best = max(position.get("best_after_tp", tp), current_bar["High"])
position["best_after_tp"] = best
position["stop_price"]    = best - TRAIL_AFTER_TP_PTS

# After — long:
best = max(position.get("best_after_tp", tp), current_bar["High"])
position["best_after_tp"] = best
activation_dist = TRAIL_ACTIVATION_R * position.get("initial_stop_pts", 0.0)
if best - tp >= activation_dist:               # price far enough past TDO?
    new_stop = best - TRAIL_AFTER_TP_PTS
    position["stop_price"] = max(position["stop_price"], new_stop)  # never widen: only tighten

# After — short (operators inverted):
best = min(position.get("best_after_tp", tp), current_bar["Low"])
position["best_after_tp"] = best
activation_dist = TRAIL_ACTIVATION_R * position.get("initial_stop_pts", 0.0)
if tp - best >= activation_dist:               # price far enough past TDO?
    new_stop = best + TRAIL_AFTER_TP_PTS
    position["stop_price"] = min(position["stop_price"], new_stop)  # never widen: only tighten

# In both cases: if threshold not yet met, original stop holds unchanged
```

The never-widen rule ensures the trail can only tighten the stop, never regress it. Without this, a large trail on a barely-crossed TDO would move the stop from ~3.5 pts below entry to 50 pts below TDO — turning a winning exit into a potential large loss.

**Change 3 — Partial exit: skip when trail is active**

```python
# Before:
if PARTIAL_EXIT_ENABLED and not position.get("partial_done"):

# After:
if PARTIAL_EXIT_ENABLED and not position.get("partial_done") and TRAIL_AFTER_TP_PTS == 0:
```

When `TRAIL_AFTER_TP_PTS > 0` the partial block is unreachable. If trail is disabled in an optimizer cell (`TRAIL_AFTER_TP_PTS = 0`), partial re-activates automatically — no separate flag needed.

### 4. Session window

```python
SESSION_END = "15:15"   # CME RTH close
```

`SIGNAL_BLACKOUT_END` stays `"13:00"`. The 11:00–13:00 blackout is retained — its finding was measured against a 13:30 close and should be re-evaluated from backtest output at 15:15, not pre-emptively removed.

### 5. Optimizer grid

4 × 5 = 20 cells, single run:

| `TRAIL_AFTER_TP_PTS` | `TRAIL_ACTIVATION_R` |
|---|---|
| 25.0, 50.0, 75.0, 100.0 | 0.0, 0.5, 1.0, 1.5, 2.0 |

All other parameters at current baseline values.

### 6. Measurements

Per optimizer cell, collect:
- Hold duration: bars held P25 / P50 / P75 / P90
- Max PnL per trade
- Win rate and expected value vs. baseline
- Exit-type breakdown: `exit_time` / `session_close` / `exit_stop` / `exit_tp` — to diagnose whether trades are dying on the time cap, session close, or the trail

## Risks / Trade-offs

- **Chop exposure**: wider trail means many entries that stall after TDO give back gains before reversing. Expect win rate to drop vs. baseline.
- **Larger open-trade drawdown**: trail 50 pts behind best = up to 50 pts of open adverse excursion on losing sessions. At $2/pt per contract this is $100 per contract of additional risk.
- **Sample size sensitivity**: `SESSION_END` change may alter which days generate signals and which session-close exits occur; full date-range re-run required.
- **Activation threshold at `TRAIL_ACTIVATION_R = 0.0`**: reproduces the current stop-regression bug on barely-crossed TDOs. The never-widen rule still applies, so it's safer than before, but `0.0` is included in the optimizer only to isolate the delay effect — the expectation is it underperforms non-zero values.

## Out of Scope

- Entry logic changes (SMT divergence detection, confirmation bar rules)
- Weekly H/L as a hard secondary-target exit
- Multi-session / overnight holds
- Swing-trade position sizing
- Hypothesis-gated trend mode (`TREND_MODE` flag)
