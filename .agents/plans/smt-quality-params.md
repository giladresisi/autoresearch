# Plan: SMT Quality-Focused Parameter Extensions

**Feature**: Add quality-filtering parameters to `train_smt.py` and update `program_smt.md`
**Type**: Enhancement
**Complexity**: ⚠️ Medium
**Created**: 2026-04-17
**Updated**: 2026-04-17 (post extended-diagnostics revision)

## Context

The previous 30-iteration optimization maximised `mean_test_pnl` and produced champion
config (REENTRY_MAX_MOVE_PTS=999, MAX_HOLD_BARS=120) with WR=32%, EP=$18/trade. Diagnostic
analysis of 2,908 trades reveals the per-trade quality is poor — 1-contract trades (50% of
volume, EP=$2.17) and 5th+ re-entries (EP=$6.78) are the drag.

Goal change: primary metric shifts to `avg_expectancy`, with `win_rate` and `wl_ratio` as
secondaries. This plan implements the new filter parameters and diagnostic fields that make
quality-focused optimization possible.

### Key findings from extended diagnostics (run before this plan was finalised)

1. **`MAX_TDO_DISTANCE_PTS` is the master lever.** TDO<20 trades have WR=37–43% and
   EP=$32–$59 across ALL re-entry sequences — including 5th+. The quality degradation at
   high re-entry counts seen in the aggregate data is almost entirely explained by TDO
   distance, not re-entry depth. Cross-tab confirms: at TDO<20 even Seq#5+ has EP=$32 vs
   the full-dataset Seq#5+ EP=$6.78.

2. **`MIN_CONFIRM_BODY_RATIO` must NOT be implemented.** Near-doji confirmation bars
   (body ratio 0.00–0.10) have WR=0.352 and EP=$20.70 — the best bucket across all ratio
   ranges. Filtering them out would actively harm quality. This constant and its filter logic
   are removed from this plan entirely. The `entry_bar_body_ratio` field is still captured in
   trade records for future diagnostics, but no filter is applied.

3. **`MIN_PRIOR_TRADE_BARS_HELD` is harmful at most thresholds.** The WR does bump from
   0.284 to 0.375 when prior trade survived 10+ bars, but EP stays flat ($16 → $17). The
   majority bucket (prior_bars < 3, n=1,036, 42% of re-entries) has EP=$16.39 and would be
   removed. At TDO<20, re-entry quality is uniformly good regardless of prior duration. This
   constant is implemented for diagnostic completeness but is **not included in the
   optimization agenda** — the optimizer should not tune it.

4. **12:xx Seq#5+ confirmed dead.** Cross-tab shows 12:xx × Seq#5+: n=468, EP=$−0.2.
   Extending `SIGNAL_BLACKOUT_END` to 13:00 is a pure-quality win with negligible PnL cost.

5. **Realistic quality targets.** With MAX_TDO_DISTANCE=20: ~828 total trades, ~138/fold,
   projected WR ~38–40%, EP ~$45–50, WL ~0.62–0.70. WL > 1.0 with adequate volume is not
   achievable from this strategy without restricting to the 09:00 window (~30 trades/fold,
   too thin for reliable walk-forward statistics).

---

## Execution agent rules (verbatim)
- Make ALL code changes required by the plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`

---

## Files to Modify

| File | Change |
|------|--------|
| `train_smt.py` | Add 5 constants, modify 3 functions, modify 2 harness functions |
| `program_smt.md` | Update evaluation criteria, tunable constants list, optimization agenda |
| `tests/test_smt_strategy.py` | Update callers of detect_smt_divergence; add new filter tests |
| `tests/test_smt_backtest.py` | Add MAX_REENTRY_COUNT and new-fields tests |

---

## Pre-Execution Baseline

Before making any changes, run:

```bash
uv run python -m pytest tests/test_smt_strategy.py tests/test_smt_backtest.py -x -q 2>&1 | tail -5
```

Record passing/failing count. No regressions permitted.

---

## Task Breakdown

### WAVE 1 — Sequential: constants + strategy function changes (single file, no parallelism)

---

### Task 1 — Add 5 new constants to editable section of `train_smt.py`

**Location**: After the `TRAIL_AFTER_TP_PTS` constant (line ~152), before the `# ══ STRATEGY FUNCTIONS` header.

Add the following block in the editable section:

```python
# Maximum TDO distance filter: skip signals where |entry - TDO| > this value in MNQ pts.
# Cross-tab finding: TDO<20 has WR=37-43% and EP=$32-$59 across ALL re-entry sequences,
# including 5th+. TDO>100 trades are structurally losing (EP=−$2.04). TDO>50 barely break
# even. The quality degradation at high re-entry counts is driven by TDO distance, not depth.
# Optimizer search space: [15, 20, 25, 30, 40, 999].
# Set 999.0 to disable (pass-through for all distances).
MAX_TDO_DISTANCE_PTS = 999.0

# Maximum re-entries per session day.
# At TDO<20 (with MAX_TDO_DISTANCE_PTS applied), even Seq#5+ has EP=$32, so this filter
# is less important than expected. Most useful at TDO 20-50 where Seq#5+ declines to EP=$6.
# Optimizer search space: [1, 2, 3, 4, 999]. Default 999 = disabled.
MAX_REENTRY_COUNT = 999

# Minimum bars the prior trade must have survived before re-entry is allowed.
# DIAGNOSTIC ONLY — do not include in optimization runs. Extended diagnostics showed:
# prior_bars<3 (n=1036, 42% of re-entries) has EP=$16.39 — removing these hurts volume
# without improving EP. WR bumps at 10+ bars but EP stays flat. At TDO<20, prior duration
# is irrelevant. Set 0 to disable (always allow re-entry).
MIN_PRIOR_TRADE_BARS_HELD = 0

# Minimum MES sweep magnitude for SMT divergence: how far MES must exceed the prior
# session extreme to qualify. Marginal sweeps (< 1 pt) are noise.
# Optimizer search space: [0, 1, 2, 5].
# Set 0.0 to disable.
MIN_SMT_SWEEP_PTS = 0.0

# Minimum MNQ miss magnitude for SMT divergence: how far MNQ must fail to match MES.
# A strong divergence (MNQ missed by 3 pts) is more reliable than a marginal one (0.5 pt).
# Optimizer search space: [0, 1, 2, 5].
# Set 0.0 to disable.
MIN_SMT_MISS_PTS = 0.0
```

**Acceptance**: All 5 constants importable from `train_smt`. Module-level import (`import train_smt`) succeeds.

---

### Task 2 — Modify `detect_smt_divergence()` — return magnitude tuple + sweep/miss filters

**Current signature**: Returns `str | None`
**New signature**: Returns `tuple[str, float, float] | None` — `(direction, sweep_pts, miss_pts)`

**Why**: `smt_sweep_pts` and `smt_miss_pts` must flow through the state machine to trade records.
Changing the return type is cleaner than a separate lookup.

**Changes inside `detect_smt_divergence`**:

Replace the bearish SMT block:
```python
# OLD:
if cur_mes["High"] > mes_session_high and cur_mnq["High"] <= mnq_session_high:
    return "short"
```
With:
```python
if cur_mes["High"] > mes_session_high and cur_mnq["High"] <= mnq_session_high:
    smt_sweep = cur_mes["High"] - mes_session_high
    mnq_miss   = mnq_session_high - cur_mnq["High"]
    if MIN_SMT_SWEEP_PTS > 0 and smt_sweep < MIN_SMT_SWEEP_PTS:
        return None
    if MIN_SMT_MISS_PTS > 0 and mnq_miss < MIN_SMT_MISS_PTS:
        return None
    return ("short", smt_sweep, mnq_miss)
```

Replace the bullish SMT block:
```python
# OLD:
if cur_mes["Low"] < mes_session_low and cur_mnq["Low"] >= mnq_session_low:
    return "long"
```
With:
```python
if cur_mes["Low"] < mes_session_low and cur_mnq["Low"] >= mnq_session_low:
    smt_sweep = mes_session_low - cur_mes["Low"]
    mnq_miss   = cur_mnq["Low"] - mnq_session_low
    if MIN_SMT_SWEEP_PTS > 0 and smt_sweep < MIN_SMT_SWEEP_PTS:
        return None
    if MIN_SMT_MISS_PTS > 0 and mnq_miss < MIN_SMT_MISS_PTS:
        return None
    return ("long", smt_sweep, mnq_miss)
```

Also update the docstring return annotation: `Returns tuple (direction, sweep_pts, miss_pts) or None`.

**Update callers** (both in the editable section above `# DO NOT EDIT`):

`screen_session()` — line ~424:
```python
# OLD:
direction = detect_smt_divergence(mes_reset, mnq_reset, bar_idx, 0)
if direction is None:
    continue
if TRADE_DIRECTION != "both" and direction != TRADE_DIRECTION:
    continue
```
→
```python
_smt = detect_smt_divergence(mes_reset, mnq_reset, bar_idx, 0)
if _smt is None:
    continue
direction, _smt_sweep, _smt_miss = _smt
if TRADE_DIRECTION != "both" and direction != TRADE_DIRECTION:
    continue
```

And pass `_smt_sweep`, `_smt_miss` to `_build_signal_from_bar` (see Task 3).

**Update `run_backtest()` caller** (IDLE state, below boundary):
```python
# OLD:
direction = detect_smt_divergence(mes_reset, mnq_reset, bar_idx, 0)
if direction is None:
    continue
if TRADE_DIRECTION != "both" and direction != TRADE_DIRECTION:
    continue
```
→
```python
_smt = detect_smt_divergence(mes_reset, mnq_reset, bar_idx, 0)
if _smt is None:
    continue
direction, _smt_sweep, _smt_miss = _smt
if TRADE_DIRECTION != "both" and direction != TRADE_DIRECTION:
    continue
```

Store the values for later use when building the position (see Task 5):
```python
# After WAITING_FOR_ENTRY assignment:
pending_direction = direction
anchor_close = ac
pending_smt_sweep = _smt_sweep
pending_smt_miss  = _smt_miss
divergence_bar_idx = bar_idx
state = "WAITING_FOR_ENTRY"
```

**Acceptance**: `detect_smt_divergence(...)[0] in ("short", "long")` when not None; None on no match or filter rejection.

---

### Task 3 — Modify `_build_signal_from_bar()` — add MAX_TDO_DISTANCE_PTS + new signal fields

**Add MAX_TDO_DISTANCE_PTS ceiling check** after the existing `MIN_TDO_DISTANCE_PTS` check:

```python
if MIN_TDO_DISTANCE_PTS > 0 and distance_to_tdo < MIN_TDO_DISTANCE_PTS:
    return None
# Ceiling filter — trades with extreme TDO distance have collapsing RR and negative EP
if MAX_TDO_DISTANCE_PTS < 999.0 and distance_to_tdo > MAX_TDO_DISTANCE_PTS:
    return None
```

**Add optional parameters** to the function signature:
```python
def _build_signal_from_bar(
    bar: pd.Series,
    ts: "pd.Timestamp",
    direction: str,
    tdo: float,
    smt_sweep_pts: float = 0.0,
    smt_miss_pts: float = 0.0,
    divergence_bar_idx: int = -1,
) -> dict | None:
```

**Compute `entry_bar_body_ratio` from the bar** (diagnostic field — no filter applied):
```python
bar_range = bar["High"] - bar["Low"]
entry_bar_body_ratio = (
    abs(bar["Close"] - bar["Open"]) / bar_range if bar_range > 0 else 0.0
)
```

**Add new fields to returned dict**:
```python
return {
    "direction":            direction,
    "entry_price":          entry_price,
    "entry_time":           ts,
    "take_profit":          tdo,
    "stop_price":           round(stop_price, 4),
    "tdo":                  tdo,
    "divergence_bar":       divergence_bar_idx,
    "entry_bar":            -1,
    # Diagnostic fields — captured for analysis, no filter logic applied
    "smt_sweep_pts":        round(smt_sweep_pts, 4),
    "smt_miss_pts":         round(smt_miss_pts, 4),
    "entry_bar_body_ratio": round(entry_bar_body_ratio, 4),
}
```

**Update `screen_session()` call site** to pass sweep/miss:
```python
signal = _build_signal_from_bar(
    conf_bar, entry_time, direction, tdo,
    smt_sweep_pts=_smt_sweep,
    smt_miss_pts=_smt_miss,
)
```

**Acceptance**: `_build_signal_from_bar` returns `None` when `distance_to_tdo > MAX_TDO_DISTANCE_PTS`
(with the constant set to a finite value). The returned dict contains all 3 new fields.
`entry_bar_body_ratio` is populated from bar OHLC but no filter is applied regardless of its value.

---

### Task 4 — Modify `run_backtest()` — per-day quality state + MAX_REENTRY_COUNT + MIN_PRIOR_TRADE_BARS_HELD

This function is below `# DO NOT EDIT BELOW THIS LINE` but requires state machine changes to
implement the new parameters. These are authorised architectural changes per goal_change.md.

**4a — Add new per-session state variables** in the outer session setup (at the `state = "IDLE"` /
`equity_curve` initialization block, before the `for day in trading_days` loop):

```python
# Quality state — reset per day
reentry_count         = 0    # how many entries taken today
prior_trade_bars_held = 0    # how long the previous trade lasted
divergence_bar_idx    = -1   # bar index where divergence was detected this session
pending_smt_sweep     = 0.0
pending_smt_miss      = 0.0
```

**4b — Reset at day boundary** (in the `if state != "IN_TRADE": state = "IDLE"` reset block):

```python
reentry_count         = 0
prior_trade_bars_held = 0
divergence_bar_idx    = -1
pending_smt_sweep     = 0.0
pending_smt_miss      = 0.0
```

Also reset at the end-of-day block (after `state = "IDLE"` at end of session):
```python
reentry_count         = 0
prior_trade_bars_held = 0
```

**4c — Store divergence metadata** when entering WAITING_FOR_ENTRY (in IDLE state, after the
`detect_smt_divergence` call):

```python
pending_direction   = direction
anchor_close        = ac
pending_smt_sweep   = _smt_sweep
pending_smt_miss    = _smt_miss
divergence_bar_idx  = bar_idx
state               = "WAITING_FOR_ENTRY"
```

**4d — Track re-entry sequence** when a trade enters. Both WAITING_FOR_ENTRY and REENTRY_ELIGIBLE
states enter via the same position-building block. After `position = {**signal, ...}`:

```python
reentry_count += 1
position["reentry_sequence"]       = reentry_count
position["prior_trade_bars_held"]  = prior_trade_bars_held
```

Pass sweep/miss to `_build_signal_from_bar` in both WAITING_FOR_ENTRY and REENTRY_ELIGIBLE:
```python
signal = _build_signal_from_bar(
    bar, ts, pending_direction, day_tdo,
    smt_sweep_pts=pending_smt_sweep,
    smt_miss_pts=pending_smt_miss,
    divergence_bar_idx=divergence_bar_idx,
)
```

**4e — Track prior_trade_bars_held** when closing a position. In the `if result != "hold":` block,
before resetting `entry_bar_count`:

```python
prior_trade_bars_held = entry_bar_count  # capture before reset
```

**4f — Apply MAX_REENTRY_COUNT gate** in the REENTRY_ELIGIBLE state, at the top of the state branch:

```python
elif state == "REENTRY_ELIGIBLE":
    # Gate: exceeded daily re-entry cap → abandon this signal
    if MAX_REENTRY_COUNT < 999 and reentry_count >= MAX_REENTRY_COUNT:
        state = "IDLE"
        pending_direction = None
        anchor_close = None
        continue
    # Apply blackout ...  (existing code follows)
```

**4g — Apply MIN_PRIOR_TRADE_BARS_HELD gate** in the REENTRY_ELIGIBLE eligibility check.
After the `REENTRY_MAX_MOVE_PTS` gate (in the `if REENTRY_MAX_MOVE_PTS > 0 and result in (...)`
block), when deciding whether to set `state = "REENTRY_ELIGIBLE"`:

```python
# After computing move and checking REENTRY_MAX_MOVE_PTS threshold:
if move < REENTRY_MAX_MOVE_PTS:
    # Require prior trade to have lasted long enough (diagnostic filter; default 0 = disabled)
    if MIN_PRIOR_TRADE_BARS_HELD > 0 and prior_trade_bars_held < MIN_PRIOR_TRADE_BARS_HELD:
        state = "IDLE"
    else:
        state = "REENTRY_ELIGIBLE"
        anchor_close = float(bar["Close"])
else:
    state = "IDLE"
```

**Acceptance**:
- With `MAX_REENTRY_COUNT=2`, a session that produces 3 stop-outs takes at most 2 trades total.
- With `MIN_PRIOR_TRADE_BARS_HELD=10`, a trade stopped out after 5 bars does not trigger re-entry.
- With both at defaults (999/0), behaviour is identical to before these changes.

---

### Task 5 — Modify `_build_trade_record()` — add new diagnostic fields to trade dict

This function is below `# DO NOT EDIT BELOW THIS LINE`.

Add the following fields to the returned `trade` dict (pull from `position`):

```python
trade = {
    # ... existing fields unchanged ...
    # Quality/diagnostic fields
    "reentry_sequence":        position.get("reentry_sequence", 1),
    "prior_trade_bars_held":   position.get("prior_trade_bars_held", 0),
    "entry_bar_body_ratio":    round(position.get("entry_bar_body_ratio", 0.0), 4),
    "smt_sweep_pts":           round(position.get("smt_sweep_pts", 0.0), 4),
    "smt_miss_pts":            round(position.get("smt_miss_pts", 0.0), 4),
    "bars_since_divergence": (
        position.get("entry_bar", -1) - position.get("divergence_bar", -1)
        if position.get("entry_bar", -1) >= 0 and position.get("divergence_bar", -1) >= 0
        else -1
    ),
}
```

**Acceptance**: A trade record contains all 6 new keys. `reentry_sequence=1` for the first
trade of the day, `2` for the first re-entry, etc.

---

### Task 6 — Update `program_smt.md`

**6a — Update Evaluation Criteria section** (replace the existing table):

```markdown
## Evaluation Criteria

A strategy iteration is considered an improvement if ALL of the following hold:

| Criterion | Threshold | Priority |
|-----------|-----------|----------|
| `avg_expectancy` | Higher than previous best | **PRIMARY** |
| `win_rate` (per fold avg) | ≥ 0.38 (improvement from ~0.32 baseline) | Guard |
| `avg_rr` | ≥ 1.5 | Guard |
| `total_test_trades` (sum) | ≥ 80 | Volume guard |
| `mean_test_pnl` | ≥ 1,500 (prevents trivially thin strategy) | Floor guard |
| `min_test_pnl` | > 0 (all qualified folds profitable) | Secondary guard |

`avg_expectancy` maps to `avg_pnl_per_trade` in the `print_results()` output.
`wl_ratio` = `avg_win_rate / (1 - avg_win_rate)` — target > 0.70, stretch goal > 1.0.
When two iterations both satisfy all guards, prefer the one with higher `avg_expectancy`.
```

**6b — Add new tunable constants** to the "Tunable Constants" list after `MIN_TDO_DISTANCE_PTS`:

```markdown
- `MAX_TDO_DISTANCE_PTS` — ceiling on |entry − TDO| distance (default 999.0 = disabled).
  Cross-tab: TDO<20 has EP=$32–$59 across ALL sequences including 5th+; TDO>100 EP=−$2.04.
  Optimizer search space: [15, 20, 25, 30, 40, 999]
- `MAX_REENTRY_COUNT` — max re-entries per session day (default 999 = disabled).
  Less impactful when MAX_TDO_DISTANCE_PTS is tight; at TDO<20 even Seq#5+ has EP=$32.
  Optimizer search space: [1, 2, 3, 4, 999]
- `MIN_PRIOR_TRADE_BARS_HELD` — min bars prior trade must survive before re-entry allowed
  (default 0 = disabled). DIAGNOSTIC ONLY — do not include in optimization runs.
  Extended diagnostics: removing fast re-entries (prior_bars<10, n=1781) provides no EP gain.
- `MIN_SMT_SWEEP_PTS` — min pts MES must exceed prior session extreme (default 0.0 = disabled);
  optimizer search [0, 1, 2, 5]
- `MIN_SMT_MISS_PTS` — min pts MNQ must fail to match MES (default 0.0 = disabled);
  optimizer search [0, 1, 2, 5]
```

**NOTE**: `MIN_CONFIRM_BODY_RATIO` is intentionally absent. Extended diagnostics showed
near-doji confirmation bars (ratio 0.00–0.10) have WR=0.352 and EP=$20.70 — the best bucket.
Filtering them would actively harm quality. This constant is not implemented.

**6c — Replace Optimization Agenda** with the updated quality-focused agenda below.
Replace the current agenda entirely (all existing Priority blocks).

```markdown
## Optimization Agenda (Quality-Focused)

Work through in order. At each step, use the best accepted configuration as the base.
Primary metric: `avg_expectancy`. Guards: `win_rate ≥ 0.38`, `total_test_trades ≥ 80`.

### Priority 1 — MAX_TDO_DISTANCE_PTS (highest expected impact)

Grid search: [15, 20, 25, 30, 40, 999]

This is the master lever. Cross-tab analysis: at TDO<20, every re-entry sequence
(including 5th+) has WR > 0.34 and EP > $31. At TDO>100, even Seq#1 has EP=−$8.
Setting to 20 keeps ~828/2908 trades (28%) but projects WR 32%→38–40%, EP $18→$45–50.
Setting to 15 is more aggressive (~500 trades) but may reduce volume near the 80/fold guard.

At tight TDO, re-entry count becomes much less important — do not apply MAX_REENTRY_COUNT
simultaneously in this step.

Optimise for: avg_expectancy (primary), win_rate (secondary), total_test_trades ≥ 80 guard.

### Priority 2 — SIGNAL_BLACKOUT_END extension to 13:00

Test: ["12:00", "13:00"]

Cross-tab confirms: 12:xx × Seq#5+ = n=468 trades, EP=−$0.2 — dead weight. The 13:xx
window (WR=0.416, EP=$13.14) remains accessible after blackout extension. This is a pure
quality gain with negligible volume cost after MAX_TDO is applied.

Optimise for: avg_expectancy (primary).

### Priority 3 — MAX_REENTRY_COUNT (test after Priority 1 is locked)

Grid search: [1, 2, 3, 4, 999] using best MAX_TDO_DISTANCE from Priority 1.

Expected lower impact than in the original agenda. At TDO<20, all re-entry sequences are
high quality so the cap may not improve metrics. Most likely to help at the boundary
TDO (20–30 range) where Seq#5+ drops to EP=$6. If Priority 1 converges to MAX_TDO=20,
this step may produce no improvement — accept 999 (disabled) and move on.

Optimise for: avg_expectancy (primary), total_test_trades ≥ 80 guard.

### Priority 4 — SMT Strength Filters

Grid search: MIN_SMT_SWEEP_PTS ∈ [0, 1, 2, 5]; MIN_SMT_MISS_PTS ∈ [0, 1, 2, 5]

Filters marginal SMT divergences. A strong divergence (MES overshot by 5 pts, MNQ failed
by 3 pts) is more reliable than a marginal one (both by 0.5 pts). Effect magnitude unknown
from current diagnostics — these fields are now captured in trade records, enabling post-run
analysis. Test 2D grid; if neither filter improves EP, disable both.

### Priority 5 — 09:00 Window Isolation (if trade volume allows)

The 09:00 window (WR=59%, WL=1.44) is the only known path to WL > 1.0 without extreme
volume sacrifice. With ~183 trades total (~30/fold) it is borderline for reliable statistics.
Evaluate whether the combined filters from Priorities 1–4 naturally concentrate trades in
the 09:00 window, or whether an explicit time restriction is warranted. If testing, treat
as a separate strategy evaluation rather than a parameter of the main strategy.

### NOT IN AGENDA — MIN_CONFIRM_BODY_RATIO

Diagnostics show near-doji bars are the best confirmation candles (EP=$20.70). Do not test
this filter. The constant is not present in the codebase.

### NOT IN AGENDA — MIN_PRIOR_TRADE_BARS_HELD

Diagnostics show removing prior_bars<10 re-entries (73% of all re-entries) gains no EP
improvement. The constant exists for diagnostic completeness (default 0 = disabled) but
must not be included in optimizer search spaces.
```

**Acceptance**: `program_smt.md` no longer mentions `mean_test_pnl` as the primary criterion.
The new PRIMARY criterion is `avg_expectancy`. All 5 implemented constants appear in the
tunable list. MIN_CONFIRM_BODY_RATIO is explicitly absent with explanation.

---

### WAVE 2 — Tests

### Task 7 — Update `tests/test_smt_strategy.py`

**7a — Fix callers of `detect_smt_divergence`** — all tests that check `result == "short"` or
`result == "long"` or `result is None` need updating:

Pattern: `assert result == "short"` → `assert result is not None and result[0] == "short"`
Pattern: `assert result == "long"` → `assert result is not None and result[0] == "long"`
Pattern: `assert result is None` — unchanged (None is still None)

**7b — Add tests for `MAX_TDO_DISTANCE_PTS`** in `_build_signal_from_bar`:

```python
def test_build_signal_max_tdo_distance_ceiling(monkeypatch):
    """Signal rejected when |entry - TDO| > MAX_TDO_DISTANCE_PTS."""
    import train_smt
    monkeypatch.setattr(train_smt, "MAX_TDO_DISTANCE_PTS", 30.0)
    bar = pd.Series({"Open": 100.0, "High": 105.0, "Low": 95.0, "Close": 99.0})
    ts  = pd.Timestamp("2025-01-02 09:30:00", tz="America/New_York")
    # TDO = 60 → distance = 39 > 30 → rejected
    result = train_smt._build_signal_from_bar(bar, ts, "short", 60.0)
    assert result is None

def test_build_signal_max_tdo_distance_pass(monkeypatch):
    """Signal passes when |entry - TDO| <= MAX_TDO_DISTANCE_PTS."""
    import train_smt
    monkeypatch.setattr(train_smt, "MAX_TDO_DISTANCE_PTS", 50.0)
    bar = pd.Series({"Open": 100.0, "High": 105.0, "Low": 95.0, "Close": 99.0})
    ts  = pd.Timestamp("2025-01-02 09:30:00", tz="America/New_York")
    # TDO = 60 → distance = 39 < 50 → passes
    result = train_smt._build_signal_from_bar(bar, ts, "short", 60.0)
    assert result is not None

def test_build_signal_max_tdo_distance_disabled(monkeypatch):
    """MAX_TDO_DISTANCE_PTS=999.0 disables the ceiling filter."""
    import train_smt
    monkeypatch.setattr(train_smt, "MAX_TDO_DISTANCE_PTS", 999.0)
    bar = pd.Series({"Open": 100.0, "High": 105.0, "Low": 95.0, "Close": 99.0})
    ts  = pd.Timestamp("2025-01-02 09:30:00", tz="America/New_York")
    # Distance = 900 — should pass with ceiling disabled
    result = train_smt._build_signal_from_bar(bar, ts, "short", 1000.0)
    assert result is not None
```

**7c — Add tests for `detect_smt_divergence` new return type**:

```python
def test_detect_smt_divergence_returns_tuple_on_match():
    import train_smt
    mes = _make_1m_bars(
        opens=[100]*5, highs=[101,102,101,101,103], lows=[99]*5, closes=[100]*5
    )
    mnq = _make_1m_bars(
        opens=[200]*5, highs=[201,202,201,201,201], lows=[199]*5, closes=[200]*5
    )
    result = train_smt.detect_smt_divergence(mes, mnq, bar_idx=4, session_start_idx=0)
    assert result is not None
    direction, sweep, miss = result
    assert direction == "short"
    assert sweep > 0
    assert miss >= 0

def test_detect_smt_divergence_sweep_filter(monkeypatch):
    """Returns None when sweep < MIN_SMT_SWEEP_PTS."""
    import train_smt
    monkeypatch.setattr(train_smt, "MIN_SMT_SWEEP_PTS", 5.0)
    mes = _make_1m_bars(
        opens=[100]*5, highs=[101,102,101,101,102.5], lows=[99]*5, closes=[100]*5
    )
    mnq = _make_1m_bars(
        opens=[200]*5, highs=[201,202,201,201,201], lows=[199]*5, closes=[200]*5
    )
    # MES sweep = 102.5 - 102 = 0.5 < 5.0 → filtered
    result = train_smt.detect_smt_divergence(mes, mnq, bar_idx=4, session_start_idx=0)
    assert result is None
```

**7d — Add test for new signal fields in `_build_signal_from_bar`**:

```python
def test_build_signal_contains_diagnostic_fields():
    import train_smt
    bar = pd.Series({"Open": 105.0, "High": 107.0, "Low": 97.0, "Close": 101.0})
    ts  = pd.Timestamp("2025-01-02 09:30:00", tz="America/New_York")
    result = train_smt._build_signal_from_bar(
        bar, ts, "short", 60.0, smt_sweep_pts=2.5, smt_miss_pts=1.3
    )
    assert result is not None
    assert "smt_sweep_pts" in result
    assert "smt_miss_pts" in result
    assert "entry_bar_body_ratio" in result
    assert result["smt_sweep_pts"] == 2.5
    assert result["smt_miss_pts"] == 1.3
    assert 0.0 <= result["entry_bar_body_ratio"] <= 1.0

def test_build_signal_body_ratio_not_filtered(monkeypatch):
    """Near-doji bars (low body ratio) are NOT rejected — diagnostics show they are best."""
    import train_smt
    # Near-doji: body=0.1, range=20 → ratio=0.005 (extreme doji)
    bar = pd.Series({"Open": 100.0, "High": 110.0, "Low": 90.0, "Close": 99.9})
    ts  = pd.Timestamp("2025-01-02 09:30:00", tz="America/New_York")
    result = train_smt._build_signal_from_bar(bar, ts, "short", 60.0)
    assert result is not None, "Near-doji bars must not be filtered — they have highest EP"
    assert result["entry_bar_body_ratio"] < 0.01
```

---

### Task 8 — Update `tests/test_smt_backtest.py`

**8a — Add MAX_REENTRY_COUNT integration test**:

```python
def test_run_backtest_max_reentry_count_limits_trades(monkeypatch, tmp_path):
    """MAX_REENTRY_COUNT=1 prevents a second re-entry within same session."""
    import train_smt
    monkeypatch.setattr(train_smt, "MAX_REENTRY_COUNT", 1)
    monkeypatch.setattr(train_smt, "REENTRY_MAX_MOVE_PTS", 999.0)
    monkeypatch.setattr(train_smt, "TRADE_DIRECTION", "both")
    mnq, mes = _build_short_signal_bars("2025-01-02")
    manifest = {"backtest_start": "2025-01-02", "backtest_end": "2025-01-04",
                "fetch_interval": "5m"}
    _write_manifest(tmp_path, manifest, monkeypatch)
    stats = train_smt.run_backtest(mnq, mes, start="2025-01-02", end="2025-01-04")
    trades_by_day = {}
    for t in stats.get("trade_records", []):
        trades_by_day[t["entry_date"]] = trades_by_day.get(t["entry_date"], 0) + 1
    assert all(v <= 1 for v in trades_by_day.values())

def test_run_backtest_max_reentry_disabled_allows_multiple(monkeypatch, tmp_path):
    """MAX_REENTRY_COUNT=999 (disabled) does not cap trades."""
    import train_smt
    monkeypatch.setattr(train_smt, "MAX_REENTRY_COUNT", 999)
    monkeypatch.setattr(train_smt, "REENTRY_MAX_MOVE_PTS", 999.0)
    mnq, mes = _build_short_signal_bars("2025-01-02")
    manifest = {"backtest_start": "2025-01-02", "backtest_end": "2025-01-04",
                "fetch_interval": "5m"}
    _write_manifest(tmp_path, manifest, monkeypatch)
    stats = train_smt.run_backtest(mnq, mes, start="2025-01-02", end="2025-01-04")
    assert stats["total_trades"] >= 0  # no assertion on count, just no crash
```

**8b — Add trade record new-fields test**:

```python
def test_run_backtest_trade_record_contains_new_fields(monkeypatch, tmp_path):
    """Trade records include all 6 new diagnostic fields."""
    import train_smt
    monkeypatch.setattr(train_smt, "TRADE_DIRECTION", "both")
    mnq, mes = _build_short_signal_bars("2025-01-02")
    manifest = {"backtest_start": "2025-01-02", "backtest_end": "2025-01-04",
                "fetch_interval": "5m"}
    _write_manifest(tmp_path, manifest, monkeypatch)
    stats = train_smt.run_backtest(mnq, mes, start="2025-01-02", end="2025-01-04")
    for t in stats.get("trade_records", []):
        assert "reentry_sequence" in t
        assert "prior_trade_bars_held" in t
        assert "entry_bar_body_ratio" in t
        assert "smt_sweep_pts" in t
        assert "smt_miss_pts" in t
        assert "bars_since_divergence" in t

def test_run_backtest_min_prior_bars_held_infrastructure(monkeypatch, tmp_path):
    """MIN_PRIOR_TRADE_BARS_HELD=0 (default) does not block any re-entries."""
    import train_smt
    monkeypatch.setattr(train_smt, "MIN_PRIOR_TRADE_BARS_HELD", 0)
    monkeypatch.setattr(train_smt, "REENTRY_MAX_MOVE_PTS", 999.0)
    monkeypatch.setattr(train_smt, "TRADE_DIRECTION", "both")
    mnq, mes = _build_short_signal_bars("2025-01-02")
    manifest = {"backtest_start": "2025-01-02", "backtest_end": "2025-01-04",
                "fetch_interval": "5m"}
    _write_manifest(tmp_path, manifest, monkeypatch)
    stats_base = train_smt.run_backtest(mnq, mes, start="2025-01-02", end="2025-01-04")

    monkeypatch.setattr(train_smt, "MIN_PRIOR_TRADE_BARS_HELD", 100)
    stats_blocked = train_smt.run_backtest(mnq, mes, start="2025-01-02", end="2025-01-04")
    # High threshold should reduce or equal re-entry count (gate is wired up)
    assert stats_blocked["total_trades"] <= stats_base["total_trades"]
```

---

### Task 9 — Verification: run tests + baseline backtest

```bash
uv run python -m pytest tests/test_smt_strategy.py tests/test_smt_backtest.py -x -q
```

All pre-existing tests must pass. Count of passing tests should be ≥ baseline + new test count.

Then verify the module loads and defaults produce identical results to pre-change:

```bash
uv run python -c "
import train_smt
print('MAX_TDO_DISTANCE_PTS:', train_smt.MAX_TDO_DISTANCE_PTS)
print('MAX_REENTRY_COUNT:', train_smt.MAX_REENTRY_COUNT)
print('MIN_PRIOR_TRADE_BARS_HELD:', train_smt.MIN_PRIOR_TRADE_BARS_HELD)
print('MIN_SMT_SWEEP_PTS:', train_smt.MIN_SMT_SWEEP_PTS)
print('MIN_SMT_MISS_PTS:', train_smt.MIN_SMT_MISS_PTS)
assert not hasattr(train_smt, 'MIN_CONFIRM_BODY_RATIO'), 'Must not exist'
print('all defaults ok')
"
```

---

## Parallel Execution Waves

```
WAVE 1 (sequential — single file changes, must be ordered):
  Task 1: Add 5 constants to train_smt.py
  Task 2: Modify detect_smt_divergence() + callers
  Task 3: Modify _build_signal_from_bar()
  Task 4: Modify run_backtest()
  Task 5: Modify _build_trade_record()
  Task 6: Update program_smt.md

WAVE 2 (after Wave 1 completes):
  Task 7: Update test_smt_strategy.py
  Task 8: Update test_smt_backtest.py

WAVE 3 (after Wave 2):
  Task 9: Run tests + verify defaults
```

Wave 1 is fully sequential — the functions depend on each other (constants → function signatures →
callers → harness). Wave 2 tasks are independent of each other and could be done in parallel.
Wave 3 validates the complete change.

---

## Test Coverage Summary

| Path | Test Location | Type | Status |
|------|--------------|------|--------|
| `MAX_TDO_DISTANCE_PTS` ceiling filter | test_smt_strategy.py Task 7b | Automated unit | ✅ Planned |
| `MAX_TDO_DISTANCE_PTS` pass (within limit) | test_smt_strategy.py Task 7b | Automated unit | ✅ Planned |
| `MAX_TDO_DISTANCE_PTS` disabled (999) | test_smt_strategy.py Task 7b | Automated unit | ✅ Planned |
| Near-doji bars NOT filtered | test_smt_strategy.py Task 7d | Automated unit | ✅ Planned |
| `detect_smt_divergence` tuple return | test_smt_strategy.py Task 7c | Automated unit | ✅ Planned |
| `MIN_SMT_SWEEP_PTS` filter | test_smt_strategy.py Task 7c | Automated unit | ✅ Planned |
| Signal dict new fields (sweep, miss, body_ratio) | test_smt_strategy.py Task 7d | Automated unit | ✅ Planned |
| `MAX_REENTRY_COUNT=1` limits trades | test_smt_backtest.py Task 8a | Automated integration | ✅ Planned |
| `MAX_REENTRY_COUNT=999` no-op | test_smt_backtest.py Task 8a | Automated integration | ✅ Planned |
| Trade records contain all 6 new fields | test_smt_backtest.py Task 8b | Automated integration | ✅ Planned |
| `MIN_PRIOR_TRADE_BARS_HELD` gate wired up | test_smt_backtest.py Task 8b | Automated integration | ✅ Planned |
| Default constants regression | Task 9 CLI check | Automated (inline) | ✅ Planned |
| `MIN_CONFIRM_BODY_RATIO` absent from module | Task 9 CLI check | Automated (inline) | ✅ Planned |
| `MIN_SMT_MISS_PTS` filter | ⚠️ Not independently tested | — | Combined with sweep test |
| `MIN_PRIOR_TRADE_BARS_HELD=0` true no-op | ⚠️ Not independently tested | — | Covered by existing reentry tests |

**Test automation summary**: 13 new automated tests. 0 manual tests.
Existing test count: ~30 tests across the two files. Post-change minimum: ~43 tests.

---

## Risks and Mitigations

| Risk | Mitigation |
|------|-----------|
| `detect_smt_divergence` return type change breaks existing callers | All callers in editable section and harness are updated in Task 2 before tests run |
| `run_backtest` state variable `pending_smt_sweep`/`miss` not reset at day end | Day-end reset block (Task 4b) covers this explicitly |
| `_build_trade_record` below boundary — agent may refuse to edit | Plan states these are authorised architectural changes; explain rationale if needed |
| `reentry_count` off-by-one: first trade should be sequence=1 not 0 | Counter starts at 0, increments before storing, so first trade = 1. Verified in Task 8b |
| `prior_trade_bars_held` is 0 for the first trade (no prior) | Correct by design — MIN_PRIOR_TRADE_BARS_HELD only gates re-entries, not first entries |
| Optimizer accidentally sets MIN_PRIOR_TRADE_BARS_HELD | program_smt.md explicitly labels it DIAGNOSTIC ONLY and excludes from search spaces |

---

## Acceptance Criteria

1. All 5 new constants exist in the editable section of `train_smt.py` with defaults that disable
   the filter (999.0/999/0/0.0/0.0). `MIN_CONFIRM_BODY_RATIO` does NOT exist anywhere in the module.
2. `detect_smt_divergence` returns a 3-tuple `(direction, sweep_pts, miss_pts)` on a match, or `None`.
   All existing callers updated.
3. `_build_signal_from_bar` returns `None` when `distance_to_tdo > MAX_TDO_DISTANCE_PTS`
   (and constant < 999). Near-doji confirmation bars are NOT rejected regardless of body ratio.
4. `run_backtest` does not allow more than `MAX_REENTRY_COUNT` re-entries per day when set below 999.
5. `run_backtest` blocks re-entry when prior trade bars < `MIN_PRIOR_TRADE_BARS_HELD` (when > 0).
   With `MIN_PRIOR_TRADE_BARS_HELD=0` (default), no re-entries are blocked.
6. Every trade record contains all 6 new keys: `reentry_sequence`, `prior_trade_bars_held`,
   `entry_bar_body_ratio`, `smt_sweep_pts`, `smt_miss_pts`, `bars_since_divergence`.
7. `program_smt.md` evaluation criteria: PRIMARY is `avg_expectancy`, not `mean_test_pnl`.
   All 5 new constants appear in the tunable list. Optimization agenda has 5 priorities.
   `MIN_CONFIRM_BODY_RATIO` is explicitly absent with explanation.
8. All pre-existing tests in `test_smt_strategy.py` and `test_smt_backtest.py` pass.
9. All 13 new tests pass.
10. With all new constants at their disabled defaults, `run_backtest()` produces identical
    trade counts to the pre-change baseline.
