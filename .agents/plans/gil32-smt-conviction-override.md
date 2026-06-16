# Plan — GIL-32: SMT-conviction override on rule2b direction (Phase 1, standalone, ungated)

EXECUTION_MODE: lightweight
EXECUTOR DIRECTIVE: Implement this sequential plan directly (do NOT use /execute or a team).
Medium/local change: one new pure module + a stateful update in the detection path + one gated
branch in `_determine_direction` + unit tests. Leave ALL changes UNSTAGED. Do NOT commit/push.
Source of truth = Linear GIL-32. Run the named unit tests before declaring done.

## Goal
At a wrong-side trend start, let a *standing SMT conviction* flip rule2b's direction to the SMT
side. UNGATED (no daily-trend gate — Phase 1). Deterministic; legacy `smt_active_set` /
`_compute_smt_score_v2` / `_co_evaluate_with_smt` / confidence path / GIL-19 relevance UNCHANGED;
no hypothesis-trigger changes; INVALIDATE_PTS NOT widened.

## Tunable module constants (put together, documented)
- `CONVICTION_STRONG = 0.5`        — |conviction| ≥ this is required to flip.
- `CONVICTION_RESIDUAL_MIN = 180`  — minutes a *fulfilled* SMT keeps a linearly-decayed residual.
- `CONVICTION_GRACE_MIN = 5`       — no adverse-run drop within this many min of fire (the sweep).
- `CONVICTION_SUSTAIN = 2`         — adverse-run drop requires this many consecutive adverse closes.
- Tier weights (reuse `_SMT_V2_TIER_WEIGHT`): ATH/week 3, day 2, fill 1.5, session 1.

## Task 1 — `smt_conviction.py` (new, pure, no IO)
Pure functions (total, JSON-serializable in/out; never raise):
- `update_standing(prev: list[dict], new_divs: list[dict], status_map: dict, mnq_close: float, now_iso: str) -> list[dict]`
  Maintain the standing conviction set (records: `ref_name, direction, side, tier, type, fire_iso,
  fire_close, adverse_streak, fulfilled_iso|None`). Rules:
  - Add each new div (collapse by `(ref_name, direction)`; wick supersedes body, newer wins — mirror
    `ingest_active_set` collapse semantics).
  - Per bar: if a record's key is `fulfilled` in `status_map` and `fulfilled_iso` is None → stamp it
    (start residual). DROP a fulfilled record once `now - fulfilled_iso > CONVICTION_RESIDUAL_MIN`.
  - Adverse-run drop (own, looser than detect_state): for a `short`, `adverse = mnq_close >=
    fire_close + INVALIDATE_PTS[tier]` (reuse smt_detect thresholds; do NOT widen). Within
    `CONVICTION_GRACE_MIN` of `fire_iso` → never adverse-drop. Increment `adverse_streak` on adverse
    closes, reset to 0 otherwise; DROP when `adverse_streak >= CONVICTION_SUSTAIN`.
  - `gone` keys (status_map) → drop.
- `conviction_score(standing: list[dict], now_iso: str) -> tuple[float, dict]`
  Collapse by `(ref_name, direction)`; per record weight = `tier_weight × residual_factor`
  (`residual_factor = 1.0` if not fulfilled, else `max(0, 1 - age/RESIDUAL_MIN)`); signed
  (`short → −`, `long → +`); `score = sum(signed)/sum(|w|)` clamped `[-1,1]`. Return `(score, inputs)`
  where inputs = `{n, n_bear, n_bull, top_tier, refs}` for event logging.

## Task 2 — maintain the standing set in the detection path
In `session_pipeline._run_smt_v2_detection` (has detect_state, the new divs this bar, and mnq_close):
after the existing `smt_active_set` update, call `smt_conviction.update_standing(...)` with the
fired divs + `smt_detect.smt_status(...)` map + current mnq_close, and **persist the result into
hypothesis state as `hypothesis["smt_conviction_set"]`** (parallel to `smt_active_set`; additive key,
DEFAULT to `[]` in `smt_state.DEFAULT_HYPOTHESIS`). Do NOT alter `smt_active_set`.

## Task 3 — consume in the direction engine
- `hypothesis.run_hypothesis`: read `smt_conviction_set`, compute `conviction, conv_inputs =
  smt_conviction.conviction_score(set, now_iso)`; pass `smt_conviction=conviction`,
  `smt_conviction_inputs=conv_inputs` as new kwargs to `_determine_direction` (default `0.0/{}` for
  back-compat).
- `_determine_direction`: signature gains the two kwargs. After rule2b sets `r2b_dir`
  (`hypothesis.py:~1771-1790`), BEFORE `return r2b_dir`:
  ```
  if smt_conviction and abs(smt_conviction) >= CONVICTION_STRONG:
      smt_dir = "down" if smt_conviction < 0 else "up"
      if smt_dir != r2b_dir:
          reason["smt_override"] = True
          reason["smt_conviction"] = round(smt_conviction, 3)
          reason["smt_conviction_inputs"] = smt_conviction_inputs
          r2b_dir = smt_dir
  ```
  No daily-trend gate. Do not touch rule1/rule2/rule3_4 or any trigger. (Apply to rule2b only in
  Phase 1.)

## Task 4 — unit tests (`tests/test_smt_conviction.py`, + extend `tests/test_smt_hypothesis.py`)
Name and implement:
1. `conviction_score`: tier weighting (week beats session), one-sidedness (3 bearish 0 bullish →
   near −1; mixed → cancels), collapse by (ref,dir) (wick+body for same logical SMT counts once),
   normalization within [-1,1], empty set → 0.0.
2. `update_standing`: residual decay (fulfilled SMT factor falls to 0 by RESIDUAL_MIN then drops);
   grace blocks an adverse close within GRACE_MIN; sustain requires CONVICTION_SUSTAIN consecutive
   adverse closes (1 adverse then a non-adverse does NOT drop); `gone` key dropped.
3. override branch in `_determine_direction`: flips when `|conv|≥STRONG` and sign contradicts
   `r2b_dir` (e.g. conv=−0.8, rule2b would say "up" → result "down", `smt_override` True); NO-OP when
   conviction aligns with r2b_dir; NO-OP when `|conv| < STRONG`; `smt_override` absent when no flip.
4. back-compat: `_determine_direction` with default `smt_conviction=0.0` is byte-identical to today.

## Task 5 — verify
- Run: `pytest tests/test_smt_conviction.py tests/test_smt_hypothesis.py -q` (+ any smt_detect/
  session_pipeline tests touching the modified functions). All green; no unrelated breakage.
- Confirm a no-conviction backtest path is unchanged (back-compat test).

## Acceptance (full validation happens in feature.md Stages C–D, not here)
Code complete + tests green + changes UNSTAGED. The A/B 1s regression + occurrence verification
(06-09/10/12 must flip "down"; 05-28/05-20 guards) is run by the orchestrating agent next.
