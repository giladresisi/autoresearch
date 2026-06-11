# Code Review — Incremental parquet validation (GIL-15)

**Verdict: APPROVE (changes-requested are all non-blocking).**

Scope reviewed: the GIL-15 incremental-validation changeset only (other untracked files in
the worktree ignored). All changes are UNSTAGED per plan.

## Stats
- Files Modified: 5 (`scripts/check_session_parquets.py`, `data/parquet_maintenance.py`,
  `tests/test_check_session_parquets.py`, `tests/test_parquet_maintenance.py`,
  `.claude/skills/parquet-check/SKILL.md`, plus `PROGRESS.md` note)
- Files Added: 4 (`scripts/parquet_validation_state.py`, `scripts/parquet_tail.py`,
  `tests/test_parquet_validation_state.py`, `tests/test_parquet_tail.py`)
- New lines: ~526 (per `--stat`)

## Test result
- New feature tests: 9 (state) + 9 (tail) + 12 (incremental) + 3 (seam) = **33 pass**.
- 3 pre-existing failures in `TestProcessInstrumentSessionEnd`
  (`test_ok_session_merges_and_backs_up`, `test_minor_session_merges_as_is`,
  `test_late_start_escalates_to_rebuild`) — confirmed pre-existing via `git stash` (fail
  identically WITHOUT this changeset). NOT introduced here.

## Focus-area findings

### 1. Body-integrity guard — CORRECT for the three structural break modes
`needs_full_validation` (`parquet_validation_state.py:65`) + the positional guard
(`check_session_parquets.py:537-540`) cover all three: rewrite (first_bar moves →
`body-rewritten`), truncation (`row_count < validated_rows` → `truncation`), interior
insert (bar at `validated_rows-1` shifts → `body-rewritten`). tz comparison is sound:
`bar` (NY-aware) vs `pd.Timestamp(entry["validated_through"])` (fixed-offset) compares by
instant and is equal across both DST regimes (verified). NOTE (by design, not a finding):
a pure interior **value** rewrite that keeps timestamps + count is not detected — the plan
scopes the guard to structural breaks; `--full-validate` / `VALIDATOR_VERSION` bump is the
lever. Acceptable.

### 2. Tail validation does NOT silently pass bad data — CORRECT
Incremental tail runs the full `validate_session_df` (price bounds + OHLC + gap) PLUS an
explicit `Close<=0` check (`:568-572`). This is strictly stronger than the full path
(which only scans `Close<=0`). `test_incremental_bad_price_in_tail_surfaced` passes.

### 3. `_seam_ok` tz-correctness — CORRECT
Overlap (`new_first <= prev_last`), contiguity (`<=90s`), and `_is_expected_closed`
fallback all operate on tz-aware NY timestamps. `_is_expected_closed` does
`.tz_convert(...)`, which requires tz-aware inputs — satisfied everywhere it's called
(watermark ISO strings carry an offset; parquet indices are tz-aware).

### 4. pyarrow row-group selection — CORRECT
`read_after` keeps groups where `stats.max > wm` OR stats absent (`:147`); boundary cases
(`wm == last`, `wm` in last group) covered by passing tests and asserted equal to the naive
`df[df.index>wm]`. `bar_at_position` walks `num_rows` cumulatively — boundary positions
(49/50/51) verified.

### 5. Atomic sidecar write + dry-run — CORRECT
`.tmp` sibling + `os.replace` (same dir/volume → atomic on Windows). Dry-run gates ALL
writes (`:596` incremental, `:681` full, repair path returns before writing). No `.tmp`
left behind (test asserts).

### 6. Import-cycle risk — NONE
`_warn_on_seam` lazily imports `_is_expected_closed`; `main()` lazily imports
`merge_session_1s_parquets`. Both function-level. All four modules import cleanly together
(verified).

### 7. Regression risk to full-validation / repair / promotion — LOW
Diff is confined to `check_1m_parquet`, new `_full_validate_1m` / `_seam_ok`, repair-path
watermark seed, and 2 argparse flags threaded to the call site. `_full_validate_1m`
reproduces the original `Close<=0`-only healthy-path scan faithfully. `process_instrument`,
merge, and promotion are untouched.

---

## Categorized issues

### BUGS — non-blocking

```
severity: low
file: scripts/check_session_parquets.py
line: 575-583, 596
issue: Unexpected seam gap is only escalated to "minor", which still advances the watermark and refreshes .bak.
detail: A genuine missing-bars hole at the join (new_first far past prev_last, not an
  expected closure) is reported via seam_issue but severity is bumped only ok->minor.
  Because the watermark advance + .bak refresh gate is `severity in ("ok","minor")`
  (:596), the hole is treated as validated and the watermark moves past it. The same hole
  inside a session is classified major/critical by validate_session_df (>=60min => critical,
  >=5min => major). Severity is inconsistent and biases toward trusting a gap. In practice
  the merge/gap_fill writers fill the seam first, so this is unlikely on the 1m main — hence
  low. seam_issue IS surfaced in the report, so it is observable.
suggestion: For the "unexpected gap at seam" branch (not the overlap branch), escalate to
  "major" instead of "minor" so the watermark/.bak gate at :596 does NOT advance past an
  unverified hole, mirroring validate_session_df's weekday-gap severity. Keep overlap at
  minor (dedup handles it).
```

### STANDARDS / CODE QUALITY — non-blocking

```
severity: low
file: scripts/check_session_parquets.py
line: 589
issue: Convoluted bad_rows arithmetic that always reduces to v["bad_rows"].
detail: `int(len(bad)) + max(0, v["bad_rows"] - int(len(bad)))`. Since validate_session_df's
  bad_idx is the union of bad_price (which already includes Close<=0) and bad_ohlc,
  v["bad_rows"] >= len(bad) always holds, so the expression is identically v["bad_rows"].
  Not a double-count (the max(0,...) prevents it) — just dead arithmetic that obscures intent.
suggestion: Replace with `"bad_rows": v["bad_rows"]` (the Close<=0 rows are already counted
  by validate_session_df). The separate `bad` frame is still useful for the severity bump at
  :571, but not for the count.
```

```
severity: info
file: scripts/check_session_parquets.py
line: 549, 802
issue: --since only takes effect when a valid watermark already exists.
detail: If entry is None, needs_full_validation returns full and the incremental/since
  branch is never reached, so --since silently no-ops on a parquet with no sidecar. This is
  acceptable for a documented recovery aid, but undocumented in the flag help.
suggestion: Optional — note in the --since help (and SKILL.md) that it requires an existing
  watermark; or leave as-is (low value).
```

### Non-issues confirmed during review
- tz of `bar_at_position` / `index_bounds` on real (tz-aware-stored) parquets: correct;
  earlier doubt was a test artifact of tz-stripping via `.values`.
- `write_atomic` output carries row-group statistics even with `use_dictionary=False`, so
  the repair-path `index_bounds`/`row_count` use the fast metadata path (fallback also correct).
- `_warn_on_seam` receives tz-aware indices (seed ts is tz-aware NY); `_is_expected_closed`'s
  `.tz_convert` is safe.
- Sidecar fail-safe: corrupt/missing -> {} -> full validation (more validation, never less).
```
