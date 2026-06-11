# Code Review — SMT Adverse-Run Invalidation (Part A)

Reviewed unstaged changes in `C:\Users\gilad\projects\auto-co-trader\smt-stuff` against plan
`.agents/plans/smt-adverse-invalidation-and-relevance-supersession.md`.

**Stats:**
- Files Modified: 5 (smt_detect.py, session_pipeline.py, tests/test_session_pipeline.py, tests/test_smt_detect.py, PROGRESS.md)
- Files Added: 2 (tests/test_smt_invalidation.py, _verify_invalidation.py) + plan file
- New lines: ~270
- Deleted lines: ~19

Unit + integration tests pass: `tests/test_smt_invalidation.py` + `tests/test_smt_detect.py` = 54 passed;
`tests/test_session_pipeline.py` = 66 passed. Production modules import clean.

---

## Findings

### 1. HIGH — Out-of-scope direction-by-sweep refactor is bundled in and DOES alter `records`, contradicting the plan invariant
- file: C:\Users\gilad\projects\auto-co-trader\smt-stuff\smt_detect.py
- lines: 230-234, 256-292, 430-431
- The unstaged diff contains far more than the invalidation feature. It rewrites FIXED-level
  direction detection: a new `__prevref_<rec_type>` reserved key (L233-234, persisted L430-431) and a
  "direction by sweep/approach" block (L256-292) that, for FIXED levels, picks `direction` from
  `prev_ref` vs `mnq_lvl_price` (above => long/down-sweep, below => short/up-sweep). In HEAD a fixed
  level's direction was unconditionally `"short" if sub=="high" else "long"`.
- This is a behavioral change to which `records` are emitted (direction/side flip for fixed levels
  approached from the opposite side; the touch wick and `_touch_sub` also flip). It is NOT additive.
  The plan's core safety claim — *"Invalidation adds a NEW flag and a NEW reserved key only — it does
  NOT alter records, fire, fulfill, or re-arm — so the 1s 2026-06-03 trade invariant (21 / $534.50) is
  preserved by construction"* (plan L38) — is therefore NOT satisfied by this changeset as a whole.
- Nothing in the plan, PROGRESS.md, or `.agents/execution-reports/` describes this refactor; it appears
  to be unrelated WIP that got mixed into the same working tree. The new direction tests in
  `tests/test_smt_detect.py` (L364-413: `test_fixed_high_bullish_when_swept_from_above`, etc.) cover it,
  confirming it is a deliberate change — just not part of THIS plan.
- suggestion: Decide ownership before committing. Either (a) split the direction-by-sweep refactor into
  its own commit/plan and review/regression it independently, or (b) if it must ship together, DROP the
  plan's "trade invariant preserved by construction" wording and re-run the 1s 2026-06-03 regression to
  re-establish the real invariant — `21 / $534.50` can no longer be assumed. Do not let a reviewer or
  future reader trust the "additive only" claim while the fixed-level direction logic is changing under it.

### 2. MEDIUM — Trade invariant (21 / $534.50) not re-verified; regression is the only backstop for the bundled change
- file: C:\Users\gilad\projects\auto-co-trader\smt-stuff\.agents\plans\smt-adverse-invalidation-and-relevance-supersession.md
- line: 153, 270
- The plan gates completion on `regression.py --mode 1s --dates 2026-06-03 -> trades=PASS n_trades=21
  pnl=534.50`. No execution report exists and I did not run the regression (heavy; a live orchestrator
  may be running per the plan's own side-effecting policy). Given finding #1 changes fixed-level signal
  directions, this invariant is the authoritative gate and is currently unproven for the combined diff.
- suggestion: Run the 1s 2026-06-03 regression on the full working tree before commit. If `n_trades`/`pnl`
  moved, the direction refactor (not invalidation) is the cause — quarantine it per finding #1.

### 3. LOW — `test_session_pipeline.py` invalidation tests call a DYNAMIC level a "fixed" SMT
- file: C:\Users\gilad\projects\auto-co-trader\smt-stuff\tests\test_session_pipeline.py
- lines: 1739-1748 (`_seed_day_high` / `_fire_then_invalidate`), 1771, 1773
- The fixtures seed `day_high`, which `_level_class` classifies as `("dynamic","day")`, but the docstrings
  say "fixed bearish SMT" (L1773) / "a fixed bearish SMT then adverse-up bars" (L1771). The test is
  correct (a dynamic high still fires bearish via the suffix mapping and invalidates), but the "fixed"
  wording is misleading and could send a future reader to the wrong code path. The adverse bars hold the
  wick below 21000 so no re-arm interferes — that part is sound.
- suggestion: Either reword the docstrings to "dynamic day_high bearish SMT", or seed a genuinely fixed
  level (e.g. `prev1_day_high`) so the wording matches and the test exercises the fixed path the plan's
  motivating case (prev1_week_high) actually uses.

### 4. LOW — `_verify_invalidation.py` hardcodes a run path, date, and tz offset (throwaway — acceptable)
- file: C:\Users\gilad\projects\auto-co-trader\smt-stuff\_verify_invalidation.py
- lines: 24-25, 129, 151
- Hardcoded default run dir `regression/sessions/2026-06-03/11-39-02`, the `2026-06-03` window, and a
  literal `-04:00` offset. It is an explicitly-throwaway, underscore-prefixed, read-only analysis script
  that stays unstaged, so this is informational only. It re-implements `_level_class`/authority locally
  (correctly noted as a read-only mirror, no entry-stuff import). The `print()` usage is fine here (not a
  production path).
- suggestion: None required. If it is ever promoted to a kept tool, parametrize the date/offset and read
  the tz from the data rather than the literal `-04:00`.

---

## Verified correct (no issue)

- **Reserved-key post-pass safety (plan's primary concern):** Both new reserved keys — `__invalidations__`
  (a list) and `__prevref_<rec_type>` (a float) — contain no `"|"`, so the post-pass guard
  `if "|" not in skey or st.get("armed")` (smt_detect.py:416) short-circuits BEFORE `.get` is called on a
  non-dict. Confirmed by `test_reserved_key_skipped_by_postpass`. The shared `detect_state` is also iterated
  by `detect_fill_smts`, which keys only by FVG name (`state.get(skey)`, no `state.items()` scan) and never
  touches the reserved keys. `__prevref_wick` vs `__prevref_body` do not collide (distinct rec_types).
- **Trade-invariant of the invalidation branch itself:** The (a2) block (smt_detect.py:344-362) only sets
  `st["invalidated"]/invalidated_time/invalidated_mnq_close` and appends to `__invalidations__`. It does
  NOT append/remove `records`, change `cond`/fire, fulfillment math, or re-arm triggers. Proven by
  `test_fixed_level_invalidation_does_not_change_records` (identical `records` with/without the adverse run).
  (Caveat: this isolation holds for invalidation in isolation; finding #1 is a SEPARATE bundled change.)
- **Fulfillment precedence (same bar):** Fulfillment (L327-336) runs first; the (a2) guard
  `not st.get("fulfilled")` (L344) excludes a bar that fulfilled this bar. Idempotency via
  `not st.get("invalidated")` (L344) — one event per transition (`test_invalidation_event_emitted_once`).
- **Cross-bar invalidated-then-fulfilled:** A previously-invalidated SMT can still later fulfill (the
  fulfillment guard does not check `invalidated`). This is intentional per plan NOTES (fixed-level fulfilled
  is informational; dynamic fulfilled triggers re-arm which resets `invalidated=False` at L373). Consistent.
- **Reset sites:** `st["invalidated"]=False` reset at fire (L397), `fire_time=iso` set at fire (L398),
  dynamic re-arm block (b) (L373), and post-pass re-arm (L428). Matches Contract INV-1.
- **Trail event schema:** All Contract INV-1 fields present (time, key, ref_name, tier, kind, direction,
  type, fire_time, fire_mnq_close, trigger_mnq_close, threshold_pts, reason=="adverse_run").
  `fire_time` uses `st.get(...)` so legacy/restored state without it yields `None` safely.
- **Trail wiring (session_pipeline.py:1757-1765):** Best-effort write to `paths.state_dir()/"smt_invalidations.json"`
  only when the list is non-empty; `paths` imported at module level (L15); written per-bar after
  `save_smts(...)`, NOT added to `sd_events`/golden events/levels.json/plot path. Matches Contract INV-2 and
  the `levels.json` pattern. Verified plot-free by `test_invalidation_trail_not_in_sd_events`.
- **Production code is silent:** No print/logging added to production paths (verified across the diff).
  The trail is structured data + a debug JSON, which the plan and CLAUDE.md explicitly permit.
- **Test isolation:** `_isolate_state` redirects `paths._STATE_DIR` to a per-test `tmp_path`, so
  `test_no_trail_file_when_no_invalidations`'s "file absent" assertion is not cross-test flaky.
