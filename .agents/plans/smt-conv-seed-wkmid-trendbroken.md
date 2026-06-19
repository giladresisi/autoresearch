# Plan — SMT conviction-seed (carry, incl. fills) + suppress weekly-mid trend-broken (GIL-39)

EXECUTION_MODE: lightweight

EXECUTOR DIRECTIVE: Implement the sequential steps below DIRECTLY — do NOT use /plan-feature or
/execute. This is a localized change: one new pure helper in `smt_conviction.py`, one call site in
`session_pipeline.on_session_start` (two force/no-force branches), a two-line guard suppression in
`trend.py`, two module-level flags, and unit tests. Both changes are behind INDEPENDENT
default-OFF flags and MUST be byte-identical to baseline when OFF. Leave everything UNSTAGED; never
commit/push; never run the live orchestrator or touch the live `global.json`.

---

## Flags (the repo convention = a module-level `NAME: bool = False` constant, toggled in tests via
`monkeypatch.setattr`; mirrors `trend.OPEN_WINDOW_DAILY_MID_SUSPEND` at `trend.py:50`)

- **Change A flag — `SMT_CONVICTION_SEED_CARRY`** — declared in `smt_conviction.py` (top, next to
  the tunable constants ~`smt_conviction.py:32-35`). Default `False`. Read inside the new
  `seed_from_standing` so the gate lives with the helper (the call site stays unconditional and
  `seed_from_standing` is a no-op when the flag is OFF → returns the prior set unchanged).
- **Change B flag — `SUPPRESS_WEEKLY_MID_TREND_BROKEN`** — declared in `trend.py` (next to
  `OPEN_WINDOW_DAILY_MID_SUSPEND` at `trend.py:50`). Default `False`. Read in
  `trend.update_position_for_trend` (the function that owns the weekly-mid invalidation).

Both flags live in the SAME module that consumes them (matches the existing
`OPEN_WINDOW_DAILY_MID_SUSPEND` pattern exactly — no separate config module exists).

---

## Change A — seed the standing-SMT conviction set from carried survivors (incl. fills)

### Why it currently no-ops
`run_hypothesis` reads `hypothesis["smt_conviction_set"]` (`hypothesis.py:2335`), scores it via
`smt_conviction.conviction_score` (`:2336`), and the rule2b override at `hypothesis.py:1858` flips
direction only when `|conviction| >= CONVICTION_STRONG (0.5)`. At session open the set is `[]`
(reset by the force `save_hypothesis(DEFAULT_HYPOTHESIS)` at `session_pipeline.py:736`), so the
override can never fire. `conviction_score` is a directional-consensus ratio
(`signed_sum/abs_sum`, `smt_conviction.py:359`) → an all-bullish carried set scores +1.0 and WOULD
flip rule2b "up"/"down"; the 0.5 threshold is not the blocker, the empty set is.

### Survivor source (already computed in `on_session_start`)
`_ingest_pending_smts` (`session_pipeline.py:2597`) already runs on cold start AFTER the force-reset
wipe (`:744`) and the no-force path (`:759`), BEFORE `run_hypothesis` (`:746`/`:762`). It calls
`smt_detect.revalidate_and_filter_pending` (`:2620`) and MERGEs the surviving carried SMTs **and
fills** into `hypothesis["smt_active_set"]` (`:2626`). Those survivors are exactly
"unfulfilled, not invalidated, not depleted" carried records (age-cap + drop_fulfilled +
drop_depleted + dedup applied inside `revalidate_and_filter_pending`,
`smt_detect.py:235-260`). They are `_pending_entry_to_record`-shaped (`smt_detect.py:394-444`):
`{ref_name, direction, side, tier, type/phase, time(=fire_time), mnq_price, mnq_lvl_price, keys,
kind}` — fills carry `kind="fill"`, `tier="day"`, `phase`/`type`, `mnq_price=None`.

### Helper signature (add to `smt_conviction.py`)

```python
SMT_CONVICTION_SEED_CARRY: bool = False  # GIL-39 Change A flag. Default OFF → byte-identical.

def seed_from_standing(
    prev: "list[dict] | None",
    survivors: "list[dict] | None",
    now_iso: str,
    *,
    require_consensus: bool = True,
) -> list[dict]:
    """GIL-39 Change A — seed the standing conviction set from carried survivor records.

    Maps each `_pending_entry_to_record`-shaped survivor (carried SMT or fill) into a conviction
    record matching `update_standing`'s new-div schema, then MERGES onto `prev` using the same
    logical-key collapse (`(ref_name, direction)`, wick supersedes body, newer time wins). When
    `SMT_CONVICTION_SEED_CARRY` is False → returns `list(prev or [])` unchanged (no-op).

    `require_consensus` gate (recommended ON): the seeded contribution is only allowed to DRIVE an
    override when it is not a single weak fill — require `top_tier in {ATH, week, day}` OR `>= 2`
    concurring (same-sign) seeded records. When the gate is not met, seeded records are still
    added but tagged `"seed_weak": True` ... (see gate note below for the chosen enforcement).
    Total: degenerate input → safe; never raises.
    """
```

Record fields each seeded record MUST produce (match `update_standing`'s new-rec schema,
`smt_conviction.py:185-198`):
`{ref_name, direction, side, tier, type, fire_iso (= survivor "time"), fire_close (=
survivor "mnq_price", else 0.0/None-safe), adverse_streak: 0, fulfilled_iso: None, keys}`.
- `tier` = `survivor.get("tier")` (already set: levels classified, fills="day"); fall back to
  `_tier_of(ref_name, survivor.get("kind"), survivor.get("tier"))`.
- `type` = `survivor.get("type")` (wick/body for levels; fill phase for fills).
- `keys` = `survivor.get("keys")` if present, else `[_detect_key(ref_name, direction, type)]`.
- `side` = `survivor.get("side")` or derived `("bearish" if direction=="short" else "bullish")`.

Reuse the existing module helpers `_logical_key`, `_confirm_strength`, `_detect_key`, `_tier_of`
for the merge/collapse so seeding is consistent with `update_standing`.

### Consensus gate (recommended — choose enforcement at implementation; default ON)
Goal: one weak session/fill-tier record must not flip a day. Recommended enforcement = the
**simplest that the existing override already respects**: compute, over the SEEDED records only,
`top_tier` (highest of ATH/week/day/fill/session) and `n_concur` (count of the dominant sign).
If NOT (`top_tier in {ATH, week, day}` OR `n_concur >= 2`), DROP the seeded records (do not add
them) so the set stays empty and the override is unchallenged — this keeps the gate fully inside
Change A and needs no edit to `conviction_score`/`hypothesis.py`. (Alternative: tag-and-let-score
-handle, but dropping is cleaner and avoids touching the scorer.) Document the chosen branch in a
comment.

### Call site (`session_pipeline.on_session_start`)
Call `seed_from_standing` AFTER each `_ingest_pending_smts(...)` and BEFORE the corresponding
`run_hypothesis(...)`, on BOTH paths:

1. **force-reset / cold path** — after `self._ingest_pending_smts(now, today_mnq_at_open)`
   (`session_pipeline.py:744`), before `run_hypothesis` (`:746`).
2. **no-force / cold path** — after `self._ingest_pending_smts(now, today_mnq_at_open)`
   (`session_pipeline.py:759`), before `run_hypothesis` (`:762`).

Both are inside `if _cold_start:` guards (carry-seed is cold-start only — matches
`_ingest_pending_smts`). Implementation at each site:

```python
# GIL-39 Change A: seed standing conviction from the just-merged carried survivors so the
# rule2b override can challenge direction at the open. No-op when the flag is OFF.
import smt_conviction as _smt_conv
_hyp_seed = _smt_state.load_hypothesis()
_survivors = _hyp_seed.get("smt_active_set", []) or []
_seeded = _smt_conv.seed_from_standing(
    _hyp_seed.get("smt_conviction_set", []) or [], _survivors, now.isoformat()
)
_hyp_seed["smt_conviction_set"] = _seeded
_smt_state.save_hypothesis(_hyp_seed)
```

Note: `_ingest_pending_smts` has already written `smt_active_set` (its survivors) to
hypothesis.json by the time this runs, so reading `smt_active_set` here yields the carried set
(plus any warm-up actives — acceptable; they are equally "standing"). The seed source is the
carried survivors merged into `smt_active_set`; if a tighter source is wanted, gather them
directly via `self._detect_state_pending_entries(...) + self._detect_state_fill_entries(...)`
re-validated, but reading the already-merged `smt_active_set` is simpler and equivalent for the
carry case. **Prefer the `smt_active_set` read** (one source of truth, no re-validation).

Because the per-bar `update_standing` (`session_pipeline.py:2312`) reads
`_hyp.get("smt_conviction_set")` as its `prev`, the seeded set carries forward through the day
naturally (no further wiring needed).

---

## Change B — suppress the trend-broken emitted on a weekly-mid cross

### Emit sites (both in `trend.py`, function `update_position_for_trend`)
The weekly-mid invalidation lives in `trend.py`, NOT `session_pipeline.py` (the GIL-39 anchor
`session_pipeline.py:1345` had drifted). Two consumption sites, both gated by the same
`_weekly_mid_cross_guard` variable:
- **flat / unarmed path** — `trend.py:482-489` (`weekly_mid_cross` → `_market_close_signal`).
- **in-position / hypothesis-reset path** — `trend.py:797-815` (emits the
  `kind="trend-broken"`, `level_name="weekly_mid"` event and sets `hypothesis["direction"]="none"`,
  clearing entry state).

The frozen-snapshot reform path reassigns `_weekly_mid_cross_guard = f_weekly_mid_cross_guard`
(`trend.py:431`), but BOTH consumption sites read the single `_weekly_mid_cross_guard` symbol, so
suppressing at the consumption gate covers flat AND active paths.

### Suppression (mirror the `_suspend_daily_mid` idiom, `trend.py:279`)
1. Add the flag at `trend.py:50` (next to `OPEN_WINDOW_DAILY_MID_SUSPEND`):
   ```python
   SUPPRESS_WEEKLY_MID_TREND_BROKEN: bool = False  # GIL-39 Change B. Default OFF → byte-identical.
   ```
2. Compute a suppression flag once, alongside `_suspend_daily_mid` (`trend.py:279`):
   ```python
   _suppress_weekly_mid = SUPPRESS_WEEKLY_MID_TREND_BROKEN  # GIL-39 Change B (unconditional when ON)
   ```
3. AND `and not _suppress_weekly_mid` into BOTH weekly-mid guards:
   - `trend.py:482`: `if weekly_mid_price is not None and _weekly_mid_cross_guard and not _suppress_weekly_mid:`
   - `trend.py:797`: `if weekly_mid_price is not None and _weekly_mid_cross_guard and not _manual_lock and not _suppress_weekly_mid:`

When OFF, `_suppress_weekly_mid` is always `False` → both conditions are byte-identical to master.
(Scope note: this suppresses the weekly-mid invalidation entirely. That is the intended Change B —
"suppress the trend-broken specifically on weekly-mid crossings". The daily-mid and every other
invalidation are untouched.)

---

## Unit tests

### Change A — `tests/test_smt_conviction.py` (EXISTS — append these tests)
Use `monkeypatch.setattr(smt_conviction, "SMT_CONVICTION_SEED_CARRY", True)` to enable.
- `test_seed_from_standing_off_is_noop` — flag OFF → `seed_from_standing(prev, survivors, now)`
  returns `prev` unchanged (byte-identical list) even with non-empty survivors.
- `test_seed_from_standing_levels_consensus` — flag ON, two bullish week/day-tier level survivors
  → seeded set has 2 records with correct schema (`fire_iso`, `tier`, `keys`, `fulfilled_iso=None`,
  `adverse_streak=0`); `conviction_score(seeded, now)` == +1.0.
- `test_seed_from_standing_fills_only` (REQUIRED fills-only case) — flag ON, survivors are ONLY
  carried fills (`kind="fill"`, `tier="day"`, `phase` set, `mnq_price=None`) all bullish →
  seeded records produced with `tier="day"`, `type=phase`, fill detect key (bare ref_name);
  `conviction_score` == +1.0. With `require_consensus`: a SINGLE day-tier fill passes
  (`top_tier=="day"`), but a single `session`/`fill`-tier record is DROPPED (gate not met → empty).
- `test_seed_from_standing_weak_single_session_dropped` — flag ON, one lone `session`-tier record
  → consensus gate not met → returns empty (or no override-driving records).
- `test_seed_from_standing_merges_with_prev` — flag ON, `prev` already holds one record; a new
  survivor on a different `(ref_name, direction)` is added; a survivor colliding on the logical key
  collapses (wick supersedes body) — no duplicates.

### Change B — `tests/test_smt_trend.py` (append next to the GIL-34 open-window tests ~L709-763)
Use `monkeypatch.setattr(trend, "SUPPRESS_WEEKLY_MID_TREND_BROKEN", True)`.
- `test_weekly_mid_trend_broken_default_off` — assert
  `trend.SUPPRESS_WEEKLY_MID_TREND_BROKEN is False`; with a direction=up hypothesis that crosses
  the weekly mid downward (flat, no active), `update_position_for_trend` returns the
  `weekly_mid_cross` market-close signal (baseline behavior preserved).
- `test_weekly_mid_trend_broken_suppressed_when_on` (REQUIRED suppression case) — same scenario,
  flag ON → returns `None` (no weekly-mid signal), hypothesis direction PERSISTS (not reset to
  "none").
- `test_daily_mid_still_fires_when_weekly_suppressed` — flag ON, a DAILY-mid cross still emits its
  trend-broken (Change B must not suppress daily-mid).

---

## Flags-OFF byte-identical verification

After implementation, with BOTH flags default OFF:
1. Run the focused suites:
   `python -m pytest tests/test_smt_conviction.py tests/test_smt_trend.py tests/test_smt_hypothesis.py -q`
   (pytest is isolated by the autouse `_isolate_global_state` conftest fixture).
2. Confirm the new default-OFF assertions pass (`SMT_CONVICTION_SEED_CARRY is False`,
   `SUPPRESS_WEEKLY_MID_TREND_BROKEN is False`, OFF-no-op tests).
3. Sanity: a 1s regression on one carry day (e.g. 2026-06-10) with both flags OFF must be
   byte-identical to baseline (the regression-runner A/B handles this at Stage C; the executor
   only needs the unit-level OFF proof here — do NOT run the live orchestrator).

Leave ALL changes UNSTAGED.

---

## Files touched (summary)
- `smt_conviction.py` — `SMT_CONVICTION_SEED_CARRY` flag + new `seed_from_standing` helper.
- `session_pipeline.py` — call `seed_from_standing` at the two cold-start sites (~`:744`/`:759`).
- `trend.py` — `SUPPRESS_WEEKLY_MID_TREND_BROKEN` flag (`:50`) + `_suppress_weekly_mid` (~`:279`)
  + AND-guard at the two weekly-mid sites (`:482`, `:797`).
- `tests/test_smt_conviction.py` — Change A tests (incl. fills-only).
- `tests/test_smt_trend.py` — Change B tests (incl. weekly-mid suppression).
