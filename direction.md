# Direction Logic for `run_hypothesis` — Design Document

## Purpose

This document specifies the algorithm for computing the `direction` field written to
`hypothesis.json`. Currently direction is hardcoded to `"up"` (Step 6 of `run_hypothesis`).
This document replaces that line with a full decision procedure grounded in ICT principles.

---

## Scope

**Implemented in this stage:**
- Rule 1 — Fresh meaningful liquidity touch (reversal)
- Rule 2 — Approaching unvisited meaningful liquidity (continuation)
- Rule 3 — Multi-timeframe premium/discount + BOS/CHoCH
- Rule 4 — SMT divergence (elevated role: co-evaluated with Rule 1; standalone scoring otherwise)
- Rule 5 — Global trend fallback
- Observability: direction reason fields in `new-hypothesis` events and plot hover

**Explicitly deferred — do not implement in this stage:**
- Order blocks / breaker blocks
- Kill zone / time macro gating
- Power of 3 / MMXM manipulation phase arc
- Inverse FVG (IFVG) polarity flips

---

## Architecture: Cascading Precedence

Rules are evaluated in order. High-priority rules return decisively if their conditions are
met. Lower-priority rules feed a shared scoring layer. If no rule clears its threshold the
global trend is the fallback.

```
Rule 1  (decisive, conf ≈ 1.0)   fresh liquidity touch + SMT co-eval ──┐
Rule 2  (decisive, conf = 0.75)   approaching unvisited liquidity    ──┤  return immediately
Rule 3+4 scoring layer            PD multi-TF + BOS/CHoCH + SMT     ──┤  if |score| ≥ threshold
Rule 2  (lower-confidence retry)                                     ──┤
Rule 5  (fallback)                global.json trend                  ──┘
```

At every exit point the function also populates a `direction_reason` dict (see Observability
section) that is attached to the `new-hypothesis` event.

---

## New Constants (add near top of `hypothesis.py` with existing CAUTIOUS_* constants)

```python
LIQUIDITY_APPROACH_DIST    = 100   # pts — Rule 2: "nearly approaching" radius
NEAR_EXTREME_DIST          =  75   # pts — Rule 3a: proximity boost to daily extreme
MOMENTUM_BARS              =   5   # 1m bars — Rule 2: recent momentum window
BOS_SWING_N                =   2   # bars each side for swing high/low detection
BOS_LOOKBACK_1HR           =   8   # 1hr bars — recency window for BOS/CHoCH
BOS_LOOKBACK_4HR           =   3   # 4hr bars — recency window for BOS/CHoCH
DIRECTION_SCORE_THRESHOLD  =  0.35 # combined Rule 3+4 score required to commit
```

---

## Meaningful Levels

Used by Rules 1 and 2. Listed in descending priority (lower number = higher priority).

| Level | Kind | Priority |
|---|---|---|
| `week_high` | high | 1 |
| `week_low`  | low  | 1 |
| `day_high`  | high | 2 |
| `day_low`   | low  | 2 |
| Far edge of 1hr bullish FVG (= FVG bottom) | low  | 3 |
| Far edge of 1hr bearish FVG (= FVG top)    | high | 3 |

**"Far edge" definition:**  
- Bullish FVG: three-candle imbalance where `candle[i-1].High < candle[i+1].Low`.  
  The gap spans `[candle[i-1].High, candle[i+1].Low]`. The far edge (for a downward approach)
  is the bottom = `candle[i-1].High`.  
- Bearish FVG: `candle[i-1].Low > candle[i+1].High`.  
  The far edge (for an upward approach) is the top = `candle[i-1].Low`.

Only FVGs detected within the last 5 completed 1hr bars are relevant.

### Helper: `_detect_fvg_1hr(hist_mnq_1m, session_mnq_1m)`

```
combined = concat(hist_mnq_1m, session_mnq_1m).drop_duplicates()
bars_1hr = combined.resample("1h").agg(Open=first, High=max, Low=min, Close=last).dropna()
# Keep only the last N+5 bars for efficiency
bars_1hr = bars_1hr.iloc[-(BOS_LOOKBACK_1HR + 5):]

fvgs = []
FOR i IN range(1, len(bars_1hr) - 1):
    hi_prev, lo_prev = bars_1hr.High[i-1], bars_1hr.Low[i-1]
    hi_next, lo_next = bars_1hr.High[i+1], bars_1hr.Low[i+1]
    IF lo_next > hi_prev:          # bullish FVG
        fvgs.append({kind:"bullish", bottom:hi_prev, top:lo_next, bar_time:bars_1hr.index[i], bar_pos:i})
    ELIF hi_next < lo_prev:        # bearish FVG
        fvgs.append({kind:"bearish", bottom:hi_next, top:lo_prev, bar_time:bars_1hr.index[i], bar_pos:i})

# Keep only FVGs from the last 5 completed 1hr bar positions (bar-count based, NOT time-based).
# A time-based cutoff (e.g. index[-1] - 5h) incorrectly excludes FVGs formed just before a
# weekend or overnight gap — e.g. a Friday 16:00 FVG is only 2 bars old by Sunday 18:00.
min_pos = len(bars_1hr) - 1 - 5
RETURN [f for f in fvgs if f.bar_pos >= min_pos]
```

### Helper: `_build_meaningful_levels(liquidities, fvg_1hr)`

```
levels = []
for liq in liquidities:
    if liq.name == "week_high": levels.append({price:liq.price, kind:"high", name:"week_high", priority:1})
    if liq.name == "week_low":  levels.append({price:liq.price, kind:"low",  name:"week_low",  priority:1})
    if liq.name == "day_high":  levels.append({price:liq.price, kind:"high", name:"day_high",  priority:2})
    if liq.name == "day_low":   levels.append({price:liq.price, kind:"low",  name:"day_low",   priority:2})
for fvg in fvg_1hr:
    if fvg.kind == "bullish":
        levels.append({price:fvg.bottom, kind:"low",  name:"fvg_1hr_bull_bottom", priority:3})
    else:
        levels.append({price:fvg.top,    kind:"high", name:"fvg_1hr_bear_top",    priority:3})
RETURN sorted(levels, key=lambda l: l.priority)
```

### Helper: `_was_previously_touched(level, prior_session_bars)`

```
FOR bar IN prior_session_bars:
    IF level.kind == "high" AND bar.High >= level.price: RETURN True
    IF level.kind == "low"  AND bar.Low  <= level.price: RETURN True
RETURN False
```

`prior_session_bars` = all session bars strictly before the current bar (`mnq_1m.iloc[:-1]`
when `mnq_1m` ends at the current bar).

---

## Rule 1: Fresh Meaningful Liquidity Touch

**Rationale (ICT):** Sweeping a fresh external range liquidity level completes the
manipulation phase of the market maker cycle. Price is expected to reverse.

### `_check_fresh_touch(current_bar, prior_session_bars, levels)`

```
FOR level IN levels (already sorted by priority, lowest number first):
    touched_now =
        current_bar.high >= level.price   IF level.kind == "high"
        current_bar.low  <= level.price   IF level.kind == "low"
    IF NOT touched_now: CONTINUE

    IF _was_previously_touched(level, prior_session_bars): CONTINUE

    direction = "down" IF level.kind == "high" ELSE "up"
    RETURN {direction, touched_level:level, base_conf:1.0}

RETURN None
```

### SMT co-evaluation

Immediately after Rule 1 fires, check whether the SMT score (computed in Rule 4's helper,
see below) aligns with the direction. This co-occurrence is the MMXM "smart money reversal"
fingerprint — the strongest possible signal.

```
FUNCTION _co_evaluate_with_smt(direction, base_conf, smt_score):
    smt_sign = +1 if direction == "up" else -1
    aligned  = (smt_sign * smt_score) > 0

    IF aligned AND abs(smt_score) >= 0.30:
        conf = min(1.0, base_conf * 1.20)   # SMT confirms — maximum confidence
        smt_alignment = "confirmed"
    ELIF NOT aligned AND abs(smt_score) >= 0.60:
        conf = base_conf * 0.80             # strong contradicting SMT — stay cautious
        smt_alignment = "contradicted"
    ELSE:
        conf = base_conf
        smt_alignment = "neutral"

    RETURN conf, smt_alignment
```

---

## Rule 2: Approaching Unvisited Meaningful Liquidity

**Rationale (ICT):** When trending toward a fresh external level, the expected move is to
complete the journey to that level. The hypothesis here is continuation. On a subsequent 5m
call, once the level is actually swept, Rule 1 takes over and flips direction. This
sequential handoff is by design.

### `_check_approaching(current_bar, prior_session_bars, levels, mnq_1m)`

```
FOR level IN levels:
    IF _was_previously_touched(level, prior_session_bars): CONTINUE

    dist = abs(level.price - current_bar.close)
    IF dist > LIQUIDITY_APPROACH_DIST: CONTINUE

    approaching =
        current_bar.close < level.price   IF level.kind == "high"
        current_bar.close > level.price   IF level.kind == "low"
    IF NOT approaching: CONTINUE

    recent = mnq_1m["Close"].iloc[-MOMENTUM_BARS:]
    momentum_ok =
        recent.iloc[-1] > recent.iloc[0]   IF level.kind == "high"
        recent.iloc[-1] < recent.iloc[0]   IF level.kind == "low"
    IF NOT momentum_ok: CONTINUE

    direction = "up" IF level.kind == "high" ELSE "down"
    RETURN {direction, approaching_level:level, dist:dist, conf:0.75}

RETURN None
```

> **Refinement note (deferred):** Rule 2's confidence could be elevated when the SMT score
> aligns with the approaching direction — i.e. SMT divergence is already forming at the level
> being approached. This would mirror the co-evaluation in Rule 1 and make the Rule 2 cascade
> path equally robust. Implementation: call `_co_evaluate_with_smt(direction, 0.75, smt_sc)`
> inside `_check_approaching` before returning, and promote the result to `conf = 1.0` if
> `smt_alignment == "confirmed"`. See Deferred section.

---

## Rule 3: Multi-Timeframe Premium/Discount + BOS/CHoCH

This rule produces a signed score in `[-1.0, 1.0]`. Positive = up/buy bias.
It does not return a direction on its own — it feeds the combined scoring layer.

### 3a: Multi-Timeframe Premium/Discount Score

```
week_mid = (week_high + week_low) / 2
day_mid  = (day_high  + day_low)  / 2

weekly_premium = current_close > week_mid
daily_premium  = current_close > day_mid

pd_score =
    -0.70  IF     weekly_premium AND     daily_premium    (both premium → sell)
    +0.70  IF NOT weekly_premium AND NOT daily_premium    (both discount → buy)
    +0.30  IF     weekly_premium AND NOT daily_premium    (discount within weekly premium → weak buy)
    -0.30  IF NOT weekly_premium AND     daily_premium    (premium within weekly discount → weak sell)

# Proximity modifier: close to a daily extreme sharpens the signal
IF (day_high - current_close) < NEAR_EXTREME_DIST: pd_score -= 0.15
IF (current_close - day_low)  < NEAR_EXTREME_DIST: pd_score += 0.15

pd_score = clamp(pd_score, -1.0, 1.0)
```

Record `weekly_zone = "premium" | "discount"` and `daily_zone = "premium" | "discount"` for
the direction_reason payload.

### 3b: BOS/CHoCH Score

```
FUNCTION _compute_bos_choch_score(bars_df, swing_n, lookback):
    # Swing high at index i: bars_df.High[i] is the max over the (2*swing_n+1)-bar window
    n   = len(bars_df)
    shs = [i for i in range(swing_n, n - swing_n)
           if bars_df["High"].iloc[i] == bars_df["High"].iloc[i-swing_n : i+swing_n+1].max()]
    sls = [i for i in range(swing_n, n - swing_n)
           if bars_df["Low"].iloc[i]  == bars_df["Low"].iloc[i-swing_n  : i+swing_n+1].min()]

    last_idx = n - 1
    current_close = bars_df["Close"].iloc[-1]
    score = 0.0

    recent_shs = [i for i in shs if last_idx - i <= lookback]
    IF recent_shs:
        latest_sh       = max(recent_shs)
        latest_sh_price = bars_df["High"].iloc[latest_sh]
        IF current_close > latest_sh_price:
            recency = 1.0 - (last_idx - latest_sh) / lookback
            score  += recency              # bullish BOS

    recent_sls = [i for i in sls if last_idx - i <= lookback]
    IF recent_sls:
        latest_sl       = max(recent_sls)
        latest_sl_price = bars_df["Low"].iloc[latest_sl]
        IF current_close < latest_sl_price:
            recency = 1.0 - (last_idx - latest_sl) / lookback
            score  -= recency              # bearish BOS / CHoCH

    RETURN clamp(score, -1.0, 1.0)
```

```
combined_1m = concat(hist_mnq_1m, session_mnq_1m).drop_duplicates().sort_index()
mnq_1hr = combined_1m.resample("1h").agg(...).dropna()
mnq_4hr = combined_1m.resample("4h").agg(...).dropna()

bos_1hr = _compute_bos_choch_score(mnq_1hr, BOS_SWING_N, BOS_LOOKBACK_1HR)
bos_4hr = _compute_bos_choch_score(mnq_4hr, BOS_SWING_N, BOS_LOOKBACK_4HR)

# 4hr weighted more heavily: higher timeframe structure dominates in ICT
bos_score = 0.35 * bos_1hr + 0.65 * bos_4hr
```

### Combined Rule 3 Score

```
rule3_score = 0.55 * pd_score + 0.45 * bos_score
```

---

## Rule 4: SMT Divergence Score

Used in two places:
1. **Co-evaluation with Rule 1** (inline, before returning from Rule 1).
2. **Standalone contribution** to the Rules 3+4 scoring layer when Rule 1 has not fired.

`divs` is the list returned by `_compute_divs` (already computed in Step 5 of
`run_hypothesis`, before direction is determined).

### Helper: `_closest_level_name(price, liquidities)`

Maps a raw price to the name of the nearest level in `liquidities` (from `daily.json`),
within a 10-point tolerance. Used by `_compute_smt_score` to weight divergences by which
liquidity level they occurred near.

```python
def _closest_level_name(price, liquidities):
    if price is None:
        return None
    candidates = []
    for liq in liquidities:
        if liq.get("kind") == "level":
            candidates.append((liq["name"], liq["price"]))
        elif liq.get("kind") == "fvg":
            mid = (liq["top"] + liq["bottom"]) / 2.0
            candidates.append((liq["name"], mid))
    if not candidates:
        return None
    best_name, best_price = min(candidates, key=lambda x: abs(x[1] - price))
    return best_name if abs(best_price - price) <= 10 else None
```

Implemented in `hypothesis.py`. Do **not** import from `plot_regression.py` — that module's
`_closest_level_name` closes over a different scope and is not importable here.

### `_compute_smt_score(divs, liquidities)`

```
LEVEL_WEIGHT = {
    "week_high":3, "week_low":3,
    "day_high":2,  "day_low":2,
    "ny_morning_high":1, "ny_morning_low":1,
    "london_high":1,     "london_low":1,
    # all other session-level names → 1
}
TF_WEIGHT   = {"30m": 2.0, "15m": 1.0}
TYPE_WEIGHT = {"wick":2.0, "wick_sym":2.0, "body":1.5, "body_sym":1.5, "fill":1.0}

score = 0.0
max_possible = 0.0

FOR div IN divs:
    level_name = _closest_level_name(div.get("mnq_div_price"), liquidities)
    lw = LEVEL_WEIGHT.get(level_name, 1)
    tw = TF_WEIGHT.get(div["timeframe"], 1.0)
    yw = TYPE_WEIGHT.get(div["type"], 1.0)
    w  = lw * tw * yw
    sign = +1 if div["side"] == "bullish" else -1
    score        += sign * w
    max_possible += w

IF max_possible == 0: RETURN 0.0
RETURN clamp(score / max_possible, -1.0, 1.0)
```

---

## Top-Level Combiner: `_determine_direction`

This function replaces the single line `direction = "up"` in Step 6 of `run_hypothesis`.
It returns a `(direction, direction_reason)` tuple. `direction_reason` is a dict that is
attached to the `new-hypothesis` event for observability (see section below).

```
FUNCTION _determine_direction(
    current_bar,           # 5m bar dict (keys: Open, High, Low, Close — uppercase, as returned by _build_5m_bar)
    mnq_1m,                # session 1m bars DataFrame up to and including current bar
    hist_mnq_1m,           # historical 1m bars DataFrame
    liquidities,           # from daily.json
    global_state,          # from global.json
    divs,                  # from _compute_divs (step 5)
):
    # ── Shared pre-computation ────────────────────────────────────────────────
    fvg_1hr  = _detect_fvg_1hr(hist_mnq_1m, mnq_1m)
    levels   = _build_meaningful_levels(liquidities, fvg_1hr)
    prior    = mnq_1m.iloc[:-1]                     # session bars before current bar
    smt_sc   = _compute_smt_score(divs, liquidities)

    week_high = named_price(liquidities, "week_high")
    week_low  = named_price(liquidities, "week_low")
    day_high  = named_price(liquidities, "day_high")
    day_low   = named_price(liquidities, "day_low")
    current_close = current_bar["Close"]   # uppercase — matches _build_5m_bar output

    reason = {                                       # populated throughout, returned always
        "rule": None,
        "weekly_zone": "premium" if current_close > (week_high+week_low)/2 else "discount"
                        if week_high and week_low else "unknown",
        "daily_zone":  "premium" if current_close > (day_high+day_low)/2   else "discount"
                        if day_high and day_low else "unknown",
        "smt_score":   round(smt_sc, 3),
        "pd_score":    None,
        "bos_score_1hr": None,
        "bos_score_4hr": None,
        "rule3_score": None,
        "combined_score": None,
        "fresh_touch_level": None,
        "smt_alignment": None,
        "approaching_level": None,
        "approaching_dist": None,
    }

    # ── Rule 1 ────────────────────────────────────────────────────────────────
    r1 = _check_fresh_touch(current_bar, prior, levels)
    IF r1:
        conf, smt_aln = _co_evaluate_with_smt(r1.direction, r1.base_conf, smt_sc)
        reason["rule"]              = "rule1"
        reason["fresh_touch_level"] = r1.touched_level.name
        reason["smt_alignment"]     = smt_aln
        RETURN r1.direction, reason

    # ── Rule 2: approaching unvisited level with momentum ────────────────────
    # _check_approaching always returns conf=0.75, so this is always decisive
    # when r2 is not None. The "lower-confidence retry" pattern was removed
    # because it was dead code in the current implementation.  It can be
    # restored if Rule 2 SMT co-evaluation (see Deferred section) introduces
    # variable confidence that can fall below a decisive threshold.
    r2 = _check_approaching(current_bar, prior, levels, mnq_1m)
    IF r2:
        reason["rule"]              = "rule2"
        reason["approaching_level"] = r2.approaching_level.name
        reason["approaching_dist"]  = round(r2.dist, 1)
        RETURN r2.direction, reason

    # ── Rules 3 + 4 scoring ───────────────────────────────────────────────────
    pd_sc  = _compute_pd_score(current_close, week_high, week_low, day_high, day_low)
    combined_1m = concat(hist_mnq_1m, mnq_1m).drop_duplicates().sort_index()
    b1hr = _compute_bos_choch_score(combined_1m.resample("1h")..., BOS_SWING_N, BOS_LOOKBACK_1HR)
    b4hr = _compute_bos_choch_score(combined_1m.resample("4h")..., BOS_SWING_N, BOS_LOOKBACK_4HR)
    bos_sc = 0.35 * b1hr + 0.65 * b4hr
    r3_sc  = 0.55 * pd_sc + 0.45 * bos_sc
    combined = 0.65 * r3_sc + 0.35 * smt_sc

    reason["pd_score"]      = round(pd_sc,  3)
    reason["bos_score_1hr"] = round(b1hr,   3)
    reason["bos_score_4hr"] = round(b4hr,   3)
    reason["rule3_score"]   = round(r3_sc,  3)
    reason["combined_score"]= round(combined, 3)

    IF abs(combined) >= DIRECTION_SCORE_THRESHOLD:
        reason["rule"] = "rule3_4"
        RETURN ("up" if combined > 0 else "down"), reason

    # ── Rule 5: global trend fallback ─────────────────────────────────────────
    reason["rule"] = "rule5_trend"
    RETURN global_state.get("trend", "up"), reason
```

---

## Changes to `run_hypothesis`

### Step 6 (currently `direction = "up"`)

Replace with:

```python
direction, direction_reason = _determine_direction(
    current_bar   = bar,
    mnq_1m        = mnq_1m,
    hist_mnq_1m   = hist_mnq_1m,
    liquidities   = liquidities,
    global_state  = global_state,
    divs          = divs,          # already computed in Step 5
)
```

`bar` is the 5m bar dict already built by `_build_5m_bar` at the top of `run_hypothesis`.

**Step 8b (direction veto) still runs unchanged after step 6.** The existing veto logic —
which overrides `direction` to `"none"` when the secondary cautious price is too close, or
when `direction == "up"` and price is already at or above ATH — is not modified by this
change. When the veto fires, the function returns `divs` only with no `hyp_event`, which is
the existing behavior. `direction_reason` is therefore only attached to `hyp_event` when
`direction != "none"` *after the veto*. No change to the veto logic is required.

### `hyp_event` (Step 10 — the emitted event)

Add `direction_reason` to the event dict:

```python
hyp_event = {
    "kind":          "new-hypothesis",
    ...                              # all existing fields unchanged
    "direction_reason": direction_reason,
}
```

No other changes to `run_hypothesis`.

---

## Observability

### What is already in `events.jsonl`

`new-hypothesis` events are already written to `events.jsonl` via `day_events`. Current
fields: `kind`, `time`, `price`, `direction`, `weekly_mid`, `daily_mid`, `last_liquidity`,
`targets`, `cautious_price_initial[_level]`, `cautious_price_secondary[_level]`,
`entry_ranges`.

### New field: `direction_reason`

The `direction_reason` dict (populated inside `_determine_direction`) is embedded directly
in the `new-hypothesis` event. It contains:

| Field | Type | Meaning |
|---|---|---|
| `rule` | str | Which rule decided: `"rule1"`, `"rule2"`, `"rule2_weak"`, `"rule3_4"`, `"rule5_trend"` |
| `weekly_zone` | str | `"premium"` or `"discount"` relative to weekly mid |
| `daily_zone` | str | `"premium"` or `"discount"` relative to daily mid |
| `smt_score` | float | Signed SMT score `[-1, 1]`; positive = bullish |
| `pd_score` | float \| null | Premium/discount score (null if rule 1 or 2 fired first) |
| `bos_score_1hr` | float \| null | BOS/CHoCH score from 1hr bars |
| `bos_score_4hr` | float \| null | BOS/CHoCH score from 4hr bars |
| `rule3_score` | float \| null | Combined PD + BOS score |
| `combined_score` | float \| null | Final Rules 3+4 combined score |
| `fresh_touch_level` | str \| null | Level name that triggered Rule 1 (e.g. `"week_high"`) |
| `smt_alignment` | str \| null | `"confirmed"`, `"contradicted"`, or `"neutral"` (Rule 1 only) |
| `approaching_level` | str \| null | Level name triggering Rule 2 |
| `approaching_dist` | float \| null | Distance to approaching level in points |

### Plot hover additions (`plot_regression.py`)

In the `new-hypothesis` block of the `OTHER_MARKER_STYLE` loop, after the existing fields
(direction, targets, cautious prices, entry ranges), append lines derived from
`e.get("direction_reason", {})`:

```
dr = e.get("direction_reason", {})

# Always shown
parts.append(f"decided_by: {dr.get('rule', '?')}")
parts.append(f"weekly: {dr.get('weekly_zone', '?')} | daily: {dr.get('daily_zone', '?')}")
parts.append(f"smt_score: {dr.get('smt_score', '?')}")

# Rule 1 specific
if dr.get("fresh_touch_level"):
    parts.append(f"touched: {dr['fresh_touch_level']}  smt: {dr.get('smt_alignment','?')}")

# Rule 2 specific
if dr.get("approaching_level"):
    parts.append(f"approaching: {dr['approaching_level']} ({dr.get('approaching_dist','?')} pts)")

# Rule 3+4 specific (shown when those rules ran, even if they didn't win)
if dr.get("combined_score") is not None:
    parts.append(
        f"pd: {dr.get('pd_score','?')}  "
        f"bos1h: {dr.get('bos_score_1hr','?')}  "
        f"bos4h: {dr.get('bos_score_4hr','?')}  "
        f"→ {dr.get('combined_score','?')}"
    )
```

This means:
- For a Rule 1 decision you see which level was freshly swept and whether SMT confirmed it.
- For a Rule 2 decision you see which level is being approached and how far away.
- For a Rule 3+4 decision you see the full score breakdown.
- For a Rule 5 fallback you see `decided_by: rule5_trend` plus the zone context.
- Scores from Rules 3+4 are always shown when they were computed (even if Rules 1 or 2 fired
  first), giving a secondary cross-check on every hypothesis.

---

## New Helper Functions Summary

All new functions live in `hypothesis.py`:

| Function | Called by |
|---|---|
| `_detect_fvg_1hr(hist_mnq_1m, session_mnq_1m)` | `_determine_direction` |
| `_build_meaningful_levels(liquidities, fvg_1hr)` | `_determine_direction` |
| `_was_previously_touched(level, prior_bars)` | `_check_fresh_touch`, `_check_approaching` |
| `_check_fresh_touch(current_bar, prior, levels)` | `_determine_direction` |
| `_check_approaching(current_bar, prior, levels, mnq_1m)` | `_determine_direction` |
| `_compute_pd_score(close, week_h, week_l, day_h, day_l)` | `_determine_direction` |
| `_compute_bos_choch_score(bars_df, swing_n, lookback)` | `_determine_direction` |
| `_named_price(liquidities, name)` | `_determine_direction` |
| `_closest_level_name(price, liquidities)` | `_compute_smt_score` |
| `_compute_smt_score(divs, liquidities)` | `_determine_direction` |
| `_determine_direction(...)` | `run_hypothesis` Step 6 |

`_detect_fvg_1hr` must be implemented inline — do **not** import or reuse `detect_fvg`
from `strategy_smt`. Its signature (`bars, bar_idx, direction, lookback`) is incompatible:
it requires a single direction, returns at most one FVG per call, and scans backward from a
specific bar index. The helper here needs to scan all bars bidirectionally and return every
FVG in the window.

---

## Deferred to Future Stage

The following are deliberately out of scope for the agent implementing this document.
Do not add scaffolding, placeholders, or TODO comments for them — implement only what
is specified above.

- **Order blocks / breaker blocks:** Require dedicated detection of institutional order
  zone origin bars; substantial additional logic.
- **Kill zone / time macro gating:** Would multiply confidence scores by a time-of-day
  factor; straightforward to add once core logic is proven.
- **PO3 / MMXM manipulation phase arc:** Models the full accumulation → manipulation →
  distribution sequence; requires tracking state across multiple hypothesis calls.
- **Inverse FVG (IFVG):** Polarity flip of an existing FVG when price closes through it;
  additive signal.
- **Rule 2 SMT co-evaluation + variable confidence:** Elevate Rule 2 confidence to ~1.0 when
  SMT divergence is already forming at the level being approached. Implementation sketch is in
  the Rule 2 refinement note above. Straightforward once Rule 2 is validated in production —
  it reuses `_co_evaluate_with_smt` with no new helpers required. When this lands, the
  lower-confidence retry pattern (`IF r2 AND r2.conf >= 0.70: decisive / else: post-scoring
  fallback`) can be restored in `_determine_direction`.
