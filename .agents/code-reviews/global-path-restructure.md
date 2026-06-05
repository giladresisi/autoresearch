# Code Review — Global Path Restructure

Branch: `global-restructure` (all changes UNSTAGED). Pre-commit technical review.

**Stats**
- Files Modified (tracked): 29 (`git diff` shortstat)
- Files Added (untracked .py): 8 production/test + plan/baseline/report docs
- New lines: 551
- Deleted lines: 159

**Test baseline:** 5 failures observed in the live-path/integration run
(`test_orchestrator_main::test_main_session_dirs_created`,
`::test_pre_session_init_skips_when_no_api_key`, and 3
`test_check_session_parquets::TestProcessInstrumentSessionEnd::*`). All 5 are listed
in `.agents/baseline_failures.md` and were confirmed to fail on HEAD via
`git stash` — **pre-existing, not introduced by this changeset.** All new unit files
(`test_paths`, `test_state_prefix`, `test_regression_run_dirs`, `test_commit_note`,
`test_migrate_to_global_paths`) pass (28/28); the state/regression/live tests pass apart
from the pre-existing 5.

---

## BLOCKING

### B1 — LIVE_TRADING parquet writer (`automation.main`) does NOT use the new global live dir; orchestrator merge/promote will find nothing
- severity: high
- file: automation/main.py
- line: 71, 1126 (writer) vs orchestrator/main.py:107,477 + scripts/check_session_parquets.py:42
- issue: In LIVE_TRADING mode the session subprocess is `automation.main`, which still
  hardcodes `BAR_DATA_DIR = Path("data")` (worktree-local) and passes it to
  `IbRealtimeSource(bar_data_dir=BAR_DATA_DIR)` (line 1126). `data/ib_realtime.py` writes
  every 1m/1s/session parquet under that passed dir only (unmodified this changeset). But
  the orchestrator's post-session `merge_session_1s_parquets(bar_data_dir)` and the
  pre-session accumulator now use `paths.data_live_dir()` (`<global>/data/live`), and
  `parquet-check` promotes from `data_live_dir()`. So the live writer writes to
  `<worktree>/data/` while the merge + live→main promotion read `<global>/data/live/` —
  they no longer agree.
- detail: Consequence in a real LIVE_TRADING session: the orchestrator's session-end
  `merge_session_1s_parquets` finds no `*_1s_session_*.parquet` in `<global>/data/live`,
  and `promote_live_to_main()` finds no live parquets to promote → `main` never advances
  and the session's 1s data is stranded in the worktree `data/` dir. This directly
  contradicts the plan's acceptance criterion "Live parquet append → `<global>/data/live/`".
  It also means the pre-session overnight accumulator (orchestrator → global/live) and the
  in-session writer (subprocess → worktree/data) write to two different parquet stores, so
  the in-session bars start from a different/empty file than the overnight ones.
- nuance: `automation.main` and `signal_smt.py` were explicitly declared OUT OF SCOPE in
  `.agents/baseline_failures.md` ("legacy v1"). If LIVE_TRADING via `automation.main` is in
  fact the live path used in production, this is a genuine pre-commit blocker for the live
  hot path and the parquet acceptance criterion is not met. If the production live path is
  signal mode only (and `automation.main` is dead/legacy), this is non-blocking but the
  scope note and the plan's "Live parquet append → global/data/live" claim are inconsistent
  and should be reconciled.
- suggestion: Confirm which subprocess is the live path. If `automation.main` is live, set
  `BAR_DATA_DIR = paths.data_live_dir()` (and `SESSIONS_DIR = paths.sessions_dir()` — see B2)
  there, or thread `bar_data_dir` from `ACT_*` env the same way state-dir is threaded.
  The state-JSON agreement (ACT_STATE_DIR) is correct; only the parquet/sessions dirs in the
  subprocess were left behind.

### B2 — `automation.main` session artifacts (comments.md, signals/logs, live_position.json) go to worktree-local `sessions/` / `data/`, not the global dirs
- severity: medium
- file: automation/main.py
- line: 72, 73, 1058, 1132
- issue: `SESSIONS_DIR = Path(os.environ.get("SESSIONS_DIR", "sessions"))` and
  `POSITION_FILE = BAR_DATA_DIR / "live_position.json"` are unchanged. So the live
  subprocess writes `comments.md` (line 1132 — the commit-note target), per-session
  artifacts, and the V1 restart-recovery `live_position.json` under the worktree, while the
  orchestrator's session channels (`signals.log`, `orchestrator.log`, `trades.tsv`) now go
  to `<global>/sessions/<date>/` (orchestrator/main.py:40,482). Same-session artifacts are
  split across two locations.
- detail: The four SMT v2 state JSONs are fine (they funnel through `smt_state` →
  `ACT_STATE_DIR`). The split only affects the non-state session artifacts and the V1
  `live_position.json` recovery file (which is a separate file from the v2 `position.json`,
  so it does not break the v2 close path — but it does mean the run-orchestrator commit-note
  in `comments.md` lands in the worktree, not the analyzable global session folder).
- suggestion: Same fix as B1 — point `automation.main`'s `SESSIONS_DIR` at
  `paths.sessions_dir()` if it is the live path. Otherwise document explicitly that the
  commit-note / comments.md path is only wired for signal mode.

---

## NON-BLOCKING

### N1 — `run_backtest_v2` leaks the module-global `paths._STATE_DIR` on exit (no restore)
- severity: low
- file: backtest_smt.py
- line: 1244 (set per date), 1533 (only `set_in_memory_mode(False)` restored on exit)
- issue: Each date iteration calls `paths.set_state_dir(_run_dir)` and the function never
  restores the prior `_STATE_DIR`. After `run_backtest_v2` returns, `paths.state_dir()`
  points at the last date's per-run folder, not the pre-call default (`data/`).
- detail: Benign in practice for the current callers: `regression.py` re-sets the state dir
  per date inside `run_backtest_v2` itself, and the test suite isolates via
  `monkeypatch.setattr(paths, "_STATE_DIR", tmp_path)` (restored per test). It is NOT a
  determinism problem — in-memory mode + `reset_in_memory()` per date isolate the store, and
  `seed_global_from_prior()` is a no-op under `_IN_MEMORY`. The only real exposure is an
  in-process caller that runs a backtest and then reads/writes live state without re-setting
  the dir (none exists today). Worth a guard to prevent a future foot-gun, matching the plan
  note that flagged this.
- suggestion: Wrap the per-run mutation in try/finally restoring the prior `_STATE_DIR` (and
  `_IN_MEMORY`) at function exit, or expose a small context manager in `paths`
  (`with paths.state_dir_scope(run_dir): ...`).

### N2 — Every `paths.*` getter mkdirs on each call (side-effecting reads)
- severity: low
- file: paths.py
- line: 25-29 (`_ensure`), all getters
- issue: `data_main_dir()`, `data_live_dir()`, `sessions_dir()`, `state_dir()`, etc. each
  `mkdir(parents=True, exist_ok=True)` on every call. Read-only consumers (e.g.
  `plot_regression.py` reading `data_main_dir()/MNQ_1s.parquet`, or `_current_price()` in
  `live_orders.py` probing `data_live_dir()` each call) create the directory as a side
  effect of a lookup.
- detail: This is the intended design per the plan ("each getter ensures the dir exists"),
  and the cost is negligible (mkdir exist_ok is a stat). The only downside is that a pure
  "does the main store exist?" check can no longer be answered — the getter always
  materializes it. Not a correctness bug; noted for awareness. The hot path
  (`_current_price` fallback) calls `data_live_dir()` per invocation, but that path only
  fires when bar_state is unavailable, so it is rare.
- suggestion: Acceptable as-is. If a read-only resolver is ever needed, add a non-mkdir
  variant; otherwise no action.

### N3 — `seed_global_from_prior()` scans every prior session dir on each call, multiple times per session
- severity: low
- file: smt_state.py
- line: 116-148; called from session_pipeline.py:141 (on_daily_or_startup)
- issue: `on_daily_or_startup` runs at startup and again at 00:00 / 09:20 ET
  (session_pipeline.py:345-349), so `seed_global_from_prior()` re-scans all of
  `paths.sessions_dir()` (every historical date folder) several times per live session.
- detail: Correct and idempotent (excludes the current dir, takes `max`, never lowers an
  existing ATH, swallows all errors). Determinism is preserved (no-op under `_IN_MEMORY`).
  Only a minor efficiency note as the sessions dir grows — an O(history) directory scan a
  few times per session. Not a bug.
- suggestion: Optional — cache the prior-ATH once per process, or read only the single most
  recent prior date folder instead of iterating all.

### N4 — Backtest writes `levels.json` twice per date (pipeline + harness)
- severity: low
- file: session_pipeline.py:218 and backtest_smt.py:1303
- issue: `on_session_start` → `on_daily_or_startup` writes `paths.state_dir()/levels.json`
  (session_pipeline.py:225), and `run_backtest_v2` writes `_run_dir/levels.json` again
  (backtest_smt.py:1303). Since `set_state_dir(_run_dir)` precedes both, they target the
  identical file; the harness write overwrites the pipeline's.
- detail: Harmless (same dir, last-writer-wins, same schema). Slightly redundant. Worth a
  note only because if the two schemas ever diverge the duplicate could mask a difference.
- suggestion: Drop the harness-side write (rely on the pipeline's), or add a comment that the
  harness write is the canonical one.

---

## NITS

### NIT1 — `_files_equal` size-only idempotency check in migration
- file: scripts/migrate_to_global_paths.py:82-88
- detail: Parquet idempotency uses size equality only. Two different parquets of equal byte
  size would be treated as "already present" and skipped. Acceptable for a one-off seeding
  script (documented as intentional), but a same-size-different-content edge could silently
  skip a legitimately newer source. No action required; flagged for completeness.

### NIT2 — `regression_run_dir` second-granularity collision
- file: paths.py:73-83
- detail: Per-run folder is `<date>/<HH-MM-SS TH>`. Two runs of the same date started within
  the same wall-clock second (e.g. a fast scripted A/B) would share a folder and the second
  would overwrite the first's outputs. Extremely unlikely for real regression runs; noting
  the theoretical collision.

### NIT3 — `_git_version()` / `_write_run_info` shell out per date
- file: regression.py:16-39, called per date at line 153
- detail: `_write_run_info` re-invokes `git rev-parse` + `git status` for every date in a
  multi-date run. Cheap and best-effort (never raises), but the result is identical across
  dates within one invocation; could be computed once. No correctness impact.

---

## Verified OK (explicitly checked, no issue)

- **Cross-process state-JSON agreement (live):** orchestrator sets
  `paths.set_state_dir(_SESSIONS_DIR/today)` (main.py:461-462) AND passes the identical
  string via `ACT_STATE_DIR` (main.py:468); the subprocess's `on_session_start`
  (session_pipeline.py:253-259) reads `ACT_STATE_DIR` unconditionally when present. Both
  processes resolve the same folder by construction — `_close_session_position` →
  `load_position()` (main.py:64-66) reads exactly what the subprocess wrote. The four state
  JSONs (global/daily/hypothesis/position) are correctly routed even via the otherwise
  out-of-scope `automation.main`, because they funnel through `smt_state.state_dir()`. No
  open-position-missed-at-close risk from the *state-JSON* path. (The parquet/sessions split
  in B1/B2 is a separate concern and does not affect the v2 position-close read.)
- **`bar_state.json` vs state JSONs date agreement:** `bar_state_path` derives its own ET
  date (smt_state.py:257) rather than using `state_dir()`. At the 18:05 ET session open the
  orchestrator's `today = now.date()` equals that ET date, so they agree. This date-naming
  behavior is unchanged from pre-refactor (was `sessions/<date>`), so no new divergence is
  introduced.
- **Backtest determinism:** `set_in_memory_mode(True)` precedes the date loop;
  `reset_in_memory()` per date; `seed_global_from_prior()` is a no-op under `_IN_MEMORY`;
  `final_snapshot()` only dumps `_STORE` entries already written. No disk reads of prior-run
  state leak into a backtest. Equivalence is preserved (location-independence test green).
- **`promote_live_to_main` atomicity:** backs up prior main to `<name>.parquet.bak`, stages
  to `.parquet.promote.tmp`, then `os.replace` — atomic, leaves no temp on success
  (test-verified). Gated on `mode == "session-end" and not dry_run and merge_succeeded`.
- **TH timezone handling:** `regression_run_dir` localizes naive ET, converts to
  Asia/Bangkok, formats `%H-%M-%S` — matches `session_times` convention; ET-evening→next-TH-day
  boundary covered by `test_paths`.
- **`live_orders` data reads:** `_current_price` fallback and `hypothesis()` now read
  `paths.data_live_dir()` (the live writer's side) — correct for a live consumer reading
  today's freshest bars. `_PAUSE_FLAG` intentionally left at `data/paused` (not a data file).
