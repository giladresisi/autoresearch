# Feature: Hypothesis Alignment Analysis Infrastructure

**⚠️ EXECUTION RULES — READ FIRST:**
- Implement ALL changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
- Only make code changes — no git operations

Validate documentation and codebase patterns before implementing. Match naming of existing utils, types, and models. Import from correct files.

---

## Feature Description

Build the data-gathering and quantitative analysis tooling described in `strategy-update.md` Phase 1–3. This infrastructure lets us measure whether hypothesis-aligned signals outperform misaligned ones — the prerequisite before any new ICT strategy parameters are added.

Four concrete deliverables:

1. **Per-rule backtest logging** — expose each ICT rule's output (pd_range_case, week_zone, day_zone, trend_direction, hypothesis_direction) as per-trade fields in `trades.tsv`, enabling rule-level decomposition.
2. **`HYPOTHESIS_FILTER` flag** in `backtest_smt.py` — when `True`, only signals where `matches_hypothesis == True` are taken, enabling Outcome B walk-forward validation.
3. **`analyze_hypothesis.py`** — CLI script that reads `trades.tsv` and prints a formatted alignment analysis: aligned vs misaligned vs no-hypothesis split with per-group win rate, avg P&L, avg RR, exit-type distribution, and per-fold consistency check.
4. **Displacement entry quality controls** — two opt-in constants that fix the two structural problems identified in Round 2 experiments with `SMT_OPTIONAL`:
   - `DISPLACEMENT_STOP_MODE: bool = False` — when True and `smt_type=="displacement"`, overrides the initial stop to the displacement bar's extreme (bar Low for long, bar High for short) rather than the SMT structural stop. Mechanistically correct: the displacement thesis fails the moment price closes back through the impulse bar.
   - `MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT: int = 0` — when > 0, gates displacement entries on a numeric hypothesis score (count of rules aligned with signal direction, 0–4). Requires `SMT_OPTIONAL=True`; has no effect on wick/body SMT entries. 0 = gate disabled.
5. **Partial exit level configurability** — `PARTIAL_EXIT_LEVEL_RATIO: float = 0.5` in `strategy_smt.py`. Currently the partial-exit target is hardcoded at the midpoint between entry and TP. This constant (0 = entry price, 1 = TP price) makes it tunable, enabling Round 3 experiments at 0.33 (earlier lock-in) and 0.67 (later, more favorable). Used by `manage_position()` when `PARTIAL_EXIT_ENABLED=True`.
6. **FVG detection diagnostic field** — `fvg_detected: bool` per-trade TSV column. Logs whether a valid FVG zone was identified at signal time (independent of whether Layer B entered). Diagnoses the Round 2 finding that `layer_b_triggers=0` across all 60 days — tells us whether FVG zones are forming but not reaching retracement, or not forming at all.
7. **Layer B hypothesis gate** — `FVG_LAYER_B_REQUIRES_HYPOTHESIS: bool = False`. When True, Layer B (FVG retracement add-on) is only allowed when the session hypothesis score meets `MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT` (reusing the same threshold constant). ICT's two-leg accumulation model is a reversal/pullback structure; this gates Layer B to sessions where hypothesis predicts that kind of structure.

## User Story

As a quant running the SMT strategy
I want to measure whether ICT hypothesis-aligned signals outperform misaligned ones across walk-forward folds
So that I can make an evidence-based decision about which ICT rules (if any) to codify as strategy parameters

## Problem Statement

The session hypothesis system (`hypothesis_smt.py`) is implemented and `matches_hypothesis` is already written to `trades.tsv`. However:
- The individual rule outputs (pd_range_case, week_zone, day_zone, trend_direction) are not logged per trade, so rule-level decomposition (Step 2B from strategy-update.md) is not yet possible.
- There is no analysis script to parse `trades.tsv` and compute the aligned vs misaligned performance split.
- There is no `HYPOTHESIS_FILTER` flag to test "take only aligned signals" as a walk-forward strategy variant.

Without this tooling, the data exists but can't be interrogated.

## Solution Statement

1. Add a `compute_hypothesis_context()` public function to `hypothesis_smt.py` that returns both the final direction AND a flat dict of per-rule fields (5 scalars). This avoids exposing the private `_build_session_context()` and adds zero LLM calls.
2. In `backtest_smt.py`, call `compute_hypothesis_context()` once per session day and attach the per-rule fields to every trade record from that session.
3. Add the new fields to `_write_trades_tsv` fieldnames.
4. Add `HYPOTHESIS_FILTER: bool = False` to the backtest mutable zone; apply the filter at signal acceptance time.
5. Create `analyze_hypothesis.py` with clean public functions and a `__main__` CLI entry point.

## Feature Metadata

**Feature Type**: New Capability
**Complexity**: Medium
**Primary Systems Affected**: `hypothesis_smt.py`, `backtest_smt.py`, new `analyze_hypothesis.py`
**Dependencies**: `pandas` (already installed), no new packages
**Breaking Changes**: No — `_write_trades_tsv` gets new columns via `extrasaction="ignore"` / DictWriter `restval`; old TSV files remain readable.

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ BEFORE IMPLEMENTING

- `hypothesis_smt.py` (lines 472–490) — `compute_hypothesis_direction()` public function; new `compute_hypothesis_context()` mirrors its signature and adds the flat breakdown dict
- `hypothesis_smt.py` (lines 90–178) — `_compute_rule1()` and `_assign_case()` — field names to expose: `pd_range_case`, `pd_range_bias`
- `hypothesis_smt.py` (lines 58–87) — `_compute_rule5()` — field names to expose: `week_zone`, `day_zone`
- `hypothesis_smt.py` (lines 181–226) — `_compute_rule2()` — field name to expose: `trend_direction`
- `hypothesis_smt.py` (lines 440–469) — `_weighted_vote()` — drives final `hypothesis_direction`
- `backtest_smt.py` (lines 87–154) — `_build_trade_record()` — existing trade dict keys; new rule fields are added by the caller (run_backtest), not here
- `backtest_smt.py` (lines 252–256) — `_session_hyp_dir = compute_hypothesis_direction(mnq_df, _hist_mnq_df, day)` — replace with `compute_hypothesis_context()` call
- `backtest_smt.py` (lines 340–347) — `signal["matches_hypothesis"] = (...)` — add rule fields here from session context dict
- `backtest_smt.py` (lines 577–595) — `_write_trades_tsv()` — add new fieldnames
- `analyze_gaps.py` — existing analysis script; follow same pattern: `load_trades()`, `compute_*()`, `print_analysis()`, `if __name__ == "__main__":`
- `tests/test_hypothesis_smt.py` (lines 1–60) — test style, fixture helpers (`_make_1m_df`), mock pattern

### New Files to Create

- `analyze_hypothesis.py` — alignment analysis script
- `tests/test_hypothesis_analysis.py` — unit + integration tests for the new code

### Patterns to Follow

**Naming Conventions**: `compute_*` for pure functions that return data, `_write_*` for file writers, `print_*` for stdout formatters. Snake_case throughout.

**Error Handling**: Return `None` from public compute functions on insufficient data (matches `compute_hypothesis_direction`). Print human-readable warnings to stdout, not stderr, so they appear in the same terminal stream as results.

**Test style**: `unittest.mock.patch` for file I/O; `pd.DataFrame` fixtures built inline with `pd.Timestamp(..., tz="America/New_York")`; no live data or API calls.

---

## PARALLEL EXECUTION STRATEGY

### Dependency Graph

```
┌─────────────────────────────────────────────────────────┐
│ WAVE 1: Foundation (Parallel)                           │
├─────────────────────────────────────────────────────────┤
│ Task 0.1: ADD DISPLACEMENT_STOP_MODE +  Task 1.1: ADD compute_hypothesis_context() │
│ MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT   to hypothesis_smt.py (+ hypothesis_score)  │
│ to strategy_smt.py                      Agent: backend-dev                         │
│ Agent: backend-dev                                                                  │
└────────────────────────────────────────────────────────────────────────────────────┘
                          ↓
┌──────────────────────────────────────────────────────────────────────────────────────┐
│ WAVE 2: Backtest integration (After Wave 0+1)                                        │
├──────────────────────────────────────────────────────────────────────────────────────┤
│ Task 2.1: UPDATE backtest_smt.py │ Task 2.2: ADD       │ Task 2.3: ADD displacement │
│ — add rule fields to trade recs  │ HYPOTHESIS_FILTER   │ stop + score gate to       │
│ + TSV fieldnames + hyp_score     │ to backtest_smt.py  │ backtest_smt.py            │
│ Agent: backend-dev               │ Agent: backend-dev  │ Agent: backend-dev         │
└──────────────────────────────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────┐
│ WAVE 3: Analysis script (After Wave 2)                  │
├─────────────────────────────────────────────────────────┤
│ Task 3.1: CREATE analyze_hypothesis.py                  │
│ Agent: backend-dev                                      │
└─────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────┐
│ WAVE 4: Tests (After Waves 1–3)                         │
├─────────────────────────────────────────────────────────┤
│ Task 4.1: CREATE tests/test_hypothesis_analysis.py      │
│ Agent: test-engineer                                    │
└─────────────────────────────────────────────────────────┘
```

### Parallelization Summary

**Wave 1 — Sequential (single task)**: Task 1.1 — public API that Wave 2 depends on
**Wave 2 — Parallel**: Tasks 2.1 and 2.2 — both modify `backtest_smt.py` in different zones; assign to same agent to avoid conflicts
**Wave 3 — Sequential**: Task 3.1 — needs the TSV schema from Wave 2
**Wave 4 — Sequential**: Task 4.1 — needs all prior code

### Interface Contracts

**Contract 1**: Task 1.1 provides `compute_hypothesis_context(mnq_1m_df, hist_mnq_df, date) -> Optional[dict]` returning:
```python
{
    "direction":        "long" | "short" | "neutral" | None,
    "pd_range_case":    str | None,       # e.g. "1.3"
    "pd_range_bias":    str,              # "long" | "short" | "neutral"
    "week_zone":        str | None,       # "premium" | "discount" | None
    "day_zone":         str | None,       # "premium" | "discount" | None
    "trend_direction":  str,              # "bullish" | "bearish" | "neutral"
    "hypothesis_score": int,              # 0–4: rules aligned with direction
}
```
Returns `None` when Rule 5 returns `None` (insufficient data), matching `compute_hypothesis_direction` behavior.

`hypothesis_score` counts how many of the 4 rule outputs agree with `direction`:
- `pd_range_bias` == direction → +1
- `week_zone` "discount"=long / "premium"=short → +1 if matches
- `day_zone` same mapping → +1 if matches
- `trend_direction` "bullish"=long / "bearish"=short → +1 if matches
Score is 0 when direction is None or "neutral".

**Contract 2**: Task 2.1 attaches these fields from the session context dict to each trade record:
`pd_range_case`, `pd_range_bias`, `week_zone`, `day_zone`, `trend_direction`, `hypothesis_direction`, `hypothesis_score`

**Contract 3**: Task 0.1 provides five constants in `strategy_smt.py` (all default off/neutral):
- `DISPLACEMENT_STOP_MODE: bool = False`
- `MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT: int = 0`
- `PARTIAL_EXIT_LEVEL_RATIO: float = 0.5`  (0 = entry price, 1 = TP price; 0.5 = midpoint, preserving current behaviour)
- `FVG_LAYER_B_REQUIRES_HYPOTHESIS: bool = False`

**Contract 4**: Task 2.3 reads `signal["displacement_bar_extreme"]` (set by Plan 2's IDLE detection when `smt_type=="displacement"`) and uses it as the initial stop when `DISPLACEMENT_STOP_MODE=True`. The score gate reads `_session_hyp_ctx["hypothesis_score"]` (available after Task 1.1) and blocks the displacement entry when score < `MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT`.

---

## IMPLEMENTATION PLAN

### Phase 0: Constants (parallel to Wave 1)

#### Task 0.1: ADD `DISPLACEMENT_STOP_MODE` and `MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT` to `strategy_smt.py`

- **WAVE**: 0 (parallel with Wave 1 — no dependencies between them)
- **AGENT_ROLE**: backend-dev
- **DEPENDS_ON**: []
- **BLOCKS**: [2.3]
- **PROVIDES**: Two new opt-in constants, both defaulting to off
- **IMPLEMENT**: In the `# ══ STRATEGY TUNING ══` block of `strategy_smt.py`, add after the `SMT_FILL_ENABLED` block (last Plan 2 constant):

```python
# ── Displacement entry quality controls (Plan 3) ──────────────────────────────
# Re-enables SMT_OPTIONAL experiments after adding the two fixes that Round 2
# identified as prerequisites: correct stop placement and hypothesis score gate.
#
# DISPLACEMENT_STOP_MODE: when True and smt_type=="displacement", sets initial
#   stop to the displacement bar's extreme (bar Low for long, bar High for short)
#   instead of the SMT structural stop. Mechanistically correct: the displacement
#   thesis fails when price closes back through the impulse bar.
#   Optimizer search space: [True, False]
DISPLACEMENT_STOP_MODE: bool = False

# MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT: minimum count of hypothesis rules that
#   must agree with signal direction for a displacement entry to be accepted.
#   0 = gate disabled (all displacement entries pass). Only effective when
#   SMT_OPTIONAL=True; has no effect on wick/body SMT entries.
#   Score range: 0–4 (pd_range_bias, week_zone, day_zone, trend_direction votes).
#   Optimizer search space: [0, 2, 3]
MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT: int = 0

# ── Partial exit level (Plan 3) ───────────────────────────────────────────────
# PARTIAL_EXIT_LEVEL_RATIO: linear interpolation between entry (0.0) and TP (1.0)
#   for the partial exit target. 0.5 = current hardcoded midpoint (no behaviour
#   change when PARTIAL_EXIT_ENABLED=True). Enables Round 3 experiments at
#   0.33 (earlier lock-in, higher probability) and 0.67 (later, more favorable RR).
#   Only read by manage_position() when PARTIAL_EXIT_ENABLED=True.
#   Optimizer search space: [0.33, 0.5, 0.67]
PARTIAL_EXIT_LEVEL_RATIO: float = 0.5

# ── Layer B hypothesis gate (Plan 3) ─────────────────────────────────────────
# FVG_LAYER_B_REQUIRES_HYPOTHESIS: when True, the FVG retracement add-on (Layer B)
#   is only accepted in sessions where hypothesis_score >= MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT.
#   ICT's two-leg accumulation model is a reversal/pullback structure — gating it to
#   hypothesis-confirmed sessions prevents Layer B entries in pure momentum days.
#   Requires TWO_LAYER_POSITION=True; has no effect when FVG is disabled.
#   Optimizer search space: [True, False]
FVG_LAYER_B_REQUIRES_HYPOTHESIS: bool = False
```

- **VALIDATE**: `uv run python -c "from strategy_smt import DISPLACEMENT_STOP_MODE, MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT, PARTIAL_EXIT_LEVEL_RATIO, FVG_LAYER_B_REQUIRES_HYPOTHESIS; print('ok')"`

**Wave 0 Checkpoint**: `uv run python -c "import strategy_smt; print('ok')"`

---

### Phase 1: New public function in hypothesis_smt.py

#### Task 1.1: ADD `compute_hypothesis_context()` to `hypothesis_smt.py`

Add a new public function immediately after `compute_hypothesis_direction()` (around line 491). The function:
- Calls the same internal functions as `compute_hypothesis_direction()` but also extracts the per-rule scalar fields
- Returns a flat dict with 6 keys (see Interface Contract above) or `None` on insufficient data
- Has zero side effects and is safe to call from backtest (no API calls)

```python
def _count_aligned_rules(r1_bias, w_zone, d_zone, trend, direction):
    """Count rule votes aligned with direction (0–4). 0 when direction is None/neutral."""
    if not direction or direction == "neutral":
        return 0
    score = 0
    if r1_bias == direction:
        score += 1
    if (w_zone == "discount" and direction == "long") or (w_zone == "premium" and direction == "short"):
        score += 1
    if (d_zone == "discount" and direction == "long") or (d_zone == "premium" and direction == "short"):
        score += 1
    if (trend == "bullish" and direction == "long") or (trend == "bearish" and direction == "short"):
        score += 1
    return score


def compute_hypothesis_context(
    mnq_1m_df: pd.DataFrame,
    hist_mnq_df: pd.DataFrame,
    date: datetime.date,
) -> Optional[dict]:
    """Return per-rule breakdown + final direction + score. No side effects. Safe for backtest."""
    try:
        r5 = _compute_rule5(mnq_1m_df, date)
        if r5 is None:
            return None
        r1 = _compute_rule1(mnq_1m_df, date)
        r2 = _compute_rule2(hist_mnq_df, date)
        direction = _weighted_vote(
            r1["pd_range_bias"],
            r5["week_zone"],
            r5["day_zone"],
            r2["trend_direction"],
        )
        r1_bias       = r1.get("pd_range_bias", "neutral")
        w_zone        = r5.get("week_zone")
        d_zone        = r5.get("day_zone")
        trend         = r2.get("trend_direction", "neutral")
        return {
            "direction":        direction,
            "pd_range_case":    r1.get("pd_range_case"),
            "pd_range_bias":    r1_bias,
            "week_zone":        w_zone,
            "day_zone":         d_zone,
            "trend_direction":  trend,
            "hypothesis_score": _count_aligned_rules(r1_bias, w_zone, d_zone, trend, direction),
        }
    except Exception:
        return None
```

**Validation**: `uv run python -c "import hypothesis_smt; print(hypothesis_smt.compute_hypothesis_context.__doc__)"`

---

### Phase 2: Backtest integration

#### Task 2.1: UPDATE `backtest_smt.py` — attach rule fields to trade records + expand TSV schema

**Part A — Import and per-session context call:**

In `backtest_smt.py`, update the import line:
```python
from hypothesis_smt import compute_hypothesis_direction
```
to:
```python
from hypothesis_smt import compute_hypothesis_direction, compute_hypothesis_context
```

Then, in the `run_backtest()` day loop where `_session_hyp_dir` is computed (around line 255), replace:
```python
_session_hyp_dir = compute_hypothesis_direction(mnq_df, _hist_mnq_df, day)
```
with:
```python
_session_hyp_ctx = compute_hypothesis_context(mnq_df, _hist_mnq_df, day)
_session_hyp_dir = _session_hyp_ctx["direction"] if _session_hyp_ctx else None
```

**Part B — Attach rule fields to signals:**

In both `WAITING_FOR_ENTRY` and `REENTRY_ELIGIBLE` blocks where `signal["matches_hypothesis"]` is set (around lines 344 and 383), also attach rule fields:

```python
signal["matches_hypothesis"] = (
    (signal.get("direction") == _session_hyp_dir)
    if _session_hyp_dir is not None else None
)
signal["hypothesis_direction"] = _session_hyp_dir
if _session_hyp_ctx is not None:
    signal["pd_range_case"]   = _session_hyp_ctx.get("pd_range_case")
    signal["pd_range_bias"]   = _session_hyp_ctx.get("pd_range_bias")
    signal["week_zone"]       = _session_hyp_ctx.get("week_zone")
    signal["day_zone"]        = _session_hyp_ctx.get("day_zone")
    signal["trend_direction"] = _session_hyp_ctx.get("trend_direction")
else:
    signal["hypothesis_direction"] = None
    signal["pd_range_case"]   = None
    signal["pd_range_bias"]   = None
    signal["week_zone"]       = None
    signal["day_zone"]        = None
    signal["trend_direction"] = None
```

Note: `_build_trade_record` copies the position dict into the trade via `{**signal, ...}` pattern (position is built from `{**signal, "entry_date": day, "contracts": contracts}`). The rule fields will flow through to the trade record because `_build_trade_record` doesn't strip unknown keys — it just picks named fields. But looking at `_build_trade_record` closely, it builds `trade = { ... }` with explicit keys. The rule fields need to be added explicitly at the call site.

**Correction**: `_build_trade_record` returns a trade dict with explicit fields. The rule fields on `position` are NOT automatically included. Instead, after calling `_build_trade_record()`, copy the rule fields from `position` to `trade`.

**Revised approach for Part B**: After the two existing lines:
```python
trade["matches_hypothesis"] = position.get("matches_hypothesis")
```
(which appear at lines 298, 442, 465), add:
```python
for _f in ("hypothesis_direction", "pd_range_case", "pd_range_bias",
           "week_zone", "day_zone", "trend_direction"):
    trade[_f] = position.get(_f)
```

This affects three locations in `run_backtest()`:
1. The `IN_TRADE` exit block (~line 298)
2. The end-of-session forced close (~line 442)
3. The end-of-backtest safety net (~line 465)

**Part C — Expand `_write_trades_tsv` fieldnames:**

In `_write_trades_tsv`, add the 6 new rule fields plus `fvg_detected` to `fieldnames` after `"matches_hypothesis"`:
```python
"hypothesis_direction", "pd_range_case", "pd_range_bias",
"week_zone", "day_zone", "trend_direction", "fvg_detected",
```

`fvg_detected` is a boolean field (True/False) set in the signal-building path: True when a valid FVG zone was identified at signal time (independent of whether `TWO_LAYER_POSITION` is enabled or Layer B fired). Set it in `_build_signal_from_bar()` or in the IDLE detection block — whichever constructs `_pending_fvg`. Default value: `False` when the FVG scan was skipped or found nothing.

```python
# In signal dict construction (IDLE → WAITING_FOR_ENTRY):
signal["fvg_detected"] = _pending_fvg is not None
```

Also propagate `fvg_detected` through the trade record at each of the 3 exit sites:
```python
for _f in (..., "fvg_detected"):
    trade[_f] = position.get(_f)
```

The DictWriter already uses `extrasaction="ignore"`, so no other changes needed. Add `restval=""` to the DictWriter constructor to handle any trade records that were built before the session context was available:
```python
w = csv.DictWriter(f, fieldnames=fieldnames, delimiter="\t",
                   extrasaction="ignore", restval="")
```

**Validation**: `uv run python -c "import backtest_smt; print('imports ok')"`

#### Task 2.2: ADD `HYPOTHESIS_FILTER` flag to `backtest_smt.py`

Add the constant to the mutable zone (near `TRADE_DIRECTION`, around line 50 of the backtest file):

```python
# When True, only signals where matches_hypothesis == True are taken.
# Default False = current behaviour. Set True to test Outcome B walk-forward validation.
# Optimization search space: [True, False]
HYPOTHESIS_FILTER: bool = False
```

Import it in the strategy import block (it's already a local constant so no import needed; it lives in `backtest_smt.py` itself).

In `run_backtest()`, in the `WAITING_FOR_ENTRY` and `REENTRY_ELIGIBLE` blocks, after `signal` is built and `signal["matches_hypothesis"]` is set, add the filter gate:

```python
if HYPOTHESIS_FILTER and signal.get("matches_hypothesis") is not True:
    state = "IDLE"
    pending_direction = None
    anchor_close = None
    continue
```

Place this immediately after the `matches_hypothesis` assignment block, before the contracts/position assignment.

**Validation**: `uv run python -c "import backtest_smt; assert hasattr(backtest_smt, 'HYPOTHESIS_FILTER')"`

Also extend `_write_trades_tsv` fieldnames to include `hypothesis_score` (after the 6 existing new fields from Task 2.1).

**Wave 2 Checkpoint**: `uv run python -m pytest tests/test_hypothesis_smt.py tests/test_smt_backtest.py -q`

---

#### Task 2.3: UPDATE `backtest_smt.py` — displacement stop override + hypothesis score gate

- **WAVE**: 2
- **AGENT_ROLE**: backend-dev
- **DEPENDS_ON**: [0.1, 1.1, 2.1]
- **BLOCKS**: [4.1]
- **PROVIDES**: `DISPLACEMENT_STOP_MODE` stop logic + `MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT` gate in `run_backtest()`

**Part A — Imports**: Add `DISPLACEMENT_STOP_MODE, MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT` to the `from strategy_smt import` line.

**Part B — Displacement bar extreme in IDLE state**: Plan 2 already detects displacement entries in the IDLE state. After direction is resolved as "displacement", extract and stash the displacement bar extreme:
```python
# Stash displacement bar extreme for stop calculation (used by DISPLACEMENT_STOP_MODE)
if _smt_type == "displacement":
    if direction == "long":
        _displacement_bar_extreme = float(mnq_reset.iloc[bar_idx]["Low"])
    else:
        _displacement_bar_extreme = float(mnq_reset.iloc[bar_idx]["High"])
else:
    _displacement_bar_extreme = None
```
Store `_displacement_bar_extreme` so it is available in WAITING_FOR_ENTRY (it can be stored as a local variable alongside `_pending_fvg`).

**Part C — Score gate in IDLE state**: After direction is resolved and `_smt_type == "displacement"`, add the score gate:
```python
if _smt_type == "displacement" and MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT > 0:
    _score = _session_hyp_ctx.get("hypothesis_score", 0) if _session_hyp_ctx else 0
    if _score < MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT:
        continue  # reject this displacement entry; resume scanning
```
Place this AFTER direction resolution, BEFORE the signal / WAITING_FOR_ENTRY transition.

**Part D — Displacement stop in WAITING_FOR_ENTRY**: When position dict is created and entry fires, add:
```python
if DISPLACEMENT_STOP_MODE and position.get("smt_type") == "displacement":
    _extreme = _displacement_bar_extreme  # captured in IDLE state
    if _extreme is not None:
        if position["direction"] == "long":
            position["stop_price"] = _extreme - STRUCTURAL_STOP_BUFFER_PTS
        else:
            position["stop_price"] = _extreme + STRUCTURAL_STOP_BUFFER_PTS
```
Place this immediately after the position dict is created, before the state transitions to IN_TRADE.

**Part E — `PARTIAL_EXIT_LEVEL_RATIO` in `manage_position()`**: In `strategy_smt.py`, update the partial-exit target computation in `manage_position()`. Currently the partial level is:
```python
partial_level = entry + (tp - entry) * 0.5  # hardcoded midpoint
```
Replace with:
```python
partial_level = entry + (tp - entry) * PARTIAL_EXIT_LEVEL_RATIO
```
(For short positions the arithmetic is analogous — entry - (entry - tp) * ratio). Import `PARTIAL_EXIT_LEVEL_RATIO` is already available since it lives in the same file.

**Part F — `FVG_LAYER_B_REQUIRES_HYPOTHESIS` gate in Layer B entry block**: In `backtest_smt.py`, add `FVG_LAYER_B_REQUIRES_HYPOTHESIS` to the `from strategy_smt import` line. In the Layer B / FVG retracement entry block (where the add-on position is created), add:
```python
if FVG_LAYER_B_REQUIRES_HYPOTHESIS:
    _score = _session_hyp_ctx.get("hypothesis_score", 0) if _session_hyp_ctx else 0
    if _score < MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT:
        _pending_fvg = None  # discard Layer B opportunity; keep Layer A position
        continue
```
Place this check at the top of the FVG retracement entry block, before the contracts/position assignment. Reuses the same threshold constant for consistency; both constants live in `strategy_smt.py`.

**Part G — TSV fieldnames**: No additional columns needed beyond `fvg_detected` (handled in Task 2.1). `smt_type` is already in the TSV from Plan 2. The new constants control behaviour.

- **VALIDATE**:
  ```bash
  uv run python -c "import backtest_smt; assert hasattr(backtest_smt, 'DISPLACEMENT_STOP_MODE'); print('ok')"
  uv run python -c "import backtest_smt; assert hasattr(backtest_smt, 'MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT'); print('ok')"
  uv run python -c "from strategy_smt import PARTIAL_EXIT_LEVEL_RATIO, FVG_LAYER_B_REQUIRES_HYPOTHESIS; print('ok')"
  ```

---

### Phase 3: Analysis script

#### Task 3.1: CREATE `analyze_hypothesis.py`

Pattern from `analyze_gaps.py`: top-level functions, `if __name__ == "__main__":` block.

**Public interface:**
```python
def load_trades(path: str = "trades.tsv") -> pd.DataFrame
def split_by_alignment(df: pd.DataFrame) -> dict[str, pd.DataFrame]
    # returns {"aligned": df, "misaligned": df, "no_hypothesis": df}
def compute_group_stats(group: pd.DataFrame) -> dict
    # returns: count, win_rate, avg_pnl, avg_rr, exit_types dict
def compute_fold_stats(df: pd.DataFrame, fold_boundaries: list[str]) -> list[dict]
    # one entry per fold — aligned/misaligned split within each fold window
def print_analysis(df: pd.DataFrame) -> None
    # Prints the full formatted alignment report to stdout
```

**`load_trades()`**: reads `path` with `pd.read_csv(path, sep="\t", dtype=str)`, coerces `pnl` to float and `matches_hypothesis` to boolean/None. Returns empty DataFrame if file not found (prints a warning).

**`split_by_alignment()`**: partitions on `matches_hypothesis` column:
- `True` (string or bool) → "aligned"
- `False` (string or bool) → "misaligned"
- `None`, `""`, missing → "no_hypothesis"

**`compute_group_stats(group)`**: 
```python
{
    "count": len(group),
    "pct_of_total": ...,   # passed in or computed from caller
    "win_rate": ...,       # pnl > 0
    "avg_pnl": ...,
    "avg_win": ...,
    "avg_loss": ...,
    "avg_rr": avg_win / abs(avg_loss),
    "exit_types": dict(group["exit_type"].value_counts()),
}
```

**`compute_fold_stats(df, fold_boundaries)`**: `fold_boundaries` is a list of ISO date strings like `["2024-01-01", "2024-04-01", ...]`. Each pair (fold_boundaries[i], fold_boundaries[i+1]) defines a fold. For each fold, filter `df` by `entry_date` and call `split_by_alignment` + `compute_group_stats` per group. Returns a list of fold dicts.

**`print_analysis(df)`**:
1. Overall summary: total trades, % aligned/misaligned/none
2. Group stats table: one row per group (aligned/misaligned/no_hypothesis)
3. Per-fold consistency check: "aligned win rate > misaligned win rate?" for each fold — prints Y/N + the edge (aligned_wr − misaligned_wr)
4. Rule decomposition section: for each of the 5 rule fields (pd_range_case, pd_range_bias, week_zone, day_zone, trend_direction), print win rate by value (requires the new columns to be present in TSV; if absent, skip with a note)
5. Meaningful-edge verdict: prints "POTENTIAL EDGE" if aligned win rate > misaligned win rate by ≥10pp AND consistent in ≥4 of 6 folds, otherwise "NO CLEAR EDGE"

**`__main__` block**:
```python
if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "trades.tsv"
    df = load_trades(path)
    if df.empty:
        print(f"No trades loaded from {path}")
        sys.exit(1)
    print_analysis(df)
```

**Script runnability criterion**: `uv run python analyze_hypothesis.py trades.tsv` must complete without raising an exception (even if `trades.tsv` doesn't exist — it prints the "No trades loaded" message and exits cleanly).

---

### Phase 4: Tests

#### Task 4.1: CREATE `tests/test_hypothesis_analysis.py`

**Test cases (30 total):**

**For `compute_hypothesis_context` (4 tests):**
1. `test_compute_hypothesis_context_returns_dict` — valid 1m df with sufficient data; assert return is dict with all 7 keys including `hypothesis_score`; `direction` is one of ("long", "short", "neutral")
2. `test_compute_hypothesis_context_returns_none_on_missing_tdo` — empty hist_mnq_df and no 09:30 bar; assert returns None
3. `test_compute_hypothesis_context_consistent_with_direction` — assert `context["direction"]` matches `compute_hypothesis_direction()` for the same inputs
4. `test_hypothesis_score_counts_aligned_rules` — construct context where 3 of 4 rules align with direction; assert `hypothesis_score == 3`. Use a neutral direction → assert score == 0.

**For `HYPOTHESIS_FILTER` in backtest (2 tests):**
4. `test_hypothesis_filter_false_does_not_filter` — with HYPOTHESIS_FILTER=False, a signal where matches_hypothesis=False is still accepted (trade appears in output)
5. `test_hypothesis_filter_true_skips_misaligned` — with HYPOTHESIS_FILTER=True, a signal where matches_hypothesis=False is skipped; only aligned or no-hypothesis signals pass

**For rule fields in trade records (2 tests):**
6. `test_trade_record_has_rule_fields` — run a minimal backtest with synthetic data; assert returned trade records include keys `pd_range_case`, `pd_range_bias`, `week_zone`, `day_zone`, `trend_direction`, `hypothesis_direction`
7. `test_write_trades_tsv_includes_rule_fields` — mock file open; call `_write_trades_tsv([sample_trade])` and assert header row includes the 6 new fieldnames

**For `analyze_hypothesis.py` functions (10 tests):**
8. `test_load_trades_reads_tsv` — write a temp TSV file, call `load_trades(path)`, assert returns DataFrame with correct columns and pnl as float
9. `test_load_trades_missing_file_returns_empty` — call `load_trades("nonexistent.tsv")`, assert returns empty DataFrame (no exception)
10. `test_split_by_alignment_three_groups` — DataFrame with True/False/None in matches_hypothesis; assert three groups returned with correct counts
11. `test_split_by_alignment_string_true_false` — TSV files store "True"/"False" as strings; assert split handles these correctly
12. `test_compute_group_stats_all_winners` — all pnl > 0; assert win_rate = 1.0, avg_loss = 0.0
13. `test_compute_group_stats_mixed` — 2 wins ($50 each), 1 loss (-$25); assert win_rate=2/3, avg_pnl=25, avg_rr≈4.0
14. `test_compute_group_stats_empty` — empty DataFrame; assert returns dict with count=0 and no exceptions
15. `test_compute_fold_stats_two_folds` — 6 trades across 2 date-bounded folds; assert 2 entries returned, each with aligned/misaligned stats
16. `test_print_analysis_runs_without_error` — call `print_analysis(df)` with a mixed DataFrame; assert no exception (uses capsys to capture output)
17. `test_print_analysis_no_rule_columns` — call `print_analysis(df)` on a TSV without the rule fields (legacy format); assert it skips rule section gracefully

**For displacement entry controls (6 tests):**
18. `test_displacement_stop_overrides_to_bar_low_for_long` — `DISPLACEMENT_STOP_MODE=True`, long displacement; assert position `stop_price` == displacement bar Low − STRUCTURAL_STOP_BUFFER_PTS (not the SMT structural stop)
19. `test_displacement_stop_overrides_to_bar_high_for_short` — same for short direction
20. `test_displacement_stop_disabled_when_constant_false` — `DISPLACEMENT_STOP_MODE=False`; assert stop is NOT overridden (uses structural stop path)
21. `test_displacement_blocked_when_score_below_threshold` — `MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT=2`, mock `_session_hyp_ctx["hypothesis_score"]=1`; assert displacement entry is skipped (no trade produced)
22. `test_displacement_allowed_when_score_meets_threshold` — `MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT=2`, mock score=2; assert displacement entry is accepted
23. `test_displacement_allowed_when_min_score_zero` — `MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT=0`; assert gate is disabled regardless of score (wick SMT entries unaffected by this constant)

**For `PARTIAL_EXIT_LEVEL_RATIO` (2 tests):**
24. `test_partial_exit_level_ratio_midpoint` — `PARTIAL_EXIT_LEVEL_RATIO=0.5`, long trade with entry=100 TP=110; assert partial exit level == 105.0 (unchanged from current behaviour)
25. `test_partial_exit_level_ratio_custom` — `PARTIAL_EXIT_LEVEL_RATIO=0.33`, same inputs; assert partial exit level == 103.3 (approx); for short: entry=110 TP=100, ratio=0.67 → level ≈ 103.3

**For `fvg_detected` TSV field (1 test):**
26. `test_fvg_detected_field_in_trade_record` — run a backtest or mock signal path where FVG scan runs; assert trade record contains `fvg_detected` key with a boolean value (True when `_pending_fvg is not None`, False otherwise); assert field appears in `_write_trades_tsv` header

**For `FVG_LAYER_B_REQUIRES_HYPOTHESIS` (3 tests):**
27. `test_layer_b_blocked_when_hypothesis_gate_enabled_low_score` — `FVG_LAYER_B_REQUIRES_HYPOTHESIS=True`, `MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT=2`, score=1; assert Layer B entry is NOT created (only Layer A position exists)
28. `test_layer_b_allowed_when_hypothesis_gate_enabled_high_score` — `FVG_LAYER_B_REQUIRES_HYPOTHESIS=True`, `MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT=2`, score=3; assert Layer B entry IS created when FVG retracement reaches the zone
29. `test_layer_b_gate_disabled_when_constant_false` — `FVG_LAYER_B_REQUIRES_HYPOTHESIS=False`; assert Layer B behaviour is unchanged regardless of hypothesis score (gate not applied)

**Test fixture helpers:**
```python
def _make_trades_df(rows: list[dict]) -> pd.DataFrame:
    """Build a minimal trades DataFrame matching trades.tsv schema."""
    defaults = {"entry_date": "2025-01-02", "direction": "long",
                "pnl": 50.0, "exit_type": "exit_tp",
                "matches_hypothesis": True, "hypothesis_direction": "long",
                "hypothesis_score": 3,
                "pd_range_case": "1.3", "pd_range_bias": "long",
                "week_zone": "discount", "day_zone": "discount",
                "trend_direction": "bullish", "fvg_detected": False}
    return pd.DataFrame([{**defaults, **r} for r in rows])
```

---

## TESTING STRATEGY

| What you're testing | Tool | Status |
|---|---|---|
| `compute_hypothesis_context()` correctness | pytest | ✅ Automated |
| `hypothesis_score` counts aligned rules correctly | pytest | ✅ Automated |
| `HYPOTHESIS_FILTER` gate in run_backtest | pytest (mock data) | ✅ Automated |
| Rule fields in trade records | pytest (mock data) | ✅ Automated |
| `_write_trades_tsv` new fieldnames | pytest (mock file) | ✅ Automated |
| `load_trades()` | pytest (tmp file) | ✅ Automated |
| `split_by_alignment()` | pytest | ✅ Automated |
| `compute_group_stats()` | pytest | ✅ Automated |
| `compute_fold_stats()` | pytest | ✅ Automated |
| `print_analysis()` (no crash) | pytest (capsys) | ✅ Automated |
| `analyze_hypothesis.py` script runs | pytest (subprocess) | ✅ Automated |
| `DISPLACEMENT_STOP_MODE` stop override (long + short) | pytest (mock backtest) | ✅ Automated |
| `DISPLACEMENT_STOP_MODE=False` leaves stop unchanged | pytest | ✅ Automated |
| `MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT` blocks low-score entries | pytest (mock backtest) | ✅ Automated |
| `MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT=0` gate disabled | pytest | ✅ Automated |
| `PARTIAL_EXIT_LEVEL_RATIO` midpoint and custom ratio | pytest (unit) | ✅ Automated |
| `fvg_detected` field in trade records and TSV | pytest (mock) | ✅ Automated |
| `FVG_LAYER_B_REQUIRES_HYPOTHESIS` blocks/allows Layer B | pytest (mock backtest) | ✅ Automated |
| Live backtest with new fields end-to-end | Manual | ⚠️ Manual |

#### Manual Test 1: Live backtest with new TSV fields

**Why Manual**: Requires `data/historical/MNQ.parquet` and `data/historical/MES.parquet` on disk (Databento data, not committed to repo).

**Steps**:
1. `uv run python backtest_smt.py`
2. Inspect `trades.tsv` header: assert `pd_range_case`, `week_zone`, `day_zone`, `trend_direction`, `hypothesis_direction` columns are present
3. Sample 5 rows: verify the rule fields are populated (not all empty/None) for sessions that had sufficient data

**Expected**: `trades.tsv` contains all 27 columns; rule fields are non-null for most records (may be null for sessions at the start of the data window where prior-day data is unavailable).

#### Manual Test 2: analyze_hypothesis.py live run

**Why Manual**: Requires `trades.tsv` produced from a live backtest run (above).

**Steps**:
1. `uv run python analyze_hypothesis.py`
2. Verify output shows: overall aligned/misaligned counts, per-group win rates, per-fold consistency, rule decomposition section
3. Verify the script exits cleanly (exit code 0)

**Expected**: Readable analysis report with no Python tracebacks.

### Test Automation Summary

| | Count | % |
|---|---|---|
| ✅ pytest | 30 | 94% |
| ⚠️ Manual | 2 | 6% |
| **Total** | 32 | 100% |

**Manual test justification**: Both require Databento parquet files not available in the repo. All code paths are covered by unit/integration tests using synthetic data.

---

## STEP-BY-STEP TASKS

### WAVE 1: Foundation

#### Task 1.1: ADD `compute_hypothesis_context()` to `hypothesis_smt.py`

- **WAVE**: 1
- **AGENT_ROLE**: backend-dev
- **DEPENDS_ON**: []
- **BLOCKS**: [2.1]
- **PROVIDES**: `compute_hypothesis_context(mnq_1m_df, hist_mnq_df, date) -> Optional[dict]` with 6 keys
- **IMPLEMENT**: Insert the function body immediately after `compute_hypothesis_direction` (after line ~491). Reuse `_compute_rule1`, `_compute_rule5`, `_compute_rule2`, `_weighted_vote`. No new imports needed.
- **PATTERN**: Mirror `compute_hypothesis_direction` structure (`hypothesis_smt.py:472–490`)
- **VALIDATE**: `uv run python -c "from hypothesis_smt import compute_hypothesis_context; print('ok')"`

**Wave 1 Checkpoint**: `uv run python -m pytest tests/test_hypothesis_smt.py -q`

---

### WAVE 2: Backtest Integration

#### Task 2.1: UPDATE `backtest_smt.py` — rule fields in trade records + TSV schema

- **WAVE**: 2
- **AGENT_ROLE**: backend-dev
- **DEPENDS_ON**: [1.1]
- **BLOCKS**: [3.1, 4.1]
- **PROVIDES**: Trade records with 6 new fields; `trades.tsv` with 27 columns
- **USES_FROM_WAVE_1**: `compute_hypothesis_context()` from `hypothesis_smt.py`
- **IMPLEMENT**:
  1. Update import: add `compute_hypothesis_context` to the `from hypothesis_smt import` line
  2. In `run_backtest()` day loop: replace `compute_hypothesis_direction(...)` call with `compute_hypothesis_context(...)`, extract `_session_hyp_dir` from context dict
  3. In WAITING_FOR_ENTRY block: after setting `signal["matches_hypothesis"]`, assign `signal["hypothesis_direction"]` and 5 rule fields from `_session_hyp_ctx` (or None if ctx is None)
  4. In REENTRY_ELIGIBLE block: same rule-field assignments
  5. In all three `trade["matches_hypothesis"] = position.get(...)` locations: add the 6-field loop below each one
  6. In `_write_trades_tsv`: extend `fieldnames` list with 6 new column names; add `restval=""` to DictWriter constructor
- **VALIDATE**: `uv run python -c "import backtest_smt; print('imports ok')"`

#### Task 2.2: ADD `HYPOTHESIS_FILTER` flag to `backtest_smt.py`

- **WAVE**: 2
- **AGENT_ROLE**: backend-dev
- **DEPENDS_ON**: [1.1]
- **BLOCKS**: [4.1]
- **PROVIDES**: `HYPOTHESIS_FILTER: bool` constant + gate logic in `run_backtest()`
- **IMPLEMENT**:
  1. Add `HYPOTHESIS_FILTER: bool = False` constant near `TRADE_DIRECTION` with search-space comment
  2. In WAITING_FOR_ENTRY block, after the rule-field assignment from Task 2.1, add the gate: if `HYPOTHESIS_FILTER and signal.get("matches_hypothesis") is not True`, reset to IDLE and `continue`
  3. Same gate in REENTRY_ELIGIBLE block
- **VALIDATE**: `uv run python -c "import backtest_smt; assert not backtest_smt.HYPOTHESIS_FILTER; print('ok')"`

**Wave 2 Checkpoint**: `uv run python -m pytest tests/test_hypothesis_smt.py tests/test_smt_backtest.py -q`

---

### WAVE 3: Analysis Script

#### Task 3.1: CREATE `analyze_hypothesis.py`

- **WAVE**: 3
- **AGENT_ROLE**: backend-dev
- **DEPENDS_ON**: [2.1, 2.2]
- **BLOCKS**: [4.1]
- **PROVIDES**: `analyze_hypothesis.py` with public API + `__main__` CLI
- **IMPLEMENT**: Create the file as specified in Phase 3 above. Functions: `load_trades`, `split_by_alignment`, `compute_group_stats`, `compute_fold_stats`, `print_analysis`. All stdout output must use ASCII-safe characters only (no Unicode box-drawing, no emojis) for cross-platform compatibility.
- **PATTERN**: Mirror `analyze_gaps.py` structure
- **VALIDATE**: `uv run python -c "import analyze_hypothesis; print('ok')"`; `uv run python analyze_hypothesis.py` exits with code 0 (no trades.tsv → "No trades loaded" message, exit 1 is fine)

**Wave 3 Checkpoint**: `uv run python analyze_hypothesis.py` — should print "No trades loaded from trades.tsv" and exit 1 without traceback (if trades.tsv absent), OR print analysis if trades.tsv present.

---

### WAVE 4: Tests

#### Task 4.1: CREATE `tests/test_hypothesis_analysis.py`

- **WAVE**: 4
- **AGENT_ROLE**: test-engineer
- **DEPENDS_ON**: [1.1, 2.1, 2.2, 3.1]
- **PROVIDES**: 30 automated tests covering all new code paths
- **IMPLEMENT**: Create the test file as specified in Phase 4 above. Use `tmp_path` fixture for TSV file tests. Mock `backtest_smt.HYPOTHESIS_FILTER` with `unittest.mock.patch`. For the run_backtest filter tests, use the existing synthetic data helpers or build minimal parquet fixtures inline.
- **VALIDATE**: `uv run python -m pytest tests/test_hypothesis_analysis.py -v`

**Final Checkpoint**: `uv run python -m pytest -q` — all pre-existing tests still pass; 30 new tests pass.

---

## VALIDATION COMMANDS

### Level 1: Import Check

```bash
uv run python -c "from hypothesis_smt import compute_hypothesis_context; print('ok')"
uv run python -c "import backtest_smt; assert hasattr(backtest_smt, 'HYPOTHESIS_FILTER'); print('ok')"
uv run python -c "import analyze_hypothesis; print('ok')"
```

### Level 2: Unit Tests

```bash
uv run python -m pytest tests/test_hypothesis_analysis.py -v
```

### Level 3: Regression Tests

```bash
uv run python -m pytest tests/test_hypothesis_smt.py tests/test_smt_backtest.py -q
uv run python -m pytest -q --tb=short
```

### Level 4: Manual Validation

1. `uv run python backtest_smt.py` (requires Databento parquets)
2. Inspect `trades.tsv` for new columns
3. `uv run python analyze_hypothesis.py` for the analysis report

---

## ACCEPTANCE CRITERIA

### Functional
- [ ] `compute_hypothesis_context(mnq_1m_df, hist_mnq_df, date)` is importable from `hypothesis_smt` and returns a dict with exactly these keys: `direction`, `pd_range_case`, `pd_range_bias`, `week_zone`, `day_zone`, `trend_direction`, `hypothesis_score`
- [ ] `compute_hypothesis_context()` returns `None` when Rule 5 cannot be computed (same conditions as `compute_hypothesis_direction`)
- [ ] `compute_hypothesis_context()["direction"]` equals the result of `compute_hypothesis_direction()` for the same inputs
- [ ] `hypothesis_score` is an int in 0–4; equals count of (pd_range_bias, week_zone, day_zone, trend_direction) votes aligned with `direction`; is 0 when `direction` is None or "neutral"
- [ ] All three `trade["matches_hypothesis"]` assignment sites in `run_backtest()` also attach the 7 fields (`hypothesis_direction`, `pd_range_case`, `pd_range_bias`, `week_zone`, `day_zone`, `trend_direction`, `hypothesis_score`)
- [ ] `trades.tsv` written by `_write_trades_tsv` contains all 7 new column headers
- [ ] `HYPOTHESIS_FILTER = False` constant exists in `backtest_smt.py`
- [ ] When `HYPOTHESIS_FILTER = True`, signals where `matches_hypothesis != True` are skipped in both WAITING_FOR_ENTRY and REENTRY_ELIGIBLE states
- [ ] `DISPLACEMENT_STOP_MODE = False` constant exists in `strategy_smt.py`
- [ ] When `DISPLACEMENT_STOP_MODE = True` and `smt_type == "displacement"`, the initial `stop_price` is set to displacement bar Low − `STRUCTURAL_STOP_BUFFER_PTS` (long) or displacement bar High + `STRUCTURAL_STOP_BUFFER_PTS` (short); wick/body SMT entries are unaffected
- [ ] `MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT = 0` constant exists in `strategy_smt.py`
- [ ] When `MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT > 0` and `SMT_OPTIONAL = True`, displacement entries are rejected when `_session_hyp_ctx["hypothesis_score"] < MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT`; wick/body SMT entries are never gated by this constant
- [ ] When `MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT = 0`, the score gate is fully disabled and all displacement entries pass
- [ ] `PARTIAL_EXIT_LEVEL_RATIO = 0.5` constant exists in `strategy_smt.py`; default value preserves current behaviour (midpoint between entry and TP)
- [ ] `manage_position()` uses `PARTIAL_EXIT_LEVEL_RATIO` to compute the partial exit target level (linear interpolation between entry and TP); only active when `PARTIAL_EXIT_ENABLED=True`
- [ ] `fvg_detected` field exists in every trade record; is `True` when a valid FVG zone was identified at signal time, `False` otherwise; appears in `trades.tsv` header
- [ ] `FVG_LAYER_B_REQUIRES_HYPOTHESIS = False` constant exists in `strategy_smt.py`
- [ ] When `FVG_LAYER_B_REQUIRES_HYPOTHESIS = True` and `_session_hyp_ctx["hypothesis_score"] < MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT`, Layer B (FVG retracement add-on) is NOT entered; Layer A position is unaffected
- [ ] When `FVG_LAYER_B_REQUIRES_HYPOTHESIS = False`, Layer B behaviour is unchanged (gate not applied)
- [ ] `analyze_hypothesis.py` exports: `load_trades`, `split_by_alignment`, `compute_group_stats`, `compute_fold_stats`, `print_analysis`
- [ ] `split_by_alignment()` correctly handles both boolean and string representations of `matches_hypothesis` ("True"/"False" from TSV)
- [ ] `print_analysis()` output contains only ASCII characters (no Unicode box-drawing)

### Error Handling
- [ ] `compute_hypothesis_context()` returns `None` (no exception) when called with an empty DataFrame
- [ ] `load_trades("nonexistent.tsv")` returns an empty DataFrame without raising an exception
- [ ] `compute_group_stats(empty_df)` returns a dict with `count=0` without raising an exception
- [ ] `uv run python analyze_hypothesis.py` with no `trades.tsv` present exits with a user-readable message and no Python traceback
- [ ] `DISPLACEMENT_STOP_MODE = True` with `position["fvg_high"] = None` (no displacement bar extreme captured) does not raise an exception — falls back to structural stop

### Integration / E2E
- [ ] `uv run python analyze_hypothesis.py trades.tsv` completes without traceback when `trades.tsv` is present
- [ ] The full test suite (`uv run python -m pytest -q`) shows no regressions vs. baseline (551 collected, 2 pre-existing failures unchanged)

### Validation
- [ ] All 30 new tests in `tests/test_hypothesis_analysis.py` pass — verified by: `uv run python -m pytest tests/test_hypothesis_analysis.py -v`
- [ ] Pre-existing hypothesis tests still pass — verified by: `uv run python -m pytest tests/test_hypothesis_smt.py -q`
- [ ] Pre-existing backtest tests still pass — verified by: `uv run python -m pytest tests/test_smt_backtest.py -q`
- [ ] Import checks pass — verified by: `uv run python -c "from hypothesis_smt import compute_hypothesis_context; from strategy_smt import DISPLACEMENT_STOP_MODE, MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT, PARTIAL_EXIT_LEVEL_RATIO, FVG_LAYER_B_REQUIRES_HYPOTHESIS; import backtest_smt; import analyze_hypothesis; print('ok')"`

### Out of Scope
- ICT strategy parameters (TWO validity check, PD range case filter, overnight sweep gate, FVG confluence) — not added without Phase 2–3 analysis evidence
- `HYPOTHESIS_FILTER = True` as the live or optimization default — remains `False`
- Live backtest run validation (requires Databento parquets not in repo)
- Session qualitative review tooling (Step 2C from strategy-update.md) — manual process, no code
- Displacement entry re-test experiments — these are Plan 3 infrastructure; the actual Round 3 backtest experiments run after plan execution

---

## COMPLETION CHECKLIST

- [ ] Task 1.1: `compute_hypothesis_context()` added to `hypothesis_smt.py`
- [ ] Task 2.1: Rule fields attached in `run_backtest()` at all 3 exit sites; TSV fieldnames expanded
- [ ] Task 2.2: `HYPOTHESIS_FILTER` constant + gate logic added to `backtest_smt.py`
- [ ] Task 3.1: `analyze_hypothesis.py` created with all public functions + `__main__` CLI
- [ ] Task 0.1: `DISPLACEMENT_STOP_MODE` and `MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT` added to `strategy_smt.py`
- [ ] Task 1.1: `compute_hypothesis_context()` added to `hypothesis_smt.py` with `hypothesis_score` field and `_count_aligned_rules()` helper
- [ ] Task 2.1: Rule fields + `hypothesis_score` attached at all 3 exit sites; TSV fieldnames expanded to include `hypothesis_score`
- [ ] Task 2.2: `HYPOTHESIS_FILTER` constant + gate logic added to `backtest_smt.py`
- [ ] Task 2.3: Displacement bar extreme captured in IDLE state; stop override applied in WAITING_FOR_ENTRY when `DISPLACEMENT_STOP_MODE=True`; score gate added in IDLE when `MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT > 0`
- [ ] Task 3.1: `analyze_hypothesis.py` created with all public functions + `__main__` CLI
- [ ] Task 4.1: `tests/test_hypothesis_analysis.py` created with 23 tests (17 original + 6 displacement controls)
- [ ] Level 1 import checks pass
- [ ] Level 2 unit tests pass (23/23)
- [ ] Level 3 regression tests pass (no new failures)
- [ ] Task 0.1 (extended): `PARTIAL_EXIT_LEVEL_RATIO` and `FVG_LAYER_B_REQUIRES_HYPOTHESIS` added to `strategy_smt.py`; `manage_position()` updated to use `PARTIAL_EXIT_LEVEL_RATIO`
- [ ] Task 2.1 (extended): `fvg_detected` field added to signal dict and trade records at all 3 exit sites; `fvg_detected` added to `_write_trades_tsv` fieldnames
- [ ] Task 2.3 (extended): `FVG_LAYER_B_REQUIRES_HYPOTHESIS` gate added to Layer B entry block; `FVG_LAYER_B_REQUIRES_HYPOTHESIS` imported in `backtest_smt.py`
- [ ] Task 4.1 (extended): 30 tests total (23 original + 7 new for ratio/fvg_detected/layer-b-gate)
- [ ] All acceptance criteria checked
- [ ] **⚠️ Debug logs added during execution REMOVED (keep pre-existing)**
- [ ] **⚠️ CRITICAL: Changes UNSTAGED — NOT committed**

---

## NOTES

### Rule fields as trade-record columns vs. separate session TSV

The strategy-update.md mentions "adding a `session_context_summary` column (or writing a separate per-session context TSV)". We chose the **per-trade column** approach because:
- It keeps the analysis self-contained in a single TSV file
- It allows per-trade analysis without joining files
- The rule values are scalars (strings/None), so TSV serialization is trivial
- A separate session context TSV would require session-to-trade joins and is harder for an agent to parse

This is a design decision that can be revisited if deeper session-level analysis is needed (e.g., sessions where a signal fired vs. sessions with no signal). For that use case, a per-session TSV remains an option for a future plan.

### HYPOTHESIS_FILTER placement

The `HYPOTHESIS_FILTER` constant is placed in the mutable zone of `backtest_smt.py` (near `TRADE_DIRECTION`), not in `strategy_smt.py`. This is intentional: the filter is a backtest-harness concern (which signals to accept for the P&L computation), not a strategy signal-generation concern. `strategy_smt.py` already produces `matches_hypothesis` on every signal; the decision to act on it is a harness decision.

### Fold boundary inference in analyze_hypothesis.py

`compute_fold_stats()` requires fold boundaries. The analysis script does not hard-code them; instead, it infers boundaries from the actual distribution of `entry_date` in the trades file using `pd.cut` / quantile bucketing when no explicit boundaries are provided. An optional `fold_boundaries` parameter allows the user to pass the exact boundaries from their last backtest run for a precise per-fold breakdown.

### Displacement constants placement

`DISPLACEMENT_STOP_MODE` and `MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT` live in `strategy_smt.py` (not `backtest_smt.py`) because they control signal-generation behaviour (stop placement, entry gating) rather than harness-level filtering. `HYPOTHESIS_FILTER` lives in `backtest_smt.py` because it is a harness decision (which signals to accept for P&L accounting). The score gate (`MIN_HYPOTHESIS_SCORE_FOR_DISPLACEMENT`) straddles this boundary — it reads a harness-computed value (`_session_hyp_ctx`) but acts as a signal quality filter. It lives in `strategy_smt.py` so the optimizer can tune it alongside `SMT_OPTIONAL` and `MIN_DISPLACEMENT_PTS`, its natural companions.

### ASCII-only output requirement

`print_analysis()` uses only ASCII table-drawing characters (`-`, `|`, `+`) and no Unicode. This ensures the script works correctly on Windows cp1252 terminals without `UnicodeEncodeError`. (See global CLAUDE.md pattern: "Windows cp1252 encoding fix").
