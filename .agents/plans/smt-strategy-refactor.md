# Feature: SMT Strategy Refactor (Bar Globals + exit_market + File Split)

**⚠️ EXECUTION RULES — READ FIRST:**
- Implement ALL changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
- Only make code changes — no git operations

Validate documentation and codebase patterns before implementing. Match naming of existing utils, types, and models. Import from correct files.

---

## Feature Description

Three sequential refactors to `train_smt.py` and its ecosystem, specified in `../smt-quality-params-apr17/refactor2.md`:

- **Phase 1**: Add module-level bar globals (`_mnq_bars`, `_mes_bars`) and a `set_bar_data()` setter so strategy functions can access full bar history without signature changes.
- **Phase 2**: Add `"exit_market"` as a new `manage_position()` return code and wire both callers (`run_backtest` and `signal_smt._process_managing`) to handle it — infrastructure only, no criteria yet.
- **Phase 3**: Split `train_smt.py` into `strategy_smt.py` (mutable strategy layer) and `backtest_smt.py` (frozen harness). Update all importers. Pure mechanical restructuring — zero logic changes.

## User Story

As an SMT strategy developer  
I want strategy functions to have structured access to bar history, a clean exit path for deliberate closes, and a structural file boundary between strategy logic and harness  
So that future criteria can be added as one-line changes and the optimizer agent can safely edit strategy code without touching the frozen harness

## Feature Metadata

**Feature Type**: Refactor  
**Complexity**: Medium  
**Primary Systems Affected**: `train_smt.py`, `signal_smt.py`, `diagnose_bar_resolution.py`, test files, `program_smt.md`  
**Dependencies**: None (no new packages)  
**Breaking Changes**: Yes — Phase 3 renames `train_smt`; all importers must be updated atomically.

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ BEFORE IMPLEMENTING

- `train_smt.py` L1–186 — strategy constants (→ `strategy_smt.py` in Phase 3)
- `train_smt.py` L188–650 — strategy functions incl. `manage_position()` (→ `strategy_smt.py`)
- `train_smt.py` L652–end — frozen harness: `run_backtest`, `_compute_metrics`, `print_results`, `__main__` (→ `backtest_smt.py`)
- `signal_smt.py` L18 — `from train_smt import screen_session, manage_position, compute_tdo`
- `signal_smt.py` L436–481 — `_process_managing()`: all `manage_position()` result branches
- `signal_smt.py` L266–312 — `on_mnq_1m_bar()` / `on_mes_1m_bar()`: where `set_bar_data()` calls go
- `diagnose_bar_resolution.py` L26 — `import train_smt` + uses `load_futures_data`, `run_backtest`, `BACKTEST_START/END`
- `tests/test_smt_strategy.py` L1–50 — import pattern: `import train_smt` inside each test function; `monkeypatch.setattr(train_smt, ...)` pattern
- `tests/test_smt_backtest.py` L1–30 — same dynamic import pattern
- `program_smt.md` — agent instructions; "DO NOT EDIT BELOW THIS LINE" convention to replace

### New Files to Create

- `strategy_smt.py` — all strategy constants + bar globals + all strategy functions
- `backtest_smt.py` — all harness code + window config + `__main__`

### Patterns to Follow

- Import inside test function body (do NOT change to top-level imports)
- `monkeypatch.setattr(train_smt, "CONSTANT", value)` → becomes `monkeypatch.setattr(strategy_smt, ...)` in Phase 3
- Result strings in `_process_managing`: `if result == "exit_tp": ...` — `"exit_market"` follows same pattern

---

## PARALLEL EXECUTION STRATEGY

Phases are strictly sequential. Within each phase, task pairs marked independent can be parallelized.

```
WAVE 1: Phase 1 implementation
  Task 1.1: Add globals + set_bar_data() to train_smt.py          [BLOCKER for 1.2, 1.3]
  Task 1.2: Wire set_bar_data() in signal_smt.py (after 1.1)      [parallel with 1.3]
  Task 1.3: Wire set_bar_data() in run_backtest() (after 1.1)     [parallel with 1.2]
WAVE 2: Phase 1 tests
  Task 1.4: Write + run Phase 1 tests
WAVE 3: Phase 2 implementation
  Task 2.1: Confirm run_backtest() handles exit_market (no change) [parallel with 2.2]
  Task 2.2: Add exit_market branch to signal_smt._process_managing [parallel with 2.1]
WAVE 4: Phase 2 tests
  Task 2.3: Write + run Phase 2 tests
WAVE 5: Phase 3 file split
  Task 3.1: Create strategy_smt.py                                 [parallel with 3.2]
  Task 3.2: Create backtest_smt.py                                 [parallel with 3.1]
  Task 3.3: Update all importers + test files (after 3.1, 3.2)
  Task 3.4: Remove train_smt.py (after 3.3)
  Task 3.5: Update program_smt.md (parallel with 3.3/3.4)
WAVE 6: Full regression
  Task 3.6: Full test suite
```

### Interface Contracts

- **Phase 1 → 1.2/1.3**: Task 1.1 provides `set_bar_data()` and globals in `train_smt`
- **Phase 2 → Phase 3**: Tasks 2.1/2.2 produce working `exit_market` path; Phase 3 copies code verbatim
- **Phase 3 3.1/3.2 → 3.3**: New files exist and import cleanly before updating importers

### Synchronization Checkpoints

- After Wave 2: `uv run pytest tests/test_smt_strategy.py tests/test_smt_backtest.py -x -q`
- After Wave 4: `uv run pytest tests/ -x -q`
- After Wave 6: `uv run pytest tests/ -x -q`

---

## IMPLEMENTATION PLAN

### Phase 1: Bar Globals

#### Task 1.1 — Add globals + set_bar_data() to train_smt.py

Insert immediately before `# ══ STRATEGY FUNCTIONS ═══` (after `MIN_SMT_MISS_PTS`):

```python
# ── Module-level bar data ─────────────────────────────────────────────────────
_mnq_bars: "pd.DataFrame | None" = None
_mes_bars: "pd.DataFrame | None" = None


def set_bar_data(mnq_df: pd.DataFrame, mes_df: pd.DataFrame) -> None:
    """Populate module-level bar globals for strategy functions that need lookback."""
    global _mnq_bars, _mes_bars
    _mnq_bars = mnq_df
    _mes_bars = mes_df
```

No changes to any existing function signatures. `manage_position()` signature stays `(position, current_bar)`.

#### Task 1.2 — Wire set_bar_data() in signal_smt.py

1. Add `set_bar_data` to the import on line 18:
   `from train_smt import screen_session, manage_position, compute_tdo, set_bar_data`

2. Add `set_bar_data(_mnq_1m_df, _mes_1m_df)` as the last statement of `on_mnq_1m_bar()` (after `_mnq_tick_bar = None`).

3. Same call at the end of `on_mes_1m_bar()` (after `_mes_tick_bar = None`).

#### Task 1.3 — Wire set_bar_data() in run_backtest()

Add `set_bar_data(mnq_df, mes_df)` as the first statement of the `run_backtest()` function body, before `start_dt = ...`.

#### Task 1.4 — Phase 1 unit tests (add to tests/test_smt_strategy.py)

```python
def test_set_bar_data_populates_globals():
    import train_smt
    mnq = _make_1m_bars([100]*3, [101]*3, [99]*3, [100]*3)
    mes = _make_1m_bars([50]*3, [51]*3, [49]*3, [50]*3)
    train_smt.set_bar_data(mnq, mes)
    assert train_smt._mnq_bars is mnq
    assert train_smt._mes_bars is mes

def test_set_bar_data_overwrites_previous():
    import train_smt
    df1 = _make_1m_bars([100]*2, [101]*2, [99]*2, [100]*2)
    df2 = _make_1m_bars([200]*2, [201]*2, [199]*2, [200]*2)
    train_smt.set_bar_data(df1, df1)
    train_smt.set_bar_data(df2, df2)
    assert train_smt._mnq_bars is df2

def test_run_backtest_calls_set_bar_data(monkeypatch):
    import train_smt
    calls = []
    monkeypatch.setattr(train_smt, "set_bar_data", lambda mnq, mes: calls.append((mnq, mes)))
    mnq = _make_1m_bars([100]*2, [101]*2, [99]*2, [100]*2)
    mes = _make_1m_bars([50]*2, [51]*2, [49]*2, [50]*2)
    monkeypatch.setattr(train_smt, "BACKTEST_START", "2025-01-02")
    monkeypatch.setattr(train_smt, "BACKTEST_END",   "2025-01-03")
    train_smt.run_backtest(mnq, mes)
    assert len(calls) == 1 and calls[0][0] is mnq
```

---

### Phase 2: exit_market Infrastructure

#### Task 2.1 — Confirm run_backtest() already handles exit_market

The `else` branch in `_build_trade_record` already uses `float(exit_bar["Close"])` for any result that is not `"exit_tp"` or `"exit_stop"` — this covers `"exit_market"` correctly. The re-entry eligibility check tuple `("exit_stop", "exit_time")` already excludes `"exit_market"`. **No code change required in `run_backtest()` or `_build_trade_record`.**

Add a short inline comment near the `else` branch: `# covers exit_time, session_close, exit_market, end_of_backtest`

#### Task 2.2 — Add exit_market branch to signal_smt._process_managing()

In `_process_managing()`, the current exit-code block is:
```python
if result == "exit_tp":
    exit_price = _position["take_profit"]
elif result == "exit_stop":
    exit_price = _position["stop_price"]
elif result != "exit_session_end":
    return  # unknown exit type
```

Insert new branch before the final `elif`:
```python
elif result == "exit_market":
    exit_price = float(bar.close)
```

#### Task 2.3 — Phase 2 unit tests

Add to `tests/test_smt_strategy.py`:
```python
def test_manage_position_does_not_return_exit_market_by_default():
    """exit_market must not fire without a criterion — infrastructure only."""
    import train_smt
    pos = {"direction": "short", "entry_price": 20100.0, "take_profit": 20000.0,
           "stop_price": 20150.0, "entry_time": pd.Timestamp("2025-01-02 09:05", tz="America/New_York")}
    bar = pd.Series({"Open": 20080.0, "High": 20090.0, "Low": 20070.0, "Close": 20080.0})
    assert train_smt.manage_position(pos, bar) != "exit_market"
```

Add to `tests/test_smt_backtest.py`:
```python
def test_run_backtest_exit_market_uses_bar_close(monkeypatch):
    """When manage_position returns exit_market, trade exit_price equals bar close."""
    import train_smt
    call_count = [0]
    orig = train_smt.manage_position
    def patched(position, bar):
        call_count[0] += 1
        return "exit_market" if call_count[0] == 1 else orig(position, bar)
    monkeypatch.setattr(train_smt, "manage_position", patched)
    mnq, mes = _build_short_signal_bars("2025-01-02"), _build_short_signal_bars("2025-01-02")
    monkeypatch.setattr(train_smt, "BACKTEST_START", "2025-01-02")
    monkeypatch.setattr(train_smt, "BACKTEST_END",   "2025-01-03")
    stats = train_smt.run_backtest(mnq, mes)
    market = [t for t in stats["trade_records"] if t["exit_type"] == "exit_market"]
    if market:
        assert market[0]["exit_price"] != market[0]["stop_price"]
        assert market[0]["exit_price"] != market[0]["tdo"]
```

---

### Phase 3: File Split

This phase is **pure mechanical restructuring — zero logic changes**. Read the full source before cutting.

#### Task 3.1 — Create strategy_smt.py

Contents (in order):
1. Module docstring: `"strategy_smt.py — SMT Divergence strategy constants and functions. Fully mutable — owned by the optimizing agent."`
2. Same imports as `train_smt.py` header
3. `FUTURES_CACHE_DIR` constant (same value as in `train_smt.py`)
4. All strategy constants from `SESSION_START` through `MIN_SMT_MISS_PTS`
5. Bar globals block + `set_bar_data()` added in Phase 1
6. All strategy functions: `_load_futures_manifest`, `load_futures_data`, `detect_smt_divergence`, `find_entry_bar`, `compute_tdo`, `print_direction_breakdown`, `find_anchor_close`, `is_confirmation_bar`, `screen_session`, `_build_signal_from_bar`, `manage_position`

**Not included**: `BACKTEST_START/END`, `TRAIN_END`, `TEST_START`, `SILENT_END`, `WALK_FORWARD_WINDOWS`, `FOLD_TEST_DAYS`, `FOLD_TRAIN_DAYS`, `WRITE_FINAL_OUTPUTS`, `PRINT_DIRECTION_BREAKDOWN`, manifest-loading try/except, and all harness functions.

No "DO NOT EDIT" boundary — the entire file is editable.

#### Task 3.2 — Create backtest_smt.py

Contents (in order):
1. Module docstring: `"backtest_smt.py — SMT Divergence backtest harness. Frozen — do not modify. Run: uv run python backtest_smt.py"`
2. Imports
3. Import from `strategy_smt` — all symbols the harness uses from strategy layer (see list below)
4. `FUTURES_CACHE_DIR`, `BACKTEST_START`, `BACKTEST_END`, `TRAIN_END`, `TEST_START`, `SILENT_END`, `WALK_FORWARD_WINDOWS`, `FOLD_TEST_DAYS`, `FOLD_TRAIN_DAYS`, `WRITE_FINAL_OUTPUTS`, `PRINT_DIRECTION_BREAKDOWN`, `MNQ_PNL_PER_POINT`
5. Manifest-loading `try/except` block
6. Deprecated compat constants: `BREAKEVEN_TRIGGER_PTS = 0.0`, `TRAIL_AFTER_BREAKEVEN_PTS = 0.0`
7. `RISK_PER_TRADE = 50.0`, `MAX_CONTRACTS = 4`
8. Harness functions verbatim: `_build_trade_record`, `_compute_fold_params`, `run_backtest`, `_compute_metrics`, `print_results`, `_write_results_tsv`
9. `__main__` block verbatim

**strategy_smt imports needed by backtest_smt**:
```python
from strategy_smt import (
    set_bar_data, load_futures_data, compute_tdo, find_anchor_close,
    is_confirmation_bar, detect_smt_divergence, _build_signal_from_bar,
    manage_position, print_direction_breakdown, screen_session,
    SESSION_START, SESSION_END, TRADE_DIRECTION, ALLOWED_WEEKDAYS,
    SIGNAL_BLACKOUT_START, SIGNAL_BLACKOUT_END, MIN_BARS_BEFORE_SIGNAL,
    REENTRY_MAX_MOVE_PTS, MIN_PRIOR_TRADE_BARS_HELD, MAX_HOLD_BARS,
    MAX_REENTRY_COUNT, BREAKEVEN_TRIGGER_PCT, TRAIL_AFTER_TP_PTS,
)
```

#### Task 3.3 — Update all importers and test files

| File | Change |
|---|---|
| `signal_smt.py` L18 | `from strategy_smt import screen_session, manage_position, compute_tdo, set_bar_data` |
| `diagnose_bar_resolution.py` L26 | `import backtest_smt as train_smt` (alias preserves all `.attribute` calls) |
| `tests/test_smt_strategy.py` | All `import train_smt` → `import strategy_smt as train_smt` (`replace_all=True`) |
| `tests/test_smt_backtest.py` | Read each test: patch targets for strategy constants → `strategy_smt`; for harness constants (`BACKTEST_START`, etc.) → `backtest_smt`. Use dual imports where needed. |

For `test_smt_backtest.py`, the safest approach per test function:
```python
import backtest_smt
import strategy_smt
monkeypatch.setattr(backtest_smt, "BACKTEST_START", "...")   # harness constant
monkeypatch.setattr(strategy_smt, "SESSION_START", "...")    # strategy constant
```

#### Task 3.4 — Remove train_smt.py

```bash
rm train_smt.py
```

Verify: `python -c "import train_smt"` → `ModuleNotFoundError`

#### Task 3.5 — Update program_smt.md

- Replace all `train_smt.py` run references with `backtest_smt.py`
- Replace "DO NOT EDIT BELOW THIS LINE" convention text with: "Edit only `strategy_smt.py`. Do not modify `backtest_smt.py`."
- Verify: `grep -n "train_smt\|DO NOT EDIT" program_smt.md` → 0 results

---

## STEP-BY-STEP TASKS

### WAVE 1

#### Task 1.1: ADD bar globals + set_bar_data() to train_smt.py
- **WAVE**: 1 | **DEPENDS_ON**: [] | **BLOCKS**: [1.2, 1.3, 1.4]
- **PROVIDES**: `_mnq_bars`, `_mes_bars`, `set_bar_data()` in `train_smt`
- **IMPLEMENT**: Insert code block before `# ══ STRATEGY FUNCTIONS ═══` (see Phase 1 > Task 1.1)
- **VALIDATE**: `python -c "import train_smt; print(train_smt._mnq_bars, callable(train_smt.set_bar_data))"`

#### Task 1.2: WIRE set_bar_data() in signal_smt.py
- **WAVE**: 1 (after 1.1) | **DEPENDS_ON**: [1.1] | **BLOCKS**: [1.4]
- **IMPLEMENT**: Update import; add call at end of `on_mnq_1m_bar()` and `on_mes_1m_bar()`
- **VALIDATE**: `python -c "import signal_smt"`

#### Task 1.3: WIRE set_bar_data() in run_backtest()
- **WAVE**: 1 (after 1.1) | **DEPENDS_ON**: [1.1] | **BLOCKS**: [1.4]
- **IMPLEMENT**: `set_bar_data(mnq_df, mes_df)` as first statement of `run_backtest()` body
- **VALIDATE**: `uv run python train_smt.py`

**Wave 1 Checkpoint**: `python -c "import train_smt; import signal_smt; print('OK')"`

### WAVE 2

#### Task 1.4: WRITE + RUN Phase 1 tests
- **WAVE**: 2 | **DEPENDS_ON**: [1.1, 1.2, 1.3] | **BLOCKS**: [2.1, 2.2]
- **IMPLEMENT**: Add 3 test functions to `tests/test_smt_strategy.py` (see Phase 1 > Task 1.4)
- **VALIDATE**: `uv run pytest tests/test_smt_strategy.py tests/test_smt_backtest.py -x -q`

### WAVE 3

#### Task 2.1: CONFIRM run_backtest() handles exit_market
- **WAVE**: 3 | **DEPENDS_ON**: [1.4] | **BLOCKS**: [2.3]
- **IMPLEMENT**: Add comment near `else` branch in `_build_trade_record`; no logic changes needed
- **VALIDATE**: `grep -n "exit_market\|exit_stop\|exit_time" train_smt.py`

#### Task 2.2: ADD exit_market branch to signal_smt._process_managing()
- **WAVE**: 3 | **DEPENDS_ON**: [1.4] | **BLOCKS**: [2.3]
- **IMPLEMENT**: Insert `elif result == "exit_market": exit_price = float(bar.close)` (see Phase 2 > Task 2.2)
- **VALIDATE**: `python -c "import signal_smt"`

**Wave 3 Checkpoint**: `uv run pytest tests/ -x -q`

### WAVE 4

#### Task 2.3: WRITE + RUN Phase 2 tests
- **WAVE**: 4 | **DEPENDS_ON**: [2.1, 2.2] | **BLOCKS**: [3.1, 3.2]
- **IMPLEMENT**: Add tests to `test_smt_strategy.py` and `test_smt_backtest.py` (see Phase 2 > Task 2.3)
- **VALIDATE**: `uv run pytest tests/ -q 2>&1 | tail -5`

### WAVE 5

#### Task 3.1: CREATE strategy_smt.py
- **WAVE**: 5 | **DEPENDS_ON**: [2.3] | **BLOCKS**: [3.3]
- **IMPLEMENT**: New file with all strategy content (see Phase 3 > Task 3.1)
- **VALIDATE**: `python -c "import strategy_smt; print(strategy_smt.SESSION_START, strategy_smt.set_bar_data)"`

#### Task 3.2: CREATE backtest_smt.py
- **WAVE**: 5 (parallel with 3.1) | **DEPENDS_ON**: [2.3] | **BLOCKS**: [3.3]
- **IMPLEMENT**: New file with harness content + import from strategy_smt (see Phase 3 > Task 3.2)
- **VALIDATE**: `python -c "import backtest_smt; print(backtest_smt.RISK_PER_TRADE)"` then `uv run python backtest_smt.py`

**Wave 5a Checkpoint**: `python -c "import strategy_smt; import backtest_smt; print('OK')"`

#### Task 3.3: UPDATE all importers and test files
- **WAVE**: 5 (after 3.1, 3.2) | **DEPENDS_ON**: [3.1, 3.2] | **BLOCKS**: [3.4]
- **IMPLEMENT**: Update `signal_smt.py`, `diagnose_bar_resolution.py`, `test_smt_strategy.py`, `test_smt_backtest.py` (see Phase 3 > Task 3.3 table)
- **VALIDATE**: `uv run pytest tests/test_smt_strategy.py tests/test_smt_backtest.py -x -q`

#### Task 3.4: REMOVE train_smt.py
- **WAVE**: 5 (after 3.3) | **DEPENDS_ON**: [3.3] | **BLOCKS**: [3.6]
- **IMPLEMENT**: `rm train_smt.py`
- **VALIDATE**: `python -c "import train_smt" 2>&1 | grep ModuleNotFoundError` then `uv run pytest tests/ -x -q`

#### Task 3.5: UPDATE program_smt.md
- **WAVE**: 5 (parallel with 3.3/3.4) | **DEPENDS_ON**: [3.1, 3.2] | **BLOCKS**: []
- **IMPLEMENT**: Replace train_smt references and "DO NOT EDIT" convention (see Phase 3 > Task 3.5)
- **VALIDATE**: `grep -n "train_smt\|DO NOT EDIT" program_smt.md | wc -l` → `0`

### WAVE 6

#### Task 3.6: RUN full test suite
- **WAVE**: 6 | **DEPENDS_ON**: [3.4, 3.5]
- **VALIDATE**: `uv run pytest tests/ -v --tb=short 2>&1 | tail -20` — 0 new failures

---

## TESTING STRATEGY

| Test | Tool | File | Status |
|---|---|---|---|
| `set_bar_data()` populates globals | pytest | `test_smt_strategy.py` | ✅ |
| `set_bar_data()` overwrites previous | pytest | `test_smt_strategy.py` | ✅ |
| `run_backtest()` calls `set_bar_data()` | pytest + monkeypatch | `test_smt_strategy.py` | ✅ |
| `manage_position()` never returns `exit_market` by default | pytest | `test_smt_strategy.py` | ✅ |
| `run_backtest()` records `exit_market` with close as exit_price | pytest + monkeypatch | `test_smt_backtest.py` | ✅ |
| `strategy_smt` + `backtest_smt` import cleanly | import check | — | ✅ |
| All existing strategy tests pass after rename | pytest | `test_smt_strategy.py` | ✅ |
| All existing backtest tests pass after rename | pytest | `test_smt_backtest.py` | ✅ |
| `signal_smt.py` imports cleanly | import check | — | ✅ |
| `diagnose_bar_resolution.py` imports cleanly | import check | — | ✅ |
| `exit_market` in `_process_managing()` reaches correct branch | pytest + monkeypatch | `test_signal_smt.py` | ✅ |

### Manual Tests

#### Manual Test 1: Live backtest output comparison

**Why Manual**: Requires `data/historical/` Databento parquets — not available in CI.  
**Steps**: `uv run python backtest_smt.py` after Phase 3 completes.  
**Expected**: `mean_test_pnl` and fold breakdown identical to last `uv run python train_smt.py` run (zero logic change).

### Test Automation Summary

| | Count | % |
|---|---|---|
| ✅ pytest automated | 11 | 92% |
| ⚠️ Manual (live data required) | 1 | 8% |
| **Total** | 12 | 100% |

---

## VALIDATION COMMANDS

### Level 1: Import Checks
```bash
python -c "import strategy_smt; print('strategy_smt OK')"
python -c "import backtest_smt; print('backtest_smt OK')"
python -c "import signal_smt; print('signal_smt OK')"
python -c "import diagnose_bar_resolution; print('diagnose_bar_resolution OK')"
```

### Level 2: Unit Tests
```bash
uv run pytest tests/test_smt_strategy.py -v -q
```

### Level 3: Integration Tests
```bash
uv run pytest tests/test_smt_backtest.py -v -q
uv run pytest tests/ -x -q
```

### Level 4: Manual Validation
```bash
uv run python backtest_smt.py   # compare output to pre-refactor baseline
```

---

## ACCEPTANCE CRITERIA

- [ ] `train_smt._mnq_bars` and `_mes_bars` exist as `None` at import time (Phase 1)
- [ ] `train_smt.set_bar_data(mnq, mes)` stores both DataFrames in the globals (Phase 1)
- [ ] `run_backtest()` calls `set_bar_data()` before the day loop (Phase 1)
- [ ] `signal_smt.py` calls `set_bar_data()` at end of both 1m bar callbacks (Phase 1)
- [ ] `manage_position()` signature unchanged — still `(position, current_bar)` (Phase 1)
- [ ] `manage_position()` does NOT return `"exit_market"` in any existing scenario (Phase 2)
- [ ] `run_backtest()` records `exit_market` trades with `exit_price = bar["Close"]` (Phase 2)
- [ ] `_process_managing()` handles `"exit_market"` with `exit_price = float(bar.close)` (Phase 2)
- [ ] `strategy_smt.py` exists with all strategy constants and functions (Phase 3)
- [ ] `backtest_smt.py` exists; `uv run python backtest_smt.py` runs without error (Phase 3)
- [ ] `train_smt.py` does not exist after Phase 3 (Phase 3)
- [ ] All importers updated: `signal_smt.py`, `diagnose_bar_resolution.py`, test files (Phase 3)
- [ ] `program_smt.md` references `strategy_smt.py`/`backtest_smt.py`; zero `train_smt` references (Phase 3)
- [ ] Full test suite passes with 0 new failures (all phases)

---

## COMPLETION CHECKLIST

- [ ] Phase 1: globals + set_bar_data() added; called in signal_smt.py and run_backtest()
- [ ] Phase 1: 3 unit tests passing
- [ ] Phase 2: _build_trade_record else branch commented; _process_managing exit_market branch added
- [ ] Phase 2: exit_market unit tests passing
- [ ] Phase 3: strategy_smt.py created; backtest_smt.py created
- [ ] Phase 3: all importers updated; train_smt.py removed; program_smt.md updated
- [ ] Full test suite passing
- [ ] **⚠️ Debug logs added during execution REMOVED**
- [ ] **⚠️ CRITICAL: Changes UNSTAGED — NOT committed**

---

## NOTES

**Phase ordering is mandatory.** Phase 3 is last because it is a pure rename — easy to review and bisect. Phases 1 and 2 add logic first so the rename is genuinely no-change.

**Alias pattern for tests.** `import strategy_smt as train_smt` inside test functions preserves all existing `train_smt.CONSTANT` call sites without touching test logic. For `test_smt_backtest.py`, use dual imports where patching targets span both modules.

**exit_market is infrastructure only.** Phase 2 adds zero new behaviour — `manage_position()` cannot return `"exit_market"` from any existing code path. Future criteria require only one return statement inside `manage_position()`.

**FUTURES_CACHE_DIR in both files.** Both `strategy_smt.py` and `backtest_smt.py` define it independently (same `os.environ.get` call). Simpler than re-exporting; value is always identical.
