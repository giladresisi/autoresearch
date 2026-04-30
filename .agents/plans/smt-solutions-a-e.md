# Feature: SMT Solutions A–E — Signal Quality & State Machine Hardening

**⚠️ EXECUTION RULES — READ FIRST:**
- Implement ALL changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
- Only make code changes — no git operations

Validate documentation and codebase patterns before implementing. Match naming of existing utils, types, and models. Import from correct files.

---

## Feature Description

Implements five complementary signal quality improvements to the SMT Divergence strategy, addressing four structural root causes responsible for the largest losses in the backtest:

- **Solution A**: `divergence_score()` — scores each divergence on sweep/miss/body magnitude + hypothesis alignment. Stored as `pending_div_score` and used for replacement decisions.
- **Solution B**: Hypothesis replacement — while in `WAITING_FOR_ENTRY`, a superior new divergence can replace the pending one. Includes provisional flag (displacement = provisional), score decay (time × adverse move), and a `REPLACE_THRESHOLD` gate.
- **Solution C**: Inverted stop guard — rejects any signal where `stop_price >= entry_price` (long) or `stop_price <= entry_price` (short). Last-line safety net; independent of A+B.
- **Solution D**: Hypothesis invalidation — if price moves `HYPOTHESIS_INVALIDATION_PTS` against the pending direction while waiting for confirmation, abandon hypothesis and reset to IDLE.
- **Solution E**: Early bar skip — skip divergence detection on the first 3 bars of the session (bar_idx < 3) when session extremes are undefined by fewer than 4 data points.

**Prerequisite**: `smt-structural-and-fixes.md` and `smt-humanize.md` are both complete. This plan executes BEFORE `smt-solution-f-draw-on-liquidity.md`.

## User Story

As a strategy developer  
I want divergences to be scored, stale or inferior pending hypotheses to be replaceable, and nonsensical entries (inverted stop, over-adverse) to be blocked  
So that large structural losses caused by locked stale hypotheses and inverted stops are eliminated

## Problem Statement

Four analyzed losing trades share the same failure pattern: divergence detected early, price moves 135–400 pts adversely, state machine stays locked in `WAITING_FOR_ENTRY`, confirmation fires at the worst possible price with an inverted stop. The largest two losses (-$490, -$460) stem from TDO-proximity causing near-zero nominal stops that fill at 60-pt bar extremes. Solutions A–E cut off these failure modes at multiple layers.

## Solution Statement

Layer 1 (A): Score all divergences so weak ones can be displaced.  
Layer 2 (B): Allow hypothesis replacement when a clearly superior divergence appears.  
Layer 3 (C): Block entry if stop is already invalid (last-resort guard).  
Layer 4 (D): Abandon hypothesis after excessive adverse move with no replacement.  
Layer 5 (E): Suppress first-bar divergences when session extremes are statistically undefined.

## Feature Metadata

**Feature Type**: Enhancement  
**Complexity**: High  
**Primary Systems Affected**: `strategy_smt.py`, `backtest_smt.py`, `signal_smt.py`  
**Dependencies**: None (internal only)  
**Breaking Changes**: No — all new params default to off/permissive values

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ BEFORE IMPLEMENTING

- `strategy_smt.py:1165` — `_build_signal_from_bar()` — Solution C guard goes here, before signal dict is built
- `strategy_smt.py:365` — `detect_smt_divergence()` — returns `(direction, sweep_pts, miss_pts, smt_type, defended_level)`
- `backtest_smt.py:256–342` — per-session state variables — add `pending_div_score`, `pending_div_provisional`, `pending_discovery_bar_idx`, `pending_discovery_price` here
- `backtest_smt.py:590–684` — `WAITING_FOR_ENTRY` block — replacement logic + invalidation check go here, before the confirmation bar check
- `backtest_smt.py:919–934` — divergence accepted into pending state — compute and store `divergence_score()` here
- `signal_smt.py:431` — `_process_scanning()` — mirror all backtest changes here
- `hypothesis_smt.py` — `compute_hypothesis_context()` returns dict with `"direction"` key — already called at `backtest_smt.py:321`

### New Files to Create

- None — all changes are additive modifications to existing files

### Patterns to Follow

- **Parameter naming**: `SCREAMING_SNAKE_CASE` float constants, defined in `strategy_smt.py` parameter block and imported into `backtest_smt.py`
- **State variables**: lowercase `_pending_*` naming, reset at day boundary (backtest_smt.py:327–341)
- **Export via `__all__`**: any new public function in `strategy_smt.py` must be added to the `from strategy_smt import (...)` block in `backtest_smt.py:12–32`

---

## PARALLEL EXECUTION STRATEGY

### Dependency Graph

```
┌─────────────────────────────────────────────────────────┐
│ WAVE 1: strategy_smt.py (Parallel tasks)                │
├──────────────────────────────┬──────────────────────────┤
│ Task 1.1: divergence_score() │ Task 1.2: Solution C     │
│ + all new params             │ inverted stop guard      │
│ Agent: strategy-specialist   │ Agent: strategy-spec.    │
└──────────────────────────────┴──────────────────────────┘
                               ↓
┌─────────────────────────────────────────────────────────┐
│ WAVE 2: backtest_smt.py state machine (Sequential)      │
├─────────────────────────────────────────────────────────┤
│ Task 2.1: pending state vars + scoring at detection     │
│ Task 2.2: replacement logic in WAITING_FOR_ENTRY        │
│ Task 2.3: HYPOTHESIS_INVALIDATION_PTS check             │
│ Task 2.4: Solution E early bar skip                     │
│ Agent: backtest-specialist                              │
└─────────────────────────────────────────────────────────┘
                               ↓
┌─────────────────────────────────────────────────────────┐
│ WAVE 3: signal_smt.py mirror (After Wave 2)             │
├─────────────────────────────────────────────────────────┤
│ Task 3.1: mirror all Wave 2 changes in _process_scanning│
│ Agent: signal-specialist                                │
└─────────────────────────────────────────────────────────┘
                               ↓
┌─────────────────────────────────────────────────────────┐
│ WAVE 4: Tests                                           │
├─────────────────────────────────────────────────────────┤
│ Task 4.1: unit tests for new functions + integration    │
│ Agent: test-specialist                                  │
└─────────────────────────────────────────────────────────┘
```

### Interface Contracts

**Wave 1 → Wave 2**: `divergence_score(sweep_pts, miss_pts, body_pts, smt_type, hypothesis_direction, div_direction) -> float` is importable from `strategy_smt`. New constants `MIN_DIV_SCORE`, `REPLACE_THRESHOLD`, `DIV_SCORE_DECAY_FACTOR`, `DIV_SCORE_DECAY_INTERVAL`, `ADVERSE_MOVE_FULL_DECAY_PTS`, `ADVERSE_MOVE_MIN_DECAY`, `HYPOTHESIS_INVALIDATION_PTS` are importable from `strategy_smt`.

**Wave 2 → Wave 3**: exact variable names and logic from `backtest_smt.py` WAITING_FOR_ENTRY block are mirrored verbatim in `signal_smt._process_scanning`.

---

## IMPLEMENTATION PLAN

### Wave 1: strategy_smt.py Changes

#### Task 1.1: ADD `divergence_score()` and new parameters to `strategy_smt.py`

- **WAVE**: 1
- **AGENT_ROLE**: strategy-specialist
- **DEPENDS_ON**: []
- **BLOCKS**: [2.1]
- **PROVIDES**: `divergence_score()` function + all new tunable constants

**New parameters** (add to the parameter block near line 295, before the first function definition):

```python
# ── Solution A+B: Divergence quality scoring and hypothesis replacement ────────
MIN_DIV_SCORE:             float = 0.0    # minimum score to accept divergence; 0 = off
REPLACE_THRESHOLD:         float = 1.5    # new_score must be > pending_effective * this to replace

# Score decay — pending hypothesis grows easier to replace the longer it is held
DIV_SCORE_DECAY_FACTOR:    float = 0.90   # per-interval multiplier; 1.0 = disabled
DIV_SCORE_DECAY_INTERVAL:  int   = 10     # bars between each decay step

# Adverse-move decay — additional decay based on how far price moved against hypothesis
ADVERSE_MOVE_FULL_DECAY_PTS:  float = 150.0  # pts of adverse move that drives score to floor; 999 = disabled
ADVERSE_MOVE_MIN_DECAY:       float = 0.10   # floor for the adverse-move decay multiplier

# ── Solution D: Hard invalidation threshold ───────────────────────────────────
HYPOTHESIS_INVALIDATION_PTS:  float = 999.0  # abandon hypothesis after this many pts adverse; 999 = disabled
```

**New function** `divergence_score()` (add just before `detect_smt_divergence` at ~line 365):

```python
def divergence_score(
    sweep_pts: float,
    miss_pts: float,
    body_pts: float,
    smt_type: str,
    hypothesis_direction: str | None,
    div_direction: str,
) -> float:
    """Score a divergence [0, 1]. Higher = stronger, harder to displace."""
    if smt_type == "displacement":
        base = min(body_pts / 20.0, 1.0)
    else:
        base = (
            min(sweep_pts / 5.0,  1.0) * 0.25
            + min(miss_pts  / 25.0, 1.0) * 0.50
            + min(body_pts  / 15.0, 1.0) * 0.25
        )
    if hypothesis_direction not in (None, "neutral"):
        if div_direction == hypothesis_direction:
            base = min(base + 0.20, 1.0)
    return base
```

**Effective score helper** (add immediately after `divergence_score`):

```python
def _effective_div_score(
    score: float,
    discovery_bar_idx: int,
    current_bar_idx: int,
    discovery_price: float,
    direction: str,
    current_bar_high: float,
    current_bar_low: float,
) -> float:
    """Apply time and adverse-move decay to a pending divergence score."""
    bars_held  = current_bar_idx - discovery_bar_idx
    time_decay = DIV_SCORE_DECAY_FACTOR ** (bars_held // max(DIV_SCORE_DECAY_INTERVAL, 1))

    if direction == "short":
        adverse = max(0.0, current_bar_high - discovery_price)
    else:
        adverse = max(0.0, discovery_price - current_bar_low)
    move_decay = max(
        ADVERSE_MOVE_MIN_DECAY,
        1.0 - adverse / max(ADVERSE_MOVE_FULL_DECAY_PTS, 1.0),
    )
    return score * time_decay * move_decay
```

**Export** both functions: add `divergence_score`, `_effective_div_score`, and all new constants to the `from strategy_smt import (...)` statement in `backtest_smt.py:12–32`.

- **VALIDATE**: `python -c "from strategy_smt import divergence_score, _effective_div_score, MIN_DIV_SCORE, REPLACE_THRESHOLD; print('ok')"`

---

#### Task 1.2: ADD inverted stop guard in `_build_signal_from_bar` (Solution C)

- **WAVE**: 1
- **AGENT_ROLE**: strategy-specialist
- **DEPENDS_ON**: []
- **BLOCKS**: [4.1]
- **PROVIDES**: Hard rejection of inverted stops

**Location**: `strategy_smt.py:1165` — `_build_signal_from_bar()`. Add immediately after `stop_price` is computed and before the signal dict is assembled:

```python
# Solution C: reject inverted stops — stop on wrong side of entry guarantees bar-1 exit
if direction == "long"  and stop_price >= entry_price:
    return None
if direction == "short" and stop_price <= entry_price:
    return None
```

- **VALIDATE**: `python -m pytest tests/test_smt_strategy.py -q`

**Wave 1 Checkpoint**: `python -c "from strategy_smt import divergence_score, _build_signal_from_bar; print('wave1 ok')"`

---

### Wave 2: backtest_smt.py State Machine

#### Task 2.1: ADD pending state variables + score at divergence detection

- **WAVE**: 2
- **AGENT_ROLE**: backtest-specialist
- **DEPENDS_ON**: [1.1]
- **BLOCKS**: [2.2, 2.3]
- **PROVIDES**: `_pending_div_score`, `_pending_div_provisional`, `_pending_discovery_bar_idx`, `_pending_discovery_price` in session state

**In the day-boundary state reset** (`backtest_smt.py:327–341`), add:

```python
_pending_div_score           = 0.0
_pending_div_provisional     = False
_pending_discovery_bar_idx   = -1
_pending_discovery_price     = 0.0
```

**At the point where divergence is accepted into pending state** (`backtest_smt.py:922–934`), compute and store the score immediately after `ac = find_anchor_close(...)`:

```python
_pending_div_score = divergence_score(
    _smt_sweep, _smt_miss,
    body_pts=abs(float(_mnq_closes[bar_idx]) - float(_mnq_opens[bar_idx])),
    smt_type=_smt_type,
    hypothesis_direction=_session_hyp_dir,
    div_direction=direction,
)
if MIN_DIV_SCORE > 0 and _pending_div_score < MIN_DIV_SCORE:
    continue  # divergence too weak — skip without entering WAITING_FOR_ENTRY
_pending_div_provisional     = (_smt_type == "displacement")
_pending_discovery_bar_idx   = bar_idx
_pending_discovery_price     = float(_mnq_closes[bar_idx])
```

Also import: `divergence_score`, `_effective_div_score`, `MIN_DIV_SCORE`, `REPLACE_THRESHOLD`, `HYPOTHESIS_INVALIDATION_PTS` from `strategy_smt`.

- **VALIDATE**: `python -m pytest tests/test_smt_backtest.py -q`

---

#### Task 2.2: ADD hypothesis replacement logic in WAITING_FOR_ENTRY

- **WAVE**: 2
- **AGENT_ROLE**: backtest-specialist
- **DEPENDS_ON**: [2.1]
- **BLOCKS**: [2.3, 3.1]
- **PROVIDES**: Superior divergences can replace stale pending hypotheses

**Location**: `backtest_smt.py:590` — top of the `WAITING_FOR_ENTRY` block, **before** the confirmation bar check.

Add a secondary divergence scan to check if a new divergence should replace the pending one:

```python
# Replacement check: scan for a new divergence that could displace the pending hypothesis
_new_div = detect_smt_divergence(
    bar_idx, mnq_reset, mes_reset,
    _run_ses_high, _run_ses_low,
    _run_mnq_high, _run_mnq_low,
)
if _new_div is not None:
    _nd_dir, _nd_sweep, _nd_miss, _nd_type, _nd_defended = _new_div
    _nd_body = abs(float(bar["Close"]) - float(bar["Open"]))
    _nd_score = divergence_score(
        _nd_sweep, _nd_miss, _nd_body,
        _nd_type, _session_hyp_dir, _nd_dir,
    )
    if _nd_score >= MIN_DIV_SCORE:
        _eff = _effective_div_score(
            _pending_div_score, _pending_discovery_bar_idx, bar_idx,
            _pending_discovery_price, pending_direction,
            float(bar["High"]), float(bar["Low"]),
        )
        _replace = False
        # Rule 1: displacement can never displace wick/body
        if _pending_smt_type in ("wick", "body") and _nd_type == "displacement":
            _replace = False
        # Rule 2: any wick/body replaces a provisional (displacement) pending
        elif _pending_div_provisional and _nd_type in ("wick", "body"):
            _replace = True
        # Rule 3: same direction — upgrade if strictly stronger
        elif _nd_dir == pending_direction and _nd_score > _eff:
            _replace = True
        # Rule 4: opposite direction — replace if significantly stronger
        elif _nd_dir != pending_direction and _nd_score > _eff * REPLACE_THRESHOLD:
            _replace = True

        if _replace:
            _new_ac = find_anchor_close(mnq_reset, bar_idx, _nd_dir)
            if _new_ac is not None:
                pending_direction           = _nd_dir
                anchor_close                = _new_ac
                pending_smt_sweep           = _nd_sweep
                pending_smt_miss            = _nd_miss
                _pending_div_bar_high       = float(bar["High"])
                _pending_div_bar_low        = float(bar["Low"])
                _pending_smt_defended       = _nd_defended
                _pending_smt_type           = _nd_type
                _pending_div_score          = _nd_score
                _pending_div_provisional    = (_nd_type == "displacement")
                _pending_discovery_bar_idx  = bar_idx
                _pending_discovery_price    = float(bar["Close"])
                divergence_bar_idx          = bar_idx
                _pending_displacement_bar_extreme = (
                    float(bar["Low"]) if _nd_dir == "long" else float(bar["High"])
                ) if _nd_type == "displacement" else None
```

- **VALIDATE**: `python -m pytest tests/test_smt_backtest.py -q`

---

#### Task 2.3: ADD HYPOTHESIS_INVALIDATION_PTS check and Solution E early bar skip

- **WAVE**: 2
- **AGENT_ROLE**: backtest-specialist
- **DEPENDS_ON**: [2.2]
- **BLOCKS**: [3.1]
- **PROVIDES**: Hypothesis abandoned on excessive adverse move; first-bar signals suppressed

**Hypothesis invalidation** (Solution D) — add AFTER the replacement check block, still inside WAITING_FOR_ENTRY, before the confirmation bar check:

```python
# Solution D: abandon hypothesis if adverse move exceeds threshold
if HYPOTHESIS_INVALIDATION_PTS < 999:
    if pending_direction == "short":
        _adverse = float(bar["High"]) - _pending_discovery_price
    else:
        _adverse = _pending_discovery_price - float(bar["Low"])
    if _adverse > HYPOTHESIS_INVALIDATION_PTS:
        state = "IDLE"
        pending_direction = None
        anchor_close = None
        continue
```

**Solution E** — in the IDLE divergence detection section, wrap the call to `detect_smt_divergence` with an early-bar guard. Find the line that calls `detect_smt_divergence` and add before it:

```python
# Solution E: skip first 3 bars — session extremes undefined with <4 data points
if bar_idx < 3:
    continue
```

(Only add this if `MIN_BARS_BEFORE_SIGNAL` does not already exceed 3 for the current configuration. Check and add the guard unconditionally as a cheap safeguard.)

- **VALIDATE**: `python -m pytest tests/test_smt_backtest.py -q`

**Wave 2 Checkpoint**: `python -m pytest tests/test_smt_backtest.py tests/test_smt_strategy.py -q`

---

### Wave 3: signal_smt.py Mirror

#### Task 3.1: MIRROR all Wave 2 changes in `_process_scanning`

- **WAVE**: 3
- **AGENT_ROLE**: signal-specialist
- **DEPENDS_ON**: [2.3]
- **BLOCKS**: [4.1]
- **PROVIDES**: Live signal path has identical state machine behaviour to backtest

**Scope** (`signal_smt.py:431` — `_process_scanning()`):

1. Add `_pending_div_score`, `_pending_div_provisional`, `_pending_discovery_bar_idx`, `_pending_discovery_price` to the module-level signal state (alongside existing `_pending_*` vars)
2. At divergence detection acceptance: compute `divergence_score()` and store
3. In the `WAITING_FOR_ENTRY` section: add the replacement block (Rules 1–4) verbatim
4. In the `WAITING_FOR_ENTRY` section: add the `HYPOTHESIS_INVALIDATION_PTS` check
5. In the IDLE detection section: add the bar_idx < 3 guard

Import `divergence_score`, `_effective_div_score`, and all new constants from `strategy_smt` at the top of `signal_smt.py`.

- **VALIDATE**: `python -m pytest tests/ -q -k "signal"` (or full suite if no signal-specific test file)

**Wave 3 Checkpoint**: `python -m pytest tests/test_smt_strategy.py tests/test_smt_backtest.py -q`

---

### Wave 4: Tests

#### Task 4.1: ADD tests for all new logic

- **WAVE**: 4
- **AGENT_ROLE**: test-specialist
- **DEPENDS_ON**: [1.1, 1.2, 2.1, 2.2, 2.3, 3.1]
- **BLOCKS**: []

Add to `tests/test_smt_strategy.py` (unit) and `tests/test_smt_backtest.py` (integration). Do NOT create a new test file unless test count warrants it (> 40 new tests).

**Required test cases:**

| # | File | Test name | What it asserts |
|---|------|-----------|-----------------|
| 1 | test_smt_strategy.py | `test_divergence_score_displacement_uses_body` | displacement type uses body/20 formula |
| 2 | test_smt_strategy.py | `test_divergence_score_wick_weights` | miss_pts carries 50% weight |
| 3 | test_smt_strategy.py | `test_divergence_score_hypothesis_bonus_aligned` | aligned hypothesis adds 0.20 bonus, capped at 1.0 |
| 4 | test_smt_strategy.py | `test_divergence_score_hypothesis_no_penalty_counter` | counter-hypothesis does NOT reduce base score |
| 5 | test_smt_strategy.py | `test_effective_div_score_time_decay` | after DIV_SCORE_DECAY_INTERVAL bars, score × DIV_SCORE_DECAY_FACTOR |
| 6 | test_smt_strategy.py | `test_effective_div_score_adverse_move_decay` | large adverse move drives score toward ADVERSE_MOVE_MIN_DECAY floor |
| 7 | test_smt_strategy.py | `test_effective_div_score_combined_decay` | both time and adverse decay applied multiplicatively |
| 8 | test_smt_strategy.py | `test_inverted_stop_guard_long_rejected` | long signal with stop > entry returns None |
| 9 | test_smt_strategy.py | `test_inverted_stop_guard_short_rejected` | short signal with stop < entry returns None |
| 10 | test_smt_strategy.py | `test_inverted_stop_guard_valid_passes` | valid stop does not trigger guard |
| 11 | test_smt_backtest.py | `test_replacement_rule1_displacement_cannot_displace_wick` | displacement signal does not replace wick pending |
| 12 | test_smt_backtest.py | `test_replacement_rule2_wick_replaces_provisional` | wick signal replaces displacement (provisional) pending |
| 13 | test_smt_backtest.py | `test_replacement_rule3_same_direction_upgrade` | stronger same-direction signal replaces pending |
| 14 | test_smt_backtest.py | `test_replacement_rule4_opposite_direction_threshold` | opposite direction replaces only when score > pending × REPLACE_THRESHOLD |
| 15 | test_smt_backtest.py | `test_replacement_full_state_reset` | after replacement, anchor_close, divergence_bar_idx, discovery_price all reset |
| 16 | test_smt_backtest.py | `test_min_div_score_filters_weak_divergence` | divergence below MIN_DIV_SCORE never enters WAITING_FOR_ENTRY |
| 17 | test_smt_backtest.py | `test_invalidation_pts_resets_to_idle` | adverse move > HYPOTHESIS_INVALIDATION_PTS resets state to IDLE |
| 18 | test_smt_backtest.py | `test_invalidation_pts_999_disabled` | with HYPOTHESIS_INVALIDATION_PTS=999, state never resets on adverse move alone |
| 19 | test_smt_backtest.py | `test_early_bar_skip_bar0_no_divergence` | no divergence accepted on bar_idx < 3 |
| 20 | test_smt_backtest.py | `test_pending_div_score_stored_at_detection` | _pending_div_score populated after divergence accepted |

- **VALIDATE**: `python -m pytest tests/test_smt_strategy.py tests/test_smt_backtest.py -q`

**Wave 4 Checkpoint**: `python -m pytest tests/ -q`

---

## TESTING STRATEGY

### Unit Tests

**Status**: ✅ Automated | **Tool**: pytest | **Location**: `tests/test_smt_strategy.py` | **Run**: `python -m pytest tests/test_smt_strategy.py -q`

Tests 1–10 above: pure function logic, no backtest harness needed.

### Integration Tests

**Status**: ✅ Automated | **Tool**: pytest | **Location**: `tests/test_smt_backtest.py` | **Run**: `python -m pytest tests/test_smt_backtest.py -q`

Tests 11–20 above: use synthetic bar fixtures to exercise the state machine through replacement and invalidation scenarios.

### End-to-End / Smoke

**Status**: ✅ Automated (synthetic data, no live connection needed) | **Run**: `python -m pytest tests/ -q`

Full suite regression: 620 tests currently passing. Zero new failures expected on pre-existing tests; only new tests added.

### Manual Tests

None required — all paths exercisable with synthetic bar fixtures.

### Test Automation Summary

| | Count | % |
|---|---|---|
| ✅ Unit (pytest) | 10 | 50% |
| ✅ Integration (pytest) | 10 | 50% |
| ⚠️ Manual | 0 | 0% |
| **Total** | 20 | 100% |

---

## VALIDATION COMMANDS

### Level 1: Import check

```bash
python -c "from strategy_smt import divergence_score, _effective_div_score, MIN_DIV_SCORE, REPLACE_THRESHOLD, HYPOTHESIS_INVALIDATION_PTS; print('imports ok')"
```

### Level 2: Unit tests

```bash
python -m pytest tests/test_smt_strategy.py -q
```

### Level 3: Integration tests

```bash
python -m pytest tests/test_smt_backtest.py -q
```

### Level 4: Full suite regression

```bash
python -m pytest tests/ -q
```

---

## ACCEPTANCE CRITERIA

- [ ] `divergence_score()` and `_effective_div_score()` importable from `strategy_smt`
- [ ] All new constants (`MIN_DIV_SCORE`, `REPLACE_THRESHOLD`, `DIV_SCORE_DECAY_FACTOR`, `DIV_SCORE_DECAY_INTERVAL`, `ADVERSE_MOVE_FULL_DECAY_PTS`, `ADVERSE_MOVE_MIN_DECAY`, `HYPOTHESIS_INVALIDATION_PTS`) defined in `strategy_smt` and imported where used
- [ ] Inverted stop guard in `_build_signal_from_bar` returns `None` for long with stop ≥ entry and short with stop ≤ entry
- [ ] Replacement Rules 1–4 implemented in `WAITING_FOR_ENTRY` block of `backtest_smt.py`
- [ ] Replacement logic mirrored verbatim in `signal_smt._process_scanning`
- [ ] `HYPOTHESIS_INVALIDATION_PTS` check resets state to IDLE when triggered
- [ ] Early bar skip (bar_idx < 3) prevents divergence detection in first 3 bars
- [ ] All 20 new tests pass
- [ ] Full suite passes with zero new regressions (baseline: 620 tests)
- [ ] All new constants default to off/permissive (`MIN_DIV_SCORE=0`, `HYPOTHESIS_INVALIDATION_PTS=999`, `REPLACE_THRESHOLD=1.5`) so existing backtest output is unchanged when defaults are used

---

## COMPLETION CHECKLIST

- [ ] Wave 1 complete (strategy_smt.py) — checkpoint passed
- [ ] Wave 2 complete (backtest_smt.py) — checkpoint passed
- [ ] Wave 3 complete (signal_smt.py) — checkpoint passed
- [ ] Wave 4 complete (tests) — all 20 new tests passing
- [ ] Level 1–4 validation commands all pass
- [ ] Full suite regression: 0 new failures
- [ ] All acceptance criteria checked
- [ ] **⚠️ Debug logs REMOVED**
- [ ] **⚠️ Changes UNSTAGED — NOT committed**

---

## NOTES

- Solutions A+B interact closely: the score computed in A is consumed by B's replacement threshold comparisons. Do not implement B without A.
- Solution C is independent of A+B — it could be a one-line PR. Keep it in this plan for a single implementation cycle but note it can be applied first if urgency demands.
- `REPLACE_THRESHOLD=1.5` is the recommended starting point. At 1.5, the new divergence must be 50% stronger in effective score to flip direction — conservative enough to prevent thrashing on noisy sessions.
- The decay defaults (`FACTOR=0.90`, `INTERVAL=10`) produce approximately: 20 bars ≈ 81%, 50 bars ≈ 59%, 100 bars ≈ 35%. This is soft enough not to force replacement on slightly stale hypotheses.
- `ADVERSE_MOVE_FULL_DECAY_PTS=150` means a 150-pt adverse move drives the score to its ADVERSE_MOVE_MIN_DECAY floor (10%). On the worst analyzed session (400 pts adverse), the score reaches 10% of original → any new divergence scoring > 15% of original (at REPLACE_THRESHOLD=1.5) will replace it.
- After implementation, run a full backtest and compare `mean_test_pnl` against the post-fill-fix baseline (mean $2,243/fold). A–E changes filter entry quality; expect trade count to decrease and avg_rr to increase. If mean_test_pnl drops significantly, loosen `MIN_DIV_SCORE` or `REPLACE_THRESHOLD` first before tuning decay params.
