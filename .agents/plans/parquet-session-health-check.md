# Feature: Parquet Session Health Check Skill

**⚠️ EXECUTION RULES — READ FIRST:**
- Implement ALL changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
- Only make code changes — no git operations

Validate documentation and codebase patterns before implementing. Match naming of existing utils, types, and models. Import from correct files.

---

## Feature Description

An ad-hoc skill (`/parquet-check`) that validates, repairs, and merges 1s session parquet
files at session end or orchestrator start. Runs fully autonomously once invoked — no
confirmation prompts.

**Scope:**
- Session files only: `MNQ_1s_session_*.parquet` and `MES_1s_session_*.parquet`
- Main parquets are read-only (assumed healthy; write only on successful merge)
- No restore-from-backup: backups are written but never read back automatically
- **Data fixes are automatic; code is never touched.** The skill writes only parquet
  data files (`.parquet`, `.parquet.bak`). If the LLM detects a systematic issue
  that would require a code change, it describes the proposed change as text in the
  terminal and stops — it does NOT use Edit/Write tools on any `.py` or other code
  files. Code changes require explicit user instruction in a separate request.

**Two modes:**
- `session-end`: session data should be complete; missing/critical files trigger full IB
  rebuild of today's session window (18:00 ET prev day → now); then merge → backup
- `orchestrator-start`: session may have just started; missing/critical files trigger IB
  gap-fill from `main[-1]` to now (no full rebuild); then merge → backup

**Severity classification:**
- `ok`: no unexpected gaps, no bad rows → merge as-is
- `minor`: unexpected gaps < 5 min OR < 1% bad rows → merge as-is (dedup handles tiny gaps)
- `major`: unexpected gaps 5–60 min OR 1–5% bad rows → targeted fill of gap windows, then merge
- `critical`: unexpected gaps > 60 min OR > 5% bad rows OR unreadable → rebuild (session-end)
  or full gap-fill (orchestrator-start), then merge

**After every successful merge:** overwrite `data/*.parquet.bak` for that instrument.

## User Story

As a session agent / user
I want to invoke `/parquet-check` after session end
So that session 1s data is validated, repaired if needed, merged into the main parquet,
and backed up — without manual intervention

## Feature Metadata

**Feature Type**: New Capability
**Complexity**: Medium
**Primary Systems Affected**: new `scripts/check_session_parquets.py`, new skill file
**Secondary Systems Affected**: `data/parquet_maintenance.py` (IB client ID registry)
**Dependencies**: `ib_insync`, `pandas`, `python-dotenv` (all existing)
**Breaking Changes**: None — additive only

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ BEFORE IMPLEMENTING

- `scripts/fill_mnq_1s_overnight_gaps.py` — IB fetch pattern: chunked `reqHistoricalData`
  at 1800s, pacing sleep on Error 162, tz handling; reuse verbatim
- `scripts/rebuild_mes_1s.py` — `_prev_trading_ts()` helper skips maintenance windows;
  reuse in session rebuild to avoid fetching dead windows
- `data/parquet_maintenance.py` — `merge_session_1s_parquets()`: IB gap-fill between
  main[-1] and session[0], then concat+dedup+atomic write; the new script reuses this
  function directly rather than reimplementing merge
- `data/parquet_maintenance.py` — `_safe_read_parquet()`, `_empty_df()`: import and reuse
- `scripts/validate_parquets.py` — validation checks (price range, OHLC, duplicates, gaps);
  the new script replicates the core logic as functions (not a script-level import)
- `.claude/skills/run-orchestrator/SKILL.md` — skill file format and frontmatter to match
- `.claude/skills/live-trading/SKILL.md` — skill file format with decision tables
- `tests/test_parquet_maintenance.py` — test patterns: `bar_dir` fixture, `_make_df()`,
  IB mock (`Mock()` with `.reqHistoricalData` return value)

### IB Client ID Registry (do NOT reuse existing IDs)

| clientId | Used by |
|---|---|
| 0–15 | strategy / orchestrator |
| 16 | `merge_session_1s_parquets` |
| 17 | **this script** (`check_session_parquets.py`) |
| 95–99 | ad-hoc rebuild/restore scripts |

### Price Bounds (generous — covers 2025–2026 price range)

| Instrument | Low | High |
|---|---|---|
| MNQ | 20000.0 | 35000.0 |
| MES | 5000.0 | 9000.0 |

### CME Globex Schedule (ET)

- Market open: Sunday 18:00
- Daily maintenance: 17:00–18:00 Mon–Thu (break)
- Weekend close: Friday 17:00 → Sunday 18:00
- RTH: 09:30–16:00; CME session: 18:00 prev day → 17:00

---

## PARALLEL EXECUTION STRATEGY

### Dependency Graph

```
┌──────────────────────────────────────────────────────────────────────────┐
│ WAVE 1 (parallel — no dependencies between tasks)                        │
├──────────────────────────────────────────────────────────────────────────┤
│ Task 0: FIX data/parquet_maintenance.py — chunked gap-fill              │
│ Task 1: CREATE scripts/check_session_parquets.py                        │
│ Task 2: CREATE .claude/skills/parquet-check/SKILL.md                    │
└──────────────────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────────────────┐
│ WAVE 2 (sequential — depends on Tasks 0 and 1)                           │
├──────────────────────────────────────────────────────────────────────────┤
│ Task 3: CREATE tests/test_check_session_parquets.py                     │
│         + UPDATE tests/test_parquet_maintenance.py                      │
└──────────────────────────────────────────────────────────────────────────┘
```

**Interface contract Wave 1 → Wave 2:**
- `check_session_parquets.py` exports no public API; tests import `validate_session_df`,
  `_is_expected_closed`, `_safe_read`, `fetch_range`, `get_session_start_for_end_mode`,
  `write_atomic`, `backup_main` as module-level functions for unit testing
- `merge_session_1s_parquets` in `data/parquet_maintenance.py` exports `_fetch_gap_chunked`
  as a module-level function for unit testing (no leading underscore needed given test access)
- JSON report schema: `{"mode": str, "dry_run": bool, "instruments": {"MNQ": {...}, "MES": {...}}, "exit_code": int}`
- Per-instrument result keys: `severity`, `action`, `merge_success`, `backup_written`,
  `validation`, `gap_fill_bars` (optional), `merged_rows` (optional), `reason` (optional)

---

## IMPLEMENTATION PLAN

---

### Task 0 — FIX `data/parquet_maintenance.py`: chunked gap-fill with pacing retry

**WAVE**: 1
**AGENT_ROLE**: Primary implementer
**DEPENDS_ON**: None

**Problem**: `merge_session_1s_parquets` currently issues a single `reqHistoricalData`
call with `durationStr=f"{gap_s} S"`. IB hard-limits 1-second bar requests to **1800 S
(30 minutes)** — any larger duration silently returns zero bars. For overnight gaps
(~59,400 S), the gap fill has always been a no-op, and the merge proceeds without the
missing bars. No error is surfaced.

**Fix**: Replace the single-request gap fill with a chunked backwards loop (1800 S chunks),
pacing retry on Error 162, and a hard abort + merge skip if retries are exhausted.

#### Constants to add at module level (top of `parquet_maintenance.py`)

```python
_GAP_FILL_CHUNK_S      = 1800   # IB hard limit: 1800 S per request for 1s bars
_GAP_FILL_PACING_SLEEP = 660    # 11 min — safe margin above IB's 10-min window
_GAP_FILL_MAX_RETRIES  = 3      # consecutive pacing failures before aborting
```

#### New helper: `_prev_trading_ts_gap(ts)`

Copy the `_prev_trading_ts` logic from `scripts/rebuild_mes_1s.py` verbatim (or extract
it to a shared utility — but for now copy it locally as `_prev_trading_ts_gap` to avoid
a cross-module dependency). This allows the chunked loop to skip the CME maintenance
break (17:00–18:00 ET) and weekend windows without making pointless IB requests.

#### New function: `_fetch_gap_chunked(ib, contract, gap_start, gap_end) -> tuple[pd.DataFrame, bool]`

Returns `(gap_df, success)`:
- `success = True` if all chunks were fetched (even if some returned 0 bars due to
  genuinely no data in that window)
- `success = False` if a pacing retry was exhausted (max retries hit)

```python
def _fetch_gap_chunked(ib, contract, gap_start, gap_end):
    import time as _time
    from ib_insync import util as _util

    all_bars = []
    chunk_end = gap_end
    consecutive_pacing = 0
    pacing_hit = False

    def _on_error(reqId, errorCode, errorString, contract):
        nonlocal pacing_hit
        if errorCode == 162 and "pacing" in errorString.lower():
            pacing_hit = True

    ib.errorEvent += _on_error
    try:
        while chunk_end > gap_start:
            adjusted = _prev_trading_ts_gap(chunk_end)
            if adjusted < chunk_end:
                chunk_end = adjusted
                continue

            chunk_start = max(gap_start, chunk_end - pd.Timedelta(seconds=_GAP_FILL_CHUNK_S))
            chunk_s = max(1, int((chunk_end - chunk_start).total_seconds()))

            pacing_hit = False
            bars = ib.reqHistoricalData(
                contract,
                endDateTime=chunk_end.tz_convert("UTC").strftime("%Y%m%d-%H:%M:%S"),
                durationStr=f"{chunk_s} S",
                barSizeSetting="1 secs",
                whatToShow="TRADES",
                useRTH=False,
                formatDate=2,
            )

            if not bars and pacing_hit:
                consecutive_pacing += 1
                if consecutive_pacing > _GAP_FILL_MAX_RETRIES:
                    print(
                        f"[merge_session_1s] gap fill: pacing retries exhausted "
                        f"({_GAP_FILL_MAX_RETRIES} consecutive) — aborting",
                        flush=True,
                    )
                    return pd.DataFrame(), False
                wait_min = _GAP_FILL_PACING_SLEEP // 60
                print(
                    f"[merge_session_1s] gap fill: pacing — sleeping {wait_min} min "
                    f"(retry {consecutive_pacing}/{_GAP_FILL_MAX_RETRIES}) ...",
                    flush=True,
                )
                _time.sleep(_GAP_FILL_PACING_SLEEP)
                pacing_hit = False
                bars = ib.reqHistoricalData(
                    contract,
                    endDateTime=chunk_end.tz_convert("UTC").strftime("%Y%m%d-%H:%M:%S"),
                    durationStr=f"{chunk_s} S",
                    barSizeSetting="1 secs",
                    whatToShow="TRADES",
                    useRTH=False,
                    formatDate=2,
                )
                if not bars:
                    chunk_end = chunk_start
                    continue
            else:
                consecutive_pacing = 0  # reset on any successful (even empty) chunk

            if bars:
                all_bars.extend(bars)
            chunk_end = chunk_start
    finally:
        ib.errorEvent -= _on_error

    if not all_bars:
        return pd.DataFrame(), True  # success=True: no bars is valid (market closed window)

    df = _util.df(all_bars).rename(columns={
        "date": "datetime", "open": "Open", "high": "High",
        "low": "Low", "close": "Close", "volume": "Volume",
    }).set_index("datetime")
    if df.index.tzinfo is None:
        df.index = df.index.tz_localize("America/New_York")
    else:
        df.index = df.index.tz_convert("America/New_York")
    df = df[["Open", "High", "Low", "Close", "Volume"]].sort_index()
    df = df[~df.index.duplicated(keep="last")]
    return df, True
```

#### Replace gap-fill block inside `merge_session_1s_parquets`

Find the block starting at `# IB gap fill: main[-1] → session[0]` (approx line 190–222).
Replace the entire inner `try` block with:

```python
if ib_ok and conid and not existing.empty:
    gap_start = existing.index[-1]
    gap_end   = session_df.index[0]
    gap_s     = max(0, int((gap_end - gap_start).total_seconds()) - 1)
    if gap_s > 1:
        contract = _IBContract(conId=int(conid), exchange="CME")
        gap_df, gap_ok = _fetch_gap_chunked(ib, contract, gap_start, gap_end)
        if not gap_ok:
            print(
                f"[merge_session_1s] {instrument}: WARNING — gap fill failed after "
                f"{_GAP_FILL_MAX_RETRIES} pacing retries. Gap "
                f"{gap_start.strftime('%m-%d %H:%M')} → "
                f"{gap_end.strftime('%m-%d %H:%M')} ({gap_s}s) not filled. "
                f"Skipping merge to avoid writing incomplete data.",
                flush=True,
            )
            continue  # skip to next instrument — do NOT write main parquet
        if not gap_df.empty:
            existing = pd.concat(
                [existing, gap_df[["Open", "High", "Low", "Close", "Volume"]]]
            ).sort_index()
            existing = existing[~existing.index.duplicated(keep="last")]
            print(f"[merge_session_1s] {instrument}: +{len(gap_df)} gap bars", flush=True)
```

Note the `continue` on gap-fill failure: this skips writing the main parquet and deleting
the session file, preserving both for the next attempt.

---

### Task 1 — CREATE `scripts/check_session_parquets.py`

**WAVE**: 1  
**AGENT_ROLE**: Primary implementer  
**DEPENDS_ON**: None

**Purpose**: Deterministic engine. Validates session files, classifies severity, takes
corrective action (targeted fill / rebuild / gap-fill), merges into main, backs up.
Outputs a JSON report on stdout; human-readable progress on stderr.

**Exit codes:**
- `0` — healthy, no action needed
- `1` — issues found and fixed successfully
- `2` — issues found; could not fully fix (manual attention needed)
- `3` — unexpected error

**CLI:**
```
uv run python scripts/check_session_parquets.py --mode session-end
uv run python scripts/check_session_parquets.py --mode orchestrator-start
uv run python scripts/check_session_parquets.py --mode session-end --dry-run
```

#### Module-level constants

```python
HOST      = os.environ.get("IB_HOST", "127.0.0.1")
PORT      = int(os.environ.get("IB_PORT", "4002"))
MNQ_CONID = int(os.environ.get("MNQ_CONID", "0"))
MES_CONID = int(os.environ.get("MES_CONID", "0"))

CHUNK_S          = 1800        # seconds per IB chunk
PACING_SLEEP_S   = 660         # 11 min pacing sleep on Error 162
IB_CLIENT_ID     = 17

PRICE_BOUNDS = {
    "MNQ": (20000.0, 35000.0),
    "MES": (5000.0,  9000.0),
}

SMALL_GAP_THRESHOLD    = pd.Timedelta("5min")
LARGE_GAP_THRESHOLD    = pd.Timedelta("60min")
BAD_ROW_MINOR_FRAC     = 0.01
BAD_ROW_CRITICAL_FRAC  = 0.05

DATA_DIR = Path(__file__).parent.parent / "data"

INSTRUMENTS = [
    ("MNQ", MNQ_CONID, "MNQ_1s.parquet", "MNQ_1s_session_*.parquet"),
    ("MES", MES_CONID, "MES_1s.parquet", "MES_1s_session_*.parquet"),
]
```

#### Functions to implement

**`validate_session_df(df, price_lo, price_hi, expected_session_start=None) -> dict`**

`expected_session_start`: the CME open for this session (18:00 ET previous evening), as a
tz-aware `pd.Timestamp`. Pass `None` in orchestrator-start mode or when not known.

Returns:
```python
{
    "severity": "ok" | "minor" | "major" | "critical",
    "rows": int,
    "first": str,         # ISO timestamp
    "last": str,          # ISO timestamp
    "bad_rows": int,
    "bad_row_frac": float,
    "unexpected_gaps": [{"start": str, "end": str, "duration_s": int}, ...],
    "max_gap_s": int,
    "late_start_hours": float,  # hours after expected_session_start that session data begins
                                # 0.0 if expected_session_start is None or df is empty
}
```

Classification logic:
- If df is None or empty → `severity="critical"`, `reason="empty or missing"`, `late_start_hours=0.0`
- Price bad rows: `Low < price_lo OR High > price_hi OR Close <= 0`
- OHLC bad rows: `H < L OR C > H OR C < L OR O > H OR O < L`
- `total_bad = len(bad_price) + len(bad_ohlc)` (union OK for conservative estimate)
- `bad_row_frac = total_bad / max(1, len(df))`
- For gaps: iterate diffs > 90s; call `_is_expected_closed()` to filter; collect unexpected
- `max_gap_s = max(g["duration_s"] for g in unexpected_gaps, default=0)`
- `late_start_hours`: if `expected_session_start` is not None and df is not empty:
  `late_start_hours = max(0.0, (df.index[0] - expected_session_start).total_seconds() / 3600)`
  else `late_start_hours = 0.0`
- Severity from data quality (before overnight check):
  - `critical` if `bad_row_frac >= BAD_ROW_CRITICAL_FRAC OR max_gap >= LARGE_GAP`
  - `major` if `bad_row_frac >= BAD_ROW_MINOR_FRAC OR SMALL_GAP <= max_gap < LARGE_GAP`
  - `minor` if `bad_rows > 0 OR any unexpected gaps < SMALL_GAP`
  - `ok` otherwise
- **Overnight coverage does NOT change severity here** — the caller (`process_instrument`)
  escalates based on `late_start_hours` and mode, because in orchestrator-start mode
  the merge gap-fill already covers overnight data automatically

**`_is_expected_closed(gap_start: pd.Timestamp, gap_end: pd.Timestamp) -> bool`**

Copy logic verbatim from `scripts/fill_mnq_1s_overnight_gaps.py::_is_expected_closed()`,
returning `True` for expected windows (return `bool`, not `str | None`).

**`_safe_read(path: Path) -> pd.DataFrame | None`**

- Returns `None` if path does not exist (distinct from empty/unreadable)
- Returns empty `pd.DataFrame()` if file exists but `pd.read_parquet()` raises
- Returns the DataFrame on success
- Never modifies the file

**`fetch_range(ib, contract, start_dt, end_dt) -> pd.DataFrame`**

Copy the chunked-fetch pattern from `scripts/fill_mnq_1s_overnight_gaps.py::fetch_gap()`:
- Loop backwards from `end_dt` in `CHUNK_S`-second chunks
- Each iteration: `chunk_start = max(start_dt, chunk_end - pd.Timedelta(seconds=CHUNK_S))`
- `actual_s = max(1, int((chunk_end - chunk_start).total_seconds()))`
- Break on empty response (IB has no more data for that window)
- Coerce tz to `America/New_York`, dedup, sort, return

Add pacing: wire `ib.errorEvent` to detect Error 162 (same pattern as `rebuild_mes_1s.py`);
on pacing hit: `print("[check] pacing — sleeping 11 min", file=sys.stderr)`, sleep
`PACING_SLEEP_S`, retry once per chunk.

Timezone normalization (from `fill_mnq_1s_overnight_gaps.py`):
```python
if df.index.tzinfo is None:
    df.index = df.index.tz_localize("America/New_York")
else:
    df.index = df.index.tz_convert("America/New_York")
```

**`get_session_start_for_end_mode() -> pd.Timestamp`**

Return the CME session open for the current trading day (18:00 ET previous evening).
- Now is after 18:00 ET → session started today 18:00
- Now is before 18:00 ET → session started yesterday 18:00
- Handle weekends: snap to the previous Friday 18:00 if now is Saturday/Sunday

**`write_atomic(df: pd.DataFrame, path: Path) -> None`**

Write `df` to `path.with_suffix(".parquet.tmp")` with `use_dictionary=False`, then
`os.replace(tmp, path)`.

**`backup_main(main_path: Path) -> None`**

`shutil.copy2(main_path, main_path.with_suffix(".parquet.bak"))`.
Print `[check] Backed up {name} -> {name}.bak` to stderr.

**`targeted_fill(ib, contract, session_path, unexpected_gaps) -> pd.DataFrame`**

For each gap in `unexpected_gaps`:
- `fetch_range(ib, contract, gap_start, gap_end)` — use the gap's start/end timestamps
- Concat fetched bars into the existing session DF, sort, dedup
Return the patched DataFrame (do NOT write to disk here — caller does `write_atomic`).

**`rebuild_session(ib, contract, session_start, session_end) -> pd.DataFrame`**

Fetch the full session window using `_prev_trading_ts()` (import from `rebuild_mes_1s.py`
or copy the function) to skip non-trading windows.
Return the fetched DataFrame.

**`gap_fill_to_now(ib, contract, main_path) -> pd.DataFrame`**

- Read `main_path` last timestamp via `_safe_read()`
- If empty: `gap_start = DATA_DIR / main_path.name` last bar or return empty DF
- `gap_end = pd.Timestamp.now(tz="America/New_York") - pd.Timedelta(seconds=2)`
- `fetch_range(ib, contract, gap_start, gap_end)`

**`process_instrument(inst, conid, main_path, session_glob, mode, dry_run, ib) -> dict`**

Orchestrates one instrument end-to-end. Returns the per-instrument result dict.

Decision tree:

```
session_files = sorted(DATA_DIR.glob(session_glob))
session_start = get_session_start_for_end_mode()  # CME open: 18:00 ET prev evening

IF no session_files:
    IF mode == "orchestrator-start":
        action = "gap_fill_created_session"
        fetch gap_fill_to_now() → write as session file
        → then fall through to merge
    ELSE (session-end):
        action = "skip", reason = "no session file"
        return (nothing to do)

FOR each session_file:
    df = _safe_read(session_file)

    # Pass expected_session_start only in session-end mode so late_start_hours is computed
    expected_start = session_start if mode == "session-end" else None
    v = validate_session_df(df, price_lo, price_hi, expected_session_start=expected_start)
    severity = v["severity"]

    # Overnight coverage escalation (session-end only)
    # If session data started > 2h after CME open, the session file is missing overnight bars.
    # The merge gap-fill can recover them from IB, but the most reliable fix is a full rebuild
    # that explicitly fetches 18:00→now with useRTH=False.
    IF mode == "session-end" AND v["late_start_hours"] > 2.0 AND severity in ("ok", "minor", "major"):
        severity = "critical"
        result["reason"] = f"overnight data missing: session started {v['late_start_hours']:.1f}h after CME open"

    IF severity in ("ok", "minor"):
        action = "merge"

    ELIF severity == "major":
        action = "targeted_fill_then_merge"
        patched = targeted_fill(ib, contract, session_file, v["unexpected_gaps"])
        write_atomic(patched, session_file)

    ELIF severity == "critical":
        IF mode == "session-end":
            action = "rebuild_then_merge"
            rebuilt = rebuild_session(ib, contract, session_start, now)
            IF rebuilt.empty: set merge_success=False, return
            write_atomic(rebuilt, session_file)
        ELSE (orchestrator-start):
            # In orchestrator-start, merge_session_1s_parquets gap-fills main[-1]→session[0]
            # automatically, so overnight data is recovered even if session file is late.
            action = "gap_fill_then_merge"
            fetched = gap_fill_to_now(ib, contract, main_path)
            IF fetched.empty: set merge_success=False, return
            write_atomic(fetched, session_file)

    # Merge into main (all paths that didn't return early)
    call merge_session_1s_parquets(DATA_DIR) from data.parquet_maintenance
    → this handles IB gap-fill between main[-1] and session[0], concat, atomic write,
      session file deletion
    backup_main(main_path)
    result["merge_success"] = True
    result["backup_written"] = True
    result["late_start_hours"] = v["late_start_hours"]
```

**Note on merge**: Rather than reimplementing the merge logic, call
`merge_session_1s_parquets(DATA_DIR)` from `data.parquet_maintenance`. This handles all
the IB gap-fill (main[-1] → session[0]), dedup, atomic write, and session file cleanup
in one call. Wrap in try/except; on exception set `merge_success=False`.

**`main()`**

1. `argparse`: `--mode {session-end,orchestrator-start}`, `--dry-run`
2. `load_dotenv()`
3. Connect IB (`clientId=IB_CLIENT_ID`); on failure: set `ib=None`, continue (merge still
   possible if session file is valid; targeted fill and rebuild will be skipped)
4. For each instrument in `INSTRUMENTS`: call `process_instrument()`, collect results
5. Disconnect IB in `finally`
6. Compute `exit_code`:
   - Start at `0`; any `action != "skip"` → `max(exit_code, 1)`
   - Any `merge_success == False` → `max(exit_code, 2)`
   - Any uncaught exception in outer try → `exit_code = 3`
7. `print(json.dumps(report, indent=2))` to stdout
8. `sys.exit(exit_code)`

**Report schema:**
```json
{
  "mode": "session-end",
  "dry_run": false,
  "instruments": {
    "MNQ": {
      "severity": "major",
      "action": "targeted_fill_then_merge",
      "merge_success": true,
      "backup_written": true,
      "validation": { "rows": 23400, "bad_rows": 50, "max_gap_s": 1800, "unexpected_gaps": [...] },
      "gap_fill_bars": 1800,
      "merged_rows": 85200
    },
    "MES": { ... }
  },
  "exit_code": 1
}
```

---

### Task 2 — CREATE `.claude/skills/parquet-check/SKILL.md`

**WAVE**: 1  
**AGENT_ROLE**: Primary implementer  
**DEPENDS_ON**: None (skill references the script by path only)

**File**: `.claude/skills/parquet-check/SKILL.md`

**Frontmatter:**
```yaml
---
name: parquet-check
description: >
  Use when the trading session ends or the orchestrator is about to start, to validate
  and repair 1s session parquet files. Validates MNQ_1s_session_*.parquet and
  MES_1s_session_*.parquet, auto-repairs issues (targeted fill for minor/major gaps,
  full IB rebuild for critical ones in session-end mode), merges into main parquets,
  and backs up the result. Runs end-to-end without confirmation prompts.
  Trigger phrases: "session ended", "run parquet check", "check the session data",
  "parquet health", "check data integrity", "merge session data", "data health check".
---
```

**Skill body — sections:**

**Mode selection table:**
| Context | Mode |
|---|---|
| User/agent says session just ended / it's post-16:00 ET on a trading day | `session-end` |
| User/agent says orchestrator is starting / pre-session | `orchestrator-start` |
| Ambiguous | Ask one question: "Is this a session-end check or a pre-session orchestrator-start check?" |

**Step 1 — Run the engine:**
```powershell
cd "C:\Users\gilad\projects\auto-co-trader\live"
uv run python scripts/check_session_parquets.py --mode <MODE> 2>check_session_stderr.log
```
Capture stdout (JSON). On failure to run: read `check_session_stderr.log` for error.

**Step 2 — Parse and assess:**

Describe exactly how to interpret the JSON:
- For each instrument: read `severity`, `action`, `merge_success`, `backup_written`
- If `merge_success = true` → success path; report what was done
- If `merge_success = false` → escalation; tell user what manual steps are needed
- If `exit_code = 3` → script error; read stderr log, report raw error

**Step 3 — LLM severity judgment for ambiguous cases:**

The script classifies mechanically. After reading the JSON, the LLM should flag these
additional concerns even if severity was "ok":
- Unusually low bar count for the session period (< 50% of expected for RTH)
- Last bar timestamp that is much earlier than when the skill was invoked (session
  data stopped mid-session)
- Price levels that look anomalous compared to the main parquet's recent prices
- `late_start_hours > 2.0` in session-end mode: the session file was missing overnight
  data; if `action = rebuild_then_merge`, confirm the rebuild covers from 18:00 ET
  (CME open) so overnight bars are restored. If action was only `merge`, note that
  `merge_session_1s_parquets` gap-fills main[-1]→session[0] from IB, which may or may
  not recover overnight data depending on IB availability.

If any of these are flagged, note it in the summary and suggest the user verify manually.

**Step 4 — Final summary:**
Concise 3–5 lines:
- What was found per instrument (severity, action, bars merged, gap bars added)
- Whether backups were written
- Any items needing manual attention

**HARD RULE — no code changes:**
The agent executing this skill MUST NOT use Edit, Write, or any tool that modifies
`.py`, `.md`, or any other non-parquet file. This skill operates on data only.

If the LLM determines that a code change would be beneficial (e.g., a recurring gap
pattern suggests a bug in `ib_realtime.py`), it MUST:
1. Describe the proposed change in plain text in the terminal output
2. Stop — do not implement it
3. Wait for the user to explicitly request the code change in a new message

Allowed file writes: `data/*.parquet`, `data/*.parquet.bak`, `data/*.parquet.tmp` only.

---

### Task 3 — CREATE `tests/test_check_session_parquets.py` + UPDATE `tests/test_parquet_maintenance.py`

**WAVE**: 2  
**AGENT_ROLE**: Primary implementer  
**DEPENDS_ON**: Task 0, Task 1

**File**: `tests/test_check_session_parquets.py`

**Test fixture:**
```python
@pytest.fixture
def bar_dir(tmp_path):
    return tmp_path

def _make_session_df(timestamps, price=27000.0):
    """Build a valid OHLCV DataFrame at given timestamps (ET-aware)."""
    idx = pd.DatetimeIndex([
        pd.Timestamp(ts, tz="America/New_York") for ts in timestamps
    ])
    return pd.DataFrame({
        "Open": price, "High": price + 10, "Low": price - 10,
        "Close": price, "Volume": 100.0,
    }, index=idx)
```

**Test classes and cases:**

**`class TestValidateSessionDf`** (pure unit — no IB, no disk)

| Test | Description | Expected severity |
|---|---|---|
| `test_ok_clean_df` | No gaps, no bad rows | `ok` |
| `test_minor_single_bad_price_row` | 1 bad price row in 1000 | `minor` |
| `test_major_bad_row_fraction` | 2% bad rows | `major` |
| `test_critical_bad_row_fraction` | 6% bad rows | `critical` |
| `test_minor_small_gap` | 3-min unexpected gap | `minor` |
| `test_major_large_gap` | 30-min unexpected gap | `major` |
| `test_critical_very_large_gap` | 90-min unexpected gap | `critical` |
| `test_empty_df_is_critical` | `pd.DataFrame()` | `critical` |
| `test_none_df_is_critical` | `None` passed as df | `critical` |
| `test_maintenance_gap_ignored` | 17:01–17:59 gap | `ok` (expected window) |
| `test_weekend_gap_ignored` | Friday 17:01 gap | `ok` (expected window) |
| `test_late_start_returns_late_start_hours` | session starts 09:30, expected 18:00 prev | `late_start_hours ≈ 15.5`, severity unchanged (caller escalates) |
| `test_on_time_start_zero_late_hours` | session starts 18:05, expected 18:00 | `late_start_hours < 0.1` |
| `test_no_expected_start_zero_late_hours` | valid df, `expected_session_start=None` | `late_start_hours == 0.0` |

**`class TestIsExpectedClosed`** (pure unit)

| Test | Description | Expected |
|---|---|---|
| `test_friday_close_expected` | Fri 17:01 → Sat 00:00 | `True` |
| `test_saturday_expected` | Sat 12:00 gap | `True` |
| `test_sunday_before_18_expected` | Sun 10:00 gap | `True` |
| `test_weekday_maint_expected` | Mon 17:01–17:55 | `True` |
| `test_weekday_overnight_unexpected` | Tue 02:00 gap | `False` |
| `test_maint_too_long_unexpected` | 17:00–19:00 (>75 min) | `False` |

**`class TestWriteAtomicAndBackup`** (disk I/O, no IB)

| Test | Description | Assertion |
|---|---|---|
| `test_write_atomic_produces_correct_file` | write 3-row DF | file readable, 3 rows |
| `test_write_atomic_no_tmp_left` | after write | `.parquet.tmp` does not exist |
| `test_backup_main_overwrites_bak` | write bak, then backup again | .bak has new content |

**`class TestProcessInstrumentSessionEnd`** (mocked IB)

```python
def _make_ib_mock(bars=None):
    ib = Mock()
    ib.connect = Mock()
    ib.disconnect = Mock()
    ib.isConnected = Mock(return_value=True)
    ib.reqHistoricalData = Mock(return_value=bars or [])
    ib.errorEvent = Mock()
    ib.errorEvent.__iadd__ = Mock()
    ib.errorEvent.__isub__ = Mock()
    return ib
```

| Test | Setup | Expected action | Expected outcome |
|---|---|---|---|
| `test_ok_session_merges_and_backs_up` | valid session file | `merge` | merge_success=True, backup_written=True, session file deleted |
| `test_minor_session_merges_as_is` | 3-min gap in session | `merge` | merge_success=True |
| `test_major_session_targeted_fill` | 30-min gap | `targeted_fill_then_merge` | IB fetch called for gap window |
| `test_critical_session_end_rebuilds` | empty session file | `rebuild_then_merge` | IB fetch called for full session range |
| `test_no_session_file_session_end_skip` | no files | `skip` | no IB call |
| `test_dry_run_no_disk_writes` | valid session | `merge` | no `.parquet` files written |
| `test_late_start_escalates_to_rebuild` | session file starts 09:30, expected 18:00 prev, mode=session-end | `rebuild_then_merge` | severity overridden to critical, IB fetch covers full 18:00→now window |

**`class TestProcessInstrumentOrchestratorStart`** (mocked IB)

| Test | Setup | Expected action | Expected outcome |
|---|---|---|---|
| `test_no_session_file_gap_fills` | no session file, valid main | `gap_fill_created_session` | IB fetch called, session file created, merge called |
| `test_critical_orch_start_gap_fills_not_rebuilds` | corrupt session file | `gap_fill_then_merge` | IB fetch main[-1]→now; NOT full session rebuild |

**`class TestMainEntryPoint`** (mocked IB connection, disk I/O)

| Test | Description | Assertion |
|---|---|---|
| `test_main_outputs_valid_json` | run with `--mode session-end --dry-run` | stdout is valid JSON with `mode`, `instruments`, `exit_code` |
| `test_main_exit_code_0_when_no_sessions` | no session files, dry-run | sys.exit(0) |
| `test_main_exit_code_1_when_fixed` | valid session, mocked IB | sys.exit(1) |

**ADD to `tests/test_parquet_maintenance.py`** — new class for the chunked gap-fill:

**`class TestFetchGapChunked`** (mocked IB — in `test_parquet_maintenance.py`)

| Test | Setup | Expected |
|---|---|---|
| `test_small_gap_single_chunk` | gap = 300s, IB returns 300 bars | returns 300-bar DF, success=True |
| `test_large_gap_multiple_chunks` | gap = 5400s (3×1800), IB returns 1800 bars per call | returns 5400-bar DF combined, success=True |
| `test_pacing_retry_succeeds` | first call returns no bars + Error 162, second returns bars | sleeps PACING_SLEEP_S, retries, returns bars, success=True |
| `test_pacing_max_retries_exceeded` | Error 162 on every retry, MAX_RETRIES+1 times | returns empty DF, success=False |
| `test_empty_response_non_pacing_is_success` | gap in weekend window, 0 bars, no Error 162 | returns empty DF, success=True |
| `test_merge_skipped_on_gap_fill_failure` | _fetch_gap_chunked returns success=False | merge_session_1s_parquets does NOT write main parquet, does NOT delete session file |
| `test_overnight_gap_fetched_in_chunks` | gap = 57600s (overnight), IB returns bars for each chunk | main parquet contains overnight bars after merge |

---

## TEST AUTOMATION SUMMARY

| # | Test | Tool | File | Run command |
|---|---|---|---|---|
| 1–7 | `TestFetchGapChunked` | pytest | `tests/test_parquet_maintenance.py` | `uv run pytest tests/test_parquet_maintenance.py::TestFetchGapChunked -v` |
| 8–21 | `TestValidateSessionDf` | pytest | `tests/test_check_session_parquets.py` | `uv run pytest tests/test_check_session_parquets.py::TestValidateSessionDf -v` |
| 22–27 | `TestIsExpectedClosed` | pytest | `tests/test_check_session_parquets.py` | `uv run pytest tests/test_check_session_parquets.py::TestIsExpectedClosed -v` |
| 28–30 | `TestWriteAtomicAndBackup` | pytest | `tests/test_check_session_parquets.py` | `uv run pytest tests/test_check_session_parquets.py::TestWriteAtomicAndBackup -v` |
| 31–37 | `TestProcessInstrumentSessionEnd` | pytest | `tests/test_check_session_parquets.py` | `uv run pytest tests/test_check_session_parquets.py::TestProcessInstrumentSessionEnd -v` |
| 38–39 | `TestProcessInstrumentOrchestratorStart` | pytest | `tests/test_check_session_parquets.py` | `uv run pytest tests/test_check_session_parquets.py::TestProcessInstrumentOrchestratorStart -v` |
| 40–43 | `TestMainEntryPoint` | pytest | `tests/test_check_session_parquets.py` | `uv run pytest tests/test_check_session_parquets.py::TestMainEntryPoint -v` |
| — | Full suite regression check | pytest | `tests/` | `uv run pytest tests/ -x -q` |

All tests automated. No manual tests required.

**Script runnability criteria (distinct from logic tests):**
- ✅ `uv run python scripts/check_session_parquets.py --mode session-end --dry-run` completes without exception (covered by `test_main_outputs_valid_json`)
- ✅ JSON output includes `late_start_hours` per instrument (covered by `TestValidateSessionDf`)
- ✅ Output uses ASCII-only characters (JSON output is ASCII-safe by design; no emoji or unicode in print statements)
- ✅ No subprocess spawns `claude` — not applicable
- ✅ `merge_session_1s_parquets` does NOT write main parquet when `_fetch_gap_chunked` returns `success=False` (covered by `test_merge_skipped_on_gap_fill_failure`)

---

## ACCEPTANCE CRITERIA

- [ ] `scripts/check_session_parquets.py` exists and accepts `--mode {session-end,orchestrator-start}` and `--dry-run`
- [ ] Running with `--dry-run` produces valid JSON on stdout and makes no changes to disk
- [ ] `severity` is classified correctly: ok/minor/major/critical as per thresholds
- [ ] `session-end` + critical → IB rebuild of full session range; result merged into main atomically
- [ ] `orchestrator-start` + critical → IB gap-fill main[-1]→now only (no full session rebuild)
- [ ] `session-end` + no session file → `action=skip`, no IB call
- [ ] `orchestrator-start` + no session file → gap-fill from main[-1] to now, write as session file, merge
- [ ] After every successful merge: `.parquet.bak` is overwritten for that instrument
- [ ] Session file is deleted after successful merge
- [ ] Main parquet is never written if merge fails or is skipped
- [ ] `write_atomic` always uses `.parquet.tmp` → `os.replace`; no `.tmp` files left on disk
- [ ] IB connection failure is non-fatal: merge proceeds with available data
- [ ] Pacing (Error 162) handled: sleep 660s, retry once
- [ ] `_fetch_gap_chunked` chunks any gap into ≤ 1800 S requests (IB hard limit for 1s bars)
- [ ] `_fetch_gap_chunked` retries on Error 162 after sleeping 660 s; aborts after 3 consecutive pacing failures
- [ ] On gap-fill abort: WARNING printed to stdout; main parquet NOT written; session file NOT deleted
- [ ] `validate_session_df` returns `late_start_hours` field (0.0 when `expected_session_start=None`)
- [ ] `session-end` mode: if session file starts > 2h after expected CME open (18:00 ET prev evening), severity is escalated to `critical` and a full session rebuild is triggered (18:00→now, useRTH=False) to restore overnight coverage
- [ ] `orchestrator-start` mode: late start does NOT trigger rebuild; merge gap-fill covers main[-1]→session[0] overnight automatically
- [ ] `.claude/skills/parquet-check/SKILL.md` exists with correct frontmatter and decision table
- [ ] All 43 tests pass
- [ ] Full test suite (`tests/`) passes with no new failures
- [ ] SKILL.md contains explicit "no code changes" hard rule: agent may only write `data/*.parquet*` files; any code-level findings are proposed as text only

---

## VERIFICATION STEPS

After implementation, run in order:

### 1. Syntax / import check
```powershell
uv run python -c "import scripts.check_session_parquets; print('ok')"
```
Expected: prints `ok` with no import errors.

### 2. Dry-run smoke test (no session files)
```powershell
uv run python scripts/check_session_parquets.py --mode session-end --dry-run
```
Expected: valid JSON on stdout, `exit_code: 0`, no disk changes, no IB connection attempt.

### 3. Skill file loadable
```powershell
Get-Content ".claude\skills\parquet-check\SKILL.md" | Select-Object -First 10
```
Expected: frontmatter header `---` visible, `name: parquet-check` present.

### 4. Unit tests
```powershell
uv run pytest tests/test_check_session_parquets.py -v
```
Expected: all 31 tests pass.

### 5. Full regression suite
```powershell
uv run pytest tests/ -x -q
```
Expected: no regressions (same pass/fail ratio as before this feature).

### 6. Live probe (optional — requires IB connected)
Create a minimal session file then run:
```powershell
uv run python -c "
import pandas as pd; from pathlib import Path
df = pd.DataFrame({'Open':[27000.],'High':[27010.],'Low':[26990.],'Close':[27005.],'Volume':[100.]},
    index=pd.DatetimeIndex([pd.Timestamp('2026-05-21 09:30:00', tz='America/New_York')]))
df.to_parquet('data/MNQ_1s_session_20260521.parquet')
"
uv run python scripts/check_session_parquets.py --mode orchestrator-start --dry-run
```
Expected: JSON shows `MNQ` with `session_found: true`, `severity: critical` (single bar,
large gap from main[-1]), `action: gap_fill_then_merge`, `merge_success: null` (dry-run).

---

## RISKS AND MITIGATIONS

| Risk | Mitigation |
|---|---|
| IB pacing during large session rebuild (44+ chunks) | Error 162 handler retries after 660s sleep; script is synchronous and patient |
| `merge_session_1s_parquets` uses clientId=16; this script uses clientId=17 — conflict if both run concurrently | clientId=17 reserved for this script; `merge_session_1s_parquets` is called from within this script, not concurrently |
| `process_instrument` calls `merge_session_1s_parquets` after writing session file; if IB is unavailable, gap between main[-1] and session[0] is unfilled | Non-fatal: merge proceeds without gap fill (existing behavior in `merge_session_1s_parquets`) |
| `get_session_start_for_end_mode` may return wrong day near midnight | Handle: if now is between 00:00–18:00 ET, session started at yesterday 18:00 |

---

## PATTERNS USED

- Chunked IB fetch: `scripts/fill_mnq_1s_overnight_gaps.py::fetch_gap()` (verbatim)
- `_prev_trading_ts()`: `scripts/rebuild_mes_1s.py` (copy or import)
- Atomic write: `data/parquet_maintenance.py` pattern (tmp → os.replace, use_dictionary=False)
- Skill file format: `.claude/skills/run-orchestrator/SKILL.md` (frontmatter + decision table)
- Test IB mock: `tests/test_parquet_maintenance.py::_make_ib_mock` pattern
