# GIL-33 — Extend the SMT-conviction override beyond rule2b (rule2 / rule3_4 scope gap)

**Issue:** GIL-33 (parent GIL-24; extends GIL-32). **Worktree/branch:** `autoresearch/smt-conviction-rule2-scope` off `dc044d5`.
**Complexity:** ⚠️ Medium. **EXECUTION_MODE:** sequential.
**EXECUTOR DIRECTIVE:** TDD. Write the named unit tests FIRST (red), then the helper + call sites (green). Leave ALL changes UNSTAGED. Do NOT commit/push/merge.

## Problem (grounded 2026-06-17)

`hypothesis._determine_direction` (def `hypothesis.py:1481`) is a rule cascade. The GIL-32
standing-SMT-conviction override (`:1800-1806`, `smt_override=True`) and the same-liquidity
reversal-lock veto (`:1819-1824`) live **inside the rule2b block**, just before `return r2b_dir, reason`
(`:1825`). The other commit points bypass them entirely:

- `rule1` fresh sweep → `return` `:1557`
- `rule2b` zone/ATH → override+lock HERE → `return` `:1825`
- `rule2` approaching-momentum → `return` `:1833`  ← **no override**
- `rule3_4` blended `0.65*r3 + 0.35*smt_sc` → `return` `:1864`  ← **no override**
- `rule5_trend` fallback → `return` `:1868`

So a `rule2`/`rule3_4` formation never consults the standing conviction. On up-day opens this
produces wrong-side "down" hypotheses that the bullish open-SMTs cannot flip. The conviction
(`smt_conviction` / `smt_conviction_inputs` kwargs) is already passed in and in scope at every return.

## Target occurrence

2026-05-08 18:10 ET: session-open hypothesis forms `direction="down"` via `rule=rule2`. Three bullish
SMTs fire at/before it (`ny_evening_low` body+wick, `day_low` body), all MES-led, no bearish → clean
**+1.0** bullish standing conviction. Override is rule2b-only → never consulted → short into a +457pt
up day. GIL-25-only baseline +$824 vs current GIL-32 stack −$859.

## Design decisions (the 3 open questions in the issue)

1. **Per-rule-return via a small shared helper, applied at `rule2` and `rule3_4` returns ONLY.**
   `rule2b`'s proven inline override+lock block is left **completely untouched** — this is the
   strongest possible guarantee of the "rule2b byte-identical" hard constraint (no refactor risk,
   reason dict unchanged on rule2b-override days). The helper duplicates ~6 lines of override logic;
   that is the deliberate, safe trade vs. retrofitting rule2b.
2. **Reversal-lock veto stays rule2b-scoped (override only extends).** The override is the 05-08 fix;
   the lock is the conservative, mechanical-false-positive guard whose `_last_liq` arming context is
   specific to the rule2b sweep path. Extending it to rule2/rule3_4 risks new up-day false positives
   and is out of scope here. (Noted as a possible follow-up.)
3. **No rule3_4 double-count.** `combined` blends `smt_sc` = `_compute_smt_score_v2(divs)` (the
   relevance-filtered *active-set authority* score). The override uses `smt_conviction` (the
   *standing* conviction from `smt_conviction.py`: residual/grace/sustain lifecycle) — a distinct
   signal, not the same quantity. The override is applied AFTER rule3_4 picks its direction, flipping
   only on `|conv| >= CONVICTION_STRONG` contradiction — i.e. a higher-authority post-hoc flip,
   exactly as it already works for rule2b. Not double counting `smt_sc`.

`rule1` (decisive fresh sweep) and `rule5_trend` (weak fallback) are left out of scope, matching the
issue's explicit "rule2/rule3_4" scope.

## Implementation

### Task 1 — shared helper `_apply_conviction_override` (`hypothesis.py`, near `_determine_direction`)

```python
def _apply_conviction_override(direction, reason, rule_name, smt_conviction, smt_conviction_inputs):
    """GIL-33: extend the GIL-32 standing-SMT-conviction override to non-rule2b returns.
    A meaningful standing conviction (|conv| >= CONVICTION_STRONG) whose sign CONTRADICTS
    `direction` flips it to the SMT side and tags `reason` (incl. `smt_override_rule` = which
    rule was flipped). Default conviction 0.0 → no-op → byte-identical. Mutates `reason`,
    returns the (possibly flipped) direction. Mirrors the rule2b inline override (:1800)."""
    if smt_conviction and abs(smt_conviction) >= _smt_conv.CONVICTION_STRONG:
        smt_dir = "down" if smt_conviction < 0 else "up"
        if smt_dir != direction:
            reason["smt_override"] = True
            reason["smt_conviction"] = round(smt_conviction, 3)
            reason["smt_conviction_inputs"] = smt_conviction_inputs
            reason["smt_override_rule"] = rule_name
            return smt_dir
    return direction
```

### Task 2 — apply at rule2 return (`hypothesis.py:1830-1833`)

```python
    reason["rule"]              = "rule2"
    reason["approaching_level"] = r2["approaching_level"]["name"]
    reason["approaching_dist"]  = round(r2["dist"], 1)
    _dir = _apply_conviction_override(r2["direction"], reason, "rule2",
                                      smt_conviction, smt_conviction_inputs)
    return _dir, reason
```

### Task 3 — apply at rule3_4 return (`hypothesis.py:1862-1864`)

```python
    if abs(combined) >= DIRECTION_SCORE_THRESHOLD:
        reason["rule"] = "rule3_4"
        _dir = _apply_conviction_override("up" if combined > 0 else "down", reason, "rule3_4",
                                          smt_conviction, smt_conviction_inputs)
        return _dir, reason
```

`_smt_conv` is the existing module alias used by the rule2b block (verify the import name in file).

## Unit tests (`tests/test_smt_hypothesis.py`) — write FIRST

Add a `_rule2_down_direction_args()` helper that reaches `reason["rule"] == "rule2"` with baseline
`direction == "down"` (price approaching an unvisited LOW with downward momentum; ensure rule1/rule2b
do NOT fire — e.g. no `_find_last_liquidity` hit so rule2b is skipped). Then:

1. **`test_gil33_rule2_override_flips_to_up`** — rule2 baseline "down" + strong **bullish** conviction
   (+0.8) → direction flips to "up"; `reason["smt_override"] is True`,
   `reason["smt_override_rule"] == "rule2"`, `smt_conviction`/`smt_conviction_inputs` tagged.
2. **`test_gil33_rule2_override_noop_below_threshold`** — same rule2 baseline + contradicting conviction
   of magnitude `CONVICTION_STRONG - 0.01` → NO flip, no `smt_override`.
3. **`test_gil33_rule2_default_conviction_back_compat`** — rule2 args with `smt_conviction=0.0` reason
   dict == the no-kwargs call's reason dict (byte-identical), no `smt_override`.
4. **`test_gil33_rule2b_unchanged_by_helper`** — the existing `_rule2b_down_direction_args` scenario
   with a contradicting strong conviction still flips via rule2b and tags `rule == "rule2b"` and does
   NOT carry `smt_override_rule` (rule2b path untouched; confirms no double-application / no regression).
5. **`test_gil33_rule3_4_override_flips_and_no_double_count`** — a `rule3_4` scenario (no rule1/2b/2
   fire; `|combined| >= threshold`) with baseline direction X and a strong contradicting standing
   conviction → flips to the conviction side, `smt_override_rule == "rule3_4"`; assert `combined_score`
   in reason is unchanged by the override (the blend is computed before the flip → no double count).

Reuse the existing GIL-32 rule2b override tests as the "rule2b unchanged" anchor — they must still pass.

## Validation / acceptance

- All 5 new tests pass; full `pytest tests/test_smt_hypothesis.py` + `tests/test_smt_conviction.py` pass; no new failures in the suite vs baseline.
- Leave UNSTAGED. Stage C (A/B 1s regression on 05-08 + May1-Jun15) and Stage D (verifier at 05-08 18:10) run after.
- Acceptance (from GIL-33): 05-08 18:10 flips rule2 "down"→"up" (smt_override); 05-08 P&L recovers toward +$824; rule2b days byte-identical; no new wrong-side flips / net not materially harmed.
