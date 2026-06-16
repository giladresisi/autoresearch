# Plan — GIL-32 Phase-1b: same-liquidity reversal lock (protect-existing)

EXECUTION_MODE: lightweight
EXECUTOR DIRECTIVE: Implement this sequential plan directly (no /execute, no team). One new pure
lifecycle helper + state plumbing (parallel to the GIL-32 conviction set) + one veto branch in
`_determine_direction` + an arm step in `run_hypothesis` + unit tests. Leave ALL changes UNSTAGED.
Do NOT commit/push. Source of truth = Linear GIL-32.

## Goal
Once a with-SMT reversal hypothesis has formed on a liquidity level L (e.g. a "down" hypothesis
backed by a live bearish SMT on `day_high`), **disallow the opposite hypothesis on that same
liquidity** until the protecting SMT is either **level-accepted-through** or **fulfilled**. This
PROTECTS the already-formed reversal hypothesis from being overwritten by a counter-flip on the
same level (the "direction churn" failure — GIL-21/22 mechanism A) — without flipping or re-arming
anything, because the protected side equals the current direction → no reform (hypothesis.py:2086).

Grounded evidence (this session): 06-10 the 10:23 override-"down" on `day_high` was overwritten by
a 10:30 "up" on the SAME `day_high` after a +0.12%→+0.32% stop-run pop tripped the 40pt close-based
adverse-run; price then fell −600pts. 06-09 identical (week_high/day_high, 09:55–10:15 up-flips
while price was already BELOW the swept highs). 06-12 (chop) NO down hyp formed → lock never arms →
quiet (correct).

## Key design decisions (locked with user)
- **Invalidation = LEVEL ACCEPTANCE**, NOT detect_state's 40pt adverse-run (that adverse-run is what
  evicts the SMT on the very stop-run the SMT predicts will reverse). Release the lock only on a
  genuine acceptance beyond the level (close beyond by a structural buffer, sustained N closes) or
  on SMT fulfillment.
- **Action = PROTECT EXISTING**: when vetoing, force the decision to the SMT side. Because that side
  equals the current hypothesis direction, build_hypothesis_from_direction does NOT reset position
  (none→dir gate) and preserves `formed_at` → the in-progress entry setup survives.
- **Arm precondition (faithful to "formed a reversal SMT AND hypothesis on it")**: a lock arms only
  when a reversal hypothesis ACTUALLY forms (direction down/up) AND a live same-side SMT exists on
  the swept level — not on a bare SMT fire.

## Tunable module constants (document together)
- `LOCK_ACCEPT_BUFFER_PCT = 0.003`  — close must exceed the level by this fraction to count as accept.
- `LOCK_ACCEPT_SUSTAIN = 2`         — consecutive accepting closes required to release (level break).
- `LOCK_MAX_AGE_MIN = 240`          — safety: a lock self-releases after this many minutes (stale-regime guard).
- High levels = {day_high, week_high, ny_morning_high, london_high, prev1_day_high, asia_high, ny_evening_high};
  Low levels = symmetric *_low. (Match `last_swept_level` to the locked level by EXACT name.)

## REDESIGN (2026-06-16, after 06-10 grounding) — arm at FIRE-time, not hypothesis-time
The original "arm when the down hyp forms + a live same-side SMT is in the conviction set" FAILED
on 06-10: the bearish day_high SMT fires EARLY (10:03-10:12 @29085-29137), price runs the
manipulation leg UP past the level (→29168→29250) which EVICTS the SMT from the conviction set
(and detect_state) via the fire-close adverse-run, and only THEN reverses — so when the down hyp
finally forms at 10:23 the conviction set is already empty (`bearHigh=[]`) → nothing to arm from.
This is the GIL-25 premature-invalidation problem (see ../smt-level-invalidation: it removes the
adverse-run and uses level-relative depletion). FIX: the lock keeps its OWN durable ledger armed
at SMT-FIRE-time with a level-acceptance lifecycle (survives the pop), and is PROMOTED to
`protecting` the bar a same-side reversal hypothesis forms on the level.

ALSO CRITICAL (root cause #2): `build_hypothesis_from_direction` does a FULL hypothesis.json
rewrite that DROPS all SMT debug keys (smt_reversal_locks, smt_conviction_set, smt_active_set) —
so every reform WIPES the lock and the next detection rebuilds from empty. Fixed by carrying
`smt_reversal_locks` across the reform (hypothesis.py, before save_hypothesis).

## Task 1 — `smt_reversal_lock.py` (new, pure, no IO, never raises) — AS IMPLEMENTED
- `ingest_fires(prev_locks, fired_records, level_price_map, now_iso)` — open/refresh a NON-
  protecting lock for each fired record that is a bearish HIGH-level or bullish LOW-level SMT
  (fvg fills excluded — ref_name doesn't end high/low). Collapse by (level_name, side); refresh
  preserves protecting/accept_streak/fire_iso. Record: `{level_name, side, locked_dir, level_price,
  fire_iso, armed_iso, accept_streak, protecting, keys}`.
- `mark_protecting(locks, direction, last_swept_level)` — flip `protecting=True` on the matching
  lock (down↔bearish, up↔bullish) when a reversal hyp forms on its level.
- `advance(prev_locks, level_price_map, status_map, mnq_close, now_iso) -> list[dict]`
  Per lock, one bar: (a) refresh `level_price` from `level_price_map` if present; (b) acceptance —
  bearish: `mnq_close > level_price*(1+BUFFER)`; bullish: `mnq_close < level_price*(1-BUFFER)`;
  increment `accept_streak` when accepting else reset to 0; DROP when `accept_streak >= SUSTAIN`;
  (c) DROP when the lock's SMT key is `fulfilled`/`gone` in `status_map`; (d) DROP when
  `now - armed_iso > LOCK_MAX_AGE_MIN`. Returns kept locks.
- `vetoes(locks, r2b_dir, last_swept_level) -> str|None`
  If `r2b_dir=="up"` and a bearish lock exists on `last_swept_level` → return `"down"`.
  If `r2b_dir=="down"` and a bullish lock exists on `last_swept_level` → return `"up"`. Else None.

## Task 2 — maintain locks in the detection path
`session_pipeline._run_smt_v2_detection` (right after the GIL-32 conviction-set block, ~2192-2202):
call `smt_reversal_lock.advance(prev_locks, level_price_map, smt_status_map, mnq_close, now_iso)` and
persist `_hyp2["smt_reversal_locks"]`. Build `level_price_map` from the same liquidities used for the
conviction status map. Add `DEFAULT_HYPOTHESIS["smt_reversal_locks"] = []` in smt_state.py.

## Task 3 — consume (veto) in the direction engine
`_determine_direction` signature: add `smt_reversal_locks=None`. At the rule2b return (after the
existing GIL-32 conviction override, hypothesis.py:1798-1804, before `return r2b_dir`):
```
veto_dir = _smt_lock.vetoes(smt_reversal_locks or [], r2b_dir, _last_liq)
if veto_dir is not None and veto_dir != r2b_dir:
    reason["smt_reversal_lock"] = _last_liq
    reason["smt_reversal_lock_dir"] = veto_dir
    r2b_dir = veto_dir
```
`run_hypothesis`: read `hypothesis.get("smt_reversal_locks", [])`, pass it through; default [] → no-op.

## Task 4 — arm after the direction is decided
`run_hypothesis`, after `_determine_direction` returns (before build_hypothesis_from_direction):
compute the swept level price (from liquidities), call `smt_reversal_lock.arm(prev_locks, direction,
last_liquidity, _conv_set, level_price, now_iso)`, and pass the armed list into
build_hypothesis_from_direction so it persists onto the new hypothesis (alongside smt_active_set /
smt_conviction_set). (Arm reads the standing conviction set `_conv_set`, which is live at arm time.)

## Task 5 — unit tests (tests/test_smt_reversal_lock.py + extend test_smt_hypothesis.py)
1. `arm`: arms a bearish/down lock when down hyp + live bearish day_high SMT on swept day_high;
   does NOT arm on a bare SMT with no same-side hypothesis; does NOT arm when swept level != SMT ref;
   collapses to one lock per level.
2. `advance`: releases on SUSTAIN consecutive accepting closes (1 accept then non-accept does NOT
   release); releases on fulfilled status; releases past MAX_AGE; survives a sub-buffer pop (the
   06-10 +0.12% case stays locked).
3. `vetoes`: bearish lock on day_high turns r2b "up"→"down"; bullish lock on day_low turns
   "down"→"up"; no lock / different level → None.
4. integration in `_determine_direction`: with a live bearish day_high lock, a rule2b high+above-mid
   "up" (false-pos flags) is forced to "down", reason tagged; back-compat default [] → byte-identical.

## Task 6 — verify
`pytest tests/test_smt_reversal_lock.py tests/test_smt_hypothesis.py tests/test_smt_conviction.py -q`
plus session_pipeline tests touching the modified function. All green; no unrelated breakage.

## Acceptance (full validation in feature.md Stages C–D)
Code complete + tests green + UNSTAGED. Then 1s A/B on 06-09/10/12 (down setups must now PERSIST /
not be counter-flipped) + 05-28/05-20 up-guards (lock must not trap shorts on the rally — verify the
acceptance release frees it).
