# Feature: Phase 5 — End-to-End Integration Test

**⚠️ EXECUTION RULES — READ FIRST:**
- Implement ALL changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
- Only make code changes — no git operations

Validate documentation and codebase patterns before implementing. Match naming of existing utils, types, and models. Import from correct files.

---

## Feature Description

Verify the complete stock backtester pipeline works end-to-end with real market data. Configure five tickers, download OHLCV history via yfinance, run the backtester, and validate the output. Also simulate one full agent loop step (threshold mutation → rerun → compare) to confirm the optimization infrastructure is viable on real data. This is the final phase before handing off the system to autonomous agent use.

## User Story

As a developer/trader,
I want to run the full pipeline (prepare → backtest → output) on real data,
So that I can hand off the system to the autonomous agent with confidence that the plumbing works.

## Problem Statement

Phases 1–4 implemented all components in isolation. Phase 5 exercises them together: real yfinance data → disk cache → backtester → parseable output → Sharpe comparison. Key unknowns:
- Do the five tickers download cleanly within the 730-day yfinance 1h window?
- Does the strict screener produce any trades in the 2026-01-01 to 2026-03-01 window?
- Does the agent loop's "modify threshold → rerun" workflow produce a different Sharpe?

## Solution Statement

1. Set `TICKERS = ["AAPL", "MSFT", "NVDA", "JPM", "TSLA"]` in `prepare.py`
2. Create `tests/test_e2e.py` with eight `@pytest.mark.integration` tests covering the full pipeline
3. Run `uv run prepare.py` to populate the Parquet cache
4. Run the integration test suite to validate data schema, backtest output, P&L arithmetic, Criterion 18 (screen_day on real data), and a one-step agent loop simulation
5. Update `PROGRESS.md` with Phase 5 completion status

## Feature Metadata

**Feature Type**: Integration / Validation
**Complexity**: Medium
**Primary Systems Affected**: `prepare.py`, `train.py`, `tests/test_e2e.py`
**Dependencies**: yfinance (network), pytest, existing parquet cache
**Breaking Changes**: No — only `prepare.py` TICKERS constant is changed; the empty list `[]` was a placeholder

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ BEFORE IMPLEMENTING

- `prepare.py` (lines 19–24) — USER CONFIGURATION block; TICKERS to be set here
- `prepare.py` (lines 95–110) — `process_ticker()`: idempotent, skips existing files
- `train.py` (lines 1–17) — CACHE_DIR and BACKTEST_START/BACKTEST_END constants
- `train.py` (lines 150–246) — `screen_day()`: the function exercised in Criterion 18
- `train.py` (lines 271–373) — `run_backtest()`: harness under test; must not be modified
- `train.py` (lines 376–394) — `print_results()` and `__main__` block
- `tests/test_prepare.py` — pattern for mocking yfinance and using `@pytest.mark.integration`
- `tests/test_backtester.py` — pattern for synthetic DataFrames and run_backtest assertions
- `tests/test_optimization.py` — pattern for subprocess runs, DO NOT EDIT hash guard, `_make_rising_dataset()`
- `pyproject.toml` — integration marker already registered (`[tool.pytest.ini_options]`)

### New Files to Create

- `tests/test_e2e.py` — eight integration tests for the full pipeline

### Relevant Documentation

- `prd.md` §Phase 5 (lines 519–528) — official task list for this phase
- `prd.md` §Risk: No trades in backtest window — motivates the relaxed-screener fallback test
- `prd.md` §Feature 5 (lines 238–261) — exact Sharpe formula and output block format
- `PROGRESS.md` §Criterion 18 — screen_day on real Parquet data, unverifiable until Phase 5

### Patterns to Follow

**Integration marker**: `@pytest.mark.integration` — see `tests/test_prepare.py::test_download_ticker_returns_expected_schema`
**Subprocess pattern**: `subprocess.run(["uv", "run", ...], capture_output=True, text=True, cwd=repo_root)` — see `tests/test_optimization.py`
**Skip-if-no-data guard**: check `os path.isdir(CACHE_DIR)` or use a pytest fixture that skips if parquet files absent
**Naming**: snake_case functions, `test_` prefix, descriptive names that state the assertion

---

## PARALLEL EXECUTION STRATEGY

### Dependency Graph

```
┌────────────────────────────────────────────────────┐
│ WAVE 1: Code Changes (Parallel)                    │
├──────────────────────────────────────────────────  ┤
│ Task 1.1: UPDATE prepare.py  │ Task 1.2: CREATE    │
│   (set TICKERS)              │   tests/test_e2e.py │
│   Agent: configurator        │   Agent: test-writer│
└──────────────────────────────┴────────────────────-┘
                    ↓
┌────────────────────────────────────────────────────┐
│ WAVE 2: Data Download (Sequential — network I/O)   │
├────────────────────────────────────────────────────┤
│ Task 2.1: RUN `uv run prepare.py` — populate cache │
│   Agent: integrator (or single execution agent)    │
└────────────────────────────────────────────────────┘
                    ↓
┌────────────────────────────────────────────────────┐
│ WAVE 3: Validation (Sequential — data required)    │
├────────────────────────────────────────────────────┤
│ Task 3.1: RUN full test suite (unit + integration) │
│   Agent: validator                                 │
└────────────────────────────────────────────────────┘
```

### Parallelization Summary

**Wave 1 — Fully Parallel**: Tasks 1.1 and 1.2 — no data dependency between them
**Wave 2 — Sequential**: Task 2.1 — requires Wave 1 (prepare.py must have TICKERS set)
**Wave 3 — Sequential**: Task 3.1 — requires Wave 2 (integration tests need Parquet cache)

### Interface Contracts

**Contract Wave 1→2**: Task 1.1 sets `TICKERS` in `prepare.py`; Task 2.1 runs that file. Task 1.2 creates `tests/test_e2e.py` referencing `CACHE_DIR` from `train.py`; if Task 1.2 is incomplete, Wave 3 will fail to collect tests.

**Contract Wave 2→3**: Wave 2 populates `~/.cache/autoresearch/stock_data/{AAPL,MSFT,NVDA,JPM,TSLA}.parquet`. Integration tests in `test_e2e.py` skip (or fail informatively) if these files are absent — so a Wave 2 failure produces clear skip output, not a mysterious import error.

### Synchronization Checkpoints

**After Wave 1**: `python -c "import prepare; import tests.test_e2e"` — both modules importable without errors
**After Wave 2**: `ls ~/.cache/autoresearch/stock_data/*.parquet | wc -l` — expect 5
**After Wave 3**: `uv run pytest tests/ -v --tb=short 2>&1 | tail -5` — all tests pass

---

## IMPLEMENTATION PLAN

### Phase 1: Wave 1 Code Changes

#### Task 1.1 — UPDATE `prepare.py` TICKERS

Set the five tickers in the USER CONFIGURATION block.

#### Task 1.2 — CREATE `tests/test_e2e.py`

Eight integration tests. All marked `@pytest.mark.integration`. Each test uses a shared
`cache_available` fixture that skips the test if the Parquet cache is absent (so the file
can be collected and run in CI without network access, producing "skipped" rather than "error").

### Phase 2: Wave 2 Data Download

#### Task 2.1 — RUN `uv run prepare.py`

Execution agent runs this as a shell command. Expects output like:
```
Downloading 5 tickers → /Users/…/.cache/autoresearch/stock_data
AAPL: saved 313 days → /…/AAPL.parquet
…
Done: 5/5 tickers cached successfully.
```

If a ticker fails (yfinance instability), note it and continue — the failing ticker will be
missing from the cache and the integration tests will still run on the available tickers.

### Phase 3: Wave 3 Validation

#### Task 3.1 — RUN full test suite

Run `uv run pytest tests/ -v --tb=short 2>&1 | tee pytest-e2e.log`.
- Pre-existing 78 tests must still pass (zero regressions)
- New 8 integration tests must all pass (or clearly skip if data unavailable)

---

## STEP-BY-STEP TASKS

Tasks organized by execution wave. Same wave = safe to run in parallel.

---

### WAVE 1: Code Changes

#### Task 1.1: UPDATE `prepare.py` — set TICKERS constant

- **WAVE**: 1
- **AGENT_ROLE**: configurator
- **DEPENDS_ON**: []
- **BLOCKS**: [2.1]
- **PROVIDES**: A `prepare.py` with 5 tickers that can be run immediately
- **IMPLEMENT**:
  Replace the empty list in the USER CONFIGURATION block:
  ```python
  # BEFORE
  TICKERS = []  # TODO: fill in ticker symbols, e.g. ["AAPL", "MSFT", "NVDA"]

  # AFTER
  TICKERS = ["AAPL", "MSFT", "NVDA", "JPM", "TSLA"]
  ```
  The surrounding comment and all other constants remain unchanged.
- **VALIDATE**: `python -c "import prepare; assert prepare.TICKERS == ['AAPL','MSFT','NVDA','JPM','TSLA'], 'TICKERS not set'"` → exits 0

---

#### Task 1.2: CREATE `tests/test_e2e.py` — eight integration tests

- **WAVE**: 1
- **AGENT_ROLE**: test-writer
- **DEPENDS_ON**: []
- **BLOCKS**: [3.1]
- **PROVIDES**: A complete integration test suite for Phase 5 validation
- **IMPLEMENT**: Create `tests/test_e2e.py` with the tests described below.

**Test file header and fixtures:**

```python
"""tests/test_e2e.py — Phase 5 end-to-end integration tests. All @pytest.mark.integration."""
import ast, importlib.util, os, pathlib, subprocess, tempfile, unittest.mock as mock
import pandas as pd
import pytest
import train
from prepare import CACHE_DIR, BACKTEST_START

REPO_ROOT = pathlib.Path(__file__).parent.parent
EXPECTED_TICKERS = ["AAPL", "MSFT", "NVDA", "JPM", "TSLA"]
EXPECTED_COLUMNS = {"open", "high", "low", "close", "volume", "price_10am"}

@pytest.fixture(scope="module")
def all_parquet_paths():
    p = pathlib.Path(CACHE_DIR)
    return sorted(p.glob("*.parquet")) if p.is_dir() else []

@pytest.fixture(scope="module")
def skip_if_cache_missing(all_parquet_paths):
    if not all_parquet_paths:
        pytest.skip(f"Cache empty at {CACHE_DIR}. Run `uv run prepare.py` first.")

@pytest.fixture(scope="module")
def first_parquet_df(all_parquet_paths, skip_if_cache_missing):
    return pd.read_parquet(all_parquet_paths[0])

@pytest.fixture(scope="module")
def all_ticker_dfs(skip_if_cache_missing):
    return train.load_all_ticker_data()

@pytest.fixture(scope="module")
def backtest_stats(all_ticker_dfs):
    return train.run_backtest(all_ticker_dfs)
```

**Test 1 — Parquet files exist after prepare.py**
```python
@pytest.mark.integration
def test_parquet_files_exist(all_parquet_paths, skip_if_cache_missing):
    """
    Verify that at least the expected tickers have parquet files in CACHE_DIR.
    One file per ticker in EXPECTED_TICKERS must exist.
    """
    present = {p.stem for p in all_parquet_paths}
    missing = [t for t in EXPECTED_TICKERS if t not in present]
    assert not missing, (
        f"Missing parquet files for tickers: {missing}. "
        f"Run `uv run prepare.py` to download them."
    )
```

**Test 2 — Parquet schema matches train.py expectations**
```python
@pytest.mark.integration
def test_parquet_schema_has_required_columns(first_parquet_df, skip_if_cache_missing):
    """
    Every parquet file must contain the columns that train.py's screener reads.
    Checks the first available file as representative.
    """
    actual_columns = set(first_parquet_df.columns)
    missing = EXPECTED_COLUMNS - actual_columns
    assert not missing, f"Parquet file is missing columns: {missing}"
```

**Test 3 — Parquet index is Python date objects**
```python
@pytest.mark.integration
def test_parquet_index_is_date_objects(first_parquet_df, skip_if_cache_missing):
    """
    train.py slices with df.loc[:today] where today is a datetime.date object.
    The index must be date objects, not pd.Timestamp or strings.
    """
    import datetime
    assert len(first_parquet_df) > 0, "Parquet file is empty"
    sample = first_parquet_df.index[0]
    assert type(sample) is datetime.date, (
        f"Expected datetime.date index, got {type(sample).__name__}. "
        "train.py slicing with df.loc[:today] requires date objects."
    )
```

**Test 4 — train.py exits 0 and produces parseable sharpe output**
```python
@pytest.mark.integration
def test_train_exits_zero_with_sharpe_output(skip_if_cache_missing, tmp_path):
    """
    `uv run train.py` must exit with code 0 and print 'sharpe: <float>' to stdout.
    This is the primary integration smoke test — if this passes, the agent loop works.
    """
    result = subprocess.run(
        ["uv", "run", "train.py"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, (
        f"train.py exited with code {result.returncode}.\n"
        f"stderr:\n{result.stderr[-2000:]}\n"
        f"stdout:\n{result.stdout[-500:]}"
    )
    sharpe_lines = [
        line for line in result.stdout.splitlines()
        if line.startswith("sharpe:")
    ]
    assert len(sharpe_lines) == 1, (
        f"Expected exactly one 'sharpe:' line in output, got {len(sharpe_lines)}.\n"
        f"stdout:\n{result.stdout}"
    )
    # Sharpe value must be parseable as float
    value_str = sharpe_lines[0].split(":", 1)[1].strip()
    sharpe_value = float(value_str)  # raises ValueError if not parseable
    assert isinstance(sharpe_value, float)
```

**Test 5 — All seven output fields present**
```python
@pytest.mark.integration
def test_output_has_all_seven_fields(skip_if_cache_missing):
    """
    The output block parsed by the agent must contain all 7 required fields.
    Runs train.py via subprocess and checks stdout for each field name.
    """
    result = subprocess.run(
        ["uv", "run", "train.py"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, f"train.py crashed: {result.stderr[-1000:]}"
    required_fields = [
        "sharpe:", "total_trades:", "win_rate:",
        "avg_pnl_per_trade:", "total_pnl:",
        "backtest_start:", "backtest_end:",
    ]
    for field in required_fields:
        assert any(line.startswith(field) for line in result.stdout.splitlines()), (
            f"Missing output field '{field}' in train.py output.\n"
            f"stdout:\n{result.stdout}"
        )
```

**Test 6 — screen_day runs on real parquet data (Criterion 18)**
```python
@pytest.mark.integration
def test_screen_day_on_real_parquet_data(all_parquet_paths, skip_if_cache_missing):
    """
    Criterion 18 from the screener acceptance criteria: screen_day must run
    without raising an exception on the last 10 days of any real parquet file.

    Does NOT assert that signals are generated — only that no exception is raised.
    """
    import datetime
    df = pd.read_parquet(all_parquet_paths[0])
    backtest_start = datetime.date.fromisoformat(BACKTEST_START)

    # Get last 10 trading days in the backtest window
    backtest_days = [d for d in df.index if d >= backtest_start]
    if not backtest_days:
        pytest.skip("No backtest-window days in the parquet file — date range mismatch.")

    sample_days = sorted(backtest_days)[-10:]
    errors = []
    for today in sample_days:
        hist = df.loc[:today]
        try:
            result = train.screen_day(hist, today)
            assert result is None or (isinstance(result, dict) and "stop" in result), (
                f"screen_day returned unexpected type {type(result)} on {today}"
            )
        except Exception as exc:
            errors.append(f"{today}: {exc}")

    assert not errors, (
        f"screen_day raised exceptions on {len(errors)} days:\n" + "\n".join(errors)
    )
```

**Test 7 — P&L self-consistency when trades occur**
```python
@pytest.mark.integration
def test_pnl_self_consistency(backtest_stats):
    """total_pnl ≈ avg_pnl_per_trade × total_trades (within $0.05/trade rounding tolerance)."""
    total, avg, n = backtest_stats["total_pnl"], backtest_stats["avg_pnl_per_trade"], backtest_stats["total_trades"]
    if n == 0:
        assert total == 0.0 and avg == 0.0
    else:
        assert abs(total - round(avg * n, 2)) <= 0.05 * n, (
            f"P&L inconsistency: total={total}, avg×n={round(avg*n,2)}"
        )
```

**Test 8 — Agent loop step simulation (threshold mutation → compare Sharpe)**

Uses the same `importlib` pattern from `tests/test_optimization.py::test_editable_section_stays_runnable_after_threshold_change`:
read `train.py` source, replace `"c0 < -50"` → `"c0 < -30"`, load as a temporary module, call `run_backtest` on real data.

```python
@pytest.mark.integration
def test_agent_loop_threshold_mutation_no_crash(all_ticker_dfs, backtest_stats):
    """
    Simulates the first agent loop iteration on real data:
      1. Baseline Sharpe from the default (strict) screener — from fixture.
      2. Relaxed screener (CCI -50 → -30) via a temporary modified module.
      3. Both runs complete without error.
      4. Relaxed screener must produce ≥ 1 trade — proves agent has a viable path.

    Does NOT assert relaxed Sharpe > strict Sharpe (flaky on real data).
    Asserts viability: at least one threshold mutation generates signal.
    """
    import importlib.util, tempfile, os as _os, ast

    train_source = REPO_ROOT.joinpath("train.py").read_text(encoding="utf-8")
    assert "c0 < -50" in train_source, (
        "CCI threshold 'c0 < -50' not found in train.py editable section. "
        "Update this test if the threshold expression was changed."
    )
    relaxed_source = train_source.replace("c0 < -50", "c0 < -30", 1)
    ast.parse(relaxed_source)  # must be valid Python

    with tempfile.NamedTemporaryFile(suffix=".py", delete=False,
                                     mode="w", encoding="utf-8") as f:
        f.write(relaxed_source)
        tmp_path = f.name

    try:
        spec = importlib.util.spec_from_file_location("train_relaxed", tmp_path)
        relaxed_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(relaxed_mod)
        relaxed_stats = relaxed_mod.run_backtest(all_ticker_dfs)
    finally:
        _os.unlink(tmp_path)

    assert "sharpe" in backtest_stats  # baseline from fixture
    assert "sharpe" in relaxed_stats

    # Key Phase 5 viability requirement (PRD: "Phase 5 validation explicitly
    # checks for at least 1 trade before handing off to the agent")
    assert relaxed_stats["total_trades"] >= 1, (
        f"Relaxed screener (CCI -30) produced 0 trades over "
        f"{BACKTEST_START} to {train.BACKTEST_END} on {len(all_ticker_dfs)} tickers. "
        "Extend BACKTEST_START/BACKTEST_END and re-run `uv run prepare.py`."
    )
```

- **VALIDATE**: `python -c "import tests.test_e2e"` → no import error; `uv run pytest tests/test_e2e.py --collect-only -q` → collects 8 tests

**Wave 1 Checkpoint**: `python -c "import prepare; import tests.test_e2e; print('imports OK')"` → prints "imports OK"

---

### WAVE 2: Data Download (Sequential — requires Wave 1)

#### Task 2.1: RUN `uv run prepare.py` — populate Parquet cache

- **WAVE**: 2
- **AGENT_ROLE**: integrator
- **DEPENDS_ON**: [1.1]
- **BLOCKS**: [3.1]
- **PROVIDES**: 5 parquet files in `~/.cache/autoresearch/stock_data/`
- **IMPLEMENT**:
  Run the following command from the repo root:
  ```bash
  uv run prepare.py
  ```
  Expected output pattern:
  ```
  Downloading 5 tickers → /Users/…/.cache/autoresearch/stock_data
  Date range: 2025-01-01 → 2026-03-01 (1h bars, resampled to daily)
    AAPL: saved NNN days → /…/AAPL.parquet
    MSFT: saved NNN days → /…/MSFT.parquet
    NVDA: saved NNN days → /…/NVDA.parquet
    JPM: saved NNN days → /…/JPM.parquet
    TSLA: saved NNN days → /…/TSLA.parquet
  Done: 5/5 tickers cached successfully.
  ```
  If a ticker fails due to transient yfinance error: note it and continue.
  If 3 or more tickers fail: stop and investigate (likely yfinance API issue).

- **VALIDATE**:
  ```bash
  ls ~/.cache/autoresearch/stock_data/*.parquet | wc -l
  # Expected: 5
  python -c "
  import pandas as pd, pathlib
  p = pathlib.Path.home() / '.cache/autoresearch/stock_data/AAPL.parquet'
  df = pd.read_parquet(p)
  print(f'AAPL: {len(df)} rows, columns: {list(df.columns)}')
  print(f'Index type: {type(df.index[0]).__name__}')
  "
  # Expected: rows >= 200, all 6 columns present, index type: date
  ```

**Wave 2 Checkpoint**:
```bash
ls ~/.cache/autoresearch/stock_data/*.parquet | wc -l  # expect 5
```

---

### WAVE 3: Validation (Sequential — requires Wave 2)

#### Task 3.1: RUN full test suite

- **WAVE**: 3
- **AGENT_ROLE**: validator
- **DEPENDS_ON**: [2.1, 1.2]
- **PROVIDES**: Pass/fail verdict on all 86 tests (78 pre-existing + 8 new)
- **IMPLEMENT**: Run the full test suite with integration tests enabled:
  ```bash
  uv run pytest tests/ -v -m "integration or not integration" --tb=short 2>&1 | tee pytest-e2e.log
  ```
  Alternatively, run unit and integration separately:
  ```bash
  uv run pytest tests/ -v --ignore=tests/test_e2e.py --tb=short  # 78 pre-existing
  uv run pytest tests/test_e2e.py -v -m integration --tb=short    # 8 new
  ```
- **VALIDATE**:
  ```bash
  grep -E "^(PASSED|FAILED|ERROR|tests/)" pytest-e2e.log | tail -20
  grep -E "^(FAILED|ERROR)" pytest-e2e.log  # must be empty
  grep "passed" pytest-e2e.log | tail -1     # expect 86 passed (or 78 + 8 if run separately)
  ```
  All 78 pre-existing tests must pass. All 8 integration tests must pass.
  SKIPPED is acceptable ONLY if the cache was not populated (Wave 2 failure).

**Final Checkpoint**: `uv run pytest tests/ --tb=short 2>&1 | tail -5`

---

## TESTING STRATEGY

**⚠️ ALL tests that can be automated MUST be automated.**

This phase has NO frontend and NO third-party API calls (yfinance runs during `prepare.py`, not during tests). All eight integration tests use subprocess calls or direct Python calls on the local cache — fully automatable with pytest.

### Unit Tests (pre-existing)

**Status**: ✅ Automated | **Tool**: pytest | **Location**: `tests/` | **Run**: `uv run pytest tests/ --ignore=tests/test_e2e.py -v`
**Count**: 78 tests (must remain all-passing — zero regressions)

### Integration Tests (new — Phase 5)

**Status**: ✅ Automated | **Tool**: pytest + subprocess | **Location**: `tests/test_e2e.py` | **Run**: `uv run pytest tests/test_e2e.py -m integration -v`

| # | Test Name | What It Covers | ✅/⚠️ |
|---|-----------|----------------|-------|
| 1 | `test_parquet_files_exist` | 5 parquet files present in CACHE_DIR | ✅ |
| 2 | `test_parquet_schema_has_required_columns` | 6 required columns in first parquet | ✅ |
| 3 | `test_parquet_index_is_date_objects` | Index type is `datetime.date`, not Timestamp | ✅ |
| 4 | `test_train_exits_zero_with_sharpe_output` | train.py subprocess: exit 0, "sharpe:" parseable | ✅ |
| 5 | `test_output_has_all_seven_fields` | All 7 output fields present in stdout | ✅ |
| 6 | `test_screen_day_on_real_parquet_data` | Criterion 18: no exception on last 10 real days | ✅ |
| 7 | `test_pnl_self_consistency` | total_pnl ≈ avg × n (arithmetic check) | ✅ |
| 8 | `test_agent_loop_threshold_mutation_no_crash` | Relaxed screener produces ≥ 1 trade on real data | ✅ |

### Edge Cases

- **Zero trades with strict screener**: ✅ Test 7 passes trivially (total == avg == 0). Test 8 verifies the relaxed screener still produces trades.
- **yfinance fails for 1 ticker**: ✅ Tests are `module`-scoped — if 4 of 5 tickers downloaded, tests still run on available data. Test 1 would fail (lists missing tickers explicitly).
- **Cache already exists**: ✅ `prepare.py` is idempotent — `process_ticker` skips existing files.
- **Parquet schema mismatch**: ✅ Test 2 catches missing columns; Test 3 catches wrong index type.
- **train.py crash during E2E**: ✅ Test 4 captures stderr and fails with full output for diagnosis.

### Test Automation Summary

| | Count | % |
|---|---|---|
| ✅ Backend (pytest — unit) | 78 | 91% |
| ✅ Backend (pytest — integration) | 8 | 9% |
| ⚠️ Manual | 0 | 0% |
| **Total** | 86 | 100% |

**Goal**: 100% path coverage. No manual tests — all Phase 5 tasks are automatable.

---

## VALIDATION COMMANDS

### Level 1: Syntax & Imports

```bash
# Both files must import cleanly
python -c "import prepare; import tests.test_e2e; print('imports OK')"
# TICKERS must be set
python -c "import prepare; assert prepare.TICKERS == ['AAPL','MSFT','NVDA','JPM','TSLA']"
```

### Level 2: Unit Tests (pre-existing — zero regressions)

```bash
uv run pytest tests/ --ignore=tests/test_e2e.py -v --tb=short
# Expected: 78 passed, 0 failed
```

### Level 3: Data Download + Schema Validation

```bash
uv run prepare.py
# Expected: "Done: 5/5 tickers cached successfully."

ls ~/.cache/autoresearch/stock_data/*.parquet | wc -l
# Expected: 5

python -c "
import pandas as pd, pathlib, datetime
for ticker in ['AAPL','MSFT','NVDA','JPM','TSLA']:
    p = pathlib.Path.home() / f'.cache/autoresearch/stock_data/{ticker}.parquet'
    df = pd.read_parquet(p)
    cols = set(df.columns)
    assert {'open','high','low','close','volume','price_10am'} <= cols, f'{ticker}: missing cols'
    assert type(df.index[0]) is datetime.date, f'{ticker}: wrong index type'
    assert len(df) >= 200, f'{ticker}: insufficient rows ({len(df)})'
    print(f'{ticker}: OK ({len(df)} rows)')
"
```

### Level 4: Integration Tests

```bash
uv run pytest tests/test_e2e.py -m integration -v --tb=short
# Expected: 8 passed, 0 failed, 0 errors (skips only if data missing)

# Full suite (all 86 tests)
uv run pytest tests/ -v --tb=short 2>&1 | tail -10
# Expected: 86 passed, 0 failed
```

### Level 5: Manual Smoke Check (10 seconds)

```bash
uv run train.py 2>&1
# Expected output ends with:
# ---
# sharpe:              X.XXXXXX
# total_trades:        N
# win_rate:            X.XXX
# avg_pnl_per_trade:   XX.XX
# total_pnl:           XXX.XX
# backtest_start:      2026-01-01
# backtest_end:        2026-03-01
```

---

## ACCEPTANCE CRITERIA

### Functional
- [ ] `prepare.py` has `TICKERS = ["AAPL", "MSFT", "NVDA", "JPM", "TSLA"]` (not the empty placeholder)
- [ ] `uv run prepare.py` exits code 0 and prints "Done: 5/5 tickers cached successfully."
- [ ] Each of the 5 parquet files contains all 6 required columns: `open, high, low, close, volume, price_10am`
- [ ] Each parquet file has a `datetime.date` index (not `pd.Timestamp`) and ≥ 200 rows
- [ ] `uv run train.py` exits code 0
- [ ] `train.py` stdout contains exactly one line starting with `sharpe:` followed by a parseable, finite float
- [ ] `train.py` stdout contains all 7 required output block fields (`sharpe:`, `total_trades:`, `win_rate:`, `avg_pnl_per_trade:`, `total_pnl:`, `backtest_start:`, `backtest_end:`)

### Edge Cases / Correctness
- [ ] P&L self-consistency: when trades = 0, `total_pnl == 0.0` and `avg_pnl_per_trade == 0.0`; when trades > 0, `|total_pnl − avg_pnl_per_trade × trades| ≤ $0.05 × trades`
- [ ] `screen_day(hist, today)` does not raise an exception on the last 10 real trading days of any parquet file — resolves Criterion 18 (previously UNVERIFIABLE)

### Integration / E2E
- [ ] Relaxed screener (CCI threshold −30) produces ≥ 1 trade across the 5 tickers over the 42-day window — proves agent loop has a viable starting path
- [ ] `tests/test_e2e.py` exists and `pytest --collect-only` finds exactly 8 tests in it
- [ ] All 8 integration tests in `tests/test_e2e.py` pass when the Parquet cache is populated

### Validation
- [ ] All 78 pre-existing tests still pass after Phase 5 changes — verified by: `uv run pytest tests/ --ignore=tests/test_e2e.py -v`

### Out of Scope
- Extending the backtest date window beyond Jan 1 – Mar 1, 2026 — not required for Phase 5
- Committing any changes to git — changes must remain unstaged
- Modifying `train.py` strategy code — only `prepare.py` and `tests/test_e2e.py` are created/changed
- Creating `results.tsv` — that happens during the autonomous agent loop, not Phase 5

---

## COMPLETION CHECKLIST

- [ ] Task 1.1 complete: `prepare.py` TICKERS updated to 5-stock list
- [ ] Task 1.2 complete: `tests/test_e2e.py` created with 8 integration tests
- [ ] Task 2.1 complete: `uv run prepare.py` ran, 5 parquet files present
- [ ] Level 1 validation passed: imports OK, TICKERS correct
- [ ] Level 2 validation passed: 78 pre-existing tests still passing
- [ ] Level 3 validation passed: data schema correct for all 5 tickers
- [ ] Level 4 validation passed: 8 integration tests passing
- [ ] Level 5 validation passed: manual train.py smoke check passed
- [ ] All acceptance criteria met
- [ ] **⚠️ Debug logs added during execution REMOVED (keep pre-existing)**
- [ ] **⚠️ CRITICAL: Changes UNSTAGED — NOT committed**

---

## NOTES

### On the "Manually verify P&L" PRD task

The PRD says "Manually verify one trade's P&L calculation is correct." This plan automates that verification via `test_pnl_self_consistency`: it checks that `total_pnl ≈ avg_pnl_per_trade × total_trades` within a per-trade rounding tolerance. Per-trade arithmetic is already unit-tested in `tests/test_backtester.py::test_run_backtest_stop_hit_closes_position` (stop hit: `pnl = (stop_price - entry_price) × shares`) and `test_run_backtest_end_of_backtest_closes_all_positions` (end-of-period close). Phase 5 exercises those code paths on real data via the self-consistency check.

### On zero trades with the strict screener

The screener's 11 rules were designed for a 6–12 month backtest. Over only 42 trading days (Jan 1 – Mar 1, 2026), it's likely that 0 signals fire for all 5 tickers. This is expected and documented in the PRD. The plan handles this via Test 8: even if the strict screener produces 0 trades, the relaxed version (CCI -30) must produce ≥ 1, proving the agent loop has a viable starting point.

### On the yfinance 730-day limit

`HISTORY_START` is now set to `BACKTEST_START - 1 year = 2025-01-01`, which is ~443 days from today (2026-03-19). This is well within the 730-day rolling window. Fixed in Phase 2 (see PROGRESS.md §HISTORY_START Fix).

### On idempotency

If `uv run prepare.py` is run twice, the second run will print "already cached, skipping" for all 5 tickers and exit 0. This is safe. Integration tests that rely on the cache will pass on both the first and subsequent runs.

### On the DO NOT EDIT harness

`test_harness_below_do_not_edit_is_unchanged` in `test_optimization.py` uses a SHA-256 hash of the harness section. Executing this plan does NOT modify `train.py` at all, so the hash remains valid. If the execution agent accidentally modifies `run_backtest()`, this test will catch it immediately.

### On Criterion 18 (Screener AC)

Test 6 (`test_screen_day_on_real_parquet_data`) directly resolves the UNVERIFIABLE status of Criterion 18 from the screener acceptance criteria. After Phase 5, Criterion 18 should be marked PASS in PROGRESS.md.
