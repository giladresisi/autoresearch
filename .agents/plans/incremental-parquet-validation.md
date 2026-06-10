# Feature: Incremental parquet validation (GIL-15)

**⚠️ EXECUTION RULES — READ FIRST:**
- Implement ALL changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
- Only make code changes — no git operations

Validate documentation and codebase patterns before implementing. Match naming of existing utils, types, and models. Import from correct files.

---

## Feature Description

The parquet-check engine (`scripts/check_session_parquets.py`) re-reads and re-scans the **entire** main 1m parquet on every invocation, even when only a small gap-filled tail is new. This feature makes the 1m validation **incremental**: after a one-time full validation, subsequent runs validate only the bars appended since the last validation, plus the concatenation seam onto the already-trusted body, then advance a persisted **validation watermark**. A body-integrity guard detects when the parquet was rewritten/truncated (not merely appended) and forces a full re-validation, so correctness is never traded for speed. The 1s merge path gets an explicit seam check at the concatenation point. The parquet-check skill doc is updated for the new sidecar write, new report fields, and a `--full-validate` flag.

User value: removes wasted I/O + CPU that grows unbounded with history length on a hot path run at every session-end and orchestrator-start, with no loss of validation rigor.

## User Story

As a **trader/operator running the parquet-check skill at each session boundary**
I want to **validate only the newly gap-filled tail and its seam instead of re-scanning the entire multi-hundred-thousand-row main parquet every time**
So that **the check stays fast and cheap as history grows, while still catching any corruption in new data or at the join.**

## Problem Statement

`check_1m_parquet` (`scripts/check_session_parquets.py:456`) does `pd.read_parquet(main_path)` — materializing all ~860k rows — then `bad = df[df["Close"] <= 0]` (`:483`) scans the whole frame, and on every healthy run `backup_main()` (`:493-494`) copies the full file to `.bak`. On the 2026-06-09 `orchestrator-start` run (Linear GIL-15, background task `bj7axjc1r`), both ~861k-row 1m parquets (MNQ `rows: 861353`, MES `rows: 861098`, both `bad_rows: 0`) were fully read/scanned purely to confirm a ~30-bar tail (`instruments.MNQ.validation.rows: 29`, MES `11`). Every prior run repeats the same full scan over an already-validated body. The cost scales with total history and runs on every session boundary.

## Solution Statement

Persist a per-parquet validation watermark in a sidecar JSON. On each run:
1. If no watermark, or watermark invalid (validator version bumped, body-integrity guard fails, truncation, or `--full-validate`) → run the existing **full** validation, then write the watermark.
2. Otherwise → read only the trailing row-group(s) past the watermark via pyarrow, validate that tail (price/OHLC/`Close<=0` + gap scan), check the seam between the watermark bar and the first new bar (reusing `_is_expected_closed`), and on clean advance the watermark.

Foundation utilities (watermark store + pyarrow tail reader) are new, self-contained modules to keep the work parallelizable and conflict-free. The core refactor wires them into `check_1m_parquet`. The 1s merge seam check and SKILL.md doc changes finish it.

## Feature Metadata

**Feature Type**: Refactor / Enhancement (performance, no behavior change on correct data)
**Complexity**: Medium
**Primary Systems Affected**: `scripts/check_session_parquets.py` (1m validation), `data/parquet_maintenance.py` (1s merge seam), `.claude/skills/parquet-check/SKILL.md`
**Dependencies**: `pyarrow` 23.0.1 (already installed — pandas parquet engine), `pandas`
**Breaking Changes**: No. JSON report gains fields (`validation_scope`, `validated_through`); existing fields unchanged. First run after deploy has no sidecar → behaves exactly like today (full validation) and seeds the watermark.

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ BEFORE IMPLEMENTING

- `scripts/check_session_parquets.py` (lines 456–537) — `check_1m_parquet`: the full read+scan+`.bak` path to make incremental. **Primary refactor target.**
- `scripts/check_session_parquets.py` (lines 480–496) — the healthy-path branch: `Close<=0` scan, `.bak` refresh, early return — where the incremental/full decision plugs in.
- `scripts/check_session_parquets.py` (lines 192–258) — `validate_session_df`: the canonical tail validator (price bounds, OHLC sanity, gap scan, severity). Reuse on the tail slice.
- `scripts/check_session_parquets.py` (lines 53–77) — `_is_expected_closed`: weekend/maintenance gap classifier. Reuse for the seam check.
- `scripts/check_session_parquets.py` (lines 295–306) — `write_atomic`, `backup_main`: atomic write + `.bak` patterns to mirror for the sidecar write.
- `scripts/check_session_parquets.py` (lines 363–372) — `gap_fill_to_now`: confirms appends are tail-only (main[-1]→now).
- `scripts/check_session_parquets.py` (lines 498–537) — repair path: after a repair the watermark must be reset/advanced.
- `scripts/check_session_parquets.py` (lines 625–749) — `main()`: CLI args (`--mode`, `--dry-run`), report assembly, exit codes. Add `--full-validate`/`--since`.
- `data/parquet_maintenance.py` (lines 85–99) — `_safe_read_parquet` index-only read via `pd.read_parquet(path, columns=[])`: **the cheap-read precedent** to mirror for metadata reads.
- `data/parquet_maintenance.py` (lines 269–360) — `merge_session_1s_parquets`: the 1s concat path (`pd.concat([existing, session_df])`) that needs an explicit seam check.
- `tests/test_check_session_parquets.py` (lines 1–420) — test conventions: `tmp_path` `bar_dir` fixture, `_make_session_df`, `_make_ib_mock`, class grouping, function-level imports of the script, MagicMock IB.
- `tests/test_parquet_maintenance.py` — existing 1s merge tests to extend for the seam check.
- `.claude/skills/parquet-check/SKILL.md` — HARD RULE "Allowed writes" (parquet-only today), Step 2 (Parse & assess), Step 3 (LLM severity).

### New Files to Create

- `scripts/parquet_validation_state.py` — watermark sidecar load/save, `VALIDATOR_VERSION`, body-integrity guard, invalidation decision.
- `scripts/parquet_tail.py` — pyarrow metadata + tail-read helpers (index bounds, row count, trailing-rows-after-watermark).
- `tests/test_parquet_validation_state.py` — unit tests for the watermark store + guard.
- `tests/test_parquet_tail.py` — unit tests for the pyarrow metadata/tail reader.

### Relevant Documentation — READ BEFORE IMPLEMENTING

- pyarrow Parquet API: `pyarrow.parquet.ParquetFile` — `.metadata` (num_rows, num_row_groups), `.metadata.row_group(i).column(j).statistics` (min/max), `.read_row_group(i)`. https://arrow.apache.org/docs/python/parquet.html — Why: read tail row-groups + column stats without materializing the whole file.
- Linear GIL-15 — full context, occurrence, and the original recommendation this plan implements.

### Patterns to Follow

**Naming Conventions**: module-private helpers prefixed `_`; constants UPPER_SNAKE (`VALIDATOR_VERSION`, `PRICE_BOUNDS`); result dicts with string keys matching the existing JSON report shape.
**Error Handling**: read helpers return `None`/empty on missing/corrupt file (mirror `_safe_read`/`_safe_read_parquet`); never raise from a read — the caller decides severity. Sidecar read failure → treat as "no watermark" → full validation (fail-safe toward MORE validation).
**Logging Pattern**: human-readable progress to `stderr` via `print(..., file=sys.stderr)` (e.g. `[check] ...`); machine output is the JSON report on stdout. Production code stays silent otherwise.
**Atomic writes**: stage to `.tmp`, then `os.replace` (mirror `write_atomic` `:295`). Apply the same to the sidecar JSON.

### Reference Implementation Sketches (guidance, not prescriptive)

**Sidecar schema** (`.validation_state.json`):
```json
{
  "MNQ_1m.parquet": {
    "validator_version": 1,
    "validated_through": "2026-06-08T23:31:00-04:00",
    "validated_rows": 861353,
    "first_bar": "2024-01-01T18:00:00-05:00"
  },
  "MES_1m.parquet": { "...": "..." }
}
```

**`parquet_tail.read_after`** (Contract B — read only trailing row-groups):
```python
import pyarrow.parquet as pq
import pandas as pd

def read_after(path, watermark):  # watermark: tz-aware pd.Timestamp
    pf = pq.ParquetFile(path)
    md = pf.metadata
    ts_col = 0  # the datetime index column; resolve by schema name if not positional
    keep = []
    for i in range(md.num_row_groups):
        stats = md.row_group(i).column(ts_col).statistics
        # stats.max is the row-group's max timestamp; include groups that may hold tail rows
        if stats is None or _to_ts(stats.max) > watermark:
            keep.append(i)
    if not keep:
        return pd.DataFrame()
    tbl = pf.read_row_groups(keep)
    df = tbl.to_pandas()
    # mirror fetch_range tz handling (:183-187), then filter
    df = df[df.index > watermark].sort_index()
    return df[~df.index.duplicated(keep="last")]
```
Fallback when `statistics is None` for the ts column: read all groups (still correct, just not minimal) or use `pd.read_parquet(path, columns=[])` to locate the boundary. Correctness is asserted by equality to the naive `df[df.index>wm]` in tests — the row-group minimization is best-effort.

**`needs_full_validation`** (Contract A — pure, no I/O):
```python
def needs_full_validation(entry, *, first_bar, row_count):
    if entry is None:
        return True, "no-watermark"
    if entry.get("validator_version") != VALIDATOR_VERSION:
        return True, "version-bump"
    if first_bar is None or entry.get("first_bar") != first_bar:
        return True, "body-rewritten"
    if row_count < entry.get("validated_rows", 0):
        return True, "truncation"
    return False, ""
```

**Incremental decision inside `check_1m_parquet`** (skeleton):
```python
first, last = index_bounds(main_path)         # cheap, no full load
n = row_count(main_path)
full, reason = needs_full_validation(entry, first_bar=_iso(first), row_count=n)
if force_full_validate:
    full, reason = True, "forced-full"
if full:
    df = pd.read_parquet(main_path)           # existing full path
    ... Close<=0 scan ...; backup_main(); set_watermark(...)
    result["validation_scope"], result["full_reason"] = "full", reason
else:
    # belt-and-suspenders positional guard
    if bar_at_position(main_path, entry["validated_rows"] - 1) != _ts(entry["validated_through"]):
        ... fall back to full path, reason="body-rewritten" ...
    tail = read_after(main_path, _ts(entry["validated_through"]))
    v = validate_session_df(tail, price_lo, price_hi) if not tail.empty else {"severity": "ok"}
    seam_ok = tail.empty or _seam_ok(_ts(entry["validated_through"]), tail.index[0])
    ... combine severity + seam; on clean advance watermark to `last`, n ...
    result["validation_scope"] = "incremental"; result["validated_through"] = _iso(last)
```

**`_seam_ok`** (reuse `_is_expected_closed`):
```python
def _seam_ok(prev_last, new_first):
    if new_first <= prev_last:          # overlap / duplicate
        return False
    if (new_first - prev_last) <= pd.Timedelta("90s"):
        return True                     # contiguous
    return _is_expected_closed(prev_last, new_first)   # weekend/maintenance OK
```

---

## PARALLEL EXECUTION STRATEGY

### Dependency Graph

```
┌─────────────────────────────────────────────────────────────┐
│ WAVE 1: Foundation utilities (Parallel — separate new files) │
├─────────────────────────────────────────────────────────────┤
│ Task 1.1: parquet_validation_state.py │ Task 1.2: parquet_tail.py │
│ Agent: backend-utility                │ Agent: backend-utility    │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│ WAVE 2: Core refactor (Sequential — same file)               │
├─────────────────────────────────────────────────────────────┤
│ Task 2.1: incremental check_1m_parquet — Deps: 1.1, 1.2     │
│ Task 2.2: CLI flags + report fields + repair reset — Deps: 2.1│
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│ WAVE 3: 1s seam + docs (Parallel — separate files)           │
├─────────────────────────────────────────────────────────────┤
│ Task 3.1: 1s merge seam check │ Task 3.2: SKILL.md doc update │
│ Agent: backend-utility        │ Agent: docs                  │
└─────────────────────────────────────────────────────────────┘
```

### Parallelization Summary

**Wave 1 — Fully Parallel**: Tasks 1.1, 1.2 — two new independent modules, no shared files.
**Wave 2 — Sequential**: 2.1 then 2.2 — both edit `check_session_parquets.py` (would conflict if parallel).
**Wave 3 — Parallel after Wave 2**: 3.1 (`parquet_maintenance.py`) and 3.2 (`SKILL.md`) — different files.

Parallelizable tasks: 4 of 6 (~67%).

### Interface Contracts

**Contract A (Task 1.1 → 2.x)**: `parquet_validation_state` exposes:
- `VALIDATOR_VERSION: int`
- `load_state(state_path: Path) -> dict` — returns `{}` if missing/corrupt.
- `get_watermark(state: dict, parquet_name: str) -> dict | None` — entry or `None`.
- `set_watermark(state_path, parquet_name, *, validated_through: str, validated_rows: int, first_bar: str) -> None` — atomic write.
- `needs_full_validation(entry: dict | None, *, first_bar: str | None, row_count: int) -> tuple[bool, str]` — `(True, reason)` when no entry / version mismatch / first_bar changed / row_count < validated_rows; `(False, "")` when a pure-append incremental check is safe. `reason` is one of `"no-watermark" | "version-bump" | "body-rewritten" | "truncation"`.

**Contract B (Task 1.2 → 2.x, 3.1)**: `parquet_tail` exposes:
- `index_bounds(path: Path) -> tuple[pd.Timestamp | None, pd.Timestamp | None]` — (first, last) via metadata, no full load. `(None, None)` if missing/empty.
- `row_count(path: Path) -> int` — `ParquetFile.metadata.num_rows`, 0 if missing.
- `read_after(path: Path, watermark: pd.Timestamp) -> pd.DataFrame` — only trailing row-groups whose max-ts > watermark, then filtered to `index > watermark`. Empty frame if none.
- `bar_at_position(path: Path, pos: int) -> pd.Timestamp | None` — timestamp at integer row position `pos` (for the body-integrity guard's `validated_rows-1` check), read from the row-group containing `pos` only.

**Mock for parallel work**: Wave 2 can be authored against the Contract A/B signatures above before Wave 1 lands; the execute skill should still run Wave 1 first (the modules are small).

### Synchronization Checkpoints

**After Wave 1**: `uv run pytest tests/test_parquet_validation_state.py tests/test_parquet_tail.py -m "not integration"`
**After Wave 2**: `uv run pytest tests/test_check_session_parquets.py -m "not integration"`
**After Wave 3**: `uv run pytest tests/test_parquet_maintenance.py tests/test_check_session_parquets.py -m "not integration"`

---

## IMPLEMENTATION PLAN

### Phase 1: Foundation utilities (no external services)

Two self-contained modules with full unit coverage. No IB, no live connections.

#### Task 1.1: CREATE `scripts/parquet_validation_state.py`
**Purpose**: Persist + interpret the validation watermark; decide append-vs-rewrite.
**Steps**: implement Contract A. Sidecar path resolved by caller (passed in) — default `paths.general_live_dir() / ".validation_state.json"`. Atomic write (`.tmp` + `os.replace`). `load_state` returns `{}` on any read/parse error. `needs_full_validation` is pure (no I/O) — caller supplies `first_bar`/`row_count` from Task 1.2.
**Validation**: `uv run pytest tests/test_parquet_validation_state.py -m "not integration"`

#### Task 1.2: CREATE `scripts/parquet_tail.py`
**Purpose**: Cheap pyarrow metadata reads + tail materialization.
**Steps**: implement Contract B using `pyarrow.parquet.ParquetFile`. For `read_after`, iterate row groups from the end, reading the timestamp column statistics (`row_group(i).column(<ts_col>).statistics.max`) to find the first row-group whose max ≤ watermark; read only groups after it, concat, set index, filter `> watermark`, sort, drop dup index (keep last). Mirror the tz handling in `fetch_range` (`:183-187`). Fall back to `pd.read_parquet(path, columns=[])` index-only read (precedent `parquet_maintenance.py:91`) if statistics are absent.
**Validation**: `uv run pytest tests/test_parquet_tail.py -m "not integration"`

### Phase 2: Core incremental refactor

#### Task 2.1: REFACTOR `check_1m_parquet` → incremental
**Purpose**: Replace the unconditional full read+scan with watermark-gated incremental validation.
**Steps**:
1. Resolve `state_path = DATA_DIR / ".validation_state.json"`; `state = load_state(state_path)`; `entry = get_watermark(state, main_1m_name)`.
2. Cheap probe: `first, last = index_bounds(main_path)`; `n = row_count(main_path)`. If file missing/unreadable → existing corrupt/repair path (unchanged).
3. `full, reason = needs_full_validation(entry, first_bar=first.isoformat() if first else None, row_count=n)`.
   - If `--full-validate` flag set → force `full=True, reason="forced-full"`.
4. **Full path**: existing behavior (read whole file, `Close<=0` scan, `.bak`), then `set_watermark(..., validated_through=last, validated_rows=n, first_bar=first)`. Set `result["validation_scope"]="full"`, `result["full_reason"]=reason`.
5. **Incremental path**:
   a. Body-integrity guard (belt-and-suspenders even though `needs_full_validation` already checked first_bar/count): confirm `bar_at_position(main_path, entry["validated_rows"]-1) == entry["validated_through"]`. If mismatch → fall back to full (reason `"body-rewritten"`).
   b. `tail = read_after(main_path, pd.Timestamp(entry["validated_through"]))`. If empty → nothing new; scope `incremental`, severity `ok`, watermark unchanged (last==validated_through).
   c. Validate the tail with `validate_session_df(tail, price_lo, price_hi)` (gives price/OHLC/gap severity) AND the `Close<=0` check for parity with full path.
   d. **Seam check**: `_is_expected_closed(prev_last, first_new)` must be True OR the gap between them ≤ 90s (contiguous). Also assert `first_new > prev_last` (no overlap/duplicate). On violation → severity ≥ `minor` with `seam_issue` detail.
   e. On clean (`severity in ("ok","minor")`) → advance watermark to new `last`, `validated_rows=n`; refresh `.bak` per existing policy. Set `result["validation_scope"]="incremental"`, `result["validated_through"]=new last`, `result["tail_rows"]=len(tail)`.
6. `--dry-run`: never write sidecar or `.bak`; still report the scope it WOULD take.
**Validation**: `uv run pytest tests/test_check_session_parquets.py -k "incremental or watermark or seam" -m "not integration"`

#### Task 2.2: ADD CLI flags + report fields + repair-path watermark reset
**Purpose**: Expose `--full-validate`/`--since`, surface scope in the report, keep watermark honest after repair.
**Dependencies**: 2.1
**Steps**:
1. `main()`: add `--full-validate` (store_true) and optional `--since <iso>` (forces incremental re-scan from a given ts; primarily a debugging/recovery aid). Thread into `check_1m_parquet`.
2. Ensure `validation_scope` + `validated_through` appear in every `instruments_1m` entry (both full and incremental).
3. Repair path (`:498-537`): after a successful repair (`write_atomic(filled_df,...)`), call `set_watermark(..., validated_through=<new last>, validated_rows=<new count>, first_bar=<first>)` so the next run trusts the repaired body from a correct baseline. On repair failure, leave/clear the entry so the next run does a full scan.
**Validation**: `uv run pytest tests/test_check_session_parquets.py -m "not integration"`

### Phase 3: 1s seam + documentation

#### Task 3.1: ADD seam check to `merge_session_1s_parquets`
**Purpose**: Validate the concatenation point when session 1s data is appended onto main 1s.
**Dependencies**: 1.2 (uses `index_bounds`); logically after 2.x.
**Steps**: in `data/parquet_maintenance.py:269-360`, around the `pd.concat([existing, session_df])` (`:349`): before concat, compare `existing.index[-1]` to `session_df.index[0]`. If `session_df.index[0] <= existing.index[-1]` (overlap/dup) → keep current dedup behavior but emit a stderr `[merge_session_1s] WARN: seam overlap ...`. If the gap is unexpected (reuse `_is_expected_closed` — import from `scripts.check_session_parquets` or factor the helper to a shared spot; prefer import to avoid duplication) → emit `[merge_session_1s] WARN: unexpected seam gap ...`. Behavior (the merge itself) is unchanged; this only surfaces seam anomalies. Keep it silent on clean seams (production-silent rule).
**Validation**: `uv run pytest tests/test_parquet_maintenance.py -k "seam or merge" -m "not integration"`

#### Task 3.2: UPDATE `.claude/skills/parquet-check/SKILL.md`
**Purpose**: Keep the skill contract truthful about the new sidecar write + report fields.
**Dependencies**: none (docs); after 2.x conceptually.
**Steps**:
1. HARD RULE "Allowed writes": add `.validation_state.json` (the watermark sidecar under `general/live/`) to the allowed-write list, with a one-line note that the script (not the agent) writes it.
2. Step 2 (Parse & assess): document `validation_scope: "incremental" | "full"` and `validated_through` on each `instruments_1m` entry; note `full_reason` when scope is full.
3. Step 3 (LLM severity): note low-bar-count/stale-last-bar heuristics now apply to the tail delta; a `full` scope run is the periodic safety net.
4. Document the new `--full-validate` (and `--since`) flags in Step 1's flag table.
**Validation**: doc-only; verified by inspection (no code path). Confirm the file still renders and the allowed-writes list includes the sidecar.

---

## STEP-BY-STEP TASKS

### WAVE 1: Foundation

#### Task 1.1: CREATE scripts/parquet_validation_state.py
- **WAVE**: 1
- **AGENT_ROLE**: backend-utility
- **DEPENDS_ON**: []
- **BLOCKS**: 2.1, 2.2
- **PROVIDES**: Contract A (watermark store + `needs_full_validation`)
- **IMPLEMENT**: load/save sidecar (atomic), `VALIDATOR_VERSION`, pure `needs_full_validation`
- **PATTERN**: `scripts/check_session_parquets.py:295` (`write_atomic`), `:80` (`_safe_read` None-on-error)
- **VALIDATE**: `uv run pytest tests/test_parquet_validation_state.py -m "not integration"`

#### Task 1.2: CREATE scripts/parquet_tail.py
- **WAVE**: 1
- **AGENT_ROLE**: backend-utility
- **DEPENDS_ON**: []
- **BLOCKS**: 2.1, 3.1
- **PROVIDES**: Contract B (pyarrow metadata + tail reader)
- **IMPLEMENT**: `index_bounds`, `row_count`, `read_after`, `bar_at_position` via `pyarrow.parquet.ParquetFile`
- **PATTERN**: `data/parquet_maintenance.py:91` (index-only read), `scripts/check_session_parquets.py:183-189` (tz handling)
- **VALIDATE**: `uv run pytest tests/test_parquet_tail.py -m "not integration"`

**Wave 1 Checkpoint**: `uv run pytest tests/test_parquet_validation_state.py tests/test_parquet_tail.py -m "not integration"`

---

### WAVE 2: Core (After Wave 1)

#### Task 2.1: REFACTOR check_1m_parquet (incremental)
- **WAVE**: 2
- **AGENT_ROLE**: backend-core
- **DEPENDS_ON**: 1.1, 1.2
- **BLOCKS**: 2.2
- **USES_FROM_WAVE_1**: 1.1 Contract A; 1.2 Contract B
- **IMPLEMENT**: watermark-gated full/incremental decision, body-integrity guard, tail validate, seam check, watermark advance; preserve corrupt/repair path
- **PATTERN**: reuse `validate_session_df` (`:192`), `_is_expected_closed` (`:53`); mirror healthy-path return shape (`:484-496`)
- **VALIDATE**: `uv run pytest tests/test_check_session_parquets.py -k "incremental or watermark or seam or full_fallback" -m "not integration"`

#### Task 2.2: ADD CLI flags + report fields + repair reset
- **WAVE**: 2
- **AGENT_ROLE**: backend-core
- **DEPENDS_ON**: 2.1
- **PROVIDES**: `--full-validate`/`--since`, `validation_scope`/`validated_through` in report, post-repair watermark
- **IMPLEMENT**: argparse flags in `main()` (`:625`), thread to `check_1m_parquet`; set watermark after repair (`:530`)
- **VALIDATE**: `uv run pytest tests/test_check_session_parquets.py -m "not integration"`

**Wave 2 Checkpoint**: `uv run pytest tests/test_check_session_parquets.py -m "not integration"`

---

### WAVE 3: Integration (Parallel after Wave 2)

#### Task 3.1: ADD 1s merge seam check
- **WAVE**: 3
- **AGENT_ROLE**: backend-utility
- **DEPENDS_ON**: 1.2, 2.1
- **PROVIDES**: seam-anomaly surfacing on 1s concat
- **IMPLEMENT**: pre-concat seam comparison in `merge_session_1s_parquets` (`data/parquet_maintenance.py:349`); stderr WARN on overlap/unexpected gap; behavior unchanged
- **PATTERN**: import/reuse `_is_expected_closed`
- **VALIDATE**: `uv run pytest tests/test_parquet_maintenance.py -k "seam or merge" -m "not integration"`

#### Task 3.2: UPDATE parquet-check SKILL.md
- **WAVE**: 3
- **AGENT_ROLE**: docs
- **DEPENDS_ON**: 2.2
- **PROVIDES**: truthful skill contract (allowed-writes sidecar, report fields, flags)
- **IMPLEMENT**: edit HARD RULE allowed-writes, Step 2, Step 3, Step 1 flag table
- **VALIDATE**: inspection — allowed-writes list contains `.validation_state.json`; no code path introduced

**Final Checkpoint**: `uv run pytest tests/ -m "not integration"` (full suite, side-effecting tests deselected per policy below)

---

## TESTING STRATEGY

**⚠️ ALL tests that can be automated MUST be automated.** All tests here are backend `pytest` against synthetic parquet fixtures in `tmp_path` — no IB, no network, no live process. The IB-touching paths are already mocked in `test_check_session_parquets.py` (`_make_ib_mock`).

| What you're testing | Tool |
|---|---|
| Watermark store + guard | `pytest` (`tests/test_parquet_validation_state.py`) |
| pyarrow metadata/tail reader | `pytest` (`tests/test_parquet_tail.py`) |
| Incremental `check_1m_parquet` | `pytest` (`tests/test_check_session_parquets.py`) |
| 1s merge seam | `pytest` (`tests/test_parquet_maintenance.py`) |

### Unit Tests
**Status**: ✅ Automated | **Tool**: pytest | **Location**: `tests/` | **Run**: `uv run pytest tests/ -m "not integration"`

**Task 1.1 — `tests/test_parquet_validation_state.py`** (✅ pytest):
- `test_load_state_missing_returns_empty` — no file → `{}`.
- `test_load_state_corrupt_returns_empty` — garbage JSON → `{}` (fail-safe).
- `test_set_then_load_roundtrip` — write entry, read back equal; `.tmp` not left behind.
- `test_set_watermark_atomic_no_tmp` — no `.validation_state.json.tmp` remains.
- `test_needs_full_no_entry` — `entry=None` → `(True, "no-watermark")`.
- `test_needs_full_version_bump` — entry with `validator_version` < current → `(True, "version-bump")`.
- `test_needs_full_first_bar_changed` — `first_bar` differs → `(True, "body-rewritten")`.
- `test_needs_full_truncation` — `row_count < validated_rows` → `(True, "truncation")`.
- `test_needs_full_pure_append_false` — matching first_bar + `row_count >= validated_rows` → `(False, "")`.

**Task 1.2 — `tests/test_parquet_tail.py`** (✅ pytest):
- `test_index_bounds_multi_rowgroup` — write a parquet with ≥3 row-groups (small `row_group_size`), assert (first,last) correct.
- `test_index_bounds_missing_file` — `(None, None)`.
- `test_row_count_matches` — equals `len(df)`.
- `test_read_after_returns_only_tail` — watermark mid-frame → only `index > watermark` rows.
- `test_read_after_watermark_at_or_past_last` — empty frame.
- `test_read_after_reads_minimal_rowgroups` — watermark in last group → result equals naive `df[df.index>wm]` (correctness; row-group minimization is an implementation detail asserted via equality, not internal calls).
- `test_bar_at_position_correct` — `bar_at_position(path, k)` == `df.index[k]`.
- `test_single_rowgroup_file` — helpers work when num_row_groups == 1.
- `test_tz_preserved` — returned tail index is tz-aware America/New_York.

**Task 2.1 / 2.2 — extend `tests/test_check_session_parquets.py`** (✅ pytest), new class `TestCheck1mIncremental`:
- `test_first_run_no_watermark_full_then_seeds` — no sidecar → `validation_scope=="full"`, `full_reason=="no-watermark"`, sidecar written with last bar.
- `test_second_run_clean_tail_incremental` — append clean tail, re-run → `validation_scope=="incremental"`, `tail_rows>0`, `severity=="ok"`, watermark advanced.
- `test_incremental_empty_tail_ok` — no new bars since watermark → `incremental`, `ok`, watermark unchanged.
- `test_incremental_bad_price_in_tail_surfaced` — inject `Close<=0`/out-of-bounds in the tail → severity ≥ `minor` (NOT silently passed — the core safety assertion).
- `test_seam_overlap_flagged` — new first ts ≤ watermark (duplicate) → `seam_issue` set, severity ≥ `minor`.
- `test_seam_unexpected_gap_flagged` — large weekday hole between watermark and tail → flagged.
- `test_seam_weekend_gap_ok` — Fri-close→Sun-open gap → `_is_expected_closed` True → `ok`.
- `test_rewritten_body_falls_back_to_full` — change first bar / shrink rows under a stale watermark → `validation_scope=="full"`, `full_reason in ("body-rewritten","truncation")`.
- `test_full_validate_flag_forces_full` — valid watermark + `--full-validate` → `full`, `full_reason=="forced-full"`.
- `test_dry_run_no_sidecar_write` — `--dry-run` → sidecar unchanged, scope reported.
- `test_repair_sets_watermark` — corrupt main + repair → after repair, sidecar entry present at repaired last bar (uses `_make_ib_mock`).
- `test_report_has_scope_fields` — every `instruments_1m` entry has `validation_scope` + `validated_through`.

**Task 3.1 — extend `tests/test_parquet_maintenance.py`** (✅ pytest):
- `test_merge_seam_contiguous_silent` — contiguous session after main → no WARN, rows merged.
- `test_merge_seam_overlap_warns_and_dedups` — overlapping session start → WARN emitted (capture stderr), no duplicate index in result.
- `test_merge_seam_unexpected_gap_warns` — weekday hole at seam → WARN emitted.

### Integration Tests
**Status**: ✅ Automated | **Tool**: pytest | **Location**: `tests/test_check_session_parquets.py` | **Run**: `uv run pytest tests/test_check_session_parquets.py -m "not integration"`
End-to-end of `check_1m_parquet` across two sequential runs on the same `tmp_path` parquet (full → append → incremental), asserting sidecar evolution and report scope. Covered by `TestCheck1mIncremental` above.

### End-to-End Tests
Not applicable (no UI / no service). The two-run sequence in the integration test is the E2E equivalent for this CLI utility.

### Manual Tests (only if automation is physically impossible)
None. Every path is automatable against synthetic `tmp_path` parquets. (The SKILL.md doc edit introduces no code path; validated by inspection.)

### Edge Cases
- **Empty/zero-row main parquet**: `index_bounds` → `(None,None)` → full path; no crash. — ✅ `test_index_bounds_missing_file` + a zero-row variant in `TestCheck1mIncremental`.
- **Single row-group history**: tail reader works. — ✅ `test_single_rowgroup_file`.
- **Watermark exactly at last bar**: empty tail, `ok`. — ✅ `test_incremental_empty_tail_ok`.
- **Interior backfill (count grows, first_bar same)**: guard's `bar_at_position(validated_rows-1)` mismatches → full fallback. — ✅ covered by `test_rewritten_body_falls_back_to_full` variant where an interior row is inserted.
- **Missing row-group statistics**: fall back to index-only read. — ✅ asserted via equality in `test_read_after_reads_minimal_rowgroups` (fixture with stats disabled).

### Test Automation Summary

| | Count | % |
|---|---|---|
| ✅ Backend (pytest) — state | 9 | |
| ✅ Backend (pytest) — tail reader | 9 | |
| ✅ Backend (pytest) — incremental check | 12 | |
| ✅ Backend (pytest) — 1s seam | 3 | |
| ⚠️ Manual | 0 | |
| **Total** | 33 | 100% |

**Goal**: 100% path coverage. All paths automated; SKILL.md edit is doc-only (no code path).

**Execution agent**: CREATE all automated test files as implementation tasks. RUN after each wave checkpoint.

---

## VALIDATION COMMANDS

### Side-effecting test policy (full-suite runs)

- **Run side-effecting tests during validation?** ☑ No (default) ☐ Yes
- **Deselect command (default-skip):** `uv run pytest tests/ -m "not integration"`
  - The repo's default `addopts` already includes `-m 'not integration'`, which deselects live-IB/network tests. This feature adds NO live-connection and NO process-lifecycle tests (all use mocked IB + `tmp_path`), so the default deselect is sufficient. If the suite contains orchestrator/process-lifecycle tests that start/kill processes, also pass `--ignore=tests/test_orchestrator_main.py` (and any sibling lifecycle test) — a live orchestrator/IB may be running on this machine; do NOT run those during validation.
- **If Yes — exact paths/markers + safe command:** N/A — this feature introduces none. Do not opt in.

### Level 0: External Service Validation
Not applicable — no external services. `pyarrow` 23.0.1 already installed (verified).

### Level 1: Syntax & Style
```bash
uv run python -c "import scripts.parquet_validation_state, scripts.parquet_tail, scripts.check_session_parquets, data.parquet_maintenance"
```

### Level 2: Unit Tests
```bash
uv run pytest tests/test_parquet_validation_state.py tests/test_parquet_tail.py -m "not integration"
uv run pytest tests/test_check_session_parquets.py tests/test_parquet_maintenance.py -m "not integration"
```

### Level 3: Integration Tests
```bash
uv run pytest tests/ -m "not integration"
```
(Baseline: run this BEFORE implementation and record pass/fail/skip counts to distinguish pre-existing failures from regressions — per project rule. PROGRESS history notes ~4 pre-existing failures historically; confirm the current baseline.)

### Level 4: E2E / Manual Validation
Optional smoke (only when NO live orchestrator/IB session is active and only after a real session-end has produced data):
```bash
uv run python scripts/check_session_parquets.py --mode orchestrator-start --dry-run
```
Assert the JSON report's `instruments_1m` entries carry `validation_scope` and `validated_through`. `--dry-run` performs no writes/IB calls. Deferred/optional — not required for acceptance.

---

## ACCEPTANCE CRITERIA

### Functional
- [ ] `scripts/parquet_validation_state.py` and `scripts/parquet_tail.py` exist and implement Contracts A and B.
- [ ] When a valid watermark exists, `check_1m_parquet` validates only the tail + seam and reports `validation_scope == "incremental"`.
- [ ] When no/invalid watermark exists, it runs full validation, reports `validation_scope == "full"` with a `full_reason`, and seeds the watermark.
- [ ] `read_after` returns exactly `df[df.index > watermark]` (asserted by equality), reading only trailing row-groups.
- [ ] `--full-validate` forces a full scan (`full_reason == "forced-full"`); `--since <iso>` re-scans from the given timestamp.

### Error Handling / Correctness Guard
- [ ] Body-integrity guard forces full re-validation on body rewrite (first_bar moved), truncation (row_count dropped), and interior insert (positional bar mismatch) — never trusts a changed body.
- [ ] Bad data injected into the tail (`Close<=0` / out-of-bounds / OHLC) is surfaced at severity ≥ minor — no correctness loss vs. the full scan.
- [ ] Sidecar read/parse failure is treated as "no watermark" → full validation (fail-safe toward more validation, never less).
- [ ] `--dry-run` performs no sidecar or `.bak` writes while still reporting the scope it would take.

### Integration / E2E
- [ ] Two sequential runs on the same parquet (full → append clean tail → incremental) evolve the sidecar correctly and advance the watermark.
- [ ] Repair path resets/advances the watermark to the repaired body's last bar.
- [ ] Seam anomalies (overlap/duplicate, unexpected weekday gap) are flagged; expected weekend/maintenance gaps pass.
- [ ] 1s `merge_session_1s_parquets` emits a stderr WARN on seam overlap/unexpected gap without changing merge behavior.

### Validation
- [ ] All 33 automated pytest tests pass — verified by: `uv run pytest tests/test_parquet_validation_state.py tests/test_parquet_tail.py tests/test_check_session_parquets.py tests/test_parquet_maintenance.py -m "not integration"`
- [ ] Full suite shows no NEW failures vs. recorded baseline — verified by: `uv run pytest tests/ -m "not integration"`
- [ ] Modules import cleanly — verified by: `uv run python -c "import scripts.parquet_validation_state, scripts.parquet_tail"`
- [ ] Report exposes `validation_scope` + `validated_through` on every `instruments_1m` entry (no removed fields).

### Non-Functional
- [ ] Production-silent: stderr `[check]` progress only, JSON report on stdout; no print in success paths.
- [ ] Sidecar + parquet writes are atomic (`.tmp` + `os.replace`); no `.tmp` left behind.
- [ ] SKILL.md updated: allowed-writes includes `.validation_state.json`; Steps 1/2/3 document the flags + new report fields.
- [ ] Changes UNSTAGED — not committed.

### Out of Scope
- Reducing `.bak` copy frequency (secondary GIL-15 optimization) — `.bak` policy left as-is.
- Auto-forcing a periodic full scan by watermark age — noted as future, not implemented.
- Any change to merge/promotion/repair semantics, exit codes, or the session 1s validation flow.
- The 1s main-parquet body validation (already incremental via session files) — only the seam WARN is added.

---

## COMPLETION CHECKLIST

- [ ] All tasks completed in wave order (1.1/1.2 → 2.1 → 2.2 → 3.1/3.2)
- [ ] Each task validation passed
- [ ] All validation levels executed (1–3; Level 4 optional)
- [ ] Baseline recorded BEFORE changes; compared AFTER
- [ ] All 33 automated tests created and passing
- [ ] No new linting/import errors
- [ ] All acceptance criteria met
- [ ] Code reviewed for quality
- [ ] **⚠️ Debug logs added during execution REMOVED (keep pre-existing)**
- [ ] **⚠️ CRITICAL: Changes UNSTAGED — NOT committed**

---

## NOTES

**Why a sidecar JSON, not a parquet metadata field**: keeps the watermark out of the data file (no rewrite of the parquet to update it), human-inspectable, and trivially deletable to force a full re-scan. It lives next to `global.json` under `paths.general_live_dir()`.

**Append-only assumption + the guard**: normal writers (`gap_fill_to_now`, `merge_session_1s_parquets`, the repair resample) only append after the last bar, so the watermark holds. The body-integrity guard (`first_bar` unchanged AND row at `validated_rows-1 == validated_through`) catches the three ways the assumption breaks — full rewrite (first_bar moves), truncation (count drops), and interior insert (positional bar mismatches) — and forces a full re-scan. This is the load-bearing safety mechanism; its tests (`test_rewritten_body_falls_back_to_full` and the interior-insert variant) are non-negotiable.

**`validator_version` bump discipline**: increment `VALIDATOR_VERSION` whenever the validation rules in `validate_session_df` or the tail/seam logic change semantics, so the first run after such a change re-validates the whole body once. Document this next to the constant.

**Periodic full safety net**: `--full-validate` is the manual lever; a future enhancement could auto-force a full scan when the watermark is older than N days. Out of scope here but noted so the sidecar carries enough info (`first_bar`, timestamps) to add it later without a schema change.

**`.bak` policy left mostly as-is**: this plan keeps the existing `.bak` refresh on healthy/advanced runs (it is the repair source via `_find_1m_backup`). Reducing `.bak` copy frequency is explicitly out of scope (secondary optimization in GIL-15) to keep the change focused and the repair path untouched.

**Scope discipline**: the 1m path is the refactor; the 1s path gets only an additive seam WARN (no behavior change). No change to merge/promotion/repair semantics, exit codes, or the session 1s validation flow.
