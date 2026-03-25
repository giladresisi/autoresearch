# Feature: Phase 1 — Pre-Market Signal CLI

**⚠️ EXECUTION RULES — READ FIRST:**
- Implement ALL changes required by this plan
- Delete debug logs added during execution (keep pre-existing ones)
- Leave ALL changes UNSTAGED — do NOT run `git add` or `git commit`
- Only make code changes — no git operations

Validate documentation and codebase patterns before implementing. Match naming of existing utils, types, and models. Import from correct files.

---

## Feature Description

Implement the Phase 1 live trading infrastructure: four scripts that give the user pre-market BUY signals and RAISE-STOP signals from the terminal, using the strategy already baked into `train.py`. No automation, VPS, or messaging — pure local CLI.

Deliverables:
1. `train.py` — small interface change: `current_price` param on `screen_day()`, plus `rsi14`/`res_atr` in return dict
2. `screener_prepare.py` — downloads last 90 days of OHLCV for the live ticker universe into a dedicated `SCREENER_CACHE_DIR`
3. `screener.py` — reads cache, fetches pre-market prices, runs `screen_day()`, prints armed candidates with gap warning
4. `analyze_gaps.py` — one-off analysis: reads `trades.tsv` to find gap-vs-pnl breakpoint for configuring the gap filter
5. `position_monitor.py` — reads `portfolio.json`, fetches current prices, prints RAISE-STOP signals
6. `portfolio.json` — template file the user edits manually after each trade

## User Story

As a trader
I want to run `uv run screener.py` each morning before market open
So that I see which tickers from my live universe have armed BUY signals, and can run `uv run position_monitor.py` to check whether my open positions need a stop adjustment

## Problem Statement

The strategy lives in `train.py` but has no interface to the live market. Pre-market, there's no way to know which tickers are close to breaking out, or whether open positions have crossed the stop-raise thresholds.

## Solution Statement

Wire `train.py`'s `screen_day()` and `manage_position()` to live yfinance data via two thin scripts. A separate `screener_prepare.py` builds and maintains a dedicated parquet cache for the larger live ticker universe, isolated from the optimization harness cache.

## Feature Metadata

**Feature Type**: New Capability
**Complexity**: Medium
**Primary Systems Affected**: `train.py` (minor interface addition), four new scripts
**Dependencies**: `yfinance`, `pandas`, `numpy` (all already in environment)
**Breaking Changes**: No — `screen_day()` gets `current_price=None` default; callers passing only `(df, today)` are unchanged

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ BEFORE IMPLEMENTING

- `train.py` (lines 224–335) — `screen_day()` signature, return dict, and `rsi`/`res_atr` locals to expose
- `train.py` (lines 338–373) — `manage_position()` signature and logic
- `train.py` (lines 11–16) — `CACHE_DIR` env-var pattern to mirror for `SCREENER_CACHE_DIR`
- `prepare.py` (lines 99–157) — `CACHE_DIR`, `download_ticker()`, `resample_to_daily()` — reuse directly
- `example-screener.py` (lines 33–85) — `fetch_universe()` for S&P 500 + Russell 1000 dynamic fetch with fallback
- `tests/test_screener.py` — existing test fixtures (`make_signal_df`, `make_pivot_signal_df`) to reuse in new tests
- `tests/conftest.py` — session-scoped fixture pattern; `SCREENER_CACHE_DIR` tests need same style

### New Files to Create

- `screener_prepare.py` — initial build + incremental refresh of SCREENER_CACHE_DIR
- `screener.py` — pre-market BUY signal scanner
- `analyze_gaps.py` — gap-vs-pnl analysis from trades.tsv
- `position_monitor.py` — RAISE-STOP signal scanner
- `portfolio.json` — user-maintained portfolio template
- `tests/test_screener_prepare.py` — unit tests for screener_prepare logic
- `tests/test_screener_script.py` — unit tests for screener.py logic
- `tests/test_position_monitor.py` — unit tests for position_monitor.py
- `tests/test_analyze_gaps.py` — unit tests for analyze_gaps.py

### Patterns to Follow

**CACHE_DIR env-var pattern** (`train.py` line 13):
```python
SCREENER_CACHE_DIR = os.environ.get(
    "AUTORESEARCH_SCREENER_CACHE_DIR",
    os.path.join(os.path.expanduser("~"), ".cache", "autoresearch", "screener_data"),
)
```

**resample_to_daily reuse** (`prepare.py` line 120): import and call directly — do not duplicate.

**Output is silent** (CLAUDE.md): no print() in library/import paths; print only in `if __name__ == "__main__"` or top-level script body.

**Stale-cache check**: if the most-recent parquet row is > 2 calendar days before today, print a warning line before scanning. Do not abort — warn and continue.

---

## PARALLEL EXECUTION STRATEGY

### Dependency Graph

```
┌────────────────────────────────────────────────────────────────┐
│ WAVE 1: Foundation (all parallel)                              │
├──────────────────────┬─────────────────────┬───────────────────┤
│ Task 1.1             │ Task 1.2             │ Task 1.3          │
│ UPDATE train.py      │ CREATE               │ CREATE            │
│ screen_day interface │ screener_prepare.py  │ analyze_gaps.py   │
│                      ├─────────────────────┤                   │
│                      │ Task 1.4             │                   │
│                      │ CREATE portfolio.json│                   │
└──────────────────────┴─────────────────────┴───────────────────┘
                            ↓
┌────────────────────────────────────────────────────────────────┐
│ WAVE 2: Scripts (parallel, after Wave 1)                       │
├───────────────────────────────┬────────────────────────────────┤
│ Task 2.1                      │ Task 2.2                       │
│ CREATE screener.py            │ CREATE position_monitor.py     │
│ Deps: 1.1, 1.2                │ Deps: 1.1, 1.2                 │
└───────────────────────────────┴────────────────────────────────┘
                            ↓
┌────────────────────────────────────────────────────────────────┐
│ WAVE 3: Tests (after Wave 2)                                   │
├────────────────────────────────────────────────────────────────┤
│ Task 3.1: Write all tests                                      │
└────────────────────────────────────────────────────────────────┘
```

### Parallelization Summary

**Wave 1 — Fully Parallel**: Tasks 1.1, 1.2, 1.3, 1.4 — no dependencies between them
**Wave 2 — Parallel after Wave 1**: Tasks 2.1, 2.2 — both need 1.1 + 1.2
**Wave 3 — Sequential**: Task 3.1 — needs all prior waves

### Interface Contracts

**Contract 1**: Task 1.1 provides `screen_day(df, today, current_price=None)` returning dict with `rsi14` and `res_atr` keys → Task 2.1 (screener.py) consumes.

**Contract 2**: Task 1.2 provides `SCREENER_CACHE_DIR` constant and idempotent download logic → Tasks 2.1 and 2.2 consume by reading parquets from that path.

**Contract 3**: Task 1.4 provides `portfolio.json` schema → Task 2.2 (position_monitor.py) parses it.

### Synchronization Checkpoints

**After Wave 1**: `cd C:/Users/gilad/projects/auto-co-trader/auto-co-trader-main && python -m pytest tests/test_screener.py -x -q`
**After Wave 2**: `python -m pytest tests/ -x -q -k "not integration"`
**After Wave 3**: `python -m pytest tests/ -q`

---

## IMPLEMENTATION PLAN

### Phase 1: Foundation

#### Task 1.1: UPDATE `train.py` — `screen_day()` interface + return dict

**Purpose**: Enable live use of `screen_day()` by accepting an injected current price, and expose `rsi14`/`res_atr` in the return dict for screener display.

**Steps**:
1. Change signature: `def screen_day(df: pd.DataFrame, today, current_price: float | None = None) -> "dict | None":`
2. Replace the single `price_1030am = float(df['price_1030am'].iloc[-1])` line with:
   ```python
   price_1030am = current_price if current_price is not None else float(df['price_1030am'].iloc[-1])
   ```
3. In the return dict, add:
   ```python
   'rsi14':   round(rsi, 4),
   'res_atr': round(res_atr, 2) if res_atr is not None else None,
   ```
   Note: `rsi` is already computed earlier in the function; `res_atr` is already computed. Just add them to the dict.
4. Update the docstring to document the new parameter.

**Validation**: `python -m pytest tests/test_screener.py -x -q` — all existing tests must still pass (current_price=None is backwards-compatible).

#### Task 1.2: CREATE `screener_prepare.py`

**Purpose**: Builds and incrementally refreshes the screener parquet cache (SCREENER_CACHE_DIR) — separate from the harness cache.

**Structure**:
```python
"""
screener_prepare.py — Build and refresh the screener parquet cache.

Downloads last 90 days of 1h OHLCV from yfinance for the live ticker universe
(S&P 500 + Russell 1000) and resamples to daily using prepare.resample_to_daily().
Idempotent: skips tickers whose parquet already exists and is current (last row = yesterday).

Usage:
    uv run screener_prepare.py

Override cache path:
    AUTORESEARCH_SCREENER_CACHE_DIR=/path/to/dir uv run screener_prepare.py
"""
```

**Key constants and logic**:
```python
import datetime, os, sys, warnings
import pandas as pd
import yfinance as yf
from prepare import resample_to_daily

SCREENER_CACHE_DIR = os.environ.get(
    "AUTORESEARCH_SCREENER_CACHE_DIR",
    os.path.join(os.path.expanduser("~"), ".cache", "autoresearch", "screener_data"),
)
HISTORY_DAYS = 90   # trading days of data to maintain

def fetch_screener_universe() -> list[str]:
    """Fetch S&P 500 (Wikipedia) + Russell 1000 (iShares IWB). Falls back to minimal list."""
    # Same logic as example-screener.py fetch_universe(), but returns deduplicated list
    # Fallback: hardcoded ~20 liquid names covering major sectors

def is_ticker_current(ticker: str) -> bool:
    """Return True if cache parquet exists and last row is from yesterday or later."""

def download_and_cache(ticker: str, history_start: str) -> None:
    """Download, resample, write parquet. Skip silently on error."""
```

**Behavior at runtime**:
1. Fetch universe (S&P 500 + Russell 1000 via Wikipedia/iShares, fallback to minimal list)
2. `os.makedirs(SCREENER_CACHE_DIR, exist_ok=True)`
3. For each ticker: skip if `is_ticker_current()`; otherwise download 90 days and write
4. Print progress: `[n/total] AAPL — cached` / `SKIP (current)` / `FAIL (error)`

**Validation**: `python screener_prepare.py --help` (or just run it; if no --help flag, it should print usage on import). Actually, test by running: `AUTORESEARCH_SCREENER_CACHE_DIR=/tmp/sp_test python screener_prepare.py` and verifying parquets appear.

#### Task 1.3: CREATE `analyze_gaps.py`

**Purpose**: One-off analysis script — reads `trades.tsv` and harness parquets, computes gap vs pnl stats to guide setting GAP_THRESHOLD in screener.py.

**Structure**:
```python
"""
analyze_gaps.py — Gap-vs-PnL analysis for gap filter threshold calibration.

For each trade in trades.tsv:
  1. Load ticker parquet from CACHE_DIR (harness cache, not screener cache)
  2. Find the close on entry_date - 1 trading day (prev_close)
  3. Compute gap = (entry_price - prev_close) / prev_close

Outputs:
  - Gap distribution for winners vs losers
  - Fraction of losers with gap < -1%, -2%, -3%, -4%
  - Fraction of winners excluded at each threshold
  - Recommended threshold (largest negative gap with < 10% winner exclusion rate)

Usage:
    uv run analyze_gaps.py [--trades trades.tsv] [--cache-dir PATH]
"""
```

**Key output** (printed to stdout):
```
Gap distribution (winners):  mean= X%  median= Y%
Gap distribution (losers):   mean= X%  median= Y%

Threshold analysis:
  gap < -1%:  removes  N losers (XX%) |  M winners (YY%)
  gap < -2%:  removes  N losers (XX%) |  M winners (YY%)
  gap < -3%:  removes  N losers (XX%) |  M winners (YY%)
  gap < -4%:  removes  N losers (XX%) |  M winners (YY%)

Recommended: GAP_THRESHOLD = -X% (removes XX% of losers, YY% of winners)
```

**Validation**: `python analyze_gaps.py` — runs without error when `trades.tsv` is present in CWD.

#### Task 1.4: CREATE `portfolio.json` template

**Purpose**: Provide the user with a ready-to-edit portfolio file.

**Content**:
```json
{
  "positions": [
    {
      "ticker":       "AAPL",
      "entry_price":  182.50,
      "entry_date":   "2026-03-10",
      "shares":       10,
      "stop_price":   174.20,
      "notes":        "optional free text"
    }
  ]
}
```

No code required. Commit a template with one example position.

### Phase 2: Core Scripts

#### Task 2.1: CREATE `screener.py`

**Purpose**: Pre-market BUY signal scanner. Reads SCREENER_CACHE_DIR, fetches live prices, runs `screen_day()`, prints armed candidates.

**Structure**:
```python
"""
screener.py — Pre-market BUY signal scanner.

Reads all parquets from SCREENER_CACHE_DIR, fetches pre-market last_price from
yfinance for each ticker, appends a synthetic today row, and calls screen_day()
from train.py. Prints armed candidates sorted by prev_vol_ratio descending.

Gap filter: candidates with gap_pct < GAP_THRESHOLD are shown but NOT armed.
Set GAP_THRESHOLD after running analyze_gaps.py.

Usage:
    uv run screener.py

Override cache path:
    AUTORESEARCH_SCREENER_CACHE_DIR=/path/to/dir uv run screener.py
"""
```

**Key constants**:
```python
GAP_THRESHOLD = -0.03   # -3% — update after running analyze_gaps.py
```

**Behavior**:
1. Check staleness: if max last-row-date across parquets < today - 2 calendar days, print warning
2. For each parquet in SCREENER_CACHE_DIR:
   a. Load df
   b. Fetch `current_price = yf.Ticker(t).fast_info.get('last_price', None)`; fall back to `df['close'].iloc[-1]` if NaN/unavailable
   c. Compute `gap_pct = (current_price - df['close'].iloc[-1]) / df['close'].iloc[-1]` — this is pre-market gap vs prev close
   d. Compute `days_to_earnings` from `next_earnings_date` column if present, else None
   e. Build synthetic today row: `{'price_1030am': current_price, 'open': current_price, 'high': current_price, 'low': current_price, 'close': current_price, 'volume': 0}` — append to df
   f. Call `signal = screen_day(df, today, current_price=current_price)`
   g. If signal is None: skip
   h. If `gap_pct < GAP_THRESHOLD`: add to output as `SKIP (gap)` row
   i. Otherwise: add to armed list
3. Print armed candidates table, sorted by `prev_vol_ratio` descending
4. After table, print any skipped-by-gap candidates separately

**Output columns**: ticker, current_price, entry_threshold (high20 + 0.01), suggested_stop, atr14, rsi14, prev_vol_ratio, vol_trend_ratio, gap_pct, res_atr, days_to_earnings

**Note on `days_to_earnings`**: compute from `df['next_earnings_date'].iloc[-2]` (yesterday, since today is synthetic). If column absent or NaT, show `None`.

**Validation**: `python screener.py --help` (or: `AUTORESEARCH_SCREENER_CACHE_DIR=/tmp/sp_test python screener.py` after screener_prepare ran).

#### Task 2.2: CREATE `position_monitor.py`

**Purpose**: RAISE-STOP signal scanner for open positions.

**Structure**:
```python
"""
position_monitor.py — RAISE-STOP signal scanner for open positions.

Reads portfolio.json, loads each ticker's parquet from SCREENER_CACHE_DIR,
appends a synthetic today row with current price, and calls manage_position()
from train.py. Prints signals where new_stop > current stop_price.

No writes — user updates portfolio.json manually after confirming.

Usage:
    uv run position_monitor.py [--portfolio portfolio.json]
"""
```

**Behavior**:
1. Load `portfolio.json` (default path; override via `--portfolio` flag or env)
2. For each position:
   a. Load parquet from SCREENER_CACHE_DIR; skip with warning if missing
   b. Fetch `current_price = yf.Ticker(t).fast_info.get('last_price', None)` → fall back to parquet close
   c. Append synthetic today row (same pattern as screener.py)
   d. Build position dict compatible with `manage_position()`:
      ```python
      pos = {
          'entry_price': position['entry_price'],
          'entry_date':  date.fromisoformat(position['entry_date']),
          'stop_price':  position['stop_price'],
          'shares':      position['shares'],
      }
      ```
   e. Call `new_stop = manage_position(pos, df)`
   f. If `new_stop > position['stop_price']`: print RAISE-STOP signal
3. If no signals: print `No stop-raise signals for [N] open positions.`

**Output per signal**:
```
RAISE-STOP  AAPL  current=$185.20  stop: $174.20 → $180.50  (+$6.30)  reason: trail
```

**Validation**: `python position_monitor.py` with the template portfolio.json (single example position — will print `No stop-raise signals` since it's a placeholder).

### Phase 3: Tests

#### Task 3.1: Write all tests

**Files to create/update**:

**Update `tests/test_screener.py`** — add 5 tests for the new `screen_day()` interface:
- `test_screen_day_current_price_overrides_df_value` — pass `current_price=150.0` where df has `price_1030am=90.0`; verify entry_price in result is 150.0 or None is returned based on filter conditions
- `test_screen_day_current_price_none_uses_df` — explicit None uses df as before (existing make_pivot_signal_df)
- `test_screen_day_returns_rsi14_key` — signal dict has `rsi14` key, value is float in (0, 100)
- `test_screen_day_returns_res_atr_key` — signal dict has `res_atr` key, value is float or None
- `test_screen_day_backtest_unchanged` — calling `screen_day(df, today)` (no 3rd arg) gives same result as before

**Create `tests/test_screener_prepare.py`**:
- `test_is_ticker_current_false_if_no_file` — missing parquet → not current
- `test_is_ticker_current_false_if_stale` — parquet with last row 5 days ago → not current
- `test_is_ticker_current_true_if_yesterday` — last row = yesterday → current
- `test_download_and_cache_creates_parquet` — with mocked yfinance, writes parquet to SCREENER_CACHE_DIR
- `test_download_and_cache_output_has_price_1030am` — output df has `price_1030am` column (resample_to_daily was used)
- `test_screener_prepare_main_skips_current_tickers` — integration: main() called with a pre-existing current parquet; ticker is skipped

**Create `tests/test_screener_script.py`**:
- `test_screener_loads_from_screener_cache_dir` — with monkeypatched SCREENER_CACHE_DIR, loads all parquets
- `test_screener_stale_cache_prints_warning` — parquets with old last row → warning line in captured output
- `test_screener_falls_back_to_close_on_nan_last_price` — when fast_info['last_price'] is NaN, uses close
- `test_screener_gap_pct_computed_correctly` — gap_pct = (current - prev_close) / prev_close
- `test_screener_armed_list_sorted_by_prev_vol_ratio` — candidates in descending order
- `test_screener_gap_below_threshold_not_armed` — candidate with gap_pct < GAP_THRESHOLD not in armed output
- `test_screener_output_has_required_columns` — all required columns present in output rows
- `test_screener_no_crash_on_empty_cache` — SCREENER_CACHE_DIR with no parquets prints "No tickers in cache" and exits 0
- `test_screener_runs_without_exception` — end-to-end with synthetic parquets in tmpdir (uses make_pivot_signal_df data)

**Create `tests/test_position_monitor.py`**:
- `test_reads_portfolio_json` — parses positions dict correctly
- `test_raise_stop_when_1_5_atr_profit` — with price 1.6 ATR above entry, output contains RAISE-STOP
- `test_no_output_when_stop_unchanged` — price at entry → manage_position returns same stop → no output
- `test_loads_from_screener_cache_dir_not_harness` — env-var for SCREENER_CACHE_DIR used, not CACHE_DIR
- `test_missing_ticker_skipped_with_warning` — ticker not in SCREENER_CACHE_DIR → warning printed, continues
- `test_empty_portfolio_prints_no_signals` — empty positions array → "No stop-raise signals for 0 open positions"
- `test_position_monitor_runs_without_exception` — full run with synthetic parquet and sample portfolio

**Create `tests/test_analyze_gaps.py`**:
- `test_gap_computed_from_prev_close` — for a trade at $100 entry where prev close = $98, gap = 2.04%
- `test_negative_gap_identified` — gap-down trade (entry < prev_close) gives negative gap_pct
- `test_missing_ticker_gracefully_skipped` — ticker not in CACHE_DIR → no crash
- `test_winner_loser_split_correct` — winners (pnl > 0) vs losers (pnl ≤ 0) counted correctly
- `test_threshold_analysis_output` — output contains threshold recommendation line

---

## STEP-BY-STEP TASKS

### WAVE 1: Foundation

#### Task 1.1: UPDATE `train.py` — `screen_day()` interface

- **WAVE**: 1
- **AGENT_ROLE**: backend-developer
- **DEPENDS_ON**: []
- **BLOCKS**: [2.1, 2.2, 3.1]
- **PROVIDES**: `screen_day(df, today, current_price=None)` — backcompat; return dict gains `rsi14`, `res_atr`
- **IMPLEMENT**:
  1. At `train.py` line 224: add `current_price: "float | None" = None` to signature
  2. At line 239: replace `price_1030am = float(df['price_1030am'].iloc[-1])` with the `current_price if ... else` form
  3. In return dict: add `'rsi14': round(rsi, 4)` and `'res_atr': round(res_atr, 2) if res_atr is not None else None`
  4. Update docstring
- **VALIDATE**: `python -m pytest tests/test_screener.py -x -q`

#### Task 1.2: CREATE `screener_prepare.py`

- **WAVE**: 1
- **AGENT_ROLE**: backend-developer
- **DEPENDS_ON**: []
- **BLOCKS**: [2.1, 2.2]
- **PROVIDES**: `SCREENER_CACHE_DIR` constant; `is_ticker_current()`; `download_and_cache()`; runnable `__main__` block
- **IMPLEMENT**: As described in Phase 1 section above. Import `resample_to_daily` from `prepare`. Reuse `fetch_universe()` pattern from `example-screener.py` lines 33–85 for dynamic universe, with a ~20-ticker fallback list.
- **PATTERN**: `prepare.py` lines 99–117 for CACHE_DIR env-var and download patterns
- **VALIDATE**: `python -c "import screener_prepare; print('OK')"` (syntax check)

#### Task 1.3: CREATE `analyze_gaps.py`

- **WAVE**: 1
- **AGENT_ROLE**: backend-developer
- **DEPENDS_ON**: []
- **BLOCKS**: []
- **PROVIDES**: One-off gap analysis script
- **IMPLEMENT**: As described in Phase 1 section above.
  - Reads `trades.tsv` (tab-separated; columns include at minimum: ticker, entry_date, entry_price, pnl)
  - For each trade: `prev_close = df.loc[prev_trading_day, 'close']`
  - Uses `CACHE_DIR` from `train.py` (harness cache), not SCREENER_CACHE_DIR
  - Computes thresholds at -1%, -2%, -3%, -4% and prints winner/loser exclusion rates
  - Recommends the most negative threshold that excludes < 10% of winners
- **VALIDATE**: `python -c "import analyze_gaps; print('OK')"`

#### Task 1.4: CREATE `portfolio.json`

- **WAVE**: 1
- **AGENT_ROLE**: backend-developer
- **DEPENDS_ON**: []
- **BLOCKS**: [2.2]
- **PROVIDES**: Template portfolio file
- **IMPLEMENT**: Write the JSON template shown in the spec. One example position (AAPL). Add a comment (in a `_comment` field) noting "Delete this example position before first use."
- **VALIDATE**: `python -c "import json; json.load(open('portfolio.json'))"` — valid JSON

**Wave 1 Checkpoint**: `python -m pytest tests/test_screener.py -x -q`

---

### WAVE 2: Core Scripts

#### Task 2.1: CREATE `screener.py`

- **WAVE**: 2
- **AGENT_ROLE**: backend-developer
- **DEPENDS_ON**: [1.1 (current_price interface), 1.2 (SCREENER_CACHE_DIR)]
- **BLOCKS**: [3.1]
- **USES_FROM_WAVE_1**: Task 1.1 provides `screen_day(..., current_price=...)` with `rsi14`/`res_atr`; Task 1.2 provides `SCREENER_CACHE_DIR`
- **IMPLEMENT**: As described in Phase 2 section above.
  - Import `SCREENER_CACHE_DIR` from `screener_prepare` (so the env-var logic is defined once)
  - Import `screen_day` from `train`
  - `GAP_THRESHOLD = -0.03` constant at top with comment
  - `days_to_earnings`: read from `df['next_earnings_date'].iloc[-2]` (pre-synthetic row); handle missing column
  - Output as aligned text table (no external table library); use `f"{val:>10.2f}"` style formatting
- **VALIDATE**: `python -c "import screener; print('OK')"`

#### Task 2.2: CREATE `position_monitor.py`

- **WAVE**: 2
- **AGENT_ROLE**: backend-developer
- **DEPENDS_ON**: [1.1 (manage_position), 1.2 (SCREENER_CACHE_DIR), 1.4 (portfolio.json schema)]
- **BLOCKS**: [3.1]
- **USES_FROM_WAVE_1**: Task 1.1 provides `manage_position()`; Task 1.2 provides `SCREENER_CACHE_DIR`; Task 1.4 defines portfolio schema
- **IMPLEMENT**: As described in Phase 2 section above.
  - Import `SCREENER_CACHE_DIR` from `screener_prepare`
  - Import `manage_position` from `train`
  - `--portfolio` CLI arg (argparse) with default `portfolio.json`
  - `entry_date` must be converted to `datetime.date` from ISO string before passing to `manage_position`
  - Detect reason for raise: `> entry + 2.0*atr` → `trail`; `> entry + 1.5*atr` → `breakeven`; else `time/stall`
- **VALIDATE**: `python position_monitor.py` (runs with template portfolio.json, prints "No stop-raise signals for 1 open position")

**Wave 2 Checkpoint**: `python -m pytest tests/ -x -q -k "not integration"`

---

### WAVE 3: Tests

#### Task 3.1: Write all tests

- **WAVE**: 3
- **AGENT_ROLE**: test-engineer
- **DEPENDS_ON**: [2.1, 2.2, 1.1, 1.2, 1.3]
- **PROVIDES**: Full test coverage for all new code
- **IMPLEMENT**: All test files described in the Testing Strategy section.
  - All tests use synthetic data (make_signal_df / make_pivot_signal_df fixtures) — no network calls
  - `SCREENER_CACHE_DIR` is always monkeypatched to a tmpdir in tests
  - yfinance calls are always mocked; never allow real network in unit tests
  - `conftest.py` additions: add a `screener_cache_tmpdir` fixture (function-scoped) that writes a few synthetic parquets and sets `AUTORESEARCH_SCREENER_CACHE_DIR`
- **VALIDATE**: `python -m pytest tests/ -q`

**Final Checkpoint**: `python -m pytest tests/ -q` — full suite green

---

## TESTING STRATEGY

All tests automated with pytest. No Playwright needed (no frontend). No manual tests required for unit/integration coverage.

### Unit Tests — `train.py` interface change

**Status**: ✅ Automated | **Tool**: pytest | **Location**: `tests/test_screener.py` | **Run**: `python -m pytest tests/test_screener.py -q`

Test cases:
- `test_screen_day_current_price_overrides_df_value` ✅ — synthetic df with price_1030am=90, call with current_price=115 (signal df values) → result is not None
- `test_screen_day_current_price_none_uses_df` ✅ — existing make_pivot_signal_df still passes without 3rd arg
- `test_screen_day_returns_rsi14_key` ✅ — result dict has 'rsi14' key, float in (0, 100)
- `test_screen_day_returns_res_atr_key` ✅ — result dict has 'res_atr' key, float or None
- `test_screen_day_backtest_unchanged` ✅ — result of `screen_day(df, today)` matches `screen_day(df, today, current_price=None)`

### Unit Tests — `screener_prepare.py`

**Status**: ✅ Automated | **Tool**: pytest | **Location**: `tests/test_screener_prepare.py` | **Run**: `python -m pytest tests/test_screener_prepare.py -q`

Test cases:
- `test_is_ticker_current_false_if_no_file` ✅
- `test_is_ticker_current_false_if_stale` ✅
- `test_is_ticker_current_true_if_yesterday` ✅
- `test_download_and_cache_creates_parquet` ✅ (mock yfinance)
- `test_download_and_cache_output_has_price_1030am` ✅ (mock yfinance)
- `test_screener_prepare_main_skips_current_tickers` ✅ (mock yfinance, pre-seed a current parquet)

### Unit Tests — `screener.py`

**Status**: ✅ Automated | **Tool**: pytest | **Location**: `tests/test_screener_script.py` | **Run**: `python -m pytest tests/test_screener_script.py -q`

Test cases:
- `test_screener_loads_from_screener_cache_dir` ✅
- `test_screener_stale_cache_prints_warning` ✅
- `test_screener_falls_back_to_close_on_nan_last_price` ✅
- `test_screener_gap_pct_computed_correctly` ✅
- `test_screener_armed_list_sorted_by_prev_vol_ratio` ✅
- `test_screener_gap_below_threshold_not_armed` ✅
- `test_screener_output_has_required_columns` ✅
- `test_screener_no_crash_on_empty_cache` ✅
- `test_screener_runs_without_exception` ✅ (synthetic parquets, mocked yfinance)

### Unit Tests — `position_monitor.py`

**Status**: ✅ Automated | **Tool**: pytest | **Location**: `tests/test_position_monitor.py` | **Run**: `python -m pytest tests/test_position_monitor.py -q`

Test cases:
- `test_reads_portfolio_json` ✅
- `test_raise_stop_when_1_5_atr_profit` ✅
- `test_no_output_when_stop_unchanged` ✅
- `test_loads_from_screener_cache_dir_not_harness` ✅
- `test_missing_ticker_skipped_with_warning` ✅
- `test_empty_portfolio_prints_no_signals` ✅
- `test_position_monitor_runs_without_exception` ✅

### Unit Tests — `analyze_gaps.py`

**Status**: ✅ Automated | **Tool**: pytest | **Location**: `tests/test_analyze_gaps.py` | **Run**: `python -m pytest tests/test_analyze_gaps.py -q`

Test cases:
- `test_gap_computed_from_prev_close` ✅
- `test_negative_gap_identified` ✅
- `test_missing_ticker_gracefully_skipped` ✅
- `test_winner_loser_split_correct` ✅
- `test_threshold_analysis_output` ✅

### Manual Tests

#### Manual Test 1: Pre-market live price verification

**Why Manual**: Requires the script to run during actual pre-market hours (4am–9:30am ET) with active pre-market trading in progress. Synthetic data cannot simulate the specific yfinance pre-market `last_price` behavior on a live connection at that time of day. The unit test mocks this path; actual pre-market behavior is verified by the user during first live use.

**Steps**:
1. On a trading morning (Mon–Fri), run `uv run screener.py` before 9:30am ET
2. Observe that `current_price` values in the output differ from the prior close (showing pre-market movement)
3. Pick one armed candidate; at 9:30am check whether its opening price crosses the `entry_threshold`

**Expected**: At least one ticker shows pre-market price; output renders without encoding errors; entry_threshold is correctly shown as `high20 + 0.01`

#### Manual Test 2: Validate `screener.py` runs without errors on fresh terminal

**Why Manual**: Requires an actual `SCREENER_CACHE_DIR` with a week or more of parquets. The first run on a new machine after `screener_prepare.py` is the definitive runnability test for the integrated system. Unit tests cover all logic; this validates the end-to-end script startup path.

**Steps**:
1. `uv run screener_prepare.py` (first run — downloads ~90 days for full universe)
2. `uv run screener.py`
3. Verify: no import errors, output table renders, columns present

**Expected**: Script completes; output contains header row; all columns present; no UnicodeEncodeError

### Script Deliverables Check

**`screener_prepare.py`**:
- ✅ Running `uv run screener_prepare.py` completes the setup phase without raising an exception (verified by: `python -c "import screener_prepare"` passes; test_screener_prepare_main_skips_current_tickers verifies main() flow)
- ✅ All user-visible output uses ASCII-safe characters (no Unicode in print statements; verified in test_screener_prepare.py by capturing stdout)

**`screener.py`**:
- ✅ Running `uv run screener.py` completes without exception against a populated SCREENER_CACHE_DIR (verified by test_screener_runs_without_exception)
- ✅ All user-visible output uses ASCII-safe characters (table formatting uses only ASCII box chars or plain text)

**`position_monitor.py`**:
- ✅ Running `uv run position_monitor.py` completes without exception with template portfolio.json (verified by test_position_monitor_runs_without_exception and manual validation step in Task 2.2)
- ✅ All user-visible output uses ASCII-safe characters

**`analyze_gaps.py`**:
- ✅ Running `uv run analyze_gaps.py` completes without exception when trades.tsv is present (verified by test_analyze_gaps.py)
- ✅ All user-visible output uses ASCII-safe characters

### Test Automation Summary

| | Count | % |
|---|---|---|
| ✅ Unit (pytest) | 34 | 94% |
| ⚠️ Manual | 2 | 6% |
| **Total** | 36 | 100% |

Manual justification:
- Test 1: Requires live pre-market yfinance data during actual market hours (4am–9:30am ET). Cannot be reproduced with mocks for the specific "pre-market price differs from close" behavior on a live connection.
- Test 2: Full integration startup on a real SCREENER_CACHE_DIR (after downloading ~1000 tickers). Logic is fully unit-tested; this verifies the installed environment.

---

## VALIDATION COMMANDS

### Level 1: Syntax

```bash
cd C:/Users/gilad/projects/auto-co-trader/auto-co-trader-main
python -c "import train, prepare, screener_prepare, screener, analyze_gaps, position_monitor; print('All imports OK')"
python -c "import json; json.load(open('portfolio.json')); print('portfolio.json valid')"
```

### Level 2: Unit Tests

```bash
python -m pytest tests/test_screener.py tests/test_screener_prepare.py tests/test_screener_script.py tests/test_position_monitor.py tests/test_analyze_gaps.py -v
```

### Level 3: Full Suite (no regressions)

```bash
python -m pytest tests/ -q
```

Expected: all existing tests pass + all new tests pass. Pre-existing skip (`test_most_recent_train_commit_modified_only_editable_section`) is acceptable.

### Level 4: Manual Validation

See Manual Tests 1 and 2 above. Run after Level 3 passes.

---

## ACCEPTANCE CRITERIA

- [ ] `screen_day(df, today)` (no 3rd arg) returns the same result as before — no regression
- [ ] `screen_day(df, today, current_price=X)` uses X as price_1030am for all filter/stop computations
- [ ] Signal dict contains `rsi14` (float, 0–100) and `res_atr` (float or None)
- [ ] `screener_prepare.py` creates parquets in SCREENER_CACHE_DIR, not AUTORESEARCH_CACHE_DIR
- [ ] `screener_prepare.py` skips tickers whose parquet has a last row from yesterday or later
- [ ] `screener.py` reads from SCREENER_CACHE_DIR (not harness CACHE_DIR)
- [ ] `screener.py` output includes all required columns: ticker, current_price, entry_threshold, suggested_stop, atr14, rsi14, prev_vol_ratio, vol_trend_ratio, gap_pct, res_atr, days_to_earnings
- [ ] `screener.py` candidates sorted by prev_vol_ratio descending
- [ ] `screener.py` prints stale-cache warning when last parquet row > 2 calendar days old
- [ ] `screener.py` gap < GAP_THRESHOLD candidates are not in the armed list
- [ ] `screener.py` falls back to close when fast_info last_price is NaN
- [ ] `position_monitor.py` reads from SCREENER_CACHE_DIR (not harness CACHE_DIR)
- [ ] `position_monitor.py` prints RAISE-STOP for a position 1.5+ ATR in profit
- [ ] `position_monitor.py` prints nothing (except summary line) when no stops need raising
- [ ] `analyze_gaps.py` computes gap = (entry_price − prev_close) / prev_close correctly
- [ ] `portfolio.json` is valid JSON and matches the schema in system_upgrade_phases.md
- [ ] All 34 automated tests pass
- [ ] Full test suite has no new failures

---

## COMPLETION CHECKLIST

- [ ] All tasks completed in wave order
- [ ] Each task validation passed
- [ ] All validation levels executed (1–4)
- [ ] All automated tests created and passing
- [ ] Manual tests documented with instructions
- [ ] Full test suite passes with no new failures
- [ ] All acceptance criteria met
- [ ] **⚠️ Debug logs added during execution REMOVED (keep pre-existing)**
- [ ] **⚠️ CRITICAL: Changes UNSTAGED — NOT committed**

---

## NOTES

**`analyze_gaps.py` trades.tsv dependency**: This script requires `trades.tsv` from a Phase 0 backtest run. If the file doesn't exist, the script should print a clear error (`trades.tsv not found — run train.py first`) and exit with code 1. Tests mock the file.

**`position_monitor.py` reason detection**: `manage_position()` returns only a float (the new stop), not the reason. The "reason" label in output is inferred: if `new_stop == price_1030am` (force-exit condition), reason is `time/stall`; if `new_stop == entry_price`, reason is `breakeven`; if `new_stop > entry_price`, reason is `trail`. This is a display heuristic, not authoritative.

**screener_prepare.py universe**: Use the `fetch_universe()` pattern from `example-screener.py` (Wikipedia S&P 500 + iShares Russell 1000 CSV). If both fetches fail, fall back to the 400-ticker list from `prepare.py`'s TICKERS constant rather than a minimal 10-ticker list. The prepare.py list is already in memory and representative.

**Import structure**: `screener.py` and `position_monitor.py` import `SCREENER_CACHE_DIR` from `screener_prepare` (single definition). They also import `screen_day`/`manage_position` from `train`. This keeps all env-var logic in one place and means updating the env-var default in `screener_prepare.py` propagates everywhere.

**No `__all__` needed**: These are scripts, not library modules. Import of symbols is fine without `__all__`.

**`screener_prepare.py` incremental cache update** (implemented post-plan): `download_and_cache()` reads the existing parquet (if any), computes `fetch_start = last_date - 1 day` (1-day overlap to refresh partial last-day data), fetches only the new window from yfinance, then merges old + new with `keep='last'` deduplication on date index. Falls back to a full `history_start` download if the existing parquet is unreadable. This means a morning run only downloads 1–2 days of data per ticker rather than 90 days. Tests: `test_download_and_cache_incremental_appends_new_rows`, `test_download_and_cache_deduplicates_overlap`, `test_download_and_cache_falls_back_to_full_on_corrupt_existing`.
