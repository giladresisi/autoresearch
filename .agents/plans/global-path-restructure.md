# Feature: Global Path Restructure for Parallel Multi-Agent Backtesting

**⚠️ EXECUTION RULES — READ FIRST:**
- Implement ALL changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
- Only make code changes — no git operations

Validate documentation and codebase patterns before implementing. Match naming of existing utils, types, and models. Import from correct files.

---

## Feature Description

Relocate the project's production parquets, live-session logs, strategy state JSONs, and
regression outputs from fixed in-project relative paths to **configurable base directories**
— with the production data and live sessions in a **machine-global folder** — so that many
agents working in separate git worktrees can run backtests in parallel without colliding
with each other or with the live orchestrator's file writes (which today cause Windows
`[WinError 5] Access is denied` rename failures when a reader holds a parquet open).

The work introduces a single source of truth for all of these paths (`paths.py`),
env-overridable, matching the existing `FUTURES_CACHE_DIR` env pattern.

## User Story

As a developer running many strategy-optimization agents in parallel worktrees,
I want production data, live sessions, and per-run state/outputs to live in well-defined,
isolated, configurable locations,
So that backtests never contend with the live orchestrator or with each other, and every
agent can consult the same production data and live-run logs.

## Problem Statement

Today all of these are hardcoded relative to the worktree CWD:
- Production parquets are read AND appended in-place at `data/*.parquet`; a backtest reader
  holding the file open makes the live writer's atomic `os.replace(.tmp → .parquet)` fail
  (`[WinError 5]`, observed post-session 2026-06-02).
- Live sessions live at `sessions/<date>/` inside the worktree, invisible to other worktrees.
- Strategy state JSONs (`global/daily/hypothesis/position.json`) live at `data/*.json`,
  shared process-globally; concurrent backtests would clobber each other's state.
- Regression outputs at `data/regression/<date>/` overwrite on every re-run — no per-run
  isolation, no record of which code version produced them.

## Solution Statement

1. **`paths.py`** — central, env-overridable path resolver. Global root defaults to
   `~/projects/auto-co-trader/global/`.
2. **Parquets** — live orchestrator appends to `<global>/data/live/`; backtests read
   `<global>/data/main/`; `parquet-check` promotes `live → main` (+ backups) after a
   successful post-session run. Live writer and backtest readers never share a file.
3. **Sessions** — `<global>/sessions/<date>/`; `run-orchestrator` records the running
   commit in that session's `comments.md` at startup.
4. **State JSONs** — all state IO funnels through a settable **state-dir prefix**: live →
   the session folder; backtest → the per-run regression folder. Backtest stays in-memory
   (isolated per run) and writes ONE final snapshot to the run folder.
5. **Regression** — move from `data/regression/` to a **worktree-root `regression/`**
   (gitignored), with a per-run subfolder named by **TH (Asia/Bangkok) start time
   `HH-mm-ss`**; all of that run's outputs + temp files + an `info.md` live inside it.

## Feature Metadata

**Feature Type**: Refactor (infrastructure)
**Complexity**: High
**Primary Systems Affected**: `paths.py` (new), `smt_state.py`, `data/ib_realtime.py`,
`data/parquet_maintenance.py`, `backtest_smt.py`, `strategy_smt.py`, `regression.py`,
`data/regression/plot_regression.py`, `orchestrator/main.py`, `plot_session.py`, `daily.py`,
`session_pipeline.py`, `live_orders.py`; skills `parquet-check`, `run-orchestrator`,
`session-analysis`, `live-trading`.
**Dependencies**: none new (stdlib `os`, `pathlib`, `zoneinfo`).
**Breaking Changes**: Yes — file locations move. A one-time migration script relocates
existing data/sessions/regression; defaults preserve behavior where possible. Live trading
must not break (Phase 5 is the riskiest and is gated + verified).

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ BEFORE IMPLEMENTING

- `smt_state.py` (lines 34–46, 91–113, 138–209) — Why: the **chokepoint** for state JSON IO
  (`DATA_DIR`, `GLOBAL/DAILY/HYPOTHESIS/POSITION_PATH`, `_atomic_write`, in-memory `_STORE`,
  `set_session_date`, `bar_state_path`). Phase 5 hinges on this.
- `data/ib_realtime.py` (lines 142–160, 237–256, 431–452) — Why: live parquet read/append via
  the already-parameterized `self._bar_data_dir`; atomic write pattern.
- `data/parquet_maintenance.py` (lines 36–57, 118–139, 269–361) — Why: backfill + session-1s
  merge; takes a `bar_data_dir` param; atomic `os.replace`.
- `backtest_smt.py` (lines 54–60, 1196, 1200–1209, 1284–1286, 1489–1491) — Why: `FUTURES_CACHE_DIR`,
  in-memory mode toggle, dual-source 1s read, regression-dir + levels.json writes.
- `strategy_smt.py` (lines 12–15, 659–695) — Why: `FUTURES_CACHE_DIR` + `load_futures_data`
  fallback chain (mirror the new main-dir source here).
- `regression.py` (lines 104–115) — Why: `reg_dir = Path("data")/"regression"/date`; output paths.
- `data/regression/plot_regression.py` (lines 33, 39–55, 559) — Why: `REG_DIR`, chart out path,
  reads `data/MNQ_1s.parquet` directly.
- `orchestrator/main.py` (lines 37, 40–53, 105, 60) — Why: `_SESSIONS_DIR`, session channels,
  `bar_data_dir = Path("data")`, `load_position`.
- `plot_session.py` (lines 26–38, 67) — Why: hardcoded `SESSION_DIR = Path(f"sessions/{DATE}")`.
- `daily.py` (lines 13–16, 165+) — Why: direct `load_global/load_daily/save_*` consumers.
- `session_times.py` (lines 13–37) — Why: `cme_session_date`, `session_date_str`, TH-tz logic
  to reuse for the run-folder `HH-mm-ss` TH timestamp.
- `.gitignore` (lines 38–61) — Why: currently ignores `sessions/`, `data/*.parquet`,
  `data/regression/*/`, `data/*.json`; must add worktree `regression/`.

### New Files to Create

- `paths.py` — central env-overridable path resolver (project root).
- `scripts/migrate_to_global_paths.py` — one-time migration of existing data/sessions/regression.
- `tests/test_paths.py` — unit tests for `paths.py` resolution + env overrides.
- `tests/test_state_prefix.py` — state-dir prefix isolation + final-snapshot tests.
- `tests/test_regression_run_dirs.py` — per-run TH-named folder + info.md tests.

### Patterns to Follow

- **Naming**: snake_case modules; `UPPER_SNAKE` constants; env vars read via
  `os.environ.get("NAME", default)` (mirror `FUTURES_CACHE_DIR`, `backtest_smt.py:54-60`).
- **Path resolution**: `pathlib.Path`, `Path.expanduser()`, `Path.mkdir(parents=True, exist_ok=True)`.
- **Atomic write**: keep the existing `.tmp` + `os.replace` pattern with the `PermissionError`
  fallback (`smt_state.py:103-113`).
- **TH timezone**: reuse `zoneinfo.ZoneInfo("Asia/Bangkok")` exactly as `session_times.py`.
- **Tests**: pytest under `tests/test_*.py`; run with `uv run python -m pytest tests/<file> -q`.
- **Production code is silent**: no print to stdout in production paths (per CLAUDE.md).

---

## PARALLEL EXECUTION STRATEGY

### Dependency Graph

```
┌─────────────────────────────────────────────────────────────┐
│ WAVE 1: Foundation                                          │
│ Task 1.1: CREATE paths.py (env-overridable resolver)        │
│ Agent: backend-specialist     BLOCKS: all                   │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│ WAVE 2: Path adoption (fully parallel — disjoint files)     │
│ 2.1 Parquets live/main split  │ 2.2 Sessions → global       │
│ 2.3 Regression → worktree per-run folders + info.md         │
│ Agent: backend-specialist ×3                                │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│ WAVE 3: Skill + wiring updates (parallel)                   │
│ 3.1 parquet-check promote live→main (needs 2.1)             │
│ 3.2 run-orchestrator commit-note (needs 2.2)                │
│ 3.3 session-analysis/live-trading/plot path updates (2.2/2.3)│
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│ WAVE 4: GUARDED — State-JSON prefix relocation (riskiest)   │
│ 4.1 smt_state prefix + in-memory isolation + final snapshot │
│ 4.2 wire live (session dir) + backtest (run dir) + ATH      │
│ Agent: backend-specialist (single owner; touches live IO)   │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│ WAVE 5: Migration + gitignore + equivalence verification    │
│ 5.1 migration script  5.2 .gitignore  5.3 byte-identical    │
│     backtest-output verification vs pre-move baseline       │
└─────────────────────────────────────────────────────────────┘
```

### Parallelization Summary

- **Wave 1** — single foundation task (everything depends on it).
- **Wave 2** — Tasks 2.1 / 2.2 / 2.3 fully parallel (disjoint file sets, all consume `paths.py`).
- **Wave 3** — 3.1 / 3.2 / 3.3 parallel (skill files; depend on the matching Wave-2 task).
- **Wave 4** — single owner, sequential (live-trading state IO; highest risk).
- **Wave 5** — 5.1/5.2 parallel; 5.3 after all.
- ~6 of 11 tasks parallelizable (>50%).

### Interface Contracts

- **Contract 1 (paths.py API)** — Task 1.1 provides, all others consume:
  - `global_root() -> Path` (env `ACT_GLOBAL_DIR`, default `~/projects/auto-co-trader/global`)
  - `data_live_dir() -> Path` (`<global>/data/live`) — live append target
  - `data_main_dir() -> Path` (`<global>/data/main`) — backtest read source
  - `sessions_dir() -> Path` (`<global>/sessions`)
  - `regression_dir() -> Path` (env `ACT_REGRESSION_DIR`, default `<cwd>/regression`)
  - `regression_run_dir(date: str, started: datetime) -> Path`
    (`<regression>/<date>/<HH-mm-ss TH>`)
  - `state_dir() -> Path` / `set_state_dir(path)` — the settable per-context state prefix
  - Each getter ensures the directory exists (mkdir parents).
- **Mock for parallel work**: Wave-2 tasks may import the contract names before 1.1 lands by
  stubbing `paths` with the signatures above; replace with the real import at integration.

### Synchronization Checkpoints

- **After Wave 1**: `uv run python -m pytest tests/test_paths.py -q`
- **After Wave 2**: `uv run python -c "import paths, regression, backtest_smt, data.ib_realtime"`
  (imports clean) + `uv run python -m pytest tests/test_ib_realtime.py tests/test_parquet_maintenance.py tests/test_smt_regression.py -q`
- **After Wave 4**: `uv run python -m pytest tests/test_smt_state.py tests/test_state_prefix.py tests/test_session_pipeline.py -q`
- **After Wave 5 (equivalence gate)**: backtest output for a fixed date is byte-identical to
  a baseline captured before the move (see Task 5.3).

---

## IMPLEMENTATION PLAN

### Phase 1: Foundation — `paths.py`

Create the single source of truth. All base dirs are env-overridable with defaults; every
getter `mkdir(parents=True, exist_ok=True)` its dir. The state prefix is a module-level
settable (mirroring `smt_state.set_session_date`) defaulting to the legacy `data/` so
nothing breaks before Wave 4.

### Phase 2: Path adoption (parquets / sessions / regression)

Replace hardcoded relatives with `paths.*` getters in the three disjoint file groups. Live
parquet append → `data_live_dir()`; backtest read → `data_main_dir()` (with the existing
`FUTURES_CACHE_DIR` as a secondary fallback). Sessions root → `sessions_dir()`. Regression
output dir → `regression_run_dir(date, started)`; write `info.md` per run.

### Phase 3: Skill + wiring updates

`parquet-check` promotes `live → main`; `run-orchestrator` writes the commit into
`comments.md` at start; `session-analysis`/`live-trading`/plot scripts point at the new
sessions + regression locations.

### Phase 4: State-JSON prefix relocation (GUARDED — last)

Route all state IO through `paths.state_dir()`. Live sets it to the session folder; backtest
sets it to the per-run folder and keeps in-memory mode but isolates `_STORE` per run and
writes one final snapshot. Preserve cross-session ATH (seed `global.json` at session start
from the prior session / history).

### Phase 5: Migration + verification

Move existing files; update `.gitignore`; prove backtest output is unchanged by the move.

---

## STEP-BY-STEP TASKS

**Task keywords**: CREATE · UPDATE · REFACTOR · ADD · REMOVE

### WAVE 1: Foundation

#### Task 1.1: CREATE `paths.py`

- **WAVE**: 1
- **AGENT_ROLE**: backend-specialist
- **DEPENDS_ON**: []
- **BLOCKS**: 2.1, 2.2, 2.3, 4.1, 4.2, 5.1
- **PROVIDES**: the Contract 1 API.
- **IMPLEMENT**:
  - Env vars: `ACT_GLOBAL_DIR` (default `Path("~/projects/auto-co-trader/global").expanduser()`),
    `ACT_REGRESSION_DIR` (default `Path.cwd()/"regression"`).
  - Getters per Contract 1; each `mkdir(parents=True, exist_ok=True)`.
  - `regression_run_dir(date, started)`: `started` converted to `ZoneInfo("Asia/Bangkok")`,
    formatted `%H-%M-%S`; path `<regression>/<date>/<HH-MM-SS>`.
  - `state_dir()`/`set_state_dir(path)`: module-global, default `Path("data")` (legacy) so
    pre-Wave-4 behavior is unchanged.
  - Do NOT import heavy modules (keep import-cheap; only `os`, `pathlib`, `datetime`, `zoneinfo`).
- **PATTERN**: `backtest_smt.py:54-60` (env+default), `session_times.py:13-37` (TH tz).
- **VALIDATE**: `uv run python -m pytest tests/test_paths.py -q`

**Wave 1 Checkpoint**: `uv run python -c "import paths; print(paths.global_root(), paths.regression_run_dir('2026-06-02', __import__('datetime').datetime.now(__import__('zoneinfo').ZoneInfo('America/New_York'))))"`

---

### WAVE 2: Path adoption (parallel)

#### Task 2.1: REFACTOR parquet IO to live/main split

- **WAVE**: 2
- **AGENT_ROLE**: backend-specialist
- **DEPENDS_ON**: [1.1]
- **BLOCKS**: 3.1
- **PROVIDES**: live append → `data_live_dir()`; backtest read → `data_main_dir()`.
- **IMPLEMENT**:
  - `orchestrator/main.py:105` — set `bar_data_dir = paths.data_live_dir()` (was `Path("data")`).
  - `data/ib_realtime.py` — no path literals to change if `_bar_data_dir` is the only source
    (verify lines 155-159, 237-256, 431-452 all use `self._bar_data_dir`); keep session-1s
    temp files under `data_live_dir()`.
  - `data/parquet_maintenance.py` — confirm `bar_data_dir` param is threaded from callers; the
    session-1s merge target is `data_live_dir()` (live), promotion to main handled in 3.1.
  - `backtest_smt.py:1200-1209` and `strategy_smt.py:659-695` — primary read becomes
    `paths.data_main_dir()/<ticker>_<interval>.parquet`; keep `FUTURES_CACHE_DIR` as secondary
    fallback; remove the in-project `Path("data")` primary for backtest reads.
  - `backtest_smt.py:436` legacy `Path("data/MNQ.parquet")` → `data_main_dir()` equivalent.
- **VALIDATE**: `uv run python -m pytest tests/test_ib_realtime.py tests/test_parquet_maintenance.py -q`
- **INTEGRATION_TEST**: `uv run python -c "import data.ib_realtime, backtest_smt, strategy_smt"`

#### Task 2.2: REFACTOR sessions root → global

- **WAVE**: 2
- **AGENT_ROLE**: backend-specialist
- **DEPENDS_ON**: [1.1]
- **BLOCKS**: 3.2, 3.3, 4.2
- **PROVIDES**: session folder = `paths.sessions_dir()/<date>`.
- **IMPLEMENT**:
  - `orchestrator/main.py:37` — `_SESSIONS_DIR = paths.sessions_dir()`.
  - `plot_session.py:38` — `SESSION_DIR = paths.sessions_dir()/DATE`.
  - `smt_state.py:181-190` — `bar_state_path()` builds under `paths.sessions_dir()` (interim;
    fully subsumed by the state prefix in Wave 4, but keep correct now).
  - Any other `Path("sessions")` / `sessions/{...}` literals (grep to confirm) → `paths.sessions_dir()`.
- **VALIDATE**: `uv run python -m pytest tests/test_orchestrator_main.py tests/test_bar_state.py -q`

#### Task 2.3: REFACTOR regression outputs → worktree per-run folders + info.md

- **WAVE**: 2
- **AGENT_ROLE**: backend-specialist
- **DEPENDS_ON**: [1.1]
- **BLOCKS**: 3.3, 4.2
- **PROVIDES**: `regression_run_dir(date, started)` holds all of a run's outputs + `info.md`.
- **IMPLEMENT**:
  - `regression.py:104-115` — compute `run_dir = paths.regression_run_dir(date, started)` once at
    run start (`started` = now in ET); write `events{_sfx}.jsonl`, `trades{_sfx}.tsv`,
    `baseline_*` into `run_dir`. Baselines: resolve from the **latest prior run** for the date
    (or a date-level `baseline/` pointer) — keep baseline semantics working across per-run dirs
    (design note: store baselines at `<regression>/<date>/baseline/` so they're stable across runs).
  - `backtest_smt.py:1284-1286,1489-1491` — `levels.json` + outputs into `run_dir`.
  - `data/regression/plot_regression.py:33,559` — `REG_DIR`/chart out under the run dir; its
    `data/MNQ_1s.parquet` read → `paths.data_main_dir()`.
  - Move temp files (`_reg*.txt`, any scratch) into `run_dir`.
  - Write `info.md` at run start: code version (`git rev-parse HEAD` + dirty flag), mode (1s/1m),
    date, TH start time, baseline ref used. (Field specifics are intentionally minimal; leave a
    clearly-marked TODO block for later expansion.)
- **VALIDATE**: `uv run python -m pytest tests/test_smt_regression.py tests/test_regression_run_dirs.py -q`

**Wave 2 Checkpoint**: `uv run python -m pytest tests/test_ib_realtime.py tests/test_parquet_maintenance.py tests/test_orchestrator_main.py tests/test_bar_state.py tests/test_smt_regression.py -q`

---

### WAVE 3: Skill + wiring updates (parallel)

#### Task 3.1: UPDATE parquet-check — promote live→main

- **WAVE**: 3
- **AGENT_ROLE**: backend-specialist
- **DEPENDS_ON**: [2.1]
- **IMPLEMENT**: `scripts/check_session_parquets.py` + `.claude/skills/parquet-check/SKILL.md` —
  after a successful session-end merge into `data_live_dir()`, **promote** (copy + backup) the
  validated parquets `data_live_dir() → data_main_dir()` atomically; write `.bak` of the prior
  main. Skill text updated to describe the live→main promotion as the final step.
- **VALIDATE**: `uv run python -m pytest tests/test_check_session_parquets.py -q`

#### Task 3.2: UPDATE run-orchestrator — record running commit in comments.md

- **WAVE**: 3
- **AGENT_ROLE**: backend-specialist
- **DEPENDS_ON**: [2.2]
- **IMPLEMENT**: `.claude/skills/run-orchestrator/SKILL.md` — at session start, write the running
  commit (`git rev-parse HEAD` + short subject + dirty flag) into the session's `comments.md`
  (reusing the live-comment write mechanism / `live_orders` comment append). Document that this
  runs once at startup. (No invocation here — skill text + the supporting code path only.)
- **VALIDATE**: manual skill read-through + a unit test asserting the commit-note writer produces
  the expected `comments.md` line given a stub commit (`tests/test_state_prefix.py` or a small
  new test). ✅ automatable.

#### Task 3.3: UPDATE session-analysis / live-trading / plot path references

- **WAVE**: 3
- **AGENT_ROLE**: backend-specialist
- **DEPENDS_ON**: [2.2, 2.3]
- **IMPLEMENT**: `.claude/skills/session-analysis/SKILL.md` and `.claude/skills/live-trading/SKILL.md`
  — update every `sessions/<date>/...` and `data/regression/<date>/...` reference to the new
  global sessions dir and worktree `regression/<date>/<run>/` layout (including the double-plot
  and regression-read steps). `plot_session.py` / `plot_regression.py` already covered in Wave 2;
  here just reconcile the skill docs to the new paths.
- **VALIDATE**: grep the two SKILL.md files for stale `data/regression` / in-project `sessions/`
  references → none remain. ✅

---

### WAVE 4: State-JSON prefix relocation (GUARDED — single owner)

#### Task 4.1: REFACTOR `smt_state.py` to a state-dir prefix + per-run isolation + final snapshot

- **WAVE**: 4
- **AGENT_ROLE**: backend-specialist
- **DEPENDS_ON**: [2.2, 2.3]
- **BLOCKS**: 4.2
- **IMPLEMENT**:
  - Replace `DATA_DIR`/`*_PATH` constants (smt_state.py:34-38) with path **functions** that
    resolve under `paths.state_dir()` at call time (so a mid-run `set_state_dir` takes effect):
    `_global_path()`, `_daily_path()`, `_hypothesis_path()`, `_position_path()`.
  - In-memory mode (`_STORE`) keyed by the resolved path string is already per-path; ensure the
    key includes the state dir so two runs with different `state_dir` never collide. Add a
    `reset_in_memory()` at run start.
  - `final_snapshot()` — dump current `_STORE` (or the four state files) to the configured
    `state_dir()` as real JSON files (for backtest inspection).
  - Keep `_atomic_write` + `PermissionError` fallback unchanged.
- **VALIDATE**: `uv run python -m pytest tests/test_smt_state.py tests/test_state_prefix.py -q`

#### Task 4.2: WIRE live (session dir) + backtest (run dir) state + ATH continuity

- **WAVE**: 4
- **AGENT_ROLE**: backend-specialist
- **DEPENDS_ON**: [4.1]
- **IMPLEMENT**:
  - Live (`orchestrator/main.py` / session start): `paths.set_state_dir(paths.sessions_dir()/date)`
    so `global/daily/hypothesis/position.json` live in the session folder (alongside bar_state.json).
  - Backtest (`backtest_smt.py:1196` area): `paths.set_state_dir(run_dir)` +
    `smt_state.reset_in_memory()` per date/run; call `smt_state.final_snapshot()` at run end.
  - `daily.py:13-16,165+` and `live_orders.py` consumers: confirm they only touch state via
    smt_state functions (no direct path literals); fix any direct access.
  - **Cross-session ATH continuity**: `global.json` is per-session now → at session start, seed
    `all_time_high` from the prior session's `global.json` (or from history) instead of the fresh
    default, preserving the dynamic ATH. Add a `seed_global_from_prior()` helper; document the
    rule. (This is the single most behavior-sensitive change — cover with a dedicated test.)
- **VALIDATE**: `uv run python -m pytest tests/test_session_pipeline.py tests/test_smt_state.py tests/test_state_prefix.py -q`

**Wave 4 Checkpoint**: `uv run python -m pytest tests/test_smt_state.py tests/test_state_prefix.py tests/test_session_pipeline.py tests/test_live_orders.py -q`

---

### WAVE 5: Migration + gitignore + equivalence verification

#### Task 5.1: CREATE `scripts/migrate_to_global_paths.py`

- **WAVE**: 5
- **AGENT_ROLE**: backend-specialist
- **DEPENDS_ON**: [2.1, 2.2, 2.3]
- **IMPLEMENT**: idempotent one-time migration — create `<global>/data/{live,main}` and
  `<global>/sessions`; copy existing `data/*.parquet` (+ `.bak`) into BOTH `data/main/` and
  `data/live/` (seed); move `sessions/*` → `<global>/sessions/`; move `data/regression/*` →
  `<cwd>/regression/` (preserving date folders). Dry-run flag; refuse to overwrite non-empty
  targets without `--force`. Print a summary (this is a one-off script, stdout allowed).
- **VALIDATE**: run with `--dry-run` on the real tree; assert the planned moves; then a pytest
  using a temp tree verifies the move logic. `uv run python -m pytest tests/test_paths.py -q`
  (migration unit cases co-located) + `uv run python scripts/migrate_to_global_paths.py --dry-run`.

#### Task 5.2: UPDATE `.gitignore`

- **WAVE**: 5
- **AGENT_ROLE**: backend-specialist
- **DEPENDS_ON**: [2.3]
- **IMPLEMENT**: add `regression/` (worktree-root regression outputs) to `.gitignore`. The global
  folder is outside any worktree so needs no ignore. Leave existing `sessions/`, `data/*.parquet`,
  `data/*.json`, `data/regression/*/` entries (harmless once empty; optionally annotate as legacy).
- **VALIDATE**: `git check-ignore regression/2026-06-02/12-00-00/info.md` → ignored.

#### Task 5.3: VERIFY backtest output unchanged by the move (equivalence gate)

- **WAVE**: 5
- **AGENT_ROLE**: backend-specialist
- **DEPENDS_ON**: [2.1, 2.3, 4.1, 4.2]
- **IMPLEMENT**: capture a **pre-move baseline** before this feature (or from current
  `data/regression/2026-06-02/events_1s.jsonl`/`trades_1s.tsv`), then run
  `run_regression(dates=["2026-06-02"], mode="1s")` post-refactor and assert the new run's
  `events_1s.jsonl` + `trades_1s.tsv` are **byte-identical** (modulo the new run-folder location).
  This proves the path move did not change strategy behavior (guardrail: a path refactor must be
  output-neutral). Encode as an automated test that reads both ledgers and diffs them.
- **VALIDATE**: `uv run python -m pytest tests/test_regression_run_dirs.py -q` (equivalence case)

**Final Checkpoint**: full suite `uv run python -m pytest tests/ -q` — no new failures vs the
pre-feature baseline; equivalence test green; a live dry sanity (`orchestrator` import + state
dir resolves to a session folder) passes.

---

## TESTING STRATEGY

**⚠️ ALL tests that can be automated MUST be automated.** This feature is pure backend/path
logic — everything is automatable with pytest; no UI, no browser, no third-party calls.

| What you're testing | Tool |
|---|---|
| Path resolution, env overrides, state prefix, regression run dirs, migration | `pytest` (`tests/`) |

### Unit Tests

**Status**: ✅ Automated | **Tool**: pytest | **Location**: `tests/` | **Run**: `uv run python -m pytest tests/<file> -q`

- `tests/test_paths.py` — defaults; `ACT_GLOBAL_DIR`/`ACT_REGRESSION_DIR` overrides; getters
  create dirs; `regression_run_dir` formats TH `HH-MM-SS` correctly (assert with a fixed input
  datetime, converting ET→TH); `state_dir`/`set_state_dir` round-trip; migration move logic on a
  temp tree (happy + refuse-overwrite + dry-run).
- `tests/test_state_prefix.py` — `set_state_dir(A)` then `set_state_dir(B)` writes to disjoint
  files; in-memory `_STORE` keys include the state dir (no cross-run clobber); `final_snapshot()`
  writes the four JSONs into the run dir; `seed_global_from_prior()` carries `all_time_high`
  forward; the run-orchestrator commit-note writer emits the expected `comments.md` line.
- `tests/test_regression_run_dirs.py` — a run writes all outputs + `info.md` into
  `<regression>/<date>/<HH-MM-SS>/`; `info.md` contains the code version + mode + TH time;
  baseline resolves from `<date>/baseline/`; **equivalence**: post-move `events_1s.jsonl` +
  `trades_1s.tsv` byte-identical to a captured pre-move baseline.

### Integration Tests

**Status**: ✅ Automated | **Tool**: pytest | **Run**: `uv run python -m pytest tests/test_smt_regression.py tests/test_session_pipeline.py tests/test_check_session_parquets.py -q`

- Existing `test_smt_regression.py`, `test_session_pipeline.py`, `test_orchestrator_main.py`,
  `test_bar_state.py`, `test_ib_realtime.py`, `test_parquet_maintenance.py`,
  `test_check_session_parquets.py`, `test_smt_state.py`, `test_live_orders.py` updated for the
  new paths and re-run as regression coverage.

### End-to-End Test

**Status**: ✅ Automated (no UI) — the **equivalence gate** (Task 5.3) is the E2E: a full 1s
regression for 2026-06-02 runs against the relocated paths and produces byte-identical ledgers.

### Manual Tests

- **Live orchestrator session write** (Phase 5): one real session writing its state JSONs into
  `<global>/sessions/<date>/` and `parquet-check` promoting live→main.
  **Why manual**: requires a live IB Gateway session (hardware/credential constraint) — consistent
  with every prior live feature in this repo (PROGRESS.md notes live-only gaps). All *logic* is
  unit-covered; only the real-session wiring is manual.

### Edge Cases

- **Env override present vs absent** — ✅ `tests/test_paths.py`
- **TH date-boundary for run folder** (ET evening → next TH day) — ✅ `tests/test_paths.py`
- **Concurrent runs, different state dirs, no clobber** — ✅ `tests/test_state_prefix.py`
- **Missing prior `global.json` → ATH seeds from history, not crash** — ✅ `tests/test_state_prefix.py`
- **Migration idempotency / refuse-overwrite** — ✅ `tests/test_paths.py`
- **Backtest read falls back to `FUTURES_CACHE_DIR` when `data/main` missing** — ✅ `tests/test_smt_regression.py`

### Test Automation Summary

| | Count | % |
|---|---|---|
| ✅ Backend (pytest) — new | ~22 | |
| ✅ Backend (pytest) — updated existing | ~9 files | |
| ⚠️ Manual (live IB session) | 1 | |
| **Total automated** | ~31 | ~97% |

**Goal**: 100% path-logic coverage; the single manual test is a hardware-bound live-session
smoke, justified above.

**Execution agent**: CREATE all automated test files as implementation tasks; RUN after each
wave checkpoint.

---

## VALIDATION COMMANDS

### Level 1: Syntax & Imports
```bash
uv run python -c "import paths, smt_state, backtest_smt, regression, data.ib_realtime, data.parquet_maintenance, strategy_smt, orchestrator.main, plot_session"
```

### Level 2: Unit Tests
```bash
uv run python -m pytest tests/test_paths.py tests/test_state_prefix.py tests/test_regression_run_dirs.py -q
```

### Level 3: Integration Tests
```bash
uv run python -m pytest tests/test_smt_state.py tests/test_smt_regression.py tests/test_session_pipeline.py tests/test_ib_realtime.py tests/test_parquet_maintenance.py tests/test_orchestrator_main.py tests/test_bar_state.py tests/test_check_session_parquets.py tests/test_live_orders.py -q
```

### Level 4: E2E / Equivalence + Full Suite
```bash
uv run python scripts/migrate_to_global_paths.py --dry-run
uv run python -m pytest tests/ -q          # no new failures vs pre-feature baseline
# equivalence gate (Task 5.3) is inside tests/test_regression_run_dirs.py
```

---

## ACCEPTANCE CRITERIA

### Functional
- [ ] `paths.py` resolves all base dirs, env-overridable (`ACT_GLOBAL_DIR`, `ACT_REGRESSION_DIR`), auto-creating each dir
- [ ] Live parquet append → `<global>/data/live/`; backtest read → `<global>/data/main/` (with `FUTURES_CACHE_DIR` fallback); no in-project `data/*.parquet` primary remains in live/backtest IO
- [ ] `parquet-check` promotes validated `live → main` (+ backup) after a successful post-session run
- [ ] Live sessions write to `<global>/sessions/<date>/`; `run-orchestrator` records the running commit in that session's `comments.md` at startup
- [ ] All strategy state JSONs resolve under `paths.state_dir()`: live → session folder; backtest → per-run folder (in-memory isolated + one final snapshot)
- [ ] Regression outputs live in `<worktree>/regression/<date>/<HH-mm-ss TH>/` with all run files + `info.md`; `regression/` gitignored

### Error Handling / Edge
- [ ] Cross-session ATH continuity preserved — missing prior `global.json` seeds `all_time_high` from history without crashing
- [ ] Migration is idempotent, refuses to overwrite non-empty targets without `--force`, and `--dry-run` works
- [ ] Backtest falls back to `FUTURES_CACHE_DIR` when `data/main` is missing; TH date-boundary handled in run-folder naming

### Integration / E2E
- [ ] **Equivalence gate**: post-refactor 1s regression for 2026-06-02 is byte-identical to the pre-move baseline ledgers
- [ ] Concurrent runs with different state dirs do not clobber each other's state

### Validation
- [ ] New unit tests green — verified by: `uv run python -m pytest tests/test_paths.py tests/test_state_prefix.py tests/test_regression_run_dirs.py -q`
- [ ] Full suite no new failures vs pre-feature baseline — verified by: `uv run python -m pytest tests/ -q`
- [ ] Migration dry-run clean — verified by: `uv run python scripts/migrate_to_global_paths.py --dry-run`

### Non-Functional
- [ ] Production paths emit no stdout (silent-production rule); changes left UNSTAGED — NOT committed

### Out of Scope
- The agentic optimization workflow (`strategy-opt-team.md`) — prerequisite only, not part of this
- `info.md` full field schema (minimal now; user to specify later)
- Live-session manual smoke (deferred — requires live IB Gateway)

---

## COMPLETION CHECKLIST

- [ ] All tasks completed in wave order
- [ ] Each task validation passed
- [ ] All validation levels (1–4) executed
- [ ] All automated tests created and passing
- [ ] Manual live-session smoke documented (deferred — requires live IB)
- [ ] Equivalence gate green
- [ ] No linting/type errors
- [ ] All acceptance criteria met
- [ ] **⚠️ Debug logs added during execution REMOVED**
- [ ] **⚠️ CRITICAL: Changes UNSTAGED — NOT committed**

---

## NOTES

- **Phasing rationale**: Phase 4 (state-JSON prefix) is the only change that touches the live
  trading hot path and cross-session ATH — it is sequenced last, single-owner, and gated by the
  equivalence test, so the lower-risk path moves (parquets/sessions/regression) land and stabilize
  first.
- **Why live=`data/live` and main=`data/main` (both global)**: per the user decision — the live
  writer and backtest readers never share a file, eliminating the Windows rename-over-open
  contention. `main` lagging by one session is fine: backtests use historical dates, never today.
- **Baseline-across-runs**: moving regression to per-run folders must NOT break A/B baselines —
  store baselines at `<regression>/<date>/baseline/` (stable across runs) rather than inside a
  timestamped run folder. Confirm `regression.py --update-baseline` writes there.
- **`info.md` fields**: minimal now (code version, mode, date, TH start, baseline ref); the user
  will specify the full schema later — leave a clearly marked TODO.
- **Out of scope**: the agentic optimization workflow (`strategy-opt-team.md`) — this restructure
  is its prerequisite, not part of it.
- **Migration is one-time**: after it runs, the in-project `data/*.parquet`, `data/*.json`,
  `sessions/`, `data/regression/` become legacy/empty; the `.gitignore` entries are harmless.
